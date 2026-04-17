#!/usr/bin/env python3
"""Find and fix SmartLead accounts missing required tags.

Every account must have exactly 3 tags:
  1. Zapmail (tag ID 262254)
  2. Client Name (matching the SmartLead client name)
  3. Warmup Start Date (format "M/D/YY")

Usage:
  python3 fix_untagged.py --dry-run   # Report only
  python3 fix_untagged.py              # Scan and fix
"""

import argparse
import os
import sys
import time
from datetime import datetime

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

SMARTLEAD_API = "https://server.smartlead.ai/api/v1"
SMARTLEAD_INTERNAL_API = "https://server.smartlead.ai/api"
SMARTLEAD_GQL = os.environ.get("SMARTLEAD_GQL", "https://fe-gql.smartlead.ai/v1/graphql")
SMARTLEAD_KEY = os.environ.get("SMARTLEAD_API_KEY", "")
SMARTLEAD_JWT = os.environ.get("SMARTLEAD_JWT", "")

ZAPMAIL_TAG_ID = 262254


# ── API helpers ──────────────────────────────────────────────


def internal_headers():
    return {"Authorization": f"Bearer {SMARTLEAD_JWT}", "Content-Type": "application/json"}


def get_all_accounts():
    """Fetch all SmartLead email accounts via public API."""
    accounts = []
    offset = 0
    while True:
        r = requests.get(
            f"{SMARTLEAD_API}/email-accounts/?api_key={SMARTLEAD_KEY}&offset={offset}&limit=100",
            timeout=30,
        )
        batch = r.json() if r.status_code == 200 else []
        if not isinstance(batch, list) or not batch:
            break
        accounts.extend(batch)
        if len(batch) < 100:
            break
        offset += 100
        time.sleep(0.5)
    return accounts


def get_clients():
    """Fetch all SmartLead clients. Returns list of {id, name, ...}."""
    r = requests.get(f"{SMARTLEAD_API}/client?api_key={SMARTLEAD_KEY}", timeout=30)
    return r.json() if r.status_code == 200 else []


def get_account_details(account_id):
    """Fetch account details (tags + warmup info) via internal API.

    Returns (tags_list, warmup_created_at_str).
    tags_list: [{"id": int, "name": str}, ...]
    warmup_created_at_str: ISO date string or ""
    """
    r = requests.get(
        f"{SMARTLEAD_INTERNAL_API}/email-account/{account_id}/details",
        headers=internal_headers(),
        timeout=15,
    )
    if r.status_code != 200:
        return [], ""
    data = r.json().get("email_accounts_by_pk", {})
    mappings = data.get("email_account_tag_mappings", [])
    tags = [{"id": m["tag"]["id"], "name": m["tag"]["name"]} for m in mappings]
    warmup_created = (data.get("warmup_details") or {}).get("warmup_created_at", "")
    return tags, warmup_created


def get_all_tags():
    """Get all existing tags from SmartLead via GraphQL. Returns {name: {id, name, color}}."""
    body = {"query": "{ tags { id name color } }"}
    r = requests.post(SMARTLEAD_GQL, headers=internal_headers(), json=body, timeout=15)
    tags = r.json().get("data", {}).get("tags", [])
    return {t["name"]: t for t in tags}


def create_tag(name, color="#808080"):
    """Create a new tag via GraphQL. Returns {id, name} or {}."""
    body = {
        "query": (
            "mutation($name: String!, $color: String!) {"
            " insert_tags_one(object: {name: $name, color: $color}) { id name }"
            " }"
        ),
        "variables": {"name": name, "color": color},
    }
    r = requests.post(SMARTLEAD_GQL, headers=internal_headers(), json=body, timeout=30)
    return r.json().get("data", {}).get("insert_tags_one", {})


def tag_account(account_id, tag_ids, client_id=None):
    """Apply tags to an account via save-management-details."""
    body = {"id": account_id, "tags": tag_ids, "clientId": client_id}
    r = requests.post(
        f"{SMARTLEAD_INTERNAL_API}/email-account/save-management-details",
        headers=internal_headers(),
        json=body,
        timeout=30,
    )
    return r.json()


# ── Tag resolution ───────────────────────────────────────────


def fuzzy_find_tag(name, all_tags):
    """Find an existing tag by exact or fuzzy match. Returns tag ID or None."""
    if name in all_tags:
        return all_tags[name]["id"]
    name_lower = name.lower().strip()
    for tag_name, tag_data in all_tags.items():
        tag_lower = tag_name.lower().strip()
        if tag_lower == name_lower:
            return tag_data["id"]
        if name_lower in tag_lower or tag_lower in name_lower:
            print(f"    Fuzzy matched: '{name}' -> '{tag_name}' (ID: {tag_data['id']})")
            return tag_data["id"]
    return None


def find_or_create_tag(name, all_tags, color="#808080"):
    """Find existing tag by fuzzy match, or create a new one. Returns tag ID or None."""
    existing_id = fuzzy_find_tag(name, all_tags)
    if existing_id:
        return existing_id
    print(f"    Creating tag: '{name}'")
    new_tag = create_tag(name, color)
    tag_id = new_tag.get("id")
    if tag_id:
        all_tags[name] = {"id": tag_id, "name": name, "color": color}
    return tag_id


# ── Classification ───────────────────────────────────────────


def classify_tags(tags, client_names):
    """Classify which of the 3 required tags are present.

    Returns (has_zapmail, has_client, has_date).
    Client name matching is fuzzy — "Coastal Lawn Care" matches
    "Coastal Lawn Care LLC" since one contains the other.
    """
    has_zapmail = any(t["id"] == ZAPMAIL_TAG_ID for t in tags)
    has_date = any("/" in t["name"] and len(t["name"]) <= 8 for t in tags)

    # Fuzzy client match: tag name contains or is contained by a known client name
    has_client = False
    for t in tags:
        tn = t["name"].lower().strip()
        if "group" in tn:
            has_client = True
            break
        for cn in client_names:
            cl = cn.lower().strip()
            if tn == cl or tn in cl or cl in tn:
                has_client = True
                break
        if has_client:
            break

    return has_zapmail, has_client, has_date


def format_warmup_date(iso_str):
    """Parse an ISO datetime string and return 'M/D/YY' or ''."""
    if not iso_str:
        return ""
    try:
        d = datetime.strptime(iso_str[:10], "%Y-%m-%d")
        return f"{d.month}/{d.day}/{str(d.year)[2:]}"
    except (ValueError, TypeError):
        return ""


# ── Main ─────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Find and fix untagged SmartLead accounts")
    parser.add_argument("--dry-run", action="store_true", help="Report only, don't apply tags")
    args = parser.parse_args()

    if not SMARTLEAD_JWT:
        print("ERROR: SMARTLEAD_JWT not set in .env")
        sys.exit(1)
    if not SMARTLEAD_KEY:
        print("ERROR: SMARTLEAD_API_KEY not set in .env")
        sys.exit(1)

    mode = "DRY RUN" if args.dry_run else "FIX"
    print(f"=== Fix Untagged Accounts ({mode}) ===\n")

    # Step 1: Fetch accounts, clients, tags
    print("Fetching all accounts...")
    accounts = get_all_accounts()
    print(f"  {len(accounts)} accounts")

    print("Fetching clients...")
    clients = get_clients()
    client_map = {c["id"]: c["name"] for c in clients}
    client_names = set(client_map.values())
    print(f"  {len(clients)} clients")

    print("Fetching existing tags...")
    all_tags = get_all_tags()
    print(f"  {len(all_tags)} tags")

    # Step 2: Scan each account for missing tags
    untagged = []
    partial = []
    correct = 0
    errors = 0

    print(f"\nScanning {len(accounts)} accounts for tag compliance...")
    for i, acc in enumerate(accounts):
        if (i + 1) % 50 == 0:
            print(f"  Scanned {i + 1}/{len(accounts)}...")

        acc_id = acc["id"]
        try:
            tags, warmup_created = get_account_details(acc_id)
        except Exception as e:
            print(f"  ERROR fetching details for account {acc_id}: {e}")
            errors += 1
            time.sleep(0.5)
            continue
        time.sleep(0.3)

        has_zapmail, has_client, has_date = classify_tags(tags, client_names)

        if has_zapmail and has_client and has_date:
            correct += 1
            continue

        tag_names = [t["name"] for t in tags]
        client_name = client_map.get(acc.get("client_id"), "Unknown")
        warmup_date = format_warmup_date(warmup_created)

        entry = {
            "id": acc_id,
            "email": acc.get("from_email", ""),
            "client_id": acc.get("client_id"),
            "client_name": client_name,
            "current_tags": tag_names,
            "existing_tag_ids": [t["id"] for t in tags],
            "missing_zapmail": not has_zapmail,
            "missing_client": not has_client,
            "missing_date": not has_date,
            "warmup_date": warmup_date,
        }

        if len(tags) == 0:
            untagged.append(entry)
        else:
            partial.append(entry)

    # Step 3: Report
    print(f"\n{'='*60}")
    print("RESULTS:")
    print(f"  Correctly tagged: {correct}")
    print(f"  Completely untagged: {len(untagged)}")
    print(f"  Partially tagged: {len(partial)}")
    print(f"  Scan errors: {errors}")
    print(f"{'='*60}")

    if untagged:
        print(f"\nCompletely untagged ({len(untagged)}):")
        for a in untagged:
            print(f"  {a['email']} — client: {a['client_name']}, warmup: {a['warmup_date'] or 'unknown'}")

    if partial:
        print(f"\nPartially tagged ({len(partial)}):")
        for a in partial:
            missing = []
            if a["missing_zapmail"]:
                missing.append("Zapmail")
            if a["missing_client"]:
                missing.append("ClientName")
            if a["missing_date"]:
                missing.append("Date")
            print(f"  {a['email']} — missing: {', '.join(missing)} — has: {a['current_tags']}")

    if args.dry_run:
        print("\n[DRY RUN] No changes made.")
        return

    # Step 4: Fix accounts
    to_fix = untagged + partial
    if not to_fix:
        print("\nAll accounts are correctly tagged!")
        return

    print(f"\nFixing {len(to_fix)} accounts...")
    fixed = 0
    skipped = 0

    for entry in to_fix:
        needed_tag_ids = []

        # Zapmail tag
        if entry["missing_zapmail"]:
            needed_tag_ids.append(ZAPMAIL_TAG_ID)

        # Client name tag
        if entry["missing_client"]:
            client_tag_name = entry["client_name"]
            if client_tag_name == "Unknown":
                print(f"  SKIP {entry['email']}: no client_id assigned")
                skipped += 1
                continue
            tag_id = find_or_create_tag(client_tag_name, all_tags, color="#B1C4FC")
            if not tag_id:
                print(f"  SKIP {entry['email']}: could not resolve client tag '{client_tag_name}'")
                skipped += 1
                continue
            needed_tag_ids.append(tag_id)

        # Date tag
        if entry["missing_date"]:
            if not entry["warmup_date"]:
                print(f"  SKIP date tag for {entry['email']}: no warmup_created_at")
            else:
                tag_id = find_or_create_tag(entry["warmup_date"], all_tags, color="#D0FCB1")
                if not tag_id:
                    print(f"  SKIP {entry['email']}: could not resolve date tag '{entry['warmup_date']}'")
                    skipped += 1
                    continue
                needed_tag_ids.append(tag_id)

        if not needed_tag_ids:
            skipped += 1
            continue

        # Merge with existing tags to preserve them
        all_tag_ids = list(set(entry["existing_tag_ids"] + needed_tag_ids))

        try:
            result = tag_account(entry["id"], all_tag_ids, entry["client_id"])
            if result.get("ok"):
                fixed += 1
                print(f"  Fixed: {entry['email']} (+{len(needed_tag_ids)} tags)")
            else:
                print(f"  FAIL: {entry['email']} -> {result}")
                skipped += 1
        except Exception as e:
            print(f"  ERROR fixing {entry['email']}: {e}")
            skipped += 1
        time.sleep(0.5)

    print(f"\nDone. Fixed: {fixed}, Skipped: {skipped}")


if __name__ == "__main__":
    main()
