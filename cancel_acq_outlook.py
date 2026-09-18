#!/usr/bin/env python3
"""Retire the 36 Microsoft/Outlook ACQUISITION inboxes: detach from campaigns,
then schedule the Zapmail mailboxes for removal on the next billing date.

Background: the Outlook acquisition batch (12 `theheadlinetheory*.info` domains,
3 mailboxes each, personas aidan / aidanh / aidanhutch) replies at roughly a
quarter of the Google rate, and Outlook provisioning is now barred by standing
policy. This retires the batch that already exists.

    python cancel_acq_outlook.py                # DRY-RUN (default) — shows the plan
    python cancel_acq_outlook.py --execute      # detach from campaigns + schedule removal

Two things this does NOT do, deliberately:
  * it never deletes a mailbox immediately — Zapmail removes them on the domain's
    next monthly billing date, which leaves a window to rescue live threads;
  * it never touches a COMPLETED/STOPPED campaign. You cannot break a finished
    campaign and pulling a sender only muddies its report.

⚠️ THE PROVIDER HEADER IS THE WHOLE TRICK. Zapmail partitions inventory by
`x-service-provider`. `setup.zm_headers()`, `cancel_lars.zm_schedule_removal()`
and `zapmail_removals.ZH` all hardcode GOOGLE, and under GOOGLE these 12 domains
DO NOT EXIST — a scheduled-removal call built on them resolves zero domain ids
and reports success having done nothing. Everything here sends MICROSOFT.
"""
from __future__ import annotations
import argparse, os, sys, time, json, re
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
            encoding="utf-8-sig")
import requests
import setup as S
from setup import log

ZM_API = "https://api.zapmail.ai/api"
PROVIDER = "MICROSOFT"

# The acquisition batch is exactly the Outlook mailboxes on theheadlinetheory*.info.
# The other 12 Outlook mailboxes in the fleet are on landrys*.info and belong to
# Landry's Landscape — a CLIENT. They are not acquisition and must never be caught here.
ACQ_DOMAIN_RE = re.compile(r"^theheadlinetheory[a-z0-9]*\.info$", re.I)
CLIENT_OUTLOOK_RE = re.compile(r"^landrys", re.I)

REMOVE_FROM_STATES = {"ACTIVE", "PAUSED", "DRAFTED"}


def zm_headers():
    return {"Content-Type": "application/json",
            "x-auth-zapmail": (os.environ.get("ZAPMAIL_API_KEY") or "").strip(),
            "x-service-provider": PROVIDER}


def zm_list_domains_ms():
    """Every MICROSOFT domain, paginated. setup.zm_list_domains() cannot be used —
    it hardcodes the GOOGLE provider header and returns none of these."""
    out, page = [], 1
    while True:
        r = requests.get(f"{ZM_API}/v2/domains?page={page}&limit=100",
                         headers=zm_headers(), timeout=90)
        if r.status_code != 200:
            raise RuntimeError(f"Zapmail /domains page {page}: HTTP {r.status_code} {r.text[:200]}")
        data = (r.json() or {}).get("data") or {}
        out.extend(data.get("domains") or [])
        if page >= (data.get("totalPages") or 1):
            break
        page += 1
    return out


def zm_schedule_removal(domain_ids, remove=True):
    """Schedule removal of all mailboxes on these domains at the next billing date.
    PUT /v2/mailboxes/scheduled-removal {domainIds, remove}. remove=False reverts.
    There is no GET to read the state back — the 200 is the record; confirm in the
    Zapmail dashboard."""
    r = requests.put(f"{ZM_API}/v2/mailboxes/scheduled-removal", headers=zm_headers(),
                     json={"domainIds": list(domain_ids), "remove": remove}, timeout=90)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text[:300]}


SL, KEY = S.SMARTLEAD_API, S.SMARTLEAD_KEY


def gj(url, tries=6):
    for i in range(tries):
        S._sl_rest_limiter.wait()
        r = requests.get(url, timeout=60)
        if r.status_code == 200:
            try:
                return r.json()
            except Exception:
                pass
        time.sleep(4 * (i + 1))
    return None


def find_targets():
    """{smartlead_account_id: email} for the Outlook acquisition batch."""
    accts, off = [], 0
    while off < 8000:
        page = S.sl_list_accounts(offset=off, limit=100)
        if not isinstance(page, list) or not page:
            break
        accts.extend(page)
        off += 100
    out, skipped = {}, []
    for a in accts:
        if (a.get("type") or "").upper() != "OUTLOOK":
            continue
        dom = (a.get("from_email") or "").split("@")[-1]
        if ACQ_DOMAIN_RE.match(dom):
            out[a["id"]] = a["from_email"]
        else:
            skipped.append(a["from_email"])
    return out, skipped, len(accts)


def campaign_memberships(ids):
    camps = gj(f"{SL}/campaigns?api_key={KEY}") or []
    out, unreadable = {}, []
    for c in camps:
        accs = gj(f"{SL}/campaigns/{c['id']}/email-accounts?api_key={KEY}")
        if not isinstance(accs, list):
            unreadable.append((c["id"], c.get("status")))
            continue
        hits = [a["id"] for a in accs if a.get("id") in ids]
        if hits:
            out[c["id"]] = {"name": c.get("name"), "status": c.get("status"),
                            "ids": hits, "total_senders": len(accs)}
    return out, unreadable, len(camps)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="actually do it (default: dry-run)")
    ap.add_argument("--skip-campaigns", action="store_true",
                    help="only schedule the Zapmail removal; leave campaign membership alone")
    args = ap.parse_args()

    log("Finding Outlook acquisition inboxes...")
    targets, other_outlook, n_accts = find_targets()
    if not targets:
        log("No Outlook acquisition inboxes found — nothing to do.", "WARN")
        return
    target_emails = {e.lower() for e in targets.values()}
    log(f"  {len(targets)} targets out of {n_accts} accounts")
    for e in sorted(other_outlook):
        if not CLIENT_OUTLOOK_RE.match(e.split('@')[-1]):
            log(f"  UNEXPECTED non-acquisition Outlook inbox (left alone): {e}", "WARN")

    log("Reading live campaign memberships...")
    mem, unreadable, n_camps = campaign_memberships(set(targets))
    remove_targets = {c: m for c, m in mem.items() if m["status"] in REMOVE_FROM_STATES}
    history = {c: m for c, m in mem.items() if m["status"] not in REMOVE_FROM_STATES}

    log(f"Mapping Zapmail domains ({PROVIDER})...")
    doms = zm_list_domains_ms()
    by_name = {(d.get("domain") or d.get("name") or "").lower(): d for d in doms}
    want_domains = sorted({e.split("@")[-1].lower() for e in target_emails})
    safe, unsafe, missing = [], [], []
    for dn in want_domains:
        d = by_name.get(dn)
        if not d:
            missing.append(dn)
            continue
        # Purity check: only schedule a domain where EVERY live mailbox is a target,
        # so a shared domain can never take an unrelated mailbox down with it.
        users = [f"{(m.get('username') or '')}@{dn}".lower() for m in (d.get("mailboxes") or [])]
        if users and all(u in target_emails for u in users):
            safe.append((dn, d.get("id"), len(users)))
        else:
            unsafe.append((dn, [u for u in users if u not in target_emails]))

    covered = sum(n for _, _, n in safe)
    print("\n" + "=" * 68)
    print("  CANCEL OUTLOOK ACQUISITION BATCH — PLAN")
    print("=" * 68)
    print(f"  Inboxes targeted:        {len(targets)} across {len(want_domains)} domains")
    print(f"  Other Outlook inboxes:   {len(other_outlook)} LEFT ALONE (Landry's Landscape — a client)")
    print(f"  Campaigns scanned:       {n_camps}"
          + (f"  ({len(unreadable)} unreadable)" if unreadable else ""))
    print(f"  Detach from:             {len(remove_targets)} campaign(s) [ACTIVE/PAUSED/DRAFTED]")
    for cid, m in sorted(remove_targets.items(), key=lambda kv: -len(kv[1]["ids"])):
        rem = m["total_senders"] - len(m["ids"])
        print(f"      {cid} [{m['status']:<8}] pulling {len(m['ids']):>2} of {m['total_senders']:>3}"
              f" senders, {rem:>3} remain  {m['name'][:44]}")
    print(f"  Left as history:         {len(history)} completed/stopped campaign(s)")
    print(f"  Zapmail domains to schedule: {len(safe)}  covering {covered}/{len(targets)} mailboxes")
    for dn, did, n in safe:
        print(f"      {dn:<34} {n} mailbox(es)")
    for dn, strangers in unsafe:
        print(f"      SKIP {dn} — carries non-target mailboxes: {strangers}")
    for dn in missing:
        print(f"      SKIP {dn} — not found under {PROVIDER}")
    print(f"  Method:                  scheduled removal on next billing date (never instant)")
    print(f"  Saving:                  ~${len(targets) * 3}/mo once Zapmail optimises the slots")
    print(f"  MODE: {'EXECUTE' if args.execute else 'DRY-RUN — nothing changed'}")
    print("=" * 68 + "\n")

    if unreadable:
        log(f"{len(unreadable)} campaign(s) could not be read — membership may be "
            f"incomplete: {unreadable[:5]}", "WARN")
    if not args.execute:
        log("DRY-RUN — re-run with --execute to detach + schedule removal.")
        return
    if not safe:
        log("No domain passed the purity check — refusing to schedule anything.", "ERROR")
        return

    if not args.skip_campaigns:
        log("STEP 1: detaching from live campaigns")
        for cid, m in remove_targets.items():
            S._sl_rest_limiter.wait()
            r = requests.delete(f"{SL}/campaigns/{cid}/email-accounts?api_key={KEY}",
                                json={"email_account_ids": m["ids"]}, timeout=60)
            ok = r.status_code == 200
            log(f"  {cid} {m['name'][:40]}: pulled {len(m['ids'])} — "
                f"{'ok' if ok else r.text[:120]}", "INFO" if ok else "WARN")
    else:
        log("STEP 1 skipped (--skip-campaigns)")

    log(f"STEP 2: scheduling Zapmail removal on {len(safe)} domains ({PROVIDER})")
    sc, res = zm_schedule_removal([did for _, did, _ in safe], remove=True)
    ok = sc == 200
    log(f"  HTTP {sc}: {res.get('message') or res}", "INFO" if ok else "ERROR")

    if ok:
        try:
            import zapmail_removals as zr
            reg = zr.register_domains([dn for dn, _, _ in safe], source="cancel_acq_outlook")
            log(f"STEP 3: registered {reg.get('added', 0)} mailboxes with the removal watcher")
        except Exception as e:
            log(f"STEP 3: could not register with the watcher: {e}", "WARN")
        log("=" * 68)
        log(f"DONE — {len(targets)} Outlook acquisition inboxes detached from "
            f"{len(remove_targets)} campaign(s) and scheduled for removal on the next "
            f"billing date. No read-back API: confirm in the Zapmail dashboard.")
        log("The removal watcher now scans GOOGLE + MICROSOFT, so it will see these "
            "disappear and post the 'tell Zapmail to optimise billing' alert.")
    else:
        log(f"STEP 2 FAILED (HTTP {sc}) — mailboxes NOT scheduled. Campaign detachment "
            f"in step 1 already happened.", "ERROR")


if __name__ == "__main__":
    main()
