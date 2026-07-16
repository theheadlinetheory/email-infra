"""Health V1 — inbox placement from SmartLead warmup.

SmartLead runs warmup on every inbox and its warmup-stats endpoint reports where
those warmup emails actually LAND: inbox vs spam. That's a real inbox-placement
signal — inbox_count / sent_count — not the opaque "reputation" score (which was
dropped from scoring). If even the friendly warmup pool can't keep an inbox out
of spam, its sending reputation is genuinely cooked.

    GET /api/v1/email-accounts/{id}/warmup-stats?api_key=...
      -> { sent_count, spam_count, inbox_count, warmup_email_received_count, ... }

There is NO bulk endpoint (the account-list warmup_details carries only zeros),
so this is one call per inbox, paced under SmartLead's 180/min REST limit
(~0.34s each). A full fleet sweep is ~8-9 min -> run it as a background script
or in <=150-inbox chunks (offset/limit) that fit a 60s serverless function.

We write only the `placement` column into today's inbox_health_daily rows via a
merge-duplicates upsert, so reply/bounce/sent are left untouched. The model
already weights placement 0.20 (renormalised when absent) and trips on it
(<45% -> burned, <65% -> at-risk), so once these numbers land, scoring uses them
with NO model change. The snapshot must run first (to create the day's rows);
placement then fills them in, and a re-run of the snapshot re-scores with it.

Volume gate: a placement % off < min_sent warmup sends is statistical noise and
is left as None (INSUFFICIENT for placement, not a false 0%).
"""

from __future__ import annotations

import os
import time
from datetime import datetime

import requests

import db as store

_BASE = "https://server.smartlead.ai/api/v1"
_PACE = 0.34            # seconds between calls -> ~176/min, under the 180/min cap
_MIN_SENT = 20          # fewer warmup sends than this -> placement is noise
_TIMEOUT = 20


def _key() -> str:
    return (os.environ.get("SMARTLEAD_API_KEY", "")
            or os.environ.get("SMARTLEAD_KEY", "")).strip()


def _fleet_accounts() -> list[tuple[str, int]]:
    """(email, smartlead_account_id) for every inbox in the overview cache."""
    ov, _ = store.cache_get("overview_v2")
    out: dict[str, int] = {}
    if not ov:
        return []
    for c in ov.get("clients", []):
        for letter in ("a", "b"):
            for ad in (c.get(f"group_{letter}") or {}).get("account_details", []):
                if ad.get("email") and ad.get("id"):
                    out[ad["email"]] = ad["id"]
    for section in ("generic_groups", "acquisition_groups"):
        for g in ov.get(section, []):
            for ad in g.get("account_details", []):
                if ad.get("email") and ad.get("id"):
                    out[ad["email"]] = ad["id"]
    return sorted(out.items(), key=lambda kv: kv[0])


def _to_int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def fetch_placement(account_id: int, key: str | None = None,
                    session: requests.Session | None = None) -> dict | None:
    """Warmup landing stats for one inbox. Returns {sent, spam, inbox, placement,
    spam_rate} or None on error. placement/spam_rate are None below the volume
    gate (too few warmup sends to trust)."""
    key = key or _key()
    if not key:
        return None
    getter = (session or requests).get
    try:
        r = getter(f"{_BASE}/email-accounts/{account_id}/warmup-stats",
                   params={"api_key": key}, timeout=_TIMEOUT)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        d = r.json()
    except ValueError:
        return None
    sent = _to_int(d.get("sent_count"))
    spam = _to_int(d.get("spam_count"))
    inbox = _to_int(d.get("inbox_count")) if d.get("inbox_count") is not None else max(0, sent - spam)
    if sent < _MIN_SENT:
        return {"sent": sent, "spam": spam, "inbox": inbox,
                "placement": None, "spam_rate": None}
    return {"sent": sent, "spam": spam, "inbox": inbox,
            "placement": round(inbox / sent * 100, 1),
            "spam_rate": round(spam / sent * 100, 1)}


def collect(emails: list[str] | None = None, today: str | None = None,
            offset: int = 0, limit: int | None = None,
            pace: float = _PACE) -> dict:
    """Sweep warmup placement for the fleet (or a slice) and write the `placement`
    column into today's inbox_health_daily rows.

    offset/limit slice the fleet so a 60s function can advance ~150 at a time:
      collect(offset=0, limit=150), collect(offset=150, limit=150), ...
    `emails` restricts to a specific set (ignores offset/limit).
    """
    key = _key()
    if not key:
        return {"ok": False, "error": "no SMARTLEAD_API_KEY"}
    today = today or datetime.now().strftime("%Y-%m-%d")

    fleet = _fleet_accounts()
    total_fleet = len(fleet)
    if emails:
        want = set(emails)
        targets = [(e, a) for e, a in fleet if e in want]
    else:
        targets = fleet[offset: (offset + limit) if limit else None]

    sess = requests.Session()
    rows, dist = [], {"inbox_ok": 0, "at_risk": 0, "burned": 0, "low_vol": 0, "no_data": 0}
    scored = 0
    for email, aid in targets:
        pl = fetch_placement(aid, key, sess)
        if pl is None:
            dist["no_data"] += 1
        elif pl["placement"] is None:
            dist["low_vol"] += 1
            rows.append({"email": email, "date": today, "placement": None})
        else:
            p = pl["placement"]
            dist["burned" if p < 45 else "at_risk" if p < 65 else "inbox_ok"] += 1
            rows.append({"email": email, "date": today, "placement": p})
            scored += 1
        time.sleep(pace)

    # merge-duplicates: updates ONLY the placement column on existing day rows
    store.upsert_health_daily(rows)
    try:
        store.log_monitor_event("health_placement", {
            "checked": len(targets), "scored": scored, "dist": dist,
            "offset": offset, "limit": limit})
    except Exception:
        pass
    return {"ok": True, "date": today, "fleet_total": total_fleet,
            "checked": len(targets), "written": len(rows), "scored": scored,
            "distribution": dist,
            "next_offset": (offset + len(targets)) if not emails and (offset + len(targets)) < total_fleet else None}


if __name__ == "__main__":
    import json
    import sys
    # optional args: [offset] [limit]
    off = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    lim = int(sys.argv[2]) if len(sys.argv) > 2 else None
    print(json.dumps(collect(offset=off, limit=lim), indent=2, default=str))
