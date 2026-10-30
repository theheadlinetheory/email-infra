"""Accept a signed-in THT CRM user as a caller.

The CRM is a public static site, so it cannot hold the dashboard password.
Instead the browser sends the user's own CRM session token, and we ask the
CRM's auth server who it belongs to — the same check as fulfillment-dashboard's
_shared/verify-crm-user.ts. No secret to leak, and every purchase is tied to a
named person.

Fails closed: an auth server we cannot reach is a refusal, not a pass.
"""
from __future__ import annotations

import requests

CRM_AUTH_URL = "https://api.theheadlinetheory.com/auth/v1/user"
# The CRM's PUBLIC anon key (tht-crm js/config.js). It authorises nothing on its
# own; GoTrue requires it as `apikey` alongside the user token.
CRM_ANON_KEY = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
                "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InZqd2thZm5sZ3FpZGZ0eGJlcWpwIiwicm9sZSI6"
                "ImFub24iLCJpYXQiOjE3NzU4NTM3MzcsImV4cCI6MjA5MTQyOTczN30."
                "27x_IdhtcJaAr0wdx6RhoWr1d6_o3zfzEPk9uneq1h8")
ALLOWED_DOMAIN = "@theheadlinetheory.com"


def verify_crm_user(auth_header: str | None, get=requests.get) -> dict:
    """{ok, email} or {ok: False, status, error}."""
    h = (auth_header or "").strip()
    if not h.lower().startswith("bearer "):
        return {"ok": False, "status": 401, "error": "Missing Authorization bearer token"}
    token = h[7:].strip()
    try:
        r = get(CRM_AUTH_URL, headers={"apikey": CRM_ANON_KEY,
                                       "Authorization": f"Bearer {token}"}, timeout=10)
    except Exception:                                # noqa: BLE001
        return {"ok": False, "status": 503, "error": "Could not verify your session — try again"}
    if r.status_code != 200:
        return {"ok": False, "status": 401, "error": "Session invalid or expired — sign in again"}
    try:
        email = str((r.json() or {}).get("email") or "").strip().lower()
    except ValueError:
        email = ""
    if not email.endswith(ALLOWED_DOMAIN):
        return {"ok": False, "status": 403, "error": "Not authorised"}
    return {"ok": True, "email": email}
