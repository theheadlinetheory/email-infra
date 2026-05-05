"""Firebase JWT verification for backend routes.

Verifies tokens from the shared THT CRM Firebase project (tht-crm).
Falls back to password-based auth when Firebase is not configured.
"""

import os
import json

_firebase_app = None
_firebase_initialized = False


def _init_firebase():
    """Initialize Firebase Admin SDK lazily."""
    global _firebase_app, _firebase_initialized
    if _firebase_initialized:
        return _firebase_app

    _firebase_initialized = True
    try:
        import firebase_admin
        from firebase_admin import credentials

        cred_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
        if cred_json:
            cred_data = json.loads(cred_json)
            cred = credentials.Certificate(cred_data)
            _firebase_app = firebase_admin.initialize_app(cred)
        elif os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
            _firebase_app = firebase_admin.initialize_app()
        else:
            print("[auth] No Firebase credentials found — using password auth fallback")
            return None
    except Exception as e:
        print(f"[auth] Firebase init failed: {e} — using password auth fallback")
        return None
    return _firebase_app


def verify_firebase_token(token):
    """Verify a Firebase JWT and return the decoded claims, or None on failure."""
    app = _init_firebase()
    if not app:
        return None
    try:
        from firebase_admin import auth
        decoded = auth.verify_id_token(token, app=app)
        return decoded
    except Exception:
        return None


def check_auth(handler):
    """Check authentication on an HTTP request.

    Tries Firebase JWT first (Authorization: Bearer <token>), then falls back
    to password-based auth (query param or cookie). Returns True if authorized.
    """
    # Try Firebase JWT
    auth_header = handler.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        claims = verify_firebase_token(token)
        if claims:
            handler._auth_user = claims
            return True

    # Fall back to password auth
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

    # Unauthorized
    handler.send_response(401)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(b'{"error":"Unauthorized"}')
    return False
