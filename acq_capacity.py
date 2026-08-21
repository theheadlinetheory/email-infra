"""Acquisition capacity — how much acquisition sending we own vs how much we use.

The question this answers (Tim, 2026-08-21): "we pay for N acquisition inboxes;
are they all actually pushing mail, and if not, which ones are free for Lars to
put in a new campaign?"

Why a dedicated module and not another view over health: health scores an inbox
on DELIVERABILITY (is it burning?). This scores it on UTILISATION (is it working
at all?). Those are different axes — a perfectly healthy inbox parked in a
completed campaign is a 15-sends-a-day hole in the budget that the health tab
deliberately calls "idle" and moves on from. Here it's the whole point.

The five capacity states, in the order they cost us money:

  sending     in >=1 ACTIVE campaign that still has leads queued. Working.
  stranded    in an ACTIVE campaign, but every ACTIVE campaign it's in has BOTH
              queues empty: no new leads AND no follow-ups due. Looks busy in
              SmartLead, sends nothing. A campaign does not release its senders
              when its work runs out.
              NOTE the "both queues" part. Testing only unsent NEW leads marks a
              campaign finished while it is still working through weeks of
              follow-ups — on 2026-08-22 that wrongly freed 40 senders across
              four campaigns, one of which had 5,528 leads mid-sequence.
  parked      only in PAUSED/COMPLETED campaigns. Idle, but at least visibly so.
  unassigned  in no campaign at all. Pure unallocated stock.
  blocked     cannot send regardless of allocation: SMTP disconnected, or burned.
              Counted OUT of usable capacity so utilisation is not flattered by
              inboxes that could never have sent.

Capacity is sum(message_per_day) read from SmartLead per inbox, NOT a hardcoded
15 — the rest of the repo assumes 15 and today every acquisition inbox is in
fact 15, but a per-inbox throttle change would silently invalidate every number
on the page, so we read the real value and fall back to 15 only when absent.

Actual usage comes from TRUE per-day rows (health_daily), reported on two bases:
per CALENDAR day, which is what we pay for, and per SENDING day, which is what a
working day looks like. They differ by a lot — Saturdays are zero and Sundays
trickle — and quoting only the calendar figure reads as ~35% spare headroom when
a weekday is actually running at 81%. See `usage()`.

Nothing here writes to the health scoring path.
"""

from __future__ import annotations

import json
import math
import time
from datetime import datetime

import db as store

SL = "https://server.smartlead.ai/api/v1"
SL_CAMPAIGN_URL = "https://app.smartlead.ai/app/email-campaign/{id}/analytics"

DEFAULT_PER_DAY = 15          # Zapmail/SmartLead default; used only when the real value is missing
USAGE_WINDOW_DAYS = 7         # how many complete days of true daily rows we average over
LIVE_DAY_FRACTION = 0.20      # a day counts as a sending day at >=20% of a median day
TARGET_BURN_DAYS = 7          # a campaign's queue "should" clear in about a week
MIN_SENDERS_ACTIVE = 1        # an ACTIVE campaign may never be taken below this

SENDING, STRANDED, PARKED, UNASSIGNED, BLOCKED = (
    "sending", "stranded", "parked", "unassigned", "blocked")

STATE_LABEL = {
    SENDING: "Sending", STRANDED: "Stranded", PARKED: "Parked",
    UNASSIGNED: "Unassigned", BLOCKED: "Blocked",
}
IDLE_STATES = (STRANDED, PARKED, UNASSIGNED)


def _sl_key() -> str:
    import os
    return (os.environ.get("SMARTLEAD_API_KEY", "") or os.environ.get("SMARTLEAD_KEY", "")).strip()


def _per_day(ad: dict) -> int:
    """Real per-inbox daily cap, falling back to the fleet default."""
    v = ad.get("message_per_day")
    try:
        v = int(v)
        return v if v > 0 else DEFAULT_PER_DAY
    except (TypeError, ValueError):
        return DEFAULT_PER_DAY


# --- inputs ---------------------------------------------------------------

def _acq_inboxes(overview: dict) -> list[dict]:
    """Every acquisition-tagged inbox, deduped by email, with its group name.

    Driven off `acquisition_groups` in the overview cache (the live tag state),
    NOT off the health fleet: the fleet's status table retains rows for inboxes
    that have since been retired or re-tagged, which would inflate the capacity
    denominator with mailboxes we no longer own.
    """
    seen, out = set(), []
    for g in overview.get("acquisition_groups") or []:
        for ad in g.get("account_details") or []:
            email = ad.get("email")
            if not email or email in seen:
                continue
            seen.add(email)
            out.append({
                "email": email,
                "account_id": ad.get("id"),
                "domain": ad.get("domain") or (email.split("@", 1)[-1]),
                "group": g.get("name") or "",
                "esp": (ad.get("esp") or "").upper() or None,
                "per_day": _per_day(ad),
                "smtp_ok": ad.get("smtp_ok") is not False,
                "sent_7d": int(ad.get("sent") or 0),
                "warmup_enabled": bool(ad.get("warmup_enabled")),
                "campaigns": list(ad.get("campaign_names") or []),
            })
    return out


def _campaign_facts(overview: dict, live: bool) -> dict:
    """{campaign name: {id, status, remaining, total_leads}} for every campaign
    an acquisition inbox sits in.

    `acq_campaign_stats` in the cache only covers campaigns whose NAME contains
    "acquisition", so it misses any acquisition inbox parked in a differently
    named campaign — those would otherwise be silently treated as "no campaign"
    and offered as free stock while still attached. The live campaign list fills
    in id+status for the rest; lead counts stay unknown (None) for them, which
    the caller treats as "assume it still has leads" rather than as zero.
    """
    facts: dict[str, dict] = {}
    for s in overview.get("acq_campaign_stats") or []:
        name = s.get("name")
        if not name:
            continue
        facts[name] = {
            "id": s.get("id"),
            "status": (s.get("status") or "").upper(),
            "remaining": s.get("remaining"),
            "in_progress": s.get("in_progress"),
            "total_leads": s.get("total_leads"),
            "senders_cached": s.get("accounts"),
        }
    if live:
        for name, info in (_live_campaign_index() or {}).items():
            row = facts.setdefault(name, {"remaining": None, "in_progress": None,
                                          "total_leads": None, "senders_cached": None})
            row["id"] = info["id"]
            row["status"] = info["status"]
        # refresh both lead queues for live acquisition campaigns — the stranded
        # call turns on these two numbers, so they must not be a day stale
        targets = {n: f["id"] for n, f in facts.items()
                   if f.get("status") == "ACTIVE" and f.get("id")
                   and is_acquisition_campaign(n)}
        for name, q in (_live_lead_queues(targets) or {}).items():
            facts[name].update({k: v for k, v in q.items() if v is not None})
    return facts


def _live_account_facts() -> dict:
    """{email: {message_per_day, esp, smtp_ok}} straight from SmartLead.

    `message_per_day` and `type` were only added to the overview cache on
    2026-08-21, so until a full sync has run the cache has neither and capacity
    silently falls back to 15/inbox for everything. This overlay (~17 paged calls,
    well inside the rate cap) makes the numbers real on the "Refresh live" path
    instead of making Lars wait for the nightly sync.
    """
    import requests
    key = _sl_key()
    if not key:
        return {}
    out, offset = {}, 0
    while True:
        try:
            r = requests.get(f"{SL}/email-accounts/",
                             params={"api_key": key, "offset": offset, "limit": 100},
                             timeout=30)
        except requests.RequestException:
            break
        if r.status_code != 200:
            break
        batch = r.json() if r.text.strip() else []
        if not isinstance(batch, list) or not batch:
            break
        for a in batch:
            email = a.get("from_email")
            if email:
                out[email] = {"message_per_day": a.get("message_per_day"),
                              "esp": a.get("type"),
                              "smtp_ok": bool(a.get("is_smtp_success"))}
        if len(batch) < 100:
            break
        offset += 100
    return out


def _live_lead_queues(names_to_ids: dict) -> dict:
    """{campaign name: {remaining, in_progress}} straight from SmartLead.

    `in_progress` was only added to the overview cache on 2026-08-22, so until a
    full sync runs the cache has no follow-up counts at all. Rather than let the
    tab fall back to "unknown" for every campaign — safe, but it would show zero
    stranded capacity and hide the whole point of the page — the live path asks
    SmartLead directly. Two cheap calls per campaign, and only for the ACTIVE
    acquisition ones, which is under a dozen.
    """
    import requests
    key = _sl_key()
    if not key:
        return {}
    out = {}
    for name, cid in names_to_ids.items():
        row = {}
        for status_key, field in (("STARTED", "remaining"), ("INPROGRESS", "in_progress")):
            try:
                r = requests.get(f"{SL}/campaigns/{cid}/leads",
                                 params={"api_key": key, "limit": 1, "offset": 0,
                                         "status": status_key}, timeout=20)
                row[field] = int((r.json() or {}).get("total_leads", 0)) if r.status_code == 200 else None
            except (requests.RequestException, ValueError):
                row[field] = None
        out[name] = row
    return out


def _live_campaign_index() -> dict:
    """One fetch of the whole campaign list -> {name: {id, status}}."""
    import requests
    key = _sl_key()
    if not key:
        return {}
    for _ in range(4):
        try:
            r = requests.get(f"{SL}/campaigns", params={"api_key": key}, timeout=60)
            if r.status_code == 200 and r.text.strip():
                return {c["name"]: {"id": c.get("id"),
                                    "status": (c.get("status") or "").upper()}
                        for c in (r.json() or []) if c.get("name")}
        except requests.RequestException:
            pass
        time.sleep(5)
    return {}


# --- classification -------------------------------------------------------

def _classify(inbox: dict, facts: dict) -> tuple:
    """(state, why[]) for one inbox. See the module docstring for the states."""
    if not inbox["smtp_ok"]:
        return BLOCKED, ["SMTP disconnected — cannot send"]
    if inbox.get("health") == "burned":
        return BLOCKED, ["burned — replace before reallocating"]

    camps = inbox["campaigns"]
    if not camps:
        return UNASSIGNED, ["in no campaign"]

    active = [c for c in camps if facts.get(c, {}).get("status") == "ACTIVE"]
    if not active:
        stat = {facts.get(c, {}).get("status") or "UNKNOWN" for c in camps}
        return PARKED, ["only in %s campaign(s)" % "/".join(sorted(stat)).lower()]

    # A campaign still has work for its senders if EITHER queue is non-empty:
    #
    #   STARTED    leads never contacted     -> first-touch emails still to send
    #   INPROGRESS leads mid-sequence        -> FOLLOW-UPS still to send
    #
    # Only STARTED was checked here originally, which was wrong in the most
    # damaging possible direction. Every one of the four campaigns this marked
    # "stranded" had thousands of leads mid-sequence and was demonstrably still
    # sending — AI-ark HVAC list 1 had 5,528 of them. Freeing those senders would
    # have cut live follow-up sequences off mid-flight, and because replies stay
    # welded to the mailbox that sent them, it would also have orphaned the
    # conversations they had already started.
    #
    # An unknown count means we could not measure that queue; treat it as
    # non-empty. Being wrong towards "still working" costs an idle inbox for a
    # day. Being wrong the other way breaks a running campaign.
    def _busy(c):
        f = facts.get(c, {})
        for k in ("remaining", "in_progress"):
            v = f.get(k, None)
            if v is None or (v or 0) > 0:
                return True
        return False

    working = [c for c in active if _busy(c)]
    if working:
        return SENDING, ["active in %d campaign(s) with leads or follow-ups queued"
                         % len(working)]
    return STRANDED, ["every active campaign it is in has no leads left AND no "
                      "follow-ups pending — sending nothing"]


# --- report ---------------------------------------------------------------

def usage(inboxes: list[dict]) -> dict:
    """Real send volume per day, from TRUE daily rows.

    Reported on two bases, because they answer different questions and quoting
    only one is misleading:

      per_calendar_day  total / every day in the window. The money question —
                        we pay for the mailboxes seven days a week.
      per_sending_day   total / the days we actually sent. The operational
                        question — what a working day really looks like.

    They are far apart, and the gap is the weekend: Saturdays are hard zero and
    Sundays trickle. Quoting only the calendar figure said 65% utilisation and
    implied ~35% headroom, when a weekday actually runs at 81% — barely 19%
    spare. Sending a Monday campaign into "headroom" that only exists on a
    Saturday is exactly the wrong move, so the tab shows both.

    Falls back to the overview cache's 7-day totals if daily rows aren't
    available yet (a fresh database, or before the first repaired snapshot).
    """
    emails = {i["email"] for i in inboxes}
    by_date = _daily_totals(emails)
    if by_date:
        vals = sorted(v for v in by_date.values() if v > 0)
        median = vals[len(vals) // 2] if vals else 0
        sending = {d: v for d, v in by_date.items() if v >= median * LIVE_DAY_FRACTION}
        total = sum(by_date.values())
        return {
            "source": "daily",
            "window_days": len(by_date),
            "sending_days": len(sending),
            "sent_window": total,
            "per_calendar_day": round(total / len(by_date)),
            "per_sending_day": round(sum(sending.values()) / len(sending)) if sending else 0,
            "per_day": round(total / len(by_date)),      # kept for callers
            "by_date": dict(sorted(by_date.items())),
        }
    sent_7d = sum(i["sent_7d"] for i in inboxes)
    return {
        "source": "overview_7d",
        "window_days": USAGE_WINDOW_DAYS,
        "sending_days": None,
        "sent_window": sent_7d,
        "per_calendar_day": round(sent_7d / USAGE_WINDOW_DAYS),
        "per_sending_day": None,
        "per_day": round(sent_7d / USAGE_WINDOW_DAYS),
        "by_date": {},
    }


def _daily_totals(emails: set, days: int = USAGE_WINDOW_DAYS) -> dict:
    """{date: sends} over the last `days` COMPLETE days, from inbox_health_daily.

    Complete days only: today is still filling up, and averaging a part-day in
    with whole ones drags the rate down for no reason other than the hour the
    page was opened.
    """
    try:
        import health_daily as hd
        wanted = set(hd.complete_days(days))
    except Exception:
        return {}
    # published by the snapshot — one row instead of ~12,000
    cached = hd.cached_daily_sends("acquisition")
    hit = {d: n for d, n in cached.items() if d in wanted}
    if len(hit) == len(wanted):
        return hit
    try:
        rows = store.get_health_daily_sends(min(wanted))
    except Exception:
        return hit                              # partial beats nothing
    out: dict = {}
    for r in rows:
        d = r.get("date")
        if d in wanted and r.get("email") in emails:
            out[d] = out.get(d, 0) + int(r.get("sent") or 0)
    return out


def _health_by_email() -> dict:
    try:
        return {r["email"]: r for r in (store.get_health_status_all() or [])}
    except Exception:
        return {}


def _state_order(s: str) -> int:
    return {STRANDED: 0, UNASSIGNED: 1, PARKED: 2, SENDING: 3, BLOCKED: 4}.get(s, 9)


def build(live: bool = False, live_accounts: bool | None = None) -> dict:
    """The whole acquisition-capacity picture. Read-only.

    `live` re-pulls campaign statuses (one call) — cheap, and what every
    correctness decision downstream depends on. `live_accounts` additionally
    re-pulls every mailbox for its real cap/provider (~17 paged calls, several
    seconds); it defaults to `live` for the page's own refresh button but the
    allocation planner turns it off, since it only needs campaign truth and
    would otherwise make "Preview move" take twice as long for no benefit.
    """
    overview, ts = store.cache_get("overview_v2")
    if not overview:
        return {"error": "overview_v2 cache empty — run a sync first"}

    inboxes = _acq_inboxes(overview)
    if not inboxes:
        return {"error": "no acquisition-tagged inboxes in the overview cache"}

    if live if live_accounts is None else live_accounts:
        facts_live = _live_account_facts()
        for i in inboxes:
            f = facts_live.get(i["email"])
            if not f:
                continue
            i["per_day"] = _per_day(f)
            i["esp"] = (f.get("esp") or "").upper() or None
            i["smtp_ok"] = f["smtp_ok"]

    health = _health_by_email()
    for i in inboxes:
        h = health.get(i["email"]) or {}
        i["health"] = h.get("status")
        i["score"] = h.get("score")
        i["bounce_3d"] = h.get("bounce_3d")
        i["reply_3d"] = h.get("reply_3d")

    facts = _campaign_facts(overview, live)
    for i in inboxes:
        i["state"], i["why"] = _classify(i, facts)
        i["active_campaigns"] = [c for c in i["campaigns"]
                                 if facts.get(c, {}).get("status") == "ACTIVE"]

    # -- capacity roll-up --
    by_state = {s: {"inboxes": 0, "capacity": 0} for s in STATE_LABEL}
    for i in inboxes:
        b = by_state[i["state"]]
        b["inboxes"] += 1
        b["capacity"] += i["per_day"]

    total_capacity = sum(i["per_day"] for i in inboxes)
    blocked_capacity = by_state[BLOCKED]["capacity"]
    usable_capacity = total_capacity - blocked_capacity
    deployed = by_state[SENDING]["capacity"]
    idle_capacity = sum(by_state[s]["capacity"] for s in IDLE_STATES)
    use = usage(inboxes)

    summary = {
        "inboxes": len(inboxes),
        "total_capacity": total_capacity,
        "usable_capacity": usable_capacity,
        "blocked_capacity": blocked_capacity,
        "blocked_inboxes": by_state[BLOCKED]["inboxes"],
        "deployed_capacity": deployed,
        "idle_capacity": idle_capacity,
        "idle_inboxes": sum(by_state[s]["inboxes"] for s in IDLE_STATES),
        "actual_per_day": use["per_calendar_day"],
        "actual_per_sending_day": use["per_sending_day"],
        "sent_window": use["sent_window"],
        "window_days": use["window_days"],
        "sending_days": use["sending_days"],
        "usage_source": use["source"],
        "sent_by_date": use["by_date"],
        # Two different questions, deliberately kept apart:
        #  utilisation = sends actually leaving / EVERYTHING we pay for. This is
        #    the money question ("are we using what we buy"), so its denominator
        #    is total capacity — blocked inboxes are billed too and dragging it
        #    down is the correct signal.
        #  allocation  = capacity pointed at a live queue / capacity that CAN
        #    send. This is the fixable-by-reallocation number, so blocked inboxes
        #    are out of both sides of it.
        # Measuring utilisation against `usable` instead would mix the two bases
        # and can print >100%, since the 7-day send window includes days when
        # now-stranded campaigns still had leads.
        "utilisation_pct": round(100 * use["per_calendar_day"] / total_capacity) if total_capacity else 0,
        # what a WORKING day looks like — the number to judge real headroom by
        "sending_day_pct": (round(100 * use["per_sending_day"] / total_capacity)
                            if total_capacity and use["per_sending_day"] else None),
        "allocation_pct": round(100 * deployed / usable_capacity) if usable_capacity else 0,
        "by_state": by_state,
    }

    campaigns = _campaign_rows(inboxes, facts)
    summary["starved_senders"] = sum(c["wants_senders"] for c in campaigns)
    summary["starved_capacity"] = summary["starved_senders"] * DEFAULT_PER_DAY

    return {
        "generated_at": datetime.now().isoformat(),
        "synced_at": ts,
        "live": bool(live),
        "summary": summary,
        "inboxes": sorted(inboxes, key=lambda x: (_state_order(x["state"]), x["email"])),
        "campaigns": campaigns,
        "state_labels": STATE_LABEL,
    }


def is_acquisition_campaign(name: str) -> bool:
    """THT's naming convention: every acquisition campaign carries "acquisition"
    in its name (and subsequences are excluded everywhere else in the repo, so
    they are here too). Used only to decide which EMPTY live campaigns are worth
    offering as allocation targets — a campaign that already holds acquisition
    senders is listed regardless of what it is called, because pretending it is
    not there would hide senders we own."""
    n = (name or "").lower()
    return "acquisition" in n and "subsequence" not in n


def _campaign_rows(inboxes: list[dict], facts: dict) -> list[dict]:
    """One row per campaign that holds acquisition senders, plus every empty
    ACTIVE *acquisition* campaign (an empty live campaign is exactly what Lars
    needs to see in order to fill it).

    Client campaigns are deliberately excluded unless they already hold an
    acquisition inbox — offering a client campaign as a target for acquisition
    senders is how cross-contamination starts, and an empty one is never
    something this tab should be inviting a click on.
    """
    holders: dict[str, list] = {}
    for i in inboxes:
        for c in i["campaigns"]:
            holders.setdefault(c, []).append(i)

    names = set(holders) | {n for n, f in facts.items()
                            if f.get("status") == "ACTIVE" and is_acquisition_campaign(n)}
    rows = []
    for name in names:
        f = facts.get(name, {})
        mine = holders.get(name, [])
        usable = [i for i in mine if i["state"] != BLOCKED]
        capacity = sum(i["per_day"] for i in usable)
        remaining = f.get("remaining")
        in_progress = f.get("in_progress")
        status = f.get("status") or "UNKNOWN"
        # "still working" is either queue being non-empty (or unmeasured)
        followups_only = (status == "ACTIVE" and remaining == 0
                          and (in_progress or 0) > 0)
        idle_campaign = (status == "ACTIVE" and remaining == 0
                         and in_progress is not None and in_progress == 0)

        runway = None
        if remaining is not None and capacity > 0:
            runway = round(remaining / capacity, 1)

        # How many more senders would clear the queue inside TARGET_BURN_DAYS.
        # Only meaningful for a live campaign that still has leads.
        wants = 0
        if status == "ACTIVE" and remaining:
            need_cap = math.ceil(remaining / TARGET_BURN_DAYS)
            wants = max(0, math.ceil((need_cap - capacity) / DEFAULT_PER_DAY))

        rows.append({
            "name": name,
            "id": f.get("id"),
            "url": SL_CAMPAIGN_URL.format(id=f["id"]) if f.get("id") else None,
            "status": status,
            "acquisition": is_acquisition_campaign(name),
            "senders": len(mine),
            # senders that can actually send — the rest are burned/SMTP-down and
            # contribute nothing, which is why capacity != senders * per_day
            "senders_usable": len(usable),
            "senders_blocked": len(mine) - len(usable),
            "capacity": capacity,
            "remaining": remaining,
            "in_progress": in_progress,
            "followups_only": followups_only,
            "total_leads": f.get("total_leads"),
            "runway_days": runway,
            "wants_senders": wants,
            # Only a campaign with BOTH queues empty is holding senders for
            # nothing. One that is out of new leads but still owes follow-ups is
            # working, and its senders must not be offered up.
            "stranding": idle_campaign and len(mine) > 0,
            "emails": [i["email"] for i in mine],
        })
    rows.sort(key=lambda r: (r["status"] != "ACTIVE", -(r["wants_senders"] or 0),
                             -(r["remaining"] or 0), r["name"]))
    return rows


# --- allocation -----------------------------------------------------------

def plan(emails: list[str], to_campaign_id=None, from_campaign_id=None,
         override_active: bool = False) -> dict:
    """Dry-run an allocation: what would be added where, what would be removed,
    and every rail that would stop it. Never touches campaign membership."""
    rep = build(live=True, live_accounts=False)
    if rep.get("error"):
        return rep
    by_email = {i["email"]: i for i in rep["inboxes"]}
    by_name = {c["name"]: c for c in rep["campaigns"]}
    by_cid = {c["id"]: c for c in rep["campaigns"] if c.get("id")}

    target = by_cid.get(to_campaign_id) if to_campaign_id else None
    source = by_cid.get(from_campaign_id) if from_campaign_id else None
    if to_campaign_id and not target:
        # a brand-new campaign Lars just made holds no acq senders yet, so it is
        # absent from the report's campaign rows — resolve it from SmartLead
        for n, info in (_live_campaign_index() or {}).items():
            if info["id"] == to_campaign_id:
                target = {"name": n, "id": info["id"], "status": info["status"],
                          "senders": 0, "capacity": 0, "remaining": None, "emails": []}
                break
    if to_campaign_id and not target:
        return {"error": "campaign %s not found in SmartLead" % to_campaign_id}
    if from_campaign_id and not source:
        return {"error": "campaign %s not found" % from_campaign_id}

    adds, removes, blocked, warnings = [], [], [], []

    if target and not is_acquisition_campaign(target["name"]):
        warnings.append("'%s' does not look like an acquisition campaign — putting "
                        "acquisition inboxes on a client campaign mixes the two fleets."
                        % target["name"])
    if target and target["status"] != "ACTIVE":
        warnings.append("'%s' is %s — senders added to it will not start sending until "
                        "you start the campaign in SmartLead."
                        % (target["name"], target["status"]))

    for email in emails:
        i = by_email.get(email)
        if not i:
            blocked.append({"email": email, "reason": "not an acquisition inbox"})
            continue
        if not i.get("account_id"):
            blocked.append({"email": email, "reason": "no SmartLead account id"})
            continue
        if i["state"] == BLOCKED:
            blocked.append({"email": email, "reason": "; ".join(i["why"])})
            continue

        # which campaigns it would leave
        if from_campaign_id:
            if email not in (source.get("emails") or []):
                blocked.append({"email": email,
                                "reason": "not a sender on %s" % source["name"]})
                continue
            leaving = [source]
        else:
            # default: detach it from every campaign it is parked in, so a resumed
            # campaign can never make it send from two places at once
            leaving = [by_name[c] for c in i["campaigns"]
                       if c in by_name and by_name[c].get("id")
                       and by_name[c]["id"] != to_campaign_id]

        stop = None
        for c in leaving:
            if c["status"] == "ACTIVE":
                # Pending FOLLOW-UPS make a campaign live just as much as unsent
                # new leads do. Checking only `remaining` would have waved through
                # the removal of senders from campaigns owing thousands of
                # follow-ups, which is the exact case this rail exists for.
                new_left = c["remaining"] is None or (c["remaining"] or 0) > 0
                fu_left = c.get("in_progress") is None or (c.get("in_progress") or 0) > 0
                if (new_left or fu_left) and not override_active:
                    what = ("%s new leads" % c["remaining"]) if new_left else ""
                    if fu_left:
                        what = (what + " and " if what else "") +                                ("%s leads mid-sequence still owed follow-ups"
                                % c.get("in_progress"))
                    stop = ("would pull a sender off live campaign '%s' (%s) — tick "
                            "'allow pulling from live campaigns' to override"
                            % (c["name"], what))
                    break
        if stop:
            blocked.append({"email": email, "reason": stop})
            continue

        if to_campaign_id:
            if email in (target.get("emails") or []):
                warnings.append("%s is already a sender on %s" % (email, target["name"]))
            else:
                adds.append({"email": email, "account_id": i["account_id"],
                             "per_day": i["per_day"]})
        for c in leaving:
            removes.append({"email": email, "account_id": i["account_id"],
                            "campaign": c["name"], "campaign_id": c["id"],
                            "status": c["status"], "per_day": i["per_day"]})

    # -- rail: never take a live campaign to zero senders --
    drain: dict = {}
    for r in removes:
        if r["status"] == "ACTIVE":
            drain[r["campaign"]] = drain.get(r["campaign"], 0) + 1
    fatal = []
    for cname, n in drain.items():
        row = by_name.get(cname)
        left = (row["senders"] if row else 0) - n
        if left < MIN_SENDERS_ACTIVE:
            fatal.append("'%s' would be left with %d sender(s) — an ACTIVE campaign must "
                         "keep at least %d. Pause it in SmartLead first, or deselect "
                         "some inboxes." % (cname, left, MIN_SENDERS_ACTIVE))
        elif left < 3:
            warnings.append("'%s' would drop to %d senders (%d/day) — check that is "
                            "intended." % (cname, left, left * DEFAULT_PER_DAY))

    if removes:
        warnings.append("Replies already received stay welded to the mailbox that sent "
                        "them — moving a sender does not move its inbox conversations.")

    gained = sum(a["per_day"] for a in adds)
    return {
        "ok": not fatal,
        # `ok` means "no safety rail tripped" — a plan can be ok and still do
        # nothing (every inbox individually blocked), so the UI gates the confirm
        # button on `actionable`, not on `ok`.
        "actionable": bool(adds or removes) and not fatal,
        "dry_run": True,
        "target": {"name": target["name"], "id": target["id"], "status": target["status"],
                   "senders_now": target["senders"], "capacity_now": target["capacity"],
                   "senders_after": target["senders"] + len(adds),
                   "capacity_after": target["capacity"] + gained} if target else None,
        "adds": adds, "removes": removes, "blocked": blocked,
        "warnings": warnings, "fatal": fatal,
        "capacity_moved": gained,
    }


def apply(emails: list[str], to_campaign_id=None, from_campaign_id=None,
          override_active: bool = False) -> dict:
    """Execute the plan. Add-before-remove so a campaign never dips below capacity
    mid-move (same convention as health_replace.swap_campaign_membership).
    Refuses outright if the plan trips any fatal rail."""
    import requests

    p = plan(emails, to_campaign_id, from_campaign_id, override_active)
    if p.get("error"):
        return p
    if p.get("fatal"):
        return {"ok": False, "error": "blocked by safety rails",
                "fatal": p["fatal"], "plan": p}
    if not p["adds"] and not p["removes"]:
        return {"ok": False, "error": "nothing to do", "plan": p}

    key = _sl_key()
    if not key:
        return {"ok": False, "error": "SMARTLEAD_API_KEY not configured"}

    def _call(method, cid, account_ids):
        url = "%s/campaigns/%s/email-accounts" % (SL, cid)
        for _ in range(4):
            r = requests.request(method, url, params={"api_key": key},
                                 json={"email_account_ids": account_ids}, timeout=60)
            if r.status_code != 429:
                return r.status_code
            time.sleep(20)
        return 429

    results = {"added": 0, "removed": 0, "add_failed": [], "remove_failed": []}
    touched_active = set()

    # 1) ADD first — one call for the whole set
    if p["adds"]:
        ids = [a["account_id"] for a in p["adds"]]
        code = _call("POST", to_campaign_id, ids)
        if code == 200:
            results["added"] = len(ids)
            if p["target"] and p["target"]["status"] == "ACTIVE":
                touched_active.add(p["target"]["name"])
        else:
            results["add_failed"] = [{"emails": [a["email"] for a in p["adds"]],
                                      "http": code}]
            # Do NOT proceed to removals when the add failed — that is exactly how
            # a campaign ends up drained with nothing put back in its place.
            return {"ok": False,
                    "error": "SmartLead rejected the add (%s); nothing was removed" % code,
                    "results": results, "plan": p}

    # 2) REMOVE, grouped per source campaign
    per_campaign: dict = {}
    for r in p["removes"]:
        per_campaign.setdefault((r["campaign_id"], r["campaign"], r["status"]), []).append(r)
    for (cid, cname, cstatus), rows in per_campaign.items():
        ids = [r["account_id"] for r in rows]
        code = _call("DELETE", cid, ids)
        if code == 200:
            results["removed"] += len(ids)
            if cstatus == "ACTIVE":
                touched_active.add(cname)
        else:
            results["remove_failed"].append({"campaign": cname, "http": code,
                                             "emails": [r["email"] for r in rows]})

    # Any ACTIVE campaign whose sender set changed needs SmartLead's manual
    # "Reallocate mailboxes" click to redistribute its lead queue — no API for it.
    if touched_active:
        try:
            import health_replace as hr
            hr._add_pending_reallocate(sorted(touched_active))
        except Exception:
            pass

    _patch_cache(p)

    try:
        store.log_monitor_event("acq_allocate", {
            "emails": emails, "to": to_campaign_id, "from": from_campaign_id,
            "override_active": override_active, "results": results})
    except Exception:
        pass

    ok = not results["add_failed"] and not results["remove_failed"]
    msg = "Added %d, removed %d." % (results["added"], results["removed"])
    if touched_active:
        msg += (" Hit 'Reallocate mailboxes' in SmartLead for: "
                + ", ".join(sorted(touched_active)) + ".")
    return {"ok": ok, "results": results, "message": msg,
            "capacity_moved": p["capacity_moved"],
            "reallocate_needed": sorted(touched_active),
            "warnings": p["warnings"]}


def _patch_cache(p: dict) -> None:
    """Reflect the move in overview_v2 so the tab is correct before the next sync.

    Without this the page re-reads a stale cache and shows the inbox still idle
    (or still on the old campaign), which reads as "the click did nothing" and
    invites a second, duplicate move.
    """
    try:
        ov, _ = store.cache_get("overview_v2")
        if not ov:
            return
        target_name = (p.get("target") or {}).get("name")
        add_emails = set(a["email"] for a in p.get("adds") or [])
        drop: dict = {}
        for r in p.get("removes") or []:
            drop.setdefault(r["email"], set()).add(r["campaign"])

        for g in ov.get("acquisition_groups") or []:
            for ad in g.get("account_details") or []:
                email = ad.get("email")
                names = list(ad.get("campaign_names") or [])
                if email in drop:
                    names = [n for n in names if n not in drop[email]]
                if email in add_emails and target_name and target_name not in names:
                    names.append(target_name)
                ad["campaign_names"] = names
                ad["in_campaign"] = bool(names)
        store.cache_patch("overview_v2", ov)
    except Exception:
        pass


if __name__ == "__main__":
    rep = build(live=True)
    if rep.get("error"):
        raise SystemExit(rep["error"])
    s = rep["summary"]
    print(json.dumps({k: v for k, v in s.items() if k != "by_state"}, indent=2))
    for st, b in s["by_state"].items():
        print("  %-11s %4d inboxes  %6d/day" % (st, b["inboxes"], b["capacity"]))
    print()
    for c in rep["campaigns"][:14]:
        print("  [%-9s] senders=%3d cap=%5d rem=%s runway=%s wants=%s  %s"
              % (c["status"], c["senders"], c["capacity"], c["remaining"],
                 c["runway_days"], c["wants_senders"], c["name"][:52]))
