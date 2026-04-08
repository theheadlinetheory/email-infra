#!/usr/bin/env python3
"""Set profile photos on all Generic A, B, D, E mailboxes via Zapmail API.
Uses PUT /v2/mailboxes with {mailboxData: [{mailboxId, profilePicture}]}
"""

import json, sys, os, time
sys.path.insert(0, os.path.dirname(__file__))
from setup import load_env, zm_list_domains, ZAPMAIL_API, zm_headers
import requests

load_env()

PHOTO_URL = "https://ui-avatars.com/api/?name=Sean+Reynolds&size=256&background=4A90D9&color=ffffff&bold=true"

# Step 1: Get all domains from Zapmail (includes mailbox IDs)
print("Fetching all Zapmail domains...")
all_zm_domains = zm_list_domains()
domain_to_mailboxes = {}
for d in all_zm_domains:
    mbs = d.get("mailboxes", [])
    if mbs:
        domain_to_mailboxes[d["domain"]] = [mb["id"] for mb in mbs]
print(f"Total domains with mailboxes: {len(domain_to_mailboxes)}")

# Step 2: Collect mailbox IDs from each Generic group config
all_ids = []
for f in ["generic_a_20260328.json", "generic_b_20260329.json",
          "generic_d_20260407.json", "generic_e_20260407.json"]:
    path = f"clients/{f}"
    if not os.path.exists(path):
        print(f"SKIP: {path} not found")
        continue
    cfg = json.loads(open(path).read())
    name = cfg["client_name"]
    count = 0
    for d in cfg["purchased_domains"]:
        domain_name = d["domain"]
        # Prefer stored mailbox_ids, fallback to API
        stored = d.get("mailbox_ids", [])
        if stored:
            all_ids.extend(stored)
            count += len(stored)
        elif domain_name in domain_to_mailboxes:
            all_ids.extend(domain_to_mailboxes[domain_name])
            count += len(domain_to_mailboxes[domain_name])
    print(f"  {name}: {count} mailbox IDs")

print(f"\nTotal: {len(all_ids)} mailbox IDs to set photos on")

# Step 3: Set photos in batches of 20
batch_size = 20
success = 0
fail = 0
for i in range(0, len(all_ids), batch_size):
    batch = all_ids[i:i+batch_size]
    mailbox_data = [{"mailboxId": mid, "profilePicture": PHOTO_URL} for mid in batch]
    try:
        r = requests.put(
            f"{ZAPMAIL_API}/v2/mailboxes",
            headers=zm_headers(),
            json={"mailboxData": mailbox_data},
            timeout=60
        )
        if r.status_code == 200:
            success += len(batch)
            print(f"  Batch {i//batch_size + 1}: {len(batch)} OK")
        else:
            fail += len(batch)
            print(f"  Batch {i//batch_size + 1}: FAILED {r.status_code} {r.text[:200]}")
    except Exception as e:
        fail += len(batch)
        print(f"  Batch {i//batch_size + 1}: ERROR {e}")
    time.sleep(1)

print(f"\nDone! Success: {success}, Failed: {fail}")
