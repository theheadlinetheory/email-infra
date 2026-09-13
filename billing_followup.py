"""Verify Zapmail is billing for the mailboxes that actually exist.

Cancelling a mailbox does not reduce the Zapmail bill on its own. Zapmail
confirmed the process on 2026-09-14:

    "We can see these mailboxes are already scheduled to be deleted from your
     workspace. Please let us know once they are removed and we can optimize
     the billing from your subscriptions."

So there are three steps, and only the first two were automated:

    1. schedule the removal                    -> zapmail_removals.cancel_domains
    2. notice the mailbox actually disappeared -> zapmail_removals.check_removals
    3. confirm the slot was released           -> this module

Step 3 was the hole. `check_removals` posts one Slack message, sets
`notified: True`, and never mentions it again. Miss that single message — busy
channel, weekend, holiday — and the slots bill on with nothing anywhere saying
so. Silent and permanent, the worst shape a money bug can have.

WHY THIS CHECKS THE SUBSCRIPTION RATHER THAN A CHECKBOX
-------------------------------------------------------
The first version of this module tracked a `billing_acked` flag per mailbox and
nagged until a human ticked it. On its first run it reported 147 mailboxes and
$5,292/yr outstanding — which was wrong. The flag had been introduced that
morning, so every historical removal read as unacknowledged, including the many
Tim had already chased and Zapmail had already optimised. A brand-new field
cannot distinguish "nobody did this" from "nobody recorded doing this".

Zapmail exposes `totalMailboxQuantity` per subscription, which is the number
they actually charge for. Comparing that to the mailboxes really in the account
answers the question outright:

    billed quantity  >  real mailboxes   ->  we are paying for slots that are gone
    billed quantity ==  real mailboxes   ->  nothing owed, whatever the registry says

That is a measurement, not a memory. It cannot drift, it needs no upkeep, and it
would never have raised the false alarm. The registry is still read, but only to
name the mailboxes likely behind a gap so the message to Zapmail can be specific.

Providers are checked separately because Zapmail partitions everything by the
`x-service-provider` header — a GOOGLE-headed call cannot see MICROSOFT
subscriptions at all, and both return HTTP 200.
"""

from __future__ import annotations

import datetime
import json
import os
from collections import defaultdict

COST_PER_MAILBOX = 3
REG_KEY = "zm_removal_registry"
PROVIDERS = ("GOOGLE", "MICROSOFT")
NOTIFY_MEMBER_IDS = ("U09B2673A4A",)
SLACK_WEBHOOK_VARS = ("SLACK_ZAPMAIL_WEBHOOK", "SLACK_INFRA_DECISIONS_WEBHOOK",
                      "SLACK_WEBHOOK_URL")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _registry() -> dict:
    try:
        import db as store
        rows = store._request("GET", "/state",
                              params={"select": "data", "key": f"eq.{REG_KEY}"})
        if rows:
            d = json.loads(rows[0]["data"])
            return d.get("entries", d if isinstance(d, dict) else {})
    except Exception:
        pass
    return {}


def _days_since(d) -> int | None:
    try:
        return (datetime.date.today() - datetime.date.fromisoformat(str(d)[:10])).days
    except (ValueError, TypeError):
        return None


def _subscriptions(provider: str) -> list:
    """ACTIVE subscriptions for one provider.

    Fetched here rather than through `zapmail_removals.fetch_subscriptions`,
    which is hard-wired to GOOGLE and takes no provider argument — calling it
    would silently report the Microsoft fleet as having zero billed slots, which
    reads as a phantom-slot gap that does not exist.
    """
    import requests
    key = (os.environ.get("ZAPMAIL_API_KEY") or "").strip()
    if not key:
        return []
    try:
        r = requests.get("https://api.zapmail.ai/api/v2/subscriptions",
                         headers={"Content-Type": "application/json",
                                  "x-auth-zapmail": key,
                                  "x-service-provider": provider}, timeout=60)
    except requests.RequestException:
        return []
    if r.status_code != 200 or not r.text.strip():
        return []
    return [s for s in (r.json().get("data") or [])
            if str(s.get("subscriptionStatus", "")).upper() == "ACTIVE"]


def reconcile() -> dict:
    """Billed mailbox quantity vs mailboxes that actually exist, per provider."""
    import infra_lifecycle as il

    inv = il.fetch_zapmail_inventory()
    actual = defaultdict(int)
    for m in inv["mailboxes"].values():
        actual[m.get("provider") or "GOOGLE"] += 1

    per, total_gap = [], 0
    for prov in PROVIDERS:
        live = _subscriptions(prov)
        billed = sum(int(s.get("totalMailboxQuantity") or 0) for s in live)
        have = actual.get(prov, 0)
        gap = billed - have
        total_gap += max(gap, 0)
        per.append({"provider": prov, "billed": billed, "actual": have,
                    "gap": gap, "subscriptions": len(live)})

    # Name the mailboxes most likely behind a gap, newest removal first, so the
    # message to Zapmail can be specific rather than "please check our billing".
    suspects = []
    if total_gap > 0:
        for email, info in (_registry() or {}).items():
            if isinstance(info, dict) and info.get("removed_date") \
                    and not info.get("billing_acked"):
                suspects.append({"email": email,
                                 "domain": info.get("domain") or email.split("@")[-1],
                                 "removed_date": info.get("removed_date"),
                                 "days": _days_since(info.get("removed_date")) or 0})
        suspects.sort(key=lambda r: r["removed_date"], reverse=True)

    return {
        "as_of": datetime.date.today().isoformat(),
        "per_provider": per,
        "gap": total_gap,
        "monthly_cost": total_gap * COST_PER_MAILBOX,
        "annual_cost": total_gap * COST_PER_MAILBOX * 12,
        "suspects": suspects[:total_gap * 2] if total_gap else [],
        "reconciled": total_gap <= 0,
    }


def format_nudge(board: dict) -> str | None:
    """Slack text, or None when billing already matches reality."""
    if board["reconciled"]:
        return None
    men = " ".join(f"<@{u}>" for u in NOTIFY_MEMBER_IDS)
    out = [f"{men} 💸 *Zapmail is billing for {board['gap']} mailboxes that no longer "
           f"exist* — ${board['monthly_cost']}/mo, ${board['annual_cost']:,}/yr.", ""]
    for p in board["per_provider"]:
        if p["gap"] > 0:
            out.append(f"• *{p['provider']}* — billed {p['billed']}, "
                       f"actually {p['actual']} → *{p['gap']} phantom slots*")
    out.append("")
    if board["suspects"]:
        out.append("Most likely these, removed but not yet released:")
        bydom = defaultdict(list)
        for s in board["suspects"]:
            bydom[s["domain"]].append(s)
        for dom in list(sorted(bydom))[:15]:
            rs = bydom[dom]
            out.append(f"    – *{dom}* ({len(rs)} mbx, gone {rs[0]['removed_date']}, "
                       f"{rs[0]['days']}d ago)")
        if len(bydom) > 15:
            out.append(f"    …and {len(bydom) - 15} more domains.")
        out.append("")
    out.append("Ask Zapmail to optimise the subscription quantity. This check runs "
               "daily and stops on its own once billed matches actual — no manual "
               "tick required.")
    return "\n".join(out)


def post(board: dict | None = None, dry_run: bool = True) -> dict:
    board = board if board is not None else reconcile()
    text = format_nudge(board)
    if not text:
        return {"posted": False, "reason": "billing matches reality",
                "gap": board["gap"]}
    if dry_run:
        return {"dry_run": True, "text": text, "gap": board["gap"]}
    import requests
    for var in SLACK_WEBHOOK_VARS:
        hook = (os.environ.get(var) or "").strip()
        if not hook:
            continue
        try:
            r = requests.post(hook, json={"text": text}, timeout=10)
            if r.status_code in (200, 201):
                return {"posted": True, "via": var, "gap": board["gap"]}
        except requests.RequestException:
            pass
    return {"posted": False, "reason": "no working webhook", "text": text}


def acknowledge(emails=None) -> dict:
    """Mark removals as settled. Optional now — the reconciliation is the real
    check — but it keeps the suspect list clean once a gap has been resolved."""
    import db as store
    ent = _registry()
    want = {e.lower() for e in emails} if emails else None
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
        store._request("POST", "/state",
                       json_body={"key": REG_KEY,
                                  "data": json.dumps({"entries": ent, "updated": _now()}),
                                  "updated_at": _now()},
                       headers={"Prefer": "resolution=merge-duplicates"})
    return {"acknowledged": done}


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--send", action="store_true", help="post to Slack if there is a gap")
    p.add_argument("--ack-all", action="store_true",
                   help="mark every removal settled (housekeeping only)")
    args = p.parse_args()
    try:
        import _envload
        _envload.load()
    except Exception:
        pass

    if args.ack_all:
        print(acknowledge())
    else:
        b = reconcile()
        for pv in b["per_provider"]:
            flag = "  <-- PHANTOM SLOTS" if pv["gap"] > 0 else ""
            print(f"{pv['provider']:<10} billed {pv['billed']:>5}  actual "
                  f"{pv['actual']:>5}  gap {pv['gap']:>+4}{flag}")
        if b["reconciled"]:
            print("\n✅ billing matches reality — nothing owed")
        else:
            print(f"\n💸 {b['gap']} phantom slots = ${b['monthly_cost']}/mo, "
                  f"${b['annual_cost']:,}/yr")
        print()
        print(post(b, dry_run=not args.send))
