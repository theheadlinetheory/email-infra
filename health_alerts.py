"""Health V1 — "what just sank" deliverability alerts.

A notification-style feed for the TOP of the dashboard: which sending inboxes
have DROPPED in deliverability over the last few days, so a burned/declining
inbox jumps out instead of being buried in the 297-row backlog below.

It reads the same per-inbox daily history the scorer already loads and, for
each inbox that's actually sending, compares its earliest vs latest daily
reply/bounce in the window:
  * reply fell by >= REPLY_DROP points   -> sinking
  * bounce rose by >= BOUNCE_RISE points -> sinking
Declining inboxes are bubbled to the top; the rest of the burned/at-risk are
listed by severity so the panel is useful even before much history accrues.
(Trend sharpens as more daily snapshots land — needs ~2 windows to be rich.)

Pure function: no network, no DB. Called from the snapshot; result is cached
in health_fleet and rendered read-only by the UI.
"""

from __future__ import annotations

REPLY_DROP = 1.0     # reply% fall over the window to count as sinking
BOUNCE_RISE = 1.5    # bounce% rise over the window to count as sinking
_RANK = {"burned": 3, "at_risk": 2, "watch": 1}


def _series(rows, key):
    """Chronological non-null values of `key` across an inbox's daily rows."""
    rows = sorted((x for x in rows if x.get("date")), key=lambda x: x["date"])
    return [x[key] for x in rows if x.get(key) is not None]


def build_alerts(status_rows: list[dict], history: dict, limit: int = 20):
    """Return (alerts, summary).

    alerts: up to `limit` inboxes ranked worst-first, decliners on top, each with
            the reply/bounce change that triggered it.
    summary: fleet-wide counts {declining, burned, at_risk, watch}.
    """
    items = []
    declining_ct = 0
    for r in status_rows:
        stt = r.get("status")
        if stt not in _RANK:
            continue
        rows = history.get(r["email"], [])
        rr = _series(rows, "reply_rate")
        bb = _series(rows, "bounce_rate")
        d_reply = (rr[-1] - rr[0]) if len(rr) >= 2 else None
        d_bounce = (bb[-1] - bb[0]) if len(bb) >= 2 else None

        change = []
        declining = False
        if d_reply is not None and d_reply <= -REPLY_DROP:
            change.append(f"reply {rr[0]:.1f}%→{rr[-1]:.1f}%")
            declining = True
        if d_bounce is not None and d_bounce >= BOUNCE_RISE:
            change.append(f"bounce {bb[0]:.1f}%→{bb[-1]:.1f}%")
            declining = True

        # a 'watch' inbox is only alert-worthy if it's actually declining
        if stt == "watch" and not declining:
            continue
        if declining:
            declining_ct += 1

        severity = ((500 if declining else 0) + _RANK[stt] * 100
                    + max(0.0, -(d_reply or 0.0)) * 10
                    + max(0.0, (d_bounce or 0.0)) * 8)
        items.append({
            "email": r["email"], "domain": r.get("domain"), "client": r.get("client"),
            "status": stt, "reply": r.get("reply_3d"), "bounce": r.get("bounce_3d"),
            "placement": r.get("placement"),
            "change": "; ".join(change),
            "why": (r.get("reasons") or [None])[0],
            "declining": declining,
            "severity": round(severity, 1),
        })

    items.sort(key=lambda a: -a["severity"])
    summary = {
        "declining": declining_ct,
        "burned": sum(1 for r in status_rows if r.get("status") == "burned"),
        "at_risk": sum(1 for r in status_rows if r.get("status") == "at_risk"),
        "watch": sum(1 for r in status_rows if r.get("status") == "watch"),
    }
    return items[:limit], summary
