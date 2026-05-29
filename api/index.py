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


@app.route("/api/subscriptions")
def subscriptions():
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    import requests as req
    from datetime import datetime, timezone
    zm_key = os.environ.get("ZAPMAIL_API_KEY", "")
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
    import requests as req
    from datetime import datetime, timezone
    from concurrent.futures import ThreadPoolExecutor, as_completed
    zm_key = os.environ.get("ZAPMAIL_API_KEY", "")
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
    active = []
    for s in subs:
        if s.get("subscriptionStatus") != "ACTIVE":
            continue
        period_end = s.get("periodEnd", "")
        try:
            renews = datetime.fromisoformat(period_end.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        active.append({"id": s.get("id"), "renews": renews.strftime("%Y-%m-%d"), "days_until": (renews - now).days})
    domain_map = {}
    def fetch_mailboxes(sub):
        try:
            r2 = req.get(f"https://api.zapmail.ai/api/v2/subscriptions/{sub['id']}/mailboxes", headers=headers, timeout=15)
            mboxes = r2.json()
            if isinstance(mboxes, dict):
                mboxes = mboxes.get("data", [])
            pairs = []
            for mb in (mboxes if isinstance(mboxes, list) else []):
                email = mb.get("email", "")
                domain = email.split("@")[1] if "@" in email else ""
                if domain:
                    pairs.append((domain, sub["renews"], sub["days_until"]))
            return pairs
        except Exception:
            return []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(fetch_mailboxes, s): s for s in active}
        for fut in as_completed(futures):
            for domain, renews, days_until in fut.result():
                if domain not in domain_map or days_until < domain_map[domain]["days_until"]:
                    domain_map[domain] = {"renews": renews, "days_until": days_until}
    return _cors(jsonify({"domain_renewals": domain_map}))


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
