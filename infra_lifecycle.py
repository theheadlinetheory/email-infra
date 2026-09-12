"""Infrastructure lifecycle — when every client's inboxes must be decided on, and stopped.

Built for Aidan's 2026-09-05 note. Two problems, one root cause.

  1. **Seasonal off-boarding.** A holiday-lighting client signs for two months.
     We buy inboxes and warm them for 14 days BEFORE the engagement starts, so
     the infra clock starts ~2 weeks ahead of the money clock and the two never
     line up again. Zapmail bills monthly from the mailbox's creation date, so a
     2-month engagement fed by a 14-day warm-up spans *three* infra billing
     cycles, and the third one is bought after the client has already left. For
     a seasonal vertical there is no next season to spend it on.

  2. **Retainer decision deadlines.** A month-to-month retainer reaches its
     three-month mark and nobody has told us whether it continues. Deciding late
     is not free: the mailboxes have already re-billed. We need a date by which
     the answer is required, notice before it, and a default of NO.

Both reduce to the same three dates, which this module computes per client:

    effective_end   last day the client is entitled to send
    hard_stop       first Zapmail billing anniversary on/after effective_end —
                    the day the mailboxes must be gone. Paying past it is pure
                    waste; stopping before it throws away days already paid for.
    decision_by     the last day an answer is useful, i.e. early enough that a
                    scheduled removal still lands on hard_stop.

Why the anchor is the mailbox, not the client
---------------------------------------------
`clients.launch_date` is the money clock; Zapmail's per-mailbox `createdAt` is
the billing clock. They differ by the warm-up, and they differ *per mailbox*:
Merry & Bright's 56 inboxes were created across five dates, so that one client
has five billing anniversaries and five different hard stops. A single
"cancel date" per client would be wrong for most of the fleet, so everything
here is computed per creation cohort and rolled up.

Why the recommended action is not always "cancel"
-------------------------------------------------
Cancelling is only correct for infra we can never reuse. A domain carrying
another client's live mailboxes must never be cancelled, and a generically
named landscaping domain is worth recycling into the reserve pool instead. So
each client's domains are split into:

    dedicated   the client's name is in the domain (galaxymechanicalhq.info) —
                unusable by anyone else, so CANCEL.
    shared      mailboxes from more than one client sit on it — cancelling it
                would kill someone else's sending, so never cancel the domain;
                remove only this client's mailboxes.
    pool        generic domain, sole occupant — RECYCLE via health_offboard
                (re-tag to a Generic group, warm-up back on).

Deliberately NOT trusted
------------------------
* `retainer_last_billed` — records when payment landed, not when we billed. See
  the note at the top of `retainers.py`.
* An empty CRM response — the anon key's RLS can silently return zero rows, and
  "no clients" renders identically to "nothing is due". `fetch_crm_clients()`
  raises instead, so a broken read can never read as a clean board.
"""

from __future__ import annotations

import calendar
import json
import os
import re
from datetime import date, datetime, timedelta

import retainers

# ── tunables ──────────────────────────────────────────────────────────────────

COST_PER_MAILBOX = 3        # ~$3/mo per Zapmail Google Workspace mailbox
WARMUP_DAYS = 14            # inboxes cannot send for their first 14 days
DEFAULT_TERM_MONTHS = 3     # "it's usually on like a three-month agreement"
SCHEDULE_BUFFER_DAYS = 3    # file the scheduled removal this far before it lands
DECISION_LEAD_DAYS = 7      # answer required this far before the engagement ends
NOTICE_DAYS = (7, 3, 1)     # countdown touches before decision_by

# State keys (this project's `state` table, via db.py).
OVERRIDE_KEY = "infra_lifecycle_overrides"   # {client: {term_months, effective_end, seasonal, vertical, status}}
DECISION_KEY = "infra_lifecycle_decisions"   # {client: {decision, set_at, set_by, note}}
PENDING_MSG_KEY = "infra_lifecycle_pending_msgs"

# Slack. This countdown gets its OWN webhook on purpose: the shared alerts
# channel is too busy for a message whose whole value is that it is not missed.
# Falls back to the existing channels so a missing env var never means silence.
SLACK_WEBHOOK_VARS = ("SLACK_INFRA_DECISIONS_WEBHOOK", "SLACK_ZAPMAIL_WEBHOOK",
                      "SLACK_WEBHOOK_URL")
# A display name like "@aidan" does not raise a notification — only the member id
# does. Aidan Hutchinson, aidan@theheadlinetheory.com.
NOTIFY_MEMBER_IDS = ("U09B2673A4A",)

# Buckets in the health table that are not clients.
NON_CLIENT_BUCKETS = {"(acquisition)", "(generic reserve)", "burnt acquisition",
                      "retired - burned", "rock pave"}

# Verticals whose value dies on a calendar date regardless of contract length.
# `season_end` is (month, day) — the last day the work is sellable.
SEASONAL_VERTICALS = {
    "holiday_lighting": {
        "label": "Holiday lighting",
        # Matched against the normalised client name. Kept narrow on purpose:
        # a bare "light" would swallow "Lightning Lawn Care".
        "match": ("christmas light", "christmas lite", "holiday light",
                  "lights of", "merry and bright", "merry & bright"),
        # Installs sell Sep–Dec and come down in January. Nothing sells after
        # Christmas, so the infra is worthless into the new year.
        "season_end": (12, 31),
    },
    "snow_removal": {
        "label": "Snow removal",
        "match": ("snow removal", "snow plow", "snowplow", "plowing"),
        "season_end": (3, 31),
    },
}


# ── name handling ─────────────────────────────────────────────────────────────

_SUFFIXES = re.compile(
    r"\b(group|inc|inc\.|llc|l\.l\.c\.|ltd|co|company|corp|companies|"
    r"services|service|and companies)\b", re.I)


def norm_name(name: str) -> str:
    """Normalise a client name for CRM <-> SmartLead comparison.

    The two systems disagree constantly: SmartLead appends " Group", the CRM
    keeps the legal suffix, and neither spells a possessive the same way
    ("Mary & Brite Christmas Lites (Medford)" vs "Mary & Brite's Christmas
    Lights"). Strip everything that is not a distinguishing word.
    """
    s = (name or "").lower().strip()
    s = re.sub(r"\(.*?\)", " ", s)          # drop "(Medford)"
    s = s.replace("&", " and ")
    s = re.sub(r"'s\b", "", s)              # possessives
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = _SUFFIXES.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def _tokens(name: str) -> set:
    return {t for t in norm_name(name).split() if len(t) > 2}


def match_client(infra_name: str, crm_names: list[str]) -> str | None:
    """Best CRM name for a SmartLead client-group name, or None.

    Exact normalised match first, then containment, then a token-overlap score.
    Requires two shared significant tokens (or one exact-normalised hit) so that
    "Lightning Lawn Care" can never land on "Lightning Group" by accident.
    """
    n = norm_name(infra_name)
    if not n:
        return None
    by_norm = {norm_name(c): c for c in crm_names}
    if n in by_norm:
        return by_norm[n]
    for cn, orig in by_norm.items():
        if cn and (cn.startswith(n) or n.startswith(cn)):
            return orig
    best, best_score = None, 0
    it = _tokens(infra_name)
    for cn, orig in by_norm.items():
        ct = {t for t in cn.split() if len(t) > 2}
        if not ct or not it:
            continue
        shared = len(it & ct)
        score = shared / max(len(it | ct), 1)
        if shared >= 2 and score > best_score:
            best, best_score = orig, score
    return best if best_score >= 0.4 else None


# ── dates ─────────────────────────────────────────────────────────────────────

def _parse(s) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except (ValueError, TypeError):
        return None


def _parse_ts(s) -> date | None:
    """Zapmail timestamps are ISO-8601 with a Z."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return _parse(s)


def anniversary_on_or_after(anchor: date, frm: date) -> date:
    """First monthly anniversary of `anchor` falling on or after `frm`.

    Always re-derived from the original anchor so a month-end mailbox clamps
    into February and springs back to the 31st in March, rather than walking
    backwards a day at a time the way repeated +1-month addition would.
    """
    n = max((frm.year - anchor.year) * 12 + (frm.month - anchor.month), 0)
    cand = retainers.add_months_clamped(anchor, n)
    while cand < frm:
        n += 1
        cand = retainers.add_months_clamped(anchor, n)
    return cand


def season_end_on_or_after(month: int, day: int, frm: date) -> date:
    """Next occurrence of a (month, day) season close on or after `frm`."""
    d = date(frm.year, month, min(day, calendar.monthrange(frm.year, month)[1]))
    if d < frm:
        d = date(frm.year + 1, month,
                 min(day, calendar.monthrange(frm.year + 1, month)[1]))
    return d


def classify_vertical(name: str, services=None):
    """(vertical key, config) if the client is seasonal, else (None, None).

    Matches the client NAME only. `services` is accepted and deliberately
    ignored: it lists what the client does, not what we are selling for them,
    and a year-round landscaper lists snow among fifteen trades. Scanning it
    classified GM Landscaping as a seasonal snow client and gave its 80 live
    inboxes a 31 March hard stop. The vertical is the offer, not the trade — so
    a genuinely seasonal client whose name does not say so is declared through
    `infra_lifecycle_overrides`, never inferred from this list.
    """
    hay = norm_name(name)
    for key, cfg in SEASONAL_VERTICALS.items():
        if any(m in hay for m in cfg["match"]):
            return key, cfg
    return None, None


# ── data sources ──────────────────────────────────────────────────────────────

def fetch_crm_clients() -> list[dict]:
    """Every CRM client row. Raises on an empty read — see the module docstring.

    `INFRA_LIFECYCLE_CRM_FILE` points at a captured JSON roster for offline runs.
    """
    path = os.environ.get("INFRA_LIFECYCLE_CRM_FILE", "").strip()
    if path:
        with open(path) as fh:
            return json.load(fh)

    import requests
    url = (os.environ.get("CRM_SUPABASE_URL", "").strip()
           or retainers.CRM_URL_DEFAULT).rstrip("/")
    key = os.environ.get("CRM_SUPABASE_KEY", "").strip() or retainers.CRM_KEY_DEFAULT
    fields = ("id,name,status,billing_model,agreement_type,launch_date,"
              "renewal_day,prepaid_months,monthly_retainer,retainer_currency,"
              "services,has_inbox_mgmt")
    r = requests.get(f"{url}/rest/v1/clients?select={fields}",
                     headers={"apikey": key, "Authorization": f"Bearer {key}"},
                     timeout=20)
    r.raise_for_status()
    rows = r.json()
    if not rows:
        # A 200 with zero rows is what RLS denial looks like. Treating it as
        # "no clients" would render as a clean board with nothing due.
        raise RuntimeError(
            f"CRM returned 0 client rows from {url} — the key is being denied by "
            "RLS or the table is empty. Refusing to build a lifecycle board that "
            "would show nothing due. Set CRM_SUPABASE_KEY to a key that can read "
            "clients, or INFRA_LIFECYCLE_CRM_FILE to a captured roster.")
    return rows


def fetch_zapmail_inventory(provider_headers=("GOOGLE", "MICROSOFT")) -> dict:
    """{"mailboxes": {email: {...}}, "domains": {domain: {...}}} straight from Zapmail.

    Zapmail partitions inventory by `x-service-provider`: a GOOGLE-headed call
    cannot see MICROSOFT domains at all, so both are always walked.
    """
    path = os.environ.get("INFRA_LIFECYCLE_ZM_FILE", "").strip()
    if path:
        with open(path) as fh:
            raw = json.load(fh)
        doms = {d["domain"]: d for d in raw.get("domains", [])}
        return {"mailboxes": raw.get("mailboxes", {}), "domains": doms}

    import requests
    key = (os.environ.get("ZAPMAIL_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("ZAPMAIL_API_KEY is not set")
    mailboxes, domains = {}, {}
    for prov in provider_headers:
        headers = {"Content-Type": "application/json", "x-auth-zapmail": key,
                   "x-service-provider": prov}
        page = 1
        while True:
            r = requests.get(
                f"https://api.zapmail.ai/api/v2/domains?page={page}&limit=100",
                headers=headers, timeout=60)
            if r.status_code != 200:
                break
            data = r.json().get("data", {})
            for dom in data.get("domains", []):
                dn = (dom.get("domain") or "").lower()
                if not dn:
                    continue
                domains[dn] = {"domain": dn, "id": dom.get("id"), "provider": prov,
                               "status": dom.get("status"),
                               "expire_on": (dom.get("expireOn") or "")[:10],
                               "auto_renew": dom.get("autoRenew"),
                               "forward_to": dom.get("forwardTo")}
                for mb in (dom.get("mailboxes") or []):
                    u = mb.get("username")
                    if u:
                        mailboxes[f"{u}@{dn}".lower()] = {
                            "id": mb.get("id"), "created_at": mb.get("createdAt"),
                            "domain": dn, "domain_id": dom.get("id"),
                            "provider": prov, "status": mb.get("status")}
            if page >= data.get("totalPages", 1):
                break
            page += 1
    if not mailboxes:
        raise RuntimeError("Zapmail returned no mailboxes — check ZAPMAIL_API_KEY")
    return {"mailboxes": mailboxes, "domains": domains}


def _state(key, default):
    try:
        import db as store
        rows = store._request("GET", "/state",
                              params={"select": "data", "key": f"eq.{key}"})
        if rows:
            return json.loads(rows[0]["data"])
    except Exception:
        pass
    return default


def load_overrides() -> dict:
    """Per-client overrides keyed by normalised name.

    The CRM has no contract-term column and no seasonal flag, so anything the
    data cannot tell us lives here: {"galaxy plumbing": {"term_months": 6}}.
    """
    return {norm_name(k): v for k, v in (_state(OVERRIDE_KEY, {}) or {}).items()}


def load_decisions() -> dict:
    return {norm_name(k): v for k, v in (_state(DECISION_KEY, {}) or {}).items()}


# ── the maths ─────────────────────────────────────────────────────────────────

def domain_role(domain: str, client_name: str, clients_on_domain: set) -> str:
    """dedicated | shared | pool — decides whether the domain may be cancelled."""
    real = {c for c in clients_on_domain if c and c.lower() not in NON_CLIENT_BUCKETS}
    if len(real) > 1:
        return "shared"
    stem = re.sub(r"\.[a-z.]+$", "", (domain or "").lower())
    tokens = [t for t in _tokens(client_name) if len(t) > 3]
    if tokens and any(t in stem for t in tokens):
        return "dedicated"
    return "pool"


def contract_end(crm: dict | None, override: dict, today: date):
    """(effective_end, basis, phase) — when the client's entitlement to send ends.

    `phase` is what makes the board quiet enough to be worth reading:

      committed  inside the agreed term. The term end is a real deadline and
                 the 7/3/1 countdown fires against it — this is the case Aidan
                 wants caught (Galaxy at its three-month mark).
      rolling    past the initial term on month-to-month. The review recurs
                 with every monthly renewal, so a date is shown, but firing a
                 countdown every month for a happy client is just noise.
      per_lead   no term at all; infra runs until the client is off-boarded.
      unknown    not enough in the CRM to say.
    """
    if override.get("effective_end"):
        d = _parse(override["effective_end"])
        if d:
            return d, "override", "committed"
    if not crm:
        return None, None, "unknown"
    if (crm.get("billing_model") or "") == "per_lead" and not crm.get("prepaid_months"):
        return None, "per-lead — no fixed term", "per_lead"

    launch = _parse(crm.get("launch_date"))
    if not launch:
        return None, "no launch date", "unknown"

    term = override.get("term_months") or crm.get("prepaid_months")
    basis = ("override term" if override.get("term_months")
             else "prepaid_months" if term else f"assumed {DEFAULT_TERM_MONTHS}mo term")
    end = retainers.add_months_clamped(launch, int(term or DEFAULT_TERM_MONTHS))
    if end >= today:
        return end, basis, "committed"

    # Term already served. A month-to-month client simply renews, so the next
    # infra review is the next renewal — not a deadline we have blown.
    if (crm.get("agreement_type") or "") == "month_to_month" and crm.get("renewal_day"):
        nxt = retainers.next_renewal_on_or_after(int(crm["renewal_day"]), today)
        return nxt, "monthly renewal (initial term served)", "rolling"
    return end, basis + " (served)", "ended"


def build_client(name, mailboxes, crm, override, decision, dom_clients, today):
    """One client's lifecycle row. `mailboxes` is a list of Zapmail records."""
    # Name matching is a heuristic — the CRM has no vertical column, so a
    # lighting client called "Twinkle & Co" would look evergreen. The override
    # registry is the authority in both directions: seasonal=False stops a false
    # positive, and vertical="holiday_lighting" declares one the name misses.
    vertical, vcfg = classify_vertical(
        (crm or {}).get("name") or name, (crm or {}).get("services"))
    if override.get("vertical") in SEASONAL_VERTICALS:
        vertical = override["vertical"]
        vcfg = SEASONAL_VERTICALS[vertical]
    if override.get("seasonal") is False:
        vertical, vcfg = None, None

    end, basis, phase = contract_end(crm, override, today)

    # A seasonal client's infra dies with the season even if the paper runs on.
    season_close = None
    if vcfg:
        anchor = _parse((crm or {}).get("launch_date")) or today
        season_close = season_end_on_or_after(*vcfg["season_end"], anchor)
        if end is None:
            end, basis, phase = season_close, "season close", "committed"
        elif season_close < end:
            end, basis = season_close, "season close (before contract end)"

    # Group mailboxes into billing cohorts by creation date — one client can
    # easily carry five different anniversaries.
    cohorts = {}
    for mb in mailboxes:
        c = _parse_ts(mb.get("created_at"))
        if not c:
            continue
        cohorts.setdefault(c, []).append(mb)

    rows, hard_stops, wasted = [], [], 0.0
    for anchor in sorted(cohorts):
        members = cohorts[anchor]
        next_bill = anniversary_on_or_after(anchor, today)
        hard = anniversary_on_or_after(anchor, end) if end else None
        waste_days = (hard - end).days if (hard and end) else None
        cost = len(members) * COST_PER_MAILBOX
        if waste_days:
            wasted += cost * (waste_days / 30.0)
        if hard:
            hard_stops.append(hard)
        rows.append({
            "created": anchor.isoformat(),
            "mailboxes": len(members),
            "billing_day": anchor.day,
            "monthly_cost": cost,
            "next_bill": next_bill.isoformat(),
            "sendable_from": (anchor + timedelta(days=WARMUP_DAYS)).isoformat(),
            "hard_stop": hard.isoformat() if hard else None,
            "schedule_by": (hard - timedelta(days=SCHEDULE_BUFFER_DAYS)).isoformat()
                           if hard else None,
            "wasted_days": waste_days,
        })

    hard_stop = max(hard_stops) if hard_stops else None
    schedule_by = (min(hard_stops) - timedelta(days=SCHEDULE_BUFFER_DAYS)
                   if hard_stops else None)

    # The answer has to arrive early enough to still stop the first cohort.
    decision_by = None
    if end:
        decision_by = end - timedelta(days=DECISION_LEAD_DAYS)
        if schedule_by and schedule_by < decision_by:
            decision_by = schedule_by

    # Domains, split by whether they may ever be cancelled.
    roles = {}
    for mb in mailboxes:
        d = mb.get("domain")
        if d:
            roles[d] = domain_role(d, (crm or {}).get("name") or name,
                                   dom_clients.get(d, set()))
    by_role = {"dedicated": [], "shared": [], "pool": []}
    for d, role in sorted(roles.items()):
        by_role[role].append(d)

    # A shared domain carries someone else's live mailboxes, so it can never be
    # cancelled outright — only this client's mailboxes come off it. Any client
    # holding one gets a split plan, never a blanket "cancel".
    if by_role["shared"]:
        action = "split" if by_role["dedicated"] else "unpick"
    elif by_role["dedicated"] and not by_role["pool"]:
        action = "cancel"
    elif by_role["dedicated"]:
        action = "split"
    else:
        action = "recycle"
    action_detail = {
        "cancel": "cancel the domains outright — dedicated, never reusable",
        "split": (f"cancel {len(by_role['dedicated'])} dedicated domain(s); "
                  + (f"leave {len(by_role['shared'])} shared domain(s) up and remove "
                     "only this client's mailboxes" if by_role["shared"]
                     else f"recycle {len(by_role['pool'])} pool domain(s) to the reserve")),
        "unpick": ("every domain is shared with another client — remove this "
                   "client's mailboxes only, cancel nothing"),
        "recycle": "generic domains — re-tag to a Generic group and turn warm-up back on",
    }[action]

    n = len(mailboxes)
    days_to_decision = (decision_by - today).days if decision_by else None
    recorded = (decision or {}).get("decision")
    # "We will default to no" — an unanswered deadline is a stop, not a pause.
    # Only a committed term can default: a rolling client renewed by paying, and
    # a per-lead client never had a term to miss.
    if recorded in ("renew", "stop"):
        outcome = recorded
    elif phase == "committed" and days_to_decision is not None and days_to_decision < 0:
        outcome = "stop (defaulted — no answer by the deadline)"
    elif phase in ("rolling", "per_lead"):
        outcome = "ongoing"
    else:
        outcome = "undecided"

    # An override can assert a client is live when the CRM has no row for it
    # (Wonderly Lights, Landy Rose). Without this the tool calls a paying client
    # "infra with nobody paying for it" and points at cancelling it.
    asserted = (override.get("status") or "").lower() or None
    effective_status = asserted or (crm or {}).get("status")

    flags = []
    if not crm and asserted == "active":
        flags.append("live client with no CRM row — add one so it gets real dates")
    elif not crm:
        flags.append("no CRM client — infra with nobody paying for it")
    elif (crm.get("status") or "") != "active":
        flags.append(f"CRM status {crm.get('status')}")
    if crm and not crm.get("launch_date") and phase != "per_lead":
        flags.append("no launch date — end date is a guess")
    if basis and basis.startswith("assumed"):
        flags.append(basis + " — no contract-term field in the CRM")
    if by_role["shared"]:
        flags.append(f"{len(by_role['shared'])} shared domain(s) — never cancel the domain")

    return {
        "client": name,
        "phase": phase,
        "infra_buckets": sorted({mb.get("bucket") for mb in mailboxes if mb.get("bucket")}),
        "crm_name": (crm or {}).get("name"),
        "status": effective_status,
        "status_asserted": bool(asserted),
        "billing_model": (crm or {}).get("billing_model"),
        "agreement_type": (crm or {}).get("agreement_type"),
        "launch_date": (crm or {}).get("launch_date"),
        "vertical": vertical,
        "vertical_label": (vcfg or {}).get("label"),
        "seasonal": bool(vcfg),
        "season_close": season_close.isoformat() if season_close else None,
        "effective_end": end.isoformat() if end else None,
        "end_basis": basis,
        "decision_by": decision_by.isoformat() if decision_by else None,
        "days_to_decision": days_to_decision,
        "hard_stop": hard_stop.isoformat() if hard_stop else None,
        "schedule_by": schedule_by.isoformat() if schedule_by else None,
        "mailboxes": n,
        "monthly_cost": n * COST_PER_MAILBOX,
        "wasted_cost": round(wasted, 2),
        "cohorts": rows,
        "domains": by_role,
        "domain_count": len(roles),
        "action": action,
        "action_detail": action_detail,
        "decision": recorded,
        "outcome": outcome,
        "flags": flags,
        "urgency": _urgency(days_to_decision, outcome, phase),
    }


def _urgency(days, outcome, phase):
    if outcome.startswith("stop (defaulted"):
        return "crit"
    # An answered client is not urgent, however close its date. Only a deadline
    # that passed with NO answer stays red.
    if outcome in ("renew", "stop", "ongoing"):
        return ""
    if phase not in ("committed", "ended") or days is None:
        return "" if phase in ("rolling", "per_lead") else "unknown"
    if days <= 3:
        return "crit"
    if days <= 7:
        return "warn"
    if days <= 30:
        return "soon"
    return ""


def build(today: date | None = None) -> dict:
    """The whole board: one row per client that has infrastructure."""
    today = today or date.today()
    import db as store

    crm_rows = fetch_crm_clients()
    crm_names = [c["name"] for c in crm_rows if c.get("name")]
    crm_by_name = {c["name"]: c for c in crm_rows if c.get("name")}

    inv = fetch_zapmail_inventory()
    mailboxes = inv["mailboxes"]

    health = {r["email"]: r for r in store.get_health_status_all()}

    # email -> owning client bucket, and domain -> the clients sitting on it
    by_bucket, dom_clients = {}, {}
    for email, mb in mailboxes.items():
        owner = (health.get(email) or {}).get("client") or "(untagged)"
        by_bucket.setdefault(owner, []).append(dict(mb, email=email, bucket=owner))
        dom_clients.setdefault(mb.get("domain"), set()).add(owner)

    # SmartLead splits one client across several tag buckets ("Denair Group" and
    # "Denair Hvac, Inc." are 106 + 3 mailboxes of the same client). Roll them up
    # under the CRM name, or the same client gets two contradictory deadlines.
    merged, labels = {}, {}
    for bucket, mbs in by_bucket.items():
        if bucket.lower() in NON_CLIENT_BUCKETS or bucket == "(untagged)":
            continue
        crm_name = match_client(bucket, crm_names)
        key = norm_name(crm_name or bucket)
        merged.setdefault(key, []).extend(mbs)
        # Prefer the CRM's spelling; otherwise the longest bucket name seen.
        if crm_name:
            labels[key] = crm_name
        elif len(bucket) > len(labels.get(key, "")):
            labels.setdefault(key, bucket)

    overrides, decisions = load_overrides(), load_decisions()

    rows, matched_crm = [], set()
    for key, mbs in merged.items():
        crm_name = match_client(labels[key], crm_names)
        if crm_name:
            matched_crm.add(crm_name)
        rows.append(build_client(labels[key], mbs, crm_by_name.get(crm_name),
                                 overrides.get(key, {}), decisions.get(key, {}),
                                 dom_clients, today))

    # Real deadlines first, then rolling reviews, then everything undated.
    phase_rank = {"committed": 0, "ended": 0, "rolling": 1, "per_lead": 2, "unknown": 2}
    rows.sort(key=lambda r: (phase_rank.get(r["phase"], 3),
                             r["days_to_decision"] if r["days_to_decision"] is not None
                             else 10 ** 6, -r["mailboxes"]))

    # An asserted-active client is not an orphan even with no CRM row.
    orphans = [r for r in rows if r["status"] != "active"]
    no_infra = sorted(c["name"] for c in crm_rows
                      if c.get("status") == "active" and c["name"] not in matched_crm)

    due = [r for r in rows if r["phase"] == "committed"
           and r["days_to_decision"] is not None
           and r["days_to_decision"] <= 30 and r["outcome"] == "undecided"]

    # Recycling an off-boarded client moves its inboxes into the reserve rather
    # than cancelling them, so the bill does not drop — the spend just stops
    # being attributable to a client. Counting the pool keeps that visible
    # instead of letting it vanish between the client rows.
    pools = {}
    for bucket, mbs in by_bucket.items():
        if bucket.lower() in NON_CLIENT_BUCKETS or bucket == "(untagged)":
            pools[bucket] = {"mailboxes": len(mbs),
                             "monthly_cost": len(mbs) * COST_PER_MAILBOX}
    pool_total = sum(p["mailboxes"] for p in pools.values())

    return {
        "as_of": today.isoformat(),
        "rows": rows,
        "orphans": orphans,
        "active_clients_without_infra": no_infra,
        "unmapped_mailboxes": len(by_bucket.get("(untagged)", [])),
        "pools": pools,
        "summary": {
            "clients": len(rows),
            "mailboxes": sum(r["mailboxes"] for r in rows),
            "monthly_cost": sum(r["monthly_cost"] for r in rows),
            "decisions_due_30d": len(due),
            "decisions_overdue": sum(1 for r in rows
                                     if r["outcome"].startswith("stop (defaulted")),
            "wasted_cost": round(sum(r["wasted_cost"] for r in rows), 2),
            "orphan_mailboxes": sum(r["mailboxes"] for r in orphans),
            "orphan_monthly_cost": sum(r["monthly_cost"] for r in orphans),
            "seasonal_clients": sum(1 for r in rows if r["seasonal"]),
            "pool_mailboxes": pool_total,
            "pool_monthly_cost": pool_total * COST_PER_MAILBOX,
        },
    }


# ── notices ───────────────────────────────────────────────────────────────────

def notices(board: dict) -> list[dict]:
    """The 7/3/1 countdown touches, plus the day the default fires.

    Distinct from the 7/3/1 pre-renewal notices in the fulfillment repo: those
    key on `clients.renewal_day` and ask "are they paying next month?". These
    key on `decision_by` and ask "do we keep buying the infrastructure?".
    """
    out = []
    for r in board["rows"]:
        d = r["days_to_decision"]
        # Only a committed term has a deadline to count down to. A rolling
        # month-to-month client would otherwise page Aidan every single month.
        if d is None or r["decision"] == "renew" or r["phase"] != "committed":
            continue
        if d in NOTICE_DAYS:
            out.append({"client": r["client"], "touch": f"T-{d}", "row": r,
                        "text": _notice_text(r, d)})
        elif d == 0:
            out.append({"client": r["client"], "touch": "final", "row": r,
                        "text": _notice_text(r, 0)})
        elif d < 0 and r["decision"] != "stop":
            out.append({"client": r["client"], "touch": "defaulted", "row": r,
                        "text": _notice_text(r, d)})
    return out


def _notice_text(r: dict, days: int) -> str:
    money = f"${r['monthly_cost']}/mo across {r['mailboxes']} inbox(es)"
    head = (f"*{r['client']}* — infrastructure decision "
            + ("OVERDUE" if days < 0 else f"due in {days} day(s)" if days else "DUE TODAY"))
    lines = [
        head,
        f"• Engagement ends *{r['effective_end']}* ({r['end_basis']})",
        f"• Decision deadline *{r['decision_by']}* — after this we default to NO",
        f"• Infra hard stop *{r['hard_stop']}* — schedule removal by {r['schedule_by']}",
        f"• {money}; recommended action: *{r['action']}*",
    ]
    if r["seasonal"]:
        lines.insert(1, f"• Seasonal ({r['vertical_label']}) — season closes {r['season_close']}, "
                        "no reuse until next year")
    if r["wasted_cost"]:
        lines.append(f"• ~${r['wasted_cost']:.0f} already committed past the end date "
                     "(warm-up started the billing clock early)")
    for f in r["flags"]:
        lines.append(f"• ⚠️ {f}")
    return "\n".join(lines)


def _mentions() -> str:
    return " ".join(f"<@{uid}>" for uid in NOTIFY_MEMBER_IDS)


def post_slack(text: str) -> str:
    """Post one notice, tagging whoever has to make the call.

    Returns 'webhook' or 'queued'. Queueing (rather than dropping) matters here:
    an unsent decision notice is exactly the failure the module exists to stop,
    so it is parked in state for a session to flush via the Slack MCP.
    """
    import requests
    body = f"{_mentions()} {text}".strip() if NOTIFY_MEMBER_IDS else text
    for var in SLACK_WEBHOOK_VARS:
        hook = (os.environ.get(var) or "").strip()
        if not hook:
            continue
        try:
            r = requests.post(hook, json={"text": body}, timeout=10)
            if r.status_code in (200, 201):
                return "webhook"
        except requests.RequestException:
            pass
    try:
        import db as store
        pend = (_state(PENDING_MSG_KEY, {}) or {}).get("messages", [])
        pend.append({"text": body, "ts": datetime.now().isoformat(timespec="seconds")})
        store._request("POST", "/state",
                       json_body={"key": PENDING_MSG_KEY,
                                  "data": json.dumps({"messages": pend}),
                                  "updated_at": datetime.now().isoformat()},
                       headers={"Prefer": "resolution=merge-duplicates"})
    except Exception:
        pass
    return "queued"


def post_notices(board: dict, dry_run: bool = True) -> dict:
    """Send today's countdown touches to Slack."""
    msgs = notices(board)
    if dry_run:
        return {"dry_run": True, "count": len(msgs),
                "messages": [f"{_mentions()} {m['text']}".strip() for m in msgs]}
    results = [post_slack(m["text"]) for m in msgs]
    return {"dry_run": False, "count": len(msgs),
            "sent": results.count("webhook"), "queued": results.count("queued")}


def removal_plan(clients, today: date | None = None) -> dict:
    """What it would take to stop paying for these clients. Plans only — sends nothing.

    Splits every domain the named clients occupy into two piles, using a rule
    that is deliberately STRICTER than `domain_role`:

      cancel_domains   EVERY mailbox on the domain belongs to a client in this
                       list, so the whole domain can go via the documented
                       `{"domainIds": [...], "remove": true}` call.
      unpick_mailboxes anything else lives on the domain — another client, or a
                       generic-reserve/acquisition inbox. The domain must stay
                       up and only these mailboxes come off.

    Why stricter: `domain_role` treats reserve/acquisition buckets as "not a
    client" so a lone client on a pool domain classifies as recyclable. That is
    right for deciding what a client's estate IS, and wrong for deciding what may
    be CANCELLED — cancelling a domain that also carries reserve inboxes would
    destroy warmed inventory that no client report would ever miss.
    """
    today = today or date.today()
    import db as store

    inv = fetch_zapmail_inventory()
    mailboxes = inv["mailboxes"]
    health = {r["email"]: r for r in store.get_health_status_all()}
    wanted = {norm_name(c) for c in clients}

    # domain -> every mailbox on it, with its owning bucket
    by_domain: dict[str, list] = {}
    for email, mb in mailboxes.items():
        owner = (health.get(email) or {}).get("client") or "(untagged)"
        by_domain.setdefault(mb.get("domain"), []).append(
            {**mb, "email": email, "bucket": owner, "norm": norm_name(owner)})

    cancel, unpick, per_client = [], [], {}
    for domain, members in sorted(by_domain.items()):
        ours = [m for m in members if m["norm"] in wanted]
        if not ours:
            continue
        others = [m for m in members if m["norm"] not in wanted]
        entry = {"domain": domain, "domain_id": ours[0].get("domain_id"),
                 "provider": ours[0].get("provider"),
                 "mailboxes": len(ours), "monthly_cost": len(ours) * COST_PER_MAILBOX}
        if others:
            entry["blocked_by"] = sorted({m["bucket"] for m in others})
            entry["other_mailboxes"] = len(others)
            entry["mailbox_ids"] = [m.get("id") for m in ours]
            entry["emails"] = sorted(m["email"] for m in ours)
            unpick.append(entry)
        else:
            cancel.append(entry)
        for m in ours:
            c = per_client.setdefault(m["bucket"], {"mailboxes": 0, "domains": set()})
            c["mailboxes"] += 1
            c["domains"].add(domain)

    def _prov_bodies(rows):
        out = {}
        for r in rows:
            out.setdefault(r["provider"] or "GOOGLE", []).append(r["domain_id"])
        return [{"provider": p, "endpoint": "PUT /api/v2/mailboxes/scheduled-removal",
                 "body": {"domainIds": ids, "remove": True}} for p, ids in out.items()]

    return {
        "as_of": today.isoformat(),
        "clients": sorted(clients),
        "cancel_domains": cancel,
        "unpick_mailboxes": unpick,
        "requests": _prov_bodies(cancel),
        "per_client": {k: {"mailboxes": v["mailboxes"], "domains": len(v["domains"])}
                       for k, v in sorted(per_client.items())},
        "summary": {
            "domains_cancellable": len(cancel),
            "mailboxes_via_domain": sum(r["mailboxes"] for r in cancel),
            "savings_via_domain": sum(r["monthly_cost"] for r in cancel),
            "domains_blocked": len(unpick),
            "mailboxes_via_unpick": sum(r["mailboxes"] for r in unpick),
            "savings_via_unpick": sum(r["monthly_cost"] for r in unpick),
            "total_monthly_saving": sum(r["monthly_cost"] for r in cancel + unpick),
        },
        "note": ("Domain cancellations use the documented domainIds call. The unpick "
                 "pile needs per-mailbox removal (mailboxIds), which is NOT documented "
                 "in ZAPMAIL_REMOVAL_BOT.md — verify it on a single mailbox before "
                 "relying on it. Nothing here has been sent."),
    }


def record_override(client: str, **fields) -> dict:
    """Set per-client facts the CRM has nowhere to store.

    Accepted: term_months, effective_end (YYYY-MM-DD), seasonal (bool),
    vertical (a SEASONAL_VERTICALS key), status ("active" to assert a client is
    live despite having no CRM row). Passing None for a field clears it.
    """
    allowed = {"term_months", "effective_end", "seasonal", "vertical", "status", "note"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"unknown override field(s): {', '.join(sorted(bad))}")
    import db as store
    cur = _state(OVERRIDE_KEY, {}) or {}
    key = norm_name(client)
    row = dict(cur.get(key, {}))
    for k, v in fields.items():
        if v is None:
            row.pop(k, None)
        else:
            row[k] = v
    row["set_at"] = datetime.now().isoformat(timespec="seconds")
    cur[key] = row
    store._request("POST", "/state",
                   json_body={"key": OVERRIDE_KEY, "data": json.dumps(cur),
                              "updated_at": datetime.now().isoformat()},
                   headers={"Prefer": "resolution=merge-duplicates"})
    return row


def record_decision(client: str, decision: str, note: str = "", by: str = "") -> dict:
    """Persist a renew/stop answer so the countdown stops chasing it."""
    if decision not in ("renew", "stop"):
        raise ValueError("decision must be 'renew' or 'stop'")
    import db as store
    cur = _state(DECISION_KEY, {}) or {}
    cur[norm_name(client)] = {"decision": decision, "note": note, "set_by": by,
                              "set_at": datetime.now().isoformat(timespec="seconds")}
    store._request("POST", "/state",
                   json_body={"key": DECISION_KEY, "data": json.dumps(cur),
                              "updated_at": datetime.now().isoformat()},
                   headers={"Prefer": "resolution=merge-duplicates"})
    return cur[norm_name(client)]


# ── CLI ───────────────────────────────────────────────────────────────────────

def _fmt(board: dict) -> str:
    s, out = board["summary"], []
    out.append(f"Infrastructure lifecycle — as of {board['as_of']}")
    out.append(f"{s['clients']} clients with infra · {s['mailboxes']} mailboxes · "
               f"${s['monthly_cost']:,}/mo")
    out.append(f"{s['decisions_due_30d']} decisions due in 30d · "
               f"{s['decisions_overdue']} overdue · "
               f"${s['wasted_cost']:,.0f} committed past end dates")
    if s.get("pool_mailboxes"):
        out.append(f"unassigned pool: {s['pool_mailboxes']} mailboxes "
                   f"(${s['pool_monthly_cost']:,}/mo) — reserve + acquisition, no client attached")
    if s["orphan_mailboxes"]:
        out.append(f"⚠️  {s['orphan_mailboxes']} mailboxes (${s['orphan_monthly_cost']:,}/mo) "
                   "belong to no active client")
    hdr = (f"  {'decide by':<11}{'d':>5}  {'ends':<11}{'hard stop':<11}"
           f"{'mb':>4}{'$/mo':>6}  {'action':<9}{'client'}")
    sections = [(("committed", "ended"), "DECISIONS WITH A DEADLINE"),
                (("rolling",), "ROLLING — reviewed each renewal, no countdown"),
                (("per_lead",), "PER-LEAD — no term; runs until off-boarded"),
                (("unknown",), "NO DATES — CRM has no launch date")]
    for phases, title in sections:
        group = [r for r in board["rows"] if r["phase"] in phases]
        if not group:
            continue
        out.append("")
        out.append(title)
        out.append(hdr)
        out.append("-" * len(hdr))
        for r in group:
            d = f"{r['days_to_decision']:>5}" if r["days_to_decision"] is not None else "    ?"
            mark = {"crit": "!!", "warn": "! "}.get(r["urgency"], "  ")
            out.append(f"{mark}{r['decision_by'] or '—':<11}{d}  {r['effective_end'] or '—':<11}"
                       f"{r['hard_stop'] or '—':<11}{r['mailboxes']:>4}{r['monthly_cost']:>6}  "
                       f"{r['action']:<9}{r['client'][:40]}"
                       + (f"\n      ↳ {'; '.join(r['flags'])}" if r["flags"] else ""))
    if board["active_clients_without_infra"]:
        out.append("")
        out.append("Active CRM clients with no Zapmail infra matched: "
                   + ", ".join(board["active_clients_without_infra"]))
    return "\n".join(out)


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--as-of", help="run the board as if today were this date")
    p.add_argument("--json", action="store_true", help="emit the raw board")
    p.add_argument("--notices", action="store_true", help="show today's Slack touches")
    p.add_argument("--send", action="store_true", help="actually post them to Slack")
    p.add_argument("--client", help="show one client's cohorts in full")
    args = p.parse_args()

    try:
        import _envload
        _envload.load()
    except Exception:
        pass

    board = build(_parse(args.as_of))
    if args.json:
        print(json.dumps(board, indent=2))
    elif args.notices or args.send:
        res = post_notices(board, dry_run=not args.send)
        for m in res.get("messages", []):
            print(m + "\n")
        print(f"{res['count']} notice(s)"
              + (f", {res.get('sent')} sent" if not res["dry_run"] else " (dry run)"))
    elif args.client:
        want = norm_name(args.client)
        for r in board["rows"]:
            if want in norm_name(r["client"]):
                print(json.dumps(r, indent=2))
    else:
        print(_fmt(board))
