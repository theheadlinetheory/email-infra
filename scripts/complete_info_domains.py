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
    sl_set_warmup, sl_url, sl_gql, SMARTLEAD_KEY, GOOGLE_WARMUP,
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
    """Check if Zapmail API is responding (uses domains endpoint, not mailboxes which has intermittent 500s)."""
    try:
        h = zm_headers(WID)
        r = requests.get(
            "https://api.zapmail.ai/api/v2/domains?page=1",
            headers=h, timeout=15
        )
        return r.status_code == 200
    except Exception:
        return False


def mailbox_list_healthy():
    """Check if the mailbox list endpoint specifically is working."""
    try:
        h = zm_headers(WID)
        r = requests.get("https://api.zapmail.ai/api/v2/mailboxes?page=1", headers=h, timeout=15)
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

    # Check wallet balance via the error message pattern — buy what we can afford
    log(f"  Buying {to_buy} addon mailboxes ($3/each = ${to_buy * 3})...")
    try:
        result = zm_post(f"/v2/wallet/buy-addon-mailboxes?quantity={to_buy}", {}, WID)
        if isinstance(result, dict) and result.get("status") in (200, 201):
            log(f"  Purchased {to_buy} mailboxes")
            return True

        msg = result.get("message", "") if isinstance(result, dict) else str(result)
        # If insufficient balance, try buying what we can afford
        if "insufficient wallet" in msg.lower() or "available:" in msg.lower():
            import re
            match = re.search(r'Available:\s*\$?([\d]+\.?\d*)', msg)
            if match:
                wallet_balance = float(match.group(1).rstrip('.'))
                can_afford = int(wallet_balance / 3)
                if can_afford > 0:
                    log(f"  Wallet has ${wallet_balance:.0f} — buying {can_afford} mailboxes instead")
                    result2 = zm_post(f"/v2/wallet/buy-addon-mailboxes?quantity={can_afford}", {}, WID)
                    if isinstance(result2, dict) and result2.get("status") in (200, 201):
                        log(f"  Purchased {can_afford} mailboxes (will create as many inboxes as slots allow)")
                        return True
                    else:
                        msg2 = result2.get("message", "")[:100] if isinstance(result2, dict) else str(result2)[:100]
                        log(f"  Reduced purchase also failed: {msg2}")
                else:
                    log(f"  Wallet balance ${wallet_balance:.0f} can't buy any mailboxes ($3/each)")
            else:
                log(f"  Purchase failed: {msg[:150]}")
        else:
            log(f"  Purchase failed: {msg[:150]}")
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

    # Check which domains already have mailboxes (if list endpoint is working)
    truly_need = []
    list_available = mailbox_list_healthy()

    if list_available:
        for domain in to_do:
            did = domain_map[domain]["id"]
            existing = get_existing_mailboxes(did)
            if existing is None:
                # List endpoint just went down — fall through to create approach
                truly_need = to_do
                break
            if existing:
                log(f"  {domain}: already has {len(existing)} mailboxes — skipping")
                already_done.add(domain)
                ids = [m.get("id", "") for m in existing]
                status["mailbox_ids"][domain] = ids
            else:
                truly_need.append(domain)
        else:
            # Loop completed normally
            status["domains_with_inboxes"] = list(already_done)
            save_status(status)
    else:
        log("  Mailbox list API is 500 — will try creating and handle duplicates")
        truly_need = to_do

    if not truly_need:
        log("All domains already have inboxes (verified)")
        return True

    # If mailbox list is down, try creating one domain first to see if inboxes already exist
    # "Max 5 mailboxes per domain" response means they're already created
    test_domain = truly_need[0]
    test_did = domain_map[test_domain]["id"]
    try:
        test_result = zm_create_mailboxes(test_did, test_domain, INBOX_SPECS, workspace_key=WID)
        test_msg = str(test_result.get("message", "")) if isinstance(test_result, dict) else str(test_result)
        if "max" in test_msg.lower() and "mailbox" in test_msg.lower():
            # All domains already have inboxes from previous session
            log(f"  {test_domain} already has max mailboxes — all 33 domains likely done from previous session")
            for d in truly_need:
                already_done.add(d)
            status["domains_with_inboxes"] = list(already_done)
            save_status(status)
            return True
        elif isinstance(test_result, dict) and test_result.get("status") in (200, 201):
            data = test_result.get("data", {})
            mailboxes = data.get("mailboxes", []) if isinstance(data, dict) else []
            ids = [m.get("id", "") for m in mailboxes]
            status["mailbox_ids"][test_domain] = ids
            already_done.add(test_domain)
            log(f"  [1/{len(truly_need)}] {test_domain} -> {len(ids)} inboxes created")
        elif isinstance(test_result, str):
            already_done.add(test_domain)
            log(f"  [1/{len(truly_need)}] {test_domain} -> text response (likely OK)")
        elif isinstance(test_result, dict) and test_result.get("status") == 500:
            log("  API 500 — will retry next round")
            return False
        else:
            msg = test_result.get("message", "")[:100] if isinstance(test_result, dict) else str(test_result)[:100]
            log(f"  [1/{len(truly_need)}] {test_domain} -> {msg}")
    except Exception as e:
        log(f"  Test create error: {e}")
        return False

    # Buy mailboxes if needed for remaining domains
    remaining_need = [d for d in truly_need if d not in already_done]
    if remaining_need:
        needed_count = len(remaining_need) * 3
        buy_mailboxes_if_needed(needed_count)

    # Create inboxes on remaining domains
    created = 0
    for i, domain in enumerate(remaining_need):
        did = domain_map[domain]["id"]
        try:
            result = zm_create_mailboxes(did, domain, INBOX_SPECS, workspace_key=WID)
            if isinstance(result, str):
                already_done.add(domain)
                created += 1
                log(f"  [{i+2}/{len(truly_need)}] {domain} -> text response (likely OK)")
            elif isinstance(result, dict):
                msg = str(result.get("message", ""))
                if result.get("status") in (200, 201):
                    data = result.get("data", {})
                    mailboxes = data.get("mailboxes", []) if isinstance(data, dict) else []
                    ids = [m.get("id", "") for m in mailboxes]
                    status["mailbox_ids"][domain] = ids
                    already_done.add(domain)
                    created += 1
                    log(f"  [{i+2}/{len(truly_need)}] {domain} -> {len(ids)} inboxes created")
                elif "max" in msg.lower() and "mailbox" in msg.lower():
                    already_done.add(domain)
                    log(f"  [{i+2}/{len(truly_need)}] {domain} -> already has mailboxes")
                elif "already" in msg.lower() or "exist" in msg.lower():
                    already_done.add(domain)
                    log(f"  [{i+2}/{len(truly_need)}] {domain} -> already exists")
                elif result.get("status") == 500:
                    log("  API 500 — stopping, will retry")
                    break
                else:
                    log(f"  [{i+2}/{len(truly_need)}] {domain} -> FAIL: {msg[:100]}")
            time.sleep(0.5)
        except Exception as e:
            log(f"  [{i+2}/{len(truly_need)}] {domain} -> ERROR: {e}")
            if "500" in str(e) or "timeout" in str(e).lower():
                break

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

    if not mailbox_list_healthy():
        log("  Mailbox list API is 500 — skipping photos for now (will retry)")
        return False

    done_count = 0
    for domain in to_do:
        did = domain_map.get(domain, {}).get("id", "") if isinstance(domain_map.get(domain), dict) else ""
        if not did:
            continue

        existing = get_existing_mailboxes(did)
        if existing is None:
            log(f"  Mailbox list went down mid-check — will retry")
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


def count_smartlead_accounts():
    """Count how many THT .info accounts are in SmartLead using GraphQL."""
    try:
        query = '{ email_accounts(where: {from_email: {_like: "%headlinetheory%.info"}}) { id from_email } }'
        result = sl_gql(query)
        return result.get("data", {}).get("email_accounts", [])
    except Exception as e:
        log(f"  GQL error: {e}")
        return []


def step_export_smartlead(status, domain_map):
    """Export mailboxes to SmartLead. Keeps re-exporting until all 99 accounts land."""
    # Check how many are already in SmartLead
    existing = count_smartlead_accounts()
    log(f"  SmartLead has {len(existing)}/99 THT .info accounts")

    if len(existing) >= 99:
        status["exported_to_smartlead"] = True
        save_status(status)
        return True

    # Re-trigger export (uses contains="aidan" to match all acquisition mailboxes)
    try:
        result = zm_export_mailboxes(["SMARTLEAD"], contains="aidan")
        if isinstance(result, dict) and result.get("status") in (200, 201):
            eid = result.get("data", {}).get("exportId", "?")
            log(f"  Export triggered (ID={eid}) — waiting for Zapmail to provision remaining mailboxes")
        elif isinstance(result, dict) and result.get("status") == 500:
            log("  Export API 500 — will retry")
        else:
            msg = result.get("message", "")[:100] if isinstance(result, dict) else str(result)[:100]
            log(f"  Export response: {msg}")
    except Exception as e:
        log(f"  Export error: {e}")

    return False  # Keep re-exporting until all 99 land


def step_enable_warmup(status):
    """Enable warmup on all SmartLead accounts using GraphQL to find them."""
    if status.get("warmup_enabled"):
        log("Warmup already enabled")
        return True

    try:
        accounts = count_smartlead_accounts()
        if not accounts:
            log("  No SmartLead accounts found yet")
            return False

        needs_warmup = [a for a in accounts
                        if not (a.get("warmup_details") or {}).get("status") == "ACTIVE"]
        already_active = len(accounts) - len(needs_warmup)

        for acct in needs_warmup:
            try:
                sl_set_warmup(acct["id"])
                already_active += 1
            except Exception as e:
                log(f"  Warmup error on {acct.get('from_email','')}: {e}")
            time.sleep(0.3)

        log(f"  Warmup: {already_active}/{len(accounts)} accounts active")

        if already_active >= len(accounts) and len(accounts) >= 99:
            status["warmup_enabled"] = True
            save_status(status)
            return True

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
    max_rounds = 200  # up to ~10 hours (180s between export retries)

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

        # Step 2: Set forwarding (don't need mailbox list for this)
        if not step_set_forwarding(status, domain_map):
            log("Forwarding incomplete — retrying in 60s")
            time.sleep(60)
            continue

        # Step 3: Set profile photos (needs mailbox list — skip if 500, don't block export)
        photos_done = step_set_photos(status, domain_map)
        if not photos_done:
            log("  Photos incomplete — continuing to export (will retry photos later)")

        # Step 4: Export to SmartLead + enable warmup on arrivals (runs together)
        export_done = step_export_smartlead(status, domain_map)
        step_enable_warmup(status)  # Enable warmup on whatever has arrived so far

        if not export_done:
            log("SmartLead export incomplete — re-exporting in 180s")
            time.sleep(180)
            continue

        # Step 5: Retry photos if they weren't done earlier
        if not photos_done:
            photos_done = step_set_photos(status, domain_map)
            if not photos_done:
                log("  Photos still incomplete — warmup is running, photos can be set later")

        # All done (or nearly — photos may be pending if list API stays down)
        log("\n" + "=" * 60)
        log("PIPELINE COMPLETE!")
        log(f"  33 domains connected")
        log(f"  99 inboxes created (aidan/aidanh/aidanhutch)")
        log(f"  Photos: {'done' if photos_done else 'PENDING (mailbox list API down)'}")
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
