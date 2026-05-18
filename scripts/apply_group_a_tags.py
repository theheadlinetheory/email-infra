#!/usr/bin/env python3
"""Apply 'Group A' tag to all active client accounts.

Fetches accounts per client, gets current tags via GQL (small batches),
appends Group A tag (395387) while preserving existing tags + client_id.

Usage:
  python3 apply_group_a_tags.py          # Dry run
  python3 apply_group_a_tags.py --apply  # Execute
"""

import sys
import os
import time
import requests

sys.path.insert(0, os.path.dirname(__file__))
from setup import (
    sl_list_accounts, sl_gql, sl_tag_account,
    SMARTLEAD_API, SMARTLEAD_KEY, SMARTLEAD_JWT,
    SMARTLEAD_INTERNAL_API, sl_internal_headers, log,
)

GROUP_A_TAG = 395387

ACTIVE_CLIENTS = {
    350067: "Borja",
    350068: "Canopy",
    325077: "Coastal",
    325080: "Dallas",
    375372: "Denair",
    325117: "GM Landscaping",
    358743: "Kays B",
    405344: "Lawnvalue",
    325078: "Lightning",
    328149: "Pioneer",
    325076: "Timesavers",
    325079: "Tropical",
    367028: "Jim Robinson",
}


def get_account_tags_batch(account_ids):
    """Get tag mappings for a batch of account IDs via GQL. Returns {account_id: [tag_ids]}."""
    if not account_ids:
        return {}
    ids_str = ", ".join(str(i) for i in account_ids)
    query = f"""{{
      email_account_tag_mappings(where: {{email_account_id: {{_in: [{ids_str}]}}}}) {{
        email_account_id
        tag_id
      }}
    }}"""
    result = sl_gql(query)
    mappings = result.get("data", {}).get("email_account_tag_mappings", [])
    by_account = {}
    for m in mappings:
        aid = m["email_account_id"]
        by_account.setdefault(aid, []).append(m["tag_id"])
    return by_account


def main():
    apply = "--apply" in sys.argv

    if not SMARTLEAD_JWT:
        log("SMARTLEAD_JWT not set", "ERROR")
        sys.exit(1)

    log("Fetching all SmartLead accounts...")
    all_accounts = []
    offset = 0
    while True:
        try:
            batch = sl_list_accounts(limit=100, offset=offset)
        except Exception as e:
            log(f"  Fetch failed at offset {offset}, retrying in 30s: {e}", "WARN")
            time.sleep(30)
            try:
                batch = sl_list_accounts(limit=100, offset=offset)
            except Exception as e2:
                log(f"  Still failing: {e2}", "ERROR")
                sys.exit(1)
        if not batch:
            break
        all_accounts.extend(batch)
        if len(batch) < 100:
            break
        offset += 100
        time.sleep(1)
    log(f"Total accounts: {len(all_accounts)}")

    client_id_set = set(ACTIVE_CLIENTS.keys())
    client_accounts = [
        a for a in all_accounts
        if a.get("client_id") in client_id_set
    ]
    log(f"Active client accounts: {len(client_accounts)}")

    by_client = {}
    for a in client_accounts:
        cid = a["client_id"]
        by_client.setdefault(cid, []).append(a)

    for cid, name in sorted(ACTIVE_CLIENTS.items(), key=lambda x: x[1]):
        accts = by_client.get(cid, [])
        log(f"  {name}: {len(accts)} accounts")

    if not apply:
        log("\n[DRY RUN] No changes made. Run with --apply to execute.")
        return

    log("\n" + "=" * 60)
    log("APPLYING GROUP A TAGS")
    log("=" * 60)

    total_ok = 0
    total_skip = 0
    total_fail = 0

    for cid, name in sorted(ACTIVE_CLIENTS.items(), key=lambda x: x[1]):
        accts = by_client.get(cid, [])
        if not accts:
            log(f"\n{name}: no accounts found, skipping")
            continue

        log(f"\n{name}: {len(accts)} accounts")
        acc_ids = [a["id"] for a in accts]

        # Get current tags in small batches of 15
        all_tag_map = {}
        for i in range(0, len(acc_ids), 15):
            chunk = acc_ids[i:i+15]
            batch_tags = get_account_tags_batch(chunk)
            all_tag_map.update(batch_tags)
            # Accounts with no tags won't appear in results — that's fine
            time.sleep(0.8)

        ok = 0
        skip = 0
        fail = 0

        for a in accts:
            aid = a["id"]
            email = a.get("from_email", "?")
            current_tags = all_tag_map.get(aid, [])

            if GROUP_A_TAG in current_tags:
                skip += 1
                continue

            new_tags = current_tags + [GROUP_A_TAG]
            result = sl_tag_account(aid, new_tags, client_id=cid)
            if result.get("ok"):
                ok += 1
            else:
                fail += 1
                log(f"  FAIL {email}: {result}")
            time.sleep(0.4)

        log(f"  {ok} tagged, {skip} already had Group A, {fail} failed")
        total_ok += ok
        total_skip += skip
        total_fail += fail

    log(f"\n{'=' * 60}")
    log(f"DONE: {total_ok} tagged, {total_skip} skipped, {total_fail} failed")
    log(f"{'=' * 60}")


if __name__ == "__main__":
    main()
