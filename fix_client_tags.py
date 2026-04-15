#!/usr/bin/env python3
"""Fix SmartLead tags for Canopy Land Solutions and Borja Landscaping Construction.

Required tag format: [Zapmail, ClientName, WarmupStartDate]
"""
import json
import time
import requests
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from setup import (
    sl_list_accounts, sl_tag_account, sl_internal_headers,
    SMARTLEAD_API, SMARTLEAD_KEY, SMARTLEAD_INTERNAL_API
)

# Tag IDs
ZAPMAIL_TAG = 262254
CANOPY_TAG = 370043
CANOPY_DATE_TAG = 351530  # 3/28/26
BORJA_TAG = 370045
BORJA_DATE_TAG = 351860   # 3/29/26

# Domain lists from config files
CANOPY_DOMAINS = [
    "turfcarepath.co", "turfcareprime.co", "turfcaresupport.co",
    "turfcareworks.co", "turfcarezone.co", "turfmaintenancedirect.co",
    "turfmaintenancehub.co", "turfmaintenancepros.co", "turfmaintenancezone.co",
    "turfservicecrew.co", "turfservicedirect.co", "turfservicefocus.co",
    "turfservicepoint.co", "turfserviceselect.co", "turfservicezone.co",
    "turfworkdirect.co", "turfworkzone.co",
]

BORJA_DOMAINS = [
    "yardandgroundscare.co", "yardandgroundsservice.co", "yardandlawnpros.co",
    "yardandoutdoorservice.co", "yardandscapecare.co", "yardandturfservice.co",
    "yardcarecenter.co", "yardcareconnect.co", "yardcareelite.co",
    "yardcarefocus.co", "yardcareguide.co", "yardcarehq.co",
    "yardcareinfo.co", "yardcarelead.co", "yardcarepath.co",
    "yardcarepoint.co", "yardcaresupport.co",
]


def get_all_accounts():
    all_accounts = []
    offset = 0
    while True:
        batch = sl_list_accounts(offset=offset, limit=100)
        if isinstance(batch, list):
            all_accounts.extend(batch)
            if len(batch) < 100:
                break
            offset += 100
        else:
            break
    return all_accounts


def fix_tags():
    print("Fetching all SmartLead accounts...")
    accounts = get_all_accounts()
    print(f"  {len(accounts)} total accounts")

    canopy_set = set(CANOPY_DOMAINS)
    borja_set = set(BORJA_DOMAINS)

    canopy_tags = [ZAPMAIL_TAG, CANOPY_TAG, CANOPY_DATE_TAG]
    borja_tags = [ZAPMAIL_TAG, BORJA_TAG, BORJA_DATE_TAG]

    canopy_count = 0
    borja_count = 0
    errors = 0

    for acc in accounts:
        email = acc.get("from_email", "")
        domain = email.split("@")[-1] if "@" in email else ""
        acc_id = acc["id"]

        if domain in canopy_set:
            target_tags = canopy_tags
            client_name = "Canopy Land Solutions"
        elif domain in borja_set:
            target_tags = borja_tags
            client_name = "Borja Landscaping Construction"
        else:
            continue

        result = sl_tag_account(acc_id, target_tags)
        if result.get("ok"):
            if domain in canopy_set:
                canopy_count += 1
            else:
                borja_count += 1
        else:
            print(f"  FAIL: {email} -> {result}")
            errors += 1
        time.sleep(0.3)

    print(f"\nDone!")
    print(f"  Canopy Land Solutions: {canopy_count}/51 tagged with {canopy_tags}")
    print(f"  Borja Landscaping Construction: {borja_count}/51 tagged with {borja_tags}")
    if errors:
        print(f"  Errors: {errors}")


if __name__ == "__main__":
    fix_tags()
