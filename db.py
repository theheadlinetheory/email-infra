"""Supabase persistence layer for pipeline state, pending deletions, and client configs.

Replaces JSON file storage with durable cloud persistence.
Requires SUPABASE_URL and SUPABASE_KEY environment variables.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime

from supabase import create_client, Client

log = logging.getLogger("db")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

_client: Client | None = None


def get_client() -> Client:
    """Lazy-init the Supabase client."""
    global _client
    if _client is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set")
        _client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _client


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
    get_client().table("pipelines").upsert(row).execute()


def load_pipeline(pipeline_id: str) -> dict | None:
    """Load a single pipeline by ID."""
    resp = (
        get_client()
        .table("pipelines")
        .select("data")
        .eq("id", pipeline_id)
        .execute()
    )
    if resp.data:
        return json.loads(resp.data[0]["data"])
    return None


def load_all_pipelines() -> list[dict]:
    """Load all pipeline records."""
    resp = get_client().table("pipelines").select("data").execute()
    pipelines = []
    for row in resp.data or []:
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
    resp = get_client().table("pending_deletions").select("*").execute()
    return resp.data or []


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
    get_client().table("pending_deletions").insert(row).execute()


def remove_pending_deletion(domain: str) -> None:
    """Remove a pending deletion after it's been processed."""
    get_client().table("pending_deletions").delete().eq("domain", domain).execute()


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
    get_client().table("client_configs").upsert(row).execute()


def load_client_config(client_name: str) -> dict | None:
    """Load a client config by name."""
    safe_name = client_name.lower().replace(" ", "_")
    resp = (
        get_client()
        .table("client_configs")
        .select("data")
        .eq("id", safe_name)
        .execute()
    )
    if resp.data:
        return json.loads(resp.data[0]["data"])
    return None


def load_all_client_configs() -> list[dict]:
    """Load all client configs."""
    resp = get_client().table("client_configs").select("data").execute()
    configs = []
    for row in resp.data or []:
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
    get_client().table("monitor_log").insert(row).execute()


# ---------------------------------------------------------------------------
# State tracking (placement tests, etc.)
# ---------------------------------------------------------------------------

def get_state(key: str) -> dict | None:
    """Get a key-value state record."""
    resp = (
        get_client()
        .table("state")
        .select("data")
        .eq("key", key)
        .execute()
    )
    if resp.data:
        return json.loads(resp.data[0]["data"])
    return None


def set_state(key: str, data: dict) -> None:
    """Upsert a key-value state record."""
    row = {
        "key": key,
        "data": json.dumps(data, default=str),
        "updated_at": datetime.now().isoformat(),
    }
    get_client().table("state").upsert(row).execute()
