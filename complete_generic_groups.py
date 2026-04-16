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
    sl_tag_accounts_bulk, sl_tag_account, log,
    SMARTLEAD_API, SMARTLEAD_KEY,
)
import requests

# ── Config ──
STATE_FILE = os.path.join(os.path.dirname(__file__), "generic_groups_state.json")
SETUP_FILE = os.path.join(os.path.dirname(__file__), "generic_groups_setup.json")
STATUS_FILE = os.path.join(os.path.dirname(__file__), "generic_groups_status.json")

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


def update_status(step_name, progress, detail=""):
    """Write a dashboard-readable status file."""
    status = {
        "step": step_name,
        "progress": progress,
        "detail": detail,
        "updated_at": datetime.now().isoformat(),
        "groups": list(GROUPS.keys()),
    }
    with open(STATUS_FILE, "w") as f:
        json.dump(status, f, indent=2)


# ── Step 1: Wait for all mailboxes to be ACTIVE ──
def wait_for_active(state):
    if step_done(state, "mailboxes_active"):
        log("Mailboxes already confirmed ACTIVE — skipping")
        return

    log("Step 1: Waiting for all mailboxes to reach ACTIVE status...")
    log("  (Will poll every 5 min indefinitely — survives laptop sleep)")
    poll = 0
    while True:
        poll += 1
        try:
            all_zm = zm_list_domains()
        except Exception as e:
            log(f"  Poll {poll}: API error ({e}), retrying in 5 min...")
            time.sleep(300)
            continue
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

        log(f"  Poll {poll}: {active}/{total} ACTIVE")
        update_status("wait_active", active / total if total > 0 else 0,
                      f"{active}/{total} mailboxes ACTIVE")

        if active == total and total > 0:
            state["mailbox_ids"] = mb_ids
            save_state(state)
            mark_done(state, "mailboxes_active")
            log(f"  All {total} mailboxes ACTIVE!")
            return

        time.sleep(300)  # 5 minutes between checks


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

    update_status("smartlead_export", 0.5, "Export sent, waiting 3 min for processing...")
    log("  Waiting 3 minutes for export to process...")
    time.sleep(180)
    update_status("smartlead_export", 1.0, "Export complete")
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
        update_status("smartlead_verify", len(found) / expected if expected > 0 else 0,
                      f"Found {len(found)}/{expected} accounts (attempt {attempt + 1})")

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
def _tag_single_with_retry(account_id, tag_ids, client_id, max_retries=3):
    """Tag a single account with retries on timeout."""
    for attempt in range(max_retries):
        try:
            result = sl_tag_account(account_id, tag_ids, client_id)
            return result.get("ok", False)
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(5 * (attempt + 1))
            else:
                log(f"    Tag failed after {max_retries} retries for {account_id}: {e}", "WARN")
                return False


def tag_and_assign(state):
    if step_done(state, "tagged"):
        log("Tagging already done — skipping")
        return

    log("Step 4: Tagging accounts and assigning to clients...")
    account_map = state.get("smartlead_account_ids", {})
    if not account_map:
        log("  No SmartLead account IDs — cannot tag", "ERROR")
        return

    total_accounts = len(account_map)
    tagged_so_far = 0

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
        success = 0
        fail = 0
        for i, aid in enumerate(group_account_ids):
            ok = _tag_single_with_retry(aid, tag_ids, client_id)
            if ok:
                success += 1
            else:
                fail += 1
            tagged_so_far += 1
            if (i + 1) % 10 == 0:
                update_status("tag_assign", tagged_so_far / total_accounts,
                              f"{group_name}: {i+1}/{len(group_account_ids)}")
            time.sleep(0.5)

        log(f"    Tagged: {success} success, {fail} failed")
        update_status("tag_assign", tagged_so_far / total_accounts,
                      f"{group_name}: done ({success} ok, {fail} fail)")

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
        ok = False
        for attempt in range(3):
            try:
                r = requests.post(
                    f"{SMARTLEAD_API}/email-accounts/{aid}/warmup",
                    params={"api_key": SMARTLEAD_KEY},
                    json=warmup_config,
                    timeout=30,
                )
                if r.status_code == 200:
                    ok = True
                    break
                elif attempt < 2:
                    time.sleep(5)
                else:
                    if fail <= 3:
                        log(f"    Warmup fail for {email}: {r.status_code} {r.text[:100]}")
            except Exception as e:
                if attempt < 2:
                    time.sleep(5)
                else:
                    if fail <= 3:
                        log(f"    Warmup error for {email}: {e}")
        if ok:
            success += 1
        else:
            fail += 1

        if (i + 1) % 20 == 0:
            log(f"  Progress: {i + 1}/{total}")
            update_status("enable_warmup", (i + 1) / total,
                          f"{i + 1}/{total} accounts")
        time.sleep(0.3)

    log(f"  Warmup enabled: {success}/{total} ({fail} failed)")
    update_status("enable_warmup", 1.0, f"Done: {success}/{total} ({fail} failed)")
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

    update_status("complete", 1.0, "All 4 generic groups set up!")
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
