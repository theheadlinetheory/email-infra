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
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
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


@app.route("/api/<path:path>", methods=["GET", "OPTIONS"])
def catch_all(path):
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    return _cors(jsonify({"error": "Not found"})), 404
