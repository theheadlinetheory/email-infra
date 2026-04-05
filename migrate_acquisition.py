#!/usr/bin/env python3
"""One-time migration: assign existing acquisition inbox accounts to group-named SmartLead clients.

These ~593 accounts have group tags (A Group, B Group, etc.) but no client_id,
so they don't appear in the dashboard. This script:
1. Fetches all unassigned accounts
2. Reads their tags to determine which group they belong to
3. Creates SmartLead clients for each group (if needed)
4. Assigns each account to its group client (preserving existing tags)

Usage:
    python3 migrate_acquisition.py          # Dry run — shows what would happen
    python3 migrate_acquisition.py --apply  # Actually assign accounts
"""

import sys
import os
import time
import requests

sys.path.insert(0, os.path.dirname(__file__))
from setup import (
    sl_list_accounts, sl_internal_headers,
    SMARTLEAD_API, SMARTLEAD_KEY, SMARTLEAD_INTERNAL_API
)


def get_all_accounts():
    """Fetch all SmartLead accounts with pagination."""
    accounts = []
    offset = 0
    while True:
        batch = sl_list_accounts(offset=offset, limit=100)
        if isinstance(batch, list):
            accounts.extend(batch)
            if len(batch) < 100:
                break
            offset += 100
        else:
            break
    return accounts


def get_clients():
    """Get all SmartLead clients. Returns {name_lower: {id, name}}."""
    r = requests.get(f"{SMARTLEAD_API}/client?api_key={SMARTLEAD_KEY}", timeout=30)
    clients = r.json() if r.status_code == 200 else []
    return {c["name"].lower().strip(): c for c in clients}


def create_client(name):
    """Create a SmartLead client for a group."""
    slug = name.lower().replace(" ", "").replace("(", "").replace(")", "").replace("/", "")
    email = f"tht.{slug}.client@gmail.com"
    r = requests.post(
        f"{SMARTLEAD_API}/client/save?api_key={SMARTLEAD_KEY}",
        json={"name": name, "email": email, "password": "THTclient2026!"},
        timeout=30,
    )
    if r.status_code == 201:
        return r.json().get("clientId")
    print(f"  WARN: Could not create client '{name}': {r.status_code} {r.text[:200]}")
    return None


def extract_group_from_tags(tags):
    """Extract group name from account tags list.

    Tags can be list of dicts [{id, name, color}] or list of IDs.
    Returns group name like "A Group (250/day)" or None.
    """
    if not tags:
        return None
    for t in tags:
        name = t.get("name", "") if isinstance(t, dict) else ""
        if name and "group" in name.lower() and "(" in name:
            return name
    return None


def main():
    apply = "--apply" in sys.argv

    print("Fetching all SmartLead accounts...")
    accounts = get_all_accounts()
    print(f"  {len(accounts)} total accounts")

    # Find unassigned accounts (no client_id)
    unassigned = [a for a in accounts if not a.get("client_id")]
    print(f"  {len(unassigned)} unassigned accounts")

    # Group by their group tag
    groups = {}  # group_name -> [account, ...]
    no_group = []
    for acc in unassigned:
        tags = acc.get("tags") or []
        group_name = extract_group_from_tags(tags)
        if group_name:
            groups.setdefault(group_name, []).append(acc)
        else:
            no_group.append(acc)

    print(f"\nFound {len(groups)} acquisition groups:")
    for name in sorted(groups.keys()):
        print(f"  {name}: {len(groups[name])} accounts")
    if no_group:
        print(f"  (no group tag): {len(no_group)} accounts — will be skipped")

    if not groups:
        print("\nNo acquisition groups found. Nothing to do.")
        return

    # Get or create SmartLead clients for each group
    print("\nChecking SmartLead clients...")
    clients = get_clients()
    group_client_ids = {}
    for group_name in sorted(groups.keys()):
        group_lower = group_name.lower().strip()
        # Try exact match first, then fuzzy
        client = clients.get(group_lower)
        if not client:
            for cl_name, cl_data in clients.items():
                if group_lower in cl_name or cl_name in group_lower:
                    client = cl_data
                    break

        if client:
            group_client_ids[group_name] = client["id"]
            print(f"  {group_name} → existing client '{client['name']}' (ID: {client['id']})")
        elif apply:
            print(f"  {group_name} → creating new client...")
            client_id = create_client(group_name)
            if client_id:
                group_client_ids[group_name] = client_id
                print(f"    Created (ID: {client_id})")
                time.sleep(0.5)
            else:
                print(f"    FAILED — accounts in this group will be skipped")
        else:
            print(f"  {group_name} → would create new client (dry run)")
            group_client_ids[group_name] = None

    if not apply:
        print("\n--- DRY RUN ---")
        total = sum(len(accs) for accs in groups.values())
        print(f"Would assign {total} accounts across {len(groups)} groups.")
        print("Run with --apply to execute.")
        return

    # Assign accounts to their group clients
    print("\nAssigning accounts to group clients...")
    assigned = 0
    failed = 0
    for group_name in sorted(groups.keys()):
        client_id = group_client_ids.get(group_name)
        if not client_id:
            print(f"  Skipping {group_name} — no client ID")
            continue

        accs = groups[group_name]
        print(f"  {group_name} ({len(accs)} accounts)...")
        for acc in accs:
            acc_id = acc["id"]
            # Preserve existing tags
            current_tags = acc.get("tags") or []
            if isinstance(current_tags, list) and current_tags and isinstance(current_tags[0], dict):
                tag_ids = [t["id"] for t in current_tags]
            else:
                tag_ids = current_tags

            body = {"id": acc_id, "clientId": client_id, "tags": tag_ids}
            r = requests.post(
                f"{SMARTLEAD_INTERNAL_API}/email-account/save-management-details",
                headers=sl_internal_headers(),
                json=body,
                timeout=30,
            )
            if r.status_code == 200 and r.json().get("ok"):
                assigned += 1
            else:
                failed += 1
                email = acc.get("from_email", "?")
                print(f"    FAIL: {email} — {r.text[:200]}")
            time.sleep(0.15)

    print(f"\nDone!")
    print(f"  Assigned: {assigned}")
    print(f"  Failed: {failed}")


if __name__ == "__main__":
    main()
