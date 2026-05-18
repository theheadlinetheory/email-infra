"""Re-tag 14 domains in Zapmail from Kay's Landscaping to Kay's Landscaping 2."""

import sys, os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from setup import zm_list_domains, zm_list_domain_tags, zm_create_domain_tag, zm_assign_domain_tag

WORKSPACE_KEY = "06d331bc-9610-40d0-b881-ef5e9883ec70"
TARGET_TAG = "Kay's Landscaping 2"

TARGET_DOMAINS = [
    "outdoormaintenancehub.co",
    "outdoormaintenancezone.co",
    "outdoorserviceconnect.co",
    "outdoorservicedirect.co",
    "outdoorservicefocus.co",
    "outdoorservicehelp.co",
    "outdoorservicehq.co",
    "outdoorserviceprime.co",
    "outdoorserviceselect.co",
    "outdoorserviceteam.co",
    "outdoorservicezone.co",
    "outdooryardcare.co",
    "propertycarebase.co",
    "propertycareelite.co",
]

# 1. List all domains and find IDs for our targets
print("Fetching all Zapmail domains...")
all_domains = zm_list_domains(WORKSPACE_KEY)
print(f"  Found {len(all_domains)} total domains")

domain_map = {}
for d in all_domains:
    name = d.get("domain", d.get("domainName", d.get("name", "")))
    if name in TARGET_DOMAINS:
        domain_map[name] = d["id"]

print(f"  Matched {len(domain_map)}/{len(TARGET_DOMAINS)} target domains")
missing = set(TARGET_DOMAINS) - set(domain_map.keys())
if missing:
    print(f"  WARNING - not found: {missing}")

if not domain_map:
    print("No domains found. Exiting.")
    sys.exit(1)

# 2. Find or create tag
print(f"\nLooking for existing '{TARGET_TAG}' tag...")
tags_resp = zm_list_domain_tags(WORKSPACE_KEY)
tag_list = tags_resp.get("data", []) if isinstance(tags_resp, dict) else []

tag_id = None
for t in tag_list:
    if t.get("name") == TARGET_TAG:
        tag_id = t["id"]
        print(f"  Found existing tag: id={tag_id}")
        break

if tag_id is None:
    print(f"  Tag not found. Creating '{TARGET_TAG}'...")
    create_resp = zm_create_domain_tag(TARGET_TAG, workspace_key=WORKSPACE_KEY)
    print(f"  Create response: {create_resp}")
    # Extract tag ID from response — format: {data: {tagIds: [...]}}
    if isinstance(create_resp, dict) and "data" in create_resp:
        data = create_resp["data"]
        if isinstance(data, dict) and "tagIds" in data and data["tagIds"]:
            tag_id = data["tagIds"][0]
        elif isinstance(data, list) and len(data) > 0:
            tag_id = data[0].get("id")
        elif isinstance(data, dict):
            tag_id = data.get("id")
    if tag_id is None:
        print("  ERROR: Could not extract tag ID from create response. Exiting.")
        sys.exit(1)
    print(f"  Created tag: id={tag_id}")

# 3. Assign tag to domains
domain_ids = list(domain_map.values())
print(f"\nAssigning tag '{TARGET_TAG}' (id={tag_id}) to {len(domain_ids)} domains...")
assign_resp = zm_assign_domain_tag(domain_ids, [tag_id], WORKSPACE_KEY)
print(f"  Response: {assign_resp}")

print("\nDone. Tagged domains:")
for name, did in sorted(domain_map.items()):
    print(f"  {name} (id={did})")
