#!/usr/bin/env python3
"""One-time script: assign all email accounts to their SmartLead clients.
Uses domain -> client mapping from local configs + SmartLead client list."""

import json
import glob
import time
import requests
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from setup import (
    sl_list_accounts, sl_tag_account, sl_internal_headers,
    SMARTLEAD_API, SMARTLEAD_KEY, SMARTLEAD_INTERNAL_API
)


def get_smartlead_clients():
    """Get all SmartLead clients. Returns {name_lower: {id, name, ...}}."""
    r = requests.get(f"{SMARTLEAD_API}/client?api_key={SMARTLEAD_KEY}", timeout=30)
    clients = r.json()
    return {c["name"].lower().strip(): c for c in clients}


def create_smartlead_client(name, email=None):
    """Create a new client in SmartLead."""
    if email is None:
        slug = name.lower().replace(" ", "-")
        email = f"{slug}@theheadlinetheory.com"
    r = requests.post(
        f"{SMARTLEAD_API}/client/save?api_key={SMARTLEAD_KEY}",
        json={"name": name, "email": email},
        timeout=30,
    )
    if r.status_code == 200:
        try:
            return r.json()
        except Exception:
            pass
    r2 = requests.post(
        f"{SMARTLEAD_INTERNAL_API}/client/save",
        headers=sl_internal_headers(),
        json={"name": name, "email": email},
        timeout=30,
    )
    try:
        return r2.json()
    except Exception:
        return {"error": f"Failed to create client: {r2.status_code} {r2.text[:200]}"}


def build_domain_client_map():
    """Build domain -> client_name from local config files."""
    domain_map = {}
    configs = glob.glob("clients/*.json")
    for path in configs:
        try:
            c = json.load(open(path))
        except Exception:
            continue
        name = c.get("client_name", "")
        if not name or name == "TEST-Run":
            continue
        for d in c.get("purchased_domains", []):
            domain_map[d["domain"]] = name
    return domain_map


def get_all_accounts():
    """Fetch all SmartLead accounts with pagination."""
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
    return all_accounts


def main():
    print("Building domain -> client map from configs...")
    domain_map = build_domain_client_map()
    print(f"  {len(domain_map)} domains mapped to clients")

    print("\nFetching SmartLead clients...")
    sl_clients = get_smartlead_clients()
    print(f"  {len(sl_clients)} clients in SmartLead")

    # Ensure Generic A and Generic B exist as clients
    for name in ["Generic A", "Generic B"]:
        if name.lower() not in sl_clients:
            print(f"  Creating client: {name}")
            result = create_smartlead_client(name)
            print(f"    Result: {result}")
    # Refresh client list
    sl_clients = get_smartlead_clients()

    def find_client_id(client_name):
        low = client_name.lower().strip()
        if low in sl_clients:
            return sl_clients[low]["id"]
        for sl_name, sl_data in sl_clients.items():
            if low in sl_name or sl_name in low:
                return sl_data["id"]
        return None

    print("\nFetching all SmartLead accounts...")
    accounts = get_all_accounts()
    print(f"  {len(accounts)} total accounts")

    assigned = 0
    skipped = 0
    unmatched = 0
    unmatched_domains = set()

    for acc in accounts:
        email = acc.get("from_email", "")
        domain = email.split("@")[-1] if "@" in email else ""
        acc_id = acc["id"]
        current_client = acc.get("client_id")

        client_name = domain_map.get(domain)
        if not client_name:
            unmatched += 1
            if domain:
                unmatched_domains.add(domain)
            continue

        target_client_id = find_client_id(client_name)
        if not target_client_id:
            print(f"  WARN: No SmartLead client found for '{client_name}'")
            unmatched += 1
            continue

        if current_client == target_client_id:
            skipped += 1
            continue

        # Fetch current tags so we don't wipe them when setting clientId
        current_tags = acc.get("tags") or []
        if isinstance(current_tags, list) and current_tags and isinstance(current_tags[0], dict):
            current_tags = [t["id"] for t in current_tags]
        body = {"id": acc_id, "clientId": target_client_id, "tags": current_tags}
        r = requests.post(
            f"{SMARTLEAD_INTERNAL_API}/email-account/save-management-details",
            headers=sl_internal_headers(),
            json=body,
            timeout=30,
        )
        if r.status_code == 200 and r.json().get("ok"):
            assigned += 1
        else:
            print(f"  FAIL: {email} -> {client_name}: {r.text[:200]}")
        time.sleep(0.2)

    print(f"\nDone!")
    print(f"  Assigned: {assigned}")
    print(f"  Already correct: {skipped}")
    print(f"  Unmatched: {unmatched}")
    if unmatched_domains:
        print(f"  Unmatched domains: {', '.join(sorted(unmatched_domains)[:10])}")


if __name__ == "__main__":
    main()
