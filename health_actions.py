"""Health V1 — acting on burned inboxes.

Turns a "burned" decision into the cost-saving Zapmail action: schedule the
mailbox for removal at its next renewal (remove-on-renewal) — you stop paying
with zero wasted paid days, rather than deleting mid-cycle.

SAFETY: every function defaults to dry_run=True and returns a PLAN. Nothing is
cancelled until the caller passes dry_run=False (the UI does that only after an
explicit confirm). This is a hard-to-reverse, money-affecting, outward action.
"""

from __future__ import annotations

import db as store
import zapmail_ops as zm


def _mailbox_index() -> dict:
    """email(lower) -> {mailbox_id, domain} for every Zapmail mailbox.

    Zapmail nests mailboxes under domains: GET /v2/domains returns each domain
    with a `mailboxes` array of {id, username, ...}; the address is
    username@domain. (Verified against the live API 2026-07-14.)"""
    import os
    import requests
    key = os.environ.get("ZAPMAIL_API_KEY", "").strip()
    headers = {"Content-Type": "application/json",
               "x-auth-zapmail": key, "x-service-provider": "GOOGLE"}
    idx: dict[str, dict] = {}
    page = 1
    while True:
        r = requests.get(f"https://api.zapmail.ai/api/v2/domains?page={page}&limit=100",
                         headers=headers, timeout=30)
        if r.status_code != 200:
            break
        data = r.json().get("data", {})
        for dom in data.get("domains", []):
            dname = dom.get("domain", "")
            for mb in (dom.get("mailboxes") or []):
                u, mid = mb.get("username"), mb.get("id")
                if u and mid and dname:
                    idx[f"{u}@{dname}".lower()] = {"mailbox_id": mid, "domain": dname}
        if page >= data.get("totalPages", 1):
            break
        page += 1
    return idx


def plan_removal(emails: list[str]) -> dict:
    """Resolve emails -> Zapmail mailbox ids and report what would be cancelled.
    Read-only. Use this to render the confirm screen."""
    idx = _mailbox_index()
    resolved, unresolved = [], []
    for e in emails:
        hit = idx.get(e.lower())
        (resolved if hit and hit.get("mailbox_id") else unresolved).append(
            {"email": e, **(hit or {})})
    return {"resolved": resolved, "unresolved": unresolved,
            "mailbox_ids": [r["mailbox_id"] for r in resolved]}


def draft_zapmail_message(emails: list[str]) -> str:
    """A ready-to-send Zapmail support message requesting billing optimization
    for the given mailboxes (Zapmail adjusts billing on their backend once the
    mailboxes are scheduled for deletion)."""
    lines = "\n".join(emails)
    return (f"Hi Zapmail team — please optimize the billing on our subscriptions. "
            f"The following {len(emails)} mailbox(es) have been scheduled for deletion "
            f"from our dashboard:\n\n{lines}\n\nThank you!")


def schedule_removal(emails: list[str], dry_run: bool = True) -> dict:
    """Schedule mailboxes for removal at next renewal + draft the Zapmail message.

    dry_run=True  -> returns the plan + draft, changes nothing (preview).
    dry_run=False -> calls Zapmail remove-on-renewal, then returns the draft to send.
    Works on ANY selection, any time — no thresholds, no date gating.
    """
    plan = plan_removal(emails)
    resolved_emails = [r["email"] for r in plan["resolved"]]
    draft = draft_zapmail_message(resolved_emails or emails)

    if dry_run:
        return {"dry_run": True, "would_remove": len(plan["resolved"]), "draft": draft, **plan}

    # Zapmail exposes NO public API to delete/schedule mailboxes — per Zapmail
    # support, deletion is done from the Zapmail dashboard, then they optimize
    # billing from the message. So we record the intent and hand back the list +
    # message; the operator finishes in the Zapmail UI. (Don't fake a "removed".)
    try:
        store.log_monitor_event("health_flag_cancel", {
            "emails": resolved_emails, "mailbox_ids": plan["mailbox_ids"]})
    except Exception:
        pass
    return {"dry_run": False, "flagged": len(resolved_emails), "manual": True,
            "draft": draft, **plan}


def burned_emails() -> list[str]:
    """Current burned inboxes from the last snapshot."""
    return [r["email"] for r in store.get_health_status_all() if r.get("status") == "burned"]
