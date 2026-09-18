#!/usr/bin/env python3
"""Register every domain in a batch config in ONE Spaceship pass.

Why up front instead of per group: a fresh .info takes 15-60 min to resolve
publicly, and acq_outlook waits on that before it will create mailboxes. Buying
all groups' domains together lets them propagate in parallel, so groups 2 and 3
are already resolvable by the time their run starts — otherwise each group pays
the propagation wait serially.

    python scripts/buy_batch_domains.py --batch batches/2026-07-28-google.json
    python scripts/buy_batch_domains.py --batch ... --execute     # spends money

Then run each group with acq_outlook's --no-buy:

    python acq_outlook.py --batch <cfg> --label "Acquisition N" --execute --no-buy

Spends money in exactly one place — acq_outlook.step1_buy_domains, which prints
the cost and requires a typed YES first.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"),
            encoding="utf-8-sig")

import acq_outlook as A
from setup import log


def main():
    ap = argparse.ArgumentParser(description="Register all domains in a batch config")
    ap.add_argument("--batch", required=True, help="batch config JSON")
    ap.add_argument("--execute", action="store_true", help="actually buy (default: dry-run)")
    args = ap.parse_args()

    with open(args.batch, encoding="utf-8") as fh:
        cfg = json.load(fh)

    domains, seen = [], set()
    for b in cfg["batches"]:
        for d in b["domains"]:
            if d not in seen:
                seen.add(d)
                domains.append(d)

    # This script never calls apply_batch, so a batch that is not on the default
    # .info pricing has to push its own per-domain cost in — otherwise the money
    # gate quotes a number well under what Spaceship is about to charge.
    costs = {b["domain_cost"] for b in cfg["batches"] if b.get("domain_cost")}
    if costs:
        A.DOMAIN_COST = max(costs)

    # Same reason: without this the "spares to consider" line on an unavailable
    # domain lists the built-in Outlook M spares, which belong to a different
    # brand entirely.
    A.SPARES = [s for b in cfg["batches"] for s in b.get("spares", [])]

    log("=" * 58)
    log(f"{len(domains)} domains across {len(cfg['batches'])} groups "
        f"— ~${len(domains) * A.DOMAIN_COST} one-time")
    for b in cfg["batches"]:
        log(f"  {b['label']}: {len(b['domains'])} domains "
            f"→ {len(b['domains']) * A.ACCOUNTS_PER_DOMAIN} inboxes")
    log("=" * 58)

    bought = A.step1_buy_domains(domains, args.execute)

    if args.execute:
        log(f"registered {len(bought)}/{len(domains)}")
        missing = [d for d in domains if d not in bought]
        if missing:
            log(f"NOT registered ({len(missing)}): {', '.join(missing)} — "
                f"check the Spaceship balance, then re-run this script", "WARN")
        log("next: run each group with acq_outlook.py --no-buy once NS propagate")


if __name__ == "__main__":
    main()
