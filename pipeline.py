"""Pipeline engine for autonomous infrastructure management.

Handles new client setup and domain replacement as state machines.
Each pipeline is persisted to Supabase for crash recovery.
"""

import json
import logging
import os
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
    sl_update_account,
    sl_verify_warmup,
    sl_dedup_check,
    export_for_sheet,
    SMARTLEAD_API,
    SMARTLEAD_KEY,
    SMARTLEAD_JWT,
    GCAL_CALENDAR_ID,
    GCAL_ROTATION_WEEKS,
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    GOOGLE_REFRESH_TOKEN,
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
from sheets import get_available_domains, mark_domains_in_use_batch, setup_client_tab
import db as store

import requests

log = logging.getLogger("pipeline")

SCRIPT_DIR = Path(__file__).parent

# Thread safety: one lock per pipeline ID to prevent concurrent step execution
_pipeline_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()

WARMUP_TARGET_REPUTATION = 98
MAILBOXES_PER_DOMAIN = 3
ZAPMAIL_ACTIVATION_TIMEOUT_S = 30 * 60
EXPORT_TIMEOUT_S = 15 * 60

# Retry policies: {step_name: (max_attempts, [wait_seconds_between_attempts])}
RETRY_POLICIES = {
    "connect_zapmail":      (3, [0, 1800, 1800]),    # 30 min poll window each
    "create_mailboxes":     (3, [0, 300, 600]),       # 5 min, then 10 min
    "export_to_smartlead":  (3, [0, 900, 900]),       # 15 min poll window each
    "enable_warmup":        (3, [0, 120, 300]),       # 2 min, then 5 min
}
DEFAULT_RETRY_POLICY = (2, [0, 120])                  # 2 attempts, 2 min wait


def notify_pipeline_error(pipeline):
    """Stub — replaced in Task 3 with Slack notification."""
    log.warning("[PIPELINE] Error in pipeline %s at step %s", pipeline.get("id"), pipeline.get("current_step"))


# Profile photo URLs (hosted on Supabase Storage — permanent, publicly accessible)
_SUPABASE_STORAGE = os.environ.get("SUPABASE_URL", "https://ghjmqpnqljgwykpjkvzy.supabase.co") + "/storage/v1/object/public/headshots"
HEADSHOT_URL = f"{_SUPABASE_STORAGE}/sean_reynolds.png"
ACQUISITION_HEADSHOT_URL = f"{_SUPABASE_STORAGE}/aidan_hutchinson.png"

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
    "smartlead_tags",
    "export_csv",
    "gcal_rotation",
]

REPLACEMENT_STEPS = SETUP_STEPS[:-2] + [
    "wait_for_warmup",
    "check_campaigns",
    "remove_old",
    "cleanup",
    "export_csv",
    "gcal_rotation",
]


def _get_pipeline_lock(pipeline_id):
    """Get or create a lock for a specific pipeline."""
    with _locks_lock:
        if pipeline_id not in _pipeline_locks:
            _pipeline_locks[pipeline_id] = threading.Lock()
        return _pipeline_locks[pipeline_id]


def _mark_all_domains_complete(pipeline, step_name):
    """Mark pending/error domains as complete for a given step.
    Skips domains already marked complete for this step (preserves retry state).
    """
    for info in pipeline["domains"].values():
        if info["step"] == step_name and info["step_status"] == "complete":
            continue
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
    """Persist pipeline state to Supabase."""
    store.save_pipeline(pipeline)


def load_pipeline(pipeline_id):
    """Load a pipeline from Supabase."""
    return store.load_pipeline(pipeline_id)


def load_all_pipelines():
    """Load all pipeline records from Supabase."""
    return store.load_all_pipelines()


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
            "attempt": 1,
            "max_attempts": 3,
            "step_history": [],
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

    # Update the client tab in the Google Sheet
    purchased = [
        {"domain": d, "provider": info.get("provider", "")}
        for d, info in pipeline["domains"].items()
    ]
    setup_client_tab(pipeline["client_name"], purchased)

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
    """Enable warmup on all new SmartLead accounts with verification."""
    all_account_ids = []
    for info in pipeline["domains"].values():
        all_account_ids.extend(info.get("smartlead_account_ids", []))

    # Enable warmup + set time_to_wait on each account
    for acc_id in all_account_ids:
        sl_set_warmup(acc_id)
        sl_update_account(acc_id, {"time_to_wait_in_mins": 5})
        time.sleep(1)

    # Verify warmup settings on all accounts
    bad_accounts = []
    for acc_id in all_account_ids:
        sl_update_account(acc_id, {"time_to_wait_in_mins": 5})
        ok, issues = sl_verify_warmup(acc_id)
        if not ok:
            log.warning("Warmup wrong on %s: %s — re-applying", acc_id, issues)
            sl_set_warmup(acc_id)
            time.sleep(1)
            ok2, issues2 = sl_verify_warmup(acc_id)
            if not ok2:
                bad_accounts.append(str(acc_id))
                log.warning("Still wrong after re-apply: %s — %s", acc_id, issues2)
        time.sleep(0.3)

    if bad_accounts:
        pipeline["errors"].append(f"Warmup verify failed: {', '.join(bad_accounts)}")
        return False

    _mark_all_domains_complete(pipeline, "enable_warmup")
    pipeline["warmup_started_at"] = datetime.now().isoformat()
    return True


def step_smartlead_tags(pipeline):
    """Tag all SmartLead accounts with 3 tags (client, Zapmail, date) + assign client."""
    client_name = pipeline["client_name"]

    # Dedup guard
    sl_dedup_check()

    existing_tags = sl_get_all_tags()

    # Date tag in M/D/YY format
    warmup_date = datetime.fromisoformat(pipeline.get("warmup_started_at", datetime.now().isoformat()))
    date_tag_name = f"{warmup_date.month}/{warmup_date.day}/{warmup_date.strftime('%y')}"

    # Find or create the 3 tags: client name, "Zapmail", date
    tag_ids = []
    for tag_name, color in [(client_name, "#B1C4FC"), ("Zapmail", "#B1FCB3"), (date_tag_name, "#D0FCB1")]:
        tag_id = sl_find_or_create_tag(tag_name, color, existing_tags)
        if tag_id:
            tag_ids.append(tag_id)

    # Find or create SmartLead client
    sl_client_id = None
    try:
        r = requests.get(f"{SMARTLEAD_API}/client?api_key={SMARTLEAD_KEY}", timeout=30)
        if r.status_code == 200:
            client_lower = client_name.lower().strip()
            for c in r.json():
                cn = c["name"].lower().strip()
                if cn == client_lower or client_lower in cn or cn in client_lower:
                    sl_client_id = c["id"]
                    break
            if not sl_client_id:
                slug = client_name.lower().replace("'", "").replace(" ", "").replace("&", "")
                cl_email = f"tht.{slug}.client@gmail.com"
                cr = requests.post(
                    f"{SMARTLEAD_API}/client/save?api_key={SMARTLEAD_KEY}",
                    json={"name": client_name, "email": cl_email, "password": "THTclient2026!"},
                    timeout=30,
                )
                if cr.status_code == 201:
                    sl_client_id = cr.json().get("clientId")
    except Exception as e:
        log.warning("SmartLead client lookup failed: %s", e)

    # Collect all account IDs
    all_account_ids = []
    for info in pipeline["domains"].values():
        all_account_ids.extend(info.get("smartlead_account_ids", []))

    if tag_ids and all_account_ids:
        sl_tag_accounts_bulk(all_account_ids, tag_ids, client_id=sl_client_id)

    _mark_all_domains_complete(pipeline, "smartlead_tags")
    return True


def step_export_csv(pipeline):
    """Export CSV summary for Google Sheet."""
    client_name = pipeline["client_name"]
    warmup_start = pipeline.get("warmup_started_at", datetime.now().isoformat())

    # Build a config dict that export_for_sheet expects
    purchased_domains = []
    for domain_name, info in pipeline["domains"].items():
        specs = [
            {"firstName": "Sean", "lastName": "Reynolds", "mailboxUsername": "s.reynolds"},
            {"firstName": "Sean", "lastName": "Reynolds", "mailboxUsername": "sean.r"},
            {"firstName": "Sean", "lastName": "Reynolds", "mailboxUsername": "sean.reynolds"},
        ]
        emails = [f"{s['mailboxUsername']}@{domain_name}" for s in specs]
        purchased_domains.append({
            "domain": domain_name,
            "inboxes": specs,
            "inbox_emails": emails,
        })

    config = {
        "client_name": client_name,
        "purchased_domains": purchased_domains,
        "infrastructure": {
            "warmup_start_date": warmup_start[:10],
            "estimated_launch_date": (
                datetime.fromisoformat(warmup_start) + timedelta(days=14)
            ).strftime("%Y-%m-%d"),
        },
    }

    csv_path, rows = export_for_sheet(config)
    pipeline["export_csv_path"] = str(csv_path)

    # Save client config for warmup tracking
    existing = store.load_client_config(client_name)
    if existing:
        existing.setdefault("purchased_domains", []).extend(purchased_domains)
        existing_ws = existing.get("infrastructure", {}).get("warmup_start_date", "9999")
        new_ws = config["infrastructure"]["warmup_start_date"]
        if new_ws < existing_ws:
            existing["infrastructure"]["warmup_start_date"] = new_ws
        config = existing
    store.save_client_config(client_name, config)

    _mark_all_domains_complete(pipeline, "export_csv")
    return True


def step_gcal_rotation(pipeline):
    """Schedule infrastructure rotation reminder in Google Calendar."""
    client_name = pipeline["client_name"]
    warmup_start = datetime.fromisoformat(
        pipeline.get("warmup_started_at", datetime.now().isoformat())
    )
    rotation_date = warmup_start + timedelta(weeks=GCAL_ROTATION_WEEKS)
    rotation_str = rotation_date.strftime("%Y-%m-%d")
    rotation_end_str = (rotation_date + timedelta(days=1)).strftime("%Y-%m-%d")

    event_title = f"{client_name} — Cancel old inboxes and set up new ones"
    domain_count = len(pipeline["domains"])
    account_count = domain_count * MAILBOXES_PER_DOMAIN

    if not all([GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN]):
        log.warning("Google Calendar OAuth not configured — skipping rotation event")
        _mark_all_domains_complete(pipeline, "gcal_rotation")
        return True

    try:
        # Exchange refresh token for access token
        token_resp = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "refresh_token": GOOGLE_REFRESH_TOKEN,
                "grant_type": "refresh_token",
            },
            timeout=15,
        )
        if token_resp.status_code != 200:
            log.warning("Google token refresh failed: %s", token_resp.text[:200])
            _mark_all_domains_complete(pipeline, "gcal_rotation")
            pipeline["status"] = "complete"
            pipeline["completed_at"] = datetime.now().isoformat()
            return True

        access_token = token_resp.json()["access_token"]
        gcal_url = f"https://www.googleapis.com/calendar/v3/calendars/{GCAL_CALENDAR_ID}/events"
        gcal_payload = {
            "summary": event_title,
            "description": (
                f"Client: {client_name}\n"
                f"Domains: {domain_count}\n"
                f"Accounts: {account_count}\n"
                f"Warmup started: {warmup_start.strftime('%Y-%m-%d')}\n"
                f"Pipeline: {pipeline['id']}\n\n"
                f"Action: Cancel the current Zapmail inboxes for this client "
                f"and run a fresh infrastructure setup."
            ),
            "start": {"date": rotation_str},
            "end": {"date": rotation_end_str},
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": 1440},
                    {"method": "popup", "minutes": 0},
                ],
            },
        }
        resp = requests.post(
            gcal_url,
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json=gcal_payload,
            timeout=30,
        )
        if resp.status_code in (200, 201):
            event_data = resp.json()
            pipeline["gcal_rotation_event"] = {
                "event_id": event_data.get("id"),
                "date": rotation_str,
                "link": event_data.get("htmlLink"),
            }
        else:
            log.warning("Calendar API error (%s): %s", resp.status_code, resp.text[:300])
    except Exception as e:
        log.warning("Calendar event failed: %s", e)

    _mark_all_domains_complete(pipeline, "gcal_rotation")
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
                rep = str(wd.get("warmup_reputation", "?")).replace("%", "").strip()
                try:
                    if float(rep) < WARMUP_TARGET_REPUTATION:
                        all_warmed = False
                except (ValueError, TypeError):
                    all_warmed = False

    if all_warmed:
        _mark_all_domains_complete(pipeline, "wait_for_warmup")

    return all_warmed


def step_check_campaigns(pipeline):
    """Swap new inboxes into old inboxes' campaigns, then remove old inboxes.

    1. Find which campaigns the old inboxes are in
    2. Add new inboxes to those same campaigns
    3. Remove old inboxes from campaigns
    """
    old_domains = pipeline.get("old_domains", [])
    if not old_domains:
        _mark_all_domains_complete(pipeline, "check_campaigns")
        return True

    # Collect old account IDs
    old_account_ids = set()
    for od in old_domains:
        old_account_ids.update(od.get("smartlead_account_ids", []))

    # Collect new account IDs from the pipeline's new domains
    new_account_ids = []
    for domain_name, info in pipeline["domains"].items():
        new_account_ids.extend(info.get("smartlead_account_ids", []))

    # Get client's campaigns
    client_name = pipeline.get("client_name", "")
    r = requests.get(f"{SMARTLEAD_API}/campaigns?api_key={SMARTLEAD_KEY}", timeout=30)
    all_campaigns = r.json() if r.status_code == 200 else []
    active_campaigns = [c for c in all_campaigns if c.get("status") in ("ACTIVE", "PAUSED")]

    # Find which campaigns contain old inboxes
    campaigns_to_update = []
    for camp in active_campaigns:
        cr = requests.get(
            f"{SMARTLEAD_API}/campaigns/{camp['id']}/email-accounts?api_key={SMARTLEAD_KEY}",
            timeout=30,
        )
        if cr.status_code != 200:
            continue
        camp_accounts = cr.json() if isinstance(cr.json(), list) else []
        camp_account_ids = {ca["id"] for ca in camp_accounts}
        if camp_account_ids & old_account_ids:
            campaigns_to_update.append(camp["id"])
        time.sleep(0.2)

    log.info(f"[SWAP] Found {len(campaigns_to_update)} campaigns to update for {client_name}")

    # Add new inboxes to those campaigns
    for camp_id in campaigns_to_update:
        r = requests.post(
            f"{SMARTLEAD_API}/campaigns/{camp_id}/email-accounts?api_key={SMARTLEAD_KEY}",
            json={"email_account_ids": new_account_ids},
            timeout=30,
        )
        log.info(f"[SWAP] Added new accounts to campaign {camp_id}: {r.status_code}")
        time.sleep(0.3)

    # Remove old inboxes from those campaigns
    for camp_id in campaigns_to_update:
        for old_id in old_account_ids:
            r = requests.delete(
                f"{SMARTLEAD_API}/campaigns/{camp_id}/email-accounts/{old_id}?api_key={SMARTLEAD_KEY}",
                timeout=30,
            )
            log.info(f"[SWAP] Removed old account {old_id} from campaign {camp_id}: {r.status_code}")
            time.sleep(0.3)

    pipeline["campaigns_updated"] = campaigns_to_update
    _mark_all_domains_complete(pipeline, "check_campaigns")
    return True


def step_remove_old(pipeline):
    """Schedule old ZapMail mailboxes for removal at renewal and record renewal dates.

    Does NOT delete SmartLead accounts yet — that happens automatically
    when the renewal date passes (handled by the monitor cleanup job).
    """
    old_domains = pipeline.get("old_domains", [])

    # Get subscription data to find renewal dates
    from zapmail_ops import zm_get_subscriptions, zm_get_subscription_mailboxes
    subs = zm_get_subscriptions()
    sub_list = subs.get("data", []) if isinstance(subs, dict) else []

    for old_domain in old_domains:
        mailbox_ids = old_domain.get("mailbox_ids", [])
        if mailbox_ids:
            zm_remove_on_renewal(mailbox_ids)
            log.info(f"[CLEANUP] Scheduled removal at renewal for {old_domain.get('domain')}")

        # Find renewal date for this domain's subscription
        domain_name = old_domain.get("domain", "")
        for sub in sub_list:
            sub_domain = sub.get("domain", {}).get("name", "")
            if sub_domain == domain_name:
                old_domain["renewal_date"] = sub.get("nextRenewalDate", "")
                old_domain["subscription_id"] = sub.get("id")
                break

    # Save pending deletions to Supabase
    for old_domain in old_domains:
        store.add_pending_deletion({
            "domain": old_domain.get("domain", ""),
            "smartlead_account_ids": old_domain.get("smartlead_account_ids", []),
            "renewal_date": old_domain.get("renewal_date", ""),
            "client_name": pipeline.get("client_name", ""),
            "pipeline_id": pipeline["id"],
            "scheduled_at": datetime.now().isoformat(),
        })
    _mark_all_domains_complete(pipeline, "remove_old")
    return True


def step_cleanup(pipeline):
    """Final cleanup — mark pipeline complete."""
    pipeline["completed_at"] = datetime.now().isoformat()
    _mark_all_domains_complete(pipeline, "cleanup")
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
    "smartlead_tags": step_smartlead_tags,
    "export_csv": step_export_csv,
    "gcal_rotation": step_gcal_rotation,
    "wait_for_warmup": step_wait_for_warmup,
    "check_campaigns": step_check_campaigns,
    "remove_old": step_remove_old,
    "cleanup": step_cleanup,
}


def _run_step_with_retry(pipeline, step_name):
    """Execute a step with automatic retry and backoff.

    Returns True if all domains passed, False if any domain still errored
    after exhausting all retry attempts.
    """
    policy = RETRY_POLICIES.get(step_name, DEFAULT_RETRY_POLICY)
    max_attempts, waits = policy

    executor = STEP_EXECUTORS[step_name]

    for attempt_num in range(1, max_attempts + 1):
        # Update attempt info on pending/error domains
        for info in pipeline["domains"].values():
            if info["step_status"] in ("pending", "error", "retry"):
                info["attempt"] = attempt_num
                info["max_attempts"] = max_attempts

        # Update pipeline retry info for frontend display
        pipeline["retry_info"] = {
            "step": step_name,
            "attempt": attempt_num,
            "max_attempts": max_attempts,
        }
        pipeline["updated_at"] = datetime.now().isoformat()
        save_pipeline(pipeline)

        # Run the step executor
        success = executor(pipeline)
        save_pipeline(pipeline)

        if success:
            pipeline.pop("retry_info", None)
            return True

        # Check if any domains actually errored (vs waiting states)
        errored = [
            d for d, info in pipeline["domains"].items()
            if info.get("step_status") in ("error", "retry")
        ]
        if not errored:
            # Step returned False but no domain errors — might be a wait state
            pipeline.pop("retry_info", None)
            return False

        # If we have more attempts, log the failure and wait
        if attempt_num < max_attempts:
            wait_secs = waits[attempt_num] if attempt_num < len(waits) else waits[-1]
            log.info(
                "[RETRY] %s attempt %d/%d failed for %d domains, waiting %ds",
                step_name, attempt_num, max_attempts, len(errored), wait_secs,
            )

            # Record attempt in step_history
            for d_name in errored:
                info = pipeline["domains"][d_name]
                info["step_history"].append({
                    "attempt": attempt_num,
                    "result": "error",
                    "message": info.get("error", ""),
                    "at": datetime.now().isoformat(),
                })
                # Reset for next attempt
                info["step_status"] = "pending"
                info["error"] = None

            save_pipeline(pipeline)
            time.sleep(wait_secs)
        else:
            # Final attempt exhausted — record in history
            for d_name in errored:
                info = pipeline["domains"][d_name]
                info["step_history"].append({
                    "attempt": attempt_num,
                    "result": "error",
                    "message": info.get("error", ""),
                    "at": datetime.now().isoformat(),
                })

    pipeline.pop("retry_info", None)
    return False


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

            success = _run_step_with_retry(pipeline, step_name)
            save_pipeline(pipeline)

            if not success:
                if pipeline["status"] == "awaiting_removal":
                    return
                if step_name == "wait_for_warmup":
                    return
                # All retries exhausted — error
                pipeline["status"] = "error"
                save_pipeline(pipeline)
                notify_pipeline_error(pipeline)
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
    A domain is flagged if ANY inbox has warmup reputation < 98%.
    """
    flagged = {}  # domain -> {emails, smartlead_account_ids}
    for acc in client_accounts:
        wd = acc.get("warmup_details") or {}
        rep = str(wd.get("warmup_reputation", "?")).replace("%", "").strip()
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

    # Load paused clients — skip auto-replacement for these
    try:
        paused_state = store.get_state("paused_clients") or {"clients": []}
        paused_clients = set(paused_state.get("clients", []))
    except Exception:
        paused_clients = set()

    for client in clients:
        if client["name"] in paused_clients:
            log.info("[MONITOR] Skipping paused client: %s", client["name"])
            continue

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

            # Look up Zapmail mailbox IDs for the old domain
            old_mailbox_ids = []
            try:
                subs = zm_get_subscriptions()
                for sub in (subs.get("data", []) if isinstance(subs, dict) else []):
                    if sub.get("domain", {}).get("name") == domain:
                        sub_mailboxes = zm_get_subscription_mailboxes(sub["id"])
                        mb_list = sub_mailboxes.get("data", []) if isinstance(sub_mailboxes, dict) else []
                        old_mailbox_ids = [m["id"] for m in mb_list]
                        break
            except Exception as e:
                log.info(f"[MONITOR] Could not fetch mailbox IDs for {domain}: {e}")

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
                "mailbox_ids": old_mailbox_ids,
            }]
            save_pipeline(pipeline)

            store.log_monitor_event("replacement_triggered", {
                "client": client["name"],
                "flagged_domain": domain,
                "replacement_domain": replacement_domain.get("domain", ""),
                "pipeline_id": pipeline["id"],
            })

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

    # Check if we already ran this week
    last = store.get_state("last_placement_test")
    if last:
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
    store.set_state("last_placement_test", {
        "last_run": datetime.now().isoformat(),
        "mailboxes_tested": sample_ids,
        "result": str(result)[:500],
    })
    log.info("[PLACEMENT] Ran placement tests for {sample_size} mailboxes")


def _run_pending_deletions():
    """Delete SmartLead accounts whose Zapmail renewal date has passed."""
    pending = store.get_pending_deletions()
    if not pending:
        return

    now = datetime.now()

    for entry in pending:
        renewal = entry.get("renewal_date", "")
        if not renewal:
            continue

        try:
            renewal_dt = datetime.fromisoformat(renewal.replace("Z", "+00:00").replace("+00:00", ""))
        except Exception:
            try:
                renewal_dt = datetime.strptime(renewal[:10], "%Y-%m-%d")
            except Exception:
                continue

        if now < renewal_dt:
            continue

        # Renewal has passed — delete from SmartLead
        domain = entry.get("domain", "")
        account_ids = entry.get("smartlead_account_ids", [])
        if isinstance(account_ids, str):
            account_ids = json.loads(account_ids)

        for acc_id in account_ids:
            r = requests.delete(
                f"{SMARTLEAD_API}/email-accounts/{acc_id}?api_key={SMARTLEAD_KEY}",
                timeout=30,
            )
            status = "ok" if r.status_code in (200, 204) else f"fail({r.status_code})"
            log.info(f"[CLEANUP] Deleted SmartLead account {acc_id} ({domain}): {status}")
            time.sleep(0.5)

        store.remove_pending_deletion(domain)
        store.log_monitor_event("deletion_completed", {
            "domain": domain,
            "client_name": entry.get("client_name", ""),
            "account_ids": account_ids,
        })
        log.info(f"[CLEANUP] Finished deleting {domain} for {entry.get('client_name')}")


def monitor_loop(check_interval_hours=4):
    """Background loop: check reputation every N hours, trigger replacements."""
    while True:
        try:
            _run_monitor_check()
        except Exception as e:
            log.info(f"[MONITOR] Error in reputation check: {e}")

        try:
            _run_pending_deletions()
        except Exception as e:
            log.info(f"[MONITOR] Error in pending deletions: {e}")

        try:
            _run_weekly_placement_tests()
        except Exception as e:
            log.info(f"[MONITOR] Error in placement tests: {e}")

        time.sleep(check_interval_hours * 3600)


def start_monitor_thread():
    """Start the background monitor as a daemon thread."""
    t = threading.Thread(target=monitor_loop, daemon=True, name="infra-monitor")
    t.start()
    return t
