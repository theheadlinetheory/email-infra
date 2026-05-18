#!/usr/bin/env python3
"""Assign 33 headlinetheory .info domains to acquisition groups and tag them.

Each group gets 5-6 domains (15-18 accounts) targeting ~250 sends/day.
Tags applied: Zapmail + Acquisition Inbox + Group Name + Warmup Start Date

Usage:
  python3 assign_info_groups.py --dry-run   # Preview assignments
  python3 assign_info_groups.py              # Execute
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

# Existing group clients to reuse (rename from 500 to 250)
EXISTING_GROUPS = {
    "H": {"client_id": 341384, "current_name": "H Group (500/day)"},
    "I": {"client_id": 341385, "current_name": "I Group (500/day)"},
    "J": {"client_id": 341386, "current_name": "J Group (500/day)"},
}

DOMAINS_PER_GROUP = 6


def internal_headers():
    return {"Authorization": f"Bearer {SMARTLEAD_JWT}", "Content-Type": "application/json"}


def get_all_accounts():
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


def get_account_details(account_id):
    r = requests.get(
        f"{SMARTLEAD_INTERNAL_API}/email-account/{account_id}/details",
        headers=internal_headers(),
        timeout=15,
    )
    if r.status_code != 200:
        return ""
    data = r.json().get("email_accounts_by_pk", {})
    return (data.get("warmup_details") or {}).get("warmup_created_at", "")


def get_all_tags():
    body = {"query": "{ tags { id name color } }"}
    r = requests.post(SMARTLEAD_GQL, headers=internal_headers(), json=body, timeout=15)
    tags = r.json().get("data", {}).get("tags", [])
    return {t["name"]: t for t in tags}


def create_tag(name, color="#808080"):
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


def find_or_create_tag(name, all_tags, color="#808080"):
    # Exact match first
    if name in all_tags:
        return all_tags[name]["id"]
    # Case-insensitive match
    for tag_name, tag_data in all_tags.items():
        if tag_name.lower().strip() == name.lower().strip():
            return tag_data["id"]
    # Create new
    print(f"    Creating tag: '{name}'")
    new_tag = create_tag(name, color)
    tag_id = new_tag.get("id")
    if tag_id:
        all_tags[name] = {"id": tag_id, "name": name, "color": color}
    return tag_id


def save_management_details(account_id, tag_ids, client_id):
    body = {"id": account_id, "tags": tag_ids, "clientId": client_id}
    r = requests.post(
        f"{SMARTLEAD_INTERNAL_API}/email-account/save-management-details",
        headers=internal_headers(),
        json=body,
        timeout=30,
    )
    return r.json()


def save_client(client_id, name):
    """Rename an existing SmartLead client."""
    r = requests.post(
        f"{SMARTLEAD_API}/client/save?api_key={SMARTLEAD_KEY}",
        json={"id": client_id, "name": name},
        timeout=30,
    )
    try:
        return r.json()
    except Exception:
        return {"status": r.status_code, "text": r.text[:200]}


def create_client(name):
    """Create a new SmartLead client. Returns client dict."""
    slug = name.lower().replace(" ", "").replace("(", "").replace(")", "").replace("/", "")
    email = f"tht.{slug}.client@gmail.com"
    r = requests.post(
        f"{SMARTLEAD_API}/client/save?api_key={SMARTLEAD_KEY}",
        json={"name": name, "email": email, "password": "THTclient2026!"},
        timeout=30,
    )
    return r.json()


def format_warmup_date(iso_str):
    if not iso_str:
        return ""
    try:
        d = datetime.strptime(iso_str[:10], "%Y-%m-%d")
        return f"{d.month}/{d.day}/{str(d.year)[2:]}"
    except (ValueError, TypeError):
        return ""


def main():
    parser = argparse.ArgumentParser(description="Assign .info domains to acquisition groups")
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    args = parser.parse_args()

    if not SMARTLEAD_JWT or not SMARTLEAD_KEY:
        print("ERROR: SMARTLEAD_JWT and SMARTLEAD_API_KEY must be set in .env")
        sys.exit(1)

    mode = "DRY RUN" if args.dry_run else "EXECUTE"
    print(f"=== Assign .info Domains to Groups ({mode}) ===\n")

    # Step 1: Find all headlinetheory .info accounts with no client
    print("Fetching all accounts...")
    accounts = get_all_accounts()
    ht_info = [
        a for a in accounts
        if "headlinetheory" in a.get("from_email", "") and a.get("from_email", "").endswith(".info")
        and not a.get("client_id")
    ]
    print(f"  Found {len(ht_info)} unassigned headlinetheory .info accounts")

    # Step 2: Group by domain
    domain_accounts = {}
    for acc in ht_info:
        domain = acc["from_email"].split("@")[-1]
        domain_accounts.setdefault(domain, []).append(acc)

    domains = sorted(domain_accounts.keys())
    print(f"  {len(domains)} unique domains\n")

    # Step 3: Split into groups of DOMAINS_PER_GROUP
    groups = []
    for i in range(0, len(domains), DOMAINS_PER_GROUP):
        groups.append(domains[i : i + DOMAINS_PER_GROUP])

    letters = "HIJKLMNOPQRSTUVWXYZ"
    print(f"Planned groups ({len(groups)}):")
    group_assignments = []
    for idx, group_domains in enumerate(groups):
        letter = letters[idx]
        group_name = f"{letter} Group (250/day)"
        acct_count = sum(len(domain_accounts[d]) for d in group_domains)
        existing = EXISTING_GROUPS.get(letter)

        if existing:
            client_id = existing["client_id"]
            action = f"reuse client {client_id}"
            if existing["current_name"] != group_name:
                action += f" (rename from '{existing['current_name']}')"
        else:
            client_id = None
            action = "create new client"

        print(f"  {group_name}: {len(group_domains)} domains, {acct_count} accounts — {action}")
        for d in group_domains:
            print(f"    {d} ({len(domain_accounts[d])} accounts)")

        group_assignments.append({
            "letter": letter,
            "group_name": group_name,
            "domains": group_domains,
            "existing_client_id": client_id,
            "needs_rename": existing and existing["current_name"] != group_name,
        })

    if args.dry_run:
        print("\n[DRY RUN] No changes made.")
        return

    # Step 4: Fetch/create tags
    print("\nFetching existing tags...")
    all_tags = get_all_tags()

    acq_tag_id = find_or_create_tag("Acquisition Inbox", all_tags, color="#FCA5B1")
    if not acq_tag_id:
        print("ERROR: Could not find/create 'Acquisition Inbox' tag")
        sys.exit(1)
    print(f"  Acquisition Inbox tag ID: {acq_tag_id}")

    # Step 5: Process each group
    total_fixed = 0
    total_errors = 0

    for ga in group_assignments:
        print(f"\n--- {ga['group_name']} ---")

        # Get or create client
        if ga["existing_client_id"]:
            client_id = ga["existing_client_id"]
            if ga["needs_rename"]:
                print(f"  Renaming client {client_id} to '{ga['group_name']}'...")
                save_client(client_id, ga["group_name"])
                time.sleep(0.5)
        else:
            print(f"  Creating client '{ga['group_name']}'...")
            result = create_client(ga["group_name"])
            client_id = result.get("id")
            if not client_id:
                print(f"  ERROR: Could not create client: {result}")
                total_errors += len(ga["domains"]) * 3
                continue
            print(f"  Created client ID: {client_id}")
            time.sleep(0.5)

        # Get or create group name tag
        group_tag_id = find_or_create_tag(ga["group_name"], all_tags, color="#B1FCE4")
        if not group_tag_id:
            print(f"  ERROR: Could not find/create group tag '{ga['group_name']}'")
            continue

        # Process each account in this group
        for domain in ga["domains"]:
            for acc in domain_accounts[domain]:
                acc_id = acc["id"]
                email = acc["from_email"]

                # Fetch warmup date
                warmup_created = get_account_details(acc_id)
                warmup_date = format_warmup_date(warmup_created)
                time.sleep(0.3)

                # Build tag list: Zapmail + Acquisition Inbox + Group + Date
                tag_ids = [ZAPMAIL_TAG_ID, acq_tag_id, group_tag_id]

                if warmup_date:
                    date_tag_id = find_or_create_tag(warmup_date, all_tags, color="#D0FCB1")
                    if date_tag_id:
                        tag_ids.append(date_tag_id)
                    else:
                        print(f"  WARN: Could not resolve date tag '{warmup_date}' for {email}")
                else:
                    print(f"  WARN: No warmup date for {email}")

                # Apply: assign client + set tags
                try:
                    result = save_management_details(acc_id, tag_ids, client_id)
                    total_fixed += 1
                    tag_count = len(tag_ids)
                    print(f"  OK: {email} -> {ga['group_name']} ({tag_count} tags)")
                except Exception as e:
                    print(f"  ERROR: {email}: {e}")
                    total_errors += 1
                time.sleep(0.5)

    print(f"\n{'='*60}")
    print(f"Done. Assigned: {total_fixed}, Errors: {total_errors}")


if __name__ == "__main__":
    main()
