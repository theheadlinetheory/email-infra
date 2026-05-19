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


def _resolve_group_account_ids(group_name, campaign_id=None):
    """Look up SmartLead account IDs for a group by matching emails.

    If campaign_id is provided, fetches accounts from that campaign (fast).
    Otherwise fetches all accounts in batches (slower).
    """
    import requests as req
    sl_key = os.environ.get("SMARTLEAD_API_KEY", "")
    if not sl_key:
        return None, "API key not configured"
    data, _ = _get_cache("overview_v2")
    if not data:
        return None, "No cached data"
    emails = set()
    for section in ["acquisition_groups", "generic_groups"]:
        for g in (data.get(section) or []):
            if g.get("name") == group_name:
                for a in (g.get("account_details") or []):
                    if a.get("email"):
                        emails.add(a["email"].lower())
    if not emails:
        return None, "No accounts found for group " + group_name
    sl = "https://server.smartlead.ai/api/v1"
    account_ids = []
    if campaign_id:
        r = req.get(f"{sl}/campaigns/{campaign_id}/email-accounts",
                    params={"api_key": sl_key}, timeout=30)
        if r and r.status_code == 200:
            for acct in (r.json() if r.text.strip() else []):
                email = (acct.get("from_email") or acct.get("email") or "").lower()
                if email in emails:
                    account_ids.append(acct["id"])
    if not account_ids:
        offset = 0
        while offset < 2000:
            r = req.get(f"{sl}/email-accounts/", params={"api_key": sl_key, "offset": offset, "limit": 100}, timeout=30)
            if not r or r.status_code != 200:
                break
            batch = r.json() if r.text.strip() else []
            if not batch:
                break
            for acct in batch:
                if acct.get("from_email", "").lower() in emails:
                    account_ids.append(acct["id"])
            if len(account_ids) >= len(emails) or len(batch) < 100:
                break
            offset += 100
    if not account_ids:
        return None, "Could not resolve IDs for " + group_name
    return account_ids, None


@app.route("/api/assign-group", methods=["POST", "OPTIONS"])
def assign_group():
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    import requests as req
    body = request.get_json(silent=True) or {}
    group_name = body.get("group_name", "")
    campaign_id = body.get("campaign_id")
    if not group_name or not campaign_id:
        return _cors(jsonify({"error": "group_name and campaign_id required"})), 400
    sl_key = os.environ.get("SMARTLEAD_API_KEY", "")
    account_ids, err = _resolve_group_account_ids(group_name, campaign_id=None)
    if err:
        return _cors(jsonify({"error": err})), 404 if "No accounts" in err or "Could not" in err else 500
    sl = "https://server.smartlead.ai/api/v1"
    r = req.post(f"{sl}/campaigns/{campaign_id}/email-accounts?api_key={sl_key}",
                 json={"email_account_ids": account_ids}, timeout=30)
    if r.status_code == 200:
        return _cors(jsonify({"ok": True, "assigned": len(account_ids),
                              "message": f"Assigned {len(account_ids)} accounts. REMINDER: Reallocate inboxes in SmartLead."}))
    return _cors(jsonify({"error": f"SmartLead returned {r.status_code}", "detail": r.text[:300]})), 502


@app.route("/api/unassign-group", methods=["POST", "OPTIONS"])
def unassign_group():
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    import requests as req
    body = request.get_json(silent=True) or {}
    group_name = body.get("group_name", "")
    campaign_id = body.get("campaign_id")
    if not group_name or not campaign_id:
        return _cors(jsonify({"error": "group_name and campaign_id required"})), 400
    sl_key = os.environ.get("SMARTLEAD_API_KEY", "")
    account_ids, err = _resolve_group_account_ids(group_name, campaign_id=campaign_id)
    if err:
        return _cors(jsonify({"error": err})), 404 if "No accounts" in err or "Could not" in err else 500
    sl = "https://server.smartlead.ai/api/v1"
    r = req.delete(f"{sl}/campaigns/{campaign_id}/email-accounts",
                   params={"api_key": sl_key},
                   json={"email_account_ids": account_ids}, timeout=30)
    if r.status_code == 200:
        return _cors(jsonify({"ok": True, "removed": len(account_ids),
                              "message": f"Removed {len(account_ids)} accounts. REMINDER: Reallocate inboxes in SmartLead."}))
    return _cors(jsonify({"error": f"SmartLead returned {r.status_code}", "detail": r.text[:300]})), 502


@app.route("/api/<path:path>", methods=["GET", "OPTIONS"])
def catch_all(path):
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    return _cors(jsonify({"error": "Not found"})), 404
