#!/usr/bin/env python3
"""Assign generic groups as B groups for clients most in need.

Mapping (by priority):
  1. Timesavers  → Generic F (49 accts) — CRITICAL health
  2. Coastal     → Generic G (48 accts) — CRITICAL bounce
  3. Lightning   → Generic H (48 accts) — HIGH bounce
  4. Canopy      → Generic I (47 accts) — HIGH bounce
  5. Pioneer     → Generic J (36 accts) — MEDIUM bounce
  6. Dallas      → Generic K (33 accts) — MEDIUM bounce
  Reserve: Generic L (35), Generic M (35)

Actions per generic group:
  1. Add "Group B" tag (395388) to all accounts (preserving existing tags)
  2. Enable warmup on all accounts
  3. Save mapping to clients/b_group_assignments.json

Usage:
  python3 assign_b_groups.py          # Dry run
  python3 assign_b_groups.py --apply  # Execute
"""

import json
import sys
import os
import time

sys.path.insert(0, os.path.dirname(__file__))
from setup import (
    sl_list_accounts, sl_gql, sl_tag_account,
    SMARTLEAD_API, SMARTLEAD_KEY, SMARTLEAD_JWT,
    SMARTLEAD_INTERNAL_API, sl_internal_headers, log,
)
import requests

GROUP_B_TAG = 395388

B_GROUP_MAP = {
    "Generic F": {"client_id": 352787, "tag_id": 370966, "serves": "Timesavers Landscaping", "serves_client_id": 325076},
    "Generic G": {"client_id": 352788, "tag_id": 365405, "serves": "Coastal Lawn Care", "serves_client_id": 325077},
    "Generic H": {"client_id": 352789, "tag_id": 370967, "serves": "Lightning Lawn Care", "serves_client_id": 325078},
    "Generic I": {"client_id": 352790, "tag_id": 370968, "serves": "Canopy Land Solutions", "serves_client_id": 350068},
    "Generic J": {"client_id": 407482, "tag_id": 394514, "serves": "Pioneer Landscaping", "serves_client_id": 328149},
    "Generic K": {"client_id": 407483, "tag_id": 394515, "serves": "Dallas Land Care", "serves_client_id": 325080},
}

WARMUP_CONFIG = {
    "warmup_enabled": True,
    "total_warmup_per_day": 30,
    "daily_rampup": 5,
    "reply_rate_percentage": 30,
}


def get_account_tags_batch(account_ids):
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


def enable_warmup(account_id):
    for attempt in range(3):
        try:
            resp = requests.post(
                f"{SMARTLEAD_API}/email-accounts/{account_id}/warmup",
                params={"api_key": SMARTLEAD_KEY},
                json=WARMUP_CONFIG,
                timeout=15,
            )
            if resp.status_code == 200:
                return True
            if resp.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            return False
        except Exception:
            time.sleep(5)
    return False


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
            batch = sl_list_accounts(limit=100, offset=offset)
        if not batch:
            break
        all_accounts.extend(batch)
        if len(batch) < 100:
            break
        offset += 100
        time.sleep(1)
    log(f"Total accounts: {len(all_accounts)}")

    log("\n" + "=" * 60)
    log("B GROUP ASSIGNMENT PLAN")
    log("=" * 60)

    for name, info in B_GROUP_MAP.items():
        accts = [a for a in all_accounts if a.get("client_id") == info["client_id"]]
        log(f"  {name} ({len(accts)} accts) → B group for {info['serves']}")

    if not apply:
        log("\n[DRY RUN] No changes made. Run with --apply to execute.")
        return

    log("\n" + "=" * 60)
    log("EXECUTING B GROUP ASSIGNMENTS")
    log("=" * 60)

    results = {}

    for name, info in B_GROUP_MAP.items():
        accts = [a for a in all_accounts if a.get("client_id") == info["client_id"]]
        if not accts:
            log(f"\n{name}: no accounts found, skipping")
            continue

        log(f"\n{name} → {info['serves']} ({len(accts)} accounts)")
        acc_ids = [a["id"] for a in accts]

        # Get current tags in batches of 15
        all_tag_map = {}
        for i in range(0, len(acc_ids), 15):
            chunk = acc_ids[i:i + 15]
            batch_tags = get_account_tags_batch(chunk)
            all_tag_map.update(batch_tags)
            time.sleep(0.8)

        tag_ok = 0
        tag_skip = 0
        tag_fail = 0
        warmup_ok = 0
        warmup_fail = 0

        for a in accts:
            aid = a["id"]
            email = a.get("from_email", "?")
            current_tags = all_tag_map.get(aid, [])

            # Add Group B tag if not present
            if GROUP_B_TAG in current_tags:
                tag_skip += 1
            else:
                new_tags = current_tags + [GROUP_B_TAG]
                result = sl_tag_account(aid, new_tags, client_id=info["client_id"])
                if result.get("ok"):
                    tag_ok += 1
                else:
                    tag_fail += 1
                    log(f"  FAIL tag {email}: {result}")
                time.sleep(0.4)

            # Enable warmup
            if enable_warmup(aid):
                warmup_ok += 1
            else:
                warmup_fail += 1
                log(f"  FAIL warmup {email}")
            time.sleep(0.3)

        log(f"  Tags: {tag_ok} added, {tag_skip} already had, {tag_fail} failed")
        log(f"  Warmup: {warmup_ok} enabled, {warmup_fail} failed")

        results[name] = {
            "generic_client_id": info["client_id"],
            "generic_tag_id": info["tag_id"],
            "serves_client": info["serves"],
            "serves_client_id": info["serves_client_id"],
            "account_count": len(accts),
            "tags_applied": tag_ok,
            "warmup_enabled": warmup_ok,
        }

    # Save mapping
    mapping_path = os.path.join(os.path.dirname(__file__), "clients", "b_group_assignments.json")
    with open(mapping_path, "w") as f:
        json.dump(results, f, indent=2)
    log(f"\nMapping saved to {mapping_path}")

    log("\n" + "=" * 60)
    log("B GROUP ASSIGNMENT COMPLETE")
    log("=" * 60)
    for name, r in results.items():
        log(f"  {name} → {r['serves_client']}: {r['account_count']} accts, {r['tags_applied']} tagged, {r['warmup_enabled']} warmup")


if __name__ == "__main__":
    main()
