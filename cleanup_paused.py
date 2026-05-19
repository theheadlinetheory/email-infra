"""Remove all email accounts from PAUSED acquisition campaigns."""

import os
import sys
import time
import requests
from pathlib import Path

env_path = Path(__file__).parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

SMARTLEAD_API = "https://server.smartlead.ai/api/v1"
SMARTLEAD_KEY = os.environ.get("SMARTLEAD_API_KEY", "")

if not SMARTLEAD_KEY:
    print("ERROR: SMARTLEAD_API_KEY not set")
    sys.exit(1)


def api_get(url, params=None, timeout=30):
    params = params or {}
    params["api_key"] = SMARTLEAD_KEY
    for attempt in range(5):
        try:
            r = requests.get(url, params=params, timeout=timeout)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            if attempt < 4:
                time.sleep(10)
                continue
            return None
        if r.status_code == 429:
            wait = 10 * (2 ** attempt)
            print(f"  429 — waiting {wait}s...")
            time.sleep(wait)
            continue
        return r
    return None


def api_delete(url, body, timeout=60):
    for attempt in range(5):
        try:
            r = requests.delete(url, params={"api_key": SMARTLEAD_KEY}, json=body, timeout=timeout)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            if attempt < 4:
                time.sleep(10)
                continue
            return None
        if r.status_code == 429:
            wait = 15 * (2 ** attempt)
            print(f"  429 — waiting {wait}s...")
            time.sleep(wait)
            continue
        return r
    return None


print("=== Cleanup: Remove accounts from PAUSED acquisition campaigns ===\n")

print("Fetching all campaigns...")
r = api_get(f"{SMARTLEAD_API}/campaigns")
if not r or r.status_code != 200:
    print("Failed to fetch campaigns")
    sys.exit(1)

campaigns = r.json() if r.text.strip() else []
paused = [c for c in campaigns if c.get("status") == "PAUSED" and "acquisition" in c.get("name", "").lower()]
print(f"  {len(campaigns)} total campaigns, {len(paused)} paused acquisition campaigns\n")

if not paused:
    print("No paused acquisition campaigns found. Nothing to do.")
    sys.exit(0)

for c in sorted(paused, key=lambda x: x.get("name", "")):
    print(f"  - [{c['id']}] {c['name']}")

print(f"\nWill remove all email accounts from {len(paused)} paused campaigns.")
confirm = input("Proceed? (yes/no): ").strip().lower()
if confirm != "yes":
    print("Aborted.")
    sys.exit(0)

total_removed = 0
for i, camp in enumerate(paused):
    cid = camp["id"]
    cname = camp.get("name", "?")
    print(f"\n[{i+1}/{len(paused)}] {cname} (id={cid})")

    r = api_get(f"{SMARTLEAD_API}/campaigns/{cid}/email-accounts")
    if not r or r.status_code != 200:
        print(f"  Failed to fetch accounts: {r.status_code if r else 'timeout'}")
        continue

    accounts = r.json()
    if not accounts:
        print("  No accounts — skipping")
        continue

    account_ids = [a["id"] for a in accounts if a.get("id")]
    print(f"  {len(account_ids)} accounts to remove")

    r = api_delete(f"{SMARTLEAD_API}/campaigns/{cid}/email-accounts", {"email_account_ids": account_ids})
    if r and r.status_code == 200:
        print(f"  Removed {len(account_ids)} accounts")
        total_removed += len(account_ids)
    else:
        status = r.status_code if r else "timeout"
        text = r.text[:200] if r else ""
        print(f"  ERROR: {status} {text}")

    time.sleep(2)

print(f"\n=== Done: removed {total_removed} account assignments from {len(paused)} paused campaigns ===")
