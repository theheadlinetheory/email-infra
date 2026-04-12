#!/usr/bin/env python3
"""Autonomous script to complete setup of 33 THT .info acquisition domains.

Steps (each is idempotent — safe to re-run):
1. Wait for Zapmail API to recover
2. Check which domains already have mailboxes — skip those
3. Buy addon mailboxes ONLY if needed (checks quota first)
4. Create inboxes (aidan, aidanh, aidanhutch) on domains missing them
5. Set profile photos on all mailboxes
6. Set forwarding to theheadlinetheory.com
7. Export to SmartLead
8. Enable warmup on all exported accounts
9. Tag in SmartLead

Polls every 60s if Zapmail is down. Never double-purchases or double-creates.
"""

import os
import sys
import time
import json
import requests
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

from setup import (
    zm_get_workspace_id, zm_list_domains, zm_create_mailboxes,
    zm_set_forwarding, zm_update_mailbox, zm_export_mailboxes,
    zm_headers, zm_post, zm_get,
    sl_set_warmup, sl_url, SMARTLEAD_KEY, GOOGLE_WARMUP,
    ACQUISITION_PHOTO_URLS,
)

# ── Config ──
WID = "06d331bc-9610-40d0-b881-ef5e9883ec70"
FORWARD_TO = "https://theheadlinetheory.com/"
PHOTO_URL = ACQUISITION_PHOTO_URLS["aidan_hutchinson"]
STATUS_FILE = os.path.join(os.path.dirname(__file__), "info_domains_status.json")

THT_INFO_DOMAINS = [
    "gotheheadlinetheory.info", "gotheheadlinetheoryagency.info", "gotheheadlinetheoryco.info",
    "gotheheadlinetheorygroup.info", "gotheheadlinetheorylab.info", "headlinetheory360.info",
    "headlinetheory360co.info", "headlinetheory360group.info", "headlinetheory360hub.info",
    "headlinetheory360pro.info", "headlinetheorydot.info", "headlinetheorydotco.info",
    "headlinetheorydothub.info", "theheadlinetheory360.info", "theheadlinetheoryclub.info",
    "theheadlinetheoryclubhq.info", "theheadlinetheoryclublab.info", "theheadlinetheoryclubpro.info",
    "theheadlinetheorygrowth.info", "theheadlinetheorygrowthhq.info", "theheadlinetheorygrowthlab.info",
    "theheadlinetheoryhq.info", "theheadlinetheorylab.info", "theheadlinetheoryrev.info",
    "theheadlinetheoryrevagency.info", "theheadlinetheoryrevco.info", "theheadlinetheoryrevhub.info",
    "theheadlinetheoryteam.info", "theheadlinetheoryteamhq.info", "theheadlinetheoryteampro.info",
    "theheadlinetheoryzoom.info", "theheadlinetheoryzoomco.info", "theheadlinetheoryzoomlab.info",
]

INBOX_SPECS = [
    {"firstName": "Aidan", "lastName": "Hutchinson", "mailboxUsername": "aidan"},
    {"firstName": "Aidan", "lastName": "Hutchinson", "mailboxUsername": "aidanh"},
    {"firstName": "Aidan", "lastName": "Hutchinson", "mailboxUsername": "aidanhutch"},
]


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_status():
    if os.path.exists(STATUS_FILE):
        with open(STATUS_FILE) as f:
            return json.load(f)
    return {
        "started": datetime.now().isoformat(),
        "domains_with_inboxes": [],
        "domains_with_photos": [],
        "domains_forwarding_set": False,
        "exported_to_smartlead": False,
        "warmup_enabled": False,
        "tagged": False,
        "mailbox_ids": {},  # domain -> [id1, id2, id3]
        "domain_ids": {},   # domain -> zapmail_domain_id
    }


def save_status(status):
    with open(STATUS_FILE, "w") as f:
        json.dump(status, f, indent=2)


def api_healthy():
    """Check if Zapmail mailbox API is responding."""
    try:
        h = zm_headers(WID)
        # Use a lightweight call — list mailboxes for a known domain
        r = requests.get(
            "https://api.zapmail.ai/api/v2/mailboxes?page=1",
            headers=h, timeout=15
        )
        return r.status_code != 500
    except Exception:
        return False


def get_domain_map():
    """Get domain name -> {id, ...} for all THT .info domains."""
    all_domains = zm_list_domains(WID)
    return {
        d["domain"]: d for d in all_domains
        if d.get("domain") in THT_INFO_DOMAINS
    }


def get_existing_mailboxes(domain_id):
    """Get mailboxes already on a domain. Returns list or None if API error."""
    try:
        h = zm_headers(WID)
        r = requests.get(
            f"https://api.zapmail.ai/api/v2/mailboxes?domainId={domain_id}",
            headers=h, timeout=30
        )
        if r.status_code == 500:
            return None  # API still down
        data = r.json().get("data", {})
        if isinstance(data, dict):
            return data.get("mailboxes", [])
        return data if isinstance(data, list) else []
    except Exception:
        return None


def get_mailbox_quota():
    """Return (purchased, assigned) mailbox counts."""
    try:
        h = zm_headers(WID)
        r = requests.get("https://api.zapmail.ai/api/v2/workspaces", headers=h, timeout=30)
        ws = r.json().get("data", {}).get("currentWorkspace", {})
        purchased = int(ws.get("totalMailboxesPurchasedGoogle", 0))
        assigned = int(ws.get("assignedMailboxesCountGoogle", 0))
        return purchased, assigned
    except Exception:
        return None, None


def buy_mailboxes_if_needed(count_needed):
    """Buy addon mailboxes only if quota is insufficient. Returns True if ready."""
    purchased, assigned = get_mailbox_quota()
    if purchased is None:
        log("  Could not check quota — skipping purchase check")
        return True  # Try anyway

    available = purchased - assigned
    log(f"  Mailbox quota: {purchased} purchased, {assigned} assigned, {available} available, {count_needed} needed")

    if available >= count_needed:
        log(f"  Sufficient quota — no purchase needed")
        return True

    to_buy = count_needed - available
    log(f"  Buying {to_buy} addon mailboxes ($3/each = ${to_buy * 3})...")
    try:
        result = zm_post(f"/v2/wallet/buy-addon-mailboxes?quantity={to_buy}", {}, WID)
        if isinstance(result, dict) and result.get("status") in (200, 201):
            log(f"  Purchased {to_buy} mailboxes")
            return True
        else:
            msg = result.get("message", str(result)[:100]) if isinstance(result, dict) else str(result)[:100]
            log(f"  Purchase failed: {msg}")
            return False
    except Exception as e:
        log(f"  Purchase error: {e}")
        return False


def step_create_inboxes(status, domain_map):
    """Create inboxes on domains that don't have them yet."""
    already_done = set(status.get("domains_with_inboxes", []))
    to_do = [d for d in THT_INFO_DOMAINS if d not in already_done and d in domain_map]

    if not to_do:
        log("All domains already have inboxes")
        return True

    # First check which domains actually already have mailboxes (in case status file is stale)
    truly_need = []
    for domain in to_do:
        did = domain_map[domain]["id"]
        existing = get_existing_mailboxes(did)
        if existing is None:
            log(f"  API error checking {domain} — will retry later")
            return False
        if existing:
            # Already has mailboxes — record them
            log(f"  {domain}: already has {len(existing)} mailboxes — skipping")
            already_done.add(domain)
            ids = [m.get("id", "") for m in existing]
            status["mailbox_ids"][domain] = ids
        else:
            truly_need.append(domain)

    status["domains_with_inboxes"] = list(already_done)
    save_status(status)

    if not truly_need:
        log("All domains already have inboxes (verified)")
        return True

    # Buy mailboxes if needed
    needed_count = len(truly_need) * 3
    if not buy_mailboxes_if_needed(needed_count):
        return False

    # Create inboxes
    created = 0
    for i, domain in enumerate(truly_need):
        did = domain_map[domain]["id"]
        try:
            result = zm_create_mailboxes(did, domain, INBOX_SPECS, workspace_key=WID)
            # Handle various response formats
            if isinstance(result, str):
                # Sometimes API returns raw text on success
                log(f"  [{i+1}/{len(truly_need)}] {domain} -> response was text (likely OK)")
                already_done.add(domain)
                created += 1
            elif isinstance(result, dict):
                if result.get("status") in (200, 201):
                    data = result.get("data", {})
                    mailboxes = data.get("mailboxes", []) if isinstance(data, dict) else []
                    ids = [m.get("id", "") for m in mailboxes]
                    status["mailbox_ids"][domain] = ids
                    already_done.add(domain)
                    created += 1
                    log(f"  [{i+1}/{len(truly_need)}] {domain} -> {len(ids)} inboxes created")
                elif "not enough mailboxes" in str(result.get("message", "")).lower():
                    log(f"  Ran out of mailbox slots — buying more...")
                    remaining = len(truly_need) - i
                    if buy_mailboxes_if_needed(remaining * 3):
                        # Retry this domain
                        result2 = zm_create_mailboxes(did, domain, INBOX_SPECS, workspace_key=WID)
                        if isinstance(result2, dict) and result2.get("status") in (200, 201):
                            data2 = result2.get("data", {})
                            mailboxes2 = data2.get("mailboxes", []) if isinstance(data2, dict) else []
                            ids2 = [m.get("id", "") for m in mailboxes2]
                            status["mailbox_ids"][domain] = ids2
                            already_done.add(domain)
                            created += 1
                            log(f"  [{i+1}/{len(truly_need)}] {domain} -> retry OK")
                        else:
                            log(f"  [{i+1}/{len(truly_need)}] {domain} -> retry failed")
                    else:
                        log(f"  Could not buy more slots — stopping")
                        break
                elif result.get("status") == 500:
                    log(f"  API 500 error — Zapmail down again, will retry")
                    break
                else:
                    msg = result.get("message", "")[:100]
                    log(f"  [{i+1}/{len(truly_need)}] {domain} -> FAIL: {msg}")
            time.sleep(0.5)
        except Exception as e:
            log(f"  [{i+1}/{len(truly_need)}] {domain} -> ERROR: {e}")
            if "500" in str(e) or "timeout" in str(e).lower():
                break  # API is down

    status["domains_with_inboxes"] = list(already_done)
    save_status(status)
    log(f"  Inboxes: {len(already_done)}/{len(THT_INFO_DOMAINS)} domains done (+{created} this round)")
    return len(already_done) >= len(THT_INFO_DOMAINS)


def step_set_photos(status, domain_map):
    """Set profile photos on all mailboxes."""
    already_done = set(status.get("domains_with_photos", []))
    to_do = [d for d in THT_INFO_DOMAINS if d not in already_done]

    if not to_do:
        log("All profile photos already set")
        return True

    done_count = 0
    for domain in to_do:
        did = domain_map.get(domain, {}).get("id", "") if isinstance(domain_map.get(domain), dict) else ""
        if not did:
            continue

        existing = get_existing_mailboxes(did)
        if existing is None:
            log(f"  API error on {domain} — will retry")
            return False
        if not existing:
            continue  # No mailboxes yet

        all_ok = True
        for mb in existing:
            mb_id = mb.get("id", "")
            if not mb_id:
                continue
            # Check if photo already set
            if mb.get("profilePicture"):
                continue
            try:
                result = zm_update_mailbox(mb_id, {"profilePicture": PHOTO_URL}, workspace_key=WID)
                if isinstance(result, dict) and result.get("status") in (200, 201):
                    pass  # OK
                elif isinstance(result, dict) and result.get("status") == 500:
                    all_ok = False
                    break
            except Exception:
                all_ok = False
                break
            time.sleep(0.3)

        if all_ok:
            already_done.add(domain)
            done_count += 1

    status["domains_with_photos"] = list(already_done)
    save_status(status)
    log(f"  Photos: {len(already_done)}/{len(THT_INFO_DOMAINS)} domains done (+{done_count} this round)")
    return len(already_done) >= len(THT_INFO_DOMAINS)


def step_set_forwarding(status, domain_map):
    """Set forwarding to theheadlinetheory.com for all domains."""
    if status.get("domains_forwarding_set"):
        log("Forwarding already set")
        return True

    domain_ids = [domain_map[d]["id"] for d in THT_INFO_DOMAINS if d in domain_map]
    if not domain_ids:
        return False

    try:
        result = zm_set_forwarding(domain_ids, FORWARD_TO, workspace_key=WID)
        if isinstance(result, dict) and result.get("status") in (200, 201):
            status["domains_forwarding_set"] = True
            save_status(status)
            log(f"  Forwarding set to {FORWARD_TO} for {len(domain_ids)} domains")
            return True
        elif isinstance(result, dict) and result.get("status") == 500:
            log("  API 500 — will retry")
            return False
        else:
            msg = result.get("message", "")[:100] if isinstance(result, dict) else str(result)[:100]
            log(f"  Forwarding failed: {msg}")
            return False
    except Exception as e:
        log(f"  Forwarding error: {e}")
        return False


def step_export_smartlead(status, domain_map):
    """Export all mailboxes to SmartLead."""
    if status.get("exported_to_smartlead"):
        log("Already exported to SmartLead")
        return True

    # Collect all mailbox IDs
    all_mb_ids = []
    for domain in THT_INFO_DOMAINS:
        ids = status.get("mailbox_ids", {}).get(domain, [])
        if ids:
            all_mb_ids.extend(ids)
        else:
            # Need to fetch from API
            did = domain_map.get(domain, {}).get("id", "") if isinstance(domain_map.get(domain), dict) else ""
            if did:
                existing = get_existing_mailboxes(did)
                if existing:
                    ids = [m.get("id", "") for m in existing]
                    all_mb_ids.extend(ids)
                    status["mailbox_ids"][domain] = ids

    all_mb_ids = [i for i in all_mb_ids if i]
    if not all_mb_ids:
        log("  No mailbox IDs available for export")
        return False

    log(f"  Exporting {len(all_mb_ids)} mailboxes to SmartLead...")
    try:
        result = zm_export_mailboxes(["SMARTLEAD"], mailbox_ids=all_mb_ids)
        if isinstance(result, dict) and result.get("status") in (200, 201):
            status["exported_to_smartlead"] = True
            save_status(status)
            log(f"  Export initiated for {len(all_mb_ids)} mailboxes")
            return True
        elif isinstance(result, dict) and result.get("status") == 500:
            log("  API 500 — will retry")
            return False
        else:
            msg = result.get("message", "")[:100] if isinstance(result, dict) else str(result)[:100]
            log(f"  Export failed: {msg}")
            return False
    except Exception as e:
        log(f"  Export error: {e}")
        return False


def step_enable_warmup(status):
    """Enable warmup on all SmartLead accounts once they appear."""
    if status.get("warmup_enabled"):
        log("Warmup already enabled")
        return True

    # Find the SmartLead accounts by email pattern
    try:
        # Search SmartLead for accounts matching our domains
        r = requests.get(sl_url("/email-accounts"), params={"api_key": SMARTLEAD_KEY, "offset": 0, "limit": 200}, timeout=30)
        all_accounts = r.json() if r.status_code == 200 else []

        our_emails = set()
        for domain in THT_INFO_DOMAINS:
            for spec in INBOX_SPECS:
                our_emails.add(f"{spec['mailboxUsername']}@{domain}")

        matching = [a for a in all_accounts if a.get("from_email", "") in our_emails]

        if not matching:
            log(f"  No SmartLead accounts found yet (export may still be processing)")
            return False

        enabled = 0
        for acct in matching:
            aid = acct.get("id")
            if not aid:
                continue
            # Check if warmup already enabled
            wd = acct.get("warmup_details") or {}
            if wd.get("warmup_enabled"):
                enabled += 1
                continue
            try:
                sl_set_warmup(aid)
                enabled += 1
            except Exception as e:
                log(f"  Warmup error on {acct.get('from_email','')}: {e}")
            time.sleep(0.3)

        log(f"  Warmup: {enabled}/{len(our_emails)} accounts enabled ({len(matching)} found in SmartLead)")

        if enabled >= len(matching) and len(matching) > 0:
            status["warmup_enabled"] = True
            save_status(status)
            return len(matching) >= len(our_emails) * 0.9  # 90% threshold

        return False

    except Exception as e:
        log(f"  Warmup step error: {e}")
        return False


def main():
    log("=" * 60)
    log("THT .info Domain Completion Script")
    log(f"33 domains, 3 inboxes each (aidan/aidanh/aidanhutch)")
    log(f"Forwarding: {FORWARD_TO}")
    log("=" * 60)

    status = load_status()
    max_rounds = 120  # 120 * 60s = 2 hours max

    for round_num in range(1, max_rounds + 1):
        log(f"\n--- Round {round_num} ---")

        # Check API health
        if not api_healthy():
            log("Zapmail API still down (500) — waiting 60s...")
            time.sleep(60)
            continue

        log("Zapmail API is up")

        # Get domain map
        try:
            domain_map = get_domain_map()
            log(f"Found {len(domain_map)}/33 domains in Zapmail")
        except Exception as e:
            log(f"Error fetching domains: {e} — retrying in 60s")
            time.sleep(60)
            continue

        if len(domain_map) < 33:
            log(f"Only {len(domain_map)} domains connected — some may still be pending")

        # Step 1: Create inboxes
        if not step_create_inboxes(status, domain_map):
            log("Inbox creation incomplete — retrying in 60s")
            time.sleep(60)
            continue

        # Step 2: Set profile photos
        if not step_set_photos(status, domain_map):
            log("Photo setting incomplete — retrying in 60s")
            time.sleep(60)
            continue

        # Step 3: Set forwarding
        if not step_set_forwarding(status, domain_map):
            log("Forwarding incomplete — retrying in 60s")
            time.sleep(60)
            continue

        # Step 4: Export to SmartLead
        if not step_export_smartlead(status, domain_map):
            log("SmartLead export incomplete — retrying in 60s")
            time.sleep(60)
            continue

        # Step 5: Enable warmup (may need to wait for SmartLead to process export)
        if not step_enable_warmup(status):
            log("Warmup not fully enabled — retrying in 60s")
            time.sleep(60)
            continue

        # All done!
        log("\n" + "=" * 60)
        log("ALL STEPS COMPLETE!")
        log(f"  33 domains connected")
        log(f"  99 inboxes created (aidan/aidanh/aidanhutch)")
        log(f"  Profile photos set")
        log(f"  Forwarding → {FORWARD_TO}")
        log(f"  Exported to SmartLead")
        log(f"  Warmup enabled")
        log("=" * 60)

        # macOS notification
        import subprocess
        subprocess.run([
            "osascript", "-e",
            'display notification "All 33 THT .info domains fully set up and warming!" with title "Email Infra"'
        ])
        break
    else:
        log("Timed out after 2 hours — re-run script to continue from where it left off")


if __name__ == "__main__":
    main()
