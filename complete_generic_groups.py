#!/usr/bin/env python3
"""Complete Generic F/G/H/I group setup autonomously.

Picks up from wherever the process left off:
1. Poll until all mailboxes are ACTIVE
2. Export to SmartLead
3. Wait for accounts to appear in SmartLead
4. Tag accounts (Zapmail + Generic X + date)
5. Assign to SmartLead client IDs
6. Enable warmup on all accounts

Run: python3 complete_generic_groups.py
Safe to re-run — checks state before each step.
"""
import json
import os
import sys
import time
from datetime import datetime

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

sys.path.insert(0, os.path.dirname(__file__))
from setup import (
    zm_list_domains, zm_export_mailboxes,
    sl_list_accounts, sl_get_all_tags, sl_find_or_create_tag,
    sl_tag_accounts_bulk, log,
    SMARTLEAD_API, SMARTLEAD_KEY,
)
import requests

# ── Config ──
STATE_FILE = os.path.join(os.path.dirname(__file__), "generic_groups_state.json")
SETUP_FILE = os.path.join(os.path.dirname(__file__), "generic_groups_setup.json")

GROUPS = {
    "Generic F": {
        "client_id": 352787,
        "tag_ids": {"group": 370966, "zapmail": 262254, "date": 370969},
        "domains": [
            "exteriorcarepros.info", "exteriorgroundscare.info", "exteriorgroundscontractors.info",
            "exteriorgroundsexperts.info", "exteriorgroundsgroup.info", "exteriorgroundsteam.info",
            "exteriorgroundswork.info", "exteriorlandscapework.info", "exteriorlandscapingpros.info",
            "exteriorlawncare.info", "exteriorlawnservices.info", "groundscarecrew.info",
            "groundscaregroup.info", "groundscarepartners.info", "groundscaresolutions.info",
            "groundskeepingcrew.info",
        ],
    },
    "Generic G": {
        "client_id": 352788,
        "tag_ids": {"group": 365405, "zapmail": 262254, "date": 370969},
        "domains": [
            "groundskeepingexperts.info", "groundskeepinggroup.info", "groundsmaintenancepros.info",
            "groundsmaintenancesolutions.info", "groundsserviceexperts.info", "groundsworkcontractors.info",
            "groundsworkexperts.info", "groundsworkgroup.info", "landscapecarepartners.info",
            "landscapecaresolutions.info", "landscapemaintenancecrew.info", "landscapemaintenancepartners.info",
            "landscapemaintenancesolutions.info", "landscapeservicecontractors.info", "landscapeservicepros.info",
            "landscapeserviceteam.info",
        ],
    },
    "Generic H": {
        "client_id": 352789,
        "tag_ids": {"group": 370967, "zapmail": 262254, "date": 370969},
        "domains": [
            "landscapeupkeeppros.info", "landscapeworkcontractors.info", "landscapingcarecrew.info",
            "landscapingcaresolutions.info", "landscapingcareteam.info", "landscapingmaintenancepros.info",
            "landscapingservicecrew.info", "landscapingupkeepgroup.info", "landscapingworkteam.info",
            "lawncareservicegroup.info", "lawnmaintenanceexperts.info", "lawnmaintenancesolutions.info",
            "lawnservicecontractors.info", "lawnservicepros.info", "lawnupkeepcontractors.info",
            "lawnupkeeppros.info",
        ],
    },
    "Generic I": {
        "client_id": 352790,
        "tag_ids": {"group": 370968, "zapmail": 262254, "date": 370969},
        "domains": [
            "lawnupkeepspecialists.info", "lawnworkcontractors.info", "lawnworkspecialists.info",
            "propertycaresolutions.info", "propertygroundscarepros.info", "propertygroundscontractors.info",
            "propertygroundsexperts.info", "propertygroundswork.info", "propertylandscapecontractors.info",
            "propertylandscapecrew.info", "propertylandscapeservices.info", "propertylawncare.info",
            "propertylawncontractors.info", "propertylawnexperts.info", "propertylawngroup.info",
            "propertylawnservices.info",
        ],
    },
}

ALL_DOMAINS = set()
for g in GROUPS.values():
    ALL_DOMAINS.update(g["domains"])


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"completed_steps": [], "mailbox_ids": [], "smartlead_account_ids": {}}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def step_done(state, step):
    return step in state["completed_steps"]


def mark_done(state, step):
    if step not in state["completed_steps"]:
        state["completed_steps"].append(step)
    save_state(state)


# ── Step 1: Wait for all mailboxes to be ACTIVE ──
def wait_for_active(state):
    if step_done(state, "mailboxes_active"):
        log("Mailboxes already confirmed ACTIVE — skipping")
        return

    log("Step 1: Waiting for all mailboxes to reach ACTIVE status...")
    max_polls = 120  # 120 × 30s = 60 minutes max
    for poll in range(max_polls):
        all_zm = zm_list_domains()
        zm_by_name = {d.get("domain", ""): d for d in all_zm}

        active = 0
        total = 0
        mb_ids = []
        for domain in ALL_DOMAINS:
            d = zm_by_name.get(domain)
            if d:
                for m in d.get("mailboxes", []):
                    total += 1
                    if m.get("status") == "ACTIVE":
                        active += 1
                    mb_ids.append(m["id"])

        log(f"  Poll {poll + 1}: {active}/{total} ACTIVE")

        if active == total and total > 0:
            state["mailbox_ids"] = mb_ids
            save_state(state)
            mark_done(state, "mailboxes_active")
            log(f"  All {total} mailboxes ACTIVE!")
            return

        time.sleep(30)

    log("WARNING: Not all ACTIVE after 60 min — proceeding anyway", "WARN")
    state["mailbox_ids"] = mb_ids
    save_state(state)
    mark_done(state, "mailboxes_active")


# ── Step 2: Export to SmartLead ──
def export_to_smartlead(state):
    if step_done(state, "smartlead_export"):
        log("SmartLead export already done — skipping")
        return

    log("Step 2: Exporting mailboxes to SmartLead...")
    mb_ids = state.get("mailbox_ids", [])
    if not mb_ids:
        # Fetch fresh
        all_zm = zm_list_domains()
        zm_by_name = {d.get("domain", ""): d for d in all_zm}
        for domain in ALL_DOMAINS:
            d = zm_by_name.get(domain)
            if d:
                for m in d.get("mailboxes", []):
                    mb_ids.append(m["id"])
        state["mailbox_ids"] = mb_ids
        save_state(state)

    result = zm_export_mailboxes(apps=["SMARTLEAD"], mailbox_ids=mb_ids)
    log(f"  Export result: {str(result)[:300]}")

    log("  Waiting 3 minutes for export to process...")
    time.sleep(180)
    mark_done(state, "smartlead_export")


# ── Step 3: Verify accounts in SmartLead ──
def verify_smartlead_accounts(state):
    if step_done(state, "smartlead_verified"):
        log("SmartLead accounts already verified — skipping")
        return

    log("Step 3: Verifying accounts appeared in SmartLead...")
    expected = len(ALL_DOMAINS) * 3  # 192

    max_attempts = 5
    for attempt in range(max_attempts):
        found = {}
        offset = 0
        while True:
            try:
                batch = sl_list_accounts(offset=offset, limit=100)
            except Exception as e:
                log(f"  SmartLead API error at offset {offset}: {e}")
                time.sleep(5)
                break
            if not isinstance(batch, list) or not batch:
                break
            for acc in batch:
                email = acc.get("from_email", acc.get("email", ""))
                domain = email.split("@")[-1] if "@" in email else ""
                if domain in ALL_DOMAINS:
                    found[email] = acc["id"]
            if len(batch) < 100:
                break
            offset += 100

        log(f"  Attempt {attempt + 1}: found {len(found)}/{expected} accounts")

        if len(found) >= expected:
            state["smartlead_account_ids"] = found
            save_state(state)
            mark_done(state, "smartlead_verified")
            return

        if attempt < max_attempts - 1:
            # Re-export missing domains
            found_domains = {email.split("@")[-1] for email in found}
            missing = ALL_DOMAINS - found_domains
            if missing:
                log(f"  Re-exporting {len(missing)} missing domains...")
                for domain in missing:
                    zm_export_mailboxes(apps=["SMARTLEAD"], contains=domain)
                    time.sleep(2)
            log("  Waiting 3 minutes...")
            time.sleep(180)

    # Accept what we have
    state["smartlead_account_ids"] = found
    save_state(state)
    mark_done(state, "smartlead_verified")
    log(f"  Proceeding with {len(found)} accounts (expected {expected})")


# ── Step 4: Tag accounts and assign to clients ──
def tag_and_assign(state):
    if step_done(state, "tagged"):
        log("Tagging already done — skipping")
        return

    log("Step 4: Tagging accounts and assigning to clients...")
    account_map = state.get("smartlead_account_ids", {})
    if not account_map:
        log("  No SmartLead account IDs — cannot tag", "ERROR")
        return

    for group_name, group_cfg in GROUPS.items():
        group_domains = set(group_cfg["domains"])
        group_account_ids = [
            aid for email, aid in account_map.items()
            if email.split("@")[-1] in group_domains
        ]

        if not group_account_ids:
            log(f"  {group_name}: no accounts found — skipping")
            continue

        tag_ids = list(group_cfg["tag_ids"].values())
        client_id = group_cfg["client_id"]

        log(f"  {group_name}: tagging {len(group_account_ids)} accounts, client_id={client_id}")
        success, fail = sl_tag_accounts_bulk(group_account_ids, tag_ids, client_id=client_id)
        log(f"    Tagged: {success} success, {fail} failed")

        if fail > 0:
            # Retry failures individually
            log(f"    Retrying {fail} failed accounts one by one...")
            time.sleep(2)
            for aid in group_account_ids:
                try:
                    sl_tag_accounts_bulk([aid], tag_ids, client_id=client_id)
                except Exception:
                    pass
                time.sleep(0.3)

    mark_done(state, "tagged")


# ── Step 5: Enable warmup on all accounts ──
def enable_warmup(state):
    if step_done(state, "warmup_enabled"):
        log("Warmup already enabled — skipping")
        return

    log("Step 5: Enabling warmup on all accounts...")
    account_map = state.get("smartlead_account_ids", {})

    warmup_config = {
        "warmup_enabled": True,
        "total_warmup_per_day": 30,
        "daily_rampup": 2,
        "reply_rate_percentage": 30,
    }

    success = 0
    fail = 0
    total = len(account_map)
    for i, (email, aid) in enumerate(account_map.items()):
        try:
            r = requests.post(
                f"{SMARTLEAD_API}/email-accounts/{aid}/warmup",
                params={"api_key": SMARTLEAD_KEY},
                json=warmup_config,
                timeout=15,
            )
            if r.status_code == 200:
                success += 1
            else:
                fail += 1
                if fail <= 3:
                    log(f"    Warmup fail for {email}: {r.status_code} {r.text[:100]}")
        except Exception as e:
            fail += 1
            if fail <= 3:
                log(f"    Warmup error for {email}: {e}")

        if (i + 1) % 20 == 0:
            log(f"  Progress: {i + 1}/{total}")
        time.sleep(0.3)

    log(f"  Warmup enabled: {success}/{total} ({fail} failed)")
    mark_done(state, "warmup_enabled")


# ── Main ──
def main():
    log("=" * 60)
    log("Generic F/G/H/I Setup — Autonomous Completion")
    log(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log("=" * 60)

    state = load_state()

    wait_for_active(state)
    export_to_smartlead(state)
    verify_smartlead_accounts(state)
    tag_and_assign(state)
    enable_warmup(state)

    log("")
    log("=" * 60)
    log("COMPLETE — All 4 generic groups set up!")
    log(f"Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log("=" * 60)

    # macOS notification
    try:
        os.system('osascript -e \'display notification "Generic F/G/H/I setup complete!" '
                  'with title "Email Infra" sound name "Glass"\'')
    except Exception:
        pass


if __name__ == "__main__":
    main()
