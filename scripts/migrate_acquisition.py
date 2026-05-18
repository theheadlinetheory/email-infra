#!/usr/bin/env python3
"""Migration: tag and organize acquisition inbox accounts into group-named SmartLead clients.

Current state (as of 2026-04-05):
- 99 Aidan Hutchinson accounts under "Acquisition" client (ID 328236) — NO tags
- Group tags A-F exist in SmartLead but are not applied to any accounts
  (tags were wiped by the assign_clients.py bug)
- Old Sean Reynolds acquisition accounts lost their group tags and are now
  assigned to various client names — out of scope for this migration

This script handles the 99 Aidan Hutchinson accounts:
1. Reads accounts from the "Acquisition" client
2. Tags them with [Acquisition Inbox, Zapmail, warmup date, group tag]
3. Creates a group-named client and moves accounts to it

The internal endpoint GET /api/email-account/{id}/details returns tags via
email_account_tag_mappings (the public API does not return tags).

Usage:
    python3 migrate_acquisition.py          # Dry run — shows what would happen
    python3 migrate_acquisition.py --apply  # Actually tag and assign accounts
"""

import sys
import os
import time
import requests
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))
from setup import (
    sl_list_accounts, sl_internal_headers, sl_find_or_create_tag, sl_get_all_tags,
    SMARTLEAD_API, SMARTLEAD_KEY, SMARTLEAD_INTERNAL_API
)

ACQUISITION_CLIENT_ID = 328236


def get_accounts_by_client(client_id):
    """Fetch all accounts for a SmartLead client."""
    accounts = []
    offset = 0
    while True:
        r = requests.get(
            f"{SMARTLEAD_API}/email-accounts/?api_key={SMARTLEAD_KEY}"
            f"&client_id={client_id}&offset={offset}&limit=100",
            timeout=30,
        )
        batch = r.json() if r.status_code == 200 else []
        if not isinstance(batch, list) or not batch:
            break
        accounts.extend(batch)
        if len(batch) < 100:
            break
        offset += 100
    return accounts


def get_account_tags(account_id):
    """Fetch tags for an account via the internal details endpoint."""
    headers = sl_internal_headers()
    r = requests.get(
        f"{SMARTLEAD_INTERNAL_API}/email-account/{account_id}/details",
        headers=headers, timeout=15,
    )
    if r.status_code != 200:
        return []
    data = r.json().get("email_accounts_by_pk", {})
    mappings = data.get("email_account_tag_mappings", [])
    return [{"id": m["tag"]["id"], "name": m["tag"]["name"]} for m in mappings]


def get_clients():
    """Get all SmartLead clients."""
    r = requests.get(f"{SMARTLEAD_API}/client?api_key={SMARTLEAD_KEY}", timeout=30)
    return r.json() if r.status_code == 200 else []


def find_client_by_name(clients, name):
    """Find a client by name (case-insensitive, fuzzy)."""
    name_lower = name.lower().strip()
    for c in clients:
        cn = c["name"].lower().strip()
        if cn == name_lower or name_lower in cn or cn in name_lower:
            return c
    return None


def create_client(name):
    """Create a SmartLead client."""
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


def detect_next_group_letter(existing_tags):
    """Find the next unused group letter from existing SmartLead tags."""
    used_letters = set()
    for tag_name in existing_tags:
        if "group" in tag_name.lower() and "(" in tag_name:
            letter = tag_name.split()[0].strip()
            if len(letter) == 1 and letter.isalpha():
                used_letters.add(letter.upper())
    for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        if c not in used_letters:
            return c
    return "Z"


def main():
    apply = "--apply" in sys.argv

    print(f"=== Acquisition Account Migration ({'APPLY' if apply else 'DRY RUN'}) ===\n")

    # Step 1: Get accounts from the Acquisition client
    print(f"Fetching accounts from Acquisition client (ID {ACQUISITION_CLIENT_ID})...")
    acq_accounts = get_accounts_by_client(ACQUISITION_CLIENT_ID)
    print(f"  {len(acq_accounts)} accounts found")

    if not acq_accounts:
        print("No accounts to migrate.")
        return

    # Step 2: Check current tags on these accounts (sample first few)
    print("\nChecking current tags (sampling first 3)...")
    for acc in acq_accounts[:3]:
        tags = get_account_tags(acc["id"])
        tag_names = [t["name"] for t in tags]
        print(f"  {acc.get('from_email', '?')}: {tag_names if tag_names else '(no tags)'}")
        time.sleep(0.15)

    # Step 3: Determine group info
    existing_tags = sl_get_all_tags()
    next_letter = detect_next_group_letter(existing_tags)

    # Group by creation date to determine warmup start date
    by_date = {}
    for acc in acq_accounts:
        created = acc.get("created_at", "")[:10]
        by_date.setdefault(created, []).append(acc)

    print(f"\nAccounts by creation date:")
    for d in sorted(by_date.keys()):
        print(f"  {d}: {len(by_date[d])} accounts")

    # Each creation date batch = one group
    groups = []
    letter = next_letter
    for date_str in sorted(by_date.keys()):
        accs = by_date[date_str]
        # Determine daily volume from account count (3 accounts per domain, ~15 emails/day each)
        daily_vol = len(accs) * 15
        # Round to nearest 250
        daily_vol = max(250, round(daily_vol / 250) * 250)
        group_name = f"{letter} Group ({daily_vol}/day)"

        # Parse warmup date from creation date
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        warmup_tag = f"{dt.month}/{dt.day}/{dt.strftime('%y')}"

        groups.append({
            "name": group_name,
            "letter": letter,
            "accounts": accs,
            "warmup_tag": warmup_tag,
            "daily_vol": daily_vol,
        })
        letter = chr(ord(letter) + 1)

    print(f"\nPlanned groups:")
    for g in groups:
        print(f"  {g['name']}: {len(g['accounts'])} accounts, warmup date tag: {g['warmup_tag']}")

    # Step 4: Resolve tag IDs
    print("\nResolving tags...")
    tag_name_to_id = {}
    for tag_name in ["Acquisition Inbox", "Zapmail"]:
        tid = sl_find_or_create_tag(tag_name, existing_tags=existing_tags)
        tag_name_to_id[tag_name] = tid
        print(f"  {tag_name}: ID {tid}")

    for g in groups:
        for tag_name in [g["warmup_tag"], g["name"]]:
            if tag_name not in tag_name_to_id:
                tid = sl_find_or_create_tag(tag_name, existing_tags=existing_tags)
                tag_name_to_id[tag_name] = tid
                print(f"  {tag_name}: ID {tid}")

    if not apply:
        print("\n--- DRY RUN ---")
        print(f"Would create {len(groups)} group client(s) and tag {len(acq_accounts)} accounts.")
        print(f"Tags per account: [Acquisition Inbox, Zapmail, warmup date, group name]")
        print("Run with --apply to execute.")
        return

    # Step 5: Create group clients and assign accounts
    clients = get_clients()
    for g in groups:
        group_name = g["name"]
        accs = g["accounts"]

        # Find or create group client
        client = find_client_by_name(clients, group_name)
        if client:
            group_client_id = client["id"]
            print(f"\n{group_name}: using existing client (ID {group_client_id})")
        else:
            print(f"\n{group_name}: creating client...")
            group_client_id = create_client(group_name)
            if not group_client_id:
                print(f"  FAILED to create client — skipping {len(accs)} accounts")
                continue
            print(f"  Created (ID {group_client_id})")
            time.sleep(0.5)

        # Build tag ID list for this group
        tag_ids = [
            tag_name_to_id["Acquisition Inbox"],
            tag_name_to_id["Zapmail"],
            tag_name_to_id[g["warmup_tag"]],
            tag_name_to_id[g["name"]],
        ]
        tag_ids = [t for t in tag_ids if t]  # filter None

        print(f"  Tagging and assigning {len(accs)} accounts (tags: {tag_ids})...")
        success = 0
        fail = 0
        headers = sl_internal_headers()
        for acc in accs:
            body = {"id": acc["id"], "clientId": group_client_id, "tags": tag_ids}
            r = requests.post(
                f"{SMARTLEAD_INTERNAL_API}/email-account/save-management-details",
                headers=headers, json=body, timeout=30,
            )
            if r.status_code == 200 and r.json().get("ok"):
                success += 1
            else:
                fail += 1
                print(f"    FAIL: {acc.get('from_email', '?')} — {r.text[:200]}")
            time.sleep(0.15)

        print(f"  Done: {success} success, {fail} failed")

    print(f"\nMigration complete!")


if __name__ == "__main__":
    main()
