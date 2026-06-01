"""Vercel serverless API — reads from Supabase cache only. No SmartLead calls."""

import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, request, jsonify, make_response, send_from_directory

app = Flask(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PUBLIC_DIR = os.path.join(_PROJECT_ROOT, "public")

DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")


def _check_auth():
    if not DASHBOARD_PASSWORD:
        return True
    if request.args.get("pw") == DASHBOARD_PASSWORD:
        return True
    if request.cookies.get("dashboard_pw", "") == DASHBOARD_PASSWORD:
        return True
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        from server_auth import verify_firebase_token
        try:
            if verify_firebase_token(auth[7:]):
                return True
        except Exception:
            pass
    return False


def _get_cache(key):
    import db as store
    try:
        data, updated_at = store.cache_get(key)
        return data, updated_at
    except Exception as e:
        return None, str(e)


def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return resp


@app.route("/")
def serve_index():
    return send_from_directory(_PUBLIC_DIR, "index.html")


@app.route("/css/<path:f>")
def serve_css(f):
    return send_from_directory(os.path.join(_PUBLIC_DIR, "css"), f)


@app.route("/js/<path:f>")
def serve_js(f):
    return send_from_directory(os.path.join(_PUBLIC_DIR, "js"), f)


@app.route("/favicon.ico")
def favicon():
    return "", 204


@app.route("/api/healthz")
def healthz():
    return "ok-v2-cache-readonly", 200


@app.route("/api/auth-check")
def auth_check():
    if _check_auth():
        return jsonify({"ok": True})
    return jsonify({"error": "Unauthorized"}), 401


@app.route("/api/crm-clients")
def crm_clients():
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import requests as _req
        crm_url = os.environ.get("CRM_SUPABASE_URL", "").strip()
        crm_key = os.environ.get("CRM_SUPABASE_KEY", "").strip()
        if not crm_url or not crm_key:
            return _cors(jsonify({"clients": []}))
        r = _req.get(f"{crm_url}/rest/v1/clients?select=name",
                      headers={"apikey": crm_key, "Authorization": f"Bearer {crm_key}"}, timeout=10)
        names = sorted(set(c["name"].strip() for c in r.json() if c.get("name"))) if r.status_code == 200 else []
        return _cors(jsonify({"clients": names}))
    except Exception:
        return _cors(jsonify({"clients": []}))


@app.route("/api/overview")
def overview():
    if not _check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data, ts = _get_cache("overview_v2")
    if data and data.get("clients"):
        data["_cached"] = True
        data["_synced_at"] = ts
        return _cors(jsonify(data))
    return _cors(jsonify({"loading": True, "clients": [], "total_accounts": 0}))


@app.route("/api/health-history")
def health_history():
    if not _check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    history, _ = _get_cache("health_history")
    return _cors(jsonify(history or []))


@app.route("/api/sync", methods=["POST", "OPTIONS"])
def trigger_sync():
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    import db as store
    import time as _time
    store._CACHE_WRITE_ENABLED = True

    def progress_cb(pct, msg):
        try:
            store.cache_set("sync_progress", {
                "pct": pct, "msg": msg, "ts": _time.time(), "status": "running"
            })
        except Exception:
            pass

    store.cache_set("sync_progress", {"pct": 0, "msg": "Starting sync...", "ts": _time.time(), "status": "running"})
    try:
        import sync
        sync.store._CACHE_WRITE_ENABLED = True
        ok = sync.sync(progress_cb=progress_cb)
        status = "done" if ok else "error"
        msg = "Sync complete" if ok else "Sync aborted (insufficient data)"
        store.cache_set("sync_progress", {"pct": 100 if ok else 0, "msg": msg, "ts": _time.time(), "status": status})
        if ok:
            return _cors(jsonify({"ok": True, "message": msg}))
        return _cors(jsonify({"ok": False, "message": msg})), 500
    except Exception as e:
        store.cache_set("sync_progress", {"pct": 0, "msg": str(e), "ts": _time.time(), "status": "error"})
        return _cors(jsonify({"error": str(e)})), 500


@app.route("/api/refresh-stats", methods=["POST", "OPTIONS"])
def refresh_stats():
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    import db as store
    store._CACHE_WRITE_ENABLED = True
    try:
        import sync
        sync.store._CACHE_WRITE_ENABLED = True
        stats = sync.fetch_acq_campaign_stats()
        data, _ = store.cache_get("overview_v2")
        if data:
            data["acq_campaign_stats"] = stats
            store.cache_patch("overview_v2", data)
        return _cors(jsonify({"ok": True, "campaigns": len(stats)}))
    except Exception as e:
        return _cors(jsonify({"error": str(e)})), 500


@app.route("/api/sync-progress")
def sync_progress():
    if not _check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data, _ = _get_cache("sync_progress")
    return _cors(jsonify(data or {"status": "idle", "pct": 0, "msg": ""}))


def _sl_request(method, url, **kwargs):
    """SmartLead API request with retry on 429."""
    import time
    import requests as req
    for attempt in range(3):
        r = getattr(req, method)(url, **kwargs)
        if r.status_code != 429:
            return r
        time.sleep(5 * (attempt + 1))
    return r


def _update_cache_campaigns(group_name, campaign_id, campaign_name, action):
    """Patch overview_v2 cache after assign/unassign so changes persist."""
    import db as store
    try:
        data, _ = store.cache_get("overview_v2")
        if not data:
            return
        for section in ["acquisition_groups", "generic_groups"]:
            for g in (data.get(section) or []):
                if g.get("name") != group_name:
                    continue
                camps = g.get("campaigns", [])
                if action == "remove":
                    g["campaigns"] = [c for c in camps if c.get("id") != campaign_id]
                elif action == "add":
                    if not any(c.get("id") == campaign_id for c in camps):
                        status = "ACTIVE"
                        for s in (data.get("acq_campaign_stats") or []):
                            if s.get("id") == campaign_id:
                                status = s.get("status", "ACTIVE")
                                break
                        camps.append({"id": campaign_id, "name": campaign_name,
                                      "status": status, "accounts": len(g.get("account_details", []))})
                        g["campaigns"] = camps
                for a in (g.get("account_details") or []):
                    names = a.get("campaign_names", [])
                    if action == "add" and campaign_name not in names:
                        names.append(campaign_name)
                    elif action == "remove" and campaign_name in names:
                        names.remove(campaign_name)
                    a["campaign_names"] = names
                    a["in_campaign"] = len(names) > 0
        store.cache_patch("overview_v2", data)
    except Exception:
        pass


def _get_campaign_name(campaign_id):
    """Look up campaign name from cached acq_campaign_stats."""
    try:
        data, _ = _get_cache("overview_v2")
        if not data:
            return str(campaign_id)
        for c in (data.get("acq_campaign_stats") or []):
            if c.get("id") == campaign_id:
                return c.get("name", str(campaign_id))
    except Exception:
        pass
    return str(campaign_id)


def _resolve_group_account_ids(group_name, campaign_id=None):
    """Read SmartLead account IDs from cached account_details."""
    try:
        data, _ = _get_cache("overview_v2")
    except Exception as e:
        return None, f"Cache error: {e}"
    if not data:
        return None, "No cached data"
    account_ids = []
    for section in ["acquisition_groups", "generic_groups"]:
        for g in (data.get(section) or []):
            if g.get("name") == group_name:
                for a in (g.get("account_details") or []):
                    if a.get("id"):
                        account_ids.append(a["id"])
    if not account_ids:
        all_names = [g.get("name") for s in ["acquisition_groups", "generic_groups"] for g in (data.get(s) or [])]
        return None, f"No account IDs for '{group_name}'. Groups: {all_names}"
    return account_ids, None


@app.route("/api/assign-group", methods=["POST", "OPTIONS"])
def assign_group():
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    body = request.get_json(silent=True) or {}
    group_name = body.get("group_name", "")
    campaign_id = body.get("campaign_id")
    if not group_name or not campaign_id:
        return _cors(jsonify({"error": "group_name and campaign_id required"})), 400
    sl_key = os.environ.get("SMARTLEAD_API_KEY", "")
    if not sl_key:
        return _cors(jsonify({"error": "SMARTLEAD_API_KEY not configured"})), 500
    account_ids, err = _resolve_group_account_ids(group_name)
    if err:
        return _cors(jsonify({"error": err})), 404 if "No account" in err else 500
    sl = "https://server.smartlead.ai/api/v1"
    r = _sl_request("post", f"{sl}/campaigns/{campaign_id}/email-accounts?api_key={sl_key}",
                    json={"email_account_ids": account_ids}, timeout=30)
    if r.status_code == 200:
        camp_name = _get_campaign_name(campaign_id)
        _update_cache_campaigns(group_name, campaign_id, camp_name, "add")
        return _cors(jsonify({"ok": True, "assigned": len(account_ids),
                              "message": f"Assigned {len(account_ids)} accounts. REMINDER: Reallocate inboxes in SmartLead."}))
    return _cors(jsonify({"error": f"SmartLead returned {r.status_code}"})), 502


@app.route("/api/unassign-group", methods=["POST", "OPTIONS"])
def unassign_group():
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    body = request.get_json(silent=True) or {}
    group_name = body.get("group_name", "")
    campaign_id = body.get("campaign_id")
    if not group_name or not campaign_id:
        return _cors(jsonify({"error": "group_name and campaign_id required"})), 400
    sl_key = os.environ.get("SMARTLEAD_API_KEY", "")
    if not sl_key:
        return _cors(jsonify({"error": "SMARTLEAD_API_KEY not configured"})), 500
    account_ids, err = _resolve_group_account_ids(group_name)
    if err:
        return _cors(jsonify({"error": err})), 404 if "No account" in err else 500
    sl = "https://server.smartlead.ai/api/v1"
    r = _sl_request("delete", f"{sl}/campaigns/{campaign_id}/email-accounts?api_key={sl_key}",
                    headers={"Content-Type": "application/json"},
                    data=json.dumps({"email_account_ids": account_ids}), timeout=30)
    if r.status_code == 200:
        camp_name = _get_campaign_name(campaign_id)
        _update_cache_campaigns(group_name, campaign_id, camp_name, "remove")
        return _cors(jsonify({"ok": True, "removed": len(account_ids),
                              "message": f"Removed {len(account_ids)} accounts. REMINDER: Reallocate inboxes in SmartLead."}))
    return _cors(jsonify({"error": f"SmartLead returned {r.status_code}"})), 502


@app.route("/api/assign-generic-to-client", methods=["POST", "OPTIONS"])
def assign_generic_to_client():
    """Convert a generic reserve group into a client group by re-tagging accounts."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import db as store
        body = request.get_json(silent=True) or {}
        group_name = body.get("group_name", "")
        client_name = body.get("client_name", "").strip()
        ab = body.get("ab", "A").upper()
        if not group_name or not client_name:
            return _cors(jsonify({"error": "group_name and client_name required"})), 400
        if ab not in ("A", "B"):
            return _cors(jsonify({"error": "ab must be A or B"})), 400

        jwt = os.environ.get("SMARTLEAD_JWT", "").strip()
        gql_url = os.environ.get("SMARTLEAD_GQL", "").strip()
        sl_key = os.environ.get("SMARTLEAD_API_KEY", "").strip()
        if not jwt or not gql_url:
            return _cors(jsonify({"error": "SMARTLEAD_JWT and SMARTLEAD_GQL required"})), 500

        sl_headers = {"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"}
        sl_internal = "https://server.smartlead.ai/api"

        account_ids, err = _resolve_group_account_ids(group_name)
        if err:
            return _cors(jsonify({"error": err})), 404

        def _gql(query, variables=None):
            import requests as _req
            r = _req.post(gql_url, headers=sl_headers,
                          json={"query": query, "variables": variables or {}}, timeout=30)
            return r.json()

        all_tags_resp = _gql("{ tags { id name color } }")
        all_tags = {t["name"]: t for t in all_tags_resp.get("data", {}).get("tags", [])}

        new_tag_name = f"{client_name} {ab}"

        def _find_or_create_tag(name):
            name_lower = name.lower().strip()
            for tn, td in all_tags.items():
                if tn.lower().strip() == name_lower:
                    return td["id"]
            palette = ["#FF6B6B","#FF8E72","#FFA94D","#FFD43B","#A9E34B","#51CF66","#20C997",
                       "#22B8CF","#339AF0","#5C7CFA","#7950F2","#BE4BDB","#E64980","#F06595"]
            used = {t.get("color") for t in all_tags.values()}
            color = next((c for c in palette if c not in used), "#D0FCB1")
            mutation = """mutation createTag($object: tags_insert_input!) {
              insert_tags_one(object: $object) { id name color }
            }"""
            result = _gql(mutation, {"object": {"name": name, "color": color}})
            tag = result.get("data", {}).get("insert_tags_one", {})
            if tag.get("id"):
                all_tags[name] = tag
            return tag.get("id")

        ZAPMAIL_TAG_ID = 2
        client_tag_id = _find_or_create_tag(new_tag_name)
        if not client_tag_id:
            return _cors(jsonify({"error": f"Failed to find/create tag '{new_tag_name}'"})), 500

        import re
        date_pattern = re.compile(r'^\d{1,2}/\d{1,2}/\d{2,4}$')
        id_set = set(account_ids)
        acct_tags = {}
        offset = 0
        while True:
            resp = _gql(
                '{ email_account_tag_mappings(limit: 1000, offset: %d) '
                '{ email_account_id tag { id name } } }' % offset
            )
            rows = (resp or {}).get("data", {}).get("email_account_tag_mappings", [])
            for row in rows:
                aid = row["email_account_id"]
                if aid in id_set:
                    tag = row.get("tag", {})
                    acct_tags.setdefault(aid, []).append(tag)
            if len(rows) < 1000:
                break
            offset += 1000

        import requests as _req
        import time
        tagged = 0
        for acc_id in account_ids:
            existing = acct_tags.get(acc_id, [])
            date_tag_id = None
            for t in existing:
                if date_pattern.match(t.get("name", "")):
                    date_tag_id = t["id"]
                    break
            tag_ids = [ZAPMAIL_TAG_ID, client_tag_id]
            if date_tag_id:
                tag_ids.append(date_tag_id)
            for attempt in range(3):
                r = _req.post(f"{sl_internal}/email-account/save-management-details",
                              headers=sl_headers,
                              json={"id": acc_id, "tags": tag_ids}, timeout=30)
                if r.status_code != 429:
                    break
                time.sleep(5 * (attempt + 1))
            if r.status_code == 200:
                tagged += 1

        data_cache, _ = store.cache_get("overview_v2")
        if data_cache:
            generic_groups = data_cache.get("generic_groups", [])
            moved_group = None
            for i, g in enumerate(generic_groups):
                if g.get("name") == group_name:
                    moved_group = generic_groups.pop(i)
                    break
            if moved_group:
                moved_group["name"] = new_tag_name
                clients = data_cache.get("clients", [])
                existing_client = None
                for c in clients:
                    if c.get("name", "").lower() == client_name.lower():
                        existing_client = c
                        break
                if existing_client:
                    if ab == "A":
                        existing_client["group_a"] = moved_group
                        existing_client["group_a_count"] = moved_group.get("accounts", 0)
                    else:
                        existing_client["group_b"] = moved_group
                        existing_client["group_b_count"] = moved_group.get("accounts", 0)
                    existing_client["accounts"] = existing_client.get("group_a_count", 0) + existing_client.get("group_b_count", 0)
                else:
                    new_client = {
                        "name": client_name,
                        "accounts": moved_group.get("accounts", 0),
                        "total_domains": moved_group.get("total_domains", 0),
                        "daily_capacity": moved_group.get("daily_capacity", 0),
                        "group_a_count": moved_group.get("accounts", 0) if ab == "A" else 0,
                        "group_b_count": moved_group.get("accounts", 0) if ab == "B" else 0,
                        "group_a": moved_group if ab == "A" else None,
                        "group_b": moved_group if ab == "B" else None,
                        "account_details": moved_group.get("account_details", []),
                    }
                    clients.append(new_client)
                data_cache["generic_groups"] = generic_groups
                data_cache["clients"] = clients
                store.cache_patch("overview_v2", data_cache)

        return _cors(jsonify({
            "ok": True,
            "tagged": tagged,
            "total": len(account_ids),
            "new_tag": new_tag_name,
            "tag_ids": tag_ids,
        }))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/subscriptions")
def subscriptions():
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    import requests as req
    from datetime import datetime, timezone
    zm_key = os.environ.get("ZAPMAIL_API_KEY", "").strip()
    if not zm_key:
        return _cors(jsonify({"error": "ZAPMAIL_API_KEY not configured"})), 500
    headers = {"Content-Type": "application/json", "x-auth-zapmail": zm_key, "x-service-provider": "GOOGLE"}
    try:
        r = req.get("https://api.zapmail.ai/api/v2/subscriptions", headers=headers, timeout=15)
        raw = r.json()
    except Exception as e:
        return _cors(jsonify({"error": str(e)})), 502
    subs = raw if isinstance(raw, list) else raw.get("data", [])
    now = datetime.now(timezone.utc)
    result = []
    total_monthly = 0
    total_mailboxes = 0
    action_needed_count = 0
    for s in subs:
        if s.get("subscriptionStatus") != "ACTIVE":
            continue
        period_end = s.get("periodEnd", "")
        try:
            renews = datetime.fromisoformat(period_end.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        days_until = (renews - now).days
        price = s.get("price", 0)
        mailboxes = s.get("totalMailboxQuantity", 0)
        total_monthly += price
        total_mailboxes += mailboxes
        action_needed = days_until <= 16
        if action_needed:
            action_needed_count += 1
        result.append({
            "id": s.get("id"),
            "subscription_id": s.get("subscriptionId"),
            "price": price,
            "mailboxes": mailboxes,
            "renews": renews.strftime("%Y-%m-%d"),
            "days_until_renewal": days_until,
            "action_needed": action_needed,
            "created": s.get("subscriptionCreationDate", "")[:10],
        })
    result.sort(key=lambda x: x["renews"])
    return _cors(jsonify({
        "subscriptions": result,
        "total_monthly": total_monthly,
        "total_mailboxes": total_mailboxes,
        "action_needed_count": action_needed_count,
    }))


@app.route("/api/domain-renewals")
def domain_renewals():
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    import db as store
    cached, updated_at = store.cache_get("domain_renewals")
    if cached:
        return _cors(jsonify(cached))
    return _cors(jsonify({"domain_renewals": {}}))


@app.route("/api/domains/inventory")
def domains_inventory():
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import db as store
        all_domains = store.get_all_domains()
        summary = {"total": 0, "available": 0, "in_use": 0, "cancelled": 0, "do_not_use": 0,
                   "by_provider": {}, "by_pool": {}}
        for d in all_domains:
            summary["total"] += 1
            s = d.get("status", "")
            if s in summary:
                summary[s] += 1
            p = d.get("provider", "")
            if p:
                summary["by_provider"][p] = summary["by_provider"].get(p, 0) + 1
            pool = d.get("pool", "")
            if pool:
                summary["by_pool"][pool] = summary["by_pool"].get(pool, 0) + 1
        return _cors(jsonify({"domains": all_domains, "summary": summary}))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


def _porkbun_list():
    import requests as _req
    pk = os.environ.get("PORKBUN_API_KEY", "").strip()
    sk = os.environ.get("PORKBUN_SECRET_KEY", "").strip()
    if not pk or not sk:
        return []
    r = _req.post("https://api.porkbun.com/api/json/v3/domain/listAll",
                   json={"apikey": pk, "secretapikey": sk}, timeout=30)
    data = r.json()
    if data.get("status") != "SUCCESS":
        return []
    return [{"domain": d.get("domain", ""), "expires": d.get("expireDate", "")[:10],
             "auto_renew": d.get("autoRenew") == "1", "registrar": "porkbun"}
            for d in data.get("domains", [])]


def _spaceship_list():
    import requests as _req
    ak = os.environ.get("SPACESHIP_API_KEY", "").strip()
    sk = os.environ.get("SPACESHIP_SECRET_KEY", "").strip()
    if not ak or not sk:
        return []
    headers = {"X-API-Key": ak, "X-API-Secret": sk, "Content-Type": "application/json"}
    result, skip = [], 0
    while True:
        r = _req.get("https://spaceship.dev/api/v1/domains", headers=headers,
                      timeout=30, params={"take": 100, "skip": skip})
        if r.status_code != 200:
            break
        items = r.json().get("items", []) if isinstance(r.json(), dict) else []
        if not items:
            break
        for d in items:
            result.append({"domain": d.get("name", ""), "expires": d.get("expirationDate", "")[:10],
                           "auto_renew": d.get("autoRenew", False), "registrar": "spaceship"})
        if len(items) < 100:
            break
        skip += 100
    return result


def _porkbun_set_ar(domain, enabled):
    import requests as _req
    pk = os.environ.get("PORKBUN_API_KEY", "").strip()
    sk = os.environ.get("PORKBUN_SECRET_KEY", "").strip()
    r = _req.post(f"https://api.porkbun.com/api/json/v3/domain/updateAutoRenew/{domain}",
                   json={"apikey": pk, "secretapikey": sk, "status": "on" if enabled else "off"}, timeout=15)
    data = r.json()
    return {"success": data.get("status") == "SUCCESS", "message": data.get("message", "")}


def _spaceship_set_ar(domain, enabled):
    import requests as _req
    ak = os.environ.get("SPACESHIP_API_KEY", "").strip()
    sk = os.environ.get("SPACESHIP_SECRET_KEY", "").strip()
    r = _req.put(f"https://spaceship.dev/api/v1/domains/{domain}/autorenew",
                  headers={"X-API-Key": ak, "X-API-Secret": sk, "Content-Type": "application/json"},
                  json={"isEnabled": enabled}, timeout=15)
    if r.status_code in (200, 204):
        return {"success": True, "message": f"Auto-renew {'enabled' if enabled else 'disabled'}"}
    return {"success": False, "message": r.text[:200]}


@app.route("/api/domains/sync-registrar", methods=["POST", "OPTIONS"])
def domains_sync_one_registrar():
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import db as store
        body = request.get_json(silent=True) or {}
        registrar = body.get("registrar", "")
        if registrar == "porkbun":
            domains = _porkbun_list()
        elif registrar == "spaceship":
            domains = _spaceship_list()
        else:
            return _cors(jsonify({"error": f"Unknown registrar: {registrar}"})), 400
        updated = 0
        for rd in domains:
            domain_name = rd.get("domain", "").strip().lower()
            if not domain_name:
                continue
            fields = {}
            if rd.get("expires"):
                fields["expires_at"] = rd["expires"]
            fields["auto_renew"] = rd.get("auto_renew", False)
            store.update_domain(domain_name, **fields)
            updated += 1
        return _cors(jsonify({"registrar": registrar, "fetched": len(domains), "updated": updated}))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/domains/auto-renew", methods=["POST", "OPTIONS"])
def domains_set_auto_renew():
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import db as store
        body = request.get_json(silent=True) or {}
        domains_list = body.get("domains", [])
        enabled = body.get("enabled", False)
        if not domains_list:
            return _cors(jsonify({"error": "No domains specified"})), 400
        all_db_domains = {d["domain"]: d for d in store.get_all_domains()}
        results = []
        for domain_name in domains_list:
            domain_name = domain_name.strip().lower()
            db_rec = all_db_domains.get(domain_name)
            if not db_rec:
                results.append({"domain": domain_name, "success": False, "message": "Not found in DB"})
                continue
            provider = db_rec.get("provider", "")
            if provider == "porkbun":
                res = _porkbun_set_ar(domain_name, enabled)
            elif provider == "spaceship":
                res = _spaceship_set_ar(domain_name, enabled)
            else:
                results.append({"domain": domain_name, "success": False, "message": f"Unknown provider: {provider}"})
                continue
            if res.get("success"):
                store.update_domain(domain_name, auto_renew=enabled)
            results.append({"domain": domain_name, **res})
        succeeded = sum(1 for r in results if r.get("success"))
        return _cors(jsonify({"results": results, "succeeded": succeeded, "total": len(results)}))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/replacements")
def get_replacements():
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    import db as store
    state = store.get_state("domain_replacements") or {"jobs": []}
    return _cors(jsonify(state))


@app.route("/api/replacements", methods=["POST", "OPTIONS"])
def create_replacement():
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    import db as store
    import uuid as _uuid
    from datetime import datetime as _dt
    body = request.get_json(silent=True) or {}
    required = ["old_domain", "group_name", "group_type", "bounce_rate"]
    for field in required:
        if not body.get(field):
            return _cors(jsonify({"error": f"{field} required"})), 400
    state = store.get_state("domain_replacements") or {"jobs": []}
    for j in state["jobs"]:
        if j["old_domain"] == body["old_domain"] and j["status"] not in ("swapped", "cancelled"):
            return _cors(jsonify({"error": f"{body['old_domain']} already flagged"})), 400
    job = {
        "id": str(_uuid.uuid4())[:8],
        "old_domain": body["old_domain"],
        "new_domain": None,
        "group_name": body["group_name"],
        "group_type": body["group_type"],
        "bounce_rate": body["bounce_rate"],
        "status": "flagged",
        "campaigns": body.get("campaigns", []),
        "flagged_at": _dt.now().strftime("%Y-%m-%d"),
        "warming_started_at": None,
        "swapped_at": None,
        "cancelled_at": None,
        "old_cancelled": False,
        "old_cancel_date": None,
        "tags_updated": False,
        "forwarding_updated": False,
        "removed_zapmail": False,
        "removed_smartlead": False,
        "domain_cancelled": False,
    }
    state["jobs"].append(job)
    store.set_state("domain_replacements", state)
    return _cors(jsonify({"ok": True, "job": job}))


@app.route("/api/replacements/update", methods=["POST", "OPTIONS"])
def update_replacement():
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    import db as store
    from datetime import datetime as _dt
    body = request.get_json(silent=True) or {}
    job_id = body.get("id")
    if not job_id:
        return _cors(jsonify({"error": "id required"})), 400
    state = store.get_state("domain_replacements") or {"jobs": []}
    job = None
    for j in state["jobs"]:
        if j["id"] == job_id:
            job = j
            break
    if not job:
        return _cors(jsonify({"error": "Job not found"})), 404
    new_status = body.get("status")
    new_domain = body.get("new_domain")
    valid_transitions = {
        "flagged": ["warming", "cancelled"],
        "warming": ["ready", "cancelled"],
        "ready": ["swapped", "cancelled"],
        "swapped": ["cancelled"],
    }
    if new_status:
        allowed = valid_transitions.get(job["status"], [])
        if new_status not in allowed:
            return _cors(jsonify({"error": f"Cannot go from {job['status']} to {new_status}"})), 400
        job["status"] = new_status
        now = _dt.now().strftime("%Y-%m-%d")
        if new_status == "warming":
            job["warming_started_at"] = now
        elif new_status == "swapped":
            job["swapped_at"] = now
        elif new_status == "cancelled":
            job["cancelled_at"] = now
    if new_domain:
        job["new_domain"] = new_domain
    if body.get("old_cancelled"):
        job["old_cancelled"] = True
        job["old_cancel_date"] = _dt.now().strftime("%Y-%m-%d")
    for flag in ("tags_updated", "forwarding_updated", "removed_zapmail", "removed_smartlead", "domain_cancelled"):
        if flag in body:
            job[flag] = bool(body[flag])
    store.set_state("domain_replacements", state)
    return _cors(jsonify({"ok": True, "job": job}))


@app.route("/api/replacements/delete", methods=["POST", "OPTIONS"])
def delete_replacement():
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    import db as store
    body = request.get_json(silent=True) or {}
    job_id = body.get("id")
    if not job_id:
        return _cors(jsonify({"error": "id required"})), 400
    state = store.get_state("domain_replacements") or {"jobs": []}
    before = len(state["jobs"])
    state["jobs"] = [j for j in state["jobs"] if j["id"] != job_id]
    if len(state["jobs"]) == before:
        return _cors(jsonify({"error": "Job not found"})), 404
    store.set_state("domain_replacements", state)
    return _cors(jsonify({"ok": True}))


@app.route("/api/<path:path>", methods=["GET", "OPTIONS"])
def catch_all(path):
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    return _cors(jsonify({"error": "Not found"})), 404
