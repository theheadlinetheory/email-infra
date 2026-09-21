"""Is it safe to delete these Smartlead accounts?

Two separate questions, and they need different scans:

  "in use"   -- is the mailbox attached to an ACTIVE campaign? Only ACTIVE
                campaigns matter, so this is ~43 calls and runs in a request.

  "owns a    -- does the mailbox own a positive-reply thread? A PAUSED or
   reply"       COMPLETED campaign can hold one just as easily as an active
                one, so this question needs EVERY campaign -- ~415 calls plus
                a leads-export each, far more than a button can wait for.

An earlier version answered the second question with the first question's
data. Because the campaign list had already been filtered to ACTIVE, every
mailbox the reply-check could possibly name was one the in-use check had
already blocked: the loop cost ~39s and could not change a single verdict,
while a reply sitting on a paused campaign went unseen.

So the full scan runs nightly and lands here in the state store. The button
reads it. If it is missing or stale, `verdict()` says so and the caller
refuses to delete -- a scan that did not happen is not a clean scan.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta

import db as store

STATE_KEY = "rule10_safety"
MAX_AGE_H = 26          # a nightly job may slip; two nights in a row may not.
SL = "https://server.smartlead.ai/api/v1"


def _get(path: str, timeout: int = 60, **params):
    import requests as rq
    params["api_key"] = (os.environ.get("SMARTLEAD_API_KEY") or "").strip()
    for attempt in range(6):
        try:
            r = rq.get(f"{SL}{path}", params=params, timeout=timeout)
            if r.status_code == 200:
                return r
            if r.status_code == 429:
                time.sleep(10 * (attempt + 1))
                continue
        except Exception:
            pass
        time.sleep(4 * (attempt + 1))
    return None


def scan(candidates: list[str]) -> dict:
    """Walk EVERY campaign for these mailboxes. Returns a complete answer or an
    error -- never a short one."""
    want = {(e or "").strip().lower() for e in candidates if e}
    if not want:
        return {"ts": datetime.now().isoformat(), "candidates": [],
                "active": [], "positives": [], "campaigns_scanned": 0}

    r = _get("/campaigns")
    if r is None:
        return {"error": "campaign list unreadable"}
    camps = r.json() or []
    if not camps:
        return {"error": "campaign list came back empty"}

    # membership across ALL campaigns, active or not
    seen: dict[str, list[dict]] = {}
    failures = 0
    for c in camps:
        got = _get(f"/campaigns/{c['id']}/email-accounts")
        if got is None:
            failures += 1
            continue
        for a in got.json() or []:
            em = (a.get("from_email") or "").strip().lower()
            if em in want:
                seen.setdefault(em, []).append(
                    {"id": c.get("id"), "status": (c.get("status") or "").upper()})
        time.sleep(0.06)
    if failures:
        return {"error": f"{failures} of {len(camps)} campaigns could not be read"}

    active = sorted(em for em, cs in seen.items()
                    if any(c["status"] == "ACTIVE" for c in cs))

    # positive replies, across every campaign the candidates actually touch
    import health_positive as hp
    positives: dict[str, list] = {}
    for em, cs in seen.items():
        for c in cs:
            try:
                threads = hp.owned_positive_threads(em, c["id"])
            except Exception as exc:
                return {"error": f"positive-reply check failed for {em} "
                                 f"on campaign {c['id']}: {exc}"}
            for t in threads:
                positives.setdefault(em, []).append({**t, "campaign_id": c["id"]})

    return {
        "ts": datetime.now().isoformat(),
        "candidates": sorted(want),
        "active": active,
        "positives": sorted(positives),
        "positive_threads": positives,
        "campaigns_scanned": len(camps),
    }


def refresh(candidates: list[str]) -> dict:
    result = scan(candidates)
    if not result.get("error"):
        store.set_state(STATE_KEY, result)
    return result


def _age_hours(rec: dict) -> float | None:
    try:
        return (datetime.now() - datetime.fromisoformat(rec["ts"])).total_seconds() / 3600
    except Exception:
        return None


def verdict(candidates: list[str]) -> dict:
    """Blocked mailboxes from the last full scan, or an explanation of why
    there is no usable answer. Never guesses."""
    want = {(e or "").strip().lower() for e in candidates if e}
    rec = store.get_state(STATE_KEY) or {}
    if not rec or rec.get("error"):
        return {"stale": True,
                "reason": "no completed full-campaign scan on record"}

    age = _age_hours(rec)
    if age is None:
        return {"stale": True, "reason": "scan record has no readable timestamp"}
    if age > MAX_AGE_H:
        return {"stale": True,
                "reason": f"last full scan was {age:.0f}h ago (limit {MAX_AGE_H}h)"}

    # A candidate the scan never looked at has no verdict. Treat the whole
    # answer as unusable rather than silently clearing that one mailbox.
    covered = set(rec.get("candidates") or [])
    missing = sorted(want - covered)
    if missing:
        return {"stale": True,
                "reason": f"{len(missing)} mailbox(es) were not in the last scan",
                "missing": missing[:10]}

    return {
        "stale": False,
        "age_hours": round(age, 1),
        "active": sorted(set(rec.get("active") or []) & want),
        "positives": sorted(set(rec.get("positives") or []) & want),
        "positive_threads": {k: v for k, v in (rec.get("positive_threads") or {}).items()
                             if k in want},
        "campaigns_scanned": rec.get("campaigns_scanned"),
        "scanned_at": rec.get("ts"),
    }
