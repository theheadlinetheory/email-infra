#!/usr/bin/env python3
"""Switch warm-up on for every inbox that is neither sending nor warming.

Why this is a script and not part of the nightly cron:

A blanket sweep is how Landy Rose Media got warmed when it should not have
been. Tim, 2026-10-15: "don't start warm-up for Landy Rose, please do not."
Auto-enabling warm-up fleet-wide, unattended, every night, would repeat that
mistake daily and silently. So the DAILY job only alerts (rule 12 /
`check_invariants.warmup_alert`); turning warm-up on is a deliberate act, run
from here, with the plan printed before anything is written.

It refuses to touch:
  - retirement buckets (cleanup / retired / burnt) — on their way out
  - anything `check_invariants.is_warmup_exempt` names, with its reason
    (Landy Rose by tag; every contact@ cold-calling inbox by mailbox name)

Usage:
    python scripts/warmup_sweep.py              # plan only, writes nothing
    python scripts/warmup_sweep.py --apply      # enable, then verify
    python scripts/warmup_sweep.py --apply --group "Acquisition P"
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

import check_invariants as civ
import setup

V1 = "https://server.smartlead.ai/api/v1"
WARMUP_BODY = getattr(setup, "GOOGLE_WARMUP",
                      {"warmup_enabled": True, "total_warmup_per_day": 15,
                       "daily_rampup": 5, "reply_rate_percentage": 40})


def fetch_accounts(key):
    """Every account, or an exception. Never a short page read as the fleet
    (rule 11) — a partial read here means we 'find' no cold inboxes."""
    out, off = [], 0
    while True:
        r = requests.get(f"{V1}/email-accounts/",
                         params={"api_key": key, "offset": off, "limit": 100},
                         timeout=60)
        r.raise_for_status()
        page = r.json()
        if not isinstance(page, list):
            raise RuntimeError(f"unexpected page shape at offset {off}: {page!r:.120}")
        out.extend(page)
        off += 100
        if len(page) < 100:
            return out


def classify(accounts, only_group=None):
    cold, skipped = [], {"retired": 0, "exempt": 0, "sending": 0, "warming": 0}
    for a in accounts:
        tag = (civ.client_tag_of(a) or "").strip() or "untagged"
        low = tag.lower()
        if any(k in low for k in civ.RETIREMENT_TAG_HINTS):
            skipped["retired"] += 1
            continue
        if civ.is_warmup_exempt(tag, a.get("from_email")):
            skipped["exempt"] += 1
            continue
        if ((a.get("warmup_details") or {}).get("status") or "").upper() == "ACTIVE":
            skipped["warming"] += 1
            continue
        if (a.get("daily_sent_count") or 0) > 0:
            skipped["sending"] += 1
            continue
        if only_group and only_group.lower() not in low:
            continue
        cold.append((a.get("id"), a.get("from_email") or "?", tag))
    return cold, skipped


def enable(key, account_id):
    """POST warm-up, retrying only a 429. Returns the final status code."""
    for attempt in range(3):
        r = requests.post(f"{V1}/email-accounts/{account_id}/warmup",
                          params={"api_key": key}, json=WARMUP_BODY, timeout=40)
        if r.status_code != 429:
            return r.status_code
        time.sleep(5 * (attempt + 1))
    return r.status_code


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually enable warm-up (default: plan only)")
    ap.add_argument("--group", default=None,
                    help="restrict to inbox groups whose tag contains this")
    args = ap.parse_args(argv)

    key = (os.environ.get("SMARTLEAD_API_KEY") or "").strip()
    if not key:
        print("SMARTLEAD_API_KEY is not set", file=sys.stderr)
        return 2

    accounts = fetch_accounts(key)
    cold, skipped = classify(accounts, args.group)
    print(f"{len(accounts)} accounts read")
    print(f"  already warming : {skipped['warming']}")
    print(f"  sending today   : {skipped['sending']}")
    print(f"  retiring        : {skipped['retired']}  (exempt)")
    print(f"  deliberately exempt: {skipped['exempt']}  "
          f"({', '.join(civ.WARMUP_EXEMPT_TAG_HINTS)})")
    print(f"  COLD            : {len(cold)}")

    by_group = {}
    for _, _, tag in cold:
        by_group[tag] = by_group.get(tag, 0) + 1
    for tag, n in sorted(by_group.items(), key=lambda kv: -kv[1]):
        print(f"      {tag}: {n}")

    if not cold:
        print("\nnothing to do")
        return 0
    if not args.apply:
        print("\nplan only — nothing was written. Re-run with --apply.")
        return 0

    ok = fail = 0
    for aid, email, tag in cold:
        code = enable(key, aid)
        if code == 200:
            ok += 1
        else:
            fail += 1
            print(f"  FAILED {email} ({tag}): HTTP {code}")
        time.sleep(0.15)
    print(f"\nenabled {ok}, failed {fail}")

    # Verify against a fresh read. "The write returned 200" is not the same
    # claim as "warm-up is on" — save-management-details returns 200 and does
    # nothing at all when its GraphQL layer is down.
    again, _ = classify(fetch_accounts(key), args.group)
    print(f"VERIFIED — still cold after the sweep: {len(again)}")
    for _, email, tag in again[:10]:
        print(f"   {email} ({tag})")
    return 0 if not again else 1


if __name__ == "__main__":
    raise SystemExit(main())
