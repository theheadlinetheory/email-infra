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
        r = _req.get(f"{crm_url}/rest/v1/clients?select=name,client_standing&client_standing=not.is.null",
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
        try:
            import re
            import requests as _req
            crm_url = os.environ.get("CRM_SUPABASE_URL", "").strip()
            crm_key = os.environ.get("CRM_SUPABASE_KEY", "").strip()
            if crm_url and crm_key:
                r = _req.get(f"{crm_url}/rest/v1/clients?select=name,client_standing&client_standing=not.is.null",
                              headers={"apikey": crm_key, "Authorization": f"Bearer {crm_key}"}, timeout=5)
                if r.status_code == 200:
                    crm_names = [c["name"].strip() for c in r.json() if c.get("name")]
                    def _norm(name):
                        n = name.lower().strip()
                        prev = ''
                        while prev != n:
                            prev = n
                            n = re.sub(r'\s+(group|llc|inc\.?|construction|landscaping|lawn\s*care|hvac|'
                                       r'land\s*care|scapes|landscape|heating\s*&?\s*air.*|'
                                       r'lawn\s*solutions|land\s*solutions|&\s*design|conditioning)\s*$',
                                       '', n, flags=re.IGNORECASE)
                            n = re.sub(r'[,.\s&]+$', '', n).strip()
                        return re.sub(r'\s+', ' ', n)
                    crm_by_norm = {_norm(cn): cn for cn in crm_names}
                    matched_norms = set()
                    for c in data["clients"]:
                        normed = _norm(c["name"])
                        if normed in crm_by_norm:
                            c["name"] = crm_by_norm[normed]
                            matched_norms.add(normed)
                    for cn in crm_names:
                        if _norm(cn) not in matched_norms:
                            data["clients"].append({
                                "name": cn, "accounts": 0, "group_a_count": 0, "group_b_count": 0,
                                "group_a": None, "group_b": None, "in_campaign": 0, "smtp_failures": 0,
                                "total_domains": 0, "avg_bounce_rate": None, "avg_reply_rate": None,
                                "daily_capacity": 0, "total_sent": 0, "daily_sent": 0,
                                "campaigns": [], "account_details": [], "crm_only": True,
                            })
                    data["crm_clients"] = crm_names
        except Exception:
            pass
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
        from datetime import date
        today_str = date.today().isoformat()
        health_today = sync.fetch_health_metrics(start_date=today_str, end_date=today_str)
        data, _ = store.cache_get("overview_v2")
        if data and health_today:
            for group_list_key in ("clients", "acquisition_groups", "generic_groups", "aging_groups"):
                for g in data.get(group_list_key, []):
                    emails = [a["email"] for a in g.get("account_details", []) if a.get("email")]
                    ds = sum(health_today.get(e, {}).get("sent", 0) for e in emails)
                    g["daily_sent"] = ds
                    if g.get("group_a") and g["group_a"].get("account_details"):
                        ea = [a["email"] for a in g["group_a"]["account_details"] if a.get("email")]
                        g["group_a"]["daily_sent"] = sum(health_today.get(e, {}).get("sent", 0) for e in ea)
                    if g.get("group_b") and g["group_b"].get("account_details"):
                        eb = [a["email"] for a in g["group_b"]["account_details"] if a.get("email")]
                        g["group_b"]["daily_sent"] = sum(health_today.get(e, {}).get("sent", 0) for e in eb)
            store.cache_patch("overview_v2", data)
        return _cors(jsonify({"ok": True, "accounts": len(health_today)}))
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

        import re as _re
        def _norm_tag(n):
            s = n.lower().strip()
            prev = ''
            while prev != s:
                prev = s
                s = _re.sub(r'\s+(group|llc|inc\.?|construction|landscaping|lawn\s*care|hvac|'
                           r'land\s*care|scapes|landscape|heating\s*&?\s*air.*|'
                           r'lawn\s*solutions|land\s*solutions|&\s*design|conditioning)\s*$',
                           '', s, flags=_re.IGNORECASE)
                s = _re.sub(r'[,.\s&]+$', '', s).strip()
            return _re.sub(r'\s+', ' ', s)

        def _find_existing_client_tag(client_name, ab):
            """Find existing tag matching this client + A/B by normalized name."""
            cn = _norm_tag(client_name)
            suffix = f" {ab.upper()}"
            for tn, td in all_tags.items():
                if not tn.upper().endswith(suffix):
                    continue
                tag_base = tn[:-(len(suffix))].strip()
                if _norm_tag(tag_base) == cn:
                    return td["id"], tn
            return None, None

        existing_tag_id, existing_tag_name = _find_existing_client_tag(client_name, ab)

        if existing_tag_id:
            new_tag_name = existing_tag_name
            client_tag_id = existing_tag_id
        else:
            new_tag_name = f"{client_name} Group {ab}"
            palette = ["#FF6B6B","#FF8E72","#FFA94D","#FFD43B","#A9E34B","#51CF66","#20C997",
                       "#22B8CF","#339AF0","#5C7CFA","#7950F2","#BE4BDB","#E64980","#F06595"]
            used = {t.get("color") for t in all_tags.values()}
            color = next((c for c in palette if c not in used), "#D0FCB1")
            mutation = """mutation createTag($object: tags_insert_input!) {
              insert_tags_one(object: $object) { id name color }
            }"""
            result = _gql(mutation, {"object": {"name": new_tag_name, "color": color}})
            tag = result.get("data", {}).get("insert_tags_one", {})
            client_tag_id = tag.get("id")
            if client_tag_id:
                all_tags[new_tag_name] = tag

        ZAPMAIL_TAG_ID = 262254
        client_tag_id = client_tag_id
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
        tag_errors = []
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
            else:
                tag_errors.append({"id": acc_id, "status": r.status_code, "body": r.text[:100]})

        time.sleep(2)
        verify_resp = _gql(
            "query($tid: Int!) { email_account_tag_mappings(where: {tag_id: {_eq: $tid}}) { email_account_id } }",
            {"tid": client_tag_id}
        )
        verified_ids = {m["email_account_id"] for m in
                        (verify_resp.get("data") or {}).get("email_account_tag_mappings", [])}
        verified = len(set(account_ids) & verified_ids)
        missing = [aid for aid in account_ids if aid not in verified_ids]

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
            "ok": verified == len(account_ids),
            "tagged": tagged,
            "verified": verified,
            "total": len(account_ids),
            "new_tag": new_tag_name,
            "missing": len(missing),
            "errors": tag_errors[:5] if tag_errors else [],
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


# ─── Domain Purchase + Generic Group Creation Wizard ───

@app.route("/api/domains/check", methods=["POST", "OPTIONS"])
def check_domains():
    """Check availability of domains on Spaceship and/or Porkbun."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import requests as req
        body = request.get_json(silent=True) or {}
        domain_names = body.get("domains", [])
        registrar = body.get("registrar", "spaceship")
        if not domain_names:
            return _cors(jsonify({"error": "No domains provided"})), 400

        ak = os.environ.get("SPACESHIP_API_KEY", "").strip()
        sk = os.environ.get("SPACESHIP_SECRET_KEY", "").strip()
        pk = os.environ.get("PORKBUN_API_KEY", "").strip()
        ps = os.environ.get("PORKBUN_SECRET_KEY", "").strip()

        results = []
        for dn in domain_names[:50]:
            dn = dn.strip().lower()
            if not dn:
                continue
            try:
                if registrar == "spaceship":
                    r = req.get(f"https://spaceship.dev/api/v1/domains/{dn}/available",
                                headers={"X-Api-Key": ak, "X-Api-Secret": sk}, timeout=10)
                    if r.status_code == 200:
                        data = r.json()
                        results.append({"domain": dn, "available": True, "price": data.get("price", "?")})
                    else:
                        results.append({"domain": dn, "available": False})
                else:
                    r = req.post(f"https://api.porkbun.com/api/json/v3/domain/checkDomain/{dn}",
                                 json={"apikey": pk, "secretapikey": ps}, timeout=10)
                    data = r.json()
                    if data.get("status") == "SUCCESS" and data.get("avail") == "yes":
                        pricing = data.get("pricing", {})
                        results.append({"domain": dn, "available": True, "price": pricing.get("registration", "?")})
                    else:
                        results.append({"domain": dn, "available": False})
            except Exception:
                results.append({"domain": dn, "available": False, "error": "timeout"})

        return _cors(jsonify({"results": results}))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/domains/purchase", methods=["POST", "OPTIONS"])
def purchase_domains():
    """Purchase domains and set CloudNS nameservers."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import time as _time
        import requests as req
        body = request.get_json(silent=True) or {}
        domain_names = body.get("domains", [])
        registrar = body.get("registrar", "spaceship")
        if not domain_names:
            return _cors(jsonify({"error": "No domains provided"})), 400

        ak = os.environ.get("SPACESHIP_API_KEY", "").strip()
        sk = os.environ.get("SPACESHIP_SECRET_KEY", "").strip()
        pk = os.environ.get("PORKBUN_API_KEY", "").strip()
        ps = os.environ.get("PORKBUN_SECRET_KEY", "").strip()
        CLOUDNS = ["pns61.cloudns.net", "pns62.cloudns.com", "pns63.cloudns.net", "pns64.cloudns.uk"]

        log_lines = []
        purchased = []
        failed = []

        for dn in domain_names[:20]:
            dn = dn.strip().lower()
            try:
                if registrar == "spaceship":
                    r = req.post(f"https://spaceship.dev/api/v1/domains/{dn}",
                                 headers={"X-Api-Key": ak, "X-Api-Secret": sk, "Content-Type": "application/json"},
                                 json={}, timeout=30)
                    if r.status_code in (200, 201, 202):
                        log_lines.append(f"Purchased: {dn}")
                        purchased.append(dn)
                    else:
                        log_lines.append(f"Failed: {dn} — {r.text[:100]}")
                        failed.append(dn)
                else:
                    r = req.post(f"https://api.porkbun.com/api/json/v3/domain/create/{dn}",
                                 json={"apikey": pk, "secretapikey": ps, "acknowledgement": "yes"}, timeout=30)
                    data = r.json()
                    if data.get("status") == "SUCCESS":
                        log_lines.append(f"Purchased: {dn}")
                        purchased.append(dn)
                    else:
                        log_lines.append(f"Failed: {dn} — {data.get('message', '')}")
                        failed.append(dn)
            except Exception as e:
                log_lines.append(f"Error: {dn} — {str(e)}")
                failed.append(dn)
            _time.sleep(1)

        # Set nameservers on purchased domains
        ns_ok = 0
        for dn in purchased:
            try:
                if registrar == "spaceship":
                    r = req.put(f"https://spaceship.dev/api/v1/domains/{dn}/nameservers",
                                headers={"X-Api-Key": ak, "X-Api-Secret": sk, "Content-Type": "application/json"},
                                json={"provider": "custom", "hosts": CLOUDNS}, timeout=15)
                    if r.status_code in (200, 204):
                        ns_ok += 1
                    else:
                        log_lines.append(f"NS failed for {dn}: {r.text[:100]}")
                else:
                    r = req.post(f"https://api.porkbun.com/api/json/v3/domain/updateNs/{dn}",
                                 json={"apikey": pk, "secretapikey": ps, "ns": CLOUDNS}, timeout=15)
                    if r.json().get("status") == "SUCCESS":
                        ns_ok += 1
                    else:
                        log_lines.append(f"NS failed for {dn}: {r.json().get('message', '')}")
            except Exception as e:
                log_lines.append(f"NS error for {dn}: {str(e)}")
            _time.sleep(0.5)

        # Disable auto-renew on purchased domains
        for dn in purchased:
            try:
                if registrar == "spaceship":
                    req.put(f"https://spaceship.dev/api/v1/domains/{dn}/autorenew",
                            headers={"X-Api-Key": ak, "X-Api-Secret": sk, "Content-Type": "application/json"},
                            json={"isEnabled": False}, timeout=10)
                else:
                    req.post(f"https://api.porkbun.com/api/json/v3/domain/updateAutoRenew/{dn}",
                             json={"apikey": pk, "secretapikey": ps, "status": "off"}, timeout=10)
            except Exception:
                pass

        log_lines.append(f"Nameservers set on {ns_ok}/{len(purchased)} domains")
        log_lines.append(f"Auto-renew disabled on all purchased domains")

        return _cors(jsonify({"ok": True, "purchased": purchased, "failed": failed,
                              "ns_set": ns_ok, "log": log_lines}))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/available-domains")
def available_domains():
    """Return fresh Spaceship domains (CloudNS set, not in Zapmail or SmartLead)."""
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import requests as req
        import db as store

        # Get domains already in SmartLead
        overview, _ = store.cache_get("overview_v2")
        sl_domains = set()
        if overview:
            for section in ["clients", "acquisition_groups", "generic_groups", "aging_groups"]:
                for g in overview.get(section, []):
                    for a in g.get("account_details", []):
                        email = a.get("email", "") or ""
                        if "@" in email:
                            sl_domains.add(email.split("@")[1])

        # Get domains already in Zapmail
        ZAPMAIL_KEY = os.environ.get("ZAPMAIL_API_KEY", "")
        zap_domains = set()
        page = 1
        while True:
            zr = req.get(f"https://api.zapmail.ai/api/v2/domains?page={page}",
                         headers={"x-auth-zapmail": ZAPMAIL_KEY}, timeout=30)
            zd = zr.json().get("data", {})
            for d in zd.get("domains", []):
                zap_domains.add(d.get("name", ""))
            if page >= zd.get("totalPages", 1):
                break
            page += 1

        # Fetch from Spaceship — only recent .info with CloudNS
        ak = os.environ.get("SPACESHIP_API_KEY", "").strip()
        sk = os.environ.get("SPACESHIP_SECRET_KEY", "").strip()
        headers = {"X-API-Key": ak, "X-API-Secret": sk}
        all_sp = []
        skip = 0
        while True:
            r = req.get("https://spaceship.dev/api/v1/domains",
                         headers=headers, timeout=30, params={"take": 100, "skip": skip})
            items = r.json().get("items", []) if r.status_code == 200 else []
            if not items:
                break
            all_sp.extend(items)
            total = r.json().get("total", 0)
            skip += 100
            if skip >= total:
                break

        available = []
        for d in all_sp:
            name = d.get("name", "")
            if name in sl_domains or name in zap_domains:
                continue
            ns_hosts = d.get("nameservers", {}).get("hosts", [])
            if not any("cloudns" in h.lower() for h in ns_hosts):
                continue
            if not name.endswith(".info"):
                continue
            reg = d.get("registrationDate", "")[:10]
            exp = d.get("expirationDate", "")[:10]
            available.append({"domain": name, "registered": reg, "expires": exp})

        available.sort(key=lambda x: x["domain"])

        existing_letters = set()
        if overview:
            for g in overview.get("generic_groups", []):
                name = g.get("name", "")
                if name.startswith("Generic "):
                    existing_letters.add(name[8:].strip())
        all_letters = [chr(i) for i in range(65, 91)]
        free_letters = [l for l in all_letters if l not in existing_letters]
        return _cors(jsonify({"domains": available, "existing_letters": sorted(existing_letters),
                              "free_letters": free_letters}))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/create-generic-group", methods=["POST", "OPTIONS"])
def create_generic_group():
    """Phase 1: Connect domains to Zapmail, buy slots, create inboxes, set photos."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import time as _time
        import requests as req
        body = request.get_json(silent=True) or {}
        letter = body.get("letter", "").strip().upper()
        domains = body.get("domains", [])
        if not letter or len(letter) > 2:
            return _cors(jsonify({"error": "Invalid group letter"})), 400
        if not domains or len(domains) > 20:
            return _cors(jsonify({"error": "Select 1-20 domains"})), 400

        ZAPMAIL_API = "https://api.zapmail.ai/api"
        ZAPMAIL_KEY = os.environ.get("ZAPMAIL_API_KEY", "")
        SUPABASE_STORAGE = os.environ.get("SUPABASE_URL", "https://ghjmqpnqljgwykpjkvzy.supabase.co") + "/storage/v1/object/public/headshots"
        PHOTO_URL = f"{SUPABASE_STORAGE}/sean_reynolds.png"
        NS_STR = "pns61.cloudns.net,pns62.cloudns.com,pns63.cloudns.net,pns64.cloudns.uk"

        def zm_h():
            return {"x-auth-zapmail": ZAPMAIL_KEY, "Content-Type": "application/json"}

        import db as store
        log_lines = []
        def _log(msg):
            log_lines.append(msg)

        # Step 1: Connect each domain to Zapmail
        _log(f"Connecting {len(domains)} domains to Zapmail...")
        for dn in domains:
            r = req.post(f"{ZAPMAIL_API}/v2/domains/connect", headers=zm_h(),
                         json={"domainName": dn, "nameServers": NS_STR}, timeout=30)
            _log(f"  {dn}: {r.json().get('message', r.text[:100])}")
            _time.sleep(0.5)

        # Step 2: Wait for domains to appear and get their IDs
        _log("Waiting 30s for Zapmail to process...")
        _time.sleep(30)

        domain_info = []
        page = 1
        zm_map = {}
        while True:
            zr = req.get(f"{ZAPMAIL_API}/v2/domains?page={page}", headers=zm_h(), timeout=30)
            zd = zr.json().get("data", {})
            for d in zd.get("domains", []):
                zm_map[d.get("name", "")] = d.get("id", "")
            if page >= zd.get("totalPages", 1):
                break
            page += 1

        missing = []
        for dn in domains:
            zid = zm_map.get(dn)
            if zid:
                domain_info.append({"domain": dn, "zapmail_id": zid})
            else:
                missing.append(dn)

        if missing:
            _log(f"WARNING: {len(missing)} domains not found in Zapmail yet: {', '.join(missing[:5])}")

        _log(f"{len(domain_info)} domains connected")

        # Step 3: Buy mailbox slots if needed
        inboxes_needed = len(domain_info) * 3
        ws_resp = req.get(f"{ZAPMAIL_API}/v2/workspaces", headers=zm_h(), timeout=30)
        ws_data = ws_resp.json().get("data", {}).get("currentWorkspace", {})
        purchased = int(ws_data.get("totalMailboxesPurchasedGoogle", "0"))
        assigned = int(ws_data.get("assignedMailboxesCountGoogle", "0"))
        free_slots = purchased - assigned

        if free_slots < inboxes_needed:
            to_buy = inboxes_needed - free_slots
            _log(f"Buying {to_buy} mailbox slots...")
            buy_r = req.post(f"{ZAPMAIL_API}/v2/wallet/buy-addon-mailboxes?quantity={to_buy}",
                             headers=zm_h(), json={}, timeout=30)
            buy_data = buy_r.json()
            if buy_r.status_code != 200 or "Insufficient" in str(buy_data.get("message", "")):
                return _cors(jsonify({"error": f"Failed to buy slots: {buy_data.get('message', buy_r.text[:200])}",
                                      "log": log_lines})), 400
            _log(f"Bought {to_buy} slots")
            _time.sleep(3)
        else:
            _log(f"Have {free_slots} free slots (need {inboxes_needed})")

        # Step 4: Create mailboxes
        SPECS = [
            {"firstName": "Sean", "lastName": "Reynolds", "mailboxUsername": "s.reynolds"},
            {"firstName": "Sean", "lastName": "Reynolds", "mailboxUsername": "sean.r"},
            {"firstName": "Sean", "lastName": "Reynolds", "mailboxUsername": "sean.reynolds"},
        ]

        all_mb_ids = []
        created_domains = []
        for di in domain_info:
            mailboxes = [{**s, "domainName": di["domain"]} for s in SPECS]
            payload = {di["zapmail_id"]: mailboxes}
            r = req.post(f"{ZAPMAIL_API}/v2/mailboxes", headers=zm_h(), json=payload, timeout=30)
            result = r.json()
            mb_ids = result.get("data", [])
            if isinstance(mb_ids, list):
                all_mb_ids.extend(mb_ids)
            emails = [f"{s['mailboxUsername']}@{di['domain']}" for s in SPECS]
            created_domains.append({"domain": di["domain"], "emails": emails, "mb_ids": mb_ids if isinstance(mb_ids, list) else []})
            _log(f"Created: {', '.join(emails)}")
            _time.sleep(1)

        # Step 5: Set profile photos
        if all_mb_ids:
            _log(f"Setting profile photos on {len(all_mb_ids)} mailboxes...")
            for i in range(0, len(all_mb_ids), 20):
                batch = all_mb_ids[i:i + 20]
                mb_data = [{"mailboxId": mid, "profilePicture": PHOTO_URL} for mid in batch]
                req.put(f"{ZAPMAIL_API}/v2/mailboxes", headers=zm_h(), json={"mailboxData": mb_data}, timeout=30)
                _time.sleep(1)
            _log("Photos set")

        # Save state for Phase 2
        state = store.get_state(f"generic_group_wizard_{letter}") or {}
        state.update({
            "letter": letter,
            "domains": created_domains,
            "all_mb_ids": all_mb_ids,
            "phase": "mailboxes_created",
            "created_at": _time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
        store.set_state(f"generic_group_wizard_{letter}", state)

        return _cors(jsonify({"ok": True, "letter": letter,
                              "domains_count": len(created_domains),
                              "mailboxes_count": len(all_mb_ids),
                              "log": log_lines,
                              "next_step": "finalize"}))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/finalize-generic-group", methods=["POST", "OPTIONS"])
def finalize_generic_group():
    """Phase 2: Export to SmartLead, tag with Zapmail + Generic letter + date, enable warmup."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import time as _time
        import requests as req
        body = request.get_json(silent=True) or {}
        letter = body.get("letter", "").strip().upper()
        if not letter:
            return _cors(jsonify({"error": "letter required"})), 400

        import db as store
        state = store.get_state(f"generic_group_wizard_{letter}")
        if not state or state.get("phase") != "mailboxes_created":
            return _cors(jsonify({"error": f"No pending group {letter} — run Phase 1 first"})), 400

        ZAPMAIL_API = "https://api.zapmail.ai/api"
        ZAPMAIL_KEY = os.environ.get("ZAPMAIL_API_KEY", "")
        SMARTLEAD_API = "https://server.smartlead.ai/api/v1"
        SMARTLEAD_KEY = os.environ.get("SMARTLEAD_API_KEY", "")
        SMARTLEAD_INTERNAL = "https://server.smartlead.ai/api"
        SMARTLEAD_JWT = os.environ.get("SMARTLEAD_JWT", "")
        SMARTLEAD_GQL = os.environ.get("SMARTLEAD_GQL", "https://fe-gql.smartlead.ai/v1/graphql")

        def zm_h():
            return {"x-auth-zapmail": ZAPMAIL_KEY, "Content-Type": "application/json"}
        def sl_h():
            return {"Authorization": f"Bearer {SMARTLEAD_JWT}", "Content-Type": "application/json"}

        log_lines = []
        def _log(msg):
            log_lines.append(msg)

        # Export to SmartLead
        mb_ids = state.get("all_mb_ids", [])
        if mb_ids:
            _log(f"Exporting {len(mb_ids)} mailboxes to SmartLead...")
            r = req.post(f"{ZAPMAIL_API}/v2/exports/mailboxes", headers=zm_h(),
                         json={"apps": ["SMARTLEAD"], "ids": mb_ids}, timeout=30)
            _log(f"Export: {r.json().get('message', r.text[:200])}")
        else:
            _log("No mailbox IDs — exporting by domain name...")
            for dd in state.get("domains", []):
                r = req.post(f"{ZAPMAIL_API}/v2/exports/mailboxes", headers=zm_h(),
                             json={"apps": ["SMARTLEAD"], "contains": dd["domain"]}, timeout=30)
                _time.sleep(2)

        _log("Waiting 90s for SmartLead to process...")
        _time.sleep(90)

        # Find new accounts in SmartLead
        our_domains = {dd["domain"] for dd in state.get("domains", [])}
        found_ids = []
        offset = 0
        for _ in range(20):
            url = f"{SMARTLEAD_API}/email-accounts/?api_key={SMARTLEAD_KEY}&offset={offset}&limit=100"
            r = req.get(url, timeout=30)
            accts = r.json() if r.status_code == 200 else []
            if not isinstance(accts, list) or not accts:
                break
            for a in accts:
                email = a.get("from_email", a.get("email", ""))
                domain = email.split("@")[-1] if "@" in email else ""
                if domain in our_domains:
                    found_ids.append(a.get("id"))
            offset += 100
            _time.sleep(0.5)

        _log(f"Found {len(found_ids)} accounts in SmartLead")

        if not found_ids:
            state["phase"] = "export_failed"
            store.set_state(f"generic_group_wizard_{letter}", state)
            return _cors(jsonify({"ok": False, "error": "No accounts found in SmartLead after export. Try again in a few minutes.",
                                  "log": log_lines, "retry": True}))

        # Get/create tags via GQL
        def _gql(query, variables=None):
            body = {"query": query}
            if variables:
                body["variables"] = variables
            r = req.post(SMARTLEAD_GQL, headers=sl_h(), json=body, timeout=30)
            return r.json()

        tags_result = _gql("{ tags { id name color } }")
        all_tags = {t["name"]: t for t in tags_result.get("data", {}).get("tags", [])}

        ZAPMAIL_TAG_ID = 262254
        tag_name = f"Generic {letter}"
        today_str = _time.strftime("%-m/%-d/%y")

        # Find or create group tag
        group_tag_id = None
        if tag_name in all_tags:
            group_tag_id = all_tags[tag_name]["id"]
        else:
            used_colors = {t.get("color", "").upper() for t in all_tags.values()}
            palette = ["#FF6B6B", "#FF8E72", "#FFA94D", "#FFD43B", "#A9E34B",
                       "#51CF66", "#20C997", "#22B8CF", "#339AF0", "#5C7CFA",
                       "#7950F2", "#BE4BDB", "#E64980", "#F06595", "#CC5DE8"]
            color = next((c for c in palette if c.upper() not in used_colors), "#7950F2")
            mut = """mutation($o: tags_insert_input!) { insert_tags_one(object: $o) { id name color } }"""
            result = _gql(mut, {"o": {"name": tag_name, "color": color}})
            group_tag_id = result.get("data", {}).get("insert_tags_one", {}).get("id")
            _log(f"Created tag: {tag_name} (ID: {group_tag_id})")

        # Find or create date tag
        date_tag_id = None
        if today_str in all_tags:
            date_tag_id = all_tags[today_str]["id"]
        else:
            mut = """mutation($o: tags_insert_input!) { insert_tags_one(object: $o) { id name color } }"""
            result = _gql(mut, {"o": {"name": today_str, "color": "#94a3b8"}})
            date_tag_id = result.get("data", {}).get("insert_tags_one", {}).get("id")
            _log(f"Created date tag: {today_str} (ID: {date_tag_id})")

        if not group_tag_id or not date_tag_id:
            return _cors(jsonify({"error": "Failed to create tags", "log": log_lines})), 500

        # Tag all accounts
        tag_ids = [ZAPMAIL_TAG_ID, group_tag_id, date_tag_id]
        tagged = 0
        for acc_id in found_ids:
            tag_body = {"id": acc_id, "tags": tag_ids, "clientId": None}
            r = req.post(f"{SMARTLEAD_INTERNAL}/email-account/save-management-details",
                         headers=sl_h(), json=tag_body, timeout=30)
            if r.status_code == 200:
                tagged += 1
            _time.sleep(0.3)
        _log(f"Tagged {tagged}/{len(found_ids)} accounts")

        # Enable warmup
        warmed = 0
        warmup_body = {"warmup_enabled": True, "total_warmup_per_day": 15,
                       "daily_rampup": 5, "reply_rate_percentage": 40}
        for acc_id in found_ids:
            r = req.post(f"{SMARTLEAD_API}/email-accounts/{acc_id}/warmup?api_key={SMARTLEAD_KEY}",
                         json=warmup_body, timeout=30)
            if r.status_code == 200:
                warmed += 1
            _time.sleep(0.3)

            # Full warmup config via internal API
            wd = req.get(f"{SMARTLEAD_INTERNAL}/email-account/fetch-warmup-details-by-email-account-id/{acc_id}",
                         headers=sl_h(), timeout=30)
            warmup_key = ""
            if wd.status_code == 200:
                warmup_key = wd.json().get("message", {}).get("warmup_key_id", "")
            if warmup_key:
                full_body = {
                    "emailAccountId": str(acc_id), "maxEmailPerDay": 15,
                    "isRampupEnabled": True, "rampupValue": 5,
                    "warmupMinCount": 10, "warmupMaxCount": 15,
                    "replyRate": 40, "dailyReplyLimit": 15,
                    "autoAdjustWarmup": False, "sendWarmupsOnlyOnWeekdays": False,
                    "useCustomDomain": False, "status": "ACTIVE", "warmupKeyId": warmup_key
                }
                req.post(f"{SMARTLEAD_INTERNAL}/email-account/save-warmup",
                         headers=sl_h(), json=full_body, timeout=30)
            _time.sleep(0.3)

        _log(f"Warmup enabled on {warmed}/{len(found_ids)} accounts")

        # Update state
        state["phase"] = "complete"
        state["accounts_found"] = len(found_ids)
        state["accounts_tagged"] = tagged
        state["accounts_warmed"] = warmed
        store.set_state(f"generic_group_wizard_{letter}", state)

        return _cors(jsonify({"ok": True, "letter": letter,
                              "accounts_found": len(found_ids),
                              "accounts_tagged": tagged,
                              "accounts_warmed": warmed,
                              "log": log_lines}))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/<path:path>", methods=["GET", "OPTIONS"])
def catch_all(path):
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    return _cors(jsonify({"error": "Not found"})), 404
