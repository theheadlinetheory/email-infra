"""The ten fleet rules, as one runnable check. See docs/INFRA_RULES.md.

    python check_invariants.py            # human output, exit 1 on any FAIL
    python check_invariants.py --json     # machine output for the dashboard

WHY THIS IS A SCRIPT AND NOT A DASHBOARD QUERY. September 2026 cost roughly
$89k/yr not through bad decisions but because nobody was in a position to
notice: every client row was individually correct while the fleet drifted. The
same function has to answer for the screen, the cron and CI, or those three can
disagree and the quiet one is believed.

THREE STATES, NOT TWO. A rule can PASS, FAIL, or SKIP. Skipping is the point:
a check that cannot see its input must say so rather than return "no
violations found". On 2026-09-17 every campaign in the account was paused
because Smartlead ran out of credits, which makes "is this inbox in use?"
answer no for the entire fleet — a gate that treats that as a pass hands back
permission to delete everything. Absence of evidence is reported as absence of
evidence.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import Counter, defaultdict

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"

# Seasonal verticals run a short, hard season, so they carry 57 inboxes
# (57 x 15/day = 855/day) instead of the standard 42 (630/day).
SEASONAL_TARGET, STANDARD_TARGET = 57, 42

# Floors for rule 11. The fleet has been 1,700-1,900 Smartlead accounts and
# ~1,700-1,800 Zapmail mailboxes all year; anything near a single API page is a
# truncated walk, not a smaller fleet.
MIN_PLAUSIBLE_ACCOUNTS = 1000
MIN_PLAUSIBLE_MAILBOXES = 1000
RESERVE_CEILING = 84          # Generic Landscaping 1 + 2, 42 each
REPLACEMENT_CEILING = 45      # a separate pool, for swapping burned inboxes.
                              # Raised from 42 to 45 on 2026-09-18: the three
                              # inboxes on landscapeservicehq.co were kept
                              # deliberately rather than cancelled. Raising a
                              # ceiling is a decision; drifting past one is not.
EXPIRY_WINDOW_DAYS = 45

RESERVE_TAGS = ("Generic Landscaping 1", "Generic Landscaping 2")
REPLACEMENT_TAGS = ("Replacement Group",)

# Buckets that legitimately have no CRM row — they are where inboxes wait.
# Acquisition inboxes rotate across shared domains and carry a pool tag
# alongside their group tag. Both are deliberate, so rules 2 and 3 skip them.
ACQUISITION_RE = re.compile(r"^\s*[\(]?\s*(acquisition|burnt|premium)\b", re.I)

OPERATIONAL_RE = re.compile(
    r"^\s*[\(]?\s*(cleanup|replacement|generic|acquisition|retired|burnt|premium|reserve|untagged)\b",
    re.I)

# Hosted outside Zapmail, so their absence from Zapmail means nothing.
EXTERNAL_DOMAINS = {
    "headlinetheorygo.com", "headlinetheoryhq.com", "headlinetheoryhub.com",
    "headlinetheoryinfo.com", "headlinetheorynow.com", "headlinetheoryone.com",
    "headlinetheoryonline.com", "headlinetheoryplus.com", "headlinetheorypro.com",
    "headlinetheoryworld.com", "headlinetheoryyes.com",
}

_DATE_TAG_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$")

# Clients on 57 inboxes because their vertical has a short, hard season.
# This is a LIST, not a word match, for two reasons. Substring matching reads
# "Lightning Lawn Care" as holiday lighting — it is a mowing client on 42.
# And snow clients (Kinsley, Peak, GM) carry no seasonal word in their name or
# their CRM services at all, so nothing can infer them.
#
# Adding a seasonal client means adding them here. Forgetting is safe rather
# than silent: rule 1 immediately reports them as 57 where 42 was expected.
SEASONAL_CLIENTS = {
    "gm landscaping design",              # snow removal
    "kinsley landscape ltd",              # snow removal
    "peak services colorado inc",         # snow removal
    # Same client, two spellings again: "LightDMV" is the Smartlead tag and
    # "Light Dmv" is the CRM name. With only the tag listed, any caller that
    # reached target_for with the CRM spelling got 42 for a 57-inbox client.
    "lightdmv",                           # holiday lighting (tag)
    "light dmv",                          # holiday lighting (CRM)
    # The same client, spelled two ways: the Smartlead tag and the CRM name.
    # Whichever one reaches target_for has to match, so both are listed.
    "mary brite christmas lites medford",            # holiday lighting (tag)
    "mary brite s christmas lights installation",    # holiday lighting (CRM)
    "merry bright christmas lights",      # holiday lighting
    "wonderly lights of birmingham",      # holiday lighting
}


class Result:
    def __init__(self, n, name, status, detail="", violations=None):
        self.n, self.name, self.status = n, name, status
        self.detail, self.violations = detail, list(violations or [])

    def as_dict(self):
        return {"rule": self.n, "name": self.name, "status": self.status,
                "detail": self.detail, "violations": self.violations[:50],
                "violation_count": len(self.violations)}


def client_tag_of(account: dict) -> str | None:
    """The one tag that names a client. Not Zapmail, not an M/D/YY warm-up tag."""
    for t in (account.get("tags") or []):
        name = (t.get("tag_name") or t.get("name") or "").strip()
        if name and name != "Zapmail" and not _DATE_TAG_RE.match(name):
            return name
    return None


def target_for(tag: str, crm_row: dict | None) -> int:
    """57 for a seasonal client, else 42."""
    names = {_words(tag)}
    if crm_row and crm_row.get("name"):
        names.add(_words(crm_row["name"]))
    return SEASONAL_TARGET if names & SEASONAL_CLIENTS else STANDARD_TARGET


def _words(n) -> str:
    """"Mary & Brite's Christmas Lites (Medford)" -> "mary brite christmas lites medford"."""
    return " ".join(re.findall(r"[a-z0-9]+", str(n or "").lower()))


def is_free_account(crm_row: dict) -> bool:
    """A client we never invoice: no amount to charge AND notices switched off.
    Both have to agree — either alone is a fault, not an intent."""
    # Three conditions, and all three are load-bearing:
    #   retainer billing   — a per-lead client has no monthly amount by
    #                        definition and is emphatically not free
    #   no amount          — nothing to invoice
    #   notices off        — for a month-to-month client the 7/3/1 notices are
    #                        the only thing that ever asks for money
    # Dropping the first made every per-lead client read as free, because
    # `monthly_update_enabled` comes back None unless it is explicitly selected.
    return (crm_row.get("billing_model") == "retainer"
            and not crm_row.get("monthly_retainer")
            and not crm_row.get("monthly_update_enabled"))


# ── rules ─────────────────────────────────────────────────────────────────────

def _same_site(a, b) -> bool:
    """https://www.acme.com/ and https://acme.com are the same destination.
    Comparing them literally reported 25 domains as mis-forwarded when the
    only difference was a `www.` — noise that buries a real one."""
    def norm(u):
        u = str(u or "").strip().lower()
        u = re.sub(r"^https?://", "", u)
        u = re.sub(r"^www\.", "", u)
        return u.rstrip("/")
    return norm(a) == norm(b)


def _fwd(domain_record) -> str | None:
    """Zapmail's inventory helper renames forwardTo -> forward_to. Reading only
    the camelCase spelling made rule 4 compare None against None and report
    PASS on a fleet where 38 domains pointed at the wrong company."""
    d = domain_record or {}
    return d.get("forward_to") or d.get("forwardTo")


def _mailbox_count(snapshot, domain) -> int:
    """Mailboxes on a domain, from the mailbox map rather than the domain
    record, which does not carry one."""
    return (snapshot.get("_per_domain") or {}).get(str(domain).lower(), 0)


def rule_1_client_counts(s) -> Result:
    name = "every active client holds exactly 42 inboxes (57 seasonal)"
    if not s.get("accounts") or not s.get("crm"):
        return Result(1, name, SKIP, "needs Smartlead accounts and the CRM roster")
    by_tag = Counter(t for t in (client_tag_of(a) for a in s["accounts"]) if t)
    crm_by_name = {c["name"]: c for c in s["crm"] if c.get("name")}
    bad = []
    for tag, n in sorted(by_tag.items()):
        if OPERATIONAL_RE.match(tag):
            continue
        row = crm_by_name.get(tag) or next(
            (c for c in s["crm"] if _norm(c.get("name")) == _norm(tag)), None)
        if row and is_free_account(row):
            continue                      # exempt by design
        if row and (row.get("status") or "") not in ("active", ""):
            continue                      # churned; rule 5 reports the infra
        want = target_for(tag, row)
        if n != want:
            bad.append(f"{tag}: {n} (want {want})")
    return Result(1, name, FAIL if bad else PASS,
                  f"{len(by_tag)} tagged buckets", bad)


def rule_2_one_tag(s) -> Result:
    name = "every inbox carries exactly one client tag, zero untagged"
    if not s.get("accounts"):
        return Result(2, name, SKIP, "needs Smartlead accounts")
    bad = []
    for a in s["accounts"]:
        names = [(t.get("tag_name") or t.get("name") or "").strip()
                 for t in (a.get("tags") or [])]
        real = [n for n in names if n and n != "Zapmail" and not _DATE_TAG_RE.match(n)]
        if not real:
            bad.append(f"{a.get('from_email')}: untagged — belongs to nobody")
        elif len(real) > 1 and not any(ACQUISITION_RE.match(n) for n in real):
            bad.append(f"{a.get('from_email')}: {len(real)} tags {real[:3]}")
    return Result(2, name, FAIL if bad else PASS, f"{len(s['accounts'])} inboxes", bad)


def rule_3_one_client_per_domain(s) -> Result:
    name = "every domain belongs to exactly one client"
    if not s.get("accounts"):
        return Result(3, name, SKIP, "needs Smartlead accounts")
    owners = defaultdict(set)
    for a in s["accounts"]:
        dom = (a.get("from_email") or "").split("@")[-1].lower()
        tag = client_tag_of(a)
        if dom and tag:
            owners[dom].add(tag)
    bad, draining = [], []
    for d, t in sorted(owners.items()):
        if len(t) <= 1 or all(ACQUISITION_RE.match(x) for x in t):
            continue
        # A domain shared with Cleanup/Retired is mid-cancellation: those
        # mailboxes disappear on their billing date and the domain resolves
        # itself. Reported separately so it does not bury a real collision
        # between two live clients.
        clients = [x for x in t if not OPERATIONAL_RE.match(x)]
        (draining if len(clients) <= 1 else bad).append(f"{d}: {sorted(t)}")
    detail = f"{len(owners)} domains"
    if draining:
        detail += f", {len(draining)} mid-cancellation (not counted)"
    return Result(3, name, FAIL if bad else PASS, detail, bad)


def rule_4_forwarding(s) -> Result:
    name = "every domain forwards to its owning client's website"
    if not s.get("accounts") or not s.get("zm_domains"):
        return Result(4, name, SKIP, "needs Smartlead accounts and Zapmail domains")
    owners = defaultdict(set)
    for a in s["accounts"]:
        dom = (a.get("from_email") or "").split("@")[-1].lower()
        tag = client_tag_of(a)
        if dom and tag:
            owners[dom].add(tag)
    # One forwarding target per client, taken from that client's own domains.
    per_client = defaultdict(Counter)
    for dom, tags in owners.items():
        if len(tags) != 1:
            continue
        fwd = _fwd(s["zm_domains"].get(dom))
        if fwd:
            per_client[next(iter(tags))][fwd] += 1
    bad = []
    for dom, tags in sorted(owners.items()):
        if len(tags) != 1:
            continue
        tag = next(iter(tags))
        if OPERATIONAL_RE.match(tag):
            continue                      # pool domains carry no client site
        expect = per_client[tag].most_common(1)
        if not expect:
            continue
        want, got = expect[0][0], _fwd(s["zm_domains"].get(dom))
        if dom not in s["zm_domains"]:
            continue                      # external, not ours to forward
        if not _same_site(got, want):
            bad.append(f"{dom} ({tag}): {got or 'unset'} != {want}")
    return Result(4, name, FAIL if bad else PASS, f"{len(owners)} domains", bad)


def rule_5_crm_rows(s) -> Result:
    name = "every client with live infra has a CRM row with a launch date and term"
    if not s.get("accounts") or not s.get("crm"):
        return Result(5, name, SKIP, "needs Smartlead accounts and the CRM roster")
    by_tag = Counter(t for t in (client_tag_of(a) for a in s["accounts"]) if t)
    crm_norm = {_norm(c.get("name")): c for c in s["crm"] if c.get("name")}
    bad = []
    for tag, n in sorted(by_tag.items()):
        if OPERATIONAL_RE.match(tag):
            continue
        row = crm_norm.get(_norm(tag))
        if not row:
            bad.append(f"{tag}: {n} inboxes, no CRM row")
            continue
        if (row.get("status") or "") != "active":
            continue
        if not row.get("launch_date"):
            bad.append(f"{tag}: CRM row has no launch_date")
        # Only a retainer client has a term; a per-lead client is billed per
        # lead and has no renewal date to count down to.
        # A month-to-month client has no initial term — Denair has run on a
        # rolling monthly retainer for a long time with no fixed end. Demanding
        # one would invent a deadline, which is the failure rule 5 exists to
        # prevent, pointed the other way.
        if (row.get("billing_model") == "retainer"
                and (row.get("agreement_type") or "") != "month_to_month"
                and not is_free_account(row)
                and not row.get("initial_term_length")
                and not row.get("prepaid_months")):
            bad.append(f"{tag}: no contract term recorded — deadline would be a guess")
    live_tags = {_norm(t) for t in by_tag}
    for c in s["crm"]:
        if (c.get("status") or "") != "active" or c.get("billing_model") != "retainer":
            continue
        if is_free_account(c):
            continue
        if _norm(c.get("name")) not in live_tags:
            bad.append(f"{c['name']}: active in CRM, no inboxes")
    return Result(5, name, FAIL if bad else PASS, f"{len(by_tag)} buckets", bad)


def rule_6_expiry(s) -> Result:
    name = f"no domain with a live sender expires within {EXPIRY_WINDOW_DAYS} days"
    if not s.get("registrar"):
        return Result(6, name, SKIP, "registrar inventory unavailable")
    if not s.get("live_domains"):
        return Result(6, name, SKIP,
                      "no domain has a sender on an ACTIVE campaign — cannot "
                      "distinguish a quiet fleet from a paused one")
    import datetime
    today = datetime.date.today()
    bad = []
    for dom in sorted(s["live_domains"]):
        info = s["registrar"].get(dom)
        if not info or not info.get("expires"):
            continue
        try:
            exp = datetime.date.fromisoformat(info["expires"][:10])
        except ValueError:
            continue
        days = (exp - today).days
        if days <= EXPIRY_WINDOW_DAYS and not info.get("auto_renew"):
            bad.append(f"{dom}: expires {info['expires']} in {days}d, auto-renew OFF")
    return Result(6, name, FAIL if bad else PASS,
                  f"{len(s['live_domains'])} domains carry live senders", bad)


def rule_7_empty_autorenew(s) -> Result:
    name = "no empty domain is auto-renewing"
    if not s.get("zm_domains") or not s.get("registrar"):
        return Result(7, name, SKIP, "needs Zapmail domains and registrar inventory")
    bad = []
    for dom, info in sorted(s["zm_domains"].items()):
        if _mailbox_count(s, dom) > 0:
            continue
        reg = s["registrar"].get(dom) or {}
        if reg.get("auto_renew"):
            bad.append(f"{dom}: 0 mailboxes, auto-renew ON, expires {reg.get('expires')}")
    empties = sum(1 for d in s["zm_domains"] if not _mailbox_count(s, d))
    return Result(7, name, FAIL if bad else PASS, f"{empties} empty domains", bad)


def rule_8_billing(s) -> Result:
    name = "Zapmail billed quantity equals the mailboxes that exist"
    if not s.get("billing"):
        return Result(8, name, SKIP, "subscription data unavailable")
    bad = []
    for p in s["billing"]:
        if p.get("gap", 0) > 0:
            bad.append(f"{p['provider']}: billed {p['billed']}, actual {p['actual']} "
                       f"-> {p['gap']} phantom (${p['gap'] * 3 * 12:,}/yr)")
    return Result(8, name, FAIL if bad else PASS,
                  " ".join(f"{p['provider']} {p['billed']}/{p['actual']}" for p in s["billing"]),
                  bad)


def rule_9_pool_ceilings(s) -> Result:
    name = f"reserve <= {RESERVE_CEILING}, replacement <= {REPLACEMENT_CEILING}"
    if not s.get("accounts"):
        return Result(9, name, SKIP, "needs Smartlead accounts")
    by_tag = Counter(t for t in (client_tag_of(a) for a in s["accounts"]) if t)
    reserve = sum(by_tag.get(t, 0) for t in RESERVE_TAGS)
    replacement = sum(by_tag.get(t, 0) for t in REPLACEMENT_TAGS)
    bad = []
    if reserve > RESERVE_CEILING:
        bad.append(f"reserve {reserve}, {reserve - RESERVE_CEILING} over "
                   f"(${(reserve - RESERVE_CEILING) * 36:,}/yr)")
    if replacement > REPLACEMENT_CEILING:
        bad.append(f"replacement {replacement}, {replacement - REPLACEMENT_CEILING} over "
                   f"(${(replacement - REPLACEMENT_CEILING) * 36:,}/yr)")
    return Result(9, name, FAIL if bad else PASS,
                  f"reserve {reserve}/{RESERVE_CEILING}, "
                  f"replacement {replacement}/{REPLACEMENT_CEILING}", bad)


def rule_10_expired_purged(s) -> Result:
    name = "every mailbox expired at Zapmail is gone from Smartlead"
    if not s.get("accounts") or not s.get("zm_mailboxes"):
        return Result(10, name, SKIP, "needs Smartlead accounts and Zapmail mailboxes")
    bad = []
    for a in s["accounts"]:
        email = (a.get("from_email") or "").lower()
        if not email or email.split("@")[-1] in EXTERNAL_DOMAINS:
            continue
        if email not in s["zm_mailboxes"]:
            bad.append(f"{email} ({client_tag_of(a) or 'untagged'})")
    return Result(10, name, FAIL if bad else PASS,
                  f"{len(s['accounts'])} Smartlead accounts vs "
                  f"{len(s['zm_mailboxes'])} Zapmail mailboxes", bad)


def rule_11_reads_were_complete(s) -> Result:
    """The rule the other ten depend on: did we actually see the fleet?

    Six bugs in this codebase have been the same shape — a read that came back
    short, treated as complete, producing a confident wrong number, and every
    one of them pointed at deleting something we should have kept. A rule that
    PASSES on a truncated snapshot is worse than one that fails, so this checks
    the snapshot itself before any verdict above it is worth reading.

    See docs/INFRA_RULES.md rule 11.
    """
    name = ("the ten rules above were checked against a COMPLETE picture "
            "(no source returned a short answer)")
    bad = []

    # Nothing read at all is the collector not having run, which every other
    # rule already reports as SKIP. This rule judges reads that DID happen.
    if all(s.get(k) is None for k in ("accounts", "zm_mailboxes", "zm_domains", "crm")):
        return Result(11, name, SKIP, "no sources were read")

    accounts = s.get("accounts")
    if accounts is None:
        bad.append("Smartlead accounts: not read at all")
    elif len(accounts) < MIN_PLAUSIBLE_ACCOUNTS:
        bad.append(f"Smartlead returned {len(accounts)} accounts — under "
                   f"{MIN_PLAUSIBLE_ACCOUNTS}, which is a truncated walk, not a smaller fleet")

    zm = s.get("zm_mailboxes")
    if zm is None:
        bad.append("Zapmail mailboxes: not read at all")
    elif len(zm) < MIN_PLAUSIBLE_MAILBOXES:
        bad.append(f"Zapmail returned {len(zm)} mailboxes — under {MIN_PLAUSIBLE_MAILBOXES}")

    crm = s.get("crm")
    if crm is not None and len(crm) == 0:
        # A 200 with zero rows is what RLS denial looks like.
        bad.append("CRM returned zero clients — that is what a denied read looks like")

    # PostgREST caps an unbounded select at exactly 1000 and returns it as a
    # normal 200, so that count is a tell rather than a coincidence.
    for key in ("accounts", "zm_mailboxes", "zm_domains"):
        v = s.get(key)
        if v is not None and len(v) == 1000:
            bad.append(f"{key}: exactly 1000 rows — the PostgREST page cap, almost "
                       "certainly truncated")

    sizes = []
    for label, key in (("Smartlead", "accounts"), ("Zapmail mailboxes", "zm_mailboxes"),
                       ("Zapmail domains", "zm_domains"), ("CRM clients", "crm")):
        v = s.get(key)
        sizes.append(f"{label} {len(v) if v is not None else 'not read'}")
    return Result(11, name, FAIL if bad else PASS, " · ".join(sizes), bad)


RULES = [rule_1_client_counts, rule_2_one_tag, rule_3_one_client_per_domain,
         rule_4_forwarding, rule_5_crm_rows, rule_6_expiry, rule_7_empty_autorenew,
         rule_8_billing, rule_9_pool_ceilings, rule_10_expired_purged, rule_11_reads_were_complete]


def _norm(n) -> str:
    """Match a Smartlead tag to a CRM name. They are written by different
    people: "Mary & Brite Christmas Lites (Medford)" is the tag for
    "Mary & Brite's Christmas Lights Installation". Strip punctuation, company
    suffixes, parenthetical locations, and the spelling drift between
    lite/light, then compare the first few significant words."""
    t = str(n or "").lower()
    t = re.sub(r"\([^)]*\)", " ", t)              # (Medford)
    t = t.replace("&", " and ")
    t = re.sub(r"\blit(e|es)\b", "light", t)      # lites -> light
    t = re.sub(r"\blights\b", "light", t)
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    drop = {"inc", "llc", "ltd", "co", "company", "companies", "group",
            "services", "service", "installation", "installations", "the", "s"}
    words = [w for w in t.split() if w and w not in drop]
    return "".join(words[:4])


def check(snapshot: dict) -> list[Result]:
    out = []
    for fn in RULES:
        try:
            out.append(fn(snapshot))
        except Exception as e:                      # a broken rule must not
            n = RULES.index(fn) + 1                 # mask the other nine
            out.append(Result(n, fn.__name__, SKIP, f"{type(e).__name__}: {e}"))
    return out


# ── collection ────────────────────────────────────────────────────────────────

def _sl_get(path, params, tries=6):
    import requests
    key = (os.environ.get("SMARTLEAD_API_KEY") or "").strip()
    last = None
    for i in range(tries):
        try:
            r = requests.get(f"https://server.smartlead.ai/api/v1/{path}",
                             params={**params, "api_key": key}, timeout=90)
        except requests.RequestException as e:
            last = str(e); time.sleep(6 * (i + 1)); continue
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                last = "non-JSON"
        else:
            last = f"HTTP {r.status_code}"
        time.sleep(6 * (i + 1))
    raise RuntimeError(f"Smartlead {path}: {last}")


def _sl_page(path, params=None):
    """Walk a paginated Smartlead list to the end. Partial pages are the tell
    that the walk finished; a short page is not an error."""
    out, offset = [], 0
    while True:
        rows = _sl_get(path, {**(params or {}), "limit": 100, "offset": offset})
        if isinstance(rows, dict):
            rows = rows.get("data") or []
        if not isinstance(rows, list) or not rows:
            break
        out += rows
        if len(rows) < 100:
            break
        offset += 100
        time.sleep(1.1)
    return out


def collect(want_campaigns: bool = True) -> dict:
    """Gather every input once. A source that fails is left absent rather than
    empty, so the rules that need it SKIP instead of reporting no violations."""
    snap = {"errors": {}}

    try:
        snap["accounts"] = _sl_page("email-accounts")
    except Exception as e:
        snap["errors"]["accounts"] = str(e)

    try:
        import infra_lifecycle as il
        inv = il.fetch_zapmail_inventory()
        snap["zm_mailboxes"] = {k.lower(): v for k, v in (inv.get("mailboxes") or {}).items()}
        doms = {}
        for name, d in (inv.get("domains") or {}).items():
            doms[str(name).lower()] = d
        snap["zm_domains"] = doms
        # Derive the per-domain mailbox count from the mailbox map: the domain
        # records carry no mailbox list, so counting off them returned 0 for
        # all 916 and reported 916 empty domains where 303 are.
        snap["_per_domain"] = dict(
            Counter(e.split("@")[-1].lower() for e in snap["zm_mailboxes"]))
    except Exception as e:
        snap["errors"]["zapmail"] = str(e)

    try:
        import infra_lifecycle as il
        snap["crm"] = il.fetch_crm_clients()
    except Exception as e:
        snap["errors"]["crm"] = str(e)

    try:
        import domain_expiry_alert as dea
        snap["registrar"] = dea.fetch_registrar_domains()
    except Exception as e:
        snap["errors"]["registrar"] = str(e)

    try:
        import billing_followup as bf
        snap["billing"] = bf.reconcile().get("per_provider")
    except Exception as e:
        snap["errors"]["billing"] = str(e)

    # Domains carrying a sender on an ACTIVE campaign. Deliberately absent —
    # not empty — when no campaign is live, because "nothing is sending" and
    # "the account is out of credits" produce identical evidence.
    if want_campaigns:
        try:
            camps = _sl_page("campaigns")
            live = [c for c in camps if c.get("status") in ("ACTIVE", "STARTED")]
            snap["active_campaigns"] = len(live)
            if live:
                doms, failed = set(), 0
                for c in live:
                    try:
                        for x in (_sl_get(f"campaigns/{c['id']}/email-accounts", {}) or []):
                            d = (x.get("from_email") or "").split("@")[-1].lower()
                            if d:
                                doms.add(d)
                    except Exception:
                        failed += 1
                    time.sleep(0.32)
                if failed:
                    snap["errors"]["campaign_scan"] = f"{failed} campaign scans failed"
                else:
                    snap["live_domains"] = doms
        except Exception as e:
            snap["errors"]["campaigns"] = str(e)
    return snap


# ── storage ───────────────────────────────────────────────────────────────────

RESULT_KEY = "invariants_last_run"


def summarise(results, snap) -> dict:
    """The stored shape. Small enough to serve instantly, complete enough that
    the page never needs to recompute anything."""
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "active_campaigns": snap.get("active_campaigns"),
        "sources_unavailable": sorted((snap.get("errors") or {})),
        "counts": dict(Counter(r.status for r in results)),
        "results": [r.as_dict() for r in results],
    }


def store_result(payload: dict) -> bool:
    try:
        import db as store
        store._request("POST", "/state",
                       json_body={"key": RESULT_KEY, "data": json.dumps(payload),
                                  "updated_at": payload.get("generated_at")},
                       headers={"Prefer": "resolution=merge-duplicates"})
        return True
    except Exception:
        return False


def load_result() -> dict | None:
    """The last stored run, or None. The caller shows its age: a result with no
    timestamp beside it is how the Clients tab came to be a day stale without
    anyone noticing."""
    try:
        import db as store
        rows = store._request("GET", "/state",
                              params={"select": "data", "key": f"eq.{RESULT_KEY}"})
        if rows:
            return json.loads(rows[0]["data"])
    except Exception:
        pass
    return None


def run_and_store(want_campaigns: bool = True) -> dict:
    """Compute and persist. Called by the daily cron with the campaign scan,
    and on demand without it — that scan is 130 of the 210 seconds a full run
    takes, and Vercel's ceiling is 300."""
    snap = collect(want_campaigns=want_campaigns)
    payload = summarise(check(snap), snap)
    payload["scanned_campaigns"] = bool(want_campaigns)
    payload["stored"] = store_result(payload)
    return payload


def render(results, snap) -> str:
    lines = []
    width = max(len(r.name) for r in results) + 2
    for r in results:
        mark = {PASS: "PASS", FAIL: "FAIL", SKIP: "SKIP"}[r.status]
        lines.append(f"RULE {r.n:<2} {r.name:<{width}} {mark}  {r.detail}")
        for v in r.violations[:12]:
            lines.append(f"          - {v}")
        if len(r.violations) > 12:
            lines.append(f"          ... and {len(r.violations) - 12} more")
    n = Counter(r.status for r in results)
    lines.append("")
    lines.append(f"{n[PASS]} passed, {n[FAIL]} failed, {n[SKIP]} skipped")
    if snap.get("active_campaigns") == 0:
        lines.append("NOTE: 0 ACTIVE campaigns — the fleet is paused, so any check "
                     "keyed on campaign membership was skipped, not passed.")
    for k, v in (snap.get("errors") or {}).items():
        lines.append(f"NOTE: {k} unavailable: {str(v)[:120]}")
    return "\n".join(lines)


def main(argv):
    as_json = "--json" in argv
    snap = collect(want_campaigns="--no-campaigns" not in argv)
    results = check(snap)
    if as_json:
        print(json.dumps({
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "active_campaigns": snap.get("active_campaigns"),
            "sources_unavailable": list((snap.get("errors") or {})),
            "results": [r.as_dict() for r in results],
        }, indent=1))
    else:
        print(render(results, snap))
    return 1 if any(r.status == FAIL for r in results) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
