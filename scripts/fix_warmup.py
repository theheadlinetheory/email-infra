#!/usr/bin/env python3
"""
Fix warmup settings on all Deeter Landscape accounts in SmartLead.
Uses the internal save-warmup endpoint to set rampup toggle, rampup value, and daily reply limit.
"""

import os
import sys
import time
import json
import requests
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
ENV_PATH = SCRIPT_DIR / ".env"

def load_env():
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
                os.environ[k.strip()] = v.strip()
    return env

ENV = load_env()

SMARTLEAD_API = "https://server.smartlead.ai/api/v1"
SMARTLEAD_KEY = ENV.get("SMARTLEAD_API_KEY", "")
SMARTLEAD_INTERNAL_API = "https://server.smartlead.ai/api"
SMARTLEAD_JWT = ENV.get("SMARTLEAD_JWT", "")

DEETER_DOMAINS = {
    "lawncarepros.co", "lawncaresupport.co", "lawncareworks.co", "lawncarezone.co",
    "lawncrewzone.co", "lawnmaintenancedirect.co", "lawnmaintenancefocus.co",
    "lawnmaintenancehub.co", "lawnmaintenanceinfo.co", "lawnmaintenanceprime.co",
    "lawnmaintenancezone.co", "lawnserviceally.co", "lawnservicebase.co",
    "lawnserviceconnect.co", "lawnservicedirect.co", "lawnservicefocus.co", "lawnservicehq.co"
}

def internal_headers():
    return {
        "Authorization": f"Bearer {SMARTLEAD_JWT}",
        "Content-Type": "application/json"
    }

def find_deeter_accounts():
    """Find all SmartLead account IDs for Deeter domains."""
    accounts = []
    offset = 0
    while True:
        url = f"{SMARTLEAD_API}/email-accounts/?api_key={SMARTLEAD_KEY}&offset={offset}&limit=100"
        r = requests.get(url, timeout=30)
        batch = r.json()
        if isinstance(batch, list):
            for acc in batch:
                email = acc.get("from_email", acc.get("email", ""))
                domain = email.split("@")[-1] if "@" in email else ""
                if domain in DEETER_DOMAINS:
                    accounts.append({"id": acc["id"], "email": email})
            if len(batch) < 100:
                break
            offset += 100
        else:
            print(f"  Error fetching accounts: {batch}")
            break
    return accounts

def fix_warmup(account_id):
    """Apply correct warmup settings via internal save-warmup endpoint."""
    headers = internal_headers()

    # Get warmup key ID
    wd = requests.get(
        f"{SMARTLEAD_INTERNAL_API}/email-account/fetch-warmup-details-by-email-account-id/{account_id}",
        headers=headers, timeout=30
    )
    if wd.status_code != 200:
        return {"error": f"fetch-warmup-details failed: {wd.status_code}"}

    warmup_data = wd.json().get("message", {})
    warmup_key = warmup_data.get("warmup_key_id", "")
    if not warmup_key:
        return {"error": "no warmup_key_id found"}

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
        "warmupKeyId": warmup_key
    }

    r = requests.post(
        f"{SMARTLEAD_INTERNAL_API}/email-account/save-warmup",
        headers=headers, json=body, timeout=30
    )
    if r.status_code == 200:
        return {"ok": True}
    return {"error": f"save-warmup failed: {r.status_code} {r.text[:200]}"}

def main():
    if not SMARTLEAD_JWT:
        print("ERROR: SMARTLEAD_JWT not set in .env")
        sys.exit(1)

    print("Finding Deeter Landscape accounts in SmartLead...")
    accounts = find_deeter_accounts()
    print(f"Found {len(accounts)} accounts across {len(DEETER_DOMAINS)} domains")

    success = 0
    fail = 0
    for i, acc in enumerate(accounts, 1):
        print(f"  [{i}/{len(accounts)}] {acc['email']} ... ", end="", flush=True)
        result = fix_warmup(acc["id"])
        if result.get("ok"):
            print("OK")
            success += 1
        else:
            print(f"FAIL: {result.get('error', 'unknown')}")
            fail += 1
        time.sleep(0.3)  # gentle rate limiting

    print(f"\nDone! {success} fixed, {fail} failed out of {len(accounts)} total")

if __name__ == "__main__":
    main()
