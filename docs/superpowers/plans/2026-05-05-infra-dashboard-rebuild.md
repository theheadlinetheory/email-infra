# Infra Dashboard Rebuild — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the infra dashboard with a hardened Python backend (retry, caching, structured errors) and a CRM-quality frontend (ES modules, reactive state, Firebase Auth, light/dark theme).

**Architecture:** Python HTTP server restructured from a 3800-line monolith into route modules + service clients with retry/caching. Frontend rebuilt from scratch with ES modules, reactive state store, three-state rendering (loading/error/data), and shared CRM design tokens.

**Tech Stack:** Python 3 (http.server), Firebase Admin SDK, ES modules (no build step), CSS custom properties, Supabase (persistence), Render (deployment).

**North Star:** `~/Desktop/code-quality-review/` — SOLID, DRY/KISS/YAGNI, Composition/Coupling.

---

## Phase 1: Backend Foundation (errors, cache, base client)

### Task 1: Structured Errors (`server/errors.py`)

**Files:**
- Create: `server/errors.py`
- Create: `server/__init__.py`

- [ ] **Step 1: Create server package and errors module**

```python
# server/__init__.py
# empty — marks server/ as a package

# server/errors.py
"""Structured error types and HTTP mapping."""

class APIError(Exception):
    """Base error for all external API failures."""
    def __init__(self, code, message, status=502):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)

class SmartLeadError(APIError):
    def __init__(self, message, status=502):
        super().__init__("SMARTLEAD_ERROR", message, status)

class ZapmailError(APIError):
    def __init__(self, message, status=502):
        super().__init__("ZAPMAIL_ERROR", message, status)

class RegistrarError(APIError):
    def __init__(self, message, status=502):
        super().__init__("REGISTRAR_ERROR", message, status)

class SheetsError(APIError):
    def __init__(self, message, status=502):
        super().__init__("SHEETS_ERROR", message, status)

class ValidationError(APIError):
    def __init__(self, message):
        super().__init__("VALIDATION_ERROR", message, status=400)

def error_dict(code, message):
    """Standard error dict for JSON responses."""
    return {"code": code, "message": message}
```

- [ ] **Step 2: Commit**

```bash
git add server/
git commit -m "feat: add structured error types for backend reliability"
```

### Task 2: TTL Cache (`server/cache.py`)

**Files:**
- Create: `server/cache.py`

- [ ] **Step 1: Implement TTL cache**

```python
# server/cache.py
"""In-memory TTL cache for API responses."""

import time
import threading

class TTLCache:
    def __init__(self):
        self._store = {}
        self._lock = threading.Lock()

    def get(self, key):
        """Return (value, stale_seconds) or (None, None) if expired/missing."""
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None, None
            value, expires_at, stored_at = entry
            now = time.time()
            if now > expires_at:
                del self._store[key]
                return None, None
            stale_seconds = int(now - stored_at)
            return value, stale_seconds

    def set(self, key, value, ttl_seconds):
        """Store value with TTL."""
        now = time.time()
        with self._lock:
            self._store[key] = (value, now + ttl_seconds, now)

    def bust(self, *prefixes):
        """Remove all keys starting with any of the given prefixes."""
        with self._lock:
            keys_to_delete = [
                k for k in self._store
                if any(k.startswith(p) for p in prefixes)
            ]
            for k in keys_to_delete:
                del self._store[k]

    def clear(self):
        with self._lock:
            self._store.clear()

# Singleton instance
cache = TTLCache()
```

- [ ] **Step 2: Commit**

```bash
git add server/cache.py
git commit -m "feat: add TTL cache with bust-by-prefix for API responses"
```

### Task 3: Base API Client with Retry (`server/services/base.py`)

**Files:**
- Create: `server/services/__init__.py`
- Create: `server/services/base.py`

- [ ] **Step 1: Implement base client with retry + caching**

```python
# server/services/__init__.py
# empty

# server/services/base.py
"""Base API client with retry, backoff, and caching."""

import time
import requests
from server.cache import cache
from server.errors import APIError

class BaseAPIClient:
    def __init__(self, base_url, name, max_retries=3, backoff_base=1.0):
        self.base_url = base_url.rstrip("/")
        self.name = name
        self.max_retries = max_retries
        self.backoff_base = backoff_base

    def _request(self, method, path, cache_key=None, cache_ttl=0, **kwargs):
        """Make an HTTP request with retry and optional caching.
        
        Returns the parsed JSON response. Raises APIError on failure.
        """
        if cache_key and cache_ttl > 0:
            cached, stale = cache.get(cache_key)
            if cached is not None:
                return cached, {"cached": True, "stale_seconds": stale}

        url = f"{self.base_url}/{path.lstrip('/')}" if path else self.base_url
        last_error = None

        for attempt in range(self.max_retries):
            try:
                resp = requests.request(method, url, timeout=30, **kwargs)
                if resp.status_code >= 500:
                    last_error = f"{self.name} returned {resp.status_code}: {resp.text[:200]}"
                    if attempt < self.max_retries - 1:
                        time.sleep(self.backoff_base * (2 ** attempt))
                        continue
                    raise self._make_error(last_error)
                if resp.status_code >= 400:
                    raise self._make_error(
                        f"{self.name} returned {resp.status_code}: {resp.text[:200]}",
                        status=resp.status_code
                    )
                data = resp.json() if resp.text else {}
                if cache_key and cache_ttl > 0:
                    cache.set(cache_key, data, cache_ttl)
                return data, {"cached": False, "stale_seconds": 0}
            except requests.RequestException as e:
                last_error = f"{self.name} request failed: {e}"
                if attempt < self.max_retries - 1:
                    time.sleep(self.backoff_base * (2 ** attempt))
                    continue
                raise self._make_error(last_error)

        raise self._make_error(last_error or f"{self.name} failed after {self.max_retries} retries")

    def get(self, path="", cache_key=None, cache_ttl=0, **kwargs):
        return self._request("GET", path, cache_key=cache_key, cache_ttl=cache_ttl, **kwargs)

    def post(self, path="", cache_key=None, cache_ttl=0, **kwargs):
        return self._request("POST", path, cache_key=cache_key, cache_ttl=cache_ttl, **kwargs)

    def put(self, path="", **kwargs):
        return self._request("PUT", path, **kwargs)

    def delete(self, path="", **kwargs):
        return self._request("DELETE", path, **kwargs)

    def _make_error(self, message, status=502):
        return APIError(f"{self.name.upper()}_ERROR", message, status)
```

- [ ] **Step 2: Commit**

```bash
git add server/services/
git commit -m "feat: add base API client with retry, backoff, and caching"
```

### Task 4: Response Helpers (`server/middleware.py`)

**Files:**
- Create: `server/middleware.py`

- [ ] **Step 1: Implement response builders**

```python
# server/middleware.py
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
```

- [ ] **Step 2: Commit**

```bash
git add server/middleware.py
git commit -m "feat: add response helpers for consistent API contract"
```

## Phase 2: Service Clients (SmartLead, Zapmail, Registrars, Sheets)

### Task 5: SmartLead Service (`server/services/smartlead.py`)

Extract all SmartLead API calls from dashboard.py into a service client with retry + caching. Covers: public API (v1), internal API (JWT), and GQL endpoints.

### Task 6: Zapmail Service (`server/services/zapmail_api.py`)

Extract all Zapmail API calls from dashboard.py and zapmail_ops.py into a service client.

### Task 7: Registrar Service (`server/services/registrars.py`)

Extract Porkbun + Spaceship calls from dashboard.py into a unified registrar service.

### Task 8: Sheets Service (`server/services/sheets_service.py`)

Wrap sheets.py with retry and caching for domain inventory operations.

## Phase 3: Route Modules

### Task 9: Route Dispatcher (`server/app.py`)

Thin HTTP server that routes to module handlers. Replaces the giant if/elif chain in dashboard.py's do_GET/do_POST.

### Task 10: Overview Routes (`server/routes/overview.py`)

`/api/overview`, `/api/clients`, `/api/clients/list`, `/api/untagged-count`, `/api/snapshot` — with partial failure support (SmartLead down → return other sections).

### Task 11: Client Routes (`server/routes/client.py`)

`/api/client/{id}/accounts`, `/api/client/{id}/trends`, archive, pause-monitor, set-target-volume.

### Task 12: Zapmail Routes (`server/routes/zapmail.py`)

`/api/zapmail`, `/api/zapmail/sync`, `/api/zapmail/cancel`, `/api/wallet`, `/api/subscriptions`, `/api/placement-tests`.

### Task 13: Domain Routes (`server/routes/domains.py`)

`/api/domains`, `/api/domains/auto-renew`, `/api/domains/bulk-auto-renew`.

### Task 14: Pipeline Routes (`server/routes/pipelines.py`)

All `/api/pipeline/*` and `/api/setup-pipeline/*` routes.

### Task 15: Operation Routes — SSE (`server/routes/operations.py`)

`/api/pipeline/assign-client`, `/api/client/delete-infra`, `/api/client/transition` — SSE streaming endpoints.

### Task 16: Acquisition Routes (`server/routes/acquisition.py`)

`/api/acquisition`, `/api/acquisition-campaigns`, `/api/acquisition/assign-campaign`, `/api/rotation/*`, `/api/generic-groups`.

### Task 17: Inventory Routes (`server/routes/inventory.py`)

`/api/domain-inventory`, `/api/unassigned`, `/api/inbox/{email}/campaigns`, `/api/inbox/remove-*`.

## Phase 4: Frontend Core

### Task 18: CSS Design System (`css/dashboard.css`)

CSS custom properties for light/dark theme, DM Sans, CRM spacing/color tokens.

### Task 19: Frontend State Store (`js/core/state.js`)

Reactive subscribe/notify store — CRM pattern adapted for infra data shapes.

### Task 20: API Client (`js/core/api.js`)

Fetch wrapper with auth headers, error normalization, meta/cached awareness.

### Task 21: Auth Module (`js/core/auth.js`)

Firebase Auth init, login/logout UI, token management for API calls.

### Task 22: Router (`js/core/router.js`)

Hash-based tab routing. Maps `#overview`, `#pipelines`, `#zapmail`, etc. to view modules.

### Task 23: Event Bus (`js/core/events.js`)

Simple pub/sub for cross-module communication.

## Phase 5: Frontend Components

### Task 24: Toast, Modal, Stat Card

Reusable UI components matching CRM patterns.

### Task 25: Data Table

Sortable table with loading/error/empty states.

### Task 26: SSE Progress

Step-by-step progress display for assign/delete/transition operations.

## Phase 6: Frontend Views

### Task 27: Overview View (`js/views/overview.js`)

Main dashboard — client cards, health stats, alerts. Three-state rendering.

### Task 28: Client Detail View (`js/views/client-detail.js`)

Account table, trends chart, actions panel.

### Task 29: Pipelines View (`js/views/pipelines.js`)

Pipeline cards with step progress pills.

### Task 30: Zapmail View (`js/views/zapmail.js`)

ZapMail domains, mailboxes, renewals, sync check.

### Task 31: Domains View (`js/views/domains.js`)

Registrar domains, expiry tracking, auto-renew management.

### Task 32: Acquisition View (`js/views/acquisition.js`)

Acquisition groups, rotation status, campaign assignment.

### Task 33: App Entry + HTML Shell

`js/app.js` entry point, new `index.html` shell, Firebase config.

## Phase 7: Integration + Deploy

### Task 34: Wire Backend to New Frontend

Update `server/app.py` to serve the new frontend. Auth middleware on all routes.

### Task 35: Firebase Auth Backend Verification (`server/auth.py`)

Verify Firebase JWTs on the Python side. Install `firebase-admin` in requirements.txt.

### Task 36: Deploy to Render

Push to GitHub, verify auto-deploy, test all routes with new API contract.
