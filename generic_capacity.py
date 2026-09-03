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
    """(busy emails, {email: [campaign rows]}, [unreadable campaign names]).

    Only ACTIVE campaigns are fetched, and that is what makes this affordable:
    whether an inbox is busy depends solely on whether some ACTIVE campaign is
    still holding it with work to do, so the 300-odd finished and paused
    campaigns need not be asked about at all — 45 calls instead of 369.

    An inbox lands in the busy set only if its campaign BOTH is active AND still
    has a non-empty queue; an active campaign that has run out of leads and
    follow-ups is holding its senders for nothing, and that stock is exactly
    what this page exists to surface.

    Returns `(None, {}, [])` if the campaign list itself could not be read: with
    no list at all the entire fleet would look free, which is the one answer
    that must never be printed.
    """
    import requests
    key = _sl_key()
    if not key:
        return None, {}, []
    session = requests.Session()
    r = _get(session, f"{SL}/campaigns", {"api_key": key}, timeout=60)
    if not r:
        return None, {}, []
    try:
        campaigns = r.json() or []
    except ValueError:
        return None, {}, []

    active = [c for c in campaigns if (c.get("status") or "").upper() == "ACTIVE"]
    queues = _lead_queues(session, [c.get("id") for c in active if c.get("id")])

    from concurrent.futures import ThreadPoolExecutor
    emails, by_email, unreadable = set(), {}, []

    def one(c):
        r = _get(session, f"{SL}/campaigns/{c['id']}/email-accounts",
                 {"api_key": key}, timeout=30)
        if not r:
            return c, None
        try:
            return c, (r.json() or [])
        except ValueError:
            return c, []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for c, rows in ex.map(one, active):
            if rows is None:
                # Its senders are unknown, so they must not fall through to
                # "free" on a transient error. The caller refuses to report.
                unreadable.append(c.get("name") or str(c.get("id")))
                continue
            q = queues.get(c.get("id"))
            has_work = campaign_has_work(q)
            for a in rows:
                e = a.get("from_email")
                if not e:
                    continue
                if has_work:
                    emails.add(e)
                by_email.setdefault(e, []).append({
                    "name": c.get("name"), "id": c.get("id"),
                    "has_work": has_work,
                    "remaining": (q or {}).get("remaining"),
                    "in_progress": (q or {}).get("in_progress"),
                })
    return emails, by_email, unreadable


MAX_WORKERS = 4          # SmartLead 429s readily; this page makes ~135 calls
RETRY_STATUS = (429, 500, 502, 503, 504)


def _get(session, url, params, timeout=30, tries=4):
    """One GET with backoff. Returns the Response, or None if it never succeeded.

    SmartLead rate-limits this key hard enough to answer a plain campaign-list
    request with a non-JSON body, so every call here has to assume it will be
    throttled at least once. `Retry-After` is honoured when sent.
    """
    import requests
    for attempt in range(tries):
        try:
            r = session.get(url, params=params, timeout=timeout)
        except requests.RequestException:
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 200:
            return r
        if r.status_code in RETRY_STATUS:
            wait = 0
            try:
                wait = int(r.headers.get("Retry-After") or 0)
            except ValueError:
                wait = 0
            time.sleep(min(wait or 4 * (attempt + 1), 30))
            continue
        return None
    return None


def _lead_queues(session, campaign_ids: list) -> dict:
    """{campaign id: {remaining, in_progress}} for the ACTIVE campaigns.

    Two queues, and BOTH have to be empty before a campaign is done with its
    senders:

      STARTED     leads never contacted   -> first-touch emails still to send
      INPROGRESS  leads mid-sequence      -> FOLLOW-UPS still to send

    Reading only STARTED is the classic way to get this wrong: a campaign that
    has finished its first touches still has thousands of leads mid-sequence,
    and freeing those senders cuts the sequences off and orphans the replies
    welded to the mailboxes that sent them.

    An unreadable queue is recorded as None, which `campaign_has_work` treats as
    "assume it still has work" — being wrong towards busy costs an idle mailbox
    for a day, being wrong the other way breaks a running campaign.
    """
    from concurrent.futures import ThreadPoolExecutor
    if not campaign_ids:
        return {}
    key = _sl_key()

    def one(cid):
        row = {}
        for status_key, field in (("STARTED", "remaining"),
                                  ("INPROGRESS", "in_progress")):
            r = _get(session, f"{SL}/campaigns/{cid}/leads",
                     {"api_key": key, "limit": 1, "offset": 0,
                      "status": status_key}, timeout=25)
            try:
                row[field] = int((r.json() or {}).get("total_leads", 0)) if r else None
            except (ValueError, TypeError):
                row[field] = None
        return cid, row

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        return {cid: row for cid, row in ex.map(one, campaign_ids)}


def campaign_has_work(queue: dict | None) -> bool:
    """True if this campaign still has anything for its senders to send.

    None (unread) counts as work — see `_lead_queues`.
    """
    if not queue:
        return True
    for field in ("remaining", "in_progress"):
        v = queue.get(field)
        if v is None or (v or 0) > 0:
            return True
    return False


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


def _classify(inbox: dict, active_emails: set, claimed: set,
              burned: set) -> tuple:
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

    # Busy = sitting in an ACTIVE campaign THAT STILL HAS WORK. `active_emails`
    # already carries that test (see `_lead_queues`): a campaign counts as having
    # work while either queue is non-empty — leads never contacted, or leads
    # mid-sequence with follow-ups still to send — or while we could not read it.
    #
    # This is what makes the "between sends" case safe. An inbox in the gap
    # between two steps of a live sequence still has INPROGRESS leads behind it,
    # so it stays busy on a day it sends nothing, and is never offered as spare.
    # Only when BOTH queues are drained is the campaign genuinely finished with
    # its senders, and only then does the inbox fall through to available.
    if inbox["email"] in active_emails:
        return SENDING, "in a live campaign that still has leads or follow-ups queued"

    # Reads free on campaign state. The true send rows get the last word: an
    # inbox that has been sending is working for someone regardless of what the
    # campaign list says.
    if inbox["sent_window"] > 0:
        return DISPUTED, ("in no active campaign, yet sent %d email(s) in the "
                          "measured window" % inbox["sent_window"])
    if not inbox["ever_in_campaign"]:
        return FREE, "in no campaign at all"
    drained = [c["name"] for c in (inbox.get("attached") or [])
               if not c.get("has_work")]
    if drained:
        return RELEASED, ("its campaign%s ran out of leads and follow-ups (%s)"
                          % ("s" if len(drained) > 1 else "",
                             ", ".join(drained[:2])))
    return RELEASED, "only in finished/paused campaigns, and sending nothing"


def client_of(tag: str) -> str | None:
    """The client a group tag belongs to, with the A/B group suffix stripped.

    "Timesavers Group B" and "Timesavers Group A" are one client holding two
    groups; reporting them apart would halve every client's apparent capacity
    and hide that the idle stock is concentrated in one of the two.
    """
    if not tag or tag.lower().startswith("generic"):
        return None
    if tag.strip().lower() in NON_CLIENT_TAGS:
        return None
    return re.sub(r"\s+(?:Group\s+)?[A-Z]\d?$", "", tag.strip()) or tag.strip()


def client_capacity(inboxes: list[dict], ndays: int) -> list[dict]:
    """Per-client capacity: what each client holds against what it actually uses.

    This is the question "are we paying for inboxes a client is not sending
    with", and it is answered from the send rows, not from the campaign list —
    a client can hold 50 warm mailboxes attached to a live campaign and still be
    sending from six of them.

    `idle_*` counts only mailboxes that are free by the same rule the rest of
    this module uses: past warmup, not held for a replacement, not blocked, in
    no campaign that still has work, and silent all window. Warming stock is
    reported separately — it is capacity the client will have, not capacity
    anyone is wasting.
    """
    from collections import defaultdict
    rows = defaultdict(list)
    for i in inboxes:
        c = client_of(i.get("tag"))
        if c:
            rows[c].append(i)

    out = []
    for name, mine in sorted(rows.items()):
        idle = [i for i in mine if i["state"] in AVAILABLE_STATES]
        warming = [i for i in mine if i["state"] == WARMING]
        blocked = [i for i in mine if i["state"] == BLOCKED]
        sending = [i for i in mine if i["state"] == SENDING]
        nameplate = sum(i["per_day"] for i in mine)
        # Deployable = what could send today: everything except stock that is
        # warming, blocked or spoken for. Utilisation against nameplate alone
        # would punish a client for mailboxes that are not usable yet.
        usable = [i for i in mine if i["state"] not in (WARMING, BLOCKED, CLAIMED)]
        usable_cap = sum(i["per_day"] for i in usable)
        actual = round(sum(i["sent_window"] for i in mine) / ndays) if ndays else None
        out.append({
            "client": name,
            "inboxes": len(mine),
            "nameplate_capacity": nameplate,
            "usable_capacity": usable_cap,
            "actual_per_day": actual,
            "utilisation_pct": (round(100 * actual / usable_cap)
                                if usable_cap and actual is not None else None),
            "idle_inboxes": len(idle),
            "idle_capacity": sum(i["per_day"] for i in idle),
            "sending_inboxes": len(sending),
            "warming_inboxes": len(warming),
            "blocked_inboxes": len(blocked),
            "silent_inboxes": sum(1 for i in usable if i["sent_window"] == 0),
            "niches": sorted({i["niche"] for i in mine}),
        })
    # Worst offenders first: the most reclaimable capacity at the top.
    out.sort(key=lambda r: (-r["idle_capacity"], r["utilisation_pct"] or 0))
    return out


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

    active_emails, active_by_email, unread = _emails_in_active_campaigns()
    if active_emails is None:
        return {"error": "could not read the campaign list from SmartLead — "
                         "without it every inbox would look free"}
    # An unreadable active campaign means we do not know who its senders are, so
    # any mailbox we would call free might be inside it. Say so and stop, rather
    # than print a number nobody should act on: an earlier version degraded
    # every unattached inbox to "disputed" instead, which quietly reported
    # 0 available across the whole fleet and looked like a broken page.
    if unread:
        return {"error": "could not read %d active campaign(s) after retries (%s)"
                         " — refusing to report capacity from an incomplete"
                         " picture. Try again in a minute; SmartLead was most"
                         " likely rate-limiting."
                         % (len(unread), ", ".join(unread[:3]))}

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
        i["attached"] = active_by_email.get(i["email"], [])
        i["active_campaigns"] = [c["name"] for c in i["attached"] if c.get("has_work")]
        i["state"], i["why"] = _classify(i, active_emails, claimed, burned)

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
        "partial": False,
    })

    clients = client_capacity(inboxes, ndays)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "live": True,
        "clients": clients,
        "client_totals": {
            "clients": len(clients),
            "inboxes": sum(c["inboxes"] for c in clients),
            "nameplate_capacity": sum(c["nameplate_capacity"] for c in clients),
            "usable_capacity": sum(c["usable_capacity"] for c in clients),
            "actual_per_day": sum(c["actual_per_day"] or 0 for c in clients),
            "idle_inboxes": sum(c["idle_inboxes"] for c in clients),
            "idle_capacity": sum(c["idle_capacity"] for c in clients),
        },
        "warmup_days": WARMUP_DAYS,
        "summary": summary,
        "verticals": verticals,
        "tracked_verticals": list(T),
        "state_labels": STATE_LABEL,
        "inboxes": sorted(inboxes, key=lambda x: (_state_order(x["state"]),
                                                  x["niche"], x["email"])),
    }
