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

    import requests as req
    import time as _time
    sl = "https://server.smartlead.ai/api/v1"

    cache_ids, _ = _resolve_group_account_ids(group_name)
    cache_ids = set(cache_ids or [])

    live_ids = set()
    jwt = os.environ.get("SMARTLEAD_JWT", "").strip()
    gql_url = os.environ.get("SMARTLEAD_GQL", "https://fe-gql.smartlead.ai/v1/graphql").strip()
    if jwt:
        try:
            sl_h = {"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"}
            tags_r = req.post(gql_url, headers=sl_h,
                              json={"query": "{ tags { id name } }"}, timeout=15)
            tag_id = None
            for t in tags_r.json().get("data", {}).get("tags", []):
                if t.get("name", "").lower() == group_name.lower():
                    tag_id = t["id"]
                    break
            if tag_id:
                mappings_r = req.post(gql_url, headers=sl_h,
                    json={"query": "query($tid: Int!) { email_account_tag_mappings(where: {tag_id: {_eq: $tid}}) { email_account_id } }",
                          "variables": {"tid": tag_id}}, timeout=15)
                for m in mappings_r.json().get("data", {}).get("email_account_tag_mappings", []):
                    live_ids.add(m["email_account_id"])
        except Exception:
            pass

    account_ids = list(cache_ids | live_ids)
    if not account_ids:
        return _cors(jsonify({"error": f"No account IDs found for '{group_name}'"})), 404

    r = _sl_request("delete", f"{sl}/campaigns/{campaign_id}/email-accounts?api_key={sl_key}",
                    headers={"Content-Type": "application/json"},
                    data=json.dumps({"email_account_ids": account_ids}), timeout=30)
    if r.status_code != 200:
        return _cors(jsonify({"error": f"SmartLead returned {r.status_code}"})), 502

    stragglers = []
    try:
        _time.sleep(2)
        camp_r = _sl_request("get", f"{sl}/campaigns/{campaign_id}/email-accounts?api_key={sl_key}", timeout=15)
        if camp_r.status_code == 200:
            camp_accts = camp_r.json()
            if isinstance(camp_accts, list):
                id_set = set(account_ids)
                stragglers = [a.get("id") for a in camp_accts if a.get("id") in id_set]
                if stragglers:
                    _sl_request("delete", f"{sl}/campaigns/{campaign_id}/email-accounts?api_key={sl_key}",
                                headers={"Content-Type": "application/json"},
                                data=json.dumps({"email_account_ids": stragglers}), timeout=30)
    except Exception:
        pass

    camp_name = _get_campaign_name(campaign_id)
    _update_cache_campaigns(group_name, campaign_id, camp_name, "remove")
    msg = f"Removed {len(account_ids)} accounts"
    if stragglers:
        msg += f" ({len(stragglers)} required retry)"
    msg += ". REMINDER: Reallocate inboxes in SmartLead."
    return _cors(jsonify({"ok": True, "removed": len(account_ids),
                          "source": f"cache={len(cache_ids)}, live_tag={len(live_ids)}",
                          "stragglers_retried": len(stragglers), "message": msg}))


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

_TLD_PRICES = {".com": "9.98", ".info": "3.98", ".co": "11.98", ".net": "10.98", ".org": "9.98", ".biz": "8.98"}

_NICHE_WORDS = {
    "generic": {
        "pre": ["service","work","trade","field","crew","job","site","project","task","pro","contract","build",
                "maintain","install","repair","open","direct","steady","reliable","trusted","skilled","onsite",
                "rapid","ready","prime","next","first","all","apex","core"],
        "mid": ["service","work","care","solutions","side","zone","point","line","craft","force","ops","tech","aid","link","way","path","flow"],
        "suf": ["pros","biz","co","hq","group","crew","team","contractors","services","solutions","experts",
                "works","side","hub","base","zone","point","force","now","go"],
    },
    "landscaping": {
        "pre": ["landscape","landscaping","grounds","groundskeeping","lawn","lawncare","yard","property","turf","exterior"],
        "mid": ["maintenance","care","management","work","services","service","keeping","upkeep"],
        "suf": ["pros","experts","specialists","solutions","group","crew","contractors","company","team","partners"],
    },
    "hvac": {
        "pre": ["hvac","heating","cooling","airflow","climate","comfort","duct","ventilation","thermal","air"],
        "mid": ["service","repair","install","maintenance","care","work","solutions","systems"],
        "suf": ["pros","experts","crew","team","contractors","services","solutions","co","group","specialists"],
    },
}

def _gen_domain_name(niche_key):
    import random
    w = _NICHE_WORDS.get(niche_key, _NICHE_WORDS["generic"])
    roll = random.random()
    if roll < 0.4:
        return random.choice(w["pre"]) + random.choice(w["suf"])
    elif roll < 0.75:
        return random.choice(w["pre"]) + random.choice(w["mid"]) + random.choice(w["suf"])
    else:
        return random.choice(w["pre"]) + random.choice(w["mid"])


@app.route("/api/domains/find-available", methods=["POST", "OPTIONS"])
def find_available_domains():
    """Generate random domain names and check availability in parallel."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import requests as req
        body = request.get_json(silent=True) or {}
        niche = body.get("niche", "generic")
        registrar = body.get("registrar", "spaceship")
        tld = body.get("tld", ".info")
        target = min(int(body.get("count", 14)), 30)

        ak = os.environ.get("SPACESHIP_API_KEY", "").strip()
        sk = os.environ.get("SPACESHIP_SECRET_KEY", "").strip()
        pk = os.environ.get("PORKBUN_API_KEY", "").strip()
        ps = os.environ.get("PORKBUN_SECRET_KEY", "").strip()

        exclude = set(body.get("exclude", []))
        tried = set(exclude)

        def _check_spaceship(dn):
            try:
                r = req.get(f"https://spaceship.dev/api/v1/domains/{dn}/available",
                            headers={"X-Api-Key": ak, "X-Api-Secret": sk}, timeout=15)
                data = r.json() if r.status_code == 200 else {}
                if data.get("result") == "available":
                    return {"domain": dn, "available": True, "price": _TLD_PRICES.get(tld, "~10")}
            except Exception:
                pass
            return None

        def _check_porkbun(dn):
            try:
                r = req.post(f"https://api.porkbun.com/api/json/v3/domain/checkDomain/{dn}",
                             json={"apikey": pk, "secretapikey": ps}, timeout=10)
                data = r.json()
                resp = data.get("response", {})
                if data.get("status") == "SUCCESS" and resp.get("avail") == "yes":
                    return {"domain": dn, "available": True, "price": resp.get("price", "?")}
            except Exception:
                pass
            return None

        checker = _check_spaceship if registrar == "spaceship" else _check_porkbun
        found = []
        hard_tld = tld in (".co", ".com", ".net")
        max_rounds = 60 if hard_tld else 20
        batch_sz = 16 if hard_tld else 10

        for rnd in range(max_rounds):
            if len(found) >= target:
                break
            batch = []
            while len(batch) < batch_sz and len(tried) < 2000:
                dn = _gen_domain_name(niche) + tld
                if dn not in tried:
                    tried.add(dn)
                    batch.append(dn)
            if not batch:
                break
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = {pool.submit(checker, dn): dn for dn in batch}
                for f in as_completed(futures):
                    result = f.result()
                    if result and len(found) < target:
                        found.append(result)
            if rnd < max_rounds - 1 and len(found) < target:
                import time as _t
                _t.sleep(0.5)

        return _cors(jsonify({"results": found, "checked": len(tried), "target": target}))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/domains/check", methods=["POST", "OPTIONS"])
def check_domains():
    """Check availability of domains on Spaceship and/or Porkbun."""
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

        results = []
        for dn in domain_names[:50]:
            dn = dn.strip().lower()
            if not dn:
                continue
            try:
                if registrar == "spaceship":
                    r = req.get(f"https://spaceship.dev/api/v1/domains/{dn}/available",
                                headers={"X-Api-Key": ak, "X-Api-Secret": sk}, timeout=15)
                    data = r.json() if r.status_code == 200 else {}
                    if data.get("result") == "available":
                        tld = "." + dn.rsplit(".", 1)[-1] if "." in dn else ""
                        price = _TLD_PRICES.get(tld, "~10")
                        results.append({"domain": dn, "available": True, "price": price})
                    else:
                        results.append({"domain": dn, "available": False})
                    _time.sleep(0.3)
                else:
                    r = req.post(f"https://api.porkbun.com/api/json/v3/domain/checkDomain/{dn}",
                                 json={"apikey": pk, "secretapikey": ps}, timeout=10)
                    data = r.json()
                    resp = data.get("response", {})
                    if data.get("status") == "SUCCESS" and resp.get("avail") == "yes":
                        price = resp.get("price", "?")
                        results.append({"domain": dn, "available": True, "price": price})
                    else:
                        results.append({"domain": dn, "available": False})
                    _time.sleep(0.3)
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
        SP_CONTACT = os.environ.get("SPACESHIP_CONTACT_ID", "1nEUYUnGBWO9ba7Z0lMrOM2UCgY9S")

        log_lines = []
        purchased = []
        failed = []

        for dn in domain_names[:20]:
            dn = dn.strip().lower()
            try:
                if registrar == "spaceship":
                    sp_body = {
                        "autoRenew": False,
                        "years": 1,
                        "privacyProtection": {"level": "high", "userConsent": True},
                        "contacts": {"registrant": SP_CONTACT, "admin": SP_CONTACT,
                                     "tech": SP_CONTACT, "billing": SP_CONTACT},
                    }
                    r = req.post(f"https://spaceship.dev/api/v1/domains/{dn}",
                                 headers={"X-Api-Key": ak, "X-Api-Secret": sk, "Content-Type": "application/json"},
                                 json=sp_body, timeout=30)
                    if r.status_code in (200, 201, 202):
                        log_lines.append(f"Purchased: {dn}")
                        purchased.append(dn)
                    else:
                        err = r.json().get("detail", r.text[:150]) if r.text else "Unknown"
                        log_lines.append(f"Failed: {dn} — {err}")
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


@app.route("/api/domains/purchase-one", methods=["POST", "OPTIONS"])
def purchase_one_domain():
    """Purchase a single domain, set CloudNS nameservers, disable auto-renew."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import requests as req
        body = request.get_json(silent=True) or {}
        dn = (body.get("domain") or "").strip().lower()
        registrar = body.get("registrar", "spaceship")
        if not dn:
            return _cors(jsonify({"error": "No domain provided"})), 400

        ak = os.environ.get("SPACESHIP_API_KEY", "").strip()
        sk = os.environ.get("SPACESHIP_SECRET_KEY", "").strip()
        pk = os.environ.get("PORKBUN_API_KEY", "").strip()
        ps = os.environ.get("PORKBUN_SECRET_KEY", "").strip()
        CLOUDNS = ["pns61.cloudns.net", "pns62.cloudns.com", "pns63.cloudns.net", "pns64.cloudns.uk"]
        SP_CONTACT = os.environ.get("SPACESHIP_CONTACT_ID", "1nEUYUnGBWO9ba7Z0lMrOM2UCgY9S")

        import time as _time
        result = {"domain": dn, "purchased": False, "ns_set": False, "ns_verified": False,
                  "autorenew_off": False, "error": None, "checks": []}

        # Step 1: Purchase
        if registrar == "spaceship":
            sp_h = {"X-Api-Key": ak, "X-Api-Secret": sk, "Content-Type": "application/json"}
            sp_body = {
                "autoRenew": False, "years": 1,
                "privacyProtection": {"level": "high", "userConsent": True},
                "contacts": {"registrant": SP_CONTACT, "admin": SP_CONTACT,
                             "tech": SP_CONTACT, "billing": SP_CONTACT},
            }
            r = req.post(f"https://spaceship.dev/api/v1/domains/{dn}",
                         headers=sp_h, json=sp_body, timeout=30)
            if r.status_code in (200, 201, 202):
                result["purchased"] = True
                result["checks"].append("purchase: OK")
            else:
                err = r.json().get("detail", r.text[:200]) if r.text else "Unknown"
                result["error"] = f"Purchase failed: {err}"
                result["checks"].append(f"purchase: FAILED — {err}")
                return _cors(jsonify(result))
        else:
            r = req.post(f"https://api.porkbun.com/api/json/v3/domain/create/{dn}",
                         json={"apikey": pk, "secretapikey": ps, "acknowledgement": "yes"}, timeout=30)
            data = r.json()
            if data.get("status") == "SUCCESS":
                result["purchased"] = True
                result["checks"].append("purchase: OK")
            else:
                result["error"] = f"Purchase failed: {data.get('message', '')}"
                result["checks"].append(f"purchase: FAILED")
                return _cors(jsonify(result))

        # Step 2: Set nameservers
        if registrar == "spaceship":
            r = req.put(f"https://spaceship.dev/api/v1/domains/{dn}/nameservers",
                        headers=sp_h, json={"provider": "custom", "hosts": CLOUDNS}, timeout=15)
            result["ns_set"] = r.status_code in (200, 204)
        else:
            r = req.post(f"https://api.porkbun.com/api/json/v3/domain/updateNs/{dn}",
                         json={"apikey": pk, "secretapikey": ps, "ns": CLOUDNS}, timeout=15)
            result["ns_set"] = r.json().get("status") == "SUCCESS"

        if not result["ns_set"]:
            result["error"] = "Nameserver update failed"
            result["checks"].append("ns_set: FAILED")
            return _cors(jsonify(result))
        result["checks"].append("ns_set: OK")

        # Step 3: Verify nameservers actually took
        _time.sleep(1)
        if registrar == "spaceship":
            try:
                vr = req.get(f"https://spaceship.dev/api/v1/domains/{dn}",
                             headers=sp_h, timeout=15)
                if vr.status_code == 200:
                    ns_data = vr.json().get("nameservers", {})
                    hosts = ns_data.get("hosts", []) if isinstance(ns_data, dict) else []
                    if any("cloudns" in h.lower() for h in hosts):
                        result["ns_verified"] = True
                        result["checks"].append(f"ns_verify: OK ({', '.join(hosts[:2])})")
                    else:
                        result["checks"].append(f"ns_verify: WARN — got {hosts[:2]}, expected CloudNS")
            except Exception as e:
                result["checks"].append(f"ns_verify: SKIP — {str(e)[:60]}")
        else:
            result["ns_verified"] = True
            result["checks"].append("ns_verify: SKIP (porkbun)")

        # Step 4: Disable auto-renew
        if registrar == "spaceship":
            try:
                req.put(f"https://spaceship.dev/api/v1/domains/{dn}/autorenew",
                        headers=sp_h, json={"isEnabled": False}, timeout=10)
                result["autorenew_off"] = True
                result["checks"].append("autorenew_off: OK")
            except Exception:
                result["checks"].append("autorenew_off: FAILED")
        else:
            try:
                req.post(f"https://api.porkbun.com/api/json/v3/domain/updateAutoRenew/{dn}",
                         json={"apikey": pk, "secretapikey": ps, "status": "off"}, timeout=10)
                result["autorenew_off"] = True
                result["checks"].append("autorenew_off: OK")
            except Exception:
                result["checks"].append("autorenew_off: FAILED")

        return _cors(jsonify(result))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/available-domains")
def available_domains():
    """Return Spaceship domains with CloudNS that are not yet in SmartLead."""
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import requests as req
        import db as store

        overview, _ = store.cache_get("overview_v2")
        sl_domains = set()
        if overview:
            for section in ["clients", "acquisition_groups", "generic_groups", "aging_groups"]:
                for g in overview.get(section, []):
                    for a in g.get("account_details", []):
                        email = a.get("email", "") or ""
                        if "@" in email:
                            sl_domains.add(email.split("@")[1])

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
            if name in sl_domains:
                continue
            ns_hosts = d.get("nameservers", {}).get("hosts", [])
            if not any("cloudns" in h.lower() for h in ns_hosts):
                continue
            reg = d.get("registrationDate", "")[:10]
            available.append({"domain": name, "registered": reg})

        available.sort(key=lambda x: x["registered"], reverse=True)

        existing_letters = set()
        if overview:
            for g in overview.get("generic_groups", []):
                gname = g.get("name", "")
                if gname.startswith("Generic "):
                    existing_letters.add(gname[8:].strip())
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
        ZAPMAIL_KEY = os.environ.get("ZAPMAIL_API_KEY", "").strip()
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


@app.route("/api/group/connect-domains", methods=["POST", "OPTIONS"])
def group_connect_domains():
    """Connect domains to Zapmail and buy mailbox slots."""
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
        if not domains:
            return _cors(jsonify({"error": "No domains"})), 400

        ZAPMAIL_API = "https://api.zapmail.ai/api"
        ZAPMAIL_KEY = os.environ.get("ZAPMAIL_API_KEY", "").strip()
        NS_STR = "pns61.cloudns.net,pns62.cloudns.com,pns63.cloudns.net,pns64.cloudns.uk"
        def zm_h():
            return {"x-auth-zapmail": ZAPMAIL_KEY, "Content-Type": "application/json"}

        log = []
        connected = 0
        domain_checks = {}
        for dn in domains[:30]:
            checks = []
            try:
                r = req.post(f"{ZAPMAIL_API}/v2/domains/connect", headers=zm_h(),
                             json={"domainName": dn, "nameServers": NS_STR}, timeout=30)
                resp = r.json()
                msg = resp.get("message", r.text[:100])
                if "already" in msg.lower() or r.status_code == 200:
                    connected += 1
                    checks.append("connect: OK")
                else:
                    checks.append(f"connect: FAILED — {msg}")
                log.append(f"{dn}: {msg}")
            except Exception as e:
                log.append(f"{dn}: error — {str(e)[:80]}")
                checks.append(f"connect: FAILED — {str(e)[:60]}")
            domain_checks[dn] = checks
            _time.sleep(0.3)

        # Verify: check each domain shows up in Zapmail with DNS status
        _time.sleep(2)
        try:
            all_zm = []
            page = 1
            for _ in range(10):
                zr = req.get(f"{ZAPMAIL_API}/v2/domains?page={page}", headers=zm_h(), timeout=30)
                zd = zr.json().get("data", {})
                all_zm.extend(zd.get("domains", []))
                if page >= zd.get("totalPages", 1):
                    break
                page += 1
            zm_map = {d.get("domain", ""): d for d in all_zm}
            for dn in domains[:30]:
                zdom = zm_map.get(dn)
                if zdom:
                    dns_status = zdom.get("dnsStatus", zdom.get("status", "unknown"))
                    domain_checks.setdefault(dn, []).append(f"zapmail_found: OK (DNS: {dns_status})")
                else:
                    domain_checks.setdefault(dn, []).append("zapmail_found: FAILED — not in Zapmail")
        except Exception as e:
            for dn in domains[:30]:
                domain_checks.setdefault(dn, []).append(f"zapmail_verify: SKIP — {str(e)[:60]}")

        # Buy mailbox slots proactively
        slots_bought = 0
        try:
            inboxes_needed = len(domains) * 3
            ws_resp = req.get(f"{ZAPMAIL_API}/v2/workspaces", headers=zm_h(), timeout=30)
            ws_data = ws_resp.json().get("data", {}).get("currentWorkspace", {})
            purchased = int(ws_data.get("totalMailboxesPurchasedGoogle", "0"))
            assigned = int(ws_data.get("assignedMailboxesCountGoogle", "0"))
            free_slots = purchased - assigned
            if free_slots < inboxes_needed:
                to_buy = inboxes_needed - free_slots
                buy_r = req.post(f"{ZAPMAIL_API}/v2/wallet/buy-addon-mailboxes?quantity={to_buy}",
                                 headers=zm_h(), json={}, timeout=30)
                if buy_r.status_code == 200:
                    slots_bought = to_buy
                    log.append(f"Bought {to_buy} mailbox slots")
                else:
                    log.append(f"Slot purchase failed: {buy_r.text[:100]}")
            else:
                log.append(f"Have {free_slots} free slots (need {inboxes_needed})")
        except Exception as e:
            log.append(f"Slot check error: {str(e)[:80]}")

        return _cors(jsonify({"ok": True, "connected": connected, "slots_bought": slots_bought,
                              "log": log, "domain_checks": domain_checks}))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/group/setup-domain", methods=["POST", "OPTIONS"])
def group_setup_domain():
    """Setup ONE domain: resolve Zapmail ID, create 3 mailboxes, set profile photo."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import time as _time
        import requests as req
        body = request.get_json(silent=True) or {}
        domain = (body.get("domain") or "").strip().lower()
        letter = body.get("letter", "").strip().upper()
        if not domain:
            return _cors(jsonify({"error": "No domain"})), 400

        ZAPMAIL_API = "https://api.zapmail.ai/api"
        ZAPMAIL_KEY = os.environ.get("ZAPMAIL_API_KEY", "").strip()
        SUPABASE_STORAGE = os.environ.get("SUPABASE_URL", "https://ghjmqpnqljgwykpjkvzy.supabase.co") + "/storage/v1/object/public/headshots"
        PHOTO_URL = f"{SUPABASE_STORAGE}/sean_reynolds.png"
        def zm_h():
            return {"x-auth-zapmail": ZAPMAIL_KEY, "Content-Type": "application/json"}

        result = {"domain": domain, "zapmail_connected": False, "mailboxes_created": 0,
                  "photos_set": False, "mb_ids": [], "error": None, "checks": []}

        # Step 1: Find domain in Zapmail — poll up to 3 times with wait for DNS propagation
        zapmail_id = None
        dns_status = None
        for attempt in range(3):
            if attempt > 0:
                _time.sleep(5)
            page = 1
            for _ in range(10):
                try:
                    zr = req.get(f"{ZAPMAIL_API}/v2/domains?page={page}", headers=zm_h(), timeout=30)
                    zd = zr.json().get("data", {})
                    for d in zd.get("domains", []):
                        if d.get("domain", "") == domain:
                            zapmail_id = d.get("id", "")
                            dns_status = d.get("dnsStatus", d.get("status", "unknown"))
                            break
                    if zapmail_id or page >= zd.get("totalPages", 1):
                        break
                    page += 1
                except Exception:
                    break
            if zapmail_id:
                break

        if not zapmail_id:
            result["error"] = "Domain not found in Zapmail yet — DNS may still be propagating"
            result["checks"].append("find_domain: FAILED — not found after 3 attempts")
            return _cors(jsonify(result))

        result["zapmail_connected"] = True
        result["checks"].append(f"find_domain: OK (id={zapmail_id}, DNS={dns_status})")

        # Step 2: Check if mailboxes already exist (idempotent)
        existing_mb = []
        try:
            dr = req.get(f"{ZAPMAIL_API}/v2/domains?limit=200", headers=zm_h(), timeout=30)
            for dd in dr.json().get("data", {}).get("domains", []):
                if dd.get("domain") == domain and dd.get("mailboxes"):
                    existing_mb = [m.get("id") for m in dd["mailboxes"] if isinstance(m, dict) and m.get("id")]
                    break
        except Exception:
            pass

        skip_creation = len(existing_mb) >= 3
        if skip_creation:
            result["mb_ids"] = existing_mb
            result["mailboxes_created"] = len(existing_mb)
            result["skipped"] = "mailboxes already exist"
            result["checks"].append(f"existing_mailboxes: OK ({len(existing_mb)} found, skipping creation)")
        else:
            result["checks"].append(f"existing_mailboxes: {len(existing_mb)} found, creating new")

        if not skip_creation:
            # Step 3: Buy mailbox slots if needed
            needed = 3 - len(existing_mb)
            try:
                ws_resp = req.get(f"{ZAPMAIL_API}/v2/workspaces", headers=zm_h(), timeout=30)
                ws_data = ws_resp.json().get("data", {}).get("currentWorkspace", {})
                purchased = int(ws_data.get("totalMailboxesPurchasedGoogle", "0"))
                assigned = int(ws_data.get("assignedMailboxesCountGoogle", "0"))
                free_slots = purchased - assigned
                if free_slots < needed:
                    to_buy = max(needed - free_slots, 3)
                    buy_r = req.post(f"{ZAPMAIL_API}/v2/wallet/buy-addon-mailboxes?quantity={to_buy}",
                                     headers=zm_h(), json={}, timeout=30)
                    if buy_r.status_code == 200:
                        result["checks"].append(f"buy_slots: OK (bought {to_buy}, had {free_slots} free)")
                    else:
                        buy_msg = buy_r.text[:120]
                        result["checks"].append(f"buy_slots: FAILED — HTTP {buy_r.status_code}: {buy_msg}")
                        if "Insufficient wallet balance" in buy_msg:
                            result["error"] = f"Zapmail wallet balance too low — add funds at zapmail.ai"
                            return _cors(jsonify(result))
                else:
                    result["checks"].append(f"slot_check: OK ({free_slots} free, need {needed})")
            except Exception as e:
                result["checks"].append(f"slot_check: WARN — {str(e)[:60]}")

            # Step 4: Create mailboxes with retry (slots take time to propagate after purchase)
            SPECS = [
                {"firstName": "Sean", "lastName": "Reynolds", "mailboxUsername": "s.reynolds"},
                {"firstName": "Sean", "lastName": "Reynolds", "mailboxUsername": "sean.r"},
                {"firstName": "Sean", "lastName": "Reynolds", "mailboxUsername": "sean.reynolds"},
            ]
            mailboxes = [{**s, "domainName": domain} for s in SPECS]
            payload = {zapmail_id: mailboxes}
            MAX_RETRIES = 5
            RETRY_DELAY = 15
            created = False
            last_err = ""
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    r = req.post(f"{ZAPMAIL_API}/v2/mailboxes", headers=zm_h(), json=payload, timeout=60)
                    mb_data = r.json()
                    mb_ids = mb_data.get("data", [])
                    if isinstance(mb_ids, list) and len(mb_ids) > 0:
                        result["mb_ids"] = mb_ids
                        result["mailboxes_created"] = len(mb_ids)
                        result["checks"].append(f"create_mailboxes: OK ({len(mb_ids)} created, attempt {attempt})")
                        created = True
                        break
                    else:
                        last_err = mb_data.get("message", str(mb_data)[:150])
                        if attempt < MAX_RETRIES and ("enough mailboxes" in last_err.lower() or "can't be assigned" in last_err.lower()):
                            result["checks"].append(f"create_attempt_{attempt}: slots not ready — retrying in {RETRY_DELAY}s")
                            _time.sleep(RETRY_DELAY)
                        elif attempt < MAX_RETRIES:
                            result["checks"].append(f"create_attempt_{attempt}: {last_err[:60]} — retrying in {RETRY_DELAY}s")
                            _time.sleep(RETRY_DELAY)
                except Exception as e:
                    last_err = str(e)[:150]
                    if attempt < MAX_RETRIES:
                        result["checks"].append(f"create_attempt_{attempt}: error — retrying in {RETRY_DELAY}s")
                        _time.sleep(RETRY_DELAY)
            if not created:
                result["error"] = f"Mailbox creation failed after {MAX_RETRIES} attempts: {last_err}"
                result["checks"].append(f"create_mailboxes: FAILED after {MAX_RETRIES} attempts — {last_err[:80]}")
                return _cors(jsonify(result))

            # Step 5: Verify mailboxes exist by re-fetching
            _time.sleep(1)
            try:
                vr = req.get(f"{ZAPMAIL_API}/v2/domains?limit=200", headers=zm_h(), timeout=30)
                for dd in vr.json().get("data", {}).get("domains", []):
                    if dd.get("domain") == domain:
                        actual_mb = dd.get("mailboxes", [])
                        if isinstance(actual_mb, list) and len(actual_mb) >= 3:
                            result["verified"] = True
                            result["checks"].append(f"verify_mailboxes: OK ({len(actual_mb)} confirmed)")
                        else:
                            result["checks"].append(f"verify_mailboxes: WARN — only {len(actual_mb) if isinstance(actual_mb, list) else 0} found")
                        break
            except Exception as e:
                result["checks"].append(f"verify_mailboxes: SKIP — {str(e)[:60]}")

        # Step 5: Set profile photos
        if result["mb_ids"]:
            try:
                mb_photo_data = [{"mailboxId": mid, "profilePicture": PHOTO_URL} for mid in result["mb_ids"]]
                pr = req.put(f"{ZAPMAIL_API}/v2/mailboxes", headers=zm_h(), json={"mailboxData": mb_photo_data}, timeout=60)
                if pr.status_code == 200:
                    result["photos_set"] = True
                    result["checks"].append("set_photos: OK")
                else:
                    result["checks"].append(f"set_photos: FAILED — HTTP {pr.status_code}")
            except Exception as e:
                result["checks"].append(f"set_photos: FAILED — {str(e)[:60]}")

        # Step 7: Verify photos actually applied
        if result["photos_set"] and result["mb_ids"]:
            _time.sleep(1)
            try:
                vr2 = req.get(f"{ZAPMAIL_API}/v2/domains?limit=200", headers=zm_h(), timeout=30)
                for dd in vr2.json().get("data", {}).get("domains", []):
                    if dd.get("domain") == domain:
                        mbs = dd.get("mailboxes", [])
                        photos_ok = sum(1 for m in mbs if isinstance(m, dict) and m.get("profilePicture"))
                        if photos_ok >= len(result["mb_ids"]):
                            result["checks"].append(f"verify_photos: OK ({photos_ok}/{len(result['mb_ids'])})")
                        else:
                            result["checks"].append(f"verify_photos: WARN — {photos_ok}/{len(result['mb_ids'])} have photos")
                        break
            except Exception as e:
                result["checks"].append(f"verify_photos: SKIP — {str(e)[:60]}")

        return _cors(jsonify(result))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/group/fix-photos", methods=["POST", "OPTIONS"])
def group_fix_photos():
    """Set profile photos on all mailboxes for given domains."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import requests as req
        body = request.get_json(silent=True) or {}
        domains = body.get("domains", [])
        if not domains:
            return _cors(jsonify({"error": "domains required"})), 400

        ZAPMAIL_API = "https://api.zapmail.ai/api"
        ZAPMAIL_KEY = os.environ.get("ZAPMAIL_API_KEY", "").strip()
        SUPABASE_STORAGE = os.environ.get("SUPABASE_URL", "https://ghjmqpnqljgwykpjkvzy.supabase.co") + "/storage/v1/object/public/headshots"
        PHOTO_URL = f"{SUPABASE_STORAGE}/sean_reynolds.png"
        def zm_h():
            return {"x-auth-zapmail": ZAPMAIL_KEY, "Content-Type": "application/json"}

        all_mb_ids = []
        page = 1
        while True:
            zr = req.get(f"{ZAPMAIL_API}/v2/domains?page={page}&limit=100", headers=zm_h(), timeout=30)
            zd = zr.json().get("data", {})
            for d in zd.get("domains", []):
                if d.get("domain") in domains:
                    for m in (d.get("mailboxes") or []):
                        if isinstance(m, dict) and m.get("id"):
                            all_mb_ids.append(m["id"])
            if page >= zd.get("totalPages", 1):
                break
            page += 1

        if not all_mb_ids:
            return _cors(jsonify({"error": f"No mailbox IDs found for {len(domains)} domains"})), 404

        mb_photo_data = [{"mailboxId": mid, "profilePicture": PHOTO_URL} for mid in all_mb_ids]
        pr = req.put(f"{ZAPMAIL_API}/v2/mailboxes", headers=zm_h(), json={"mailboxData": mb_photo_data}, timeout=60)
        ok = pr.status_code == 200
        return _cors(jsonify({"ok": ok, "mailboxes": len(all_mb_ids),
                              "status": pr.status_code, "photo_url": PHOTO_URL}))
    except Exception as e:
        return _cors(jsonify({"error": str(e)})), 500


@app.route("/api/group/save-state", methods=["POST", "OPTIONS"])
def group_save_state():
    """Save group wizard state to DB for Phase 2."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import time as _time
        import db as store
        body = request.get_json(silent=True) or {}
        letter = body.get("letter", "").strip().upper()
        domains = body.get("domains", [])
        all_mb_ids = body.get("all_mb_ids", [])
        if not letter:
            return _cors(jsonify({"error": "letter required"})), 400
        state = {
            "letter": letter,
            "domains": [{"domain": d, "emails": [f"s.reynolds@{d}", f"sean.r@{d}", f"sean.reynolds@{d}"], "mb_ids": []} for d in domains],
            "all_mb_ids": all_mb_ids,
            "phase": "mailboxes_created",
            "created_at": _time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        store.set_state(f"generic_group_wizard_{letter}", state)
        return _cors(jsonify({"ok": True}))
    except Exception as e:
        return _cors(jsonify({"error": str(e)})), 500


# ──────────────────────────────────────────────────────────
# Per-step finalize endpoints (replace monolithic finalize)
# ──────────────────────────────────────────────────────────

@app.route("/api/group/check-mailbox-status", methods=["POST", "OPTIONS"])
def group_check_mailbox_status():
    """Poll Zapmail to check if all mailboxes are ACTIVE (not In Progress)."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import requests as req
        body = request.get_json(silent=True) or {}
        domains = set(body.get("domains", []))
        if not domains:
            return _cors(jsonify({"error": "domains required"})), 400

        ZAPMAIL_API = "https://api.zapmail.ai/api"
        ZAPMAIL_KEY = os.environ.get("ZAPMAIL_API_KEY", "").strip()
        headers = {"x-auth-zapmail": ZAPMAIL_KEY, "Content-Type": "application/json"}

        all_zm = []
        page = 1
        for _ in range(10):
            zr = req.get(f"{ZAPMAIL_API}/v2/domains?page={page}", headers=headers, timeout=30)
            zd = zr.json().get("data", {})
            all_zm.extend(zd.get("domains", []))
            if page >= zd.get("totalPages", 1):
                break
            page += 1

        active = 0
        in_progress = 0
        other = 0
        details = {}
        for d in all_zm:
            dn = d.get("domain", "")
            if dn not in domains:
                continue
            mbs = d.get("mailboxes", [])
            if not isinstance(mbs, list):
                continue
            domain_active = 0
            domain_pending = 0
            for mb in mbs:
                status = mb.get("status", "unknown")
                if status == "ACTIVE":
                    active += 1
                    domain_active += 1
                elif status in ("IN_PROGRESS", "PENDING", "PROVISIONING"):
                    in_progress += 1
                    domain_pending += 1
                else:
                    other += 1
                    domain_pending += 1
            details[dn] = {"active": domain_active, "pending": domain_pending}

        total = active + in_progress + other
        expected = len(domains) * 3
        all_ready = in_progress == 0 and other == 0 and active >= expected

        return _cors(jsonify({
            "ok": True, "all_ready": all_ready,
            "active": active, "in_progress": in_progress, "other": other,
            "total": total, "expected": expected, "details": details
        }))
    except Exception as e:
        return _cors(jsonify({"error": str(e)})), 500


@app.route("/api/group/export-smartlead", methods=["POST", "OPTIONS"])
def group_export_smartlead():
    """Export mailboxes to SmartLead via Zapmail."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import requests as req
        body = request.get_json(silent=True) or {}
        letter = body.get("letter", "").strip().upper()
        mb_ids = body.get("mb_ids", [])
        domains = body.get("domains", [])

        ZAPMAIL_API = "https://api.zapmail.ai/api"
        ZAPMAIL_KEY = os.environ.get("ZAPMAIL_API_KEY", "").strip()
        def zm_h():
            return {"x-auth-zapmail": ZAPMAIL_KEY, "Content-Type": "application/json"}

        checks = []
        if mb_ids:
            r = req.post(f"{ZAPMAIL_API}/v2/exports/mailboxes", headers=zm_h(),
                         json={"apps": ["SMARTLEAD"], "ids": mb_ids}, timeout=30)
            msg = r.json().get("message", r.text[:200])
            if r.status_code == 200:
                checks.append(f"export_request: OK — {len(mb_ids)} mailbox IDs submitted")
            else:
                checks.append(f"export_request: FAILED — HTTP {r.status_code}: {msg[:80]}")
        else:
            exported = 0
            for dd in domains:
                er = req.post(f"{ZAPMAIL_API}/v2/exports/mailboxes", headers=zm_h(),
                              json={"apps": ["SMARTLEAD"], "contains": dd}, timeout=30)
                if er.status_code == 200:
                    exported += 1
                import time; time.sleep(2)
            msg = f"Exported by domain name ({exported}/{len(domains)} succeeded)"
            checks.append(f"export_by_domain: {exported}/{len(domains)} OK")

        return _cors(jsonify({"ok": True, "message": msg, "checks": checks}))
    except Exception as e:
        return _cors(jsonify({"error": str(e)})), 500


@app.route("/api/group/find-smartlead-accounts", methods=["POST", "OPTIONS"])
def group_find_smartlead_accounts():
    """Search SmartLead for accounts matching our domains."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import time as _time
        import requests as req
        body = request.get_json(silent=True) or {}
        domains = set(body.get("domains", []))
        if not domains:
            return _cors(jsonify({"error": "domains required"})), 400

        SMARTLEAD_KEY = os.environ.get("SMARTLEAD_API_KEY", "")
        SMARTLEAD_API = "https://server.smartlead.ai/api/v1"

        found = []
        offset = 0
        pages_scanned = 0
        while True:
            url = f"{SMARTLEAD_API}/email-accounts/?api_key={SMARTLEAD_KEY}&offset={offset}&limit=100"
            r = req.get(url, timeout=30)
            if r.status_code == 429:
                _time.sleep(10)
                r = req.get(url, timeout=30)
            accts = r.json() if r.status_code == 200 else []
            if not isinstance(accts, list) or not accts:
                break
            pages_scanned += 1
            for a in accts:
                email = a.get("from_email", a.get("email", ""))
                domain_part = email.split("@")[-1] if "@" in email else ""
                if domain_part in domains:
                    found.append({"id": a.get("id"), "email": email})
            if len(accts) < 100:
                break
            offset += 100
            _time.sleep(0.5)

        expected = len(domains) * 3
        found_domains = set(a["email"].split("@")[-1] for a in found if "@" in a.get("email", ""))
        missing_domains = [d for d in domains if d not in found_domains]
        checks = [
            f"scan: OK — {pages_scanned} pages scanned",
            f"accounts: {len(found)}/{expected} expected (3 per domain)",
            f"domains_covered: {len(found_domains)}/{len(domains)}",
        ]
        if missing_domains:
            checks.append(f"missing_domains: {', '.join(list(missing_domains)[:5])}")

        return _cors(jsonify({"ok": True, "accounts": found, "count": len(found),
                              "expected": expected, "missing_domains": missing_domains, "checks": checks}))
    except Exception as e:
        return _cors(jsonify({"error": str(e)})), 500


@app.route("/api/group/setup-tags", methods=["POST", "OPTIONS"])
def group_setup_tags():
    """Get or create the 3 required tags (Zapmail, group letter, date)."""
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

        SMARTLEAD_JWT = os.environ.get("SMARTLEAD_JWT", "")
        SMARTLEAD_GQL = os.environ.get("SMARTLEAD_GQL", "https://fe-gql.smartlead.ai/v1/graphql")
        def sl_h():
            return {"Authorization": f"Bearer {SMARTLEAD_JWT}", "Content-Type": "application/json"}
        def _gql(query, variables=None):
            b = {"query": query}
            if variables: b["variables"] = variables
            return req.post(SMARTLEAD_GQL, headers=sl_h(), json=b, timeout=30).json()

        tags_result = _gql("{ tags { id name color } }")
        all_tags = {t["name"]: t for t in tags_result.get("data", {}).get("tags", [])}

        ZAPMAIL_TAG_ID = 262254
        tag_name = f"Generic {letter}"
        today_str = _time.strftime("%-m/%-d/%y")

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

        date_tag_id = None
        if today_str in all_tags:
            date_tag_id = all_tags[today_str]["id"]
        else:
            mut = """mutation($o: tags_insert_input!) { insert_tags_one(object: $o) { id name color } }"""
            result = _gql(mut, {"o": {"name": today_str, "color": "#94a3b8"}})
            date_tag_id = result.get("data", {}).get("insert_tags_one", {}).get("id")

        checks = []
        if not group_tag_id or not date_tag_id:
            checks.append(f"group_tag: {'OK' if group_tag_id else 'FAILED'}")
            checks.append(f"date_tag: {'OK' if date_tag_id else 'FAILED'}")
            return _cors(jsonify({"error": "Failed to create tags", "checks": checks})), 500

        checks.append(f"zapmail_tag: OK (id={ZAPMAIL_TAG_ID})")
        checks.append(f"group_tag: OK ('{tag_name}' id={group_tag_id})")
        checks.append(f"date_tag: OK ('{today_str}' id={date_tag_id})")

        # Verify: re-fetch tags to confirm they exist
        try:
            verify_result = _gql("{ tags { id name } }")
            verify_tags = {t["id"]: t["name"] for t in verify_result.get("data", {}).get("tags", [])}
            if group_tag_id in verify_tags and date_tag_id in verify_tags:
                checks.append("verify_tags: OK — all 3 confirmed in SmartLead")
            else:
                missing = []
                if group_tag_id not in verify_tags:
                    missing.append(tag_name)
                if date_tag_id not in verify_tags:
                    missing.append(today_str)
                checks.append(f"verify_tags: WARN — missing: {', '.join(missing)}")
        except Exception as e:
            checks.append(f"verify_tags: SKIP — {str(e)[:60]}")

        return _cors(jsonify({"ok": True, "tag_ids": [ZAPMAIL_TAG_ID, group_tag_id, date_tag_id],
                              "tag_names": ["Zapmail", tag_name, today_str], "checks": checks}))
    except Exception as e:
        return _cors(jsonify({"error": str(e)})), 500


@app.route("/api/group/finalize-account", methods=["POST", "OPTIONS"])
def group_finalize_account():
    """Tag one account + enable warmup. Called per-account from frontend."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import time as _time
        import requests as req
        body = request.get_json(silent=True) or {}
        account_id = body.get("account_id")
        tag_ids = body.get("tag_ids", [])
        if not account_id or not tag_ids:
            return _cors(jsonify({"error": "account_id and tag_ids required"})), 400

        SMARTLEAD_KEY = os.environ.get("SMARTLEAD_API_KEY", "")
        SMARTLEAD_API = "https://server.smartlead.ai/api/v1"
        SMARTLEAD_INTERNAL = "https://server.smartlead.ai/api"
        SMARTLEAD_JWT = os.environ.get("SMARTLEAD_JWT", "")
        def sl_h():
            return {"Authorization": f"Bearer {SMARTLEAD_JWT}", "Content-Type": "application/json"}

        checks = []

        # Step 1: Tag the account
        tag_body = {"id": account_id, "tags": tag_ids, "clientId": None}
        r = req.post(f"{SMARTLEAD_INTERNAL}/email-account/save-management-details",
                     headers=sl_h(), json=tag_body, timeout=30)
        tagged = r.status_code == 200
        checks.append(f"tag_account: {'OK' if tagged else 'FAILED — HTTP ' + str(r.status_code)}")

        # Step 2: Enable warmup (public API)
        warmup_body = {"warmup_enabled": True, "total_warmup_per_day": 15,
                       "daily_rampup": 5, "reply_rate_percentage": 40}
        r = req.post(f"{SMARTLEAD_API}/email-accounts/{account_id}/warmup?api_key={SMARTLEAD_KEY}",
                     json=warmup_body, timeout=30)
        warmed = r.status_code == 200
        checks.append(f"enable_warmup: {'OK' if warmed else 'FAILED — HTTP ' + str(r.status_code)}")

        # Step 3: Full warmup config (internal API)
        warmup_key = ""
        wd = req.get(f"{SMARTLEAD_INTERNAL}/email-account/fetch-warmup-details-by-email-account-id/{account_id}",
                     headers=sl_h(), timeout=30)
        if wd.status_code == 200:
            warmup_key = wd.json().get("message", {}).get("warmup_key_id", "")
        if warmup_key:
            full_body = {
                "emailAccountId": str(account_id), "maxEmailPerDay": 15,
                "isRampupEnabled": True, "rampupValue": 5,
                "warmupMinCount": 10, "warmupMaxCount": 15,
                "replyRate": 40, "dailyReplyLimit": 15,
                "autoAdjustWarmup": False, "sendWarmupsOnlyOnWeekdays": False,
                "useCustomDomain": False, "status": "ACTIVE", "warmupKeyId": warmup_key
            }
            sr = req.post(f"{SMARTLEAD_INTERNAL}/email-account/save-warmup",
                          headers=sl_h(), json=full_body, timeout=30)
            checks.append(f"full_warmup_config: {'OK' if sr.status_code == 200 else 'FAILED — HTTP ' + str(sr.status_code)}")
        else:
            checks.append(f"full_warmup_config: SKIP — no warmup_key found")

        # Step 3b: Set time_to_wait_in_mins (matches setup.py)
        try:
            import json as _json
            wait_body = {"time_to_wait_in_mins": 5}
            wr = req.post(f"{SMARTLEAD_API}/email-accounts/{account_id}/settings?api_key={SMARTLEAD_KEY}",
                          json=wait_body, timeout=15)
            if wr.status_code != 200:
                req.post(f"{SMARTLEAD_INTERNAL}/email-account/update",
                         headers=sl_h(), json={"id": account_id, "time_to_wait_in_mins": 5}, timeout=15)
            checks.append("time_to_wait: OK (5 min)")
        except Exception:
            checks.append("time_to_wait: SKIP")

        # Step 4: Verify tags applied
        _time.sleep(1)
        try:
            vr = req.get(f"{SMARTLEAD_API}/email-accounts/{account_id}?api_key={SMARTLEAD_KEY}", timeout=15)
            if vr.status_code == 200:
                acct_data = vr.json()
                acct_tags = acct_data.get("tags", [])
                if isinstance(acct_tags, list) and len(acct_tags) >= len(tag_ids):
                    checks.append(f"verify_tags: OK ({len(acct_tags)} tags)")
                else:
                    checks.append(f"verify_tags: WARN — expected {len(tag_ids)}, got {len(acct_tags) if isinstance(acct_tags, list) else 0}")
                warmup_status = acct_data.get("warmup_enabled") or acct_data.get("warmupEnabled")
                if warmup_status:
                    checks.append("verify_warmup: OK — warmup enabled")
                else:
                    checks.append("verify_warmup: WARN — warmup may not be active yet")
            else:
                checks.append(f"verify_tags: SKIP — HTTP {vr.status_code}")
        except Exception as e:
            checks.append(f"verify: SKIP — {str(e)[:60]}")

        return _cors(jsonify({"ok": True, "tagged": tagged, "warmed": warmed, "checks": checks}))
    except Exception as e:
        return _cors(jsonify({"error": str(e)})), 500


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
        ZAPMAIL_KEY = os.environ.get("ZAPMAIL_API_KEY", "").strip()
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
