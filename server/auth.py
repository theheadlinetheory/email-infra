"""Firebase JWT verification for backend routes.

Verifies tokens from the shared THT CRM Firebase project (tht-crm).
Uses lightweight PyJWT + Google public keys instead of heavy firebase-admin SDK.
Falls back to password-based auth when Firebase is not configured.
"""

import os
import json
import time

import jwt
import requests
from cachetools import TTLCache

FIREBASE_PROJECT_ID = "tht-crm"
GOOGLE_CERTS_URL = "https://www.googleapis.com/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com"

_cert_cache = TTLCache(maxsize=1, ttl=3600)


def _get_google_certs():
    """Fetch Google's public certs for Firebase JWT verification (cached 1hr)."""
    if "certs" in _cert_cache:
        return _cert_cache["certs"]
    resp = requests.get(GOOGLE_CERTS_URL, timeout=10)
    resp.raise_for_status()
    certs = resp.json()
    _cert_cache["certs"] = certs
    return certs


def verify_firebase_token(token):
    """Verify a Firebase JWT and return the decoded claims, or None on failure."""
    try:
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        if not kid:
            return None

        certs = _get_google_certs()
        cert_pem = certs.get(kid)
        if not cert_pem:
            return None

        decoded = jwt.decode(
            token,
            cert_pem,
            algorithms=["RS256"],
            audience=FIREBASE_PROJECT_ID,
            issuer=f"https://securetoken.google.com/{FIREBASE_PROJECT_ID}",
        )

        if decoded.get("exp", 0) < time.time():
            return None

        return decoded
    except Exception:
        return None


def check_auth(handler):
    """Check authentication on an HTTP request.

    Tries Firebase JWT first (Authorization: Bearer <token>), then falls back
    to password-based auth (query param or cookie). Returns True if authorized.
    """
    auth_header = handler.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        claims = verify_firebase_token(token)
        if claims:
            handler._auth_user = claims
            return True

    password = os.environ.get("DASHBOARD_PASSWORD", "")
    if not password:
        return True

    from urllib.parse import urlparse, parse_qs
    parsed = urlparse(handler.path)
    params = parse_qs(parsed.query)

    if params.get("pw", [None])[0] == password:
        return True
    cookie = handler.headers.get("Cookie", "")
    if f"dashboard_pw={password}" in cookie:
        return True

    handler.send_response(401)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(b'{"error":"Unauthorized"}')
    return False
