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


def step_profile_photos(pipeline_id: str, config: dict) -> bool:
    """Step 3: Set profile photos on all mailboxes."""
    _start_step(pipeline_id, 2)
    mailbox_ids = config.get("mailbox_ids", [])
    sender = config.get("sender", "sean_reynolds")
    photo_url = PHOTO_URLS.get(sender, PROFILE_PHOTO_URL)

    if not mailbox_ids:
        _fail_step(pipeline_id, 2, "No mailbox IDs found in config")
        return False

    done = 0
    batch_size = 50
    for i in range(0, len(mailbox_ids), batch_size):
        batch = mailbox_ids[i:i + batch_size]
        mailbox_data = [{"mailboxId": mid, "profilePicture": photo_url} for mid in batch]
        try:
            result = zm_put("/v2/mailboxes", {"mailboxData": mailbox_data})
            if isinstance(result, dict) and result.get("_raw_status", 200) >= 400:
                log(f"[PIPELINE] Photo batch failed: {result.get('message', '')[:100]}", "WARN")
            else:
                done += len(batch)
        except Exception as e:
            log(f"[PIPELINE] Photo batch error: {e}", "WARN")
        _progress(pipeline_id, 2, done)
        time.sleep(1)

    _finish_step(pipeline_id, 2)
    return True


def step_smartlead_export(pipeline_id: str, config: dict) -> bool:
    """Step 4: Export mailboxes to SmartLead and verify they appear."""
    _start_step(pipeline_id, 3)
    mailbox_ids = config.get("mailbox_ids", [])
    domains = set(config["domains"])
    expected = len(domains) * INBOXES_PER_DOMAIN

    if not mailbox_ids:
        _fail_step(pipeline_id, 3, "No mailbox IDs found in config")
        return False

    # Export
    result = zm_export_mailboxes(apps=["SMARTLEAD"], mailbox_ids=mailbox_ids)
    log(f"[PIPELINE] Export result: {str(result)[:200]}")

    # Wait for propagation then verify
    time.sleep(180)

    max_attempts = 5
    found = {}
    for attempt in range(max_attempts):
        found = {}
        offset = 0
        while True:
            try:
                batch = sl_list_accounts(offset=offset, limit=100)
            except Exception:
                time.sleep(5)
                break
            if not isinstance(batch, list) or not batch:
                break
            for acc in batch:
                email = acc.get("from_email", acc.get("email", ""))
                domain = email.split("@")[-1] if "@" in email else ""
                if domain in domains:
                    found[email] = acc["id"]
            if len(batch) < 100:
                break
            offset += 100

        _progress(pipeline_id, 3, len(found))
        log(f"[PIPELINE] Export verify attempt {attempt + 1}: {len(found)}/{expected}")

        if len(found) >= expected:
            break

        if attempt < max_attempts - 1:
            # Re-export missing domains
            found_domains = {email.split("@")[-1] for email in found}
            missing = domains - found_domains
            if missing:
                for domain in missing:
                    zm_export_mailboxes(apps=["SMARTLEAD"], contains=domain)
                    time.sleep(2)
            time.sleep(180)

    # Store account IDs
    config["smartlead_account_ids"] = found
    store.update_setup_pipeline(pipeline_id, config=config)

    if len(found) < expected * 0.8:
        _fail_step(pipeline_id, 3,
                   f"Only {len(found)}/{expected} accounts found in SmartLead after {max_attempts} attempts")
        return False

    _finish_step(pipeline_id, 3)
    return True


def step_tag_assign(pipeline_id: str, config: dict) -> bool:
    """Step 5: Tag accounts and assign to SmartLead client."""
    _start_step(pipeline_id, 4)
    account_map = config.get("smartlead_account_ids", {})
    if not account_map:
        _fail_step(pipeline_id, 4, "No SmartLead account IDs in config")
        return False

    tag_ids = list(config.get("tag_ids", {}).values())
    client_id = config.get("smartlead_client_id")
    account_ids = list(account_map.values())

    if not tag_ids:
        _fail_step(pipeline_id, 4, "No tag IDs in config")
        return False

    log(f"[PIPELINE] Tagging {len(account_ids)} accounts with tags {tag_ids}, client_id={client_id}")
    success, fail = sl_tag_accounts_bulk(account_ids, tag_ids, client_id=client_id)
    _progress(pipeline_id, 4, success)

    if fail > 0:
        # Retry failures individually
        log(f"[PIPELINE] Retrying {fail} failed tags individually...")
        time.sleep(2)
        retry_success = 0
        for aid in account_ids:
            try:
                s, f = sl_tag_accounts_bulk([aid], tag_ids, client_id=client_id)
                retry_success += s
            except Exception:
                pass
            time.sleep(0.3)
        _progress(pipeline_id, 4, success + retry_success)

    _finish_step(pipeline_id, 4)
    return True


def step_enable_warmup(pipeline_id: str, config: dict) -> bool:
    """Step 6: Enable warmup on all SmartLead accounts."""
    _start_step(pipeline_id, 5)
    account_map = config.get("smartlead_account_ids", {})
    if not account_map:
        _fail_step(pipeline_id, 5, "No SmartLead account IDs in config")
        return False

    done = 0
    failures = 0
    total = len(account_map)
    for email, aid in account_map.items():
        try:
            r = requests.post(
                f"{SMARTLEAD_API}/email-accounts/{aid}/warmup",
                params={"api_key": SMARTLEAD_KEY},
                json=WARMUP_CONFIG,
                timeout=15,
            )
            if r.status_code == 200:
                done += 1
            else:
                failures += 1
        except Exception:
            failures += 1
        _progress(pipeline_id, 5, done)
        time.sleep(0.3)

    log(f"[PIPELINE] Warmup enabled: {done}/{total} ({failures} failed)")

    if failures > total * 0.2:
        _fail_step(pipeline_id, 5, f"Warmup failed for {failures}/{total} accounts")
        return False

    _finish_step(pipeline_id, 5)
    return True


# Step functions indexed to match STEP_NAMES
_STEP_FUNCTIONS = [
    step_connect_domains,
    step_create_inboxes,
    step_profile_photos,
    step_smartlead_export,
    step_tag_assign,
    step_enable_warmup,
]


def run_pipeline(pipeline_id: str):
    """Execute all pending steps in sequence. Called in a background thread."""
    p = store.get_setup_pipeline(pipeline_id)
    if not p:
        log(f"[PIPELINE] Pipeline {pipeline_id} not found", "ERROR")
        return

    store.update_setup_pipeline(pipeline_id, status="running")
    config = p["config"]
    steps = p["steps"]

    for i, step_fn in enumerate(_STEP_FUNCTIONS):
        # Skip completed steps (for resume after restart)
        if steps[i]["status"] == "completed":
            continue
        # Stop if a previous step failed
        if i > 0 and steps[i - 1]["status"] == "failed":
            break

        log(f"[PIPELINE] {p['name']}: starting step {i + 1}/6 — {STEP_NAMES[i]}")
        success = step_fn(pipeline_id, config)

        if not success:
            log(f"[PIPELINE] {p['name']}: step {STEP_NAMES[i]} failed")
            return

        # Re-read config in case step updated it (mailbox_ids, account_ids)
        p = store.get_setup_pipeline(pipeline_id)
        if not p:
            return
        config = p["config"]
        steps = p["steps"]

    store.update_setup_pipeline(pipeline_id, status="completed")
    log(f"[PIPELINE] {p['name']}: COMPLETE")

    # macOS notification
    try:
        os.system(f'osascript -e \'display notification "{p["name"]} pipeline complete!" '
                  f'with title "Email Infra" sound name "Glass"\'')
    except Exception:
        pass


def create_and_start(name: str, pipeline_type: str, config: dict) -> str:
    """Create a pipeline and start it in a background thread. Returns pipeline ID."""
    steps = _init_steps(len(config["domains"]))
    pipeline_id = store.create_setup_pipeline(name, pipeline_type, config, steps)
    if not pipeline_id:
        raise RuntimeError("Failed to create pipeline in Supabase")

    t = threading.Thread(target=run_pipeline, args=(pipeline_id,), daemon=True,
                         name=f"pipeline-{name}")
    _active_threads[pipeline_id] = t
    t.start()
    return pipeline_id


def retry_failed_step(pipeline_id: str) -> bool:
    """Retry the current failed step. Returns True if retry started."""
    p = store.get_setup_pipeline(pipeline_id)
    if not p or p["status"] != "failed":
        return False

    steps = p["steps"]
    failed_idx = None
    for i, s in enumerate(steps):
        if s["status"] == "failed":
            failed_idx = i
            break

    if failed_idx is None:
        return False

    # Reset the failed step
    steps[failed_idx]["status"] = "running"
    steps[failed_idx]["error"] = None
    store.update_setup_pipeline(pipeline_id, steps=steps, status="running")

    # Re-run from the failed step
    t = threading.Thread(target=run_pipeline, args=(pipeline_id,), daemon=True,
                         name=f"pipeline-retry-{p['name']}")
    _active_threads[p["id"]] = t
    t.start()
    return True


def resume_running_pipelines():
    """On server start, resume any pipelines that were running when the server stopped."""
    running = store.list_setup_pipelines(status="running")
    for p in running:
        log(f"[PIPELINE] Resuming: {p['name']} (step {p['current_step'] + 1}/6)")
        t = threading.Thread(target=run_pipeline, args=(p["id"],), daemon=True,
                             name=f"pipeline-resume-{p['name']}")
        _active_threads[p["id"]] = t
        t.start()