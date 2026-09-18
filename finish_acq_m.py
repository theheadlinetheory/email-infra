#!/usr/bin/env python3
"""Autonomous finisher for Acquisition M.

Polls Zapmail until all 36 mailboxes are ACTIVE (tolerating transient network
errors), then runs the finishing pass — export -> tag -> warmup — and verifies.
Buys nothing: step 3's resume-safety reuses the already-created mailboxes.
"""
import collections
import time

import acq_outlook as A
from setup import log

WANT = set(A.DOMAINS)
EXPECT = len(A.DOMAINS) * A.ACCOUNTS_PER_DOMAIN  # 36


def snapshot():
    c = collections.Counter()
    for d in A.zm_list_domains():
        if A._dname(d) in WANT:
            for m in (d.get("mailboxes") or []):
                c[m.get("status")] += 1
    return c


def main():
    log(f"FINISHER: waiting for {EXPECT} mailboxes to go ACTIVE")
    for i in range(300):                     # up to ~10h at 120s
        try:
            c = snapshot()
        except Exception as e:               # network blip — keep going
            log(f"  poll {i+1}: transient error, retrying ({str(e)[:60]})", "WARN")
            time.sleep(120)
            continue
        log(f"  poll {i+1}: {dict(c)}")
        if c.get("ACTIVE", 0) >= EXPECT:
            log("  all ACTIVE — running finishing pass")
            break
        time.sleep(120)
    else:
        log("timed out waiting for ACTIVE — nothing bought; re-run later", "ERROR")
        return

    # Finishing pass (idempotent, buys nothing).
    domain_ids = {A._dname(d): d.get("id")
                  for d in A.zm_list_domains() if A._dname(d) in WANT}
    mailbox_ids = {n: [m["id"] for m in (d.get("mailboxes") or []) if m.get("id")]
                   for d in A.zm_list_domains()
                   if (n := A._dname(d)) in WANT}

    A.step4_photos_and_forwarding(mailbox_ids, domain_ids)
    if not A.step5_export_to_smartlead(mailbox_ids, set(domain_ids)):
        log("export failed — resume with --no-buy later", "ERROR")
        return

    accounts = A._accounts_for(set(domain_ids))
    log(f"matched {len(accounts)}/{EXPECT} SmartLead accounts")
    A.step6_tag(accounts)
    A.step8_warmup(accounts)

    ok = A.verify(list(WANT))
    log("=" * 58)
    log("ACQUISITION M COMPLETE — all verified" if ok
        else "FINISHED WITH MISMATCHES — see FAILs above", "INFO" if ok else "ERROR")
    log("=" * 58)


if __name__ == "__main__":
    main()
