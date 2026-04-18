"""Inbox history — snapshot diffing and query helpers.

Handles periodic state comparison to detect external inbox changes.
Real-time event logging lives in db.py (log_inbox_event/log_inbox_events).
"""

from datetime import datetime

import db as store


def snapshot_inboxes(accounts: list[dict]) -> dict:
    """Compare current inbox state against last snapshot and log diffs.

    Args:
        accounts: list of SmartLead account dicts (must have id, client_id, from_email)

    Returns:
        {"diffs": int, "accounts": int}
    """
    if not accounts:
        return {"error": "No accounts provided"}

    current = {
        a["id"]: {"client_id": a.get("client_id"), "email": a.get("from_email", "")}
        for a in accounts
    }

    prev = store.get_state("inbox_snapshot") or {}
    prev_map = prev.get("accounts", {})

    events = []
    for acc_id, cur in current.items():
        acc_id_int = int(acc_id) if isinstance(acc_id, str) else acc_id
        old = prev_map.get(str(acc_id_int))
        if old is None:
            events.append({
                "account_id": acc_id_int, "email": cur["email"],
                "event_type": "snapshot_new",
                "old_value": None,
                "new_value": {"client_id": cur["client_id"]},
                "source": "snapshot",
            })
        elif old.get("client_id") != cur["client_id"]:
            events.append({
                "account_id": acc_id_int, "email": cur["email"],
                "event_type": "client_change",
                "old_value": {"client_id": old.get("client_id")},
                "new_value": {"client_id": cur["client_id"]},
                "source": "snapshot",
            })

    for acc_id_str, old in prev_map.items():
        if int(acc_id_str) not in current:
            events.append({
                "account_id": int(acc_id_str), "email": old.get("email", ""),
                "event_type": "snapshot_deleted",
                "old_value": {"client_id": old.get("client_id")},
                "new_value": None,
                "source": "snapshot",
            })

    if events:
        store.log_inbox_events(events)

    store.set_state("inbox_snapshot", {
        "accounts": {str(a_id): v for a_id, v in current.items()},
        "taken_at": datetime.now().isoformat(),
        "account_count": len(current),
    })

    return {"diffs": len(events), "accounts": len(current)}


def query_history(params: dict) -> list[dict]:
    """Parse HTTP query params and return inbox history.

    Supports: ?account_id=X for per-inbox, ?limit=N for global feed.
    """
    account_id = params.get("account_id", [None])[0]
    limit = int(params.get("limit", ["100"])[0])
    if account_id:
        return store.get_inbox_history(account_id=int(account_id), limit=limit)
    return store.get_inbox_history(limit=limit)
