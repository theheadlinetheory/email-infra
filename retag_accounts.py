#!/usr/bin/env python3
"""Remediation script: re-apply inbox tags to accounts that lost them
when assign_clients.py set clientId without preserving tags.

Reads client configs to build domain -> (client_name, date_tag) mapping,
then re-tags each account with [client_name, "Zapmail", date_tag] while
preserving the existing clientId.

Usage:
  python3 retag_accounts.py          # Dry run (default)
  python3 retag_accounts.py --apply  # Actually apply tags
"""

import json
import glob
import re
import sys
import os
import time

sys.path.insert(0, os.path.dirname(__file__))
from setup import (
    sl_list_accounts, sl_get_all_tags, sl_find_or_create_tag,
    sl_internal_headers, sl_tag_account,
    SMARTLEAD_API, SMARTLEAD_KEY, SMARTLEAD_INTERNAL_API, SMARTLEAD_JWT,
    log,
)


def build_domain_tag_map():
    """Build domain -> (client_name, date_tag_name) from config files."""
    domain_map = {}
    configs = glob.glob(os.path.join(os.path.dirname(__file__), "clients/*.json"))
    for path in sorted(configs):
        try:
            c = json.load(open(path))
        except Exception:
            continue
        name = c.get("client_name", "")
        if not name or name == "TEST-Run":
            continue

        # Extract date from filename: _YYYYMMDD.json
        m = re.search(r"_(\d{4})(\d{2})(\d{2})\.json", path)
        if not m:
            continue
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        date_tag = f"{mo}/{d}/{str(y)[2:]}"

        for dom in c.get("purchased_domains", []):
            domain_map[dom["domain"]] = (name, date_tag)

    return domain_map


def main():
    apply = "--apply" in sys.argv
    mode = "APPLY" if apply else "DRY RUN"
    print(f"=== Re-tag Accounts ({mode}) ===\n")

    if not SMARTLEAD_JWT:
        print("ERROR: SMARTLEAD_JWT not set in .env")
        sys.exit(1)

    # Step 1: Build domain -> (client, date) map
    domain_map = build_domain_tag_map()
    print(f"Mapped {len(domain_map)} domains from config files")

    # Step 2: Get all existing SmartLead tags
    existing_tags = sl_get_all_tags()
    print(f"Found {len(existing_tags)} existing tags in SmartLead")

    # Step 3: Pre-resolve all needed tag IDs (client names + dates + Zapmail)
    needed_tags = set()
    for client_name, date_tag in domain_map.values():
        needed_tags.add(client_name)
        needed_tags.add(date_tag)
    needed_tags.add("Zapmail")

    tag_id_cache = {}
    colors = {
        "Zapmail": "#B1FCB3",
    }
    # Default colors for client vs date tags
    for tag_name in sorted(needed_tags):
        if tag_name in tag_id_cache:
            continue
        color = colors.get(tag_name)
        if color is None:
            # Date tags get green, client tags get blue
            color = "#D0FCB1" if re.match(r"\d+/\d+/\d+", tag_name) else "#B1C4FC"

        if apply:
            tag_id = sl_find_or_create_tag(tag_name, color, existing_tags)
        else:
            # In dry run, just look up existing
            if tag_name in existing_tags:
                tag_id = existing_tags[tag_name]["id"]
            else:
                tag_id = f"NEW({tag_name})"
        tag_id_cache[tag_name] = tag_id

    print(f"Resolved {len(tag_id_cache)} tag IDs")
    for name, tid in sorted(tag_id_cache.items()):
        print(f"  {name}: {tid}")

    # Step 4: Fetch all accounts
    print("\nFetching all SmartLead accounts...")
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
    print(f"Found {len(all_accounts)} total accounts")

    # Step 5: Re-tag accounts
    tagged = 0
    skipped = 0
    unmatched = 0

    for acc in all_accounts:
        email = acc.get("from_email", "")
        domain = email.split("@")[-1] if "@" in email else ""
        acc_id = acc["id"]
        client_id = acc.get("client_id")

        mapping = domain_map.get(domain)
        if not mapping:
            unmatched += 1
            continue

        client_name, date_tag = mapping
        client_tag_id = tag_id_cache.get(client_name)
        zapmail_tag_id = tag_id_cache.get("Zapmail")
        date_tag_id = tag_id_cache.get(date_tag)

        tag_ids = [t for t in [client_tag_id, zapmail_tag_id, date_tag_id] if t]

        if not tag_ids:
            print(f"  SKIP (no tags resolved): {email}")
            skipped += 1
            continue

        if apply:
            result = sl_tag_account(acc_id, tag_ids, client_id=client_id)
            if isinstance(result, dict) and result.get("ok"):
                tagged += 1
            else:
                print(f"  FAIL: {email} -> {result}")
            time.sleep(0.15)
        else:
            tagged += 1
            if tagged <= 10:
                print(f"  Would tag: {email} -> tags={tag_ids} clientId={client_id}")
            elif tagged == 11:
                print(f"  ... (showing first 10 only)")

    print(f"\n{'APPLIED' if apply else 'WOULD APPLY'}:")
    print(f"  Tagged: {tagged}")
    print(f"  Skipped: {skipped}")
    print(f"  Unmatched (no config): {unmatched}")
    print(f"  Total: {tagged + skipped + unmatched}")

    if not apply and tagged > 0:
        print(f"\nRun with --apply to execute: python3 retag_accounts.py --apply")


if __name__ == "__main__":
    main()
