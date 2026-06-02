#!/usr/bin/env python3
"""Create 4 new generic Sean Reynolds groups (O, P, Q, R) from available domains.

Pipeline:
  1. Connect fresh .info domains to Zapmail (if not already connected)
  2. Create 3 mailboxes per domain (s.reynolds, sean.r, sean.reynolds)
  3. Set profile photos
  4. Export to SmartLead
  5. Tag with Zapmail + Generic O/P/Q/R + 6/2/26
  6. Enable warmup
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from setup import (
    INBOX_SPECS, PROFILE_PHOTO_URL,
    zm_connect_domain_single, zm_create_mailboxes, zm_list_domains,
    zm_export_mailboxes, zm_buy_addon_mailboxes, zm_put, zm_get,
    zm_list_mailboxes, zm_get_workspace_id,
    sl_get_all_tags, sl_find_or_create_tag, sl_pick_unique_color,
    sl_set_warmup, sl_list_accounts, sl_tag_account,
    log,
)
from tag_utils import ZAPMAIL_TAG_ID

import db

GROUPS = {
    "O": [],
    "P": [],
    "Q": [],
    "R": [],
}

DOMAINS_PER_GROUP = 14
ACCOUNTS_PER_DOMAIN = 3
TODAY_TAG = "6/2/26"


def get_available_domains():
    """Get domains from the ready pool (.info) + fresh .info from Spaceship May 8 batch."""
    pool_raw = db._request("GET", "/state", params={"select": "data", "key": "eq.domain_ready_pool"})
    pool_data = json.loads(pool_raw[0]["data"]) if pool_raw else {}
    pool_domains = pool_data.get("domains", [])

    info_ready = [d for d in pool_domains if d["domain"].endswith(".info")]
    log(f"Ready pool .info domains: {len(info_ready)}")

    import requests
    spaceship_key = os.environ["SPACESHIP_API_KEY"]
    spaceship_secret = os.environ["SPACESHIP_SECRET_KEY"]
    headers = {"X-Api-Key": spaceship_key, "X-Api-Secret": spaceship_secret}

    overview, _ = db.cache_get("overview_v2")
    sl_domains = set()
    for section in ["clients", "acquisition_groups", "generic_groups", "aging_groups"]:
        for g in overview.get(section, []):
            for a in g.get("account_details", []):
                email = a.get("email", "") or ""
                if "@" in email:
                    sl_domains.add(email.split("@")[1])

    ready_names = {d["domain"] for d in info_ready}

    all_spaceship = []
    skip = 0
    while True:
        resp = requests.get(f"https://spaceship.dev/api/v1/domains?take=100&skip={skip}",
                            headers=headers, timeout=30)
        data = resp.json()
        items = data.get("items", [])
        if not items:
            break
        all_spaceship.extend(items)
        total = data.get("total", 0)
        skip += 100
        if skip >= total:
            break

    fresh_info = []
    for d in all_spaceship:
        name = d["name"]
        if not name.endswith(".info"):
            continue
        if name in sl_domains or name in ready_names:
            continue
        reg = d.get("registrationDate", "")[:10]
        ns_hosts = d.get("nameservers", {}).get("hosts", [])
        has_cloudns = any("cloudns" in h.lower() for h in ns_hosts)
        if reg == "2026-05-08" and has_cloudns:
            fresh_info.append({"domain": name, "zapmail_id": None, "source": "spaceship_may8"})

    log(f"Fresh May 8 .info domains (CloudNS set): {len(fresh_info)}")
    return info_ready, fresh_info


def assign_domains_to_groups(ready, fresh):
    """Assign 14 domains per group, using ready pool first, then fresh."""
    all_domains = []
    for d in ready:
        all_domains.append({"domain": d["domain"], "zapmail_id": d.get("zapmail_id"), "source": "ready_pool"})
    for d in fresh:
        all_domains.append(d)

    needed = DOMAINS_PER_GROUP * len(GROUPS)
    if len(all_domains) < needed:
        log(f"WARNING: Only {len(all_domains)} domains available, need {needed}", "WARN")

    idx = 0
    for letter in sorted(GROUPS.keys()):
        for _ in range(DOMAINS_PER_GROUP):
            if idx >= len(all_domains):
                break
            GROUPS[letter].append(all_domains[idx])
            idx += 1

    for letter, doms in sorted(GROUPS.items()):
        log(f"  Generic {letter}: {len(doms)} domains")


def step1_connect_to_zapmail():
    """Connect fresh domains to Zapmail (ready pool already connected)."""
    log("STEP 1: Connect fresh domains to Zapmail")

    fresh_domains = []
    for doms in GROUPS.values():
        for d in doms:
            if d["source"] == "spaceship_may8":
                fresh_domains.append(d)

    if not fresh_domains:
        log("  All domains already connected to Zapmail")
        return

    log(f"  Connecting {len(fresh_domains)} fresh domains...")
    for d in fresh_domains:
        result = zm_connect_domain_single(d["domain"])
        if isinstance(result, dict) and result.get("status") not in (400, 500):
            log(f"  Connected: {d['domain']}")
        else:
            log(f"  Issue connecting {d['domain']}: {json.dumps(result)[:200]}", "WARN")
        time.sleep(1)

    log("  Waiting 60s for DNS propagation...")
    time.sleep(60)

    # Poll until all are ACTIVE or timeout
    max_polls = 20
    poll_interval = 30
    fresh_names = {d["domain"] for d in fresh_domains}

    for poll in range(max_polls):
        all_zm = zm_list_domains()
        zm_map = {d.get("name", d.get("domain", "")): d for d in all_zm}

        still_pending = []
        for d in fresh_domains:
            zm_d = zm_map.get(d["domain"])
            if zm_d and zm_d.get("dnsStatus") == "VERIFIED":
                d["zapmail_id"] = zm_d.get("id")
            elif zm_d:
                still_pending.append(f"{d['domain']} ({zm_d.get('dnsStatus', '?')})")
            else:
                still_pending.append(f"{d['domain']} (not found)")

        if not still_pending:
            log(f"  All {len(fresh_domains)} domains are VERIFIED in Zapmail!")
            break

        log(f"  Poll {poll+1}/{max_polls}: {len(still_pending)} still pending")
        if poll < max_polls - 1:
            time.sleep(poll_interval)
    else:
        log(f"  WARNING: {len(still_pending)} domains still not verified", "WARN")
        for p in still_pending:
            log(f"    {p}", "WARN")


def step2_create_mailboxes():
    """Create 3 Sean Reynolds mailboxes per domain."""
    log("STEP 2: Create mailboxes")

    workspace_id = zm_get_workspace_id()

    # Check how many mailbox slots we need
    total_needed = sum(len(doms) for doms in GROUPS.values()) * ACCOUNTS_PER_DOMAIN
    ws_result = zm_get("/v2/workspaces", workspace_id)
    ws_data = {}
    if isinstance(ws_result, dict):
        ws_data = ws_result.get("data", {}).get("currentWorkspace", {})
    total_purchased = int(ws_data.get("totalMailboxesPurchasedGoogle", "0"))
    total_assigned = int(ws_data.get("assignedMailboxesCountGoogle", "0"))
    unassigned = total_purchased - total_assigned

    slots_to_buy = max(0, total_needed - unassigned)
    if slots_to_buy > 0:
        log(f"  Buying {slots_to_buy} mailbox slots (have {unassigned}, need {total_needed})")
        buy_result = zm_buy_addon_mailboxes(slots_to_buy, workspace_id)
        if isinstance(buy_result, dict) and "Insufficient" in str(buy_result.get("message", "")):
            log(f"  ERROR: {buy_result.get('message')}", "ERROR")
            sys.exit(1)
        log(f"  Bought: {json.dumps(buy_result)[:200]}")
        time.sleep(3)
    else:
        log(f"  Have {unassigned} slots — enough for {total_needed} new mailboxes")

    # Create mailboxes
    all_mailbox_ids = {}  # domain -> [ids]
    for letter, doms in sorted(GROUPS.items()):
        log(f"  Creating mailboxes for Generic {letter}...")
        for d in doms:
            domain_id = d.get("zapmail_id")
            if not domain_id:
                log(f"    {d['domain']}: no zapmail_id — skipping", "WARN")
                continue

            result = zm_create_mailboxes(domain_id, d["domain"], INBOX_SPECS, workspace_id)
            if isinstance(result, dict) and result.get("status") not in (400, 422, 500):
                mb_ids = result.get("data", [])
                all_mailbox_ids[d["domain"]] = mb_ids if isinstance(mb_ids, list) else []
                d["mailbox_ids"] = all_mailbox_ids[d["domain"]]
                emails = [f"{s['mailboxUsername']}@{d['domain']}" for s in INBOX_SPECS]
                log(f"    {d['domain']}: {', '.join(emails)}")
            else:
                log(f"    {d['domain']}: FAILED — {json.dumps(result)[:200]}", "WARN")
                # Retry once after delay
                time.sleep(10)
                result = zm_create_mailboxes(domain_id, d["domain"], INBOX_SPECS, workspace_id)
                if isinstance(result, dict) and result.get("status") not in (400, 422, 500):
                    mb_ids = result.get("data", [])
                    all_mailbox_ids[d["domain"]] = mb_ids if isinstance(mb_ids, list) else []
                    d["mailbox_ids"] = all_mailbox_ids[d["domain"]]
                    log(f"    {d['domain']}: retry succeeded")
                else:
                    log(f"    {d['domain']}: retry also FAILED", "ERROR")

            time.sleep(1)

    return all_mailbox_ids


def step3_profile_photos(all_mailbox_ids):
    """Set Sean Reynolds profile photo on all mailboxes."""
    log("STEP 3: Set profile photos")

    all_ids = []
    for ids in all_mailbox_ids.values():
        all_ids.extend(ids)

    if not all_ids:
        # Fallback: fetch from Zapmail
        log("  No stored mailbox IDs — fetching from Zapmail...")
        all_zm = zm_list_domains()
        our_domains = set()
        for doms in GROUPS.values():
            for d in doms:
                our_domains.add(d["domain"])
        for zd in all_zm:
            if zd.get("name", zd.get("domain", "")) in our_domains:
                for mb in zd.get("mailboxes", []):
                    if mb.get("id"):
                        all_ids.append(mb["id"])

    log(f"  Setting photos on {len(all_ids)} mailboxes...")
    batch_size = 20
    success = 0
    for i in range(0, len(all_ids), batch_size):
        batch = all_ids[i:i + batch_size]
        mailbox_data = [{"mailboxId": mid, "profilePicture": PROFILE_PHOTO_URL} for mid in batch]
        result = zm_put("/v2/mailboxes", {"mailboxData": mailbox_data})
        if isinstance(result, dict) and result.get("status") == 200:
            success += len(batch)
        else:
            log(f"  Batch issue: {json.dumps(result)[:200]}", "WARN")
        time.sleep(1)
    log(f"  Photos set on {success}/{len(all_ids)} mailboxes")


def step4_export_to_smartlead(all_mailbox_ids):
    """Export all mailboxes to SmartLead via Zapmail bulk export."""
    log("STEP 4: Export to SmartLead")

    # Wait for mailboxes to reach ACTIVE in Zapmail
    log("  Waiting for mailboxes to activate...")
    our_domains = set()
    for doms in GROUPS.values():
        for d in doms:
            our_domains.add(d["domain"])

    max_polls = 24
    for poll in range(max_polls):
        all_zm = zm_list_domains()
        active = 0
        pending = 0
        for zd in all_zm:
            if zd.get("name", zd.get("domain", "")) in our_domains:
                for mb in zd.get("mailboxes", []):
                    if mb.get("status") == "ACTIVE":
                        active += 1
                    else:
                        pending += 1

        expected = len(our_domains) * ACCOUNTS_PER_DOMAIN
        if pending == 0 and active >= expected:
            log(f"  All {active} mailboxes ACTIVE!")
            break

        log(f"  Poll {poll+1}: {active}/{expected} ACTIVE, {pending} pending")
        if poll < max_polls - 1:
            time.sleep(30)
    else:
        log(f"  Proceeding with {active} ACTIVE, {pending} still pending", "WARN")

    # Collect all mailbox IDs
    mb_ids = []
    for ids in all_mailbox_ids.values():
        mb_ids.extend(ids)

    if not mb_ids:
        log("  No stored IDs — fetching from Zapmail API...")
        all_zm = zm_list_domains()
        for zd in all_zm:
            if zd.get("name", zd.get("domain", "")) in our_domains:
                for mb in zd.get("mailboxes", []):
                    if mb.get("id"):
                        mb_ids.append(mb["id"])

    if mb_ids:
        log(f"  Bulk exporting {len(mb_ids)} mailboxes...")
        result = zm_export_mailboxes(apps=["SMARTLEAD"], mailbox_ids=mb_ids)
        log(f"  Export result: {json.dumps(result)[:300]}")
    else:
        log("  No mailbox IDs found — exporting by domain name...")
        for domain in our_domains:
            result = zm_export_mailboxes(apps=["SMARTLEAD"], contains=domain)
            log(f"  {domain}: {json.dumps(result)[:200]}")
            time.sleep(2)

    log("  Waiting 3 minutes for SmartLead to process export...")
    time.sleep(180)


def step5_tag_in_smartlead():
    """Tag all accounts with Zapmail + Generic letter + date tag."""
    log("STEP 5: Tag accounts in SmartLead")

    existing_tags = sl_get_all_tags()

    # Find or create the date tag
    date_tag_id = sl_find_or_create_tag(TODAY_TAG, existing_tags=existing_tags)

    # Find or create group tags
    group_tag_ids = {}
    for letter in sorted(GROUPS.keys()):
        tag_name = f"Generic {letter}"
        tag_id = sl_find_or_create_tag(tag_name, existing_tags=existing_tags)
        group_tag_ids[letter] = tag_id
        log(f"  Tag '{tag_name}': ID {tag_id}")

    # Find accounts in SmartLead by domain
    our_domains = {}
    for letter, doms in GROUPS.items():
        for d in doms:
            our_domains[d["domain"]] = letter

    log("  Scanning SmartLead for new accounts...")
    offset = 0
    found = {}  # account_id -> letter
    max_scan = 2000
    while offset < max_scan:
        accounts = sl_list_accounts(offset=offset, limit=100)
        if not isinstance(accounts, list) or not accounts:
            break
        for acc in accounts:
            email = acc.get("from_email", acc.get("email", ""))
            domain = email.split("@")[-1] if "@" in email else ""
            if domain in our_domains:
                acc_id = acc.get("id")
                letter = our_domains[domain]
                found[acc_id] = letter
        offset += 100

    log(f"  Found {len(found)} accounts to tag")

    # Tag each account with 3 tags: Zapmail + group + date
    success = 0
    for acc_id, letter in found.items():
        tag_ids = [ZAPMAIL_TAG_ID, group_tag_ids[letter], date_tag_id]
        result = sl_tag_account(acc_id, tag_ids)
        if result.get("ok"):
            success += 1
        else:
            log(f"  Tag failed for {acc_id}: {result}", "WARN")

    log(f"  Tagged {success}/{len(found)} accounts")


def step6_enable_warmup():
    """Enable warmup on all new accounts."""
    log("STEP 6: Enable warmup")

    our_domains = set()
    for doms in GROUPS.values():
        for d in doms:
            our_domains.add(d["domain"])

    offset = 0
    accounts_to_warm = []
    max_scan = 2000
    while offset < max_scan:
        accounts = sl_list_accounts(offset=offset, limit=100)
        if not isinstance(accounts, list) or not accounts:
            break
        for acc in accounts:
            email = acc.get("from_email", acc.get("email", ""))
            domain = email.split("@")[-1] if "@" in email else ""
            if domain in our_domains:
                accounts_to_warm.append(acc.get("id"))
        offset += 100

    log(f"  Enabling warmup on {len(accounts_to_warm)} accounts...")
    success = 0
    for acc_id in accounts_to_warm:
        result = sl_set_warmup(acc_id)
        if result:
            success += 1
        time.sleep(0.5)

    log(f"  Warmup enabled on {success}/{len(accounts_to_warm)} accounts")


def main():
    log("=" * 60)
    log("CREATE GENERIC GROUPS O, P, Q, R")
    log("=" * 60)

    ready, fresh = get_available_domains()
    assign_domains_to_groups(ready, fresh)

    for letter, doms in sorted(GROUPS.items()):
        log(f"\nGeneric {letter}:")
        for d in doms:
            log(f"  {d['domain']} ({d['source']})")

    step1_connect_to_zapmail()
    all_mb_ids = step2_create_mailboxes()
    step3_profile_photos(all_mb_ids)
    step4_export_to_smartlead(all_mb_ids)
    step5_tag_in_smartlead()
    step6_enable_warmup()

    log("\n" + "=" * 60)
    log("COMPLETE! 4 new generic groups created:")
    for letter, doms in sorted(GROUPS.items()):
        log(f"  Generic {letter}: {len(doms)} domains, {len(doms)*3} inboxes")
    log("=" * 60)


if __name__ == "__main__":
    main()
