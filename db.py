"""Supabase persistence layer for pipeline state, pending deletions, and client configs.

Replaces JSON file storage with durable cloud persistence.
Requires SUPABASE_URL and SUPABASE_KEY environment variables.
Uses httpx directly with HTTP/1.1 to avoid StreamReset errors on Render.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime

import httpx

log = logging.getLogger("db")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip().replace("\n", "").replace("\r", "").replace(" ", "")

# Log key diagnostics on startup (first/last chars only, not the full key)
if SUPABASE_KEY:
    import base64 as _b64
    try:
        _payload = SUPABASE_KEY.split(".")[1]
        _payload += "=" * (4 - len(_payload) % 4)
        _decoded = json.loads(_b64.b64decode(_payload))
        log.info("Supabase key role: %s, key length: %d", _decoded.get("role"), len(SUPABASE_KEY))
    except Exception as _e:
        log.warning("Could not decode Supabase key: %s (length=%d, first10=%s, last10=%s)",
                    _e, len(SUPABASE_KEY), SUPABASE_KEY[:10], SUPABASE_KEY[-10:])

_http: httpx.Client | None = None
_http_lock = threading.Lock()


def _get_http() -> httpx.Client:
    """Lazy-init an HTTP/1.1 client pointed at the Supabase REST API."""
    global _http
    if _http is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set")
        log.info("Initializing Supabase HTTP client: url=%s, key_len=%d, key_first10=%s, key_last10=%s",
                 SUPABASE_URL, len(SUPABASE_KEY), SUPABASE_KEY[:10], SUPABASE_KEY[-10:])
        _http = httpx.Client(
            base_url=f"{SUPABASE_URL}/rest/v1",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            http2=False,
            timeout=30,
        )
    return _http


def _request(method: str, path: str, *, params: dict | None = None,
             json_body=None, headers: dict | None = None):
    """Make a request to the Supabase REST API (thread-safe)."""
    h = headers or {}
    with _http_lock:
        resp = _get_http().request(method, path, params=params, json=json_body, headers=h)
    resp.raise_for_status()
    if resp.status_code == 204 or not resp.content:
        return []
    return resp.json()


# ---------------------------------------------------------------------------
# Pipelines
# ---------------------------------------------------------------------------

def save_pipeline(pipeline: dict) -> None:
    """Upsert a pipeline record."""
    row = {
        "id": pipeline["id"],
        "data": json.dumps(pipeline, default=str),
        "status": pipeline.get("status", "running"),
        "client_name": pipeline.get("client_name", ""),
        "pipeline_type": pipeline.get("type", ""),
        "updated_at": datetime.now().isoformat(),
    }
    _request("POST", "/pipelines", json_body=row,
             headers={"Prefer": "resolution=merge-duplicates"})


def load_pipeline(pipeline_id: str) -> dict | None:
    """Load a single pipeline by ID."""
    rows = _request("GET", "/pipelines", params={"select": "data", "id": f"eq.{pipeline_id}"})
    if rows:
        return json.loads(rows[0]["data"])
    return None


def load_all_pipelines() -> list[dict]:
    """Load all pipeline records."""
    rows = _request("GET", "/pipelines", params={"select": "data"})
    pipelines = []
    for row in rows:
        try:
            pipelines.append(json.loads(row["data"]))
        except Exception as e:
            log.warning("Failed to parse pipeline: %s", e)
    return pipelines


# ---------------------------------------------------------------------------
# Pending Deletions
# ---------------------------------------------------------------------------

def get_pending_deletions() -> list[dict]:
    """Get all pending deletion entries."""
    return _request("GET", "/pending_deletions", params={"select": "*"})


def add_pending_deletion(entry: dict) -> None:
    """Add a pending deletion record."""
    row = {
        "domain": entry.get("domain", ""),
        "smartlead_account_ids": json.dumps(entry.get("smartlead_account_ids", [])),
        "mailbox_ids": json.dumps(entry.get("mailbox_ids", [])),
        "renewal_date": entry.get("renewal_date", ""),
        "removal_date": entry.get("removal_date", ""),
        "client_name": entry.get("client_name", ""),
        "pipeline_id": entry.get("pipeline_id", ""),
        "scheduled_at": entry.get("scheduled_at", datetime.now().isoformat()),
    }
    _request("POST", "/pending_deletions", json_body=row)


def remove_pending_deletion(domain: str) -> None:
    """Remove a pending deletion after it's been processed."""
    _request("DELETE", "/pending_deletions", params={"domain": f"eq.{domain}"})


# ---------------------------------------------------------------------------
# Client Rotations (A/B group swap)
# ---------------------------------------------------------------------------

def get_all_rotations() -> list[dict]:
    """Get all client rotation records."""
    return _request("GET", "/client_rotations", params={"select": "*", "order": "client_name"})


def get_rotation(client_name: str) -> dict | None:
    """Get a single client's rotation record."""
    rows = _request("GET", "/client_rotations", params={
        "select": "*",
        "client_name": f"eq.{client_name}",
    })
    return rows[0] if rows else None


def upsert_rotation(client_name: str, group_a_ids: list, group_b_ids: list,
                    active_group: str = "A", last_swap_date: str = "") -> None:
    """Create or update a rotation record."""
    row = {
        "client_name": client_name,
        "group_a_ids": json.dumps(group_a_ids),
        "group_b_ids": json.dumps(group_b_ids),
        "active_group": active_group,
        "last_swap_date": last_swap_date,
    }
    _request("POST", "/client_rotations", json_body=row, headers={
        "Prefer": "resolution=merge-duplicates",
    })


def update_rotation_swap(client_name: str, new_active: str, swap_date: str) -> None:
    """Flip the active group after a swap."""
    _request("PATCH", "/client_rotations", params={
        "client_name": f"eq.{client_name}",
    }, json_body={
        "active_group": new_active,
        "last_swap_date": swap_date,
    })


# ---------------------------------------------------------------------------
# Setup Pipelines
# ---------------------------------------------------------------------------

def create_setup_pipeline(name: str, pipeline_type: str, config: dict, steps: list) -> str:
    """Create a new setup pipeline. Returns the generated UUID."""
    row = {
        "name": name, "type": pipeline_type, "config": json.dumps(config),
        "status": "pending", "current_step": 0, "steps": json.dumps(steps),
    }
    result = _request("POST", "/setup_pipelines", json_body=row,
                      headers={"Prefer": "return=representation"})
    return result[0]["id"] if result else ""


def get_setup_pipeline(pipeline_id: str) -> dict | None:
    rows = _request("GET", "/setup_pipelines",
                    params={"select": "*", "id": f"eq.{pipeline_id}"})
    if rows:
        r = rows[0]
        if isinstance(r.get("config"), str):
            r["config"] = json.loads(r["config"])
        if isinstance(r.get("steps"), str):
            r["steps"] = json.loads(r["steps"])
        return r
    return None


def list_setup_pipelines(status: str = None) -> list[dict]:
    params = {"select": "*", "order": "created_at.desc", "limit": "50"}
    if status:
        params["status"] = f"eq.{status}"
    rows = _request("GET", "/setup_pipelines", params=params)
    for r in rows:
        if isinstance(r.get("config"), str):
            r["config"] = json.loads(r["config"])
        if isinstance(r.get("steps"), str):
            r["steps"] = json.loads(r["steps"])
    return rows


def update_setup_pipeline(pipeline_id: str, **fields) -> None:
    """Update arbitrary fields on a setup pipeline. JSON-encodes config/steps if present."""
    body = {}
    for k, v in fields.items():
        if k in ("config", "steps") and not isinstance(v, str):
            body[k] = json.dumps(v)
        else:
            body[k] = v
    body["updated_at"] = "now()"
    _request("PATCH", "/setup_pipelines",
             params={"id": f"eq.{pipeline_id}"}, json_body=body)


# ---------------------------------------------------------------------------
# Client Configs
# ---------------------------------------------------------------------------

def save_client_config(client_name: str, config: dict) -> None:
    """Upsert a client config record."""
    safe_name = client_name.lower().replace(" ", "_")
    row = {
        "id": safe_name,
        "client_name": client_name,
        "data": json.dumps(config, default=str),
        "updated_at": datetime.now().isoformat(),
    }
    _request("POST", "/client_configs", json_body=row,
             headers={"Prefer": "resolution=merge-duplicates"})


def load_client_config(client_name: str) -> dict | None:
    """Load a client config by name."""
    safe_name = client_name.lower().replace(" ", "_")
    rows = _request("GET", "/client_configs", params={"select": "data", "id": f"eq.{safe_name}"})
    if rows:
        return json.loads(rows[0]["data"])
    return None


def load_all_client_configs() -> list[dict]:
    """Load all client configs."""
    rows = _request("GET", "/client_configs", params={"select": "data"})
    configs = []
    for row in rows:
        try:
            configs.append(json.loads(row["data"]))
        except Exception:
            continue
    return configs


# ---------------------------------------------------------------------------
# Monitor Log
# ---------------------------------------------------------------------------

def log_monitor_event(event_type: str, details: dict) -> None:
    """Write an audit log entry for monitor actions."""
    row = {
        "event_type": event_type,
        "details": json.dumps(details, default=str),
        "created_at": datetime.now().isoformat(),
    }
    _request("POST", "/monitor_log", json_body=row)


# ---------------------------------------------------------------------------
# State tracking (placement tests, etc.)
# ---------------------------------------------------------------------------

def get_state(key: str) -> dict | None:
    """Get a key-value state record."""
    rows = _request("GET", "/state", params={"select": "data", "key": f"eq.{key}"})
    if rows:
        return json.loads(rows[0]["data"])
    return None


def set_state(key: str, data: dict) -> None:
    """Upsert a key-value state record."""
    row = {
        "key": key,
        "data": json.dumps(data, default=str),
        "updated_at": datetime.now().isoformat(),
    }
    _request("POST", "/state", json_body=row,
             headers={"Prefer": "resolution=merge-duplicates"})


# ---------------------------------------------------------------------------
# SmartLead Cache (for fast dashboard loads)
# ---------------------------------------------------------------------------

_CACHE_WRITE_ENABLED = False

def cache_set(key: str, data) -> None:
    """Write a cache entry. Only works when _CACHE_WRITE_ENABLED is True (set by sync.py)."""
    if not _CACHE_WRITE_ENABLED:
        return
    if key in ("overview", "overview_v2"):
        clients = data.get("clients", []) if isinstance(data, dict) else []
        if len(clients) < 8:
            print(f"[cache_set] BLOCKED {key} write — only {len(clients)} clients (need >= 8)")
            return
        print(f"[cache_set] Writing {key} with {len(clients)} clients")
    prefixed = f"cache:{key}"
    row = {
        "key": prefixed,
        "data": json.dumps(data, default=str),
        "updated_at": datetime.now().isoformat(),
    }
    _request("POST", "/state", json_body=row,
             headers={"Prefer": "resolution=merge-duplicates"})


def cache_get(key: str):
    """Read a cache entry. Returns (data, updated_at) or (None, None)."""
    prefixed = f"cache:{key}"
    rows = _request("GET", "/state",
                    params={"select": "data,updated_at", "key": f"eq.{prefixed}"})
    if rows:
        return json.loads(rows[0]["data"]), rows[0]["updated_at"]
    return None, None


def cache_patch(key: str, data) -> None:
    """Write a cache entry (no guard). For use by API endpoints after mutations."""
    prefixed = f"cache:{key}"
    row = {
        "key": prefixed,
        "data": json.dumps(data, default=str),
        "updated_at": datetime.now().isoformat(),
    }
    _request("POST", "/state", json_body=row,
             headers={"Prefer": "resolution=merge-duplicates"})


# ---------------------------------------------------------------------------
# Domain Inventory (replaces Google Sheets "THT Domains" tab)
# ---------------------------------------------------------------------------

def get_all_domains(status=None, pool=None, client=None):
    """Get domains with optional filters. Returns list of dicts."""
    params = {"select": "*", "order": "domain.asc"}
    if status:
        params["status"] = f"eq.{status}"
    if pool:
        params["pool"] = f"eq.{pool}"
    if client:
        params["client"] = f"eq.{client}"
    all_rows = []
    page_size = 1000
    offset = 0
    while True:
        params["limit"] = str(page_size)
        params["offset"] = str(offset)
        rows = _request("GET", "/domains", params=params)
        if not rows:
            break
        all_rows.extend(rows)
        if len(rows) < page_size:
            break
        offset += page_size
    return all_rows


def get_available_domains(pool="client"):
    """Get available domains for a pool. Replaces sheets.get_available_domains/get_acquisition_domains."""
    return get_all_domains(status="available", pool=pool)


def insert_domain(domain, provider, status="available", pool="client", client=""):
    """Insert a single domain. Called after purchase."""
    row = {
        "domain": domain.strip().lower(),
        "status": status,
        "provider": provider.strip().lower(),
        "client": client,
        "pool": pool,
        "updated_at": datetime.now().isoformat(),
    }
    _request("POST", "/domains", json_body=row,
             headers={"Prefer": "resolution=merge-duplicates"})


def insert_domains_batch(domains_list):
    """Insert multiple domains at once. Each item: {domain, provider, status, pool, client}."""
    rows = []
    for d in domains_list:
        rows.append({
            "domain": d["domain"].strip().lower(),
            "status": d.get("status", "available"),
            "provider": d.get("provider", "").strip().lower(),
            "client": d.get("client", ""),
            "pool": d.get("pool", "client"),
            "updated_at": datetime.now().isoformat(),
        })
    if rows:
        _request("POST", "/domains", json_body=rows,
                 headers={"Prefer": "resolution=merge-duplicates"})


def claim_domains(domain_names, client_name):
    """Mark domains as in_use and assign to a client. Replaces sheets.claim_domains."""
    for name in domain_names:
        _request("PATCH", "/domains",
                 params={"domain": f"eq.{name.strip().lower()}"},
                 json_body={"status": "in_use", "client": client_name,
                            "updated_at": datetime.now().isoformat()})


def update_domain(domain_name, **fields):
    """Update arbitrary fields on a domain record."""
    fields["updated_at"] = datetime.now().isoformat()
    _request("PATCH", "/domains",
             params={"domain": f"eq.{domain_name.strip().lower()}"},
             json_body=fields)


def get_domain_summary():
    """Get counts by status, provider, pool."""
    all_d = get_all_domains()
    summary = {"total": len(all_d), "by_status": {}, "by_provider": {}, "by_pool": {}}
    for d in all_d:
        s = d.get("status", "")
        summary["by_status"][s] = summary["by_status"].get(s, 0) + 1
        p = d.get("provider", "")
        if p:
            summary["by_provider"][p] = summary["by_provider"].get(p, 0) + 1
        pool = d.get("pool", "")
        if pool:
            summary["by_pool"][pool] = summary["by_pool"].get(pool, 0) + 1
    return summary


# ---------------------------------------------------------------------------
# Inbox History (audit log for every inbox state change)
# ---------------------------------------------------------------------------

def _build_history_row(account_id, email, event_type, old_value, new_value, source):
    """Build a single inbox_history row dict."""
    return {
        "account_id": account_id,
        "email": email,
        "event_type": event_type,
        "old_value": json.dumps(old_value, default=str) if old_value else None,
        "new_value": json.dumps(new_value, default=str) if new_value else None,
        "source": source,
    }


def _parse_history_rows(rows):
    """Deserialize JSON strings in old_value/new_value fields."""
    for r in rows:
        if isinstance(r.get("old_value"), str):
            r["old_value"] = json.loads(r["old_value"])
        if isinstance(r.get("new_value"), str):
            r["new_value"] = json.loads(r["new_value"])
    return rows


def log_inbox_event(account_id: int, email: str, event_type: str,
                    old_value: dict | None, new_value: dict | None,
                    source: str = "dashboard") -> None:
    """Write a single inbox history event."""
    row = _build_history_row(account_id, email, event_type, old_value, new_value, source)
    try:
        _request("POST", "/inbox_history", json_body=row)
    except Exception as e:
        log.warning("Failed to log inbox event for %s: %s", email, e)


def log_inbox_events(events: list[dict]) -> None:
    """Bulk-write inbox history events. Each dict needs: account_id, email,
    event_type, old_value, new_value, source."""
    if not events:
        return
    rows = [_build_history_row(
        ev["account_id"], ev.get("email", ""), ev["event_type"],
        ev.get("old_value"), ev.get("new_value"), ev.get("source", "dashboard"),
    ) for ev in events]
    try:
        _request("POST", "/inbox_history", json_body=rows)
    except Exception as e:
        log.warning("Failed to bulk-log %d inbox events: %s", len(rows), e)


def get_inbox_history(account_id: int = None, limit: int = 100) -> list[dict]:
    """Get inbox history. Per-inbox if account_id given, global otherwise."""
    params = {"select": "*", "order": "created_at.desc", "limit": str(limit)}
    if account_id:
        params["account_id"] = f"eq.{account_id}"
    return _parse_history_rows(_request("GET", "/inbox_history", params=params))


# ---------------------------------------------------------------------------
# Inbox Groups (source of truth for group state)
# ---------------------------------------------------------------------------

def get_all_inbox_groups(status: str = None) -> list[dict]:
    """Get all inbox groups, optionally filtered by status."""
    params = {"select": "*", "order": "group_letter,batch"}
    if status:
        params["status"] = f"eq.{status}"
    rows = _request("GET", "/inbox_groups", params=params)
    for r in rows:
        for col in ("account_ids", "account_emails", "domains", "campaign_ids", "tag_ids", "drift_flags"):
            if isinstance(r.get(col), str):
                r[col] = json.loads(r[col])
    return rows


def get_inbox_group(group_letter: str, batch: int = 1) -> dict | None:
    """Get a single inbox group by letter + batch."""
    rows = _request("GET", "/inbox_groups", params={
        "select": "*",
        "group_letter": f"eq.{group_letter}",
        "batch": f"eq.{batch}",
    })
    if not rows:
        return None
    r = rows[0]
    for col in ("account_ids", "account_emails", "domains", "campaign_ids", "tag_ids", "drift_flags"):
        if isinstance(r.get(col), str):
            r[col] = json.loads(r[col])
    return r


def get_inbox_group_by_id(group_id: int) -> dict | None:
    """Get a single inbox group by primary key."""
    rows = _request("GET", "/inbox_groups", params={
        "select": "*",
        "id": f"eq.{group_id}",
    })
    if not rows:
        return None
    r = rows[0]
    for col in ("account_ids", "account_emails", "domains", "campaign_ids", "tag_ids", "drift_flags"):
        if isinstance(r.get(col), str):
            r[col] = json.loads(r[col])
    return r


def upsert_inbox_group(data: dict) -> dict:
    """Insert or update an inbox group. Returns the upserted row."""
    data["updated_at"] = datetime.utcnow().isoformat()
    for col in ("account_ids", "account_emails", "domains", "campaign_ids", "tag_ids", "drift_flags"):
        if col in data and not isinstance(data[col], str):
            data[col] = json.dumps(data[col])
    result = _request("POST", "/inbox_groups", json_body=data, headers={
        "Prefer": "resolution=merge-duplicates,return=representation",
    })
    return result[0] if result else data


def update_inbox_group(group_id: int, **fields) -> None:
    """Update specific fields on an inbox group by ID."""
    fields["updated_at"] = datetime.utcnow().isoformat()
    for col in ("account_ids", "account_emails", "domains", "campaign_ids", "tag_ids", "drift_flags"):
        if col in fields and not isinstance(fields[col], str):
            fields[col] = json.dumps(fields[col])
    _request("PATCH", "/inbox_groups", params={
        "id": f"eq.{group_id}",
    }, json_body=fields)


def check_campaign_exclusivity(group_id: int, campaign_id: int) -> dict | None:
    """Check if a group is already in an active campaign.

    Returns None if clear, or a dict with the conflicting campaign info.
    """
    group = get_inbox_group_by_id(group_id)
    if not group:
        return None
    existing = group.get("campaign_ids") or []
    if isinstance(existing, str):
        existing = json.loads(existing)
    for cid in existing:
        if cid != campaign_id and cid:
            return {"group_id": group_id, "group_tag": group.get("group_tag", ""), "conflicting_campaign_id": cid}
    return None


def get_inbox_group_by_tag(group_tag: str) -> dict | None:
    """Get an inbox group by its group_tag."""
    rows = _request("GET", "/inbox_groups", params={
        "select": "*",
        "group_tag": f"eq.{group_tag}",
    })
    if not rows:
        return None
    r = rows[0]
    for col in ("account_ids", "account_emails", "domains", "campaign_ids", "tag_ids", "drift_flags"):
        if isinstance(r.get(col), str):
            r[col] = json.loads(r[col])
    return r


def log_group_event(group_id: int, event: str, details: dict, previous_state: dict = None) -> None:
    """Append an event to inbox_group_history."""
    _request("POST", "/inbox_group_history", json_body={
        "group_id": group_id,
        "event": event,
        "details": json.dumps(details) if not isinstance(details, str) else details,
        "previous_state": json.dumps(previous_state or {}) if not isinstance(previous_state or {}, str) else (previous_state or "{}"),
    })


def get_group_history(group_id: int = None, limit: int = 100) -> list[dict]:
    """Get group history. Per-group if group_id given, global otherwise."""
    params = {"select": "*", "order": "created_at.desc", "limit": str(limit)}
    if group_id:
        params["group_id"] = f"eq.{group_id}"
    rows = _request("GET", "/inbox_group_history", params=params)
    for r in rows:
        for col in ("details", "previous_state"):
            if isinstance(r.get(col), str):
                r[col] = json.loads(r[col])
    return rows


# ---------------------------------------------------------------------------
# Health V1  (inbox_health_daily / inbox_health_status / inbox_health_config)
# ---------------------------------------------------------------------------

def upsert_health_daily(rows: list[dict]) -> None:
    """Upsert per-inbox per-day metric rows (unique on email+date)."""
    if not rows:
        return
    _request("POST", "/inbox_health_daily", params={"on_conflict": "email,date"},
             json_body=rows, headers={"Prefer": "resolution=merge-duplicates"})


def get_health_daily(email: str, limit: int = 14) -> list[dict]:
    """Recent daily rows for one inbox, newest first."""
    return _request("GET", "/inbox_health_daily", params={
        "select": "*", "email": f"eq.{email}",
        "order": "date.desc", "limit": str(limit),
    })


def get_health_daily_bulk(since_date: str) -> dict:
    """All daily rows since `since_date` (YYYY-MM-DD), grouped email -> [rows].
    One request instead of one-per-inbox — used by the snapshot scorer."""
    rows = _request("GET", "/inbox_health_daily", params={
        "select": "*", "date": f"gte.{since_date}", "order": "date.desc",
    })
    out: dict[str, list] = {}
    for r in rows:
        out.setdefault(r["email"], []).append(r)
    return out


def upsert_health_status(rows: list[dict]) -> None:
    """Upsert current computed status rows (primary key = email)."""
    if not rows:
        return
    # serialize jsonb columns on COPIES — never mutate the caller's dicts
    # (the same list objects are reused for the health_fleet cache).
    payload = []
    for r in rows:
        row = dict(r)
        for col in ("reasons", "subscores", "campaigns"):
            if col in row and not isinstance(row[col], str):
                row[col] = json.dumps(row[col])
        payload.append(row)
    _request("POST", "/inbox_health_status", params={"on_conflict": "email"},
             json_body=payload, headers={"Prefer": "resolution=merge-duplicates"})


def get_health_status_all() -> list[dict]:
    """All current inbox statuses — paginated past PostgREST's 1000-row cap."""
    rows, offset = [], 0
    while True:
        page = _request("GET", "/inbox_health_status", params={
            "select": "*", "order": "email.asc",
            "limit": "1000", "offset": str(offset),
        })
        rows.extend(page)
        if len(page) < 1000:
            break
        offset += 1000
    for r in rows:
        for col in ("reasons", "subscores"):
            if isinstance(r.get(col), str):
                try:
                    r[col] = json.loads(r[col])
                except Exception:
                    pass
    return rows


def get_health_config(key: str = "default") -> dict:
    """Load tunable weights/thresholds; {} if unset (model falls back to defaults)."""
    rows = _request("GET", "/inbox_health_config",
                    params={"select": "value", "key": f"eq.{key}"})
    return rows[0]["value"] if rows else {}
