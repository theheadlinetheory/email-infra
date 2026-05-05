"""Thin HTTP server with route dispatching.

Replaces the monolithic DashboardHandler with a dispatcher that routes
to module-level handlers. Each route module registers GET/POST handlers
via a simple dict-based routing table.
"""

import json
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path
from server.errors import APIError
from server.middleware import error_response
from server.auth import check_auth

SCRIPT_DIR = Path(__file__).parent.parent


def create_route_table():
    """Build the route table from all route modules.

    Returns (get_routes, post_routes) where each is a list of
    (pattern, handler_fn) tuples. Pattern is either an exact string
    or a callable that takes a path and returns (match: bool, kwargs: dict).
    """
    from server.routes import overview, client, zapmail, domains, pipelines, operations, acquisition, inventory

    get_routes = []
    post_routes = []
    for module in [overview, client, zapmail, domains, pipelines, operations, acquisition, inventory]:
        get_routes.extend(getattr(module, "GET_ROUTES", []))
        post_routes.extend(getattr(module, "POST_ROUTES", []))
    return get_routes, post_routes


def match_route(path, params, routes):
    """Find a matching route handler. Returns (handler_fn, kwargs) or (None, None)."""
    for pattern, handler in routes:
        if callable(pattern):
            matched, kwargs = pattern(path)
            if matched:
                kwargs["_params"] = params
                return handler, kwargs
        elif pattern == path:
            return handler, {"_params": params}
    return None, None


class DashboardHandler(BaseHTTPRequestHandler):
    get_routes = None
    post_routes = None

    @classmethod
    def init_routes(cls):
        if cls.get_routes is None:
            cls.get_routes, cls.post_routes = create_route_table()

    def _check_auth(self):
        """Check auth via Firebase JWT or password fallback."""
        return check_auth(self)

    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)
        pw = params.get("pw", [None])[0]

        # Static assets — no auth required (login page needs these to render)
        if path == "/" or path == "/index.html":
            self._serve_file("index.html", "text/html", set_cookie=pw)
            return
        if path == "/dashboard.html":
            self._serve_file("dashboard.html", "text/html", set_cookie=pw)
            return
        if path.startswith("/css/") or path.startswith("/js/"):
            ext_map = {".css": "text/css", ".js": "application/javascript"}
            ext = os.path.splitext(path)[1]
            self._serve_file(path.lstrip("/"), ext_map.get(ext, "application/octet-stream"))
            return
        if path == "/headshots/sean_reynolds.png":
            self._serve_file("headshots/sean_reynolds.png", "image/png")
            return

        # Auth-check probes its own auth
        if path == "/api/auth-check":
            if self._check_auth():
                self._json_response({"ok": True})
            return

        # All other API routes require auth
        if not self._check_auth():
            return

        if path.startswith("/api/"):
            self.__class__.init_routes()
            handler_fn, kwargs = match_route(path, params, self.get_routes)
            if handler_fn:
                try:
                    result = handler_fn(**kwargs)
                    self._json_response(result)
                except APIError as e:
                    resp, status = error_response(e.code, e.message, e.status)
                    self._json_response(resp, status)
                except Exception as e:
                    import traceback
                    tb = traceback.format_exc()
                    print(f"API ERROR on {path}: {tb}")
                    self._json_response({"error": str(e), "traceback": tb}, 500)
            else:
                self._error(404, "Not found")
        else:
            self._error(404, "Not found")

    def do_POST(self):
        if not self._check_auth():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length)) if content_length else {}

        self.__class__.init_routes()
        handler_fn, kwargs = match_route(path, {}, self.post_routes)
        if handler_fn:
            try:
                kwargs["body"] = body
                kwargs["handler"] = self
                result = handler_fn(**kwargs)
                if result is not None:
                    status = 400 if isinstance(result, dict) and "error" in result else 200
                    self._json_response(result, status)
            except APIError as e:
                resp, status = error_response(e.code, e.message, e.status)
                self._json_response(resp, status)
            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                print(f"API ERROR on {path}: {tb}")
                self._json_response({"error": str(e), "traceback": tb}, 500)
        else:
            self._error(404, "Not found")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _json_response(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _serve_file(self, filename, content_type, set_cookie=None):
        filepath = SCRIPT_DIR / filename
        if filepath.exists():
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            password = os.environ.get("DASHBOARD_PASSWORD", "")
            if set_cookie and password:
                self.send_header("Set-Cookie", f"dashboard_pw={set_cookie}; Path=/; Max-Age=2592000; SameSite=Strict")
            self.end_headers()
            self.wfile.write(filepath.read_bytes())
        else:
            self._error(404, f"{filename} not found")

    def _error(self, status, message):
        self._json_response({"error": message}, status)

    def send_sse_headers(self):
        """Set up SSE streaming response headers."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def stream_sse(self, generator):
        """Stream SSE events from a generator."""
        self.send_sse_headers()
        try:
            for chunk in generator:
                self.wfile.write(chunk.encode())
                self.wfile.flush()
        except Exception as e:
            error_data = json.dumps({"step": 0, "status": "error", "message": str(e)})
            self.wfile.write(f"data: {error_data}\n\n".encode())
            self.wfile.flush()

    def log_message(self, format, *args):
        pass
