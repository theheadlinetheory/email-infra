#!/usr/bin/env python3
"""Enable warmup on all headlinetheory .info acquisition accounts.

Usage:
  python3 enable_info_warmup.py --dry-run   # Preview
  python3 enable_info_warmup.py              # Execute
"""

import argparse
import os
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

SMARTLEAD_API = "https://server.smartlead.ai/api/v1"
SMARTLEAD_INTERNAL_API = "https://server.smartlead.ai/api"
SMARTLEAD_KEY = os.environ.get("SMARTLEAD_API_KEY", "")
SMARTLEAD_JWT = os.environ.get("SMARTLEAD_JWT", "")


def internal_headers():
    return {"Authorization": f"Bearer {SMARTLEAD_JWT}", "Content-Type": "application/json"}


def get_all_accounts():
    accounts = []
    offset = 0
    while True:
        r = requests.get(
            f"{SMARTLEAD_API}/email-accounts/?api_key={SMARTLEAD_KEY}&offset={offset}&limit=100",
            timeout=30,
        )
        if r.status_code == 429:
            print("  Rate limited, waiting 30s...")
            time.sleep(30)
            continue
        batch = r.json() if r.status_code == 200 else []
        if not isinstance(batch, list) or not batch:
            break
        accounts.extend(batch)
        if len(batch) < 100:
            break
        offset += 100
        time.sleep(0.5)
    return accounts


def enable_warmup(account_id):
    """Fetch warmup key, then enable warmup with standard settings."""
    headers = internal_headers()

    r = requests.get(
        f"{SMARTLEAD_INTERNAL_API}/email-account/fetch-warmup-details-by-email-account-id/{account_id}",
        headers=headers,
        timeout=30,
    )
    if r.status_code != 200:
        return {"error": f"fetch-warmup failed: {r.status_code}"}

    warmup_data = r.json().get("message", {})
    warmup_key = warmup_data.get("warmup_key_id", "")
    if not warmup_key:
        return {"error": "no warmup_key_id"}

    body = {
        "emailAccountId": str(account_id),
        "maxEmailPerDay": 15,
        "isRampupEnabled": True,
        "rampupValue": 5,
        "warmupMinCount": 10,
        "warmupMaxCount": 15,
        "replyRate": 40,
        "dailyReplyLimit": 15,
        "autoAdjustWarmup": False,
        "sendWarmupsOnlyOnWeekdays": False,
        "useCustomDomain": False,
        "status": "ACTIVE",
        "warmupKeyId": warmup_key,
    }

    r = requests.post(
        f"{SMARTLEAD_INTERNAL_API}/email-account/save-warmup",
        headers=headers,
        json=body,
        timeout=30,
    )
    if r.status_code == 200:
        return {"ok": True}
    return {"error": f"save-warmup failed: {r.status_code} {r.text[:200]}"}


def main():
    parser = argparse.ArgumentParser(description="Enable warmup on .info acquisition accounts")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not SMARTLEAD_JWT or not SMARTLEAD_KEY:
        print("ERROR: SMARTLEAD_JWT and SMARTLEAD_API_KEY required")
        sys.exit(1)

    mode = "DRY RUN" if args.dry_run else "EXECUTE"
    print(f"=== Enable Warmup on .info Accounts ({mode}) ===\n")

    print("Fetching accounts...")
    accounts = get_all_accounts()
    if not accounts:
        print("ERROR: Got 0 accounts — likely rate limited.")
        sys.exit(1)
    print(f"  {len(accounts)} total accounts")

    # Filter to headlinetheory .info accounts
    ht_info = [
        a for a in accounts
        if "headlinetheory" in a.get("from_email", "") and a.get("from_email", "").endswith(".info")
    ]
    print(f"  {len(ht_info)} headlinetheory .info accounts")

    if args.dry_run:
        print(f"\n[DRY RUN] Would enable warmup on {len(ht_info)} accounts.")
        return

    print(f"\nEnabling warmup...")
    success = 0
    fail = 0

    for i, acc in enumerate(ht_info, 1):
        email = acc.get("from_email", "?")
        if i % 10 == 0 or i <= 3:
            print(f"  [{i}/{len(ht_info)}] {email} ... ", end="", flush=True)

        result = enable_warmup(acc["id"])

        if result.get("ok"):
            if i % 10 == 0 or i <= 3:
                print("OK")
            success += 1
        else:
            print(f"FAIL: {result.get('error', 'unknown')}" if i % 10 != 0 and i > 3 else "")
            print(f"  FAIL [{i}] {email}: {result.get('error', 'unknown')}")
            fail += 1
        time.sleep(0.5)

    print(f"\n{'='*60}")
    print(f"Done. Enabled: {success}, Failed: {fail}")


if __name__ == "__main__":
    main()
