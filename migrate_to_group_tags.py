#!/usr/bin/env python3
"""One-time migration: update all SmartLead + Zapmail tags to the new group tag format.

Run once to migrate existing accounts. Safe to re-run (idempotent).

Usage: python3 migrate_to_group_tags.py [--dry-run]
"""

import json
import re
import sys
import time
import requests
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from setup import (
    sl_get_all_tags, sl_find_or_create_tag, sl_tag_account,
    SMARTLEAD_API, SMARTLEAD_KEY,
    _RateLimiter, _api_retry,
)
from tag_utils import (
    get_group_tag_from_account, parse_group_tag,
    build_client_group_tag, build_acquisition_tag, ZAPMAIL_TAG_ID,
)
import db as store

DRY_RUN = "--dry-run" in sys.argv

_sl_rate = _RateLimiter(max_requests=150, window_seconds=60)
RATE_LIMIT_COOLDOWN = 45

ACQ_CLIENT_MAP = {
    "A Group (250/day)": "A",
    "B Group (250/day)": "B",
    "C Group (250/day)": "C",
    "D Group (250/day)": "D",
    "E Group (250/day)": "E",
    "F Group (250/day)": "F",
    "G Group (250/day)": "G",
    "H Group (250/day)": "H",
    "I Group (250/day)": "I",
    "J Group (250/day)": "J",
    "K Group (250/day)": "K",
    "L Group (250/day)": "L",
    "Acquisition Inboxes": None,
}


def _sl_request(method, path, **kwargs):
    """SmartLead request with proactive rate limiting and 429 auto-retry."""
    _sl_rate.wait()
    url = f"{SMARTLEAD_API}{path}"
    if "?" in url:
        url += f"&api_key={SMARTLEAD_KEY}"
    else:
        url += f"?api_key={SMARTLEAD_KEY}"
    r = requests.request(method, url, timeout=30, **kwargs)
    if r.status_code == 429:
        print(f"  Rate limited — cooling down {RATE_LIMIT_COOLDOWN}s...")
        time.sleep(RATE_LIMIT_COOLDOWN)
        _sl_rate.wait()
        r = requests.request(method, url, timeout=30, **kwargs)
    return r


def get_all_sl_accounts():
    accounts = []
    offset = 0
    while True:
        r = _sl_request("GET", f"/email-accounts/?offset={offset}&limit=100")
        batch = r.json() if r.status_code == 200 and isinstance(r.json(), list) else []
        if r.status_code == 429:
            print(f"  Still rate limited at offset {offset}, waiting another {RATE_LIMIT_COOLDOWN}s...")
            time.sleep(RATE_LIMIT_COOLDOWN)
            continue
        accounts.extend(batch)
        if len(batch) < 100:
            break
        offset += 100
    return accounts


def get_sl_clients():
    r = _sl_request("GET", "/client")
    return r.json() if r.status_code == 200 else []


def migrate():
    print("Fetching SmartLead data...")
    all_accounts = get_all_sl_accounts()
    clients = get_sl_clients()
    all_tags = sl_get_all_tags()
    rotations = store.get_all_rotations()

    client_map = {c["id"]: c["name"] for c in clients}
    rotation_map = {r["client_name"]: r for r in rotations}

    a_account_ids = set()
    b_account_ids = set()
    for r in rotations:
        a_ids = r.get("group_a_ids", [])
        b_ids = r.get("group_b_ids", [])
        if isinstance(a_ids, str):
            a_ids = json.loads(a_ids)
        if isinstance(b_ids, str):
            b_ids = json.loads(b_ids)
        a_account_ids.update(a_ids)
        b_account_ids.update(b_ids)

    stats = {"client_migrated": 0, "acquisition_migrated": 0, "generic_ok": 0, "skipped": 0, "errors": 0}

    for acc in all_accounts:
        acc_id = acc["id"]
        email = acc.get("from_email", "")
        client_id = acc.get("client_id")
        client_name = client_map.get(client_id, "")
        current_group_tag = get_group_tag_from_account(acc)

        target_tag = None

        if client_name in ACQ_CLIENT_MAP:
            letter = ACQ_CLIENT_MAP[client_name]
            if letter:
                target_tag = build_acquisition_tag(letter)
            else:
                stats["skipped"] += 1
                continue

        elif client_name.lower().startswith("generic"):
            if current_group_tag and current_group_tag.lower() == client_name.lower():
                stats["generic_ok"] += 1
                continue
            target_tag = client_name

        elif client_name:
            if acc_id in b_account_ids:
                ab = "B"
            elif acc_id in a_account_ids:
                ab = "A"
            else:
                ab = "A"
            target_tag = build_client_group_tag(client_name, ab)

        else:
            stats["skipped"] += 1
            continue

        if current_group_tag == target_tag:
            if client_name.lower().startswith("generic"):
                stats["generic_ok"] += 1
            else:
                stats["skipped"] += 1
            continue

        print(f"  {email}: '{current_group_tag}' -> '{target_tag}'")

        if DRY_RUN:
            if "acquisition" in (target_tag or "").lower():
                stats["acquisition_migrated"] += 1
            else:
                stats["client_migrated"] += 1
            continue

        try:
            _sl_rate.wait()
            tag_id = sl_find_or_create_tag(target_tag, existing_tags=all_tags)
            new_tag_ids = [ZAPMAIL_TAG_ID, tag_id]
            for t in acc.get("tags", []):
                if re.match(r'^\d{1,2}/\d{1,2}/\d{2}$', t.get("name", "")):
                    new_tag_ids.append(t["id"])
                    break

            for attempt in range(3):
                _sl_rate.wait()
                try:
                    sl_tag_account(acc_id, new_tag_ids, client_id=client_id)
                    break
                except Exception as tag_err:
                    if "429" in str(tag_err) and attempt < 2:
                        print(f"  Rate limited tagging {email}, cooling down {RATE_LIMIT_COOLDOWN}s...")
                        time.sleep(RATE_LIMIT_COOLDOWN)
                    else:
                        raise

            all_tags[target_tag] = {"id": tag_id, "name": target_tag}

            if "acquisition" in target_tag.lower():
                stats["acquisition_migrated"] += 1
            else:
                stats["client_migrated"] += 1
        except Exception as e:
            print(f"  ERROR on {email}: {e}")
            stats["errors"] += 1

    # Update inbox_groups Supabase records
    print("\nUpdating Supabase inbox_groups...")
    ig_groups = store.get_all_inbox_groups()
    for ig in ig_groups:
        old_name = ig.get("smartlead_client_name", "")
        assigned = ig.get("assigned_client")
        role = ig.get("role", "generic")

        if role == "generic" and not assigned:
            new_tag = old_name
        elif assigned:
            rotation = rotation_map.get(assigned)
            ab = "A"
            if rotation:
                a_ids = rotation.get("group_a_ids", [])
                b_ids = rotation.get("group_b_ids", [])
                if isinstance(a_ids, str):
                    a_ids = json.loads(a_ids)
                if isinstance(b_ids, str):
                    b_ids = json.loads(b_ids)
                ig_account_ids = set(ig.get("account_ids") or [])
                if ig_account_ids & set(b_ids):
                    ab = "B"
            new_tag = build_client_group_tag(assigned, ab)
        else:
            new_tag = old_name

        if new_tag != ig.get("group_tag"):
            print(f"  inbox_group {ig['id']}: '{ig.get('group_tag')}' -> '{new_tag}'")
            if not DRY_RUN:
                store.update_inbox_group(ig["id"], group_tag=new_tag)

    print(f"\nMigration {'(DRY RUN) ' if DRY_RUN else ''}complete:")
    print(f"  Client accounts migrated: {stats['client_migrated']}")
    print(f"  Acquisition accounts migrated: {stats['acquisition_migrated']}")
    print(f"  Generic already correct: {stats['generic_ok']}")
    print(f"  Skipped: {stats['skipped']}")
    print(f"  Errors: {stats['errors']}")


if __name__ == "__main__":
    migrate()
