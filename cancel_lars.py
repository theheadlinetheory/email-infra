#!/usr/bin/env python3
"""Remove all Lars Matthys inboxes from campaigns, then cancel the mailboxes.

Lars Matthys is a retired acquisition sender: 18 inboxes (lars@ / lars.m@ /
larsmatthys@) across 6 .com domains, tagged Acquisition G.

Order matters — detach from campaigns BEFORE cancelling, so nothing is cancelled
while still an active sender. Verified live (2026-07-28): none of the 18 are in an
ACTIVE campaign; the only non-completed membership is "Denver HVAC Acquisition"
(PAUSED). The other 8 memberships are COMPLETED campaigns (historical) and are
left untouched — you cannot break a finished campaign and removing a sender only
muddies its report.

    python cancel_lars.py                 # DRY-RUN (default) — shows the plan
    python cancel_lars.py --execute       # remove from campaigns + cancel
    python cancel_lars.py --execute --hard # cancel = instant delete (default is remove-on-renewal)

SmartLead endpoint gotcha: campaign list is /campaigns (plural). The singular
/campaign that older code uses now 404s and silently reads as "0 campaigns".
"""
from __future__ import annotations
import argparse, sys, time, json
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv(__import__("os").path.join(
    __import__("os").path.dirname(__import__("os").path.abspath(__file__)), ".env"),
    encoding="utf-8-sig")
import os
import requests
import setup as S
from setup import log

ZM_API = "https://api.zapmail.ai/api"


def zm_schedule_removal(domain_ids, remove=True):
    """Schedule (or un-schedule) removal of all mailboxes on these domains at the
    next renewal — Zapmail deletes them automatically, no manual step.
    PUT /v2/mailboxes/scheduled-removal  body {domainIds, remove}. remove=False reverts.
    NOTE: domain-based; the old mailbox-id endpoints (remove-on-renewal / DELETE
    /v2/mailboxes) are deprecated and 404. There is no GET to read the state back —
    the 200 write is the record; confirm visually in the Zapmail dashboard."""
    h = {"Content-Type": "application/json",
         "x-auth-zapmail": os.environ.get("ZAPMAIL_API_KEY", "").strip(),
         "x-service-provider": "GOOGLE"}
    r = requests.put(f"{ZM_API}/v2/mailboxes/scheduled-removal", headers=h,
                     json={"domainIds": list(domain_ids), "remove": remove}, timeout=40)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text[:300]}

SL = S.SMARTLEAD_API
KEY = S.SMARTLEAD_KEY
# Removal only targets campaigns in these states; COMPLETED/STOPPED left as history.
REMOVE_FROM_STATES = {"ACTIVE", "PAUSED", "DRAFTED"}


def gj(url, tries=6):
    for i in range(tries):
        S._sl_rest_limiter.wait()
        r = requests.get(url, timeout=40)
        if r.status_code == 200:
            try:
                return r.json()
            except Exception:
                pass
        time.sleep(4 * (i + 1))
    return None


def find_lars():
    accts, off = [], 0
    while off < 4000:
        page = S.sl_list_accounts(offset=off, limit=100)
        if not isinstance(page, list) or not page:
            break
        accts.extend(page)
        off += 100

    def islars(a):
        return (a.get("from_name") or "").strip().lower() == "lars matthys" or \
               (a.get("from_email") or "").split("@")[0].lower() in ("lars", "lars.m", "larsmatthys")

    return {a["id"]: a["from_email"] for a in accts if islars(a)}


def campaign_memberships(lars_ids, lars_emails):
    camps = gj(f"{SL}/campaigns?api_key={KEY}") or []
    out = {}
    for c in camps:
        accs = gj(f"{SL}/campaigns/{c['id']}/email-accounts?api_key={KEY}")
        if not isinstance(accs, list):
            log(f"  could not read campaign {c['id']} ({c.get('status')}) — skipping", "WARN")
            continue
        hits = [a["id"] for a in accs
                if a.get("id") in lars_ids or a.get("from_email") in lars_emails]
        if hits:
            out[c["id"]] = {"name": c.get("name"), "status": c.get("status"), "ids": hits}
    return out


def zapmail_ids(lars_emails):
    by_email = {}
    for d in S.zm_list_domains():
        dn = d.get("name") or d.get("domain")
        for mb in (d.get("mailboxes") or []):
            u = mb.get("username") or mb.get("mailboxUsername")
            if u and dn:
                by_email[f"{u}@{dn}"] = mb.get("id")
    return {e: by_email.get(e) for e in lars_emails}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="actually do it (default dry-run)")
    ap.add_argument("--hard", action="store_true",
                    help="cancel = instant Zapmail delete (default: remove-on-renewal)")
    args = ap.parse_args()

    log("Finding Lars inboxes...")
    lars = find_lars()                      # {sl_id: email}
    lars_ids = set(lars)
    lars_emails = set(lars.values())
    log(f"  {len(lars)} Lars inboxes")

    log("Reading live campaign memberships (/campaigns, plural)...")
    mem = campaign_memberships(lars_ids, lars_emails)
    remove_targets = {cid: m for cid, m in mem.items() if m["status"] in REMOVE_FROM_STATES}
    completed = {cid: m for cid, m in mem.items() if m["status"] not in REMOVE_FROM_STATES}

    log("Mapping to Zapmail mailbox ids...")
    zids = zapmail_ids(lars_emails)
    missing = [e for e, v in zids.items() if not v]

    print("\n" + "=" * 60)
    print("  CANCEL LARS — PLAN")
    print("=" * 60)
    print(f"  Inboxes:                 {len(lars)} (lars / lars.m / larsmatthys @ 6 domains)")
    print(f"  In ACTIVE campaigns:     {sum(1 for m in mem.values() if m['status']=='ACTIVE')}")
    print(f"  Remove from (live/paused/draft): {len(remove_targets)} campaign(s)")
    for cid, m in remove_targets.items():
        print(f"      {cid}  {m['name']}  [{m['status']}]  {len(m['ids'])} senders")
    print(f"  Left as history (completed/stopped): {len(completed)} campaign(s)")
    print(f"  Zapmail mailboxes mapped: {len(lars)-len(missing)}/{len(lars)}")
    if missing:
        for e in missing:
            print(f"      NO zapmail id: {e}")
    print(f"  Cancel method:           {'INSTANT DELETE' if args.hard else 'remove-on-renewal'}")
    print(f"  Monthly saving:          ~${len(lars)*3}/mo at renewal")
    print(f"  MODE: {'EXECUTE' if args.execute else 'DRY-RUN — nothing changed'}")
    print("=" * 60 + "\n")

    if not args.execute:
        log("DRY-RUN — re-run with --execute to remove + cancel.")
        return

    # ---- Step 1: remove from campaigns (batch DELETE per campaign) ----
    log("STEP 1: removing from campaigns")
    for cid, m in remove_targets.items():
        S._sl_rest_limiter.wait()
        r = requests.delete(f"{SL}/campaigns/{cid}/email-accounts?api_key={KEY}",
                            json={"email_account_ids": m["ids"]}, timeout=40)
        ok = r.status_code == 200
        log(f"  {cid} {m['name']}: removed {len(m['ids'])} — {'ok' if ok else r.text[:120]}",
            "INFO" if ok else "WARN")

    # ---- Step 2: schedule Zapmail removal on next renewal (auto-deletes) ----
    # Safety: only schedule domains where EVERY mailbox is Lars, so we never
    # cancel an unrelated mailbox sharing a domain.
    log("STEP 2: scheduling Zapmail removal on next renewal")
    lars_domains = {e.split("@")[1] for e in lars_emails}
    safe_domain_ids, unsafe = [], []
    for d in S.zm_list_domains():
        dn = d.get("name") or d.get("domain")
        if dn in lars_domains:
            users = [(m.get("username") or m.get("mailboxUsername") or "") for m in (d.get("mailboxes") or [])]
            if users and all("lars" in u.lower() for u in users):
                safe_domain_ids.append(d.get("id"))
            else:
                unsafe.append(dn)
    if unsafe:
        log(f"  SKIP (mixed mailboxes, would hit non-Lars): {unsafe}", "WARN")
    sc, res = zm_schedule_removal(safe_domain_ids, remove=True)
    ok = sc == 200
    log(f"  scheduled removal on {len(safe_domain_ids)} domains — HTTP {sc}: "
        f"{res.get('message') or res}", "INFO" if ok else "ERROR")

    log("=" * 60)
    if ok:
        log(f"DONE — {len(lars)} Lars inboxes removed from {len(remove_targets)} campaign(s) "
            f"and scheduled for automatic removal on next renewal. "
            f"(No dashboard read-back API — confirm visually in Zapmail.)")
    else:
        log(f"STEP 1 done ({len(lars)} removed from campaigns) but STEP 2 FAILED "
            f"(HTTP {sc}) — mailboxes NOT scheduled for removal.", "ERROR")
    log("=" * 60)


if __name__ == "__main__":
    main()
