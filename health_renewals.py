"""Health V1 - renewal tracking + renew/replace decisions.

Zapmail doesn't expose a per-mailbox renewal date or a mailbox->subscription
link, but it bills monthly and every mailbox carries a `createdAt`. So a
mailbox's next renewal is the monthly anniversary of its creation. We join that
with each inbox's health status to drive the renew-vs-drop decision:

  * healthy / watch      -> renew (keep)
  * idle                 -> cancel (not sending)
  * burned + renews soon -> CANCEL NOW (avoid the imminent ~$3 charge)
  * burned + just renewed-> already paid; use until replacement, drop before next
  * at-risk near renewal -> decide now

Zapmail mailbox list is cached in the `state` table (refreshed on demand).
"""

from __future__ import annotations

import calendar
import os
from datetime import datetime

import db as store

COST_PER_INBOX = 3          # ~$3/mo Zapmail Google Workspace mailbox
CACHE_KEY = "zapmail_mailboxes"


def _headers():
    return {"Content-Type": "application/json",
            "x-auth-zapmail": os.environ.get("ZAPMAIL_API_KEY", "").strip(),
            "x-service-provider": "GOOGLE"}


def fetch_mailboxes() -> dict:
    """email(lower) -> {created_at, domain, status, domain_expire} from Zapmail."""
    import requests
    idx, page = {}, 1
    while True:
        r = requests.get(f"https://api.zapmail.ai/api/v2/domains?page={page}&limit=100",
                         headers=_headers(), timeout=30)
        if r.status_code != 200:
            break
        data = r.json().get("data", {})
        for dom in data.get("domains", []):
            dname = dom.get("domain", "")
            for mb in (dom.get("mailboxes") or []):
                u = mb.get("username")
                if u and dname:
                    idx[f"{u}@{dname}".lower()] = {
                        "created_at": mb.get("createdAt"),
                        "domain": dname,
                        "status": mb.get("status"),
                        "domain_expire": (dom.get("expireOn") or "")[:10],
                    }
        if page >= data.get("totalPages", 1):
            break
        page += 1
    store.cache_set(CACHE_KEY, {"mailboxes": idx, "fetched_at": datetime.now().isoformat()})
    return idx


def _mailboxes(refresh: bool = False) -> dict:
    if not refresh:
        cached, _ = store.cache_get(CACHE_KEY)
        if cached and cached.get("mailboxes"):
            return cached["mailboxes"]
    return fetch_mailboxes()


def est_renewal(created_iso: str | None):
    """Next monthly anniversary of a mailbox's creation. Returns (date_str, days_until)."""
    if not created_iso:
        return None, None
    try:
        c = datetime.fromisoformat(created_iso.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None, None
    now = datetime.now()
    r = c
    while r < now:
        m = r.month + 1
        y = r.year + (1 if m > 12 else 0)
        m = (m - 1) % 12 + 1
        day = min(c.day, calendar.monthrange(y, m)[1])
        r = r.replace(year=y, month=m, day=day)
    return r.strftime("%Y-%m-%d"), (r - now).days


def _decide(status: str, days):
    """Return (decision text, css class) for the renew/replace call."""
    if status in ("healthy", "watch"):
        return "renew - keep", "good"
    if status == "idle":
        return "cancel - not sending", "crit"
    if status == "burned":
        if days is None:
            return "cancel - burned", "crit"
        if days <= 5:
            return f"CANCEL NOW - renews in {days}d", "crit"
        return f"paid {days}d - use til replacement, then drop", "serious"
    if status == "at_risk":
        if days is not None and days <= 5:
            return f"decide now - renews in {days}d", "serious"
        return "watch near renewal", "serious"
    if status == "warming":
        return "keep (warming)", "mut"
    return "-", "mut"


def build_tracking(refresh: bool = False) -> dict:
    mbx = _mailboxes(refresh)
    status = {r["email"]: r for r in store.get_health_status_all()}
    rows = []
    for email, m in mbx.items():
        rd, days = est_renewal(m.get("created_at"))
        h = status.get(email, {})
        st = h.get("status", "unknown")
        decision, dc = _decide(st, days)
        rows.append({
            "email": email, "domain": m.get("domain"),
            "client": h.get("client"), "campaigns": h.get("campaigns") or [],
            "status": st, "score": h.get("score"),
            "bounce_3d": h.get("bounce_3d"), "reply_3d": h.get("reply_3d"),
            "renewal_date": rd, "days_until": days,
            "cost": COST_PER_INBOX,
            "domain_expire": m.get("domain_expire"),
            "decision": decision, "decision_class": dc,
        })
    # urgency sort: soonest renewal among burned/idle first
    def _key(r):
        pri = {"burned": 0, "idle": 1, "at_risk": 2}.get(r["status"], 5)
        return (pri, r["days_until"] if r["days_until"] is not None else 999)
    rows.sort(key=_key)
    # summary: money renewing in the next 7 days on burned/idle inboxes
    soon_waste = sum(r["cost"] for r in rows
                     if r["status"] in ("burned", "idle")
                     and r["days_until"] is not None and r["days_until"] <= 7)
    return {"rows": rows, "count": len(rows),
            "renewing_waste_7d": soon_waste,
            "fetched_at": (store.cache_get(CACHE_KEY)[0] or {}).get("fetched_at")}
