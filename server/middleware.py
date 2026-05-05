"""Response building helpers for consistent API contract."""

import json
from datetime import datetime, timezone
from server.errors import APIError, error_dict


def success_response(data, meta_overrides=None):
    """Build a standard success response."""
    meta = {
        "cached": False,
        "stale_seconds": 0,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if meta_overrides:
        meta.update(meta_overrides)
    return {"data": data, "errors": [], "meta": meta}


def partial_response(data, errors, meta_overrides=None):
    """Build a response where some sections succeeded and others failed."""
    meta = {
        "cached": False,
        "stale_seconds": 0,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if meta_overrides:
        meta.update(meta_overrides)
    return {"data": data, "errors": errors, "meta": meta}


def error_response(code, message, status=500):
    """Build a standard error response."""
    return {
        "data": None,
        "errors": [error_dict(code, message)],
        "meta": {"cached": False, "timestamp": datetime.now(timezone.utc).isoformat()},
    }, status


def handle_route(handler_fn, handler):
    """Wrap a route handler with error catching and JSON response."""
    try:
        result = handler_fn()
        body = json.dumps(result).encode()
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
    except APIError as e:
        resp, status = error_response(e.code, e.message, e.status)
        body = json.dumps(resp).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
    except Exception as e:
        resp, status = error_response("INTERNAL_ERROR", str(e))
        body = json.dumps(resp).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
