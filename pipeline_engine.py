"""Infrastructure setup pipeline engine.

Runs as background threads in the dashboard. State persists to the
setup_pipelines Supabase table so pipelines survive server restarts.
"""
import json
import os
import threading
import time
from datetime import datetime

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import db as store
from setup import (
    zm_connect_domain_single, zm_list_domains, zm_create_mailboxes,
    zm_buy_addon_mailboxes, zm_export_mailboxes, zm_put, zm_get,
    sl_list_accounts, sl_get_all_tags, sl_find_or_create_tag,
    sl_tag_accounts_bulk,
    SMARTLEAD_API, SMARTLEAD_KEY,
    PROFILE_PHOTO_URL, ACQUISITION_PHOTO_URL, ACQUISITION_PHOTO_URLS,
    log,
)
import requests

# Mailbox specs per sender
SENDER_SPECS = {
    "sean_reynolds": [
        {"firstName": "Sean", "lastName": "Reynolds", "mailboxUsername": "s.reynolds"},
        {"firstName": "Sean", "lastName": "Reynolds", "mailboxUsername": "sean.r"},
        {"firstName": "Sean", "lastName": "Reynolds", "mailboxUsername": "sean.reynolds"},
    ],
    "aidan_hutchinson": [
        {"firstName": "Aidan", "lastName": "Hutchinson", "mailboxUsername": "aidan"},
        {"firstName": "Aidan", "lastName": "Hutchinson", "mailboxUsername": "aidanh"},
        {"firstName": "Aidan", "lastName": "Hutchinson", "mailboxUsername": "aidanhutch"},
    ],
    "lars_matthys": [
        {"firstName": "Lars", "lastName": "Matthys", "mailboxUsername": "lars"},
        {"firstName": "Lars", "lastName": "Matthys", "mailboxUsername": "lars.m"},
        {"firstName": "Lars", "lastName": "Matthys", "mailboxUsername": "larsmatthys"},
    ],
}

PHOTO_URLS = {
    "sean_reynolds": PROFILE_PHOTO_URL,
    "aidan_hutchinson": ACQUISITION_PHOTO_URLS.get("aidan_hutchinson", ACQUISITION_PHOTO_URL),
    "lars_matthys": ACQUISITION_PHOTO_URLS.get("lars_matthys", ACQUISITION_PHOTO_URL),
}

STEP_NAMES = [
    "Connect Domains",
    "Create Inboxes",
    "Profile Photos",
    "SmartLead Export",
    "Tag & Assign",
    "Enable Warmup",
]

INBOXES_PER_DOMAIN = 3

WARMUP_CONFIG = {
    "warmup_enabled": True,
    "total_warmup_per_day": 30,
    "daily_rampup": 2,
    "reply_rate_percentage": 30,
}

# Active pipeline threads keyed by pipeline ID
_active_threads: dict[str, threading.Thread] = {}


def _init_steps(total_domains: int) -> list[dict]:
    """Build the initial 6-step array."""
    total_mbs = total_domains * INBOXES_PER_DOMAIN
    totals = [total_domains, total_mbs, total_mbs, total_mbs, total_mbs, total_mbs]
    return [
        {"name": name, "status": "pending", "progress": 0, "total": t,
         "error": None, "started_at": None, "completed_at": None}
        for name, t in zip(STEP_NAMES, totals)
    ]


def _update_step(pipeline_id: str, step_idx: int, **fields):
    """Update a single step in the steps array and persist."""
    p = store.get_setup_pipeline(pipeline_id)
    if not p:
        return
    steps = p["steps"]
    steps[step_idx].update(fields)
    store.update_setup_pipeline(pipeline_id, steps=steps, current_step=step_idx)


def _start_step(pipeline_id: str, step_idx: int):
    _update_step(pipeline_id, step_idx,
                 status="running", started_at=datetime.utcnow().isoformat() + "Z")


def _finish_step(pipeline_id: str, step_idx: int):
    p = store.get_setup_pipeline(pipeline_id)
    if not p:
        return
    steps = p["steps"]
    steps[step_idx]["status"] = "completed"
    steps[step_idx]["completed_at"] = datetime.utcnow().isoformat() + "Z"
    store.update_setup_pipeline(pipeline_id, steps=steps, current_step=step_idx)


def _fail_step(pipeline_id: str, step_idx: int, error: str):
    _update_step(pipeline_id, step_idx, status="failed", error=error)
    store.update_setup_pipeline(pipeline_id, status="failed")


def _progress(pipeline_id: str, step_idx: int, progress: int):
    """Update progress count without re-reading full pipeline (lightweight)."""
    p = store.get_setup_pipeline(pipeline_id)
    if not p:
        return
    steps = p["steps"]
    steps[step_idx]["progress"] = progress
    store.update_setup_pipeline(pipeline_id, steps=steps)


def step_connect_domains(pipeline_id: str, config: dict) -> bool:
    """Step 1: Connect domains to Zapmail."""
    _start_step(pipeline_id, 0)
    domains = config["domains"]

    # Check which are already connected
    existing = zm_list_domains()
    existing_names = {d.get("domain", "") for d in existing}

    connected = 0
    failures = []
    for domain in domains:
        if domain in existing_names:
            connected += 1
            _progress(pipeline_id, 0, connected)
            continue
        try:
            result = zm_connect_domain_single(domain)
            if isinstance(result, dict) and result.get("_raw_status", 200) >= 400:
                failures.append(f"{domain}: {result.get('message', 'unknown error')[:80]}")
            else:
                connected += 1
        except Exception as e:
            failures.append(f"{domain}: {e}")
        _progress(pipeline_id, 0, connected)
        time.sleep(0.5)

    if failures and len(failures) > len(domains) * 0.2:
        _fail_step(pipeline_id, 0, f"{len(failures)} domains failed: " + "; ".join(failures[:5]))
        return False

    _finish_step(pipeline_id, 0)
    return True


def step_create_inboxes(pipeline_id: str, config: dict) -> bool:
    """Step 2: Buy mailbox slots if needed, create mailboxes on each domain."""
    _start_step(pipeline_id, 1)
    domains = config["domains"]
    sender = config.get("sender", "sean_reynolds")
    specs = SENDER_SPECS.get(sender, SENDER_SPECS["sean_reynolds"])

    # Get domain IDs and existing mailbox counts from Zapmail
    all_zm = zm_list_domains()
    zm_by_name = {d.get("domain", ""): d for d in all_zm}

    domains_needing_inboxes = []
    mailbox_ids = []
    already_done = 0
    for domain in domains:
        d = zm_by_name.get(domain)
        if not d:
            continue
        existing_mbs = d.get("mailboxes", [])
        if len(existing_mbs) >= INBOXES_PER_DOMAIN:
            already_done += len(existing_mbs)
            mailbox_ids.extend(m["id"] for m in existing_mbs)
        else:
            domains_needing_inboxes.append((domain, d["id"]))

    _progress(pipeline_id, 1, already_done)

    # Buy slots if needed
    if domains_needing_inboxes:
        needed = len(domains_needing_inboxes) * INBOXES_PER_DOMAIN
        ws = zm_get("/v2/workspaces")
        cw = ws.get("data", {}).get("currentWorkspace", {}) if isinstance(ws, dict) else {}
        total_purchased = int(cw.get("totalMailboxesPurchasedGoogle", "0"))
        total_assigned = int(cw.get("assignedMailboxesCountGoogle", "0"))
        unassigned = total_purchased - total_assigned
        slots_to_buy = max(0, needed - unassigned)

        if slots_to_buy > 0:
            buy_result = zm_buy_addon_mailboxes(slots_to_buy)
            if isinstance(buy_result, dict) and "Insufficient wallet balance" in buy_result.get("message", ""):
                _fail_step(pipeline_id, 1, buy_result["message"])
                return False
            time.sleep(3)  # Let purchase process

    # Create mailboxes
    created = already_done
    failures = []
    for domain, domain_id in domains_needing_inboxes:
        try:
            result = zm_create_mailboxes(domain_id, domain, specs)
            if isinstance(result, dict) and result.get("_raw_status", 200) >= 400:
                msg = result.get("message", "")
                if "don't have enough mailboxes" in msg or "not enough mailboxes" in msg.lower():
                    # Retry after buying more
                    zm_buy_addon_mailboxes(INBOXES_PER_DOMAIN)
                    time.sleep(3)
                    result = zm_create_mailboxes(domain_id, domain, specs)
                if isinstance(result, dict) and result.get("_raw_status", 200) >= 400:
                    failures.append(f"{domain}: {result.get('message', '')[:80]}")
                    continue
            created += INBOXES_PER_DOMAIN
        except Exception as e:
            failures.append(f"{domain}: {e}")
        _progress(pipeline_id, 1, created)
        time.sleep(0.3)

    # Re-fetch to get all mailbox IDs
    all_zm = zm_list_domains()
    zm_by_name = {d.get("domain", ""): d for d in all_zm}
    mailbox_ids = []
    for domain in domains:
        d = zm_by_name.get(domain)
        if d:
            mailbox_ids.extend(m["id"] for m in d.get("mailboxes", []))

    # Store IDs in config
    config["mailbox_ids"] = mailbox_ids
    store.update_setup_pipeline(pipeline_id, config=config)

    if failures and len(failures) > len(domains) * 0.2:
        _fail_step(pipeline_id, 1, f"{len(failures)} domains failed: " + "; ".join(failures[:5]))
        return False

    _finish_step(pipeline_id, 1)
    return True
