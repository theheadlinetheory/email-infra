"""Health V1 — the scoring brain.

Turns a small set of deliverability signals into ONE 0-100 score + a status,
using the rules agreed with Aidan (2026-07-10 call):

  * Reply rate is the anchor (highest weight).
  * Bounce rate is #2.
  * SmartLead warmup "reputation" is DROPPED from the score (info-only) — it only
    reflects warmup-pool behaviour and is not reliable.
  * Out-of-office (OOO) reply rate is a real inbox-placement proxy (weight 15%).
  * Inbox placement (from seed tests) is used ONLY when we actually have it; in
    V1 we usually don't, so its weight is redistributed across the other signals.
  * Metrics are read over a 3-DAY window — long enough to ignore one noisy day,
    short enough to catch a real dip fast.
  * A campaign/inbox with too little recent volume is NOT scored (its rates are
    statistical noise — e.g. 1 bounce out of 17 sends = 5.6%).
  * Any single critical metric can TRIP the status on its own, even if the blended
    score looks fine (this is what made the old weighted-only score confusing).

Pure module: no network, no DB. Unit-testable. Tunables live in DEFAULT_CONFIG
and can be overridden per call (later: from the inbox_health_config table).
"""

from __future__ import annotations

# --- statuses -------------------------------------------------------------
HEALTHY      = "healthy"       # leave it
WATCH        = "watch"         # keep an eye — early wobble
AT_RISK      = "at_risk"       # act soon — pull back to warming to recover
BURNED       = "burned"        # cooked — remove / cancel
WARMING      = "warming"       # in warmup period, not judged on production rates
INSUFFICIENT = "insufficient"  # some sends, but too few to judge reliably
IDLE         = "idle"          # zero sends — not doing anything (cancel/assign)

STATUS_LABEL = {
    HEALTHY: "Healthy", WATCH: "Watch", AT_RISK: "At-risk",
    BURNED: "Burned", WARMING: "Warming", INSUFFICIENT: "Low volume", IDLE: "Idle",
}
STATUS_RANK = {HEALTHY: 0, WATCH: 1, AT_RISK: 2, BURNED: 3,
               WARMING: -1, INSUFFICIENT: -2, IDLE: -3}


DEFAULT_CONFIG = {
    # weights — only the signals we actually have are used; the rest are
    # redistributed proportionally so the weights of present signals always
    # sum to 1.0.  Reputation is intentionally absent.
    "weights": {"reply": 0.35, "bounce": 0.30, "ooo": 0.15, "placement": 0.20},

    # sub-score anchor points (value that scores 100  ->  value that scores 0)
    "reply_full": 2.0, "reply_zero": 0.3,      # reply% : >=2 great, <=0.3 dead
    "bounce_full": 1.0, "bounce_zero": 2.5,    # bounce%: <=1 great, >=2.5 bad
    "ooo_full": 4.0, "ooo_zero": 0.3,          # ooo%   : >=4 great, ~0 = not landing
    "placement_full": 90.0, "placement_zero": 40.0,

    # hard single-metric tripwires (override the blended score)
    "bounce_burn": 3.0,        # 3-day bounce >= this  -> BURNED
    "bounce_risk": 2.5,        # 3-day bounce >= this  -> at least AT_RISK
    "reply_dead": 0.3,         # 3-day reply  <= this (with volume) -> BURNED
    "reply_risk": 0.8,         # 3-day reply  <= this  -> at least AT_RISK
    "placement_burn": 45.0,    # placement  <  this   -> BURNED (when known)
    "placement_risk": 65.0,    # placement  <  this   -> at least AT_RISK (when known)

    # status bands from the blended score
    "band_healthy": 80, "band_watch": 60, "band_atrisk": 40,

    # data-quality gate
    "min_sent_3d": 30,         # fewer sends than this over 3 days -> INSUFFICIENT
    "warmup_days": 14,         # inboxes younger than this are WARMING, not scored

    # trend: reply drop (pts) vs the prior 3-day window that pulls a HEALTHY
    # inbox down into WATCH even before absolute thresholds are hit
    "trend_watch_drop": 0.6,
}


def _lerp_score(value, full, zero):
    """Map a metric value onto 0-100 given its 'full marks' and 'zero' anchors.
    Handles both directions (full>zero for reply/ooo/placement, full<zero for
    bounce where a *lower* value is better)."""
    if value is None:
        return None
    if full == zero:
        return 100.0
    t = (value - zero) / (full - zero)
    return max(0.0, min(100.0, t * 100.0))


def sub_scores(sig, cfg):
    """Per-metric 0-100 sub-scores for whatever signals are present."""
    out = {}
    if sig.get("reply") is not None:
        out["reply"] = _lerp_score(sig["reply"], cfg["reply_full"], cfg["reply_zero"])
    if sig.get("bounce") is not None:
        out["bounce"] = _lerp_score(sig["bounce"], cfg["bounce_full"], cfg["bounce_zero"])
    if sig.get("ooo") is not None:
        out["ooo"] = _lerp_score(sig["ooo"], cfg["ooo_full"], cfg["ooo_zero"])
    if sig.get("placement") is not None:
        out["placement"] = _lerp_score(sig["placement"], cfg["placement_full"], cfg["placement_zero"])
    return out


def blended_score(subs, cfg):
    """Weighted blend over ONLY the present sub-scores (weights renormalised)."""
    weights = cfg["weights"]
    present = {k: weights[k] for k in subs if k in weights}
    total_w = sum(present.values())
    if total_w <= 0:
        return None
    return round(sum(subs[k] * (present[k] / total_w) for k in present))


def score_inbox(signals, cfg=None):
    """Score one inbox.

    signals: {
        "reply": float|None,      # 3-day avg reply rate %
        "bounce": float|None,     # 3-day avg bounce rate %
        "ooo": float|None,        # 3-day avg out-of-office reply rate % (optional)
        "placement": float|None,  # inbox placement % from seed test (optional)
        "sent_3d": int,           # total sends over the window (volume gate)
        "age_days": int|None,     # inbox age; < warmup_days -> WARMING
        "reply_prev": float|None, # prior-window reply for trend (optional)
        "in_warmup": bool|None,   # explicit warmup flag (optional override)
    }

    returns: {score, status, label, subscores, reasons[]}
    """
    cfg = {**DEFAULT_CONFIG, **(cfg or {})}
    reasons = []

    # --- warmup: don't judge a warming inbox on production rates ---
    age = signals.get("age_days")
    if signals.get("in_warmup") or (age is not None and age < cfg["warmup_days"]):
        return {"score": None, "status": WARMING, "label": STATUS_LABEL[WARMING],
                "subscores": {}, "reasons": ["in warmup period"]}

    # --- idle: sent nothing at all — can't be measured, and may be paid-for waste ---
    sent = signals.get("sent_3d") or 0
    if sent == 0:
        return {"score": None, "status": IDLE, "label": STATUS_LABEL[IDLE],
                "subscores": {}, "reasons": ["no sends — idle"]}

    # --- data-quality gate: some volume, but too little to trust the rates ---
    if sent < cfg["min_sent_3d"]:
        return {"score": None, "status": INSUFFICIENT, "label": STATUS_LABEL[INSUFFICIENT],
                "subscores": {}, "reasons": [f"only {sent} sends in 3d"]}

    subs = sub_scores(signals, cfg)
    score = blended_score(subs, cfg)
    band = (HEALTHY if score >= cfg["band_healthy"]
            else WATCH if score >= cfg["band_watch"]
            else AT_RISK if score >= cfg["band_atrisk"]
            else BURNED)

    # --- single-metric tripwires: escalate status regardless of the blend ---
    trip = HEALTHY
    bounce = signals.get("bounce")
    reply = signals.get("reply")
    placement = signals.get("placement")

    # SMTP disconnected = the inbox physically can't send — surface it
    if signals.get("smtp_ok") is False:
        trip = _worse(trip, AT_RISK); reasons.append("SMTP disconnected")

    if bounce is not None and bounce >= cfg["bounce_burn"]:
        # High bounce only means a DEAD inbox if replies have also dried up.
        # High bounce + healthy reply = the inbox is landing; the bounces are a
        # bad lead list, not a burned inbox -> at-risk / check the list, don't cancel.
        if reply is not None and reply > cfg["reply_risk"]:
            trip = _worse(trip, AT_RISK)
            reasons.append(f"bounce {bounce:.1f}% but reply {reply:.2f}% - check lead list")
        else:
            trip = BURNED
            reasons.append(f"bounce {bounce:.1f}% >= {cfg['bounce_burn']}% + low reply")
    elif bounce is not None and bounce >= cfg["bounce_risk"]:
        trip = _worse(trip, AT_RISK); reasons.append(f"bounce {bounce:.1f}% >= {cfg['bounce_risk']}%")

    if reply is not None and reply <= cfg["reply_dead"]:
        trip = BURNED; reasons.append(f"reply {reply:.2f}% <= {cfg['reply_dead']}%")
    elif reply is not None and reply <= cfg["reply_risk"]:
        trip = _worse(trip, AT_RISK); reasons.append(f"reply {reply:.2f}% <= {cfg['reply_risk']}%")

    if placement is not None and placement < cfg["placement_burn"]:
        trip = BURNED; reasons.append(f"placement {placement:.0f}% < {cfg['placement_burn']}%")
    elif placement is not None and placement < cfg["placement_risk"]:
        trip = _worse(trip, AT_RISK); reasons.append(f"placement {placement:.0f}% < {cfg['placement_risk']}%")

    status = _worse(band, trip)

    # --- trend: a still-passing inbox that's dropping fast -> WATCH ---
    prev = signals.get("reply_prev")
    if status == HEALTHY and reply is not None and prev is not None:
        if (prev - reply) >= cfg["trend_watch_drop"]:
            status = WATCH
            reasons.append(f"reply dropping ({prev:.1f}->{reply:.1f})")

    if not reasons:
        reasons.append("within thresholds")

    return {"score": score, "status": status, "label": STATUS_LABEL[status],
            "subscores": {k: round(v) for k, v in subs.items()}, "reasons": reasons}


def _worse(a, b):
    """Return the more severe of two production statuses."""
    return a if STATUS_RANK.get(a, 0) >= STATUS_RANK.get(b, 0) else b


def rolling(daily_rows, days=3):
    """Aggregate the most recent `days` daily snapshot rows for one inbox into
    window signals. Each row: {date, reply_rate, bounce_rate, ooo_rate, sent}.
    Returns (window_signals, prev_window_reply) for trend.
    """
    rows = sorted([r for r in daily_rows if r.get("date")], key=lambda r: r["date"])
    recent = rows[-days:]
    prior = rows[-2 * days:-days]

    def _avg(rs, key):
        vals = [r[key] for r in rs if r.get(key) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    sig = {
        "reply": _avg(recent, "reply_rate"),
        "bounce": _avg(recent, "bounce_rate"),
        "ooo": _avg(recent, "ooo_rate"),
        "placement": _avg(recent, "placement"),
        "sent_3d": sum(int(r.get("sent") or 0) for r in recent),
        "reply_prev": _avg(prior, "reply_rate"),
    }
    return sig


if __name__ == "__main__":
    # quick self-test — run: python health_model.py
    cases = {
        "healthy":        {"reply": 4.3, "bounce": 1.1, "ooo": 4.6, "sent_3d": 210, "age_days": 60, "reply_prev": 4.2},
        "declining":      {"reply": 1.9, "bounce": 2.6, "ooo": 2.0, "sent_3d": 240, "age_days": 60, "reply_prev": 3.8},
        "burned_bounce":  {"reply": 0.2, "bounce": 9.2, "ooo": 0.1, "sent_3d": 300, "age_days": 90, "reply_prev": 2.4},
        "one_bad_metric": {"reply": 4.5, "bounce": 3.1, "ooo": 5.0, "sent_3d": 200, "age_days": 90, "reply_prev": 4.5},
        "low_volume":     {"reply": 5.6, "bounce": 5.6, "ooo": 0.0, "sent_3d": 17,  "age_days": 90},
        "warming":        {"reply": 1.0, "bounce": 0.4, "ooo": 3.0, "sent_3d": 60,  "age_days": 6},
        "watch_trend":    {"reply": 3.6, "bounce": 1.2, "ooo": 4.0, "sent_3d": 200, "age_days": 60, "reply_prev": 4.4},
    }
    for name, sig in cases.items():
        r = score_inbox(sig)
        print(f"{name:16} score={str(r['score']):>4}  {r['label']:8}  {'; '.join(r['reasons'])}")
