"""Health V1 — replacement tracking.

Turns "this inbox is burned" into a tracked replacement job with an enforced
2-week warmup, so a replacement can't be (a) forgotten mid-flight or (b) put
into a campaign before it's warmed.

Lifecycle:  flagged -> warming -> (14 days) -> ready -> swapped   (or cancelled)
  * flagged : we've decided to replace it; replacement not started yet.
  * warming : a fresh inbox is provisioned and warming (warming_started_at set).
              It CANNOT send during this window.
  * ready   : computed — warming_started_at + WARMUP_DAYS has elapsed.
  * swapped : replacement assigned to the campaign; old inbox can now be cancelled.

Stored as a JSON list in the `state` table (key `health_replacements`) — no new
migration. Uses only stdlib datetime (server-side, not the workflow sandbox).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import db as store

WARMUP_DAYS = 14
STATE_KEY = "health_replacements"
_ACTIVE = ("flagged", "warming", "ready", "reserved")


def _load() -> dict:
    return store.get_state(STATE_KEY) or {"jobs": []}


def _save(st: dict) -> None:
    store.set_state(STATE_KEY, st)


def _annotate(j: dict) -> dict:
    """Add computed warmup countdown / readiness to a job."""
    ws = j.get("warming_started_at")
    if ws:
        ready = datetime.fromisoformat(ws) + timedelta(days=WARMUP_DAYS)
        j["ready_at"] = ready.strftime("%Y-%m-%d")
        j["days_left"] = max(0, (ready - datetime.now()).days + (1 if ready > datetime.now() else 0))
        j["is_ready"] = datetime.now() >= ready
        if j["status"] == "warming" and j["is_ready"]:
            j["status"] = "ready"
    else:
        j["ready_at"], j["days_left"], j["is_ready"] = None, None, False
    return j


def list_jobs() -> list[dict]:
    return [_annotate(j) for j in _load().get("jobs", [])]


def reserve_summary() -> dict:
    """How many warmed reserve inboxes are ready to deploy right now.
    Reads generic groups from the overview cache; 'ready' = warmed >= WARMUP_DAYS.
    'available' subtracts inboxes already claimed by active reserved jobs."""
    ov, _ = store.cache_get("overview_v2")
    ready, groups = 0, []
    for g in (ov or {}).get("generic_groups", []):
        n = len(g.get("account_details", []))
        wd = g.get("warmup_days")
        if n and wd is not None and wd >= WARMUP_DAYS:
            ready += n
            groups.append({"name": g.get("name"), "count": n})
    claimed = sum(1 for j in _load().get("jobs", []) if j["status"] == "reserved")
    return {"ready": ready, "claimed": claimed,
            "available": max(0, ready - claimed), "groups": groups}


def pick_reserve_inbox(exclude=None) -> dict | None:
    """Pick a specific warmed reserve inbox (email + SmartLead account id) that
    isn't already claimed by another active job."""
    exclude = exclude or set()
    ov, _ = store.cache_get("overview_v2")
    for g in (ov or {}).get("generic_groups", []):
        wd = g.get("warmup_days")
        if wd is None or wd < WARMUP_DAYS:
            continue
        for ad in g.get("account_details", []):
            em = ad.get("email")
            if em and em not in exclude and ad.get("id"):
                return {"email": em, "account_id": ad["id"], "group": g.get("name")}
    return None


def create_jobs(emails: list[str]) -> dict:
    """Flag burned inboxes for replacement (idempotent on active jobs)."""
    st = _load()
    active = {j["old_email"] for j in st["jobs"] if j["status"] in _ACTIVE}
    status_by = {r["email"]: r for r in store.get_health_status_all()}
    made = 0
    now = datetime.now().strftime("%Y-%m-%d")
    next_id = max([j.get("id", 0) for j in st["jobs"]], default=0)
    for email in emails:
        if email in active:
            continue
        r = status_by.get(email, {})
        next_id += 1
        st["jobs"].append({
            "id": next_id,
            "old_email": email,
            "old_domain": r.get("domain", email.split("@")[-1] if "@" in email else ""),
            "client": r.get("client"),
            "group_letter": r.get("group_letter"),
            "campaigns": r.get("campaigns") or [],
            "reason": "; ".join(r.get("reasons") or []) or f"score {r.get('score')}",
            "status": "flagged",
            "new_domain": None,
            "flagged_at": now,
            "warming_started_at": None,
            "swapped_at": None,
        })
        made += 1
    _save(st)
    return {"created": made, "skipped": len(emails) - made}


def advance(job_id: int, action: str, new_domain: str | None = None, confirm: bool = False) -> dict:
    """Move a job forward. action: warm | reserve | swap | cancel.
    For swap on a reserved job, dry_run (confirm=False) returns the SmartLead
    re-tag plan; confirm=True executes the re-tag and finalizes the swap."""
    st = _load()
    job = next((j for j in st["jobs"] if j.get("id") == job_id), None)
    if not job:
        return {"error": "job not found"}

    if action == "warm":
        job["status"] = "warming"
        job["warming_started_at"] = datetime.now().isoformat()
        if new_domain:
            job["new_domain"] = new_domain
    elif action == "reserve":
        # instant path: draw a specific pre-warmed inbox from the generic reserve
        if reserve_summary()["available"] <= 0:
            return {"error": "no ready reserve inboxes available - warm a new one instead"}
        used = {j.get("reserve_email") for j in st["jobs"] if j.get("reserve_email")}
        pick = pick_reserve_inbox(used)
        if not pick:
            return {"error": "could not pick a reserve inbox"}
        job["status"] = "reserved"
        job["reserve_email"] = pick["email"]
        job["reserve_account_id"] = pick["account_id"]
        job["reserve_source"] = pick["group"]
        job["reserved_at"] = datetime.now().strftime("%Y-%m-%d")
    elif action == "swap":
        _annotate(job)
        if job["status"] != "reserved" and not job.get("is_ready"):
            return {"error": f"not warmed yet - {job.get('days_left')} day(s) left of the {WARMUP_DAYS}-day warmup"}
        retag = None
        if job.get("reserve_account_id") and job.get("client"):
            import health_smartlead as hsl
            retag = hsl.reassign(job["reserve_account_id"], job.get("reserve_email"),
                                 job["client"], job.get("group_letter") or "A",
                                 dry_run=not confirm)
            job["retag"] = retag
        # dry-run: show the re-tag plan, don't finalize the swap yet
        if not confirm and retag is not None and not retag.get("error"):
            _save(st)
            return {"ok": True, "dry_run": True, "job": _annotate(job), "retag": retag}
        job["status"] = "swapped"
        job["swapped_at"] = datetime.now().strftime("%Y-%m-%d")
        _save(st)
        return {"ok": True, "job": _annotate(job), "retag": retag}
    elif action == "cancel":
        job["status"] = "cancelled"
    else:
        return {"error": f"unknown action {action}"}
    _save(st)
    return {"ok": True, "job": _annotate(job)}
