#!/usr/bin/env python3
"""Poll Zapmail connect-domain until all .info domains are connected and ACTIVE."""

import subprocess
import sys
import time
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

from setup import zm_connect_domains, zm_list_domains, zm_get_workspace_id

DOMAINS = [
    "gotheheadlinetheory.info",
    "gotheheadlinetheoryagency.info",
    "gotheheadlinetheoryco.info",
    "gotheheadlinetheorygroup.info",
    "gotheheadlinetheorylab.info",
    "headlinetheory360.info",
    "headlinetheory360co.info",
    "headlinetheory360group.info",
    "headlinetheory360hub.info",
    "headlinetheory360pro.info",
    "headlinetheorydot.info",
    "headlinetheorydotco.info",
    "headlinetheorydothub.info",
    "theheadlinetheory360.info",
    "theheadlinetheoryclub.info",
    "theheadlinetheoryclubhq.info",
    "theheadlinetheoryclublab.info",
    "theheadlinetheoryclubpro.info",
    "theheadlinetheorygrowth.info",
    "theheadlinetheorygrowthhq.info",
    "theheadlinetheorygrowthlab.info",
    "theheadlinetheoryhq.info",
    "theheadlinetheorylab.info",
    "theheadlinetheoryrev.info",
    "theheadlinetheoryrevagency.info",
    "theheadlinetheoryrevco.info",
    "theheadlinetheoryrevhub.info",
    "theheadlinetheoryteam.info",
    "theheadlinetheoryteamhq.info",
    "theheadlinetheoryteampro.info",
    "theheadlinetheoryzoom.info",
    "theheadlinetheoryzoomco.info",
    "theheadlinetheoryzoomlab.info",
]

POLL_INTERVAL = 300  # 5 minutes
TIMEOUT_DAYS = 3
STATUS_FILE = os.path.join(os.path.dirname(__file__), "ns_propagation_status.json")


def load_status():
    if os.path.exists(STATUS_FILE):
        with open(STATUS_FILE) as f:
            return json.load(f)
    return {"connected": [], "started": datetime.now().isoformat()}


def save_status(status):
    with open(STATUS_FILE, "w") as f:
        json.dump(status, f, indent=2)


def notify(message):
    subprocess.run([
        "osascript", "-e",
        f'display notification "{message}" with title "Zapmail Domains"'
    ])
    print(f"[NOTIFY] {message}")


def main():
    status = load_status()
    start_time = datetime.fromisoformat(status["started"])
    deadline = start_time + timedelta(days=TIMEOUT_DAYS)
    already_connected = set(status.get("connected", []))

    workspace_id = zm_get_workspace_id()

    print(f"Polling {len(DOMAINS)} .info domains every {POLL_INTERVAL}s")
    print(f"Workspace: {workspace_id}")
    print(f"Started: {start_time.isoformat()}")
    print(f"Timeout: {deadline.isoformat()}")
    print(f"Already connected: {len(already_connected)}/{len(DOMAINS)}")
    print()

    while True:
        now = datetime.now()
        if now > deadline:
            notify(f"TIMEOUT: {len(already_connected)}/{len(DOMAINS)} connected after {TIMEOUT_DAYS} days.")
            save_status({"connected": list(already_connected), "started": status["started"], "timed_out": True})
            break

        remaining = [d for d in DOMAINS if d not in already_connected]
        if not remaining:
            notify(f"All 33 .info domains connected to Zapmail!")
            save_status({"connected": list(already_connected), "started": status["started"], "completed": now.isoformat()})
            break

        # Step 1: Call connect-domain to trigger Zapmail to check/create zones
        try:
            connect_result = zm_connect_domains(remaining, workspace_key=workspace_id)
            statuses = connect_result.get("data", {}).get("domains", {})
            success_count = sum(1 for s in statuses.values() if s == "SUCCESS")
            not_reg = sum(1 for s in statuses.values() if s == "DOMAIN_NOT_REGISTERED")
        except Exception as e:
            print(f"[{now.strftime('%H:%M:%S')}] Connect error: {e}")
            statuses = {}
            success_count = 0
            not_reg = len(remaining)

        # Step 2: Check domain list for any that went ACTIVE
        try:
            all_domains = zm_list_domains(workspace_id)
            active_names = {d.get("domain", "") for d in all_domains if d.get("status") == "ACTIVE"}
        except Exception:
            active_names = set()

        newly_connected = []
        for domain in remaining:
            if domain in active_names or statuses.get(domain) == "SUCCESS":
                newly_connected.append(domain)
                already_connected.add(domain)

        if newly_connected:
            count = len(already_connected)
            print(f"[{now.strftime('%H:%M:%S')}] +{len(newly_connected)} connected → {count}/{len(DOMAINS)} total")
            for d in newly_connected:
                print(f"  + {d}")
            save_status({"connected": list(already_connected), "started": status["started"]})

            if count == len(DOMAINS):
                notify(f"All 33 .info domains connected to Zapmail!")
                save_status({"connected": list(already_connected), "started": status["started"], "completed": now.isoformat()})
                break
        else:
            elapsed = now - start_time
            hours = elapsed.total_seconds() / 3600
            print(f"[{now.strftime('%H:%M:%S')}] {len(already_connected)}/{len(DOMAINS)} connected ({hours:.1f}h) | API: {success_count} ok, {not_reg} not_registered")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
