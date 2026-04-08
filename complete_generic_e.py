#!/usr/bin/env python3
"""Complete Generic E pipeline: SmartLead export → warmup → tags → client → CSV.
Steps 1-6 already done. This picks up from step 7."""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from setup import (
    load_env, zm_export_mailboxes, sl_list_accounts, sl_set_warmup,
    sl_get_all_tags, sl_create_tag, sl_tag_accounts_bulk,
    export_for_sheet, save_config, log, log_step, _api_retry,
    SMARTLEAD_API, SMARTLEAD_KEY
)
import json
import time
import requests

ENV = load_env()

CONFIG_PATH = "clients/generic_e_20260407.json"
config = json.loads(open(CONFIG_PATH).read())
completed = config.get("steps_completed", [])
client = config["client_name"]
our_domains = {d["domain"] for d in config["purchased_domains"]}

print(f"\n{'='*60}")
print(f"  COMPLETING GENERIC E — steps remaining after: {', '.join(completed)}")
print(f"  Domains: {len(our_domains)}, Expected accounts: 48")
print(f"{'='*60}\n")

# ── STEP 7: Bulk export to SmartLead ──
if "smartlead_export" not in completed:
    log_step(7, 11, "EXPORT TO SMARTLEAD")

    # Collect all mailbox UUIDs from config
    all_mailbox_ids = []
    for d in config["purchased_domains"]:
        all_mailbox_ids.extend(d.get("mailbox_ids", []))

    log(f"Bulk exporting {len(all_mailbox_ids)} mailboxes to SmartLead...")
    result = zm_export_mailboxes(apps=["SMARTLEAD"], mailbox_ids=all_mailbox_ids)
    log(f"  Export result: {json.dumps(result)[:300]}")

    # Wait for accounts to appear in SmartLead
    log("Waiting for accounts to appear in SmartLead (polling every 30s, max 10 min)...")
    max_wait = 600
    poll_interval = 30
    waited = 0
    found_count = 0

    while waited < max_wait:
        time.sleep(poll_interval)
        waited += poll_interval

        found_ids = []
        offset = 0
        while True:
            batch = sl_list_accounts(offset=offset, limit=100)
            if isinstance(batch, list):
                for acc in batch:
                    email = acc.get("from_email", acc.get("email", ""))
                    domain = email.split("@")[-1] if "@" in email else ""
                    if domain in our_domains:
                        found_ids.append(acc["id"])
                if len(batch) < 100:
                    break
                offset += 100
            else:
                break

        found_count = len(found_ids)
        log(f"  [{waited}s] Found {found_count}/48 accounts in SmartLead")
        if found_count >= 48:
            break

    if found_count < 48:
        log(f"Only found {found_count}/48 after {max_wait}s. Checking which domains are missing...", "WARN")
        # Find missing domains
        found_domains = set()
        offset = 0
        while True:
            batch = sl_list_accounts(offset=offset, limit=100)
            if isinstance(batch, list):
                for acc in batch:
                    email = acc.get("from_email", acc.get("email", ""))
                    domain = email.split("@")[-1] if "@" in email else ""
                    if domain in our_domains:
                        found_domains.add(domain)
                if len(batch) < 100:
                    break
                offset += 100
            else:
                break
        missing = our_domains - found_domains
        if missing:
            log(f"Missing domains: {', '.join(missing)} — re-exporting individually...")
            for domain_name in missing:
                result = zm_export_mailboxes(apps=["SMARTLEAD"], contains=domain_name)
                log(f"  Re-export {domain_name}: {json.dumps(result)[:200]}")
                time.sleep(2)
            # Wait another 2 min for stragglers
            log("Waiting 2 more minutes for re-exported accounts...")
            time.sleep(120)

    completed.append("smartlead_export")
    config["steps_completed"] = completed
    save_config(config, CONFIG_PATH)
    log("SmartLead export step complete.")

# ── STEP 8: Set warmup on all accounts ──
if "smartlead_warmup" not in completed:
    log_step(8, 11, "SET WARMUP ON ALL ACCOUNTS")

    our_account_ids = []
    offset = 0
    while True:
        batch = sl_list_accounts(offset=offset, limit=100)
        if isinstance(batch, list):
            for acc in batch:
                email = acc.get("from_email", acc.get("email", ""))
                domain = email.split("@")[-1] if "@" in email else ""
                if domain in our_domains:
                    our_account_ids.append(acc["id"])
            if len(batch) < 100:
                break
            offset += 100
        else:
            break

    log(f"Found {len(our_account_ids)} accounts. Setting warmup...")
    for i, acc_id in enumerate(our_account_ids, 1):
        try:
            result = sl_set_warmup(acc_id)
            full = "✓ full" if isinstance(result, dict) and result.get("full_config") else "public only"
            log(f"  [{i}/{len(our_account_ids)}] Account {acc_id}: warmup set ({full})")
        except Exception as e:
            log(f"  [{i}/{len(our_account_ids)}] Account {acc_id}: FAILED - {e}", "ERROR")
        time.sleep(0.5)

    config["smartlead_account_ids"] = our_account_ids
    completed.append("smartlead_warmup")
    config["steps_completed"] = completed
    save_config(config, CONFIG_PATH)
    log("Warmup step complete.")

# ── STEP 9: Create/find tags and tag all accounts ──
if "smartlead_tags" not in completed:
    log_step(9, 11, "TAG ACCOUNTS IN SMARTLEAD")

    from datetime import datetime
    date_tag = datetime.now().strftime("%-m/%-d/%y")
    tag_names = [client, "Zapmail", date_tag]
    log(f"Tags needed: {tag_names}")

    all_tags = sl_get_all_tags()
    tag_ids = []
    for name in tag_names:
        if name in all_tags:
            tag_ids.append(all_tags[name]["id"])
            log(f"  Found existing tag: '{name}' (ID: {all_tags[name]['id']})")
        else:
            new_tag = sl_create_tag(name)
            if new_tag and new_tag.get("id"):
                tag_ids.append(new_tag["id"])
                log(f"  Created tag: '{name}' (ID: {new_tag['id']})")
            else:
                log(f"  Failed to create tag '{name}': {new_tag}", "ERROR")

    # Create/find SmartLead client
    sl_client_id = None
    try:
        sl_clients = requests.get(
            f"{SMARTLEAD_API}/client?api_key={SMARTLEAD_KEY}", timeout=30
        ).json()
        client_lower = client.lower().strip()
        for c in sl_clients:
            cn = c["name"].lower().strip()
            if cn == client_lower or client_lower in cn or cn in client_lower:
                sl_client_id = c["id"]
                log(f"  Matched SmartLead client: '{c['name']}' (ID: {sl_client_id})")
                break
        if not sl_client_id:
            slug = client.lower().replace("'", "").replace(" ", "").replace("&", "")
            cl_email = f"tht.{slug}.client@gmail.com"
            cr = requests.post(
                f"{SMARTLEAD_API}/client/save?api_key={SMARTLEAD_KEY}",
                json={"name": client, "email": cl_email, "password": "THTclient2026!"},
                timeout=30
            )
            if cr.status_code == 201:
                sl_client_id = cr.json().get("clientId")
                log(f"  Created SmartLead client: '{client}' (ID: {sl_client_id})")
            else:
                log(f"  Could not create SmartLead client: {cr.status_code} {cr.text[:200]}", "WARN")
    except Exception as e:
        log(f"  SmartLead client lookup failed: {e}", "WARN")

    config["smartlead_client_id"] = sl_client_id

    # Get account IDs
    our_account_ids = config.get("smartlead_account_ids", [])
    if not our_account_ids:
        offset = 0
        while True:
            batch = sl_list_accounts(offset=offset, limit=100)
            if isinstance(batch, list):
                for acc in batch:
                    email = acc.get("from_email", acc.get("email", ""))
                    domain = email.split("@")[-1] if "@" in email else ""
                    if domain in our_domains:
                        our_account_ids.append(acc["id"])
                if len(batch) < 100:
                    break
                offset += 100
            else:
                break

    if tag_ids and our_account_ids:
        log(f"Tagging {len(our_account_ids)} accounts with {len(tag_ids)} tags + client_id={sl_client_id}...")
        success, fail = sl_tag_accounts_bulk(our_account_ids, tag_ids, client_id=sl_client_id)
        log(f"  Tagged: {success} success, {fail} failed")
    else:
        log(f"  Skipping: {len(tag_ids)} tags, {len(our_account_ids)} accounts", "WARN")

    config["smartlead_tag_ids"] = tag_ids
    completed.append("smartlead_tags")
    config["steps_completed"] = completed
    save_config(config, CONFIG_PATH)
    log("Tagging step complete.")

# ── STEP 10: Export CSV ──
if "export_csv" not in completed:
    log_step(10, 11, "EXPORT CSV")

    csv_path, rows = export_for_sheet(config)
    log(f"Exported {len(rows)} rows to {csv_path}")
    config["export_csv_path"] = str(csv_path)

    completed.append("export_csv")
    config["steps_completed"] = completed
    save_config(config, CONFIG_PATH)

# Mark complete
config["status"] = "complete"
save_config(config, CONFIG_PATH)

print(f"\n{'='*60}")
print(f"  GENERIC E COMPLETE!")
print(f"  {len(our_domains)} domains, 48 accounts")
print(f"  Steps: {', '.join(completed)}")
print(f"{'='*60}\n")
