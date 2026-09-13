"""Chase the Zapmail billing optimisation until it is actually done.

Cancelling a mailbox does NOT reduce the Zapmail bill. Zapmail confirmed the
process on 2026-09-14:

    "We can see these mailboxes are already scheduled to be deleted from your
     workspace. Please let us know once they are removed and we can optimize
     the billing from your subscriptions."

So there are three steps, and only the first two were automated:

    1. schedule the removal                    -> zapmail_removals.cancel_domains
    2. notice the mailbox actually disappeared -> zapmail_removals.check_removals
    3. TELL ZAPMAIL, and confirm they did it   -> this module

Step 3 was the hole. `check_removals` posts one Slack message, sets
`notified: True`, and never mentions it again. Miss that single message — it
lands in a busy channel, on a weekend, during a holiday — and Zapmail is never
asked, the slots keep billing, and nothing anywhere says so. The failure is
silent and permanent, which is the worst shape a money bug can have.

This module treats a removal as unfinished until someone says otherwise. It
re-posts every day, escalating as the debt ages, and only stops when the
registry is marked acknowledged. 386 mailboxes were cancelled in one pass in
September — at $3/mo each that is $13,896/yr riding on a Slack message being
read.

Acknowledge from the CLI once Zapmail confirms:

    python billing_followup.py --ack-all
    python billing_followup.py --ack a@x.info b@y.info
"""

from __future__ import annotations

import datetime
import json
import os
from collections import defaultdict

COST_PER_MAILBOX = 3          # $/month per Zapmail mailbox slot
REG_KEY = "zm_removal_registry"

# Escalation ladder, in days since the mailbox actually went. Day 0 is
# `check_removals`' own alert; this module takes over from day 1.
NUDGE_AT = (1, 2, 3, 5, 7, 10, 14, 21, 30)

SLACK_WEBHOOK_VARS = ("SLACK_ZAPMAIL_WEBHOOK", "SLACK_INFRA_DECISIONS_WEBHOOK",
                      "SLACK_WEBHOOK_URL")
NOTIFY_MEMBER_IDS = ("U09B2673A4A",)   # Aidan Hutchinson


def _registry() -> dict:
    import db as store
    rows = store._request("GET", "/state",
                          params={"select": "data", "key": f"eq.{REG_KEY}"})
    if not rows:
        return {}
    data = json.loads(rows[0]["data"])
    return data.get("entries", data if isinstance(data, dict) else {})


def _save(entries: dict) -> None:
    import db as store
    store._request("POST", "/state",
                   json_body={"key": REG_KEY,
                              "data": json.dumps({"entries": entries,
                                                  "updated": _now()}),
                              "updated_at": _now()},
                   headers={"Prefer": "resolution=merge-duplicates"})


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _days_since(d: str | None) -> int | None:
    if not d:
        return None
    try:
        return (datetime.date.today() - datetime.date.fromisoformat(str(d)[:10])).days
    except ValueError:
        return None


def outstanding(entries: dict | None = None) -> dict:
    """Mailboxes that are GONE but whose slots Zapmail has not yet released.

    Keyed on `removed_date` rather than on the scheduling date: a mailbox that
    is merely scheduled is still doing its job and is still legitimately billed.
    The debt only starts when it actually disappears.
    """
    ent = entries if entries is not None else _registry()
    rows = []
    for email, info in (ent or {}).items():
        if not isinstance(info, dict):
            continue
        if not info.get("removed_date"):
            continue                      # still present — not billable waste yet
        if info.get("billing_acked"):
            continue                      # Zapmail has released it
        age = _days_since(info.get("removed_date"))
        rows.append({"email": email,
                     "domain": info.get("domain") or email.split("@")[-1],
                     "removed_date": info.get("removed_date"),
                     "days": age if age is not None else 0,
                     "source": info.get("source") or "unknown"})
    rows.sort(key=lambda r: (-r["days"], r["email"]))
    n = len(rows)
    oldest = rows[0]["days"] if rows else 0
    return {
        "rows": rows,
        "count": n,
        "monthly_cost": n * COST_PER_MAILBOX,
        "annual_cost": n * COST_PER_MAILBOX * 12,
        "oldest_days": oldest,
        # Money already thrown away while waiting, at ~30 days a billing cycle.
        "wasted_so_far": round(sum(r["days"] / 30.0 * COST_PER_MAILBOX for r in rows), 2),
    }


def should_nudge(board: dict, force: bool = False) -> bool:
    """Nudge on the ladder, and every day once the debt is over two weeks old.

    A reminder that arrives every single day from day one becomes wallpaper; one
    that never escalates gets ignored. So: sparse early, relentless late.
    """
    if not board["count"]:
        return False
    if force:
        return True
    d = board["oldest_days"]
    return d in NUDGE_AT or d > 14


def format_nudge(board: dict) -> str:
    men = " ".join(f"<@{u}>" for u in NOTIFY_MEMBER_IDS)
    s, d = board, board["oldest_days"]
    urgency = "🔴" if d >= 7 else ("🟠" if d >= 3 else "💸")
    head = (f"{men} {urgency} *{s['count']} removed mailboxes are still on the "
            f"Zapmail bill* — ${s['monthly_cost']}/mo, ${s['annual_cost']:,}/yr.")
    lines = [head, ""]
    if d >= 7:
        lines.append(f"⚠️ The oldest has been gone *{d} days* and we are still paying "
                     f"for it. About *${s['wasted_so_far']:,.0f}* burned waiting.")
        lines.append("")
    lines.append("These mailboxes no longer exist. Zapmail needs to be told so they "
                 "can reduce the subscription quantity — they do not do it automatically.")
    lines.append("")
    bydom = defaultdict(list)
    for r in s["rows"]:
        bydom[r["domain"]].append(r)
    for dom in sorted(bydom)[:20]:
        rs = bydom[dom]
        lines.append(f"• *{dom}* — {len(rs)} mbx, gone {rs[0]['removed_date']} "
                     f"({rs[0]['days']}d ago)")
    if len(bydom) > 20:
        lines.append(f"…and {len(bydom) - 20} more domains.")
    lines.append("")
    lines.append("_Once Zapmail confirms the billing is optimised, run "
                 "`python billing_followup.py --ack-all` so this stops._")
    return "\n".join(lines)


def post(board: dict, dry_run: bool = True, force: bool = False) -> dict:
    if not should_nudge(board, force=force):
        return {"posted": False,
                "reason": "nothing outstanding" if not board["count"]
                          else f"not a nudge day (oldest {board['oldest_days']}d)"}
    text = format_nudge(board)
    if dry_run:
        return {"dry_run": True, "text": text, "count": board["count"]}
    import requests
    for var in SLACK_WEBHOOK_VARS:
        hook = (os.environ.get(var) or "").strip()
        if not hook:
            continue
        try:
            r = requests.post(hook, json={"text": text}, timeout=10)
            if r.status_code in (200, 201):
                return {"posted": True, "via": var, "count": board["count"]}
        except requests.RequestException:
            pass
    return {"posted": False, "reason": "no working webhook", "text": text}


def acknowledge(emails=None) -> dict:
    """Mark slots as released by Zapmail. `emails=None` acknowledges everything."""
    ent = _registry()
    want = set(e.lower() for e in emails) if emails else None
    done = 0
    for email, info in ent.items():
        if not isinstance(info, dict) or not info.get("removed_date"):
            continue
        if info.get("billing_acked"):
            continue
        if want is not None and email.lower() not in want:
            continue
        info["billing_acked"] = True
        info["acked_at"] = _now()
        done += 1
    if done:
        _save(ent)
    return {"acknowledged": done, "still_outstanding": outstanding(ent)["count"]}


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--ack-all", action="store_true",
                   help="Zapmail has optimised the billing — stop chasing")
    p.add_argument("--ack", nargs="*", help="acknowledge specific mailboxes")
    p.add_argument("--send", action="store_true", help="post the nudge to Slack")
    p.add_argument("--force", action="store_true", help="post even on a non-nudge day")
    args = p.parse_args()
    try:
        import _envload
        _envload.load()
    except Exception:
        pass

    if args.ack_all:
        print(acknowledge())
    elif args.ack:
        print(acknowledge(args.ack))
    else:
        b = outstanding()
        print(f"removed but still billed: {b['count']} mailboxes "
              f"(${b['monthly_cost']}/mo, ${b['annual_cost']:,}/yr)")
        if b["count"]:
            print(f"oldest gone {b['oldest_days']} days ago; "
                  f"~${b['wasted_so_far']:,.0f} burned waiting")
            for r in b["rows"][:15]:
                print(f"   {r['removed_date']}  {r['days']:>3}d  {r['email']}")
        print()
        print(post(b, dry_run=not args.send, force=args.force))
