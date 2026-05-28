"""Quick data audit — dumps group sizes, identifies oversized groups, checks Rock Pave."""

import os
import sys
from pathlib import Path
from collections import defaultdict

env_path = Path(__file__).parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from setup import sl_gql
from sync import fetch_all_accounts, fetch_tag_mappings, fetch_health_metrics
from tag_utils import parse_group_tag, get_group_tag_from_account

print("=== THT Infrastructure Data Audit ===\n")

print("Fetching accounts...")
accounts = fetch_all_accounts()
print(f"  {len(accounts)} accounts\n")

print("Fetching tags...")
tag_map = fetch_tag_mappings()
print(f"  Tags for {len(tag_map)} accounts\n")

for a in accounts:
    a["tags"] = tag_map.get(a["id"], [])

print("Fetching health metrics...")
health = fetch_health_metrics()
print(f"  {len(health)} health records\n")

# Group accounts by tag
client_groups = defaultdict(list)
acq_groups = defaultdict(list)
generic_groups = defaultdict(list)
untagged = []

for a in accounts:
    tag = get_group_tag_from_account(a)
    if not tag:
        untagged.append(a)
        continue
    parsed = parse_group_tag(tag)
    if parsed["role"] == "acquisition":
        acq_groups[f"Acquisition {parsed['group_letter']}"].append(a)
    elif parsed["role"] == "generic":
        generic_groups[f"Generic {parsed['group_letter']}"].append(a)
    else:
        key = f"{parsed['client_name']} {parsed['group_letter']}"
        client_groups[key].append(a)

def domain_count(accts):
    return len(set(a.get("from_email", "").split("@")[-1] for a in accts if a.get("from_email")))

def smtp_fails(accts):
    return sum(1 for a in accts if not a.get("is_smtp_success"))

print("=" * 70)
print("CLIENT GROUPS")
print("=" * 70)
for name in sorted(client_groups.keys()):
    accts = client_groups[name]
    domains = domain_count(accts)
    fails = smtp_fails(accts)
    cap = len(accts) * 15
    flag = " ⚠️ OVERSIZED" if len(accts) > 45 else ""
    flag += " ⚠️ UNDERSIZED" if len(accts) < 35 and "B" not in name else ""
    print(f"  {name:<40} {len(accts):>4} accounts  {domains:>3} domains  {cap:>5}/day  SMTP-fail:{fails}{flag}")

print(f"\n  Total client groups: {len(client_groups)}")
print(f"  Total client accounts: {sum(len(v) for v in client_groups.values())}")

# Check Rock Pave specifically
print("\n" + "=" * 70)
print("ROCK PAVE DETAIL")
print("=" * 70)
rockpave_groups = {k: v for k, v in client_groups.items() if "rock" in k.lower() or "pave" in k.lower() or "jim" in k.lower()}
if rockpave_groups:
    for name, accts in sorted(rockpave_groups.items()):
        print(f"  {name}: {len(accts)} accounts, {domain_count(accts)} domains")
        for a in sorted(accts, key=lambda x: x.get("from_email", "")):
            email = a.get("from_email", "?")
            h = health.get(email, {})
            br = h.get("bounce_rate", "?")
            rr = h.get("reply_rate", "?")
            smtp = "OK" if a.get("is_smtp_success") else "FAIL"
            print(f"    {email:<45} bounce:{br}  reply:{rr}  smtp:{smtp}")
else:
    print("  No Rock Pave groups found! Checking all tags for 'rock' or 'jim'...")
    for a in accounts:
        tags = [t.get("name", "") for t in a.get("tags", [])]
        email = a.get("from_email", "")
        if any("rock" in t.lower() or "jim" in t.lower() for t in tags):
            print(f"  {email} -> tags: {tags}")

# Check Tropical specifically
print("\n" + "=" * 70)
print("TROPICAL DETAIL")
print("=" * 70)
tropical_groups = {k: v for k, v in client_groups.items() if "tropical" in k.lower()}
for name, accts in sorted(tropical_groups.items()):
    print(f"  {name}: {len(accts)} accounts, {domain_count(accts)} domains, cap: {len(accts)*15}/day")

print("\n" + "=" * 70)
print("GENERIC GROUPS")
print("=" * 70)
for name in sorted(generic_groups.keys()):
    accts = generic_groups[name]
    domains = domain_count(accts)
    cap = len(accts) * 15
    flag = ""
    if len(accts) > 45:
        flag = f" ⚠️ OVERSIZED (should be ~42, has {len(accts)})"
    elif len(accts) < 35:
        flag = f" ⚠️ UNDERSIZED ({len(accts)})"
    print(f"  {name:<25} {len(accts):>4} accounts  {domains:>3} domains  {cap:>5}/day{flag}")

print(f"\n  Total generic groups: {len(generic_groups)}")
print(f"  Total generic accounts: {sum(len(v) for v in generic_groups.values())}")

print("\n" + "=" * 70)
print("ACQUISITION GROUPS")
print("=" * 70)
for name in sorted(acq_groups.keys()):
    accts = acq_groups[name]
    domains = domain_count(accts)
    cap = len(accts) * 15
    print(f"  {name:<25} {len(accts):>4} accounts  {domains:>3} domains  {cap:>5}/day")

print(f"\n  Total acquisition groups: {len(acq_groups)}")
print(f"  Total acquisition accounts: {sum(len(v) for v in acq_groups.values())}")

print(f"\n  Untagged: {len(untagged)}")
if untagged:
    for a in untagged[:10]:
        print(f"    {a.get('from_email', '?')} (tags: {[t.get('name','') for t in a.get('tags', [])]})")
    if len(untagged) > 10:
        print(f"    ... and {len(untagged) - 10} more")

print("\n" + "=" * 70)
print("SUMMARY")
print("=" * 70)
print(f"  Total accounts:     {len(accounts)}")
print(f"  Client accounts:    {sum(len(v) for v in client_groups.values())}")
print(f"  Generic accounts:   {sum(len(v) for v in generic_groups.values())}")
print(f"  Acquisition:        {sum(len(v) for v in acq_groups.values())}")
print(f"  Untagged:           {len(untagged)}")

oversized = [(k, len(v)) for k, v in {**generic_groups, **client_groups}.items() if len(v) > 45]
if oversized:
    print(f"\n  ⚠️ OVERSIZED GROUPS (>45 accounts):")
    for name, count in sorted(oversized, key=lambda x: -x[1]):
        print(f"    {name}: {count} accounts")
