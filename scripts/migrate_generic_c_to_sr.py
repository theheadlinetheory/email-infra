#!/usr/bin/env python3
"""Migrate Generic C inboxes (69 .co accounts) to 4 SR acquisition groups.

Splits 23 domains (3 inboxes each = 69 accounts) from the Acquisition Inboxes
client into SR-A through SR-D groups using tags only (no new clients).

Creates tags: SR-A Group (250/day), SR-B Group (250/day), etc.
Tags each account with: [Acquisition Inbox, Zapmail, SR-X Group (250/day)]
Saves domain->group mapping to clients/sr_groups.json for dashboard use.

Usage:
  python3 migrate_generic_c_to_sr.py          # Dry run (default)
  python3 migrate_generic_c_to_sr.py --apply  # Actually apply tags
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from setup import (
    sl_list_accounts, sl_get_all_tags, sl_find_or_create_tag,
    sl_create_tag, sl_pick_unique_color, sl_tag_account, log,
    SMARTLEAD_JWT,
)

ACQUISITION_INBOXES_CLIENT_ID = 328152
ACQUISITION_INBOX_TAG_ID = 268568
ZAPMAIL_TAG_ID = 262254
GENERIC_C_TAG_ID = 359383

SR_GROUPS = [
    "SR-A Group (250/day)",
    "SR-B Group (250/day)",
    "SR-C Group (250/day)",
    "SR-D Group (250/day)",
]


def get_co_accounts():
    """Fetch all .co domain accounts from Acquisition Inboxes client."""
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

    co_accounts = [
        a for a in all_accounts
        if a.get("client_id") == ACQUISITION_INBOXES_CLIENT_ID
        and a.get("from_email", "").endswith(".co")
    ]
    return co_accounts


def group_by_domain(accounts):
    """Group accounts by domain, returns {domain: [account, ...]}."""
    by_domain = {}
    for acc in accounts:
        email = acc.get("from_email", "")
        domain = email.split("@")[-1] if "@" in email else ""
        if domain:
            by_domain.setdefault(domain, []).append(acc)
    return by_domain


def assign_domains_to_groups(domains_sorted):
    """Split domains into 4 groups: 6, 6, 6, 5 — keeping all inboxes per domain together."""
    groups = {name: [] for name in SR_GROUPS}
    group_sizes = [6, 6, 6, 5]

    idx = 0
    for i, (group_name, size) in enumerate(zip(SR_GROUPS, group_sizes)):
        groups[group_name] = domains_sorted[idx:idx + size]
        idx += size

    return groups


def main():
    apply = "--apply" in sys.argv

    if not SMARTLEAD_JWT:
        log("SMARTLEAD_JWT not set in .env — cannot tag accounts.", "ERROR")
        sys.exit(1)

    # Step 1: Get the 69 .co accounts
    log("Fetching accounts from Acquisition Inboxes client...")
    co_accounts = get_co_accounts()
    by_domain = group_by_domain(co_accounts)
    domains_sorted = sorted(by_domain.keys())

    log(f"Found {len(co_accounts)} .co accounts across {len(domains_sorted)} domains")

    if len(domains_sorted) != 23:
        log(f"Expected 23 domains, got {len(domains_sorted)}. Aborting.", "ERROR")
        for d in domains_sorted:
            log(f"  {d}: {len(by_domain[d])} inboxes")
        sys.exit(1)

    # Step 2: Assign domains to groups
    domain_groups = assign_domains_to_groups(domains_sorted)

    log("\n=== Group Assignments ===")
    for group_name, group_domains in domain_groups.items():
        inbox_count = sum(len(by_domain[d]) for d in group_domains)
        log(f"\n{group_name}: {len(group_domains)} domains, {inbox_count} inboxes")
        for d in group_domains:
            emails = [a["from_email"] for a in by_domain[d]]
            log(f"  {d}: {', '.join(sorted(emails))}")

    if not apply:
        log("\n[DRY RUN] No changes made. Run with --apply to execute.")
        # Still save the mapping for review
        _save_mapping(domain_groups)
        return

    # Step 3: Create/find SR tags (exact match only — no fuzzy, "SR-A" != "A Group")
    log("\nCreating SR group tags...")
    all_tags = sl_get_all_tags()
    sr_tag_ids = {}
    for group_name in SR_GROUPS:
        if group_name in all_tags:
            sr_tag_ids[group_name] = all_tags[group_name]["id"]
            log(f"  {group_name}: existing tag ID {sr_tag_ids[group_name]}")
        else:
            color = sl_pick_unique_color(all_tags)
            tag = sl_create_tag(group_name, color)
            tag_id = tag.get("id")
            sr_tag_ids[group_name] = tag_id
            all_tags[group_name] = {"id": tag_id, "name": group_name, "color": color}
            log(f"  {group_name}: created tag ID {tag_id}")

    # Step 4: Build warmup date tags (read from each account's warmup_created_at)
    log("\nResolving warmup date tags...")
    all_tags = sl_get_all_tags()  # Refresh after SR tag creation
    date_tag_cache = {}  # date_str -> tag_id

    def get_date_tag_id(acc):
        wd = (acc.get("warmup_details") or {}).get("warmup_created_at", "")
        if not wd:
            return None
        date_str = wd[:10]  # "YYYY-MM-DD"
        if date_str in date_tag_cache:
            return date_tag_cache[date_str]
        # Format as M/D/YY to match existing tag convention
        from datetime import datetime as dt
        parsed = dt.strptime(date_str, "%Y-%m-%d")
        tag_name = f"{parsed.month}/{parsed.day}/{str(parsed.year)[2:]}"
        if tag_name in all_tags:
            tag_id = all_tags[tag_name]["id"]
        else:
            tag = sl_create_tag(tag_name)
            tag_id = tag.get("id")
            all_tags[tag_name] = {"id": tag_id, "name": tag_name}
        date_tag_cache[date_str] = tag_id
        log(f"  Date tag '{tag_name}' -> ID {tag_id}")
        return tag_id

    # Step 5: Tag each account [Acquisition Inbox, Zapmail, date, SR group]
    log("\nTagging accounts...")
    success = 0
    fail = 0
    for group_name, group_domains in domain_groups.items():
        group_tag_id = sr_tag_ids[group_name]

        for domain in group_domains:
            for acc in by_domain[domain]:
                acc_id = acc["id"]
                email = acc["from_email"]
                date_tag_id = get_date_tag_id(acc)
                tag_ids = [ACQUISITION_INBOX_TAG_ID, ZAPMAIL_TAG_ID, group_tag_id]
                if date_tag_id:
                    tag_ids.insert(2, date_tag_id)
                result = sl_tag_account(acc_id, tag_ids, client_id=ACQUISITION_INBOXES_CLIENT_ID)
                if result.get("ok"):
                    success += 1
                    log(f"  Tagged {email} -> {group_name}")
                else:
                    fail += 1
                    log(f"  FAILED {email}: {result}", "WARN")
                time.sleep(0.15)  # Rate limit

    log(f"\nDone: {success} tagged, {fail} failed")

    # Step 6: Save mapping
    _save_mapping(domain_groups, sr_tag_ids)


def _save_mapping(domain_groups, sr_tag_ids=None):
    """Save domain->group mapping for dashboard use."""
    mapping = {
        "groups": {},
        "domain_to_group": {},
    }
    for group_name, group_domains in domain_groups.items():
        mapping["groups"][group_name] = {
            "domains": group_domains,
            "tag_id": sr_tag_ids.get(group_name) if sr_tag_ids else None,
            "target_daily_volume": 250,
        }
        for d in group_domains:
            mapping["domain_to_group"][d] = group_name

    path = os.path.join(os.path.dirname(__file__), "clients", "sr_groups.json")
    with open(path, "w") as f:
        json.dump(mapping, f, indent=2)
    log(f"\nSaved domain mapping to {path}")


if __name__ == "__main__":
    main()
