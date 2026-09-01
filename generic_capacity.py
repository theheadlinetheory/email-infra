"""Generic inbox capacity — how much unbranded sending stock is actually free.

THT buys unbranded domains (`landscapingcarecrew.info`, `yardcarepath.co`,
`christmaslightpros.info`), warms them, and points them at whichever client
needs volume. That stock is the constraint on taking new work, so the question
"how many generic inboxes can I use on Monday" has to have a number.

The trap this module exists to avoid is counting an inbox as free because a
STATUS FIELD looks quiet. Three separate things all look like "idle" and only
one of them is capacity you can actually deploy:

  * a mailbox mid-sequence, in the gap between its sends -> BUSY, not free
  * a mailbox 6 days into a 14-day warmup                 -> NOT YET, not free
  * a mailbox held for a burned-inbox replacement         -> SPOKEN FOR
  * a mailbox in nothing but finished/paused campaigns    -> genuinely free

So every inbox is classified on campaign state and then CHECKED AGAINST TRUE
SEND ROWS. Where the two disagree — the state says free but the mailbox has
been sending — the send data wins and the inbox is reported as `disputed`
rather than quietly added to the headline. Being wrong towards "busy" costs an
idle mailbox for a day; being wrong the other way cuts a live sequence off
mid-flight, and replies stay welded to the mailbox that sent them.

Read-only. Nothing here writes to SmartLead, the health tables, or the cache.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone

import db as store

# A new mailbox is not capacity until it has warmed. Same 14 days the
# replacement pipeline uses (health_replace.WARMUP_DAYS) — deliberately the
# same constant so the two pages can never disagree about what "ready" means.
WARMUP_DAYS = 14
DEFAULT_PER_DAY = 15
USAGE_WINDOW_DAYS = 14        # calendar days pulled; weekends get filtered out below
MIN_SENDING_DAY = 2000        # fleet-wide sends that make a date a real working day

# --- verticals ------------------------------------------------------------
# Classified off the DOMAIN, not the tag: the group tag is only a letter
# ("Generic V"), and the same pool gets re-tagged into a client group the
# moment it is allocated, so the tag stops being a reliable label for what the
# inbox actually looks like to a prospect.
#
# `holiday` is tested FIRST and deliberately does not contain a bare "light":
# "Lightning Group" is a landscaping client, and `lightning` contains `light`.
_HOLIDAY = re.compile(
    r"holiday|christmas|xmas|festive|twinkle|garland|wreath|santa|illuminate|"
    r"merrylight|seasonallight|deckthelight|lightupholiday|glowand|brightholiday", re.I)
_HVAC = re.compile(
    r"hvac|heating|cooling|furnace|refrigerat|mechanical|climate|comfort|"
    r"aircon|conditioning|heatpump", re.I)
_LAND = re.compile(
    r"lawn|landscap|landcare|yard|turf|grounds|mow|garden|outdoor|scape|"
    r"irrigation|hardscape|planting|greenery|nursery|treecare", re.I)

VERTICALS = ("landscaping", "service", "holiday")

# Tags that are bookkeeping, not clients. Stock sitting under these is NOT
# spare capacity: "Retired - burned" is exactly the inboxes we took out of
# service, and re-deploying them is the one thing the health pipeline is
# trying to prevent.
NON_CLIENT_TAGS = {"retired - burned", "burnt acquisition", "__untagged__"}


def niche(domain_or_email: str) -> str:
    """'holiday' | 'hvac' | 'landscaping' | 'service' for one domain.

    'service' is the residual — neutral trade branding (`jobcrew.info`,
    `dispatchworkops.co`) that fits any vertical, which is what makes it the
    most flexible stock we hold and worth counting separately.
    """
    d = (domain_or_email or "").lower()
    if "@" in d:
        d = d.split("@")[-1]
    if _HOLIDAY.search(d):
        return "holiday"
    if _HVAC.search(d):
        return "hvac"
    if _LAND.search(d):
        return "landscaping"
    return "service"


# --- states ---------------------------------------------------------------
SENDING, WARMING, CLAIMED, BLOCKED, FREE, RELEASED, DISPUTED = (
    "sending", "warming", "claimed", "blocked", "free", "released", "disputed")

STATE_LABEL = {
    SENDING:  "Sending",
    WARMING:  "Warming up",
    CLAIMED:  "Held for a replacement",
    BLOCKED:  "Blocked",
    FREE:     "Free — in no campaign",
    RELEASED: "Free — only finished/paused campaigns",
    DISPUTED: "Looks free but is still sending",
}
# The two states that are genuinely deployable capacity.
AVAILABLE_STATES = (FREE, RELEASED)


# --- inputs ---------------------------------------------------------------
#
# Deliberately LIVE, not cache-backed. The overview cache is rebuilt by a
# nightly sync, and when that sync stops running the cache does not look broken
# — it looks like a smaller fleet. Read on 2026-09-02 it was 14 days old and
# contained NO holiday stock at all (the pool was bought on 2026-08-27), so a
# cache-backed version of this page would have reported "0 holiday inboxes" with
# a straight face while 42 sat warming. A capacity number that silently decays
# is worse than no capacity number, so the roster is pulled from SmartLead.

SL = "https://server.smartlead.ai/api/v1"


def _sl_key() -> str:
    import os
    return (os.environ.get("SMARTLEAD_API_KEY", "")
            or os.environ.get("SMARTLEAD_KEY", "")).strip()


def _tag_name(t: dict) -> str:
    """SmartLead spells this two ways: `tag_name` from the REST account list,
    `name` from the GraphQL tag mapping sync uses."""
    return (t.get("tag_name") or t.get("name") or "").strip()


def group_tag(account: dict) -> str:
    """The account's group tag — the one that is not Zapmail and not a date."""
    for t in account.get("tags") or []:
        n = _tag_name(t)
        if not n or n.lower() in ("zapmail", "premium inboxes"):
            continue
        if re.match(r"^\d{1,2}/\d{1,2}/\d{2,4}$", n):
            continue
        return n
    return "__untagged__"


def _live_accounts() -> list[dict]:
    """Every mailbox with its tags, straight from SmartLead (~18 paged calls)."""
    import sync
    accounts = sync.fetch_all_accounts()
    if not accounts:
        return []
    # The REST list usually carries `tags` inline; when it does not, fall back to
    # the GraphQL mapping. Skipping the fallback when it is not needed saves
    # several seconds on every page load.
    if not any(a.get("tags") for a in accounts):
        tag_map = sync.fetch_tag_mappings()
        for a in accounts:
            a["tags"] = tag_map.get(a.get("id"), [])
    return accounts


def _emails_in_active_campaigns() -> tuple:
    """(emails attached to an ACTIVE campaign, {email: [campaign names]}).

    Only ACTIVE campaigns are fetched, and that is the whole trick that makes
    this affordable: whether an inbox is busy depends solely on whether some
    ACTIVE campaign is holding it, so the 300-odd finished and paused campaigns
    need not be asked about at all. Roughly 45 calls instead of 369.

    Returns `(None, {})` if the campaign list itself could not be read — the
    caller must then refuse to report, because an empty attachment map would
    make the entire fleet look free.
    """
    import requests
    key = _sl_key()
    if not key:
        return None, {}
    try:
        r = requests.get(f"{SL}/campaigns", params={"api_key": key}, timeout=60)
        if r.status_code != 200 or not r.text.strip():
            return None, {}
        campaigns = r.json() or []
    except (requests.RequestException, ValueError):
        return None, {}

    active = [c for c in campaigns if (c.get("status") or "").upper() == "ACTIVE"]
    emails, by_email = set(), {}
    from concurrent.futures import ThreadPoolExecutor
    session = requests.Session()

    def one(c):
        for attempt in range(3):
            try:
                rr = session.get(f"{SL}/campaigns/{c['id']}/email-accounts",
                                 params={"api_key": key}, timeout=30)
            except requests.RequestException:
                time.sleep(2 * (attempt + 1))
                continue
            if rr.status_code == 200:
                try:
                    return c, (rr.json() or [])
                except ValueError:
                    return c, []
            if rr.status_code in (429, 500, 502, 503):
                time.sleep(5 * (attempt + 1))
                continue
            break
        return c, None                     # unreadable — treated as busy below

    with ThreadPoolExecutor(max_workers=6) as ex:
        for c, rows in ex.map(one, active):
            if rows is None:
                # Could not read this campaign. Its senders must NOT fall through
                # to "free" on a network error, so mark the whole campaign
                # unreadable and let the caller keep its inboxes out of the
                # available pool.
                by_email.setdefault("__unreadable__", []).append(c.get("name"))
                continue
            for a in rows:
                e = a.get("from_email")
                if not e:
                    continue
                emails.add(e)
                by_email.setdefault(e, []).append(c.get("name"))
    return emails, by_email


def _warmup_age_days(a: dict) -> int | None:
    """Days since the mailbox started warming, or None if undatable.

    `created_at` is the honest clock: THT enables warmup at creation, and the
    warmup record itself is recreated when a mailbox is re-pointed, which would
    reset the age of a mailbox that has actually been warm for months.
    """
    raw = a.get("created_at") or (a.get("warmup_details") or {}).get("warmup_created_at")
    if not raw:
        return None
    try:
        started = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, (datetime.now(timezone.utc) - started).days)


def _roster(accounts: list[dict]) -> list[dict]:
    """Every non-acquisition mailbox we hold, tagged with vertical and owner.

    Both halves of the question live here and are counted the same way:
      `Generic X`   -> stock in no client's hands
      a client tag  -> stock already allocated, which is still ours to re-point
                       if that client is not sending with it

    Acquisition inboxes are excluded: they are THT's own prospecting fleet and
    `acq_capacity` reports them. Counting them here would double-count them.
    """
    out = []
    for a in accounts:
        email = a.get("from_email")
        if not email:
            continue
        tag = group_tag(a)
        if tag.lower().startswith("acquisition"):
            continue
        try:
            cap = int(a.get("message_per_day") or 0) or DEFAULT_PER_DAY
        except (TypeError, ValueError):
            cap = DEFAULT_PER_DAY
        is_generic = tag.lower().startswith("generic")
        out.append({
            "email": email,
            "account_id": a.get("id"),
            "domain": email.split("@", 1)[-1],
            "tag": tag,
            "owner": None if is_generic else tag,
            "niche": niche(email),
            "per_day": cap,
            "smtp_ok": a.get("is_smtp_success") is not False,
            "age_days": _warmup_age_days(a),
            "esp": (a.get("type") or "").upper() or None,
        })
    return out


def _claimed_emails() -> set:
    """Inboxes a burned-inbox replacement job has already spoken for.

    These sit in their generic group until the next sync re-tags them, so
    without this they would be offered twice — once as replacement reserve on
    the Renewals tab and once as free capacity here. Only `reserved` counts:
    a `swapped` job has already moved its inbox into the client group, where it
    is either sending or idle on its own merits.
    """
    try:
        import health_replace as hr
        return {j.get("reserve_email") for j in hr._load().get("jobs", [])
                if j.get("reserve_email") and j.get("status") == "reserved"}
    except Exception:
        return set()


def _burned_emails() -> set:
    try:
        return {r["email"] for r in (store.get_health_status_all() or [])
                if (r.get("status") or "") == "burned"}
    except Exception:
        return set()


def _sends_by_email(days: int = USAGE_WINDOW_DAYS) -> tuple:
    """({email: sends}, [real sending dates]) over the last complete days.

    Weekends are dropped, not averaged in. THT sends nothing on Saturday and
    trickles on Sunday, so dividing by calendar days would understate every
    mailbox by ~30% and make a busy inbox look part-idle. A date counts only if
    the whole fleet cleared MIN_SENDING_DAY on it.
    """
    try:
        import health_daily as hd
        wanted = set(hd.complete_days(days))
    except Exception:
        return {}, []
    try:
        rows = store.get_health_daily_sends(min(wanted))
    except Exception:
        return {}, []
    per_date, per_email = {}, {}
    for r in rows:
        d = r.get("date")
        if d not in wanted:
            continue
        n = int(r.get("sent") or 0)
        per_date[d] = per_date.get(d, 0) + n
        e = r.get("email")
        if e:
            per_email.setdefault(d, {})[e] = per_email.get(d, {}).get(e, 0) + n
    live_dates = sorted(d for d, n in per_date.items() if n >= MIN_SENDING_DAY)
    totals = {}
    for d in live_dates:
        for e, n in (per_email.get(d) or {}).items():
            totals[e] = totals.get(e, 0) + n
    return totals, live_dates


def _classify(inbox: dict, active_emails: set, unreadable: bool,
              claimed: set, burned: set) -> tuple:
    """(state, why) for one inbox, most-disqualifying test first."""
    if not inbox["smtp_ok"]:
        return BLOCKED, "SMTP disconnected — cannot send"
    if inbox["email"] in burned:
        return BLOCKED, "burned — must be replaced, not redeployed"
    if str(inbox["tag"]).strip().lower() in NON_CLIENT_TAGS:
        return BLOCKED, "retired / bookkeeping tag — deliberately out of service"

    age = inbox.get("age_days")
    if age is not None and age < WARMUP_DAYS:
        return WARMING, f"{WARMUP_DAYS - age} more day(s) of warmup"

    if inbox["email"] in claimed:
        return CLAIMED, "reserved for a burned-inbox replacement"

    # In an ACTIVE campaign -> busy, full stop. The lead queues are NOT consulted:
    # a campaign whose first-touch queue is empty is still shipping the follow-ups
    # already in flight, and pulling a sender out mid-sequence both cuts those off
    # and orphans the replies it has already earned — replies live in the mailbox
    # that sent them. This is the "between sends" case the tracker must never
    # count as spare, and it is why an inbox in a live campaign is busy even on a
    # day it happens to send nothing.
    if inbox["email"] in active_emails:
        return SENDING, "attached to a live campaign"

    if unreadable:
        # We could not read every active campaign, so "not in active_emails" is
        # not proof of anything. Refuse to call it free.
        return DISPUTED, "could not read every active campaign — not counted as free"

    # Reads free on campaign state. The true send rows get the last word: an
    # inbox that has been sending is working for someone regardless of what the
    # campaign list says.
    if inbox["sent_window"] > 0:
        return DISPUTED, ("in no active campaign, yet sent %d email(s) in the "
                          "measured window" % inbox["sent_window"])
    if not inbox["ever_in_campaign"]:
        return FREE, "in no campaign at all"
    return RELEASED, "only in finished/paused campaigns, and sending nothing"


def _state_order(s: str) -> int:
    return {FREE: 0, RELEASED: 1, WARMING: 2, CLAIMED: 3,
            DISPUTED: 4, SENDING: 5, BLOCKED: 6}.get(s, 9)


def _ever_in_campaign() -> set:
    """Emails that have ever been attached to any campaign, from the cache.

    Only used to split "never used" from "finished with", both of which are
    already counted as available — so cache staleness here changes a label, not
    a number, and is not worth 300 extra API calls.
    """
    ov, _ = store.cache_get("overview_v2")
    out = set()
    for bucket in ("generic_groups", "clients"):
        for g in (ov or {}).get(bucket) or []:
            details = list(g.get("account_details") or [])
            for L in ("a", "b"):
                details += list((g.get(f"group_{L}") or {}).get("account_details") or [])
            for ad in details:
                if ad.get("campaign_names") and ad.get("email"):
                    out.add(ad["email"])
    return out


def build(live: bool = True) -> dict:
    """The whole generic-capacity picture. Read-only.

    `live` is the default and the only honest setting; it is a parameter purely
    so a caller that has just pulled the same data can skip the round trip.

    The headline `available` is deliberately conservative — warming, claimed,
    blocked and disputed stock are all held OUT of it — and every excluded
    bucket is reported on its own line, so the number can be argued with instead
    of taken on faith, and "how much frees up next week" is answerable from the
    same screen.
    """
    accounts = _live_accounts() if live else []
    if not accounts:
        return {"error": "could not read the mailbox list from SmartLead — "
                         "refusing to report a capacity number from stale data"}

    active_emails, active_by_email = _emails_in_active_campaigns()
    if active_emails is None:
        return {"error": "could not read the campaign list from SmartLead — "
                         "without it every inbox would look free"}
    unreadable = bool(active_by_email.pop("__unreadable__", None))

    inboxes = _roster(accounts)
    if not inboxes:
        return {"error": "no generic or client inboxes found"}

    sends, live_dates = _sends_by_email()
    ndays = len(live_dates)
    ever = _ever_in_campaign()
    claimed, burned = _claimed_emails(), _burned_emails()

    for i in inboxes:
        i["sent_window"] = sends.get(i["email"], 0)
        i["sent_per_day"] = round(i["sent_window"] / ndays, 1) if ndays else 0
        i["ever_in_campaign"] = i["email"] in ever
        i["active_campaigns"] = active_by_email.get(i["email"], [])
        i["state"], i["why"] = _classify(i, active_emails, unreadable, claimed, burned)

    # HVAC-branded stock is counted but reported apart from the three tracked
    # verticals: it cannot fill a landscaping or holiday slot without putting the
    # wrong trade in front of the prospect.
    def blank():
        return {s: {"inboxes": 0, "capacity": 0} for s in STATE_LABEL}

    by_vertical = {v: blank() for v in list(VERTICALS) + ["hvac"]}
    for i in inboxes:
        b = by_vertical[i["niche"]][i["state"]]
        b["inboxes"] += 1
        b["capacity"] += i["per_day"]

    def tot(v, states, key):
        return sum(by_vertical[v][s][key] for s in states)

    verticals = {}
    for v in by_vertical:
        rows = [i for i in inboxes if i["niche"] == v]
        av = [i for i in rows if i["state"] in AVAILABLE_STATES]
        verticals[v] = {
            "available_inboxes": len(av),
            "available_capacity": sum(i["per_day"] for i in av),
            "available_unassigned": sum(1 for i in av if not i["owner"]),
            "available_from_clients": sum(1 for i in av if i["owner"]),
            "free_inboxes": by_vertical[v][FREE]["inboxes"],
            "released_inboxes": by_vertical[v][RELEASED]["inboxes"],
            "warming_inboxes": by_vertical[v][WARMING]["inboxes"],
            "warming_capacity": by_vertical[v][WARMING]["capacity"],
            "warming_ready_in_days": min(
                [WARMUP_DAYS - i["age_days"] for i in rows
                 if i["state"] == WARMING and i["age_days"] is not None], default=None),
            "claimed_inboxes": by_vertical[v][CLAIMED]["inboxes"],
            "sending_inboxes": by_vertical[v][SENDING]["inboxes"],
            "sending_capacity": by_vertical[v][SENDING]["capacity"],
            "disputed_inboxes": by_vertical[v][DISPUTED]["inboxes"],
            "blocked_inboxes": by_vertical[v][BLOCKED]["inboxes"],
            "total_inboxes": len(rows),
            "total_capacity": sum(i["per_day"] for i in rows),
            "actual_per_day": round(sum(i["sent_window"] for i in rows) / ndays) if ndays else None,
            "by_state": by_vertical[v],
            "owners_with_idle": sorted(
                {i["owner"] for i in av if i["owner"]}),
        }

    T = VERTICALS
    summary = {k: sum(verticals[v][k] for v in T) for k in (
        "available_inboxes", "available_capacity", "available_unassigned",
        "available_from_clients", "warming_inboxes", "warming_capacity",
        "claimed_inboxes", "sending_inboxes", "sending_capacity",
        "disputed_inboxes", "blocked_inboxes", "total_inboxes", "total_capacity")}
    summary.update({
        "window_sending_days": ndays,
        "window_from": live_dates[0] if live_dates else None,
        "window_to": live_dates[-1] if live_dates else None,
        "measured": bool(ndays),
        "actual_per_day": (round(sum(i["sent_window"] for i in inboxes
                                     if i["niche"] in T) / ndays) if ndays else None),
        "partial": unreadable,
    })

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "live": True,
        "warmup_days": WARMUP_DAYS,
        "summary": summary,
        "verticals": verticals,
        "tracked_verticals": list(T),
        "state_labels": STATE_LABEL,
        "inboxes": sorted(inboxes, key=lambda x: (_state_order(x["state"]),
                                                  x["niche"], x["email"])),
    }
