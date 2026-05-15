#!/usr/bin/env python3
"""Rebalance ready generic groups to ~14 domains / ~42 accounts each.

Minimal-moves approach: merge G+G2, fix split domains, then transfer
only the excess domains from oversized groups to undersized ones.
Also restores warmup date tags lost during migration.

K batch 2 (still warming) is excluded.

Usage: python3 rebalance_generics.py [--dry-run]
"""

from __future__ import annotations

import json
import re
import sys
import time
import requests
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from setup import (
    sl_get_all_tags, sl_find_or_create_tag, sl_tag_account,
    SMARTLEAD_API, SMARTLEAD_KEY,
    _RateLimiter,
)
from tag_utils import ZAPMAIL_TAG_ID
import db as store

DRY_RUN = "--dry-run" in sys.argv
_sl_rate = _RateLimiter(max_requests=150, window_seconds=60)
RATE_LIMIT_COOLDOWN = 45

GROUP_NAMES = [
    "Generic F", "Generic G", "Generic H", "Generic I",
    "Generic J", "Generic K", "Generic L", "Generic M",
]


def _sl_request(method, path, **kwargs):
    _sl_rate.wait()
    url = f"{SMARTLEAD_API}{path}"
    sep = "&" if "?" in url else "?"
    url += f"{sep}api_key={SMARTLEAD_KEY}"
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
            time.sleep(RATE_LIMIT_COOLDOWN)
            continue
        accounts.extend(batch)
        if len(batch) < 100:
            break
        offset += 100
    return accounts


def warmup_date_tag(warmup_created_at: str) -> str | None:
    if not warmup_created_at:
        return None
    try:
        dt = datetime.fromisoformat(warmup_created_at.replace("Z", "+00:00"))
        return f"{dt.month}/{dt.day}/{dt.strftime('%y')}"
    except (ValueError, AttributeError):
        return None


def rebalance():
    print("Fetching SmartLead data...")
    all_accounts = get_all_sl_accounts()
    clients_raw = _sl_request("GET", "/client")
    client_map = {c["id"]: c["name"] for c in clients_raw.json()} if clients_raw.status_code == 200 else {}
    all_tags = sl_get_all_tags()

    k_b2 = store.get_inbox_group("GEN-K", batch=2)
    k_b2_ids = set(k_b2.get("account_ids", []) if k_b2 else [])
    canopy_ids = set()
    for ig in store.get_all_inbox_groups():
        if "canopy" in ig.get("smartlead_client_name", "").lower():
            canopy_ids.update(ig.get("account_ids") or [])
    exclude_ids = k_b2_ids | canopy_ids
    print(f"  Excluding {len(k_b2_ids)} K-batch-2 + {len(canopy_ids)} Canopy = {len(exclude_ids)} accounts")

    # Build pool of ready generic accounts
    generic_accs = []
    for acc in all_accounts:
        acc_id = acc["id"]
        if acc_id in exclude_ids:
            continue
        client_name = client_map.get(acc.get("client_id"), "")
        if not client_name.lower().startswith("generic"):
            continue

        # Merge G2 into G
        group = "Generic G" if client_name == "Generic G2" else client_name

        domain = acc.get("from_email", "").split("@")[-1] if "@" in acc.get("from_email", "") else ""
        wd = acc.get("warmup_details") or {}
        wdate = warmup_date_tag(wd.get("warmup_created_at", ""))

        generic_accs.append({
            "id": acc_id,
            "email": acc.get("from_email", ""),
            "domain": domain,
            "group": group,
            "client_id": acc.get("client_id"),
            "warmup_date": wdate,
        })

    # Group by domain — all accounts on a domain must be in the same group
    by_domain = defaultdict(list)
    for a in generic_accs:
        by_domain[a["domain"]].append(a)

    # Fix split domains: assign to whichever group has the majority of accounts
    for domain, accs in by_domain.items():
        groups = [a["group"] for a in accs]
        if len(set(groups)) > 1:
            majority = max(set(groups), key=groups.count)
            print(f"  Fixing split domain {domain}: consolidating to {majority}")
            for a in accs:
                a["group"] = majority

    # Build current group -> domains mapping
    group_domains = defaultdict(set)
    for a in generic_accs:
        group_domains[a["group"]].add(a["domain"])

    total_domains = len(by_domain)
    n_groups = len(GROUP_NAMES)
    base_size = total_domains // n_groups
    remainder = total_domains % n_groups
    print(f"  Pool: {total_domains} domains, {len(generic_accs)} accounts")
    print(f"  Target: {n_groups} groups, {base_size} domains each + {remainder} groups with {base_size + 1}")

    # Assign target sizes: give the +1 to groups already at or above base_size+1
    targets = {}
    oversized = sorted(GROUP_NAMES, key=lambda g: len(group_domains.get(g, set())), reverse=True)
    for i, gn in enumerate(oversized):
        targets[gn] = base_size + 1 if i < remainder else base_size

    print(f"\n{'='*60}")
    print("CURRENT STATE vs TARGET")
    print(f"{'='*60}")
    surplus_pool = []
    deficit_groups = []
    for gn in GROUP_NAMES:
        current = len(group_domains.get(gn, set()))
        target = targets[gn]
        delta = current - target
        status = f"+{delta}" if delta > 0 else str(delta) if delta < 0 else "OK"
        print(f"  {gn}: {current} domains -> target {target} ({status})")
        if delta > 0:
            domains_list = sorted(group_domains[gn])
            give_domains = domains_list[-delta:]
            surplus_pool.extend(give_domains)
        elif delta < 0:
            deficit_groups.append((gn, -delta))

    # Assign surplus domains to deficit groups
    transfers = {}
    pool_idx = 0
    for gn, need in deficit_groups:
        for _ in range(need):
            if pool_idx < len(surplus_pool):
                domain = surplus_pool[pool_idx]
                transfers[domain] = gn
                pool_idx += 1

    # Apply transfers: update the group assignment for moved accounts
    move_count = 0
    print(f"\n{'='*60}")
    print(f"TRANSFERS ({len(transfers)} domains)")
    print(f"{'='*60}")
    for domain, new_group in sorted(transfers.items()):
        old_group = by_domain[domain][0]["group"]
        n_accs = len(by_domain[domain])
        print(f"  {domain} ({n_accs} accs): {old_group} -> {new_group}")
        for a in by_domain[domain]:
            a["group"] = new_group
            move_count += 1

    # Rebuild final group contents
    final_groups = defaultdict(list)
    for a in generic_accs:
        final_groups[a["group"]].append(a)

    print(f"\n{'='*60}")
    print("FINAL DISTRIBUTION")
    print(f"{'='*60}")
    for gn in GROUP_NAMES:
        accs = final_groups.get(gn, [])
        doms = sorted(set(a["domain"] for a in accs))
        dates = sorted(set(a["warmup_date"] or "?" for a in accs))
        print(f"  {gn}: {len(doms)} domains, {len(accs)} accounts, warmup: {dates}")

    # Count accounts needing re-tag (moved accounts + all accounts for warmup date restoration)
    needs_retag = [a for a in generic_accs]
    print(f"\nAccounts to re-tag (all, for warmup date restoration): {len(needs_retag)}")
    print(f"Accounts that changed group: {move_count}")

    if DRY_RUN:
        print("\n=== DRY RUN — no changes made ===")
        return

    # Re-tag ALL accounts with correct 3 tags
    print("\nApplying SmartLead tags...")
    errors = 0
    tagged = 0
    for gn in GROUP_NAMES:
        group_tag_id = sl_find_or_create_tag(gn, existing_tags=all_tags)
        all_tags[gn] = {"id": group_tag_id, "name": gn}

        for acc in final_groups.get(gn, []):
            tag_ids = [ZAPMAIL_TAG_ID, group_tag_id]
            if acc["warmup_date"]:
                wd_tag_id = sl_find_or_create_tag(acc["warmup_date"], existing_tags=all_tags)
                all_tags[acc["warmup_date"]] = {"id": wd_tag_id, "name": acc["warmup_date"]}
                tag_ids.append(wd_tag_id)

            for attempt in range(3):
                _sl_rate.wait()
                try:
                    sl_tag_account(acc["id"], tag_ids, client_id=acc["client_id"])
                    tagged += 1
                    if tagged % 50 == 0:
                        print(f"  Tagged {tagged}/{len(needs_retag)}...")
                    break
                except Exception as e:
                    if "429" in str(e) and attempt < 2:
                        print(f"  Rate limited, cooling down {RATE_LIMIT_COOLDOWN}s...")
                        time.sleep(RATE_LIMIT_COOLDOWN)
                    else:
                        print(f"  ERROR {acc['email']}: {e}")
                        errors += 1
                        break

    print(f"  Tagged {tagged}/{len(needs_retag)} total")

    # Update Supabase inbox_groups
    print("\nUpdating Supabase inbox_groups...")
    ig_all = store.get_all_inbox_groups()
    ig_by_tag = {}
    for ig in ig_all:
        tag = ig.get("group_tag") or ig.get("smartlead_client_name", "")
        if tag.lower().startswith("generic") and ig.get("status") != "warming":
            ig_by_tag[tag] = ig

    for gn in GROUP_NAMES:
        accs = final_groups.get(gn, [])
        new_ids = sorted(a["id"] for a in accs)
        new_emails = sorted(a["email"] for a in accs)
        new_domains = sorted(set(a["domain"] for a in accs))

        ig = ig_by_tag.get(gn)
        if ig:
            store.update_inbox_group(ig["id"],
                account_ids=new_ids,
                account_emails=new_emails,
                domains=new_domains,
                group_tag=gn,
            )
            print(f"  Updated {gn} (id={ig['id']}): {len(new_domains)} domains, {len(accs)} accounts")
        else:
            print(f"  WARNING: No Supabase record for {gn}")

    g2 = ig_by_tag.get("Generic G2")
    if g2:
        store.update_inbox_group(g2["id"],
            account_ids=[],
            account_emails=[],
            domains=[],
            status="dissolved",
        )
        print(f"  Dissolved Generic G2 (id={g2['id']})")

    print(f"\nRebalance complete. Tagged: {tagged}, Errors: {errors}")


if __name__ == "__main__":
    rebalance()
