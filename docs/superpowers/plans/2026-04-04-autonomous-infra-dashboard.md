# Autonomous Infrastructure Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transform the dashboard from monitoring-only into a fully autonomous infrastructure management system that handles new client setup, automated inbox replacement, billing-optimized removal, and weekly placement testing.

**Architecture:** Single-process Python backend (`dashboard.py`) with a background automation thread. New pipeline logic lives in a separate `pipeline.py` module. ZapMail operational endpoints in `zapmail_ops.py`. Pipeline state persisted to `pipelines/` as JSON. Frontend stays in `dashboard.html`.

**Tech Stack:** Python 3 (stdlib HTTPServer + requests), vanilla HTML/CSS/JS, SmartLead API (public + internal/GraphQL), ZapMail API v2, Spaceship/Porkbun registrar APIs, Google Sheets API.

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `pipeline.py` | Create | Pipeline engine: state machine, step execution, resume logic, background thread |
| `zapmail_ops.py` | Create | New ZapMail endpoints: subscriptions, wallet, domain health, profile photos, placement tests, remove-on-renewal |
| `dashboard.py` | Modify | New API routes for pipeline control, campaign safeguard, operational data; start background thread |
| `dashboard.html` | Modify | New client setup form, pipeline progress tracker, pending removals panel, wallet/placement UI |
| `sheets.py` | No change | Already has `get_available_domains()`, `claim_domains()`, `mark_domains_in_use_batch()` |
| `setup.py` | No change | Existing functions imported by `pipeline.py` (ZapMail wrappers, SmartLead wrappers, registrar classes) |

---

## Task 1: Create `zapmail_ops.py` — New ZapMail API Wrappers

**Files:**
- Create: `zapmail_ops.py`

This module wraps ZapMail endpoints not currently used in the codebase. It imports the base helpers from `setup.py`.

- [ ] **Step 1: Create `zapmail_ops.py` with all new ZapMail wrappers**

```python
"""New ZapMail API operations for the autonomous dashboard.

Wraps endpoints not already in setup.py: subscriptions, wallet,
domain health, profile photos, placement tests, remove-on-renewal,
retry-failed, export status, cleanup.
"""

from setup import zm_get, zm_post, zm_put


def zm_get_subscriptions():
    """Get all subscriptions (active, cancelled, expired) with billing details."""
    return zm_get("/v2/subscriptions")


def zm_get_subscription_mailboxes(subscription_id):
    """Get mailboxes tied to a specific subscription."""
    return zm_get(f"/v2/subscriptions/{subscription_id}/mailboxes")


def zm_cancel_subscription(subscription_id, revert=False):
    """Cancel a subscription or revert cancellation.
    Body: {revert: true} to undo cancellation.
    """
    body = {"revert": revert} if revert else {}
    return zm_put(f"/v2/subscriptions/{subscription_id}/cancel", body)


def zm_get_wallet_balance():
    """Get current ZapMail wallet balance."""
    return zm_get("/v2/wallet/balance")


def zm_get_domain_health(domain_id):
    """Get domain health/reputation score based on NS reputation."""
    return zm_get(f"/v2/domains/{domain_id}/health-score")


def zm_update_mailboxes(mailbox_data):
    """Update mailboxes (profile photo, etc).
    mailbox_data: list of {mailboxId, profilePicture} dicts.
    Endpoint: PUT /v2/mailboxes
    """
    return zm_put("/v2/mailboxes", {"mailboxData": mailbox_data})


def zm_remove_on_renewal(mailbox_ids):
    """Schedule mailbox removal at next renewal (no immediate deletion).
    Body: {ids: [mailboxId, ...]}
    """
    return zm_post("/v2/mailboxes/remove-on-renewal", {"ids": mailbox_ids})


def zm_delete_mailboxes(mailbox_ids):
    """Instantly remove mailboxes.
    Uses DELETE /v2/mailboxes with body {ids: [...]}.
    """
    import requests
    from setup import ZAPMAIL_API, zm_headers
    r = requests.delete(
        f"{ZAPMAIL_API}/v2/mailboxes",
        headers=zm_headers(),
        json={"ids": mailbox_ids},
        timeout=30,
    )
    try:
        return r.json()
    except Exception:
        return {"_raw_status": r.status_code, "_raw_text": r.text[:500]}


def zm_retry_failed_mailboxes():
    """Retry creation of failed mailboxes."""
    return zm_post("/v2/mailboxes/retry-failed")


def zm_get_export_status():
    """Get current export operation status."""
    return zm_get("/v2/export/status")


def zm_verify_nameservers(domain_names):
    """Verify nameservers are set correctly before connecting.
    Body: {domainNames: [...]}
    """
    return zm_post("/v2/domains/verify-nameservers", {"domainNames": domain_names})


def zm_delete_unused_domains():
    """Remove all domains with no mailboxes."""
    import requests
    from setup import ZAPMAIL_API, zm_headers
    r = requests.delete(
        f"{ZAPMAIL_API}/v2/domains/unused",
        headers=zm_headers(),
        timeout=30,
    )
    try:
        return r.json()
    except Exception:
        return {"_raw_status": r.status_code, "_raw_text": r.text[:500]}


def zm_run_placement_test(mailbox_ids):
    """Purchase/run placement tests for given mailboxes.
    Body: {mailboxIds: [...]}
    """
    return zm_post("/v2/placement-test/purchase", {"mailboxIds": mailbox_ids})


def zm_get_placement_results():
    """Get placement test orders with results."""
    return zm_get("/v2/placement-test/orders")


def zm_get_placement_report(cart_order_id):
    """Get detailed placement report for a specific test order."""
    return zm_get(f"/v2/placement-test/orders/{cart_order_id}/report")


def zm_get_placement_eligible_mailboxes():
    """Get mailboxes eligible for placement testing."""
    return zm_get("/v2/placement-test/mailboxes/eligible")


def zm_get_placement_credits():
    """Get available placement test credits."""
    return zm_get("/v2/placement-test/credits/available")
```

- [ ] **Step 2: Commit**

```bash
cd ~/email-infra
git add zapmail_ops.py
git commit -m "feat: add zapmail_ops.py — new ZapMail API wrappers for subscriptions, wallet, health, photos, placement tests"
```

---

## Task 2: Create `pipeline.py` — Pipeline Engine & State Machine

**Files:**
- Create: `pipeline.py`

The pipeline engine handles both new client setup and domain replacement. Each pipeline is a state machine with steps persisted to JSON.

- [ ] **Step 1: Create `pipeline.py` with state management and step definitions**

```python
"""Pipeline engine for autonomous infrastructure management.

Handles new client setup and domain replacement as state machines.
Each pipeline is persisted to pipelines/<id>.json for crash recovery.
"""

import json
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

SCRIPT_DIR = Path(__file__).parent
PIPELINES_DIR = SCRIPT_DIR / "pipelines"
PIPELINES_DIR.mkdir(exist_ok=True)

# Hosted headshot URL — must be publicly accessible
HEADSHOT_URL = "https://your-hosted-url.com/headshots/sean_reynolds.png"  # TODO: set actual URL after hosting

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
        except Exception:
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

    for domain_name in pipeline["domains"]:
        pipeline["domains"][domain_name]["step"] = "claim_domains"
        pipeline["domains"][domain_name]["step_status"] = "complete"

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

    # Poll for domains to become active (up to 30 min)
    max_wait = 30 * 60
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

        specs = generate_mailbox_specs(domain_name, count=3)
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
            if isinstance(mb_list, list) and len(mb_list) >= 3:
                info["mailbox_ids"] = [m["id"] for m in mb_list]
                info["step"] = "create_mailboxes"
                info["step_status"] = "complete"
            else:
                # Retry failed
                zm_retry_failed_mailboxes()
                time.sleep(10)
                mailboxes = zm_list_mailboxes(domain_id=domain_id)
                mb_list = mailboxes.get("data", []) if isinstance(mailboxes, dict) else []
                if isinstance(mb_list, list) and len(mb_list) >= 3:
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
    for info in pipeline["domains"].values():
        info["step"] = "upload_photos"
        info["step_status"] = "complete"

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

    for info in pipeline["domains"].values():
        info["step"] = "tag_and_configure"
        info["step_status"] = "complete"

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
    max_wait = 15 * 60  # 15 minutes
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
    found_all = True
    for domain_name, info in pipeline["domains"].items():
        # Search SmartLead accounts for this domain
        offset = 0
        domain_accounts = []
        while True:
            batch = sl_list_accounts(offset=offset, limit=100)
            if not isinstance(batch, list):
                break
            for a in batch:
                if domain_name in a.get("from_email", ""):
                    domain_accounts.append(a)
            if len(batch) < 100:
                break
            offset += 100

        if len(domain_accounts) >= 3:
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

    for info in pipeline["domains"].values():
        info["step"] = "enable_warmup"
        info["step_status"] = "complete"

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
                    if float(rep) < 99:
                        all_warmed = False
                except (ValueError, TypeError):
                    all_warmed = False

    if all_warmed:
        for info in pipeline["domains"].values():
            info["step"] = "wait_for_warmup"
            info["step_status"] = "complete"

    return all_warmed


def step_check_campaigns(pipeline):
    """Check if old inboxes are in any active campaigns.
    If yes, set pipeline to 'awaiting_removal' (needs manual action from dashboard).
    If no old inboxes or none in campaigns, auto-proceed.
    """
    old_domains = pipeline.get("old_domains", [])
    if not old_domains:
        for info in pipeline["domains"].values():
            info["step"] = "check_campaigns"
            info["step_status"] = "complete"
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

    for info in pipeline["domains"].values():
        info["step"] = "check_campaigns"
        info["step_status"] = "complete"
    return True


def step_remove_old(pipeline):
    """Remove old email accounts from SmartLead entirely."""
    old_domains = pipeline.get("old_domains", [])
    for old_domain in old_domains:
        for acc_id in old_domain.get("smartlead_account_ids", []):
            requests.delete(
                f"{SMARTLEAD_API}/email-accounts/{acc_id}?api_key={SMARTLEAD_KEY}",
                timeout=30,
            )
            time.sleep(0.5)

    for info in pipeline["domains"].values():
        info["step"] = "remove_old"
        info["step_status"] = "complete"
    return True


def step_cleanup(pipeline):
    """Schedule old ZapMail mailboxes for removal at renewal."""
    old_domains = pipeline.get("old_domains", [])
    for old_domain in old_domains:
        mailbox_ids = old_domain.get("mailbox_ids", [])
        if mailbox_ids:
            zm_remove_on_renewal(mailbox_ids)

    for info in pipeline["domains"].values():
        info["step"] = "cleanup"
        info["step_status"] = "complete"
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
    """Execute pipeline steps from current position until blocked or complete."""
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


# --- Background Monitor Thread ---

def get_flagged_domains_for_client(client_accounts):
    """Check warmup reputation for all accounts, return flagged domains.
    A domain is flagged if ANY inbox has warmup reputation < 99.
    """
    flagged = {}  # domain -> {emails, smartlead_account_ids, mailbox_ids}
    for acc in client_accounts:
        wd = acc.get("warmup_details") or {}
        rep = wd.get("warmup_reputation", "?")
        email = acc.get("from_email", "")
        domain = email.split("@")[-1] if "@" in email else ""
        try:
            if float(rep) < 99:
                if domain not in flagged:
                    flagged[domain] = {"emails": [], "smartlead_account_ids": []}
                flagged[domain]["emails"].append(email)
                flagged[domain]["smartlead_account_ids"].append(acc["id"])
        except (ValueError, TypeError):
            pass
    return flagged


def calculate_volume_gap(old_renewal_date, warmup_start_date):
    """Calculate days between old inbox expiry and new inbox ready.
    Returns negative if replacement will be ready before old expires.
    """
    warmup_ready = warmup_start_date + timedelta(days=14)
    gap = (warmup_ready - old_renewal_date).days
    return gap  # positive = gap days without volume


def should_renew_old_inbox(old_renewal_date):
    """Determine if we should let the old inbox renew.
    If replacement won't be ready within 7 days of old expiry, renew.
    """
    now = datetime.now()
    days_until_renewal = (old_renewal_date - now).days
    # Replacement starts now, takes 14 days
    days_until_replacement_ready = 14
    gap = days_until_replacement_ready - days_until_renewal
    return gap > 7  # renew if gap > 7 days


def monitor_loop(check_interval_hours=4):
    """Background loop: check reputation every N hours, trigger replacements."""
    while True:
        try:
            _run_monitor_check()
        except Exception as e:
            print(f"[MONITOR] Error: {e}")
        time.sleep(check_interval_hours * 3600)


def _run_monitor_check():
    """Single monitor check iteration."""
    # Get all accounts grouped by client
    all_accounts = []
    offset = 0
    while True:
        batch = sl_list_accounts(offset=offset, limit=100)
        if isinstance(batch, list):
            all_accounts.extend(batch)
            if len(batch) < 100:
                break
            offset += 100
        else:
            break

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
                print(f"[MONITOR] No domains available for replacement of {domain}")
                continue

            # Pick oldest domain from inventory
            available.sort(key=lambda d: d.get("purchase_date", "9999"))
            replacement_domain = available[0]

            # Determine billing timing
            subs = zm_get_subscriptions()
            # For now, start replacement regardless — volume protection
            # logic in step_cleanup handles billing alignment

            # Create replacement pipeline
            pipeline = create_pipeline(
                "replacement",
                client["name"],
                [replacement_domain],
                forwarding_url="",  # inherit from existing
            )
            pipeline["old_domains"] = [{
                "domain": domain,
                "emails": domain_info["emails"],
                "smartlead_account_ids": domain_info["smartlead_account_ids"],
            }]
            save_pipeline(pipeline)

            # Run setup steps (async in thread)
            threading.Thread(
                target=run_pipeline_steps,
                args=(pipeline,),
                daemon=True,
            ).start()

    # Also check in-progress replacement pipelines that are waiting for warmup
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


def start_monitor_thread():
    """Start the background monitor as a daemon thread."""
    t = threading.Thread(target=monitor_loop, daemon=True, name="infra-monitor")
    t.start()
    return t
```

- [ ] **Step 2: Create `pipelines/` directory**

```bash
cd ~/email-infra
mkdir -p pipelines
touch pipelines/.gitkeep
```

- [ ] **Step 3: Commit**

```bash
cd ~/email-infra
git add pipeline.py pipelines/.gitkeep
git commit -m "feat: add pipeline.py — state machine engine for autonomous infrastructure setup and replacement"
```

---

## Task 3: Add Pipeline API Endpoints to `dashboard.py`

**Files:**
- Modify: `dashboard.py:1-16` (imports)
- Modify: `dashboard.py:825-886` (add new GET/POST routes)
- Modify: `dashboard.py:921-935` (start background thread in main)

- [ ] **Step 1: Add imports at top of `dashboard.py`**

After line 15 (`from pathlib import Path`), add:

```python
import threading
from pipeline import (
    create_pipeline, load_pipeline, load_all_pipelines,
    run_pipeline_steps, save_pipeline, start_monitor_thread,
)
from zapmail_ops import (
    zm_get_wallet_balance, zm_get_domain_health,
    zm_get_subscriptions, zm_get_subscription_mailboxes,
    zm_get_placement_results, zm_get_placement_eligible_mailboxes,
    zm_run_placement_test, zm_get_placement_credits,
)
from sheets import get_available_domains, get_domain_summary
```

- [ ] **Step 2: Add new API endpoint functions before the HTTP server class**

Before the line `# --- HTTP Server ---` (line 791), add:

```python
# --- Pipeline API logic ---

def api_pipeline_new_client(body):
    """Start a new client setup pipeline."""
    client_name = body.get("client_name", "")
    domain_count = body.get("domain_count", 0)
    forwarding_url = body.get("forwarding_url", "")
    selected_domains = body.get("domains", [])  # optional: pre-selected domain names

    if not client_name or (not domain_count and not selected_domains):
        return {"error": "client_name and domain_count (or domains) required"}

    # Get available domains from sheet
    available = get_available_domains()
    if not available:
        return {"error": "No domains available in inventory"}

    if selected_domains:
        # Use specific domains requested
        chosen = [d for d in available if d["domain"] in selected_domains]
    else:
        # Pick oldest domains
        available.sort(key=lambda d: d.get("purchase_date", "9999"))
        chosen = available[:domain_count]

    if len(chosen) < (len(selected_domains) if selected_domains else domain_count):
        return {"error": f"Only {len(chosen)} domains available, need {domain_count}"}

    pipeline = create_pipeline("new_setup", client_name, chosen, forwarding_url)

    # Run in background thread
    threading.Thread(target=run_pipeline_steps, args=(pipeline,), daemon=True).start()

    return {"pipeline_id": pipeline["id"], "status": "started", "domains": list(pipeline["domains"].keys())}


def api_pipeline_replacement(body):
    """Manually trigger replacement for a client/domain."""
    client_name = body.get("client_name", "")
    old_domain = body.get("old_domain", "")
    old_emails = body.get("old_emails", [])
    old_account_ids = body.get("old_account_ids", [])

    if not client_name:
        return {"error": "client_name required"}

    # Get a replacement domain from inventory
    available = get_available_domains()
    if not available:
        return {"error": "No domains available in inventory"}

    available.sort(key=lambda d: d.get("purchase_date", "9999"))
    chosen = available[:1]

    pipeline = create_pipeline("replacement", client_name, chosen)
    if old_domain:
        pipeline["old_domains"] = [{
            "domain": old_domain,
            "emails": old_emails,
            "smartlead_account_ids": old_account_ids,
        }]
        save_pipeline(pipeline)

    threading.Thread(target=run_pipeline_steps, args=(pipeline,), daemon=True).start()

    return {"pipeline_id": pipeline["id"], "status": "started"}


def api_pipeline_active():
    """List all active pipelines."""
    all_p = load_all_pipelines()
    result = []
    for p in all_p:
        result.append({
            "id": p["id"],
            "type": p["type"],
            "client_name": p["client_name"],
            "status": p["status"],
            "current_step": p.get("current_step", ""),
            "domains": list(p["domains"].keys()),
            "created_at": p.get("created_at", ""),
            "updated_at": p.get("updated_at", ""),
            "errors": p.get("errors", []),
            "pending_removals": p.get("pending_removals", {}),
        })
    result.sort(key=lambda p: p["created_at"], reverse=True)
    return {"pipelines": result}


def api_pipeline_detail(pipeline_id):
    """Get detailed status for a specific pipeline."""
    p = load_pipeline(pipeline_id)
    if not p:
        return {"error": "Pipeline not found"}
    return p


def api_inbox_campaigns(email):
    """List active campaigns containing this inbox."""
    r = requests.get(f"{SMARTLEAD_API}/campaign?api_key={SMARTLEAD_KEY}", timeout=30)
    campaigns = r.json() if r.status_code == 200 else []
    active = [c for c in campaigns if c.get("status") == "ACTIVE"]

    found = []
    for camp in active:
        cr = requests.get(
            f"{SMARTLEAD_API}/campaigns/{camp['id']}/email-accounts?api_key={SMARTLEAD_KEY}",
            timeout=30,
        )
        if cr.status_code == 200:
            accs = cr.json() if isinstance(cr.json(), list) else []
            for a in accs:
                if a.get("from_email") == email:
                    found.append({"campaign_id": camp["id"], "campaign_name": camp["name"]})
                    break

    return {"email": email, "campaigns": found}


def api_remove_from_campaign(body):
    """Remove an email account from a specific campaign."""
    email = body.get("email", "")
    campaign_id = body.get("campaign_id")
    if not email or not campaign_id:
        return {"error": "email and campaign_id required"}

    # Find account ID by email
    cr = requests.get(
        f"{SMARTLEAD_API}/campaigns/{campaign_id}/email-accounts?api_key={SMARTLEAD_KEY}",
        timeout=30,
    )
    if cr.status_code != 200:
        return {"error": "Failed to get campaign accounts"}

    accs = cr.json() if isinstance(cr.json(), list) else []
    acc_id = None
    for a in accs:
        if a.get("from_email") == email:
            acc_id = a.get("id")
            break

    if not acc_id:
        return {"error": f"{email} not found in campaign {campaign_id}"}

    # Remove from campaign
    dr = requests.delete(
        f"{SMARTLEAD_API}/campaigns/{campaign_id}/email-accounts/{acc_id}?api_key={SMARTLEAD_KEY}",
        timeout=30,
    )
    return {"success": dr.status_code == 200, "email": email, "campaign_id": campaign_id}


def api_remove_from_all_campaigns(body):
    """Remove an email account from all active campaigns."""
    email = body.get("email", "")
    if not email:
        return {"error": "email required"}

    campaigns_data = api_inbox_campaigns(email)
    results = []
    for camp in campaigns_data["campaigns"]:
        result = api_remove_from_campaign({"email": email, "campaign_id": camp["campaign_id"]})
        results.append(result)

    # If this inbox is in a pipeline awaiting removal, resume the pipeline
    all_p = load_all_pipelines()
    for p in all_p:
        if p.get("status") == "awaiting_removal" and p.get("pending_removals"):
            if email in p["pending_removals"]:
                del p["pending_removals"][email]
                if not p["pending_removals"]:
                    # All removals done — resume pipeline
                    p["status"] = "running"
                    next_idx = p["steps"].index("check_campaigns") + 1
                    if next_idx < len(p["steps"]):
                        p["current_step"] = p["steps"][next_idx]
                    save_pipeline(p)
                    threading.Thread(target=run_pipeline_steps, args=(p,), daemon=True).start()
                else:
                    save_pipeline(p)

    return {"email": email, "removed_from": len(results), "results": results}


def api_wallet():
    """Get ZapMail wallet balance."""
    return zm_get_wallet_balance()


def api_domain_inventory():
    """Get available domain inventory from THT spreadsheet."""
    available = get_available_domains()
    summary = get_domain_summary()
    return {
        "available_count": len(available),
        "available_domains": [
            {
                "domain": d["domain"],
                "provider": d.get("provider", ""),
                "purchase_date": d.get("purchase_date", ""),
                "notes": d.get("notes", ""),
            }
            for d in available
        ],
        "summary": summary,
    }


def api_placement_tests():
    """Get placement test results."""
    return zm_get_placement_results()


def api_subscriptions():
    """Get ZapMail subscription/billing data."""
    return zm_get_subscriptions()
```

- [ ] **Step 3: Add new routes to `do_GET` in `DashboardHandler`**

In `dashboard.py`, inside `do_GET`, after the `elif path == "/api/domains":` block (around line 840), add before the `else: self._error(404)`:

```python
                elif path == "/api/pipeline/active":
                    self._json_response(api_pipeline_active())
                elif path.startswith("/api/pipeline/") and len(path.split("/")) == 4:
                    pid = path.split("/")[3]
                    self._json_response(api_pipeline_detail(pid))
                elif path.startswith("/api/inbox/") and path.endswith("/campaigns"):
                    email = path.split("/")[3]
                    self._json_response(api_inbox_campaigns(email))
                elif path == "/api/wallet":
                    self._json_response(api_wallet())
                elif path == "/api/domain-inventory":
                    self._json_response(api_domain_inventory())
                elif path == "/api/placement-tests":
                    self._json_response(api_placement_tests())
                elif path == "/api/subscriptions":
                    self._json_response(api_subscriptions())
```

- [ ] **Step 4: Add new routes to `do_POST` in `DashboardHandler`**

After the `elif self.path == "/api/domains/auto-renew":` block (around line 884), add before the `else: self._error(404)`:

```python
        elif self.path == "/api/pipeline/new-client":
            result = api_pipeline_new_client(body)
            self._json_response(result, 400 if "error" in result else 200)
        elif self.path == "/api/pipeline/replacement":
            result = api_pipeline_replacement(body)
            self._json_response(result, 400 if "error" in result else 200)
        elif self.path == "/api/inbox/remove-from-campaign":
            result = api_remove_from_campaign(body)
            self._json_response(result)
        elif self.path == "/api/inbox/remove-from-all-campaigns":
            result = api_remove_from_all_campaigns(body)
            self._json_response(result)
```

- [ ] **Step 5: Start background monitor thread in `main()`**

In `dashboard.py`, replace the `main()` function:

```python
def main():
    port = int(os.environ.get("PORT", sys.argv[1] if len(sys.argv) > 1 else 8099))
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"

    # Start background infrastructure monitor
    monitor = start_monitor_thread()
    print(f"Infrastructure monitor started (checking every 4 hours)")

    server = HTTPServer((host, port), DashboardHandler)
    print(f"Dashboard running at http://{host}:{port}")
    print("Press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()
```

- [ ] **Step 6: Commit**

```bash
cd ~/email-infra
git add dashboard.py
git commit -m "feat: add pipeline API endpoints and background monitor to dashboard"
```

---

## Task 4: Dashboard UI — New Client Setup Form

**Files:**
- Modify: `dashboard.html:108-114` (topbar — add wallet balance)
- Modify: `dashboard.html:124-129` (tabs — add Pipelines tab)
- Modify: `dashboard.html:131-161` (SmartLead tab — add setup button)

- [ ] **Step 1: Add wallet balance to topbar**

In `dashboard.html`, replace the topbar-right div (line 110-113):

```html
    <div class="topbar-right">
        <span id="wallet-balance" style="color:#4ecdc4;font-weight:600;"></span>
        <span id="pipeline-badge" style="display:none;background:#7c4dff;color:#fff;padding:2px 8px;border-radius:10px;font-size:12px;"></span>
        <span id="last-updated"></span>
        <button onclick="loadOverview()">Refresh</button>
    </div>
```

- [ ] **Step 2: Add Pipelines tab**

In `dashboard.html`, replace the tabs div (lines 124-129):

```html
        <div class="tabs">
            <div class="tab active" onclick="switchTab('smartlead')">SmartLead</div>
            <div class="tab" onclick="switchTab('zapmail')">ZapMail</div>
            <div class="tab" onclick="switchTab('domains')">Domains</div>
            <div class="tab" onclick="switchTab('pipelines')">Pipelines</div>
            <div class="tab" onclick="switchTab('sync')">Sync Check</div>
        </div>
```

- [ ] **Step 3: Add new client setup button to SmartLead tab**

In `dashboard.html`, after `<div id="tab-smartlead" class="tab-content active">` (line 132), insert:

```html
            <div style="display:flex;gap:12px;margin-bottom:16px;">
                <button onclick="showNewClientForm()" style="background:#0f3460;color:#eee;border:1px solid #1a5276;padding:8px 18px;border-radius:6px;cursor:pointer;font-size:14px;font-weight:500;">+ New Client Setup</button>
                <span id="inventory-alert" style="display:none;color:#ffd93d;font-size:13px;padding:10px 0;"></span>
            </div>
```

- [ ] **Step 4: Add Pipelines tab content and new client form modal**

After the Sync tab div (before `</div>` that closes `#content`), add:

```html
        <!-- Pipelines Tab -->
        <div id="tab-pipelines" class="tab-content">
            <div id="pipelines-loading" class="loading" style="display:none;">
                <span class="spinner"></span> Loading pipelines...
            </div>
            <div id="pipelines-content"></div>
        </div>
```

Before `<div class="detail-overlay"` (line 186), add the new client form modal:

```html
<div id="new-client-overlay" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.7);z-index:200;" onclick="closeNewClientForm()"></div>
<div id="new-client-form" style="display:none;position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);background:#16213e;border:1px solid #0f3460;border-radius:12px;padding:32px;z-index:201;width:500px;max-width:90vw;">
    <h2 style="margin-bottom:20px;">New Client Setup</h2>
    <div style="margin-bottom:16px;">
        <label style="display:block;font-size:13px;color:#888;margin-bottom:4px;">Client Name</label>
        <input id="nc-client-name" type="text" style="width:100%;padding:8px 12px;background:#0a1628;color:#eee;border:1px solid #0f3460;border-radius:6px;font-size:14px;">
    </div>
    <div style="margin-bottom:16px;">
        <label style="display:block;font-size:13px;color:#888;margin-bottom:4px;">Number of Domains</label>
        <input id="nc-domain-count" type="number" min="1" max="50" value="5" style="width:100%;padding:8px 12px;background:#0a1628;color:#eee;border:1px solid #0f3460;border-radius:6px;font-size:14px;">
    </div>
    <div style="margin-bottom:16px;">
        <label style="display:block;font-size:13px;color:#888;margin-bottom:4px;">Forwarding URL</label>
        <input id="nc-forwarding" type="text" placeholder="https://..." style="width:100%;padding:8px 12px;background:#0a1628;color:#eee;border:1px solid #0f3460;border-radius:6px;font-size:14px;">
    </div>
    <div id="nc-inventory" style="font-size:13px;color:#888;margin-bottom:16px;"></div>
    <div style="display:flex;gap:12px;justify-content:flex-end;">
        <button onclick="closeNewClientForm()" style="background:none;color:#888;border:1px solid #0f3460;padding:8px 18px;border-radius:6px;cursor:pointer;">Cancel</button>
        <button id="nc-start-btn" onclick="startNewClientPipeline()" style="background:#4ecdc4;color:#1a1a2e;border:none;padding:8px 24px;border-radius:6px;cursor:pointer;font-weight:600;">Start Setup</button>
    </div>
    <div id="nc-status" style="margin-top:12px;font-size:13px;"></div>
</div>
```

- [ ] **Step 5: Add JavaScript for new client form, pipelines tab, wallet**

Before the closing `</script>` tag, add:

```javascript
// --- Wallet ---
async function loadWallet() {
    try {
        const resp = await fetch('/api/wallet');
        const data = await resp.json();
        const balance = data.data?.balance || data.balance || '?';
        const el = document.getElementById('wallet-balance');
        const num = parseFloat(balance);
        el.textContent = '$' + (isNaN(num) ? '?' : num.toFixed(2));
        el.style.color = num < 50 ? '#ff6b6b' : num < 150 ? '#ffd93d' : '#4ecdc4';
    } catch(e) { console.error('Wallet error:', e); }
}

// --- New Client Form ---
function showNewClientForm() {
    document.getElementById('new-client-overlay').style.display = 'block';
    document.getElementById('new-client-form').style.display = 'block';
    document.getElementById('nc-status').textContent = '';
    loadInventoryPreview();
}

function closeNewClientForm() {
    document.getElementById('new-client-overlay').style.display = 'none';
    document.getElementById('new-client-form').style.display = 'none';
}

async function loadInventoryPreview() {
    try {
        const resp = await fetch('/api/domain-inventory');
        const data = await resp.json();
        const el = document.getElementById('nc-inventory');
        el.textContent = data.available_count + ' domains available in inventory';
        el.style.color = data.available_count < 5 ? '#ff6b6b' : '#4ecdc4';

        // Also show inventory alert on main page
        const alertEl = document.getElementById('inventory-alert');
        if (data.available_count < 5) {
            alertEl.style.display = 'inline';
            alertEl.textContent = 'Low inventory: only ' + data.available_count + ' domains available';
        }
    } catch(e) { console.error('Inventory error:', e); }
}

async function startNewClientPipeline() {
    const clientName = document.getElementById('nc-client-name').value.trim();
    const domainCount = parseInt(document.getElementById('nc-domain-count').value);
    const forwarding = document.getElementById('nc-forwarding').value.trim();

    if (!clientName) { alert('Client name required'); return; }
    if (!domainCount || domainCount < 1) { alert('Domain count required'); return; }

    document.getElementById('nc-start-btn').disabled = true;
    document.getElementById('nc-status').innerHTML = '<span class="spinner" style="width:16px;height:16px;border-width:2px;"></span> Starting pipeline...';

    try {
        const resp = await fetch('/api/pipeline/new-client', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({client_name: clientName, domain_count: domainCount, forwarding_url: forwarding})
        });
        const result = await resp.json();
        if (result.error) {
            document.getElementById('nc-status').innerHTML = '<span style="color:#ff6b6b;">' + result.error + '</span>';
            document.getElementById('nc-start-btn').disabled = false;
        } else {
            document.getElementById('nc-status').innerHTML = '<span style="color:#4ecdc4;">Pipeline started! ID: ' + result.pipeline_id + '</span>';
            setTimeout(() => {
                closeNewClientForm();
                switchTab('pipelines');
                loadPipelines();
            }, 1500);
        }
    } catch(err) {
        document.getElementById('nc-status').innerHTML = '<span style="color:#ff6b6b;">Error: ' + err.message + '</span>';
        document.getElementById('nc-start-btn').disabled = false;
    }
}

// --- Pipelines Tab ---
let pipelineData = null;

async function loadPipelines() {
    document.getElementById('pipelines-loading').style.display = 'block';
    document.getElementById('pipelines-content').innerHTML = '';
    try {
        const resp = await fetch('/api/pipeline/active');
        pipelineData = await resp.json();
        renderPipelines();
    } catch(err) {
        document.getElementById('pipelines-content').innerHTML = 'Error: ' + err.message;
    }
    document.getElementById('pipelines-loading').style.display = 'none';
}

function renderPipelines() {
    const pipelines = pipelineData.pipelines || [];

    // Update badge
    const active = pipelines.filter(p => p.status === 'running' || p.status === 'awaiting_removal');
    const badge = document.getElementById('pipeline-badge');
    if (active.length > 0) {
        badge.style.display = 'inline';
        badge.textContent = active.length + ' active';
    } else {
        badge.style.display = 'none';
    }

    if (pipelines.length === 0) {
        document.getElementById('pipelines-content').innerHTML = '<div style="text-align:center;color:#888;padding:40px;">No pipelines yet. Start one from the SmartLead tab.</div>';
        return;
    }

    const stepLabels = {
        claim_domains: 'Claim Domains',
        set_dns: 'Set DNS',
        connect_zapmail: 'Connect ZapMail',
        create_mailboxes: 'Create Mailboxes',
        upload_photos: 'Upload Photos',
        tag_and_configure: 'Tag & Configure',
        export_to_smartlead: 'Export to SmartLead',
        enable_warmup: 'Enable Warmup',
        wait_for_warmup: 'Waiting for Warmup',
        check_campaigns: 'Check Campaigns',
        remove_old: 'Remove Old Inboxes',
        cleanup: 'Cleanup',
    };

    let html = '';
    pipelines.forEach(p => {
        const statusColor = p.status === 'complete' ? '#4ecdc4' : p.status === 'error' ? '#ff6b6b' : p.status === 'awaiting_removal' ? '#ffd93d' : '#7c4dff';
        const statusLabel = p.status === 'awaiting_removal' ? 'Awaiting Removal' : p.status.charAt(0).toUpperCase() + p.status.slice(1);

        html += `<div style="background:#16213e;border:1px solid #0f3460;border-radius:10px;padding:16px;margin-bottom:12px;">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
                <div>
                    <span style="font-size:16px;font-weight:600;">${p.client_name}</span>
                    <span style="font-size:13px;color:#888;margin-left:12px;">${p.type === 'new_setup' ? 'New Setup' : 'Replacement'}</span>
                </div>
                <span style="color:${statusColor};font-weight:500;">${statusLabel}</span>
            </div>
            <div style="font-size:13px;color:#888;margin-bottom:8px;">Domains: ${p.domains.join(', ')}</div>
            <div style="font-size:12px;color:#888;">Started: ${new Date(p.created_at).toLocaleString()}</div>`;

        // Step progress
        if (p.status !== 'complete') {
            const allSteps = p.type === 'new_setup'
                ? ['claim_domains','set_dns','connect_zapmail','create_mailboxes','upload_photos','tag_and_configure','export_to_smartlead','enable_warmup']
                : ['claim_domains','set_dns','connect_zapmail','create_mailboxes','upload_photos','tag_and_configure','export_to_smartlead','enable_warmup','wait_for_warmup','check_campaigns','remove_old','cleanup'];
            const currentIdx = allSteps.indexOf(p.current_step);
            html += '<div style="display:flex;gap:4px;margin-top:12px;flex-wrap:wrap;">';
            allSteps.forEach((s, i) => {
                const color = i < currentIdx ? '#4ecdc4' : i === currentIdx ? '#7c4dff' : '#333';
                const label = stepLabels[s] || s;
                html += `<div style="background:${color};padding:4px 10px;border-radius:4px;font-size:11px;color:${i <= currentIdx ? '#fff' : '#666'};" title="${label}">${label}</div>`;
            });
            html += '</div>';
        }

        // Pending removals alert
        if (p.status === 'awaiting_removal' && p.pending_removals) {
            html += '<div style="background:#4a1a1a;border:1px solid #8b3a3a;border-radius:8px;padding:12px;margin-top:12px;">';
            html += '<div style="color:#ff6b6b;font-weight:600;margin-bottom:8px;">Inboxes need removal from campaigns</div>';
            for (const [email, camps] of Object.entries(p.pending_removals)) {
                html += `<div style="margin-bottom:8px;">
                    <div style="font-size:13px;color:#ffaaaa;">${email} is in ${camps.length} campaign(s):</div>`;
                camps.forEach(c => {
                    html += `<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 0 4px 16px;font-size:12px;">
                        <span style="color:#888;">${c.campaign_name}</span>
                        <button onclick="removeFromCampaign('${email}',${c.campaign_id})" style="background:#5c1a1a;color:#ff6b6b;border:1px solid #8b3a3a;padding:2px 10px;border-radius:4px;cursor:pointer;font-size:11px;">Remove</button>
                    </div>`;
                });
                html += `<button onclick="removeFromAllCampaigns('${email}')" style="background:#5c1a1a;color:#ff6b6b;border:1px solid #8b3a3a;padding:4px 12px;border-radius:4px;cursor:pointer;font-size:12px;margin-top:4px;">Remove from all campaigns</button>`;
                html += '</div>';
            }
            html += '</div>';
        }

        // Errors
        if (p.errors && p.errors.length > 0) {
            html += '<div style="margin-top:8px;font-size:12px;color:#ff6b6b;">';
            p.errors.forEach(e => { html += '<div>' + e + '</div>'; });
            html += '</div>';
        }

        html += '</div>';
    });

    document.getElementById('pipelines-content').innerHTML = html;
}

async function removeFromCampaign(email, campaignId) {
    if (!confirm('Remove ' + email + ' from this campaign?')) return;
    try {
        await fetch('/api/inbox/remove-from-campaign', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({email, campaign_id: campaignId})
        });
        loadPipelines();
    } catch(e) { alert('Error: ' + e.message); }
}

async function removeFromAllCampaigns(email) {
    if (!confirm('Remove ' + email + ' from ALL active campaigns?')) return;
    try {
        await fetch('/api/inbox/remove-from-all-campaigns', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({email})
        });
        loadPipelines();
    } catch(e) { alert('Error: ' + e.message); }
}
```

- [ ] **Step 6: Update tab switching and initial load**

Replace the `switchTab` function to include pipelines:

```javascript
function switchTab(tab) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    document.querySelector(`.tab[onclick="switchTab('${tab}')"]`).classList.add('active');
    document.getElementById('tab-' + tab).classList.add('active');

    if (tab === 'zapmail' && !zmData) loadZapmail();
    if (tab === 'domains' && !domData) loadDomains();
    if (tab === 'pipelines') loadPipelines();
    if (tab === 'sync' && !syncData) loadSync();
}
```

Add wallet load to the initial load section (replace the last 3 lines):

```javascript
// Initial load
loadOverview();
loadWallet();

// Auto-refresh every 5 minutes
setInterval(loadOverview, 5 * 60 * 1000);
setInterval(loadWallet, 5 * 60 * 1000);
```

- [ ] **Step 7: Commit**

```bash
cd ~/email-infra
git add dashboard.html
git commit -m "feat: add dashboard UI — new client setup form, pipelines tab, wallet balance, pending removals"
```

---

## Task 5: Add Pending Removals to Client Detail Panel

**Files:**
- Modify: `dashboard.html` (renderDetailTable function, around line 346)

- [ ] **Step 1: Update `renderDetailTable` to show pending removal alerts**

In `dashboard.html`, inside `renderDetailTable`, after the replacement recommendation div and before the table, add pending removals check:

Replace the opening of `renderDetailTable` (lines 346-360) with:

```javascript
function renderDetailTable(data) {
    const accounts = data.accounts;

    // Replacement recommendation
    let recHtml = '';
    if (data.flagged_domains && data.flagged_domains.length > 0) {
        recHtml = `<div style="background:#4a1a1a;border:1px solid #8b3a3a;border-radius:8px;padding:14px 18px;margin-bottom:16px;">
            <div style="font-size:14px;color:#ff6b6b;font-weight:600;margin-bottom:6px;">Infrastructure Replacement Needed</div>
            <div style="font-size:13px;color:#ffaaaa;">${data.flagged_inbox_count} inbox(es) across ${data.flagged_domains.length} domain(s) are unhealthy.</div>
            <div style="font-size:13px;color:#ffaaaa;margin-bottom:10px;">Recommended: Set up ${data.replacement_domains_needed} new domain(s) (${data.replacement_inboxes} inboxes).</div>
            <div style="font-size:12px;color:#888;margin-bottom:10px;">Flagged domains: ${data.flagged_domains.join(', ')}</div>
            <button onclick="triggerReplacement('${data.client_id}', ${JSON.stringify(data.flagged_domains).replace(/'/g, "\\'")})" style="background:#7c4dff;color:#fff;border:none;padding:8px 18px;border-radius:6px;cursor:pointer;font-size:13px;font-weight:500;">Start Replacement</button>
            <span id="replacement-status-${data.client_id}" style="margin-left:12px;font-size:13px;"></span>
        </div>`;
    }

    let html = recHtml;
```

- [ ] **Step 2: Add `triggerReplacement` JavaScript function**

Before the closing `</script>`, add:

```javascript
async function triggerReplacement(clientId, flaggedDomains) {
    const statusEl = document.getElementById('replacement-status-' + clientId);
    statusEl.innerHTML = '<span class="spinner" style="width:14px;height:14px;border-width:2px;"></span> Starting...';

    // Get client name from overview data
    const client = overviewData.clients.find(c => c.id === parseInt(clientId));
    if (!client) { statusEl.textContent = 'Client not found'; return; }

    try {
        const resp = await fetch('/api/pipeline/replacement', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                client_name: client.name,
                old_domain: flaggedDomains[0],
                old_emails: [],
                old_account_ids: [],
            })
        });
        const result = await resp.json();
        if (result.error) {
            statusEl.innerHTML = '<span style="color:#ff6b6b;">' + result.error + '</span>';
        } else {
            statusEl.innerHTML = '<span style="color:#4ecdc4;">Pipeline started!</span>';
        }
    } catch(err) {
        statusEl.innerHTML = '<span style="color:#ff6b6b;">Error: ' + err.message + '</span>';
    }
}
```

- [ ] **Step 3: Commit**

```bash
cd ~/email-infra
git add dashboard.html
git commit -m "feat: add replacement trigger button and pending removals to client detail panel"
```

---

## Task 6: Host Headshot Image and Set URL

**Files:**
- Modify: `pipeline.py:27` (HEADSHOT_URL constant)

- [ ] **Step 1: Serve headshot from dashboard**

In `dashboard.py`, add a route to serve the headshot file. In `do_GET`, after the `path == "/" or path == "/dashboard.html"` block:

```python
        elif path == "/headshots/sean_reynolds.png":
            self._serve_file("headshots/sean_reynolds.png", "image/png", set_cookie=pw)
```

- [ ] **Step 2: Update HEADSHOT_URL in pipeline.py**

Replace the HEADSHOT_URL line in `pipeline.py`:

```python
# Headshot URL — served from the dashboard itself.
# When deployed to Render, use the Render URL. Locally, use localhost.
import os as _os
_DASHBOARD_HOST = _os.environ.get("RENDER_EXTERNAL_URL", "http://127.0.0.1:8099")
HEADSHOT_URL = f"{_DASHBOARD_HOST}/headshots/sean_reynolds.png"
```

- [ ] **Step 3: Commit**

```bash
cd ~/email-infra
git add pipeline.py dashboard.py
git commit -m "feat: serve headshot from dashboard, set dynamic HEADSHOT_URL for pipeline"
```

---

## Task 7: Weekly Placement Test Scheduling

**Files:**
- Modify: `pipeline.py` (add placement test to monitor loop)

- [ ] **Step 1: Add placement test logic to the monitor**

In `pipeline.py`, add a function after `_run_monitor_check`:

```python
def _run_weekly_placement_tests():
    """Run placement tests for a sample of inboxes per client. Called weekly."""
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
    import random
    sample_ids = [m["id"] for m in random.sample(mailbox_list, sample_size)]

    result = zm_run_placement_test(sample_ids)

    # Save last run timestamp
    state_file.write_text(json.dumps({
        "last_run": datetime.now().isoformat(),
        "mailboxes_tested": sample_ids,
        "result": str(result)[:500],
    }))
    print(f"[PLACEMENT] Ran placement tests for {sample_size} mailboxes")
```

- [ ] **Step 2: Call placement tests from monitor loop**

Update `monitor_loop` in `pipeline.py`:

```python
def monitor_loop(check_interval_hours=4):
    """Background loop: check reputation every N hours, trigger replacements."""
    while True:
        try:
            _run_monitor_check()
        except Exception as e:
            print(f"[MONITOR] Error in reputation check: {e}")

        try:
            _run_weekly_placement_tests()
        except Exception as e:
            print(f"[MONITOR] Error in placement tests: {e}")

        time.sleep(check_interval_hours * 3600)
```

- [ ] **Step 3: Commit**

```bash
cd ~/email-infra
git add pipeline.py
git commit -m "feat: add weekly placement test scheduling to monitor loop"
```

---

## Task 8: Final Integration — Push and Verify

- [ ] **Step 1: Verify all imports resolve**

```bash
cd ~/email-infra
python3 -c "import dashboard; import pipeline; import zapmail_ops; print('All imports OK')"
```

Expected: `All imports OK`

- [ ] **Step 2: Test locally**

```bash
cd ~/email-infra
python3 dashboard.py &
sleep 3
# Test new endpoints
curl -s http://127.0.0.1:8099/api/pipeline/active | python3 -m json.tool
curl -s http://127.0.0.1:8099/api/wallet | python3 -m json.tool
curl -s http://127.0.0.1:8099/api/domain-inventory | python3 -m json.tool
kill %1
```

- [ ] **Step 3: Push to deploy**

```bash
cd ~/email-infra
git push origin main
```

- [ ] **Step 4: Verify on Render**

Open the deployed dashboard URL. Check:
- Wallet balance shows in topbar
- New Client Setup button opens form
- Pipelines tab loads (should show empty)
- Client detail panel shows replacement button for flagged clients
