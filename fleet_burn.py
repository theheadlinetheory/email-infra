"""Burn rate and replacement runway — how fast the fleet is dying, and how long
the replacement pool covers it.

A count of currently-burned inboxes is a snapshot and answers nothing you can
plan with. 25 burned is fine if it has been 25 for a month and an emergency if
it was 4 last week. The number that matters is the RATE, and the only thing that
makes a rate actionable is what you have to replace them from — 45 in the
Replacement Group is nine weeks of cover at 5/week and under two at 25/week.

WHY THE RATE IS RECORDED RATHER THAN RECONSTRUCTED. The obvious approach is to
replay `inbox_health_daily` through health_model's BURNED rule and count first
crossings. That was tried and thrown away: applied to 2026-09-16 the rule fires
on 139 inboxes while `inbox_health_status` carries 25 burned, and only 7 of them
are the same inboxes. The two tables disagree in both directions, which fits
what is already known about that history — the daily rows carried trailing
7-day totals for a period, and the count columns were never migrated, so nothing
before 2026-08-07 is trustworthy. A rate derived from it looked authoritative
and was fiction: the first cut reported 873 burns in four weeks.

So this records the truth instead of reconstructing it. `inbox_health_status` IS
trustworthy — it is what the health tab acts on — so a daily snapshot of its
counts is taken, and the rate is the movement between snapshots. That means the
rate is unavailable on day one and says so, rather than inventing a number to
fill the card.

NEW BURNS, NOT NET CHANGE. Between two snapshots the burned count can fall
because inboxes were cancelled or swapped out, so a net difference understates
new damage and can even go negative while the fleet is burning. The snapshot
therefore stores the burned EMAIL SET, and new burns are the addresses that were
not burned last time.
"""
from __future__ import annotations

from datetime import date, timedelta

STATE_KEY = "fleet_burn_history"
# Keep enough snapshots for a rolling quarter; older ones answer nothing.
MAX_SNAPSHOTS = 120


def snapshot(statuses: list[dict], today: str) -> dict:
    """One day's record: the counts, and which inboxes are burned."""
    counts: dict[str, int] = {}
    burned = []
    for s in statuses or []:
        st = s.get("status")
        counts[st] = counts.get(st, 0) + 1
        if st == "burned" and s.get("email"):
            burned.append(s["email"].lower())
    return {"date": today, "counts": counts, "burned": sorted(burned)}


def record(history: list[dict], statuses: list[dict], today: str) -> list[dict]:
    """Add today's snapshot, replacing any existing one for the same date."""
    snap = snapshot(statuses, today)
    kept = [h for h in (history or []) if h.get("date") != today]
    kept.append(snap)
    kept.sort(key=lambda h: h.get("date") or "")
    return kept[-MAX_SNAPSHOTS:]


def _d(s):
    try:
        return date.fromisoformat(str(s)[:10])
    except (ValueError, TypeError):
        return None


def build(history: list[dict] | None, statuses: list[dict] | None,
          replacement_pool: int | None, today: str, weeks: int = 4) -> dict:
    """Burn rate from recorded snapshots, and the runway it implies."""
    if statuses is None:
        # A rate from a failed read always comes back low, which reads as
        # "the fleet is fine".
        return {"measured": False, "recording": False,
                "reason": "health status unavailable — burn rate not computed",
                "weeks": [], "summary": {"burned_now": None, "at_risk": None,
                                         "per_week": None, "runway_weeks": None,
                                         "replacement_pool": replacement_pool}}

    hist = sorted((history or []), key=lambda h: h.get("date") or "")
    now = snapshot(statuses, today)
    burned_now = now["counts"].get("burned", 0)
    at_risk = now["counts"].get("at_risk", 0)

    # Snapshots strictly before today, so today's own record cannot be compared
    # against itself and report zero.
    prior = [h for h in hist if (h.get("date") or "") < today]
    if not prior:
        return {
            "measured": False, "recording": True,
            "reason": ("Recording started today. The rate needs a second day to "
                       "compare against — it is not zero, it is not yet known."),
            "weeks": [],
            "summary": {"burned_now": burned_now, "at_risk": at_risk,
                        "per_week": None, "runway_weeks": None,
                        "replacement_pool": replacement_pool,
                        "days_recorded": len(hist)},
        }

    t = _d(today) or date.today()
    start = t - timedelta(days=7 * weeks)
    window = [h for h in prior if (_d(h["date"]) or t) >= start] + [now]
    window.sort(key=lambda h: h["date"])

    # New burns between consecutive snapshots: addresses burned now that were not
    # burned before. Net change would be wrong — a cancelled inbox leaves the
    # burned set and would cancel out a genuinely new one.
    new_by_date = []
    for a, b in zip(window, window[1:]):
        fresh = set(b.get("burned") or []) - set(a.get("burned") or [])
        new_by_date.append({"date": b["date"], "new_burns": len(fresh)})

    buckets = []
    for w in range(weeks):
        w_start = (start + timedelta(days=7 * w)).isoformat()
        w_end = (start + timedelta(days=7 * w + 6)).isoformat()
        n = sum(r["new_burns"] for r in new_by_date if w_start <= r["date"] <= w_end)
        buckets.append({"from": w_start, "to": w_end, "new_burns": n})

    total = sum(r["new_burns"] for r in new_by_date)
    first, last = _d(window[0]["date"]), _d(window[-1]["date"])
    days = max(1, (last - first).days) if first and last else 1
    per_week = round(total * 7 / days, 1)
    runway = (round(replacement_pool / per_week, 1)
              if replacement_pool is not None and per_week > 0 else None)

    return {
        "measured": True, "recording": True,
        "window_from": window[0]["date"], "window_to": window[-1]["date"],
        # Says how much history the rate actually rests on, so a figure built
        # from two days is not read with the confidence of one built from thirty.
        "days_observed": days,
        "days_recorded": len(hist),
        "weeks": buckets,
        "summary": {
            "burned_now": burned_now,
            "at_risk": at_risk,
            "new_burns_window": total,
            "per_week": per_week,
            "replacement_pool": replacement_pool,
            # None when nothing is burning: "infinite cover" is an invitation to
            # cancel the replacement pool.
            "runway_weeks": runway,
            "days_observed": days,
        },
    }
