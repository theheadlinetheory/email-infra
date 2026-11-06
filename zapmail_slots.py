"""How many Zapmail mailbox slots are free, from the /v2/workspaces `currentWorkspace` block.

Use the workspace-wide counters, not the Google ones. Add-on slots bought through
/v2/wallet/buy-addon-mailboxes count only in `totalMailboxesPurchased`, yet Google mailboxes
sit on them: on 2026-11-07 the workspace showed 1,395 Google slots purchased against 1,420
Google mailboxes assigned (−25 "free"), while the totals were 1,443 purchased and 1,432
assigned (11 free). The Google-only maths made every buy path purchase ~25 slots it did not
need. PC Landscape's contact@ alone over-bought 20–25.
"""


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def free_slots(ws: dict) -> int | None:
    """Free slots in the workspace, or None if the block carries no counts."""
    ws = ws or {}
    bought, used = _int(ws.get("totalMailboxesPurchased")), _int(ws.get("assignedMailboxesCount"))
    if bought is None or used is None:
        # Older payloads only had the Google counters.
        bought = _int(ws.get("totalMailboxesPurchasedGoogle"))
        used = _int(ws.get("assignedMailboxesCountGoogle"))
    if bought is None or used is None:
        return None
    return bought - used
