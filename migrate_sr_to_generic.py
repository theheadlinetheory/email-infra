#!/usr/bin/env python3
"""Migrate SR acquisition groups back to generic client groups.

SR-A + SR-B (12 domains, 36 inboxes) → Generic J
SR-C + SR-D (11 domains, 33 inboxes) → Generic K

Steps per group:
  1. Create SmartLead client (Generic J / Generic K)
  2. Create SmartLead tag (Generic J / Generic K)
  3. Re-tag accounts: remove SR tags, apply [Zapmail, group tag, warmup date]
  4. Reassign client_id from Acquisition Inboxes to new generic client
  5. Enable warmup on all accounts

Usage:
  python3 migrate_sr_to_generic.py          # Dry run
  python3 migrate_sr_to_generic.py --apply  # Execute
"""

import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))
from setup import (
    sl_list_accounts, sl_get_all_tags, sl_find_or_create_tag,
    sl_create_tag, sl_pick_unique_color, sl_tag_account,
    SMARTLEAD_API, SMARTLEAD_KEY, SMARTLEAD_JWT, log,
)
import requests

ACQUISITION_CLIENT_ID = 328152
ZAPMAIL_TAG_ID = 262254

SR_GROUPS_FILE = os.path.join(os.path.dirname(__file__), "clients", "sr_groups.json")

MERGE_MAP = {
    "Generic J": ["SR-A Group (250/day)", "SR-B Group (250/day)"],
    "Generic K": ["SR-C Group (250/day)", "SR-D Group (250/day)"],
}

WARMUP_CONFIG = {
    "warmup_enabled": True,
    "total_warmup_per_day": 30,
    "daily_rampup": 5,
    "reply_rate_percentage": 30,
}


def load_sr_mapping():
    with open(SR_GROUPS_FILE) as f:
        return json.load(f)


def get_sr_accounts(all_accounts, sr_mapping):
    """Get all accounts belonging to SR groups, grouped by SR group name."""
    sr_domains = set(sr_mapping["domain_to_group"].keys())
    by_group = {}
    for a in all_accounts:
        email = a.get("from_email", "")
        domain = email.split("@")[-1] if "@" in email else ""
        if domain in sr_domains:
            group = sr_mapping["domain_to_group"][domain]
            by_group.setdefault(group, []).append(a)
    return by_group


def create_smartlead_client(name):
    """Create a SmartLead client, return client ID."""
    slug = name.lower().replace(" ", "")
    resp = requests.post(
        f"{SMARTLEAD_API}/client/save",
        params={"api_key": SMARTLEAD_KEY},
        json={
            "name": name,
            "email": f"tht.{slug}.client@gmail.com",
            "password": "THTclient2026!",
        },
        timeout=30,
    )
    if resp.status_code in (200, 201):
        client_id = resp.json().get("clientId")
        log(f"  Created SmartLead client '{name}' -> ID {client_id}")
        return client_id
    else:
        log(f"  FAILED to create client '{name}': {resp.text[:200]}", "ERROR")
        return None


def find_smartlead_client(name):
    """Find existing SmartLead client by name."""
    for attempt in range(3):
        try:
            resp = requests.get(
                f"{SMARTLEAD_API}/client",
                params={"api_key": SMARTLEAD_KEY},
                timeout=30,
            )
            if resp.status_code != 200:
                time.sleep(5)
                continue
            clients = resp.json()
            for c in clients:
                if c["name"].lower().strip() == name.lower().strip():
                    return c["id"]
            return None
        except Exception as e:
            log(f"  Client lookup attempt {attempt + 1} failed: {e}", "WARN")
            time.sleep(5)
    return None


def enable_warmup(account_id):
    """Enable warmup on a single account."""
    resp = requests.post(
        f"{SMARTLEAD_API}/email-accounts/{account_id}/warmup",
        params={"api_key": SMARTLEAD_KEY},
        json=WARMUP_CONFIG,
        timeout=15,
    )
    return resp.status_code == 200


def main():
    apply = "--apply" in sys.argv

    if not SMARTLEAD_JWT:
        log("SMARTLEAD_JWT not set — cannot tag accounts.", "ERROR")
        sys.exit(1)

    sr_mapping = load_sr_mapping()
    log("Loaded SR group mapping")

    # Fetch all accounts
    log("Fetching all SmartLead accounts...")
    all_accounts = []
    offset = 0
    while True:
        batch = sl_list_accounts(limit=100, offset=offset)
        if not batch:
            break
        all_accounts.extend(batch)
        if len(batch) < 100:
            break
        offset += 100
    log(f"Total accounts: {len(all_accounts)}")

    sr_by_group = get_sr_accounts(all_accounts, sr_mapping)

    # Preview
    log("\n" + "=" * 70)
    log("MIGRATION PLAN")
    log("=" * 70)

    for generic_name, sr_groups in MERGE_MAP.items():
        merged_accounts = []
        for sg in sr_groups:
            merged_accounts.extend(sr_by_group.get(sg, []))

        domains = sorted(set(
            a["from_email"].split("@")[-1]
            for a in merged_accounts
            if "@" in a.get("from_email", "")
        ))
        daily_cap = sum(a.get("message_per_day", 0) or 0 for a in merged_accounts)

        log(f"\n{' + '.join(sr_groups)} → {generic_name}")
        log(f"  {len(domains)} domains, {len(merged_accounts)} inboxes, {daily_cap}/day capacity")
        for d in domains:
            d_accts = [a for a in merged_accounts if a["from_email"].endswith(f"@{d}")]
            emails = sorted(a["from_email"] for a in d_accts)
            log(f"    {d}: {', '.join(emails)}")

    if not apply:
        log("\n[DRY RUN] No changes made. Run with --apply to execute.")
        return

    # Execute
    log("\n" + "=" * 70)
    log("EXECUTING MIGRATION")
    log("=" * 70)

    all_tags = sl_get_all_tags()

    for generic_name, sr_groups in MERGE_MAP.items():
        log(f"\n--- {generic_name} ---")

        # 1. Create or find SmartLead client
        client_id = find_smartlead_client(generic_name)
        if client_id:
            log(f"  SmartLead client '{generic_name}' already exists -> ID {client_id}")
        else:
            client_id = create_smartlead_client(generic_name)
            if not client_id:
                log(f"  Skipping {generic_name} — client creation failed", "ERROR")
                continue

        # 2. Create or find tag
        group_tag_id = sl_find_or_create_tag(generic_name, existing_tags=all_tags)
        log(f"  Tag '{generic_name}' -> ID {group_tag_id}")
        all_tags = sl_get_all_tags()  # refresh

        # 3. Merge accounts
        merged_accounts = []
        for sg in sr_groups:
            merged_accounts.extend(sr_by_group.get(sg, []))

        log(f"  Processing {len(merged_accounts)} accounts...")

        # Build warmup date tag cache
        date_tag_cache = {}

        success = 0
        fail = 0
        warmup_ok = 0
        warmup_fail = 0

        for acc in merged_accounts:
            acc_id = acc["id"]
            email = acc["from_email"]

            # Resolve warmup date tag
            wd = (acc.get("warmup_details") or {}).get("warmup_created_at", "")
            date_tag_id = None
            if wd:
                date_str = wd[:10]
                if date_str not in date_tag_cache:
                    parsed = datetime.strptime(date_str, "%Y-%m-%d")
                    tag_name = f"{parsed.month}/{parsed.day}/{str(parsed.year)[2:]}"
                    date_tag_cache[date_str] = sl_find_or_create_tag(tag_name, existing_tags=all_tags)
                date_tag_id = date_tag_cache[date_str]

            # Tag: [Zapmail, group, date]
            tag_ids = [ZAPMAIL_TAG_ID, group_tag_id]
            if date_tag_id:
                tag_ids.append(date_tag_id)

            result = sl_tag_account(acc_id, tag_ids, client_id=client_id)
            if result.get("ok"):
                success += 1
            else:
                fail += 1
                log(f"    FAILED tag {email}: {result}", "WARN")
            time.sleep(0.15)

            # Enable warmup
            if enable_warmup(acc_id):
                warmup_ok += 1
            else:
                warmup_fail += 1
                log(f"    FAILED warmup {email}", "WARN")
            time.sleep(0.1)

        log(f"  Tags: {success} ok, {fail} failed")
        log(f"  Warmup: {warmup_ok} enabled, {warmup_fail} failed")

    log("\n" + "=" * 70)
    log("MIGRATION COMPLETE")
    log("=" * 70)


if __name__ == "__main__":
    main()
