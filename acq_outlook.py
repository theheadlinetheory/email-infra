#!/usr/bin/env python3
"""Outlook acquisition pipeline — buy .info domains, create Microsoft mailboxes, warm them.

Why this is a separate module and not a flag on create_generic_groups.py:
setup.py's Zapmail helpers hardcode `x-service-provider: GOOGLE` and read the
`*Google` workspace counters. That path has provisioned 1,545 live mailboxes, so
it is not worth destabilising. This module reuses everything provider-neutral
from setup.py (Spaceship, SmartLead tags, photo URL, sender specs) and carries
its own MICROSOFT-flavoured Zapmail wrappers.

Provider string confirmed empirically: GET /v2/domains returns 698 domains under
GOOGLE, 64 under MICROSOFT, and HTTP 500 for OUTLOOK/MS/garbage.

Money is spent in exactly two steps — step 1 (Spaceship registrations) and
step 3 (Zapmail mailbox slots). Both refuse to run without --execute, and both
print a cost line and require a typed confirmation first.

    python acq_outlook.py --plan                  # show the plan, touch nothing
    python acq_outlook.py --limit 1 --execute     # pilot: 1 domain, 3 inboxes
    python acq_outlook.py --execute               # the remaining 11 domains
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Windows consoles default to cp1252 and mangle the em-dashes/arrows below.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
            encoding="utf-8-sig")

import requests

import db
import setup as S
from setup import log
from tag_utils import ZAPMAIL_TAG_ID

PROVIDER = "MICROSOFT"
GROUP_LETTER = "M"                      # A-L are taken by existing acquisition groups
GROUP_TAG = f"Acquisition {GROUP_LETTER}"
SENDER = "aidan_hutchinson"
FORWARD_TO = "https://theheadlinetheory.com/"
ACCOUNTS_PER_DOMAIN = 3

# Who the inboxes belong to. Defaults to the acquisition sender above; --batch
# overrides both for Sean Reynolds (generic) batches. Everything downstream
# reads these rather than looking the sender up again, so one config drives
# both the mailbox usernames and the profile photo.
SPECS = S.ACQUISITION_SENDERS[SENDER]["specs"]
PHOTO_URL = S.ACQUISITION_PHOTO_URLS[SENDER]

# Set by --allow-deficit. Off means the run aborts rather than buying slots
# that Zapmail would apply to a lapsed subscription instead of to our inboxes.
ALLOW_DEFICIT = False

# Fresh .info registrations can take up to an hour to propagate — 60 x 60s.
POLL_ATTEMPTS = 60
POLL_SECONDS = 60

MAILBOX_COST = 3        # $/mailbox/mo, Zapmail Pro
DOMAIN_COST = 3         # ~$ one-time .info registration; --batch can override
                        # it per batch (a .co is ~4x a .info, and this number is
                        # what the typed-YES money gate shows before charging)

# No SmartLead signature by design (2026-07-22): the sign-off lives in the
# campaign copy instead, so setting one here would duplicate it in every send.

# Warmup settings per the 2026-07-21 spec screenshot.
WARMUP = {
    "reply_rate": 35,
    "max_emails_per_day": 20,
    "warmup_max_count": 20,
    "warmup_min_count": 3,
    "enable_rampup": True,
    "weekdays_only": True,
    # Spec screenshot said 10, but SmartLead's UI slider for "Daily Target for
    # Replies to Inbound Warmup Emails" bottoms out at 20 — the API accepted 10
    # yet the UI still rendered 20, so 10 is below the product floor and risks
    # being clamped at send time. Set to 20 (Tim's call, 2026-07-22) so the
    # stored value and the UI agree.
    "daily_reply_limit": 20,
}

# 12 .info domains. Every modifier here is unused across all 488 domains already
# in SmartLead — deliberately avoiding 360/club/growth/hq/lab/rev/team/zoom/dot
# and the .com modifier set, so these are not near-duplicates of existing infra.
DOMAINS = [
    "theheadlinetheorydesk.info",
    "theheadlinetheorybrief.info",
    "theheadlinetheorypress.info",
    "theheadlinetheorystory.info",
    "theheadlinetheorywire.info",
    "theheadlinetheorybeat.info",
    "theheadlinetheorydraft.info",
    "theheadlinetheoryangle.info",
    "theheadlinetheorybyline.info",
    "theheadlinetheorycolumn.info",
    "theheadlinetheorysource.info",
    "theheadlinetheoryframe.info",
]

# Used only if one of the above turns out to be taken at purchase time.
SPARES = [
    "theheadlinetheoryprint.info",
    "theheadlinetheorycopy.info",
    "theheadlinetheorysignal.info",
    "theheadlinetheorymethod.info",
]

STATE_KEY = f"acq_outlook_{GROUP_LETTER.lower()}"


# ─── MICROSOFT-flavoured Zapmail wrappers ───

def zm_headers():
    return {
        "Content-Type": "application/json",
        "x-auth-zapmail": os.environ.get("ZAPMAIL_API_KEY", "").strip(),
        "x-service-provider": PROVIDER,
        "User-Agent": "tht-infra-automation/1.0",
    }


def _zm(method, path, body=None, params=None, timeout=60):
    """Zapmail call with retries. Their API drops connections (SSL EOF, reset,
    read timeout) often enough that an unretried call will eventually kill a
    long provisioning run mid-way."""
    url = f"https://api.zapmail.ai/api{path}"
    last = None
    for attempt in range(3):
        try:
            r = requests.request(method, url, headers=zm_headers(), json=body,
                                 params=params, timeout=timeout)
            try:
                return r.json()
            except Exception:
                return {"status": r.status_code, "raw": r.text[:400]}
        except requests.exceptions.RequestException as e:
            last = e
            if attempt < 2:
                wait = 10 * (attempt + 1)
                log(f"  Zapmail {method} {path} failed ({type(e).__name__}), "
                    f"retry {attempt + 1}/2 in {wait}s", "WARN")
                time.sleep(wait)
    log(f"  Zapmail {method} {path} failed after 3 attempts: {last}", "ERROR")
    return {"status": 599, "error": str(last)}


def zm_workspace():
    d = _zm("GET", "/v2/workspaces")
    return (d.get("data") or {}).get("currentWorkspace") or {}


def zm_list_domains():
    """All MICROSOFT-side domains.

    Paging quirk: `take`/`skip` are ignored (always 10/page) — `limit` is the
    parameter that actually widens the page, and `page` walks them. We ask for
    500 and still page defensively in case the cap is lower than the fleet.
    """
    out, page = [], 1
    while page <= 50:
        d = _zm("GET", "/v2/domains", params={"limit": 500, "page": page})
        data = d.get("data") or {}
        items = data.get("domains") or data.get("items") or []
        out.extend(items)
        if not items or page >= int(data.get("totalPages") or 1):
            break
        page += 1
    return out


def _dname(zd):
    """Zapmail returns the hostname under `domain`; older Google-side payloads
    used `name`. Accept either."""
    return zd.get("domain") or zd.get("name") or ""


def _ns_resolves(domain):
    """True once the registry actually publishes NS records for the domain.

    Zapmail reports status=ACTIVE the moment a domain is attached, well before
    it resolves publicly, so trusting Zapmail alone means creating mailboxes on
    a domain the world cannot yet look up. A fresh .info typically takes
    15-60 min to appear."""
    try:
        out = subprocess.run(["nslookup", "-type=NS", domain, "8.8.8.8"],
                             capture_output=True, text=True, timeout=25).stdout
        return "nameserver" in out.lower() and "cloudns" in out.lower()
    except Exception:
        return False


def _dns_ok(zd):
    """A domain is usable once Zapmail marks it ACTIVE with valid nameservers.
    The MICROSOFT payload has no `dnsStatus` field at all — it carries `status`
    plus the two dns* flags below — so checking dnsStatus=='VERIFIED' (as the
    Google pipeline does) would never fire here."""
    if zd.get("dnsStatus") == "VERIFIED":
        return True
    return (zd.get("status") == "ACTIVE"
            and not zd.get("dnsAuthenticationInProgress")
            and not zd.get("dnsBoxInvalidNameServers"))


def zm_connect_domain(domain):
    """Connect a domain to Zapmail. `nameServers` must be a COMMA-SEPARATED
    STRING — passing the list gives 422 "Name servers are required"."""
    return _zm("POST", "/v2/domains/connect",
               {"domainName": domain,
                "nameServers": ",".join(S.CLOUDNS_NAMESERVERS)})


def _zm_ok(res):
    """Zapmail signals failure via a `status` field in a 200-shaped body, and
    uses 422 as well as 400/500 — so only treat an explicit success (or a body
    with no status/error at all) as OK."""
    if not isinstance(res, dict):
        return False
    st = res.get("status")
    if st is None:
        return not res.get("errorId") and not res.get("message")
    return int(st) in (200, 201, 202)


def zm_buy_mailbox_slots(qty):
    return _zm("POST", f"/v2/wallet/buy-addon-mailboxes?quantity={qty}", {})


def zm_create_mailboxes(domain_id, domain, specs):
    body = {domain_id: [{
        "firstName": m["firstName"], "lastName": m["lastName"],
        "mailboxUsername": m["mailboxUsername"], "domainName": domain,
    } for m in specs]}
    return _zm("POST", "/v2/mailboxes", body)


def zm_set_photos(mailbox_ids, photo_url):
    return _zm("PUT", "/v2/mailboxes", {"mailboxData": [
        {"mailboxId": m, "profilePicture": photo_url} for m in mailbox_ids]})


def zm_set_forwarding(domain_ids, url):
    return _zm("POST", "/v2/domains/forwarding",
               {"domainIds": domain_ids, "forwardTo": url})


def zm_export_to_smartlead(mailbox_ids):
    """Export to SmartLead. Path is /v2/exports/mailboxes (NOT /v2/mailboxes/export,
    which 404s) and the id list goes under `ids`."""
    return _zm("POST", "/v2/exports/mailboxes",
               {"apps": ["SMARTLEAD"], "ids": mailbox_ids}, timeout=120)


def zm_wallet():
    d = _zm("GET", "/v2/wallet/balance")
    return d.get("walletBalance"), d.get("autoRechargeEnabled")


# ─── money gate ───

def confirm_spend(what, amount):
    """Refuse to spend without a typed confirmation. Never bypass this."""
    print()
    print("  " + "!" * 58)
    print(f"  ABOUT TO SPEND MONEY: {what}")
    print(f"  Estimated cost: ${amount}")
    print("  " + "!" * 58)
    resp = input(f"  Type YES to authorise: ").strip()
    if resp != "YES":
        log("Not confirmed — aborting before any charge.", "WARN")
        sys.exit(1)
    return True


# ─── state (so a re-run resumes instead of double-buying) ───

def load_state():
    try:
        rows = db._request("GET", "/state", params={
            "select": "data", "key": f"eq.{STATE_KEY}"})
        return json.loads(rows[0]["data"]) if rows else {}
    except Exception:
        return {}


def save_state(st):
    try:
        db._request("POST", "/state",
                    json_body={"key": STATE_KEY, "data": json.dumps(st)},
                    headers={"Prefer": "resolution=merge-duplicates"})
    except Exception as e:
        log(f"Could not persist state: {e}", "WARN")


# ─── pipeline steps ───

def step1_buy_domains(domains, execute):
    """Register on Spaceship + point NS at CloudNS. SPENDS MONEY."""
    log(f"STEP 1: register {len(domains)} domains on Spaceship")

    if not S.Spaceship.is_configured():
        log("SPACESHIP_API_KEY / SPACESHIP_SECRET_KEY are not set in .env — "
            "cannot buy domains.", "ERROR")
        sys.exit(1)

    # `unknown` is deliberately kept apart from `taken`: Spaceship rate-limits
    # availability checks per domain, and a 429 used to be reported as "taken",
    # which quietly shrinks the batch. A name we could not check is an operator
    # decision, not a silent drop.
    available, taken, unknown, premium = [], [], [], []
    for d in domains:
        try:
            res = S.Spaceship.check_domain(d)
        except Exception as e:
            unknown.append((d, str(e)[:120]))
            continue
        if res.get("error"):
            unknown.append((d, res["error"]))
        elif res.get("available"):
            available.append(d)
            if res.get("premium"):
                premium.append(d)
        else:
            taken.append(d)
        time.sleep(0.5)

    for d in taken:
        log(f"  UNAVAILABLE: {d}", "WARN")
    for d, why in unknown:
        log(f"  COULD NOT CHECK: {d} ({why})", "ERROR")
    for d in premium:
        log(f"  PREMIUM PRICING: {d} — costs well above ${DOMAIN_COST}", "WARN")
    log(f"  available: {len(available)}/{len(domains)}")

    if unknown:
        log(f"  {len(unknown)} domains could not be checked — re-run in a few "
            f"minutes rather than buying a short batch.", "ERROR")
        sys.exit(1)

    if taken and len(available) < len(domains):
        log(f"  {len(taken)} unavailable — spares to consider: "
            f"{', '.join(SPARES) or '(none configured)'}", "WARN")

    if not execute:
        log(f"  DRY-RUN — would register {len(available)} domains "
            f"(~${len(available) * DOMAIN_COST})")
        return available

    confirm_spend(f"register {len(available)} .info domains on Spaceship",
                  len(available) * DOMAIN_COST)

    bought = []
    for d in available:
        res = S.Spaceship.purchase_domain(d)
        if res.get("success"):
            bought.append(d)
            log(f"  bought {d}")
        else:
            log(f"  FAILED {d}: {res.get('error')}", "ERROR")
        time.sleep(2)

    log(f"  registered {len(bought)}/{len(available)}")
    log("  setting CloudNS nameservers...")
    for d in bought:
        try:
            ok = S.Spaceship.set_nameservers(d)
            log(f"    {d}: {'ok' if ok else 'FAILED'}", "INFO" if ok else "WARN")
        except Exception as e:
            log(f"    {d}: {e}", "WARN")
        time.sleep(1)

    return bought


def step2_connect_to_zapmail(domains):
    """Connect domains on the MICROSOFT side and wait for DNS verification."""
    log(f"STEP 2: connect {len(domains)} domains to Zapmail ({PROVIDER})")

    # Connecting a domain Zapmail already has returns 400 "Unable to fetch NS
    # records to add", which is indistinguishable from a real failure — so check
    # what is already there first and only connect the genuinely missing ones.
    existing = {_dname(z): z for z in zm_list_domains()}
    todo = [d for d in domains if d not in existing]
    for d in domains:
        if d in existing:
            log(f"  {d}: already connected (id {existing[d].get('id')})")

    failed = []
    for d in todo:
        res = zm_connect_domain(d)
        ok = _zm_ok(res)
        if not ok:
            failed.append(d)
        log(f"  {d}: {'connected' if ok else json.dumps(res)[:200]}",
            "INFO" if ok else "WARN")
        time.sleep(1)

    if failed and len(failed) == len(domains):
        log("  every connect call failed — not waiting on DNS", "ERROR")
        return {}

    log("  waiting 60s for DNS propagation...")
    time.sleep(60)

    # A newly registered domain needs registry propagation before mailboxes can
    # be provisioned on it, so gate on BOTH Zapmail's view and public DNS.
    ids, pending = {}, list(domains)
    for poll in range(POLL_ATTEMPTS):
        zm_map = {_dname(d): d for d in zm_list_domains()}
        pending = []
        for d in domains:
            zd = zm_map.get(d)
            if not (zd and _dns_ok(zd)):
                pending.append(f"{d} (zapmail: {zd.get('status') if zd else 'not found'})")
            elif not _ns_resolves(d):
                pending.append(f"{d} (NS not public yet)")
            else:
                ids[d] = zd.get("id")
        if not pending:
            log(f"  all {len(ids)} domains verified + resolving publicly")
            break
        log(f"  poll {poll + 1}/{POLL_ATTEMPTS}: {len(pending)} pending — {pending[0]}")
        time.sleep(POLL_SECONDS)
    else:
        log(f"  {len(pending)} never verified: {pending}", "WARN")

    return ids


def step3_create_mailboxes(domain_ids, execute):
    """Buy Microsoft mailbox slots, then create 3 Aidan inboxes per domain.
    SPENDS MONEY."""
    needed = len(domain_ids) * ACCOUNTS_PER_DOMAIN
    log(f"STEP 3: create {needed} mailboxes")

    # Resume safety: a domain that already has mailboxes must not be charged for
    # or provisioned twice. Slots are counted as purchased AND assigned once used,
    # so spare would read 0 and we would re-buy the exact slots we already own.
    existing = {}
    for zd in zm_list_domains():
        name = _dname(zd)
        if name in domain_ids:
            ids = [m["id"] for m in (zd.get("mailboxes") or []) if m.get("id")]
            if ids:
                existing[name] = ids

    todo = {d: i for d, i in domain_ids.items() if d not in existing}
    for d, ids in existing.items():
        log(f"  {d}: already has {len(ids)} mailboxes — reusing, not re-buying")
    if not todo:
        log("  all domains already provisioned — skipping purchase and creation")
        return existing

    needed = len(todo) * ACCOUNTS_PER_DOMAIN

    # Workspace counters are provider-suffixed (…Google / …Microsoft).
    sfx = "Google" if PROVIDER == "GOOGLE" else "Microsoft"
    ws = zm_workspace()
    purchased = int(ws.get(f"totalMailboxesPurchased{sfx}") or
                    ws.get(f"assignedMailboxesCount{sfx}") or 0)
    assigned = int(ws.get(f"assignedMailboxesCount{sfx}") or 0)
    spare = max(0, purchased - assigned)
    to_buy = max(0, needed - spare)

    log(f"  {sfx} slots: {purchased} purchased / {assigned} assigned "
        f"→ {spare} free, need {needed}, buying {to_buy}")

    # `assigned` > `purchased` means live mailboxes are running on seats the
    # account no longer pays for — i.e. a subscription lapsed (2026-07-28: a
    # 69-seat add-on went PAST_DUE, "card has insufficient funds"). Zapmail
    # applies newly bought slots to that hole FIRST, so a purchase here buys no
    # usable capacity and mailbox creation still fails with "you have -N
    # mailboxes left to assign". Refuse rather than burn money on someone
    # else's arrears — fix the lapsed subscription, then re-run with --no-buy.
    deficit = max(0, assigned - purchased)
    if deficit and not ALLOW_DEFICIT:
        log(f"  {deficit} {sfx} seats are assigned but NOT paid for — a lapsed "
            f"subscription. Any slots bought now go to that deficit, not to "
            f"these {needed} mailboxes.", "ERROR")
        log(f"  Fix the PAST_DUE subscription in Zapmail billing (that also "
            f"protects the {deficit} live inboxes on it), then re-run with "
            f"--no-buy. To buy through it anyway: --allow-deficit "
            f"(costs an extra ${deficit * MAILBOX_COST}/mo).", "ERROR")
        sys.exit(1)
    if deficit:
        to_buy += deficit
        log(f"  --allow-deficit: buying {deficit} extra to clear the arrears "
            f"→ {to_buy} slots (~${to_buy * MAILBOX_COST}/mo)", "WARN")

    if to_buy:
        bal, auto = zm_wallet()
        log(f"  Zapmail wallet: ${bal} (auto-recharge: {auto})")
        if not execute:
            log(f"  DRY-RUN — would buy {to_buy} slots (~${to_buy * MAILBOX_COST}/mo)")
        else:
            confirm_spend(f"buy {to_buy} {PROVIDER.title()} mailbox slots from Zapmail",
                          f"{to_buy * MAILBOX_COST}/mo")
            res = zm_buy_mailbox_slots(to_buy)
            if "Insufficient" in str(res.get("message", "")):
                log(f"  {res.get('message')}", "ERROR")
                sys.exit(1)
            log(f"  bought: {json.dumps(res)[:200]}")
            time.sleep(3)

    if not execute:
        return dict(existing)

    out = dict(existing)
    starved = []
    for domain, did in todo.items():
        res = zm_create_mailboxes(did, domain, SPECS)
        if _zm_ok(res):
            out[domain] = res.get("data") or []
            log(f"  {domain}: " +
                ", ".join(f"{s['mailboxUsername']}@{domain}" for s in SPECS))
        else:
            msg = str(res.get("message") or "")
            if "left to assign" in msg:
                starved.append(domain)
            log(f"  {domain}: FAILED {json.dumps(res)[:200]}", "WARN")
        time.sleep(1)

    # `purchased - assigned` OVERSTATES real capacity (2026-08-08, Landry's):
    # Zapmail billed 1629 Google seats against 1611 live mailboxes, yet refused
    # to assign with "0 mailboxes left to assign" — the 18-seat gap belonged to
    # already-removed mailboxes whose subscriptions run to period end, so those
    # seats expire rather than free up. The pre-flight math above therefore
    # under-buys, and every affected domain silently ends up with no inboxes.
    # There is no endpoint exposing the true assignable count, so surface it
    # loudly here instead of leaving a half-built group looking finished.
    if starved:
        short = len(starved) * ACCOUNTS_PER_DOMAIN
        log(f"  {len(starved)} domains got NO mailboxes — Zapmail reports zero "
            f"assignable seats despite the counters showing "
            f"{spare + to_buy} free. Real capacity is lower than "
            f"purchased-minus-assigned.", "ERROR")
        log(f"  To finish, buy {short} more slots (~${short * MAILBOX_COST}/mo) "
            f"and re-run with --no-buy: {', '.join(starved)}", "ERROR")
    return out


def step4_photos_and_forwarding(mailbox_ids, domain_ids):
    log("STEP 4: profile photos + domain forwarding")

    ids = [m for v in mailbox_ids.values() for m in v]
    done = 0
    for i in range(0, len(ids), 20):
        res = zm_set_photos(ids[i:i + 20], PHOTO_URL)
        if res.get("status") == 200:
            done += len(ids[i:i + 20])
        else:
            log(f"  photo batch issue: {json.dumps(res)[:160]}", "WARN")
        time.sleep(1)
    log(f"  photos set on {done}/{len(ids)}")

    # Generic (reserve) batches ship with no forwarding — it gets set from the
    # "Client website" field when the group is assigned to a client, and a
    # stale forward is worse than none. See health_offboard.set_domain_forwarding.
    if FORWARD_TO:
        res = zm_set_forwarding(list(domain_ids.values()), FORWARD_TO)
        log(f"  forwarding → {FORWARD_TO}: {json.dumps(res)[:200]}")
    else:
        log("  forwarding: none (set on client assignment)")


def step5_export_to_smartlead(mailbox_ids, domains):
    log("STEP 5: export to SmartLead")

    # Microsoft mailboxes sit in IN_PROGRESS for a while after creation — this
    # took well over 12 minutes on the pilot, so be patient rather than
    # exporting mailboxes that do not exist yet.
    log("  waiting for mailboxes to go ACTIVE...")
    expected = len(domains) * ACCOUNTS_PER_DOMAIN
    active = 0
    for poll in range(POLL_ATTEMPTS):
        states = [mb.get("status") for zd in zm_list_domains()
                  if _dname(zd) in domains for mb in (zd.get("mailboxes") or [])]
        active = sum(1 for s in states if s == "ACTIVE")
        if active >= expected:
            log(f"  all {active} ACTIVE")
            break
        log(f"  poll {poll + 1}/{POLL_ATTEMPTS}: {active}/{expected} ACTIVE "
            f"({', '.join(sorted(set(s or '?' for s in states))) or 'none'})")
        time.sleep(POLL_SECONDS)

    if not active:
        log("  no mailboxes reached ACTIVE — skipping export. Re-run with "
            "--no-buy once Zapmail finishes provisioning.", "ERROR")
        return False

    ids = [m for v in mailbox_ids.values() for m in v]
    res = zm_export_to_smartlead(ids)
    log(f"  export: {json.dumps(res)[:250]}")
    if not _zm_ok(res):
        log("  export failed", "ERROR")
        return False
    log("  waiting 3min for SmartLead to ingest...")
    time.sleep(180)
    return True


def _internal_jwt_available():
    """True if we can reach SmartLead's internal API — either a static
    SMARTLEAD_JWT or login creds that mint one."""
    if S.SMARTLEAD_JWT:
        return True
    try:
        import health_smartlead as hsl
        return bool(hsl.get_jwt())
    except Exception:
        return False


def verify(domains):
    """Read back what SmartLead ACTUALLY has for every inbox and diff it against
    the spec. This is the answer to 'do I have to check by hand' — no.
    Returns True only if every inbox matches on all 7 warmup settings + signature."""
    log("VERIFY: reading live state back from SmartLead")

    accounts = _accounts_for(set(domains))
    expected_n = len(domains) * ACCOUNTS_PER_DOMAIN
    if not accounts:
        log(f"  no accounts found for {sorted(domains)}", "ERROR")
        return False
    log(f"  found {len(accounts)}/{expected_n} inboxes")

    headers = S.sl_internal_headers() if _internal_jwt_available() else None
    if not headers:
        log("  no internal token — cannot read warmup detail", "WARN")

    # (label, key in SmartLead's warmup payload, expected value)
    checks = [
        ("reply rate",       "reply_rate",                   WARMUP["reply_rate"]),
        ("max emails/day",   "max_email_per_day",            WARMUP["max_emails_per_day"]),
        ("warmup max count", "warmup_max_count",             WARMUP["warmup_max_count"]),
        ("warmup min count", "warmup_min_count",             WARMUP["warmup_min_count"]),
        ("rampup enabled",   "is_rampup_enabled",            WARMUP["enable_rampup"]),
        ("weekdays only",    "send_warmups_only_on_weekdays", WARMUP["weekdays_only"]),
        ("daily reply limit", "daily_reply_limit",           WARMUP["daily_reply_limit"]),
    ]

    # Tag names live under `tag_name` (not `name`) on the list payload.
    tags_by_email = {}
    off = 0
    while off < 3000:
        page = S.sl_list_accounts(offset=off, limit=100)
        if not isinstance(page, list) or not page:
            break
        for a in page:
            em = a.get("from_email") or ""
            if "@" in em and em.split("@")[1] in domains:
                tags_by_email[em] = {t.get("tag_name") for t in (a.get("tags") or [])}
        off += 100

    all_ok = True
    for acc_id, email in sorted(accounts.items(), key=lambda kv: kv[1]):
        problems = []

        acct = S.sl_get_account(acc_id) or {}

        # Signature must stay EMPTY — the sign-off is in the campaign copy, so a
        # signature here would double it up on every send.
        sig = (acct.get("signature") or "").strip()
        if sig:
            problems.append(f"signature should be empty, got {sig[:40]!r}")

        want_name = f"{SPECS[0]['firstName']} {SPECS[0]['lastName']}"
        if acct.get("from_name") != want_name:
            problems.append(f"from_name={acct.get('from_name')!r} (want {want_name!r})")
        # SmartLead calls a Google account GMAIL and a Microsoft one OUTLOOK.
        want_type = "GMAIL" if PROVIDER == "GOOGLE" else "OUTLOOK"
        if str(acct.get("type", "")).upper() != want_type:
            problems.append(f"type={acct.get('type')} (want {want_type})")
        if not acct.get("is_smtp_success"):
            problems.append("SMTP not connected")
        if not acct.get("is_imap_success"):
            problems.append("IMAP not connected")

        have_tags = tags_by_email.get(email, set())
        for want_tag in (GROUP_TAG, "Zapmail"):
            if want_tag not in have_tags:
                problems.append(f"missing tag {want_tag!r}")

        if headers:
            r = requests.get(
                f"{S.SMARTLEAD_INTERNAL_API}/email-account/"
                f"fetch-warmup-details-by-email-account-id/{acc_id}",
                headers=headers, timeout=30)
            w = {}
            if r.status_code == 200:
                try:
                    w = (r.json() or {}).get("message") or {}
                except ValueError:
                    w = {}
            if not w:
                problems.append("no warmup detail")
            for label, key, want in checks:
                got = w.get(key)
                if isinstance(want, bool):
                    if bool(got) != want:
                        problems.append(f"{label}={got} (want {want})")
                elif got is None or int(got) != int(want):
                    problems.append(f"{label}={got} (want {want})")
            if str(w.get("status", "")).upper() not in ("ACTIVE", ""):
                problems.append(f"warmup status={w.get('status')}")

        if problems:
            all_ok = False
            log(f"  FAIL {email}: " + "; ".join(problems), "WARN")
        else:
            log(f"  OK   {email}")

    if len(accounts) < expected_n:
        all_ok = False
        log(f"  only {len(accounts)}/{expected_n} inboxes present", "WARN")

    log("  VERIFIED — all inboxes match the spec" if all_ok
        else "  MISMATCHES ABOVE — do not scale until these are fixed",
        "INFO" if all_ok else "ERROR")
    return all_ok


def _accounts_for(domains):
    """SmartLead account ids whose from_email is on one of our domains."""
    found, offset = {}, 0
    while offset < 3000:
        accts = S.sl_list_accounts(offset=offset, limit=100)
        if not isinstance(accts, list) or not accts:
            break
        for a in accts:
            em = a.get("from_email") or a.get("email") or ""
            if "@" in em and em.split("@")[1] in domains:
                found[a["id"]] = em
        offset += 100
    return found


def step6_tag(accounts):
    log("STEP 6: tag in SmartLead")
    existing = S.sl_get_all_tags()
    date_tag = S.sl_find_or_create_tag(
        datetime.now().strftime("%#m/%#d/%y" if sys.platform == "win32" else "%-m/%-d/%y"),
        existing_tags=existing)
    group_tag = S.sl_find_or_create_tag(GROUP_TAG, existing_tags=existing)
    log(f"  tags: '{GROUP_TAG}'={group_tag}, date={date_tag}")

    ok = 0
    for acc_id in accounts:
        if S.sl_tag_account(acc_id, [ZAPMAIL_TAG_ID, group_tag, date_tag]).get("ok"):
            ok += 1
    log(f"  tagged {ok}/{len(accounts)}")


def step8_warmup(accounts):
    """Warmup with the spec settings. The public endpoint only carries a subset,
    so the full config goes through the internal save-warmup endpoint."""
    log("STEP 8: enable warmup")

    # Gate on a token we can actually obtain, not on the static env var: the
    # env var is deliberately blank here, but sl_internal_headers() mints a
    # fresh JWT from SMARTLEAD_LOGIN_EMAIL/PASSWORD. Checking S.SMARTLEAD_JWT
    # would skip the internal call and silently drop 4 of the 7 settings.
    if not _internal_jwt_available():
        log("  no SmartLead internal token — only the public subset "
            "(rate/day/rampup) will apply; min/max count, weekday-only and "
            "reply limit will NOT.", "WARN")

    public_body = {
        "warmup_enabled": True,
        "total_warmup_per_day": WARMUP["max_emails_per_day"],
        "daily_rampup": 5,
        "reply_rate_percentage": WARMUP["reply_rate"],
    }

    headers = S.sl_internal_headers() if _internal_jwt_available() else None
    ok = full = 0
    for acc_id, email in accounts.items():
        try:
            requests.post(S.sl_url(f"/email-accounts/{acc_id}/warmup"),
                          json=public_body, timeout=30)
            ok += 1
        except Exception as e:
            log(f"  {email}: public warmup failed {e}", "WARN")

        if headers:
          # One account's internal-API hiccup must not abort the rest of the
          # loop — the public settings are already applied by this point.
          try:
            # SmartLead creates the warmup record ASYNCHRONOUSLY after the
            # public POST above: fetching the key immediately can return
            # {"message": null} even though the POST answered 200 "updated
            # successfully". Retry rather than skip — without this, a couple of
            # accounts per batch silently keep only the public subset (no
            # min/max count, weekday setting or reply limit). Note the null also
            # means .get("message", {}) yields None, not {}, so unwrap with `or`.
            key = ""
            for attempt in range(3):
                wd = requests.get(
                    f"{S.SMARTLEAD_INTERNAL_API}/email-account/"
                    f"fetch-warmup-details-by-email-account-id/{acc_id}",
                    headers=headers, timeout=30)
                if wd.status_code == 200:
                    try:
                        key = ((wd.json() or {}).get("message") or {}).get("warmup_key_id", "")
                    except ValueError:
                        key = ""
                if key or attempt == 2:
                    break
                time.sleep(3)
            if not key:
                log(f"  {email}: no warmup record after 3 tries — only the "
                    f"public subset applied; re-run to finish", "WARN")
            if key:
                r2 = requests.post(
                    f"{S.SMARTLEAD_INTERNAL_API}/email-account/save-warmup",
                    headers=headers, timeout=30,
                    json={
                        "emailAccountId": str(acc_id),
                        "maxEmailPerDay": WARMUP["max_emails_per_day"],
                        "isRampupEnabled": WARMUP["enable_rampup"],
                        "rampupValue": 5,
                        "warmupMinCount": WARMUP["warmup_min_count"],
                        "warmupMaxCount": WARMUP["warmup_max_count"],
                        "replyRate": WARMUP["reply_rate"],
                        "dailyReplyLimit": WARMUP["daily_reply_limit"],
                        "autoAdjustWarmup": False,
                        "sendWarmupsOnlyOnWeekdays": WARMUP["weekdays_only"],
                        "useCustomDomain": False,
                        "status": "ACTIVE",
                        "warmupKeyId": key,
                    })
                if r2.status_code == 200:
                    full += 1
          except Exception as e:
            log(f"  {email}: full warmup config failed ({type(e).__name__}) — "
                f"public settings still applied", "WARN")
        time.sleep(0.5)

    log(f"  warmup on {ok}/{len(accounts)} (full config: {full})")


def show_plan(domains, execute):
    inboxes = len(domains) * ACCOUNTS_PER_DOMAIN
    sender_label = f"{SPECS[0]['firstName']} {SPECS[0]['lastName']}"
    print()
    print("  " + "=" * 58)
    print(f"  ZAPMAIL BATCH — {GROUP_TAG}")
    print("  " + "=" * 58)
    print(f"  Provider:        Zapmail / {PROVIDER}")
    print(f"  Domains:         {len(domains)} from Spaceship")
    print(f"  Inboxes:         {inboxes}  ({ACCOUNTS_PER_DOMAIN}/domain)")
    print(f"  Sender:          {sender_label} "
          f"({', '.join(s['mailboxUsername'] for s in SPECS)})")
    print(f"  Forwarding:      {FORWARD_TO or 'none (set on client assignment)'}")
    print(f"  Photo:           {PHOTO_URL.rsplit('/', 1)[-1]}")
    print(f"  Signature:       none (lives in the campaign copy)")
    print()
    print(f"  Warmup:          reply {WARMUP['reply_rate']}%, "
          f"{WARMUP['max_emails_per_day']}/day, "
          f"count {WARMUP['warmup_min_count']}-{WARMUP['warmup_max_count']}, "
          f"rampup {'on' if WARMUP['enable_rampup'] else 'off'}, "
          f"{'weekdays only' if WARMUP['weekdays_only'] else 'all days'}, "
          f"reply limit {WARMUP['daily_reply_limit']}")
    print()
    print(f"  COST — domains:  ~${len(domains) * DOMAIN_COST} one-time")
    print(f"  COST — mailboxes: ~${inboxes * MAILBOX_COST}/month")
    print()
    for d in domains:
        print(f"    {d}")
    print()
    print(f"  MODE: {'EXECUTE (will spend money, with confirmation)' if execute else 'DRY-RUN — nothing bought'}")
    print("  " + "=" * 58)
    print()


def apply_batch(path, label):
    """Point this pipeline at a batch config instead of the built-in Outlook M
    constants. One config file can hold several batches (one per group); `label`
    picks which. Everything the steps read is a module global, so overriding
    them here is all that is needed to run a different provider/sender/group.

    Batch schema (see batches/*.json):
      {"batches": [{"label": "Generic S", "provider": "GOOGLE",
                    "tag": "Generic S",   # optional, defaults to label
                    "sender": "sean_reynolds" | an ACQUISITION_SENDERS key,
                    "forward_to": "" , "domains": [...], "warmup": {...}}]}
    """
    global PROVIDER, GROUP_LETTER, GROUP_TAG, SENDER, FORWARD_TO
    global SPECS, PHOTO_URL, DOMAINS, SPARES, STATE_KEY, WARMUP, DOMAIN_COST

    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    batches = {b["label"]: b for b in cfg["batches"]}
    if label not in batches:
        log(f"--label {label!r} not in {path} (have: {', '.join(batches)})", "ERROR")
        sys.exit(1)
    b = batches[label]

    PROVIDER = b.get("provider", "GOOGLE")
    # `label` picks the batch and keys its state; `tag` is what SmartLead gets.
    # They differ only when one logical group is split across providers — a
    # client whose inboxes are part Google, part Outlook runs as two labelled
    # batches that must land under ONE client tag.
    GROUP_TAG = b.get("tag") or b["label"]
    GROUP_LETTER = GROUP_TAG.split()[-1]
    SENDER = b.get("sender", "sean_reynolds")
    FORWARD_TO = b.get("forward_to", "")
    DOMAINS = b["domains"]
    SPARES = b.get("spares", [])
    # Sean Reynolds is the standard client/generic sender (setup.INBOX_SPECS);
    # anyone else must be a registered acquisition sender.
    if SENDER == "sean_reynolds":
        SPECS = S.INBOX_SPECS
        PHOTO_URL = S.PROFILE_PHOTO_URL
    else:
        SPECS = S.ACQUISITION_SENDERS[SENDER]["specs"]
        PHOTO_URL = S.ACQUISITION_PHOTO_URLS[SENDER]
    if b.get("warmup"):
        WARMUP = b["warmup"]
    if b.get("domain_cost"):
        DOMAIN_COST = b["domain_cost"]
    # Keyed on the label, not the tag: two provider-split batches share a tag,
    # and a shared state key would make the second run treat the first run's
    # domains as already provisioned and skip them.
    STATE_KEY = "batch_" + re.sub(r"[^a-z0-9]+", "_", b["label"].lower()).strip("_")


def main():
    ap = argparse.ArgumentParser(description="Buy domains + Zapmail inboxes for a group")
    ap.add_argument("--batch", help="batch config JSON (default: the built-in Outlook M batch)")
    ap.add_argument("--label", help="which batch in --batch to run, e.g. 'Generic S'")
    ap.add_argument("--skip-export", action="store_true",
                    help="accounts are already in SmartLead — skip the Zapmail export "
                         "and go straight to tag + warmup (safe finishing pass)")
    ap.add_argument("--allow-deficit", action="store_true",
                    help="buy slots even when live mailboxes are running on unpaid "
                         "seats (pays off the arrears too — see step 3)")
    ap.add_argument("--limit", type=int, help="only do the first N domains (pilot)")
    ap.add_argument("--skip", type=int, default=0, help="skip the first N domains")
    ap.add_argument("--execute", action="store_true", help="actually buy (default: dry-run)")
    ap.add_argument("--no-buy", action="store_true",
                    help="domains are already registered — skip step 1 and resume "
                         "from the Zapmail connect (use after a mid-run failure)")
    ap.add_argument("--plan", action="store_true", help="print the plan and exit")
    ap.add_argument("--verify", action="store_true",
                    help="read live state back from SmartLead and diff it against "
                         "the spec (signature + all 7 warmup settings). Buys nothing.")
    args = ap.parse_args()

    global ALLOW_DEFICIT
    ALLOW_DEFICIT = args.allow_deficit

    if args.batch:
        if not args.label:
            ap.error("--batch requires --label (which group in the file to run)")
        apply_batch(args.batch, args.label)

    if args.verify:
        sel = DOMAINS[args.skip:]
        if args.limit:
            sel = sel[:args.limit]
        sys.exit(0 if verify(sel) else 1)

    domains = DOMAINS[args.skip:]
    if args.limit:
        domains = domains[:args.limit]

    show_plan(domains, args.execute)
    if args.plan:
        return

    st = load_state()
    already = set(st.get("provisioned", []))
    if already:
        log(f"state: {len(already)} domains already provisioned in a prior run")
        domains = [d for d in domains if d not in already]
        if not domains:
            log("nothing left to do")
            return

    if args.no_buy:
        log(f"--no-buy: treating {len(domains)} domains as already registered")
        bought = domains
    else:
        bought = step1_buy_domains(domains, args.execute)

    if not args.execute:
        log("")
        log("DRY-RUN complete — nothing was purchased. "
            "Re-run with --execute (you will still be asked to confirm each charge).")
        return

    domain_ids = step2_connect_to_zapmail(bought)
    if not domain_ids:
        log("no domains verified in Zapmail — stopping before buying mailboxes", "ERROR")
        return

    mailbox_ids = step3_create_mailboxes(domain_ids, args.execute)
    step4_photos_and_forwarding(mailbox_ids, domain_ids)
    if args.skip_export:
        log("--skip-export: accounts already in SmartLead, going straight to "
            "tag + warmup")
    elif not step5_export_to_smartlead(mailbox_ids, set(domain_ids)):
        log("stopping: mailboxes exist in Zapmail but are not in SmartLead yet. "
            "Nothing further to buy — resume later with --no-buy.", "ERROR")
        return

    accounts = _accounts_for(set(domain_ids))
    log(f"matched {len(accounts)} SmartLead accounts")
    if not accounts:
        log("no SmartLead accounts matched — cannot tag/sign/warm. "
            "Check the export, then re-run with --no-buy.", "ERROR")
        return

    step6_tag(accounts)
    step8_warmup(accounts)

    # Only checkpoint a domain as done when it FULLY succeeded — marking a
    # partial run as provisioned makes the next run skip it and silently leave
    # inboxes untagged/unwarmed.
    expected = len(domain_ids) * ACCOUNTS_PER_DOMAIN
    log("=" * 58)
    if len(accounts) < expected:
        log(f"PARTIAL — {len(accounts)}/{expected} inboxes made it into "
            f"{GROUP_TAG}. NOT checkpointing; investigate before the rest.", "WARN")
    else:
        st["provisioned"] = sorted(already | set(domain_ids))
        save_state(st)
        log(f"DONE — {len(domain_ids)} domains / {len(accounts)} inboxes in "
            f"{GROUP_TAG}, warming for 14 days.")
    log("=" * 58)


if __name__ == "__main__":
    main()
