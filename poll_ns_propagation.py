#!/usr/bin/env python3
"""Poll .info domain NS propagation until all resolve to Zapmail ClouDNS nameservers."""

import subprocess
import time
import json
import os
from datetime import datetime, timedelta

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

EXPECTED_NS = {"pns61.cloudns.net", "pns62.cloudns.com", "pns63.cloudns.net", "pns64.cloudns.uk"}
POLL_INTERVAL = 300  # 5 minutes
TIMEOUT_DAYS = 3
STATUS_FILE = os.path.join(os.path.dirname(__file__), "ns_propagation_status.json")


def check_domain(domain):
    try:
        result = subprocess.run(
            ["dig", "+short", "NS", domain],
            capture_output=True, text=True, timeout=10
        )
        ns_records = {line.strip().rstrip(".") for line in result.stdout.strip().split("\n") if line.strip()}
        return ns_records >= EXPECTED_NS
    except Exception:
        return False


def load_status():
    if os.path.exists(STATUS_FILE):
        with open(STATUS_FILE) as f:
            return json.load(f)
    return {"propagated": [], "started": datetime.now().isoformat()}


def save_status(status):
    with open(STATUS_FILE, "w") as f:
        json.dump(status, f, indent=2)


def notify(message):
    subprocess.run([
        "osascript", "-e",
        f'display notification "{message}" with title "NS Propagation"'
    ])
    print(f"[NOTIFY] {message}")


def main():
    status = load_status()
    start_time = datetime.fromisoformat(status["started"])
    deadline = start_time + timedelta(days=TIMEOUT_DAYS)
    already_propagated = set(status.get("propagated", []))

    print(f"Polling {len(DOMAINS)} .info domains every {POLL_INTERVAL}s")
    print(f"Started: {start_time.isoformat()}")
    print(f"Timeout: {deadline.isoformat()}")
    print(f"Already propagated: {len(already_propagated)}/{len(DOMAINS)}")
    print()

    while True:
        now = datetime.now()
        if now > deadline:
            notify(f"TIMEOUT: {len(already_propagated)}/{len(DOMAINS)} propagated after {TIMEOUT_DAYS} days. Manual check needed.")
            save_status({"propagated": list(already_propagated), "started": status["started"], "timed_out": True})
            break

        remaining = [d for d in DOMAINS if d not in already_propagated]
        if not remaining:
            notify(f"All 33 .info domains propagated! Ready for Zapmail.")
            save_status({"propagated": list(already_propagated), "started": status["started"], "completed": now.isoformat()})
            break

        newly_propagated = []
        for domain in remaining:
            if check_domain(domain):
                newly_propagated.append(domain)
                already_propagated.add(domain)

        if newly_propagated:
            count = len(already_propagated)
            print(f"[{now.strftime('%H:%M:%S')}] +{len(newly_propagated)} propagated → {count}/{len(DOMAINS)} total")
            for d in newly_propagated:
                print(f"  ✓ {d}")
            save_status({"propagated": list(already_propagated), "started": status["started"]})

            if count == len(DOMAINS):
                notify(f"All 33 .info domains propagated! Ready for Zapmail.")
                save_status({"propagated": list(already_propagated), "started": status["started"], "completed": now.isoformat()})
                break
        else:
            elapsed = now - start_time
            hours = elapsed.total_seconds() / 3600
            print(f"[{now.strftime('%H:%M:%S')}] {len(already_propagated)}/{len(DOMAINS)} propagated ({hours:.1f}h elapsed)")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
