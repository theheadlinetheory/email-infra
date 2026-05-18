#!/usr/bin/env python3
"""Create missing mailboxes on incomplete generic domains.

Finds domains with < 3 accounts, creates missing inboxes in Zapmail,
exports to SmartLead, tags correctly, and updates Supabase.

Usage: python3 complete_incomplete_domains.py [--dry-run]
"""

from __future__ import annotations

import json
import re
import sys
import time
import requests
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from setup import (
    SMARTLEAD_API, SMARTLEAD_KEY,
    INBOX_SPECS,
    zm_list_domains, zm_create_mailboxes, zm_export_mailboxes,
    zm_update_mailbox,
    sl_get_all_tags, sl_find_or_create_tag, sl_tag_account,
    sl_list_accounts,
    _RateLimiter,
    PROFILE_PHOTO_URL,
)
from tag_utils import ZAPMAIL_TAG_ID
import db as store

DRY_RUN = "--dry-run" in sys.argv
_sl_rate = _RateLimiter(max_requests=150, window_seconds=60)
RATE_LIMIT_COOLDOWN = 45
ALL_USERNAMES = {"s.reynolds", "sean.r", "sean.reynolds"}

SPEC_BY_USERNAME = {s["mailboxUsername"]: s for s in INBOX_SPECS}


def log(msg):
    print(msg, flush=True)


def get_all_sl_accounts():
    accounts = []
    offset = 0
    while True:
        _sl_rate.wait()
        url = f"{SMARTLEAD_API}/email-accounts/?offset={offset}&limit=100&api_key={SMARTLEAD_KEY}"
        r = requests.get(url, timeout=30)
        if r.status_code == 429:
            time.sleep(RATE_LIMIT_COOLDOWN)
            continue
        batch = r.json() if r.status_code == 200 and isinstance(r.json(), list) else []
        accounts.extend(batch)
        if len(batch) < 100:
            break
        offset += 100
    return accounts


def find_incomplete_domains():
    """Find generic domains with < 3 accounts in SmartLead."""
    log("Fetching SmartLead accounts...")
    all_accounts = get_all_sl_accounts()
    clients_raw = requests.get(f"{SMARTLEAD_API}/client?api_key={SMARTLEAD_KEY}", timeout=30).json()
    client_map = {c["id"]: c["name"] for c in clients_raw}

    k_b2 = store.get_inbox_group("GEN-K", batch=2)
    k_b2_ids = set(k_b2.get("account_ids", []) if k_b2 else [])
    canopy_ids = set()
    for ig in store.get_all_inbox_groups():
        if "canopy" in ig.get("smartlead_client_name", "").lower():
            canopy_ids.update(ig.get("account_ids") or [])
    exclude = k_b2_ids | canopy_ids

    by_domain = defaultdict(list)
    for acc in all_accounts:
        if acc["id"] in exclude:
            continue
        cn = client_map.get(acc.get("client_id"), "")
        if not cn.lower().startswith("generic"):
            continue
        domain = acc.get("from_email", "").split("@")[-1]
        by_domain[domain].append(acc)

    incomplete = {}
    for domain, accs in sorted(by_domain.items()):
        if len(accs) >= 3:
            continue
        present = {a.get("from_email", "").split("@")[0] for a in accs}
        missing = ALL_USERNAMES - present
        if missing:
            group = client_map.get(accs[0].get("client_id"), "unknown")
            if group == "Generic G2":
                group = "Generic G"
            incomplete[domain] = {
                "missing_usernames": sorted(missing),
                "group": group,
                "present_count": len(accs),
            }

    return incomplete


def create_missing_mailboxes(incomplete, zm_domains):
    """Create missing mailboxes in Zapmail. Returns list of created mailbox IDs."""
    zm_by_name = {d.get("domain", ""): d for d in zm_domains}
    created_mb_ids = []

    for domain, info in sorted(incomplete.items()):
        zm_domain = zm_by_name.get(domain)
        if not zm_domain:
            log(f"  WARNING: {domain} not found in Zapmail — skipping")
            continue

        domain_id = zm_domain["id"]
        specs = [SPEC_BY_USERNAME[u] for u in info["missing_usernames"]]

        log(f"  {domain}: creating {len(specs)} mailboxes ({', '.join(info['missing_usernames'])})")
        if DRY_RUN:
            continue

        result = zm_create_mailboxes(domain_id, domain, specs)
        log(f"    Result: {str(result)[:200]}")

        if isinstance(result, dict) and "data" in result:
            for mb in result["data"]:
                if isinstance(mb, dict) and "id" in mb:
                    created_mb_ids.append(mb["id"])
        time.sleep(2)

    return created_mb_ids


def wait_for_active(domains, zm_domains_fn):
    """Poll until all mailboxes on the given domains are ACTIVE."""
    log("Waiting for mailboxes to reach ACTIVE status...")
    poll = 0
    while True:
        poll += 1
        try:
            all_zm = zm_domains_fn()
        except Exception as e:
            log(f"  Poll {poll}: API error ({e}), retrying in 2 min...")
            time.sleep(120)
            continue

        zm_by_name = {d.get("domain", ""): d for d in all_zm}
        active = 0
        total = 0
        all_mb_ids = []

        for domain in domains:
            d = zm_by_name.get(domain)
            if d:
                for m in d.get("mailboxes", []):
                    total += 1
                    if m.get("status") == "ACTIVE":
                        active += 1
                    all_mb_ids.append(m["id"])

        log(f"  Poll {poll}: {active}/{total} ACTIVE")
        if active == total and total > 0:
            return all_mb_ids
        time.sleep(120)


def set_profile_photos(mb_ids, zm_domains):
    """Set profile photos on new mailboxes that don't have one."""
    zm_by_name = {d.get("domain", ""): d for d in zm_domains}
    count = 0
    for d in zm_domains:
        for mb in d.get("mailboxes", []):
            if mb["id"] in mb_ids and not mb.get("profilePicture"):
                try:
                    zm_update_mailbox(mb["id"], {"profilePicture": PROFILE_PHOTO_URL})
                    count += 1
                    time.sleep(1)
                except Exception as e:
                    log(f"  Photo error for {mb.get('email', mb['id'])}: {e}")
    log(f"  Set profile photos on {count} mailboxes")


def export_and_verify(domains):
    """Export new mailboxes to SmartLead and verify they appear."""
    log("Exporting to SmartLead...")
    for domain in domains:
        zm_export_mailboxes(apps=["SMARTLEAD"], contains=domain)
        time.sleep(2)

    log("  Waiting 3 minutes for export to process...")
    time.sleep(180)

    log("Verifying accounts in SmartLead...")
    for attempt in range(3):
        found = {}
        offset = 0
        while True:
            _sl_rate.wait()
            batch = sl_list_accounts(offset=offset, limit=100)
            if batch is None:
                time.sleep(RATE_LIMIT_COOLDOWN)
                continue
            if not batch:
                break
            for acc in batch:
                email = acc.get("from_email", "")
                d = email.split("@")[-1] if "@" in email else ""
                if d in domains:
                    found[email] = acc["id"]
            if len(batch) < 100:
                break
            offset += 100

        expected = len(domains) * 3
        log(f"  Attempt {attempt + 1}: found {len(found)}/{expected} accounts on target domains")

        if len(found) >= expected:
            return found

        if attempt < 2:
            log("  Re-exporting missing domains...")
            found_domains = {e.split("@")[-1] for e in found}
            for d in domains:
                if d not in found_domains or len([e for e in found if e.endswith(d)]) < 3:
                    zm_export_mailboxes(apps=["SMARTLEAD"], contains=d)
                    time.sleep(2)
            log("  Waiting 3 minutes...")
            time.sleep(180)

    return found


def tag_new_accounts(found_accounts, incomplete, all_tags):
    """Tag newly created accounts with Zapmail + GroupTag + WarmupDate."""
    log("Tagging new accounts...")
    errors = 0
    tagged = 0

    for email, acc_id in sorted(found_accounts.items()):
        domain = email.split("@")[-1]
        username = email.split("@")[0]
        if domain not in incomplete:
            continue
        if username not in incomplete[domain]["missing_usernames"]:
            continue

        group_name = incomplete[domain]["group"]
        group_tag_id = sl_find_or_create_tag(group_name, existing_tags=all_tags)
        all_tags[group_name] = {"id": group_tag_id, "name": group_name}

        tag_ids = [ZAPMAIL_TAG_ID, group_tag_id]

        for attempt in range(3):
            _sl_rate.wait()
            try:
                sl_tag_account(acc_id, tag_ids)
                tagged += 1
                log(f"  {email} -> {group_name}")
                break
            except Exception as e:
                if "429" in str(e) and attempt < 2:
                    time.sleep(RATE_LIMIT_COOLDOWN)
                else:
                    log(f"  ERROR tagging {email}: {e}")
                    errors += 1
                    break

    log(f"  Tagged {tagged} new accounts, {errors} errors")
    return tagged, errors


def update_supabase(incomplete, found_accounts):
    """Add new account IDs to Supabase inbox_groups."""
    log("Updating Supabase inbox_groups...")
    ig_all = store.get_all_inbox_groups()
    ig_by_tag = {}
    for ig in ig_all:
        tag = ig.get("group_tag") or ig.get("smartlead_client_name", "")
        if tag.lower().startswith("generic"):
            ig_by_tag[tag] = ig

    for domain, info in incomplete.items():
        group = info["group"]
        ig = ig_by_tag.get(group)
        if not ig:
            log(f"  WARNING: No Supabase record for {group}")
            continue

        current_ids = ig.get("account_ids") or []
        current_emails = ig.get("account_emails") or []
        current_domains = ig.get("domains") or []

        new_ids = list(current_ids)
        new_emails = list(current_emails)
        added = 0

        for username in info["missing_usernames"]:
            email = f"{username}@{domain}"
            acc_id = found_accounts.get(email)
            if acc_id and acc_id not in new_ids:
                new_ids.append(acc_id)
                new_emails.append(email)
                added += 1

        if domain not in current_domains:
            current_domains.append(domain)

        if added > 0:
            store.update_inbox_group(ig["id"],
                account_ids=sorted(new_ids),
                account_emails=sorted(new_emails),
                domains=sorted(current_domains),
            )
            log(f"  {group}: added {added} accounts for {domain}")


def main():
    incomplete = find_incomplete_domains()
    if not incomplete:
        log("All generic domains are complete (3 accounts each)!")
        return

    log(f"\nFound {len(incomplete)} incomplete domains:")
    total_missing = 0
    for domain, info in sorted(incomplete.items()):
        log(f"  {domain}: {info['present_count']}/3, missing {info['missing_usernames']} ({info['group']})")
        total_missing += len(info["missing_usernames"])
    log(f"Total missing: {total_missing} accounts")

    if DRY_RUN:
        log("\n=== DRY RUN — no changes made ===")
        return

    # Step 1: Create missing mailboxes in Zapmail
    log("\n--- Step 1: Creating missing mailboxes ---")
    zm_domains = zm_list_domains()
    created = create_missing_mailboxes(incomplete, zm_domains)
    log(f"  Created {len(created)} mailboxes")

    # Step 2: Wait for ACTIVE status
    log("\n--- Step 2: Waiting for ACTIVE status ---")
    target_domains = list(incomplete.keys())
    all_mb_ids = wait_for_active(target_domains, zm_list_domains)

    # Step 3: Set profile photos
    log("\n--- Step 3: Setting profile photos ---")
    fresh_zm = zm_list_domains()
    new_mb_ids = set()
    zm_by_name = {d.get("domain", ""): d for d in fresh_zm}
    for domain, info in incomplete.items():
        d = zm_by_name.get(domain)
        if d:
            for mb in d.get("mailboxes", []):
                username = mb.get("email", "").split("@")[0]
                if username in info["missing_usernames"]:
                    new_mb_ids.add(mb["id"])
    set_profile_photos(new_mb_ids, fresh_zm)

    # Step 4: Export to SmartLead
    log("\n--- Step 4: Exporting to SmartLead ---")
    found = export_and_verify(target_domains)

    # Step 5: Tag new accounts
    log("\n--- Step 5: Tagging new accounts ---")
    all_tags = sl_get_all_tags()
    tagged, errors = tag_new_accounts(found, incomplete, all_tags)

    # Step 6: Update Supabase
    log("\n--- Step 6: Updating Supabase ---")
    update_supabase(incomplete, found)

    log(f"\nComplete! {tagged} accounts created and tagged, {errors} errors.")


if __name__ == "__main__":
    main()
