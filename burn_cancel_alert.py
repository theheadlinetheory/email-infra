"""Slack: which burned domains to cancel, and which burned inboxes to swap.

The Zapmail watcher only speaks about domains someone has ALREADY scheduled for
removal. Nothing told anyone which domains should be scheduled in the first
place: that decision lived on the dashboard's Burned-domains view and nowhere
else, so when the burn list froze (2026-10-24..26) the silence looked like
"nothing to cancel".

Rule (Tim, 2026-10-27): a domain with 2 or 3 of its mailboxes burned is a cancel
candidate; a domain with 1 burned keeps the domain and swaps that inbox.

Posts when the candidate set changes, and again weekly while it stands, so an
unchanged list does not repeat every six hours.
"""
from __future__ import annotations

import hashlib
from datetime import date

import db as store

STATE_KEY = "burn_cancel_alert_last"     # {posted_on, fingerprint}
REPEAT_DAYS = 7
DASHBOARD = "https://email-infra.vercel.app/dashboard"


def build(view: dict | None = None) -> dict:
    if view is None:
        import health_domains as hd
        view = hd.domain_view()
    cancel, swap = [], []
    for d in view.get("domains") or []:
        if d.get("scheduled"):
            continue
        row = {"domain": d["domain"], "burned": d["counts"]["burned"],
               "total": d["total"], "clients": d.get("clients") or [],
               "collateral": d["counts"].get("collateral", 0)}
        (cancel if row["burned"] >= 2 else swap).append(row)
    data, _ = store.cache_get("health_fleet")
    pipeline = (data or {}).get("pipeline") or {} if isinstance(data, dict) else {}
    return {"cancel": cancel, "swap": swap,
            "newest_data_day": pipeline.get("newest_data_day"),
            "fetch_error": pipeline.get("fetch_error")}


def format_alert(board: dict) -> str | None:
    cancel, swap = board["cancel"], board["swap"]
    if not cancel and not swap:
        return None
    lines = [f":fire: *Burned inboxes — decision needed* "
             f"(data through {board.get('newest_data_day') or '?'})",
             "_Burned = 0 replies in the latest 200 sends (≤1 in 400) or bounce ≥3%._", ""]
    if board.get("fetch_error"):
        lines += [":warning: Smartlead did not answer this run; scored from stored history.", ""]
    if cancel:
        lines.append(f"*Cancel these {len(cancel)} domains* (2+ of 3 mailboxes burned):")
        by_client: dict[str, list] = {}
        for r in cancel:
            by_client.setdefault(", ".join(r["clients"]) or "?", []).append(r)
        for client in sorted(by_client):
            doms = ", ".join(f"{r['domain']} ({r['burned']}/{r['total']}"
                             + (f", {r['collateral']} healthy still sending" if r["collateral"] else "")
                             + ")" for r in by_client[client])
            lines.append(f"• *{client}*: {doms}")
        lines.append("")
    if swap:
        lines.append(f"*Swap the burned inbox, keep the domain* — {len(swap)} domains "
                     f"with 1 burned. Full list on the dashboard.")
    lines += ["", f"Nothing is cancelled or swapped automatically. Decide on <{DASHBOARD}|the "
                  "Inboxes tab → Burned domains>."]
    return "\n".join(lines)


def _fingerprint(board: dict) -> str:
    key = "|".join(sorted(f"{r['domain']}:{r['burned']}" for r in board["cancel"]))
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def post(board: dict | None = None, dry_run: bool = True, today: date | None = None) -> dict:
    board = board if board is not None else build()
    text = format_alert(board)
    if not text:
        return {"posted": False, "reason": "nothing burned"}
    if dry_run:
        return {"dry_run": True, "text": text}
    today = today or date.today()
    fp = _fingerprint(board)
    last = store.get_state(STATE_KEY) or {}
    if last.get("fingerprint") == fp and last.get("posted_on"):
        age = (today - date.fromisoformat(last["posted_on"])).days
        if age < REPEAT_DAYS:
            return {"posted": False, "reason": "unchanged since last post", "suppressed": True}
    import zapmail_removals as zr
    via = zr.post_slack(text)
    if via == "webhook":
        store.set_state(STATE_KEY, {"posted_on": today.isoformat(), "fingerprint": fp})
    return {"posted": via == "webhook", "via": via,
            "cancel": len(board["cancel"]), "swap": len(board["swap"])}
