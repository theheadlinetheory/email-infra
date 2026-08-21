"""Health V1 — TRUE per-day metric collection.

Why this module exists (2026-08-21). The scorer used to get its numbers from the
`overview_v2` cache, whose per-inbox `sent` comes from
`sync.fetch_health_metrics()` called with NO dates — which defaults to a SEVEN-DAY
window. That 7-day total was then stored as a single day's row, and
`health_model.rolling(days=3)` SUMMED three of those rows. The result called
`sent_3d` was therefore roughly three overlapping 7-day totals — about 8x reality
(fleet median read 78 when the true 3-day figure is nearer 30) — so the
`min_sent_3d` volume gate almost never fired: 3 inboxes out of 1,794 were ever
marked "low volume".

The stored rows were wrong a second way too: because every snapshot wrote the
same 7-day total under a different date, consecutive dates held IDENTICAL values
(61,792 fleet-wide for Aug 12-18). That silently flatlined `health_alerts`, which
works by comparing the earliest and latest day in the window.

The fix, and the rule this module enforces:

    NEVER re-aggregate an aggregate. Ask SmartLead for exactly the window you
    want to reason about.

`name-wise-health-metrics` honours arbitrary start/end dates and returns raw
COUNTS (sent, bounced, replied, opened, positive_replied, unique_lead_count)
alongside its own rates. So:

  * daily history  -> one call per day, stored with real counts, one true day per
                      row. Trend and alerts become meaningful.
  * scoring window -> ONE call for the whole 3-day range, so the rates are
                      SmartLead's own, computed over its own de-duplicated lead
                      set. Summing three days of `unique_lead_count` would
                      double-count any lead contacted on more than one day; the
                      single windowed call has no such error.

That last point matters for threshold compatibility. SmartLead's rates divide by
`unique_lead_count`, NOT by `sent` — verified 621/621 inboxes over a 7-day window
(only 213 matched bounced/sent). Every threshold in health_model (bounce_burn 3.0,
reply_dead 0.5, ...) was tuned against that definition, so we keep it exactly.

Auth: the internal endpoint needs a browser JWT. `sync.sl_internal_headers()`
reads the static SMARTLEAD_JWT env var, which expires; when it does the fetch
returns {} and every inbox looks like it sent nothing. Here we mint through
`health_smartlead.get_jwt()` (login-credential auto-refresh) and treat a thin
result as a hard failure rather than as "the fleet went quiet".
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import db as store

URL = "https://server.smartlead.ai/api/analytics/mailbox/name-wise-health-metrics"
TZ = "America/New_York"          # the timezone SmartLead buckets days by

# A window that returns fewer than this many inboxes is treated as a broken
# fetch (expired JWT / rate limit), never as a quiet fleet. The scorer aborts
# instead of writing zeros over everyone's history.
MIN_RECORDS = 50

WINDOW_DAYS = 3                  # the scoring window, matching min_sent_3d
HISTORY_DAYS = 10                # how many complete days of daily rows we keep fresh

# Count columns we persist per day. Rates are derived from these, never averaged.
COUNT_FIELDS = ("sent", "bounced", "replied", "opened",
                "positive_replied", "unique_lead_count")


def _pct(v):
    """SmartLead returns rates as strings like '7.69%'."""
    if v is None or v == "":
        return None
    try:
        return float(str(v).rstrip("%"))
    except ValueError:
        return None


def _int(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def today_local() -> str:
    """Today's date in SmartLead's reporting timezone (America/New_York).

    Using the server's local date would roll over at the wrong moment — a job
    running just after UTC midnight would ask for a 'yesterday' that is still
    today in New York, i.e. an incomplete day.
    """
    return datetime.now(timezone.utc).astimezone(
        timezone(timedelta(hours=-4))).strftime("%Y-%m-%d")


def complete_days(n: int, end_day: str | None = None) -> list[str]:
    """The `n` most recent COMPLETE days, oldest first.

    A day is complete only once it is over in New York, so we never include
    today: a partial day would drag `sent_3d` down by however many hours are
    left in it, which is precisely the kind of quiet inaccuracy this module
    exists to remove.
    """
    last = datetime.strptime(end_day or today_local(), "%Y-%m-%d") - timedelta(days=1)
    return [(last - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n - 1, -1, -1)]


def _headers() -> dict:
    import health_smartlead as hsl
    jwt = hsl.get_jwt()
    if not jwt:
        raise RuntimeError("no SmartLead JWT — set SMARTLEAD_LOGIN_EMAIL/PASSWORD "
                           "or SMARTLEAD_JWT")
    return {"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"}


def fetch_window(start: str, end: str, retries: int = 4, min_records: int = 0) -> dict:
    """Per-inbox metrics for the inclusive date range [start, end].

    Returns {email: {sent, bounced, replied, ..., bounce_rate, reply_rate}} with
    counts as ints and rates as floats. Raises on a persistently failed fetch.

    `min_records` defaults to 0 on purpose. A SINGLE day can legitimately return
    zero rows — the fleet does not send at weekends (Sat 2026-08-15 was empty
    fleet-wide), and treating that as a broken fetch would both retry pointlessly
    and refuse to record a true zero. A well-formed HTTP 200 IS the success
    signal; auth failure is a 401 and rate limiting a 429, both handled below.
    Callers spanning several days pass MIN_RECORDS, where a thin result really
    does mean something is wrong.
    """
    import requests
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(URL, params={"start_date": start, "end_date": end,
                                          "timezone": TZ, "full_data": "true"},
                             headers=_headers(), timeout=60)
        except requests.RequestException as e:
            last = str(e)
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code == 401:
            # expired token — force a fresh mint and retry rather than returning {}
            import health_smartlead as hsl
            hsl.get_jwt(force=True)
            last = "401 unauthorized"
            time.sleep(2)
            continue
        if r.status_code != 200:
            last = f"HTTP {r.status_code}"
            time.sleep(5 * (attempt + 1))
            continue
        rows = (r.json().get("data") or {}).get("email_health_metrics", [])
        out = {}
        for x in rows:
            email = x.get("from_email")
            if not email:
                continue
            rec = {f: _int(x.get(f)) for f in COUNT_FIELDS}
            rec["bounce_rate"] = _pct(x.get("bounce_rate"))
            rec["reply_rate"] = _pct(x.get("reply_rate"))
            rec["open_rate"] = _pct(x.get("open_rate"))
            out[email] = rec
        if len(out) < min_records:
            last = f"only {len(out)} records"
            time.sleep(5 * (attempt + 1))
            continue
        return out
    raise RuntimeError(f"metrics fetch failed for {start}..{end}: {last}")


LIVE_DAY_FRACTION = 0.20   # a day counts as a sending day at >=20% of a median day


def sending_days(sent_by_date: dict, fraction: float = LIVE_DAY_FRACTION) -> list[str]:
    """Dates on which the fleet genuinely sent, oldest first.

    Not simply `sent > 0`. The real calendar looks like this: weekdays run
    ~11,000-12,700 sends, Saturdays are hard zero, and Sundays TRICKLE — 185
    sends across 32 inboxes on 2026-08-16, 338 across 59 on 2026-08-09. A
    Sunday like that is a dead day wearing a non-zero total, and counting it
    would put two-thirds of a "3 sending day" window on a day nobody worked.

    So the bar is relative to a normal day: at least `fraction` of the median
    non-zero day. That self-calibrates — it needs no weekday table, and it keeps
    working if THT starts sending weekends, pauses for a holiday week, or grows
    the fleet.
    """
    vals = sorted(n for n in sent_by_date.values() if (n or 0) > 0)
    if not vals:
        return []
    median = vals[len(vals) // 2]
    floor = median * fraction
    return sorted(d for d, n in sent_by_date.items() if (n or 0) >= floor)


def choose_window(sent_by_date: dict, days: int = WINDOW_DAYS) -> list[str]:
    """The calendar range covering the last `days` genuine SENDING days.

    We do not send at weekends, so a fixed three-calendar-day window run on a
    Monday would span Fri/Sat/Sun — one sending day — and drop essentially the
    whole fleet under min_sent_3d every Monday and Tuesday. That is manufactured
    noise, not a real signal, and it is exactly the kind of thing that trains
    people to ignore a status column.

    So the window stretches back until it covers `days` real sending days. The
    returned range stays CONTIGUOUS (dead days inside it are included) because
    the scoring call must be a single date range — and that costs nothing: a day
    with no sends contributes 0 to both the numerator and the denominator of
    every rate, so it cannot shift a single number.
    """
    live = sending_days(sent_by_date)
    if not live:
        return []
    keep = live[-days:]
    return [d for d in sorted(sent_by_date) if keep[0] <= d <= keep[-1]]


def window_signals(days: int = WINDOW_DAYS, end_day: str | None = None,
                   sent_by_date: dict | None = None) -> tuple:
    """(signals_by_email, [dates]) for the trailing `days` SENDING days.

    ONE call for the whole range, so `bounce`/`reply` are SmartLead's own rates
    over its own de-duplicated lead set — not an average of daily rates and not
    a ratio over a denominator that double-counts leads contacted on more than
    one day.

    Pass `sent_by_date` (from refresh_history) to pick the window without
    re-probing each day.
    """
    if sent_by_date:
        dates = choose_window(sent_by_date, days)
    else:
        dates = complete_days(days, end_day)
    if not dates:
        raise RuntimeError("no sending days found in the recent history window")
    data = fetch_window(dates[0], dates[-1], min_records=MIN_RECORDS)
    sig = {}
    for email, rec in data.items():
        sig[email] = {
            "reply": rec["reply_rate"],
            "bounce": rec["bounce_rate"],
            "ooo": None,
            "sent_3d": rec["sent"],
            "sent": rec["sent"],
            "bounced": rec["bounced"],
            "replied": rec["replied"],
            "unique_lead_count": rec["unique_lead_count"],
        }
    return sig, dates


def daily_rows(dates: list[str], attrs: dict) -> list[dict]:
    """True one-day rows for `dates`, ready for inbox_health_daily.

    `attrs` is {email: {client, group_letter, source, domain, smtp_ok,
    warmup_reputation}} — the tagging/attribution that only the overview cache
    knows. Metrics come from SmartLead, attribution from the cache; the two are
    kept strictly separate so a metrics outage can never rewrite attribution and
    vice versa.

    `placement` is deliberately never written here — health_placement.py owns
    that column, and including it as None would clobber it on every run
    (merge-duplicates only touches columns present in the payload).
    """
    zero = {f: 0 for f in COUNT_FIELDS}
    zero.update({"reply_rate": 0.0, "bounce_rate": 0.0, "open_rate": 0.0})
    rows = []
    for d in dates:
        data = fetch_window(d, d)
        # A row for EVERY tracked inbox, zero-filled when SmartLead reports
        # nothing for it that day. Writing only the inboxes that appear would
        # leave the previous (wrong) value standing for everyone else — and on a
        # day the whole fleet is quiet, such as any Saturday, SmartLead returns
        # no records at all, so not one stale row would be corrected. An inbox
        # that sent nothing sent zero, and the history must say so.
        for email, a in attrs.items():
            rec = data.get(email) or zero
            rows.append({
                "email": email, "date": d,
                "client": a.get("client"), "group_letter": a.get("group_letter"),
                "source": a.get("source"), "domain": a.get("domain")
                or (email.split("@", 1)[-1] if "@" in email else None),
                "reply_rate": rec["reply_rate"], "bounce_rate": rec["bounce_rate"],
                "ooo_rate": None,
                "sent": rec["sent"], "bounced": rec["bounced"],
                "replied": rec["replied"], "opened": rec["opened"],
                "positive_replied": rec["positive_replied"],
                "unique_lead_count": rec["unique_lead_count"],
                "smtp_ok": a.get("smtp_ok"),
                "warmup_reputation": a.get("warmup_reputation"),
            })
    return rows


def refresh_history(attrs: dict, days: int = HISTORY_DAYS,
                    end_day: str | None = None) -> dict:
    """Rewrite the last `days` complete days of daily rows with true per-day
    numbers. Idempotent — upserts on (email, date), so re-running repairs rather
    than duplicates."""
    dates = complete_days(days, end_day)
    rows = daily_rows(dates, attrs)
    store.upsert_health_daily(rows)
    return {"days": dates, "rows": len(rows),
            "sent_by_date": _sent_by_date(rows)}


def _sent_by_date(rows: list[dict]) -> dict:
    out: dict = {}
    for r in rows:
        out[r["date"]] = out.get(r["date"], 0) + (r.get("sent") or 0)
    return out


def _self_test() -> None:
    """Window selection against the fleet's real weekly shape. No network."""
    # a real fortnight: weekdays ~11k, Saturday hard zero, Sunday a trickle
    cal = {
        "2026-08-10": 11419, "2026-08-11": 10918, "2026-08-12": 12711,
        "2026-08-13": 11694, "2026-08-14": 11166,
        "2026-08-15": 0,     "2026-08-16": 185,                 # Sat / Sun
        "2026-08-17": 10755, "2026-08-18": 12658, "2026-08-19": 11155,
    }
    live = sending_days(cal)
    assert "2026-08-15" not in live, "Saturday must not count"
    assert "2026-08-16" not in live, "a 185-send Sunday must not count"
    assert len(live) == 8, live

    # mid-week: three clean weekdays
    assert choose_window(cal, 3) == ["2026-08-17", "2026-08-18", "2026-08-19"]

    # THE MONDAY CASE: run first thing Tuesday, so the last complete day is
    # Monday and the two before it are the weekend. A naive 3-calendar-day
    # window would be Sat+Sun+Mon = ONE sending day and would strand the fleet
    # under min_sent_3d. It must instead reach back over the weekend to Thu/Fri.
    monday = {d: n for d, n in cal.items() if d <= "2026-08-17"}
    win = choose_window(monday, 3)
    assert win == ["2026-08-13", "2026-08-14", "2026-08-15",
                   "2026-08-16", "2026-08-17"], win
    live_in_win = [d for d in win if d in sending_days(monday)]
    assert live_in_win == ["2026-08-13", "2026-08-14", "2026-08-17"], live_in_win
    # The weekend rides along inside the range. That is intended: whatever it
    # really sent (0 on the Saturday, 185 on the Sunday) is real traffic and
    # belongs in sent_3d — it just must not DEFINE the window. The three
    # sending days carry the window; the weekend adds under 1% on top.
    weekend = monday["2026-08-15"] + monday["2026-08-16"]
    assert weekend / sum(monday[d] for d in win) < 0.01

    # a fleet that has never sent must not fabricate a window
    assert choose_window({"2026-08-19": 0}, 3) == []
    print("self-test OK — weekend-aware window selection")


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv          # local runs only; Vercel injects env
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

    _self_test()
    dates = complete_days(WINDOW_DAYS)
    print("scoring window:", dates)
    sig, _ = window_signals()
    tot = sum(s["sent_3d"] for s in sig.values())
    active = [s for s in sig.values() if s["sent_3d"] > 0]
    print(f"inboxes: {len(sig)}  sending: {len(active)}  total sent: {tot}")
    if active:
        vals = sorted(s["sent_3d"] for s in active)
        print("sent_3d  median %d  p90 %d  max %d" %
              (vals[len(vals) // 2], vals[int(len(vals) * .9)], vals[-1]))
        print("under min_sent_3d=30: %d of %d sending (%.0f%%)" %
              (sum(1 for v in vals if v < 30), len(vals),
               100 * sum(1 for v in vals if v < 30) / len(vals)))
    for d in complete_days(5):
        w = fetch_window(d, d)
        print("  %s  inboxes=%-5d sent=%d" % (d, len(w), sum(x["sent"] for x in w.values())))
