#!/usr/bin/env python3
"""Seed client_rotations table with A/B group data.

Reads the corrected b_group_assignments.json mapping and fetches
actual account IDs from SmartLead to populate rotation records.

Usage:
  python3 seed_rotations.py          # Dry run
  python3 seed_rotations.py --apply  # Execute
"""

import json
import sys
import os
import time

sys.path.insert(0, os.path.dirname(__file__))
from setup import sl_list_accounts, log
import db

MAPPING_FILE = os.path.join(os.path.dirname(__file__), "clients", "b_group_assignments.json")


def main():
    apply = "--apply" in sys.argv

    with open(MAPPING_FILE) as f:
        mapping = json.load(f)

    log("Fetching all SmartLead accounts...")
    all_accounts = []
    offset = 0
    while True:
        try:
            batch = sl_list_accounts(limit=100, offset=offset)
        except Exception as e:
            log(f"  Retry at offset {offset}: {e}", "WARN")
            time.sleep(15)
            batch = sl_list_accounts(limit=100, offset=offset)
        if not batch:
            break
        all_accounts.extend(batch)
        if len(batch) < 100:
            break
        offset += 100
        time.sleep(1)
    log(f"Total accounts: {len(all_accounts)}")

    log("\nRotation records to create:")
    records = []

    for generic_name, info in mapping.items():
        client_name = info["serves_client"]
        client_id = info["serves_client_id"]
        generic_client_id = info["generic_client_id"]

        group_a_ids = [a["id"] for a in all_accounts if a.get("client_id") == client_id]
        group_b_ids = [a["id"] for a in all_accounts if a.get("client_id") == generic_client_id]

        log(f"  {client_name}")
        log(f"    Group A: {len(group_a_ids)} accounts (client {client_id})")
        log(f"    Group B: {len(group_b_ids)} accounts ({generic_name}, client {generic_client_id})")

        records.append({
            "client_name": client_name,
            "group_a_ids": group_a_ids,
            "group_b_ids": group_b_ids,
            "b_group_label": generic_name,
        })

    if not apply:
        log("\n[DRY RUN] No changes made. Run with --apply to execute.")
        return

    log("\nUpserting rotation records...")
    for rec in records:
        db.upsert_rotation(
            client_name=rec["client_name"],
            group_a_ids=rec["group_a_ids"],
            group_b_ids=rec["group_b_ids"],
            active_group="A",
        )
        log(f"  {rec['client_name']}: A={len(rec['group_a_ids'])}, B={len(rec['group_b_ids'])}")

    log("\nDone — rotation records seeded for 6 clients.")


if __name__ == "__main__":
    main()
