#!/usr/bin/env python3
"""Parameterized buy -> create -> warm pipeline (reserve replenishment).

Reuses the tested provisioning steps in create_generic_groups.py, but driven by
a target inbox count instead of the hardcoded O/P/Q/R batch:

    python provision.py --inboxes 52 --label X            # DRY-RUN (default)
    python provision.py --inboxes 52 --label X --execute   # actually provision

DRY-RUN lists the domains it would use and the cost; --execute runs the full
pipeline: connect domains -> buy Zapmail mailbox slots ($3 ea) -> create 3
mailboxes/domain -> profile photos -> export to SmartLead -> tag Generic <label>
+ today -> enable the 14-day warmup.

This is long-running (DNS/export/warmup waits, ~15-20 min), so it runs here as a
script/worker — it cannot run inside a Vercel serverless request (60s limit).
The dashboard's Reserve-replenishment planner tells you how many inboxes to pass.
"""

import argparse
import math
import sys
from datetime import datetime

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

import create_generic_groups as cgg
from setup import log

MAILBOX_COST = 3   # $/mailbox (Zapmail Pro)


def main():
    ap = argparse.ArgumentParser(description="Buy+create+warm N reserve inboxes")
    ap.add_argument("--inboxes", type=int, required=True, help="how many inboxes to provision")
    ap.add_argument("--label", default="X", help="generic group letter (must be unused, e.g. S)")
    ap.add_argument("--execute", action="store_true", help="actually provision (default is dry-run)")
    args = ap.parse_args()

    per_domain = cgg.ACCOUNTS_PER_DOMAIN
    domains_needed = math.ceil(args.inboxes / per_domain)

    log("=" * 60)
    log(f"PROVISION {args.inboxes} inboxes -> Generic {args.label}  ({domains_needed} domains x {per_domain})")
    log("=" * 60)

    ready, fresh = cgg.get_available_domains()
    pool = [{"domain": d["domain"], "zapmail_id": d.get("zapmail_id"), "source": "ready_pool"} for d in ready] + fresh
    chosen = pool[:domains_needed]

    log(f"Available domains: {len(pool)}  |  using: {len(chosen)}")
    for d in chosen:
        log(f"  {d['domain']} ({d['source']})")
    short = domains_needed - len(chosen)
    if short > 0:
        log(f"SHORT {short} domains — buy more (Spaceship) before provisioning this many", "WARN")

    est_inboxes = len(chosen) * per_domain
    log(f"Estimated: {est_inboxes} mailboxes  ~=  ${est_inboxes * MAILBOX_COST}/mo (Zapmail slots)")

    if not args.execute:
        log("")
        log("DRY-RUN — nothing bought. Re-run with --execute to provision.")
        return

    # --- execute: reuse the tested pipeline with a dynamic single group ---
    cgg.GROUPS = {args.label: chosen}
    cgg.TODAY_TAG = datetime.now().strftime("%-m/%-d/%y") if sys.platform != "win32" \
        else datetime.now().strftime("%#m/%#d/%y")

    cgg.step1_connect_to_zapmail()
    mb_ids = cgg.step2_create_mailboxes()
    cgg.step3_profile_photos(mb_ids)
    cgg.step4_export_to_smartlead(mb_ids)
    cgg.step5_tag_in_smartlead()
    cgg.step6_enable_warmup()

    log("=" * 60)
    log(f"DONE: provisioned ~{est_inboxes} inboxes in Generic {args.label}. "
        f"Warming 14 days — they become ready reserve after that.")
    log("=" * 60)


if __name__ == "__main__":
    main()
