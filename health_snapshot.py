"""Health V1 — daily snapshot job.

Records TRUE per-day metrics for every inbox, then re-scores each one over its
trailing 3-SENDING-day window.

Where the numbers come from, and why it changed (2026-08-21). This job used to
take its metrics from the `overview_v2` cache, on the reasoning that the cache
was already fresh and the internal metrics endpoint needed an expiring JWT. That
was a bad trade: the cache's per-inbox `sent` is a SEVEN-day rolling total, it
was written as a single day's row, and three such rows were then summed into
`sent_3d` — roughly 8x reality, which left the `min_sent_3d` volume gate firing
for 3 inboxes out of 1,794. Worse, consecutive dates held identical values, so
health_alerts' day-over-day comparison could never see a change.

Metrics now come from health_daily, which asks SmartLead for exactly the window
being reasoned about and stores one real day per row. The JWT problem is solved
properly there (auto-mint via login credentials, and a thin response raises
instead of being mistaken for a quiet fleet) rather than avoided.

Division of responsibility, kept strict so neither can corrupt the other:
  * overview_v2  -> attribution only (client, group, domain, smtp, warmup, the
                    campaigns an inbox sits in). See attrs_from_overview.
  * health_daily -> every metric. Nothing else may supply sent/bounce/reply.

OOO/placement still aren't collected here; placement is written separately by
health_placement.py, so this job never writes that column.

No inbox is deleted here — this is read-only measurement. Acting on burned
inboxes is health_actions.py, behind an explicit confirm.
"""

from __future__ import annotations

from datetime import datetime

import db as store
import health_model as hm

SOURCE_DEFAULT = "Zapmail"   # all THT inboxes come from Zapmail


def _num(v):
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def attrs_from_overview(overview: dict) -> dict:
    """{email: attribution} for every inbox — client, group, domain, smtp, warmup.

    The overview cache is the ONLY source for who an inbox belongs to. It is not
    a source for how much it sent: its `sent` is a 7-day rolling total (see
    health_daily's module docstring), which is what made `sent_3d` read ~8x high
    for the first six weeks of Health V1. Metrics now come from health_daily;
    this function exists to keep the two strictly apart.
    """
    return {r["email"]: {
        "client": r["client"], "group_letter": r["group_letter"],
        "source": r["source"], "domain": r["domain"],
        "smtp_ok": r["smtp_ok"], "warmup_reputation": r["warmup_reputation"],
    } for r in build_fleet_from_overview(overview)}


def build_fleet_from_overview(overview: dict) -> list[dict]:
    """Flatten every inbox in the overview cache into scored-ready records,
    with client / group / warming attribution. Deduped by email."""
    seen: dict[str, dict] = {}

    def add(ad, client, letter, in_warmup):
        email = ad.get("email")
        if not email or email in seen:
            return
        seen[email] = {
            "email": email,
            "domain": ad.get("domain") or (email.split("@", 1)[1] if "@" in email else ""),
            "client": client,
            "group_letter": letter,
            "source": SOURCE_DEFAULT,
            "in_warmup": in_warmup,
            "reply_rate": _num(ad.get("reply_rate")),
            "bounce_rate": _num(ad.get("bounce_rate")),
            "sent": int(ad.get("sent") or 0),
            "smtp_ok": ad.get("smtp_ok"),
            "warmup_reputation": _num(ad.get("warmup_reputation")),
            "campaigns": ad.get("campaign_names") or [],
            "in_campaign": ad.get("in_campaign"),
        }

    # client A/B groups = production
    for c in overview.get("clients", []):
        for letter in ("A", "B"):
            g = c.get(f"group_{letter.lower()}") or {}
            for ad in g.get("account_details", []):
                add(ad, c.get("name"), letter, in_warmup=False)
    # acquisition groups = production (THT's own outreach)
    for g in overview.get("acquisition_groups", []):
        for ad in g.get("account_details", []):
            add(ad, "(acquisition)", None, in_warmup=False)
    # generic groups = warming reserve
    for g in overview.get("generic_groups", []):
        for ad in g.get("account_details", []):
            add(ad, "(generic reserve)", None, in_warmup=True)

    return list(seen.values())


def snapshot_daily(overview: dict | None = None, today: str | None = None,
                   cfg: dict | None = None) -> dict:
    """Snapshot + score the fleet. Pass `overview` from sync (fresh), otherwise
    it's read from the overview_v2 cache."""
    today = today or datetime.now().strftime("%Y-%m-%d")
    cfg = cfg or store.get_health_config("default") or {}

    if overview is None:
        overview, _ = store.cache_get("overview_v2")
    if not overview:
        return {"ok": False, "error": "overview_v2 cache empty", "date": today}

    fleet = build_fleet_from_overview(overview)
    if not fleet:
        return {"ok": False, "error": "no inboxes in overview", "date": today}

    # 1) Record TRUE per-day metrics for the recent complete days, then pick the
    #    scoring window from real sending days.
    #
    #    This replaces the original approach of writing the overview cache's
    #    per-inbox `sent` as "today's" row. That value is a SEVEN-day rolling
    #    total, so every row held a week of sending under a single date, three of
    #    them were summed into `sent_3d`, and consecutive dates ended up holding
    #    IDENTICAL numbers (which also flatlined health_alerts' day-over-day
    #    comparison). See health_daily for the full account.
    import health_daily as hd
    hist_info = hd.refresh_history(attrs_from_overview(overview))
    window_sig, window_dates = hd.window_signals(sent_by_date=hist_info["sent_by_date"])

    # 2) re-score each inbox off that window
    #    (one bulk fetch of recent history, not one query per inbox)
    from datetime import timedelta
    since = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=14)).strftime("%Y-%m-%d")
    history = store.get_health_daily_bulk(since)

    status_rows = []
    counts: dict[str, int] = {}
    # Burn-confirmation hysteresis: an inbox only flips to BURNED after it's met the
    # burn condition on `confirm_days` CONSECUTIVE snapshots; one day back under the
    # line resets the streak. Stops borderline inboxes (bounce ~3%) bouncing in/out
    # of the burned list. Existing burned inboxes are grandfathered so turning this
    # on doesn't wipe the current list.
    confirm_days = int(cfg.get("burn_confirm_days", 3))
    prev_status = {row["email"]: row.get("status") for row in store.get_health_status_all()}
    # {email: [streak_days, last_counted_date]} — date-keyed so re-running the
    # snapshot within the same day doesn't inflate the streak (it counts DAYS).
    streaks = store.get_state("burn_streaks") or {}
    new_streaks: dict[str, list] = {}

    for r in fleet:
        rows = history.get(r["email"], [])
        # Prefer SmartLead's OWN aggregate for the scoring window: one call over
        # the whole range means the rates are computed against its de-duplicated
        # lead set. Re-pooling our stored daily rows would double-count any lead
        # contacted on more than one day in the window. `rolling` stays as the
        # fallback for an inbox missing from the window response (it sent
        # nothing) so it still gets its trend and prior-window figures.
        w = window_sig.get(r["email"])
        sig = hm.rolling(rows, days=3)
        if w:
            sig.update({k: w[k] for k in ("reply", "bounce", "sent_3d")})
        else:
            sig["sent_3d"] = 0                 # absent from the window = no sends
        sig["in_warmup"] = r["in_warmup"]
        sig["smtp_ok"] = r["smtp_ok"]
        sig["in_campaign"] = r["in_campaign"]
        res = hm.score_inbox(sig, cfg)
        status, reasons = res["status"], res["reasons"]

        if status == hm.BURNED:
            email = r["email"]
            prev = streaks.get(email)
            prev_count = prev[0] if isinstance(prev, list) else (prev if isinstance(prev, int) else None)
            prev_date = prev[1] if isinstance(prev, list) else None
            if prev_date == today:
                streak = prev_count or 1             # already counted today (same-day re-run)
            elif prev_count is not None:
                streak = prev_count + 1              # a new day still over the line
            elif prev_status.get(email) == hm.BURNED:
                streak = confirm_days                # grandfather already-burned inboxes
            else:
                streak = 1
            new_streaks[email] = [streak, today]
            if streak < confirm_days:                # not yet confirmed -> hold at at-risk
                status = hm.AT_RISK
                reasons = list(reasons) + [f"burning {streak}/{confirm_days} days — not yet confirmed"]

        counts[status] = counts.get(status, 0) + 1
        status_rows.append({
            "email": r["email"],
            "score": res["score"], "status": status,
            "reasons": reasons, "subscores": res["subscores"],
            "client": r["client"], "group_letter": r["group_letter"],
            "source": r["source"], "domain": r["domain"],
            "reply_3d": sig.get("reply"), "bounce_3d": sig.get("bounce"),
            "ooo_3d": sig.get("ooo"), "placement": sig.get("placement"),
            "sent_3d": sig.get("sent_3d", 0),
            "smtp_ok": r["smtp_ok"], "warmup_reputation": r["warmup_reputation"],
            "campaigns": r["campaigns"],
            "updated_at": datetime.now().isoformat(),
        })
    store.upsert_health_status(status_rows)
    store.set_state("burn_streaks", new_streaks)

    # 3) build the top-of-page "what just sank" alert feed from the same history
    try:
        import health_alerts as ha
        alerts, alert_summary = ha.build_alerts(status_rows, history)
    except Exception:
        alerts, alert_summary = [], {}

    # 4) cache the fleet for fast, read-only UI access
    fleet_out = sorted(status_rows, key=lambda x: hm.STATUS_RANK.get(x["status"], 0), reverse=True)
    store.cache_set("health_fleet", {
        "generated_at": datetime.now().isoformat(),
        "date": today, "counts": counts, "inboxes": fleet_out,
        "alerts": alerts, "alert_summary": alert_summary,
        # The exact days "3d" refers to. Published so the UI can state the window
        # instead of implying "the last three days" — which is wrong whenever the
        # window reaches back over a weekend to find three real sending days.
        "window_dates": window_dates,
        "window_sent": hist_info["sent_by_date"],
    })

    return {"ok": True, "date": today, "inboxes": len(status_rows), "counts": counts,
            "window_dates": window_dates, "daily_rows_written": hist_info["rows"]}


if __name__ == "__main__":
    import json
    print(json.dumps(snapshot_daily(), indent=2, default=str))
