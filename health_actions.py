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
    """email(lower) -> {mailbox_id, subscription_id} from Zapmail subscriptions.

    Mailbox object keys vary by Zapmail API version — we try the common ones.
    Confirm against one live record and pin the key (HEALTH_V1.md, step 6)."""
    idx: dict[str, dict] = {}
    subs = zm.zm_get_subscriptions()
    sub_list = subs if isinstance(subs, list) else subs.get("data", []) if isinstance(subs, dict) else []
    for s in sub_list:
        sub_id = s.get("id") or s.get("subscriptionId")
        try:
            mbs = zm.zm_get_subscription_mailboxes(sub_id)
        except Exception:
            continue
        mb_list = mbs if isinstance(mbs, list) else mbs.get("data", []) if isinstance(mbs, dict) else []
        for mb in mb_list:
            email = (mb.get("email") or mb.get("emailAddress")
                     or mb.get("username") or mb.get("mailbox") or "")
            if email:
                idx[email.lower()] = {
                    "mailbox_id": mb.get("id") or mb.get("mailboxId"),
                    "subscription_id": sub_id,
                }
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


def schedule_removal(emails: list[str], dry_run: bool = True) -> dict:
    """Schedule burned mailboxes for removal at next renewal.

    dry_run=True  -> returns the plan, changes nothing (default).
    dry_run=False -> calls Zapmail remove-on-renewal for the resolved mailboxes.
    """
    plan = plan_removal(emails)
    ids = plan["mailbox_ids"]
    if dry_run:
        return {"dry_run": True, "would_remove": len(ids), **plan}

    if not ids:
        return {"dry_run": False, "removed": 0, "error": "no mailbox ids resolved", **plan}

    result = zm.zm_remove_on_renewal(ids)
    # audit trail (reuses the existing monitor_log table)
    try:
        store.log_monitor_event("health_remove_on_renewal", {
            "emails": [r["email"] for r in plan["resolved"]],
            "mailbox_ids": ids, "zapmail_response": result,
        })
    except Exception:
        pass
    return {"dry_run": False, "removed": len(ids), "zapmail": result, **plan}


def burned_emails() -> list[str]:
    """Current burned inboxes from the last snapshot."""
    return [r["email"] for r in store.get_health_status_all() if r.get("status") == "burned"]
