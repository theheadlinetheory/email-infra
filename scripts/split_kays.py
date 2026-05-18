#!/usr/bin/env python3
"""Split Kay's Landscaping (84 accounts, 28 domains) into two clients (~500/day each).

Group 1 stays as "Kay's Landscaping" (client 325082) — first 14 domains alphabetically.
Group 2 moves to new client "Kay's Landscaping 2" — last 14 domains alphabetically.

For each moved account:
  - Reassign client_id to new client
  - Replace "Kay's Landscaping" tag with "Kay's Landscaping 2" tag
  - Keep Zapmail + warmup date tags

Usage:
  python3 split_kays.py --dry-run   # Preview
  python3 split_kays.py              # Execute
"""

import argparse
import os
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

SMARTLEAD_API = "https://server.smartlead.ai/api/v1"
SMARTLEAD_INTERNAL_API = "https://server.smartlead.ai/api"
SMARTLEAD_GQL = os.environ.get("SMARTLEAD_GQL", "https://fe-gql.smartlead.ai/v1/graphql")
SMARTLEAD_KEY = os.environ.get("SMARTLEAD_API_KEY", "")
SMARTLEAD_JWT = os.environ.get("SMARTLEAD_JWT", "")

KAYS_CLIENT_ID = 325082
KAYS_TAG_ID = 350275  # "Kay's Landscaping"
ZAPMAIL_TAG_ID = 262254


def internal_headers():
    return {"Authorization": f"Bearer {SMARTLEAD_JWT}", "Content-Type": "application/json"}


def get_client_accounts(client_id):
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
        accounts.extend([a for a in batch if a.get("client_id") == client_id])
        if len(batch) < 100:
            break
        offset += 100
        time.sleep(0.5)
    return accounts


def get_account_tags(account_id):
    """Fetch current tags for an account via internal API."""
    r = requests.get(
        f"{SMARTLEAD_INTERNAL_API}/email-account/{account_id}/details",
        headers=internal_headers(),
        timeout=15,
    )
    if r.status_code != 200:
        return []
    data = r.json().get("email_accounts_by_pk", {})
    mappings = data.get("email_account_tag_mappings", [])
    return [{"id": m["tag"]["id"], "name": m["tag"]["name"]} for m in mappings]


def create_client(name):
    slug = name.lower().replace(" ", "").replace("'", "").replace("(", "").replace(")", "")
    email = f"tht.{slug}.client@gmail.com"
    r = requests.post(
        f"{SMARTLEAD_API}/client/save?api_key={SMARTLEAD_KEY}",
        json={"name": name, "email": email, "password": "THTclient2026!"},
        timeout=30,
    )
    return r.json()


def create_tag(name, color="#5C7CFA"):
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


def save_management_details(account_id, tag_ids, client_id):
    body = {"id": account_id, "tags": tag_ids, "clientId": client_id}
    r = requests.post(
        f"{SMARTLEAD_INTERNAL_API}/email-account/save-management-details",
        headers=internal_headers(),
        json=body,
        timeout=30,
    )
    return r.json()


def main():
    parser = argparse.ArgumentParser(description="Split Kay's Landscaping into two groups")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not SMARTLEAD_JWT or not SMARTLEAD_KEY:
        print("ERROR: SMARTLEAD_JWT and SMARTLEAD_API_KEY required")
        sys.exit(1)

    mode = "DRY RUN" if args.dry_run else "EXECUTE"
    print(f"=== Split Kay's Landscaping ({mode}) ===\n")

    # Step 1: Get all Kay's accounts
    print("Fetching Kay's Landscaping accounts...")
    accounts = get_client_accounts(KAYS_CLIENT_ID)
    print(f"  {len(accounts)} accounts")

    # Step 2: Group by domain
    domain_accounts = {}
    for acc in accounts:
        domain = acc["from_email"].split("@")[-1]
        domain_accounts.setdefault(domain, []).append(acc)

    domains = sorted(domain_accounts.keys())
    print(f"  {len(domains)} unique domains\n")

    if len(domains) < 2:
        print("ERROR: Not enough domains to split")
        sys.exit(1)

    # Step 3: Split into two halves
    mid = len(domains) // 2
    group1_domains = domains[:mid]
    group2_domains = domains[mid:]

    group1_count = sum(len(domain_accounts[d]) for d in group1_domains)
    group2_count = sum(len(domain_accounts[d]) for d in group2_domains)

    print(f"Group 1 — Kay's Landscaping (stays): {len(group1_domains)} domains, {group1_count} accounts")
    for d in group1_domains:
        print(f"  {d} ({len(domain_accounts[d])} accounts)")

    print(f"\nGroup 2 — Kay's Landscaping 2 (new): {len(group2_domains)} domains, {group2_count} accounts")
    for d in group2_domains:
        print(f"  {d} ({len(domain_accounts[d])} accounts)")

    if args.dry_run:
        print(f"\n[DRY RUN] Would move {group2_count} accounts to new 'Kay's Landscaping 2' client.")
        return

    # Step 4: Get or create client
    new_client_id = 358743  # Already created
    print(f"\nUsing Kay's Landscaping 2 client ID: {new_client_id}")

    # Step 5: Get or create tag
    print("Finding/creating 'Kay's Landscaping 2' tag...")
    # Check if tag already exists
    body = {"query": "{ tags { id name } }"}
    r = requests.post(SMARTLEAD_GQL, headers=internal_headers(), json=body, timeout=15)
    all_tags = r.json().get("data", {}).get("tags", [])
    new_tag_id = None
    for t in all_tags:
        if t["name"] == "Kay's Landscaping 2":
            new_tag_id = t["id"]
            break
    if not new_tag_id:
        new_tag = create_tag("Kay's Landscaping 2", "#5C7CFA")
        new_tag_id = new_tag.get("id")
        if not new_tag_id:
            print(f"ERROR: Could not create tag: {new_tag}")
            sys.exit(1)
        print(f"  Created tag ID: {new_tag_id}")
    else:
        print(f"  Found existing tag ID: {new_tag_id}")
    time.sleep(0.5)

    # Step 6: Move Group 2 accounts
    print(f"\nMoving {group2_count} accounts to Kay's Landscaping 2...")
    moved = 0
    errors = 0

    for domain in group2_domains:
        for acc in domain_accounts[domain]:
            acc_id = acc["id"]
            email = acc["from_email"]

            # Fetch current tags
            current_tags = get_account_tags(acc_id)
            time.sleep(0.3)

            # Replace Kay's tag with Kay's 2 tag, keep everything else
            new_tag_ids = []
            for t in current_tags:
                if t["id"] == KAYS_TAG_ID:
                    new_tag_ids.append(new_tag_id)
                else:
                    new_tag_ids.append(t["id"])

            # Ensure Zapmail is present
            if ZAPMAIL_TAG_ID not in new_tag_ids:
                new_tag_ids.append(ZAPMAIL_TAG_ID)

            # Ensure new tag is present (in case old tag wasn't found)
            if new_tag_id not in new_tag_ids:
                new_tag_ids.append(new_tag_id)

            try:
                save_management_details(acc_id, new_tag_ids, new_client_id)
                moved += 1
                if moved <= 5 or moved % 10 == 0:
                    print(f"  OK [{moved}]: {email}")
            except Exception as e:
                print(f"  ERROR: {email}: {e}")
                errors += 1
            time.sleep(0.5)

    print(f"\n{'='*60}")
    print(f"Done. Moved: {moved}, Errors: {errors}")
    print(f"Kay's Landscaping: {group1_count} accounts ({len(group1_domains)} domains)")
    print(f"Kay's Landscaping 2: {moved} accounts ({len(group2_domains)} domains)")


if __name__ == "__main__":
    main()
