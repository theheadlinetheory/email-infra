"""Pipeline engine for autonomous infrastructure management.

Handles new client setup and domain replacement as state machines.
Each pipeline is persisted to pipelines/<id>.json for crash recovery.
"""

import json
import logging
import random
import time
import uuid
import threading
from datetime import datetime, timedelta
from pathlib import Path

from setup import (
    set_nameservers_for_domain,
    zm_connect_domains,
    zm_create_mailboxes,
    zm_list_mailboxes,
    zm_set_forwarding,
    zm_create_domain_tag,
    zm_assign_domain_tag,
    zm_list_domain_tags,
    zm_export_mailboxes,
    zm_find_domain,
    zm_buy_addon_mailboxes,
    generate_mailbox_specs,
    sl_set_warmup,
    sl_list_accounts,
    sl_find_or_create_tag,
    sl_tag_accounts_bulk,
    sl_get_all_tags,
    SMARTLEAD_API,
    SMARTLEAD_KEY,
)
from zapmail_ops import (
    zm_update_mailboxes,
    zm_remove_on_renewal,
    zm_retry_failed_mailboxes,
    zm_get_export_status,
    zm_verify_nameservers,
    zm_get_subscriptions,
    zm_get_subscription_mailboxes,
)
from sheets import get_available_domains, mark_domains_in_use_batch

import requests

log = logging.getLogger("pipeline")

SCRIPT_DIR = Path(__file__).parent
PIPELINES_DIR = SCRIPT_DIR / "pipelines"
PIPELINES_DIR.mkdir(exist_ok=True)

# Thread safety: one lock per pipeline ID to prevent concurrent step execution
_pipeline_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()

WARMUP_TARGET_REPUTATION = 99
MAILBOXES_PER_DOMAIN = 3
ZAPMAIL_ACTIVATION_TIMEOUT_S = 30 * 60
EXPORT_TIMEOUT_S = 15 * 60

# Headshot URL — served from the dashboard itself.
# When deployed to Render, use the Render URL. Locally, use localhost.
import os as _os
_DASHBOARD_HOST = _os.environ.get("RENDER_EXTERNAL_URL", "http://127.0.0.1:8099")
HEADSHOT_URL = f"{_DASHBOARD_HOST}/headshots/sean_reynolds.png"

# Pipeline step definitions (order matters)
SETUP_STEPS = [
    "claim_domains",
    "set_dns",
    "connect_zapmail",
    "create_mailboxes",
    "upload_photos",
    "tag_and_configure",
    "export_to_smartlead",
    "enable_warmup",
]

REPLACEMENT_STEPS = SETUP_STEPS + [
    "wait_for_warmup",
    "check_campaigns",
    "remove_old",
    "cleanup",
]


def _get_pipeline_lock(pipeline_id):
    """Get or create a lock for a specific pipeline."""
    with _locks_lock:
        if pipeline_id not in _pipeline_locks:
            _pipeline_locks[pipeline_id] = threading.Lock()
        return _pipeline_locks[pipeline_id]


def _mark_all_domains_complete(pipeline, step_name):
    """Mark all domains as complete for a given step."""
    for info in pipeline["domains"].values():
        info["step"] = step_name
        info["step_status"] = "complete"


def _fetch_all_smartlead_accounts():
    """Paginate through all SmartLead accounts."""
    all_accounts = []
    offset = 0
    while True:
        batch = sl_list_accounts(offset=offset, limit=100)
        if not isinstance(batch, list):
            break
        all_accounts.extend(batch)
        if len(batch) < 100:
            break
        offset += 100
    return all_accounts


def generate_pipeline_id():
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def save_pipeline(pipeline):
    """Persist pipeline state to JSON."""
    path = PIPELINES_DIR / f"{pipeline['id']}.json"
    path.write_text(json.dumps(pipeline, indent=2, default=str))


def load_pipeline(pipeline_id):
    """Load a pipeline from disk."""
    path = PIPELINES_DIR / f"{pipeline_id}.json"
    if path.exists():
        return json.loads(path.read_text())
    return None


def load_all_pipelines():
    """Load all pipeline state files."""
    pipelines = []
    for path in PIPELINES_DIR.glob("*.json"):
        try:
            pipelines.append(json.loads(path.read_text()))
        except Exception as e:
            log.warning("Failed to load pipeline %s: %s", path.name, e)
            continue
    return pipelines


def create_pipeline(pipeline_type, client_name, domains, forwarding_url=""):
    """Create a new pipeline.

    pipeline_type: 'new_setup' or 'replacement'
    domains: list of dicts from sheets.py with 'domain', 'provider', 'row_number'
    """
    steps = SETUP_STEPS if pipeline_type == "new_setup" else REPLACEMENT_STEPS

    pipeline = {
        "id": generate_pipeline_id(),
        "type": pipeline_type,
        "client_name": client_name,
        "forwarding_url": forwarding_url,
        "status": "running",
        "current_step": steps[0],
        "steps": steps,
        "domains": {},
        "old_domains": [],  # for replacement: domains being replaced
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "errors": [],
    }

    for d in domains:
        pipeline["domains"][d["domain"]] = {
            "provider": d.get("provider", ""),
            "row_number": d.get("row_number"),
            "step": steps[0],
            "step_status": "pending",
            "zapmail_domain_id": None,
            "mailbox_ids": [],
            "smartlead_account_ids": [],
            "error": None,
        }

    save_pipeline(pipeline)
    return pipeline


# --- Step Executors ---

def step_claim_domains(pipeline):
    """Mark domains as claimed in the Google Sheet."""
    domains_to_claim = []
    for domain_name, info in pipeline["domains"].items():
        if info.get("row_number"):
            domains_to_claim.append({
                "domain": domain_name,
                "row_number": info["row_number"],
            })

    if domains_to_claim:
        mark_domains_in_use_batch(domains_to_claim, pipeline["client_name"])

    _mark_all_domains_complete(pipeline, "claim_domains")
    return True


def step_set_dns(pipeline):
    """Set nameservers for each domain via registrar API."""
    all_ok = True
    for domain_name, info in pipeline["domains"].items():
        if info["step_status"] == "complete" and info["step"] == "set_dns":
            continue  # already done
        provider = info.get("provider", "spaceship")
        success, msg = set_nameservers_for_domain(domain_name, provider)
        if success:
            info["step"] = "set_dns"
            info["step_status"] = "complete"
        else:
            info["step_status"] = "error"
            info["error"] = f"DNS failed: {msg}"
            pipeline["errors"].append(f"{domain_name}: DNS failed — {msg}")
            all_ok = False
        time.sleep(1)
    return all_ok


def step_connect_zapmail(pipeline):
    """Connect domains to ZapMail and wait for them to become active."""
    domain_names = list(pipeline["domains"].keys())

    # Verify nameservers first
    zm_verify_nameservers(domain_names)
    time.sleep(5)

    # Connect
    result = zm_connect_domains(domain_names)
    if isinstance(result, dict) and result.get("_raw_status", 200) >= 400:
        for d in pipeline["domains"]:
            pipeline["domains"][d]["step_status"] = "error"
            pipeline["domains"][d]["error"] = f"Connect failed: {result}"
        pipeline["errors"].append(f"ZapMail connect failed: {result}")
        return False

    # Poll for domains to become active
    max_wait = ZAPMAIL_ACTIVATION_TIMEOUT_S
    waited = 0
    poll_interval = 30
    while waited < max_wait:
        time.sleep(poll_interval)
        waited += poll_interval
        all_active = True
        for domain_name, info in pipeline["domains"].items():
            if info.get("zapmail_domain_id"):
                continue  # already found
            found = zm_find_domain(domain_name)
            if found and found.get("status") == "ACTIVE":
                info["zapmail_domain_id"] = found["id"]
            else:
                all_active = False
        if all_active:
            break

    for domain_name, info in pipeline["domains"].items():
        if info.get("zapmail_domain_id"):
            info["step"] = "connect_zapmail"
            info["step_status"] = "complete"
        else:
            info["step_status"] = "error"
            info["error"] = "Domain did not become active in ZapMail within 30 min"
            pipeline["errors"].append(f"{domain_name}: ZapMail connect timeout")

    return all(i.get("zapmail_domain_id") for i in pipeline["domains"].values())


def step_create_mailboxes(pipeline):
    """Create 3 mailboxes per domain (s.reynolds, sean.r, sean.reynolds)."""
    all_ok = True
    for domain_name, info in pipeline["domains"].items():
        if info["step"] == "create_mailboxes" and info["step_status"] == "complete":
            continue
        domain_id = info.get("zapmail_domain_id")
        if not domain_id:
            info["step_status"] = "error"
            info["error"] = "No ZapMail domain ID"
            all_ok = False
            continue

        specs = generate_mailbox_specs(domain_name, count=MAILBOXES_PER_DOMAIN)
        result = zm_create_mailboxes(str(domain_id), domain_name, specs)

        # Check if we need to buy addon mailboxes first
        if isinstance(result, dict) and "error" in str(result).lower() and "mailbox" in str(result).lower():
            zm_buy_addon_mailboxes(3)
            time.sleep(2)
            result = zm_create_mailboxes(str(domain_id), domain_name, specs)

        # Verify mailboxes were created
        time.sleep(5)
        mailboxes = zm_list_mailboxes(domain_id=domain_id)
        if isinstance(mailboxes, dict):
            mb_list = mailboxes.get("data", [])
            if isinstance(mb_list, list) and len(mb_list) >= MAILBOXES_PER_DOMAIN:
                info["mailbox_ids"] = [m["id"] for m in mb_list]
                info["step"] = "create_mailboxes"
                info["step_status"] = "complete"
            else:
                # Retry failed
                zm_retry_failed_mailboxes()
                time.sleep(10)
                mailboxes = zm_list_mailboxes(domain_id=domain_id)
                mb_list = mailboxes.get("data", []) if isinstance(mailboxes, dict) else []
                if isinstance(mb_list, list) and len(mb_list) >= MAILBOXES_PER_DOMAIN:
                    info["mailbox_ids"] = [m["id"] for m in mb_list]
                    info["step"] = "create_mailboxes"
                    info["step_status"] = "complete"
                else:
                    info["step_status"] = "error"
                    info["error"] = f"Only {len(mb_list)} mailboxes created, expected 3"
                    all_ok = False
        else:
            info["step_status"] = "error"
            info["error"] = f"Mailbox list failed: {mailboxes}"
            all_ok = False

        time.sleep(2)

    return all_ok


def step_upload_photos(pipeline):
    """Upload profile photos to all mailboxes via ZapMail API."""
    all_ids = []
    for info in pipeline["domains"].values():
        all_ids.extend(info.get("mailbox_ids", []))

    if not all_ids:
        return False

    mailbox_data = [
        {"mailboxId": mid, "profilePicture": HEADSHOT_URL}
        for mid in all_ids
    ]
    result = zm_update_mailboxes(mailbox_data)

    # Mark complete regardless — photo upload is best-effort
    _mark_all_domains_complete(pipeline, "upload_photos")
    return True


def step_tag_and_configure(pipeline):
    """Assign client tag to domains and set forwarding."""
    client_name = pipeline["client_name"]

    # Find or create ZapMail tag
    existing_tags = zm_list_domain_tags()
    tag_list = existing_tags.get("data", []) if isinstance(existing_tags, dict) else []
    tag_id = None
    for t in tag_list:
        if t.get("name", "").lower() == client_name.lower():
            tag_id = t["id"]
            break

    if not tag_id:
        result = zm_create_domain_tag(client_name)
        if isinstance(result, dict) and "data" in result:
            created = result["data"]
            if isinstance(created, list) and created:
                tag_id = created[0].get("id")

    # Assign tag and forwarding to each domain
    domain_ids = [
        str(info["zapmail_domain_id"])
        for info in pipeline["domains"].values()
        if info.get("zapmail_domain_id")
    ]

    if tag_id and domain_ids:
        zm_assign_domain_tag(domain_ids, [str(tag_id)])

    if pipeline.get("forwarding_url") and domain_ids:
        zm_set_forwarding(domain_ids, pipeline["forwarding_url"])

    _mark_all_domains_complete(pipeline, "tag_and_configure")
    return True


def step_export_to_smartlead(pipeline):
    """Export mailboxes to SmartLead via ZapMail export."""
    all_mailbox_ids = []
    for info in pipeline["domains"].values():
        all_mailbox_ids.extend(info.get("mailbox_ids", []))

    if not all_mailbox_ids:
        return False

    result = zm_export_mailboxes(["SMARTLEAD"], mailbox_ids=all_mailbox_ids)

    # Poll export status
    max_wait = EXPORT_TIMEOUT_S
    waited = 0
    while waited < max_wait:
        time.sleep(30)
        waited += 30
        status = zm_get_export_status()
        if isinstance(status, dict):
            if status.get("data", {}).get("status") in ("COMPLETED", "completed", None):
                break

    # Verify accounts appeared in SmartLead — check by domain name
    time.sleep(30)
    all_sl_accounts = _fetch_all_smartlead_accounts()
    found_all = True
    for domain_name, info in pipeline["domains"].items():
        domain_accounts = [a for a in all_sl_accounts if domain_name in a.get("from_email", "")]

        if len(domain_accounts) >= MAILBOXES_PER_DOMAIN:
            info["smartlead_account_ids"] = [a["id"] for a in domain_accounts]
            info["step"] = "export_to_smartlead"
            info["step_status"] = "complete"
        else:
            # Re-export per lesson: if accounts don't appear in 10 min, re-export
            zm_export_mailboxes(["SMARTLEAD"], mailbox_ids=info.get("mailbox_ids", []))
            time.sleep(60)
            info["step_status"] = "retry"
            found_all = False

    return found_all


def step_enable_warmup(pipeline):
    """Enable warmup on all new SmartLead accounts and assign client tag."""
    client_name = pipeline["client_name"]

    # Find or create SmartLead tag
    existing_tags = sl_get_all_tags()
    tag_id = sl_find_or_create_tag(client_name, existing_tags=existing_tags)

    all_account_ids = []
    for info in pipeline["domains"].values():
        all_account_ids.extend(info.get("smartlead_account_ids", []))

    # Enable warmup on each account
    for acc_id in all_account_ids:
        sl_set_warmup(acc_id)
        time.sleep(1)

    # Tag accounts with client name
    if tag_id and all_account_ids:
        # Get client ID from SmartLead
        client_id = None
        r = requests.get(
            f"{SMARTLEAD_API}/client?api_key={SMARTLEAD_KEY}", timeout=30
        )
        if r.status_code == 200:
            for c in r.json():
                if c["name"].lower() == client_name.lower():
                    client_id = c["id"]
                    break
        sl_tag_accounts_bulk(all_account_ids, [tag_id], client_id=client_id)

    _mark_all_domains_complete(pipeline, "enable_warmup")
    pipeline["warmup_started_at"] = datetime.now().isoformat()
    return True


def step_wait_for_warmup(pipeline):
    """Check if all new accounts have reached reputation 99+.
    Returns True when all warmed, False if still warming.
    """
    all_warmed = True
    for domain_name, info in pipeline["domains"].items():
        for acc_id in info.get("smartlead_account_ids", []):
            r = requests.get(
                f"{SMARTLEAD_API}/email-accounts/{acc_id}/?api_key={SMARTLEAD_KEY}",
                timeout=30,
            )
            if r.status_code == 200:
                acc = r.json()
                wd = acc.get("warmup_details") or {}
                rep = wd.get("warmup_reputation", "?")
                try:
                    if float(rep) < WARMUP_TARGET_REPUTATION:
                        all_warmed = False
                except (ValueError, TypeError):
                    all_warmed = False

    if all_warmed:
        _mark_all_domains_complete(pipeline, "wait_for_warmup")

    return all_warmed


def step_check_campaigns(pipeline):
    """Check if old inboxes are in any active campaigns.
    If yes, set pipeline to 'awaiting_removal' (needs manual action from dashboard).
    If no old inboxes or none in campaigns, auto-proceed.
    """
    old_domains = pipeline.get("old_domains", [])
    if not old_domains:
        _mark_all_domains_complete(pipeline, "check_campaigns")
        return True

    # Get all campaigns
    r = requests.get(
        f"{SMARTLEAD_API}/campaign?api_key={SMARTLEAD_KEY}", timeout=30
    )
    campaigns = r.json() if r.status_code == 200 else []
    active_campaigns = [c for c in campaigns if c.get("status") == "ACTIVE"]

    # Check each old inbox against active campaigns
    inbox_campaigns = {}  # email -> [campaign_names]
    for old_domain in old_domains:
        for old_email in old_domain.get("emails", []):
            for camp in active_campaigns:
                camp_id = camp["id"]
                cr = requests.get(
                    f"{SMARTLEAD_API}/campaigns/{camp_id}/email-accounts?api_key={SMARTLEAD_KEY}",
                    timeout=30,
                )
                if cr.status_code == 200:
                    camp_accounts = cr.json() if isinstance(cr.json(), list) else []
                    for ca in camp_accounts:
                        if ca.get("from_email") == old_email:
                            if old_email not in inbox_campaigns:
                                inbox_campaigns[old_email] = []
                            inbox_campaigns[old_email].append({
                                "campaign_id": camp_id,
                                "campaign_name": camp["name"],
                            })

    if inbox_campaigns:
        pipeline["pending_removals"] = inbox_campaigns
        pipeline["status"] = "awaiting_removal"
        return False  # Needs manual action

    _mark_all_domains_complete(pipeline, "check_campaigns")
    return True


def step_remove_old(pipeline):
    """Remove old email accounts from SmartLead entirely."""
    old_domains = pipeline.get("old_domains", [])
    for old_domain in old_domains:
        for acc_id in old_domain.get("smartlead_account_ids", []):
            r = requests.delete(
                f"{SMARTLEAD_API}/email-accounts/{acc_id}?api_key={SMARTLEAD_KEY}",
                timeout=30,
            )
            if r.status_code not in (200, 204):
                log.warning("Failed to delete SmartLead account %s: %s", acc_id, r.text[:200])
            time.sleep(0.5)

    _mark_all_domains_complete(pipeline, "remove_old")
    return True


def step_cleanup(pipeline):
    """Schedule old ZapMail mailboxes for removal at renewal."""
    old_domains = pipeline.get("old_domains", [])
    for old_domain in old_domains:
        mailbox_ids = old_domain.get("mailbox_ids", [])
        if mailbox_ids:
            zm_remove_on_renewal(mailbox_ids)

    _mark_all_domains_complete(pipeline, "cleanup")
    pipeline["status"] = "complete"
    pipeline["completed_at"] = datetime.now().isoformat()
    return True


# Step executor map
STEP_EXECUTORS = {
    "claim_domains": step_claim_domains,
    "set_dns": step_set_dns,
    "connect_zapmail": step_connect_zapmail,
    "create_mailboxes": step_create_mailboxes,
    "upload_photos": step_upload_photos,
    "tag_and_configure": step_tag_and_configure,
    "export_to_smartlead": step_export_to_smartlead,
    "enable_warmup": step_enable_warmup,
    "wait_for_warmup": step_wait_for_warmup,
    "check_campaigns": step_check_campaigns,
    "remove_old": step_remove_old,
    "cleanup": step_cleanup,
}


def run_pipeline_steps(pipeline):
    """Execute pipeline steps from current position until blocked or complete.
    Acquires a per-pipeline lock to prevent concurrent execution.
    """
    lock = _get_pipeline_lock(pipeline["id"])
    if not lock.acquire(blocking=False):
        log.info("Pipeline %s already running, skipping", pipeline["id"])
        return

    try:
        steps = pipeline["steps"]
        current_idx = steps.index(pipeline["current_step"])

        for i in range(current_idx, len(steps)):
            step_name = steps[i]
            pipeline["current_step"] = step_name
            pipeline["updated_at"] = datetime.now().isoformat()
            save_pipeline(pipeline)

            executor = STEP_EXECUTORS[step_name]
            success = executor(pipeline)
            save_pipeline(pipeline)

            if not success:
                if pipeline["status"] == "awaiting_removal":
                    return  # Paused for manual action
                if step_name == "wait_for_warmup":
                    return  # Will be re-checked by monitor
                # Error — stop
                pipeline["status"] = "error"
                save_pipeline(pipeline)
                return

        # All steps complete
        if pipeline["status"] != "complete":
            pipeline["status"] = "complete"
            pipeline["completed_at"] = datetime.now().isoformat()
            save_pipeline(pipeline)
    finally:
        lock.release()


# --- Background Monitor Thread ---

def get_flagged_domains_for_client(client_accounts):
    """Check warmup reputation for all accounts, return flagged domains.
    A domain is flagged if ANY inbox has warmup reputation < 99.
    """
    flagged = {}  # domain -> {emails, smartlead_account_ids}
    for acc in client_accounts:
        wd = acc.get("warmup_details") or {}
        rep = wd.get("warmup_reputation", "?")
        email = acc.get("from_email", "")
        domain = email.split("@")[-1] if "@" in email else ""
        try:
            if float(rep) < WARMUP_TARGET_REPUTATION:
                if domain not in flagged:
                    flagged[domain] = {"emails": [], "smartlead_account_ids": []}
                flagged[domain]["emails"].append(email)
                flagged[domain]["smartlead_account_ids"].append(acc["id"])
        except (ValueError, TypeError):
            pass
    return flagged


def _run_monitor_check():
    """Single monitor check iteration."""
    all_accounts = _fetch_all_smartlead_accounts()

    # Get clients
    r = requests.get(f"{SMARTLEAD_API}/client?api_key={SMARTLEAD_KEY}", timeout=30)
    clients = r.json() if r.status_code == 200 else []

    # Check active pipelines to avoid duplicates
    active_pipelines = load_all_pipelines()
    domains_in_pipeline = set()
    for p in active_pipelines:
        if p["status"] in ("running", "awaiting_removal"):
            domains_in_pipeline.update(p["domains"].keys())
            for old in p.get("old_domains", []):
                domains_in_pipeline.add(old.get("domain", ""))

    for client in clients:
        cl_accounts = [a for a in all_accounts if a.get("client_id") == client["id"]]
        if not cl_accounts:
            continue

        flagged = get_flagged_domains_for_client(cl_accounts)

        for domain, domain_info in flagged.items():
            if domain in domains_in_pipeline:
                continue  # already being replaced

            # Check domain inventory
            available = get_available_domains()
            if len(available) < 1:
                log.info("[MONITOR] No domains available for replacement of {domain}")
                continue

            # Pick oldest domain from inventory
            available.sort(key=lambda d: d.get("purchase_date", "9999"))
            replacement_domain = available[0]

            # Create replacement pipeline
            pipeline = create_pipeline(
                "replacement",
                client["name"],
                [replacement_domain],
                forwarding_url="",
            )
            pipeline["old_domains"] = [{
                "domain": domain,
                "emails": domain_info["emails"],
                "smartlead_account_ids": domain_info["smartlead_account_ids"],
            }]
            save_pipeline(pipeline)

            # Run setup steps in background thread
            threading.Thread(
                target=run_pipeline_steps,
                args=(pipeline,),
                daemon=True,
            ).start()

    # Also check in-progress replacement pipelines waiting for warmup
    for p in active_pipelines:
        if p["status"] == "running" and p.get("current_step") == "wait_for_warmup":
            if step_wait_for_warmup(p):
                # Warmup complete — continue pipeline
                next_idx = p["steps"].index("wait_for_warmup") + 1
                if next_idx < len(p["steps"]):
                    p["current_step"] = p["steps"][next_idx]
                    save_pipeline(p)
                    threading.Thread(
                        target=run_pipeline_steps,
                        args=(p,),
                        daemon=True,
                    ).start()


def _run_weekly_placement_tests():
    """Run placement tests for a sample of inboxes per client. Called weekly."""
    from zapmail_ops import (
        zm_get_placement_eligible_mailboxes,
        zm_get_placement_credits,
        zm_run_placement_test,
    )

    state_file = PIPELINES_DIR / "last_placement_test.json"

    # Check if we already ran this week
    if state_file.exists():
        last = json.loads(state_file.read_text())
        last_run = datetime.fromisoformat(last.get("last_run", "2000-01-01"))
        if (datetime.now() - last_run).days < 7:
            return

    # Get eligible mailboxes
    eligible = zm_get_placement_eligible_mailboxes()
    if not isinstance(eligible, dict):
        return

    mailbox_list = eligible.get("data", [])
    if not mailbox_list:
        return

    # Check credits
    credits = zm_get_placement_credits()
    available_credits = 0
    if isinstance(credits, dict):
        available_credits = credits.get("data", {}).get("available", 0)

    if available_credits < 1:
        print("[PLACEMENT] No credits available for placement tests")
        return

    # Sample: test up to 10 mailboxes (or available credits, whichever is less)
    sample_size = min(10, available_credits, len(mailbox_list))
    sample_ids = [m["id"] for m in random.sample(mailbox_list, sample_size)]

    result = zm_run_placement_test(sample_ids)

    # Save last run timestamp
    state_file.write_text(json.dumps({
        "last_run": datetime.now().isoformat(),
        "mailboxes_tested": sample_ids,
        "result": str(result)[:500],
    }))
    log.info("[PLACEMENT] Ran placement tests for {sample_size} mailboxes")


def monitor_loop(check_interval_hours=4):
    """Background loop: check reputation every N hours, trigger replacements."""
    while True:
        try:
            _run_monitor_check()
        except Exception as e:
            log.info("[MONITOR] Error in reputation check: {e}")

        try:
            _run_weekly_placement_tests()
        except Exception as e:
            log.info("[MONITOR] Error in placement tests: {e}")

        time.sleep(check_interval_hours * 3600)


def start_monitor_thread():
    """Start the background monitor as a daemon thread."""
    t = threading.Thread(target=monitor_loop, daemon=True, name="infra-monitor")
    t.start()
    return t
