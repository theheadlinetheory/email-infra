#!/usr/bin/env python3
"""Add warmup date tags to fulfillment accounts using their SmartLead created_at date.

Usage:
  python3 fix_missing_dates.py --dry-run   # Preview
  python3 fix_missing_dates.py              # Execute
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


def internal_headers():
    return {"Authorization": f"Bearer {SMARTLEAD_JWT}", "Content-Type": "application/json"}


def api_get_with_retry(url, **kwargs):
    """GET with retry on 429."""
    for attempt in range(3):
        r = requests.get(url, **kwargs)
        if r.status_code == 429:
            wait = 30 * (attempt + 1)
            print(f"  Rate limited, waiting {wait}s...")
            time.sleep(wait)
            continue
        return r
    return r


def get_all_accounts():
    accounts = []
    offset = 0
    while True:
        r = api_get_with_retry(
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


def get_all_tags():
    body = {"query": "{ tags { id name color } }"}
    r = requests.post(SMARTLEAD_GQL, headers=internal_headers(), json=body, timeout=15)
    tags = r.json().get("data", {}).get("tags", [])
    return {t["name"]: t for t in tags}


def find_or_create_tag(name, all_tags, color="#808080"):
    if name in all_tags:
        return all_tags[name]["id"]
    for tn, td in all_tags.items():
        if tn.lower().strip() == name.lower().strip():
            return td["id"]
    body = {
        "query": (
            "mutation($name: String!, $color: String!) {"
            " insert_tags_one(object: {name: $name, color: $color}) { id name }"
            " }"
        ),
        "variables": {"name": name, "color": color},
    }
    r = requests.post(SMARTLEAD_GQL, headers=internal_headers(), json=body, timeout=30)
    tag_id = r.json().get("data", {}).get("insert_tags_one", {}).get("id")
    if tag_id:
        all_tags[name] = {"id": tag_id, "name": name, "color": color}
        print(f"    Created tag: '{name}' (ID={tag_id})")
    return tag_id


def format_date(iso_str):
    if not iso_str:
        return ""
    try:
        d = datetime.strptime(iso_str[:10], "%Y-%m-%d")
        return f"{d.month}/{d.day}/{str(d.year)[2:]}"
    except (ValueError, TypeError):
        return ""


def main():
    parser = argparse.ArgumentParser(description="Add date tags using created_at")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not SMARTLEAD_JWT or not SMARTLEAD_KEY:
        print("ERROR: SMARTLEAD_JWT and SMARTLEAD_API_KEY required")
        sys.exit(1)

    mode = "DRY RUN" if args.dry_run else "EXECUTE"
    print(f"=== Fix Missing Date Tags ({mode}) ===\n")

    print("Fetching accounts...")
    accounts = get_all_accounts()
    if not accounts:
        print("ERROR: Got 0 accounts — likely rate limited. Try again in a minute.")
        sys.exit(1)
    print(f"  {len(accounts)} accounts")

    print("Fetching clients...")
    r = api_get_with_retry(f"{SMARTLEAD_API}/client?api_key={SMARTLEAD_KEY}", timeout=30)
    clients = r.json() if r.status_code == 200 else []
    print(f"  {len(clients)} clients")

    # Identify acquisition group clients to skip
    acq_client_ids = set()
    for c in clients:
        name = c.get("name", "").lower()
        if "group" in name or "acquisition" in name:
            acq_client_ids.add(c["id"])

    # Filter to fulfillment accounts with client_id
    fulfillment = [a for a in accounts if a.get("client_id") and a["client_id"] not in acq_client_ids]
    print(f"  {len(fulfillment)} fulfillment accounts to check")

    print("\nFetching tags...")
    all_tags = get_all_tags()
    print(f"  {len(all_tags)} existing tags")

    # Scan and fix
    print(f"\nScanning for missing date tags...")
    fixed = 0
    already_ok = 0
    errors = 0

    for i, acc in enumerate(fulfillment):
        if (i + 1) % 50 == 0:
            print(f"  Checked {i + 1}/{len(fulfillment)}...")

        acc_id = acc["id"]
        created_at = acc.get("created_at", "")
        warmup_date = format_date(created_at)
        if not warmup_date:
            continue

        # Fetch current tags
        try:
            r = api_get_with_retry(
                f"{SMARTLEAD_INTERNAL_API}/email-account/{acc_id}/details",
                headers=internal_headers(),
                timeout=15,
            )
            if r.status_code != 200:
                errors += 1
                time.sleep(0.5)
                continue
            data = r.json().get("email_accounts_by_pk", {})
            mappings = data.get("email_account_tag_mappings", [])
            current_tags = [{"id": m["tag"]["id"], "name": m["tag"]["name"]} for m in mappings]
        except Exception as e:
            print(f"  ERROR fetching {acc.get('from_email', '?')}: {e}")
            errors += 1
            time.sleep(0.5)
            continue
        time.sleep(0.3)

        # Already has date tag?
        has_date = any("/" in t["name"] and len(t["name"]) <= 8 for t in current_tags)
        if has_date:
            already_ok += 1
            continue

        if args.dry_run:
            fixed += 1
            if fixed <= 10:
                print(f"  Would fix: {acc.get('from_email', '?')} +date={warmup_date}")
            continue

        # Add date tag
        date_tag_id = find_or_create_tag(warmup_date, all_tags, "#D0FCB1")
        if not date_tag_id:
            print(f"  ERROR: Could not create date tag '{warmup_date}'")
            errors += 1
            continue

        existing_ids = [t["id"] for t in current_tags]
        all_tag_ids = list(set(existing_ids + [date_tag_id]))

        try:
            body = {"id": acc_id, "tags": all_tag_ids, "clientId": acc["client_id"]}
            r = requests.post(
                f"{SMARTLEAD_INTERNAL_API}/email-account/save-management-details",
                headers=internal_headers(),
                json=body,
                timeout=30,
            )
            fixed += 1
            if fixed <= 5 or fixed % 50 == 0:
                print(f"  Fixed: {acc.get('from_email', '?')} +date={warmup_date}")
        except Exception as e:
            print(f"  ERROR fixing {acc.get('from_email', '?')}: {e}")
            errors += 1
        time.sleep(0.5)

    print(f"\n{'='*60}")
    print(f"Done. Fixed: {fixed}, Already had date: {already_ok}, Errors: {errors}")
    if args.dry_run:
        print("[DRY RUN] No changes made.")


if __name__ == "__main__":
    main()
