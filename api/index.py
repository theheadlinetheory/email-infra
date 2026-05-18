"""Vercel Python serverless function — Flask WSGI wrapper for the THT dashboard API.

Delegates to the existing route table in server/routes/*.py. All business logic
stays in dashboard.py; this file just translates between Flask and the route dispatcher.
"""

import os
import sys
import json
import time

# Ensure project root is on sys.path for local imports (dashboard, db, setup, etc.)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, request, jsonify, make_response
from server.app import create_route_table, match_route

app = Flask(__name__)

# Lazy-init route table (once per cold start)
_routes = {"get": None, "post": None}


def _init_routes():
    if _routes["get"] is None:
        _routes["get"], _routes["post"] = create_route_table()


def _check_auth():
    """Check Firebase JWT or password auth. Returns True if authorized."""
    # Firebase JWT
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        from server.auth import verify_firebase_token
        claims = verify_firebase_token(auth_header[7:])
        if claims:
            return True

    # Password auth (query param or cookie)
    password = os.environ.get("DASHBOARD_PASSWORD", "")
    if not password:
        return True
    if request.args.get("pw") == password:
        return True
    cookie = request.cookies.get("dashboard_pw", "")
    if cookie == password:
        return True

    return False


@app.route("/api/cron/sync", methods=["GET"])
def cron_sync():
    """Vercel Cron endpoint — runs the SmartLead → Supabase sync."""
    # Verify cron secret (Vercel sends this header)
    cron_secret = os.environ.get("CRON_SECRET", "")
    if cron_secret and request.headers.get("Authorization") != f"Bearer {cron_secret}":
        if not _check_auth():
            return jsonify({"error": "Unauthorized"}), 401

    from dashboard import _sync_smartlead_data
    _sync_smartlead_data()
    return jsonify({"ok": True, "synced_at": time.strftime("%Y-%m-%dT%H:%M:%S")})


@app.route("/api/healthz", methods=["GET"])
def healthz():
    return "ok", 200


@app.route("/api/<path:path>", methods=["GET", "POST", "OPTIONS"])
def catch_all(path):
    if request.method == "OPTIONS":
        resp = make_response("", 200)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        return resp

    full_path = f"/api/{path}"

    # Auth check (except auth-check endpoint which handles its own)
    if full_path != "/api/auth-check" and not _check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    _init_routes()

    if request.method == "GET":
        params = {k: request.args.getlist(k) for k in request.args}
        handler_fn, kwargs = match_route(full_path, params, _routes["get"])
        if handler_fn:
            try:
                result = handler_fn(**kwargs)
                resp = jsonify(result)
                resp.headers["Access-Control-Allow-Origin"] = "*"
                return resp
            except Exception as e:
                import traceback
                return jsonify({"error": str(e), "traceback": traceback.format_exc()}), 500
        return jsonify({"error": "Not found"}), 404

    elif request.method == "POST":
        body = request.get_json(silent=True) or {}
        handler_fn, kwargs = match_route(full_path, {}, _routes["post"])
        if handler_fn:
            try:
                kwargs["body"] = body
                kwargs["handler"] = None  # Not needed — routes don't use it
                result = handler_fn(**kwargs)
                if result is not None:
                    status = 400 if isinstance(result, dict) and "error" in result else 200
                    resp = jsonify(result)
                    resp.headers["Access-Control-Allow-Origin"] = "*"
                    return resp, status
                return "", 204
            except Exception as e:
                import traceback
                return jsonify({"error": str(e), "traceback": traceback.format_exc()}), 500
        return jsonify({"error": "Not found"}), 404
