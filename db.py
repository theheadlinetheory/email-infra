"""Supabase persistence layer for pipeline state, pending deletions, and client configs.

Replaces JSON file storage with durable cloud persistence.
Requires SUPABASE_URL and SUPABASE_KEY environment variables.
Uses httpx directly with HTTP/1.1 to avoid StreamReset errors on Render.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime

import httpx

log = logging.getLogger("db")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip().replace("\n", "").replace("\r", "")

_http: httpx.Client | None = None


def _get_http() -> httpx.Client:
    """Lazy-init an HTTP/1.1 client pointed at the Supabase REST API."""
    global _http
    if _http is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set")
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
    """Make a request to the Supabase REST API."""
    h = headers or {}
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
        "renewal_date": entry.get("renewal_date", ""),
        "client_name": entry.get("client_name", ""),
        "pipeline_id": entry.get("pipeline_id", ""),
        "scheduled_at": entry.get("scheduled_at", datetime.now().isoformat()),
    }
    _request("POST", "/pending_deletions", json_body=row)


def remove_pending_deletion(domain: str) -> None:
    """Remove a pending deletion after it's been processed."""
    _request("DELETE", "/pending_deletions", params={"domain": f"eq.{domain}"})


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
