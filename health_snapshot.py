"""Health V1 — daily snapshot job.

Reads per-inbox metrics from the already-fresh `overview_v2` cache (the same
data the live dashboard shows), snapshots them per-inbox per-day, and re-scores
every inbox off its trailing 3-day window.

Why the cache and not SmartLead directly: overview_v2 already carries per-inbox
bounce_rate / reply_rate / sent / smtp_ok, refreshed by the normal sync. The
internal name-wise-health-metrics endpoint needs an expiring JWT (and was found
broken in prod), so V1 rides on the stable, working cache instead. No token, no
timeout risk. OOO/placement aren't in the cache yet -> the model scores on
reply+bounce (renormalised), exactly the two signals prioritised on the call.

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

    # 1) upsert today's per-inbox daily rows
    daily_rows = [{
        "email": r["email"], "date": today,
        "client": r["client"], "group_letter": r["group_letter"],
        "source": r["source"], "domain": r["domain"],
        "reply_rate": r["reply_rate"], "bounce_rate": r["bounce_rate"],
        "ooo_rate": None,
        # NOTE: `placement` is deliberately omitted — it's written separately by
        # health_placement.py (SmartLead warmup landing). Sending placement:None
        # here would clobber it on every snapshot (merge-duplicates only touches
        # the columns present in the payload).
        "sent": r["sent"], "smtp_ok": r["smtp_ok"],
        "warmup_reputation": r["warmup_reputation"],
    } for r in fleet]
    store.upsert_health_daily(daily_rows)

    # 2) re-score each inbox off its trailing 3-day window
    #    (one bulk fetch of the last week, not one query per inbox)
    from datetime import timedelta
    since = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
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
        sig = hm.rolling(rows, days=3)
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
    })

    return {"ok": True, "date": today, "inboxes": len(status_rows), "counts": counts}


if __name__ == "__main__":
    import json
    print(json.dumps(snapshot_daily(), indent=2, default=str))
