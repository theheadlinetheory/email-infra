"""Fixes for the invariant rules, as scripts rather than judgement.

Each fix is a plain function: it takes the rule's violations, works out the
exact operations, and either reports them (dry run) or performs them. No
inference, no free-form decisions — a rule fails for a stated reason, and the
fix for that reason is fixed in advance and testable.

THREE RULES CAN BE FIXED FROM THE DASHBOARD. The others cannot, and saying so
is part of the design:

  rule 7   an empty domain is auto-renewing        -> turn auto-renew off
  rule 10  a mailbox is gone from Zapmail but
           still in Smartlead                      -> delete the Smartlead account
  rule 9   a pool is over its ceiling              -> reported only; which inbox
                                                     leaves is a judgement call

  rule 1   a client is off target                  -> buying or moving inboxes
                                                     costs money, and the right
                                                     answer differs per client
  rule 2/3/4/5  tagging, ownership, forwarding, CRM rows
                                                  -> each needs a decision about
                                                     WHICH value is correct
  rule 6   a domain with live senders is expiring  -> renewing costs money
  rule 8   billed quantity != mailboxes            -> Zapmail's side, not ours
  rule 11  a read came back short                  -> re-run, nothing to fix

EVERY FIX IS DRY-RUN BY DEFAULT and every one re-derives its own target list
from live data rather than trusting the stored rule result, which may be hours
old. A fix that acts on a stale violation is exactly the failure this repo has
had six times.
"""
from __future__ import annotations

import re

# Rules a button can act on, and what the button says.
FIXABLE = {
    7: {"label": "Turn auto-renew off",
        "what": "Disables registrar auto-renew on domains that have no mailboxes."},
    10: {"label": "Remove from Smartlead",
         "what": "Deletes Smartlead accounts whose Zapmail mailbox no longer exists."},
}

_DOMAIN_RE = re.compile(r"^([a-z0-9][a-z0-9.-]*\.[a-z]{2,})\s*:", re.I)
_EMAIL_RE = re.compile(r"^([^\s@]+@[^\s@]+\.[a-z]{2,})", re.I)


def targets_for(rule: int, violations: list[str]) -> list[str]:
    """The domains or emails a rule's violation lines refer to.

    Parsed, not guessed: each rule writes its violations in a fixed shape, and
    anything that does not match that shape is dropped rather than acted on.
    """
    out = []
    for v in violations or []:
        m = (_DOMAIN_RE if rule == 7 else _EMAIL_RE).match(str(v).strip())
        if m:
            out.append(m.group(1).lower())
    return sorted(set(out))


def fix_rule_7(io, confirm: bool) -> dict:
    """Turn registrar auto-renew off for domains that have no mailboxes.

    Re-derives the list live: a domain that was empty when the rule ran may
    have been provisioned since, and turning auto-renew off on a domain with
    live senders is the one mistake here that cannot be undone.
    """
    registrar = io.registrar_domains()
    mailbox_counts = io.mailbox_counts_by_domain()
    sender_counts = io.sender_counts_by_domain()
    if registrar is None or mailbox_counts is None or sender_counts is None:
        return {"error": "could not read the registrar, Zapmail and Smartlead in "
                         "full — refusing to disable auto-renew on an unverified list"}

    # EMPTY MEANS EMPTY EVERYWHERE. "No Zapmail mailbox" is not enough: the
    # headlinetheory*.com domains were never in Zapmail and carry 33 actively
    # sending Smartlead accounts between them. Checking only Zapmail put all
    # eleven of them on the disable list — letting them lapse would have taken
    # 33 live acquisition senders down with them.
    #
    # A domain is only safe to let go when NOTHING is on it: no mailbox in
    # Zapmail and no sender in Smartlead.
    targets, kept = [], []
    for d, r in registrar.items():
        if not r.get("auto_renew"):
            continue
        mbx, snd = mailbox_counts.get(d, 0), sender_counts.get(d, 0)
        if mbx or snd:
            if not mbx and snd:
                # Worth naming: it looks empty from Zapmail and is not.
                kept.append({"domain": d, "smartlead_senders": snd})
            continue
        targets.append(d)
    targets = sorted(targets)
    plan = {"rule": 7, "targets": targets, "count": len(targets),
            "saving_yr": round(sum(io.renewal_price(d) for d in targets), 2),
            # Domains that have no Zapmail mailbox but DO have live senders.
            "kept_external": sorted(kept, key=lambda x: -x["smartlead_senders"]),
            "held_reason": "has live Smartlead senders despite no Zapmail mailbox"}
    if not targets:
        return {**plan, "note": "nothing to do — no empty domain is auto-renewing"}
    if not confirm:
        return {**plan, "dry_run": True,
                "note": "nothing changed. Confirm to disable auto-renew on these."}
    return {**plan, **io.disable_auto_renew(targets)}


def fix_rule_10(io, confirm: bool) -> dict:
    """Delete Smartlead accounts whose Zapmail mailbox is gone.

    Refuses anything still attached to an ACTIVE campaign or owning a positive
    reply, whatever the rule said — the rule checks existence, not safety, and
    a mailbox can be re-created or a campaign re-started between runs.
    """
    live = io.zapmail_mailboxes()
    accounts = io.smartlead_accounts()
    if live is None or accounts is None:
        return {"error": "could not read Zapmail and Smartlead in full — refusing "
                         "to delete from a partial roster"}
    external = io.external_domains()
    gone = [a for a in accounts
            if a["email"] not in live
            and a["email"].split("@")[-1] not in external]
    if not gone:
        return {"rule": 10, "targets": [], "count": 0,
                "note": "nothing to do — every Smartlead account has a Zapmail mailbox"}

    emails = [a["email"] for a in gone]

    # "Is it on a live campaign" needs only the ACTIVE ones and runs here.
    guard = io.safety_check(emails)
    if guard.get("error"):
        return {"error": f"safety check failed: {guard['error']} — nothing deleted"}

    # "Does it own a positive reply" needs EVERY campaign, including paused and
    # completed ones, which is far more than a request can scan. That walk runs
    # nightly; this reads its result. No fresh result means no deletion.
    import rule10_safety
    full = rule10_safety.verdict(emails)

    blocked = set(guard.get("active") or [])
    if not full.get("stale"):
        blocked |= set(full.get("active") or []) | set(full.get("positives") or [])
    safe = [a for a in gone if a["email"] not in blocked]

    plan = {
        "rule": 10,
        "targets": [a["email"] for a in safe],
        "count": len(safe),
        "held": sorted(blocked),
        "held_reason": "on an ACTIVE campaign or owns a positive reply",
        "reply_scan": ("stale: " + full.get("reason", "")) if full.get("stale")
                      else f"{full.get('campaigns_scanned')} campaigns, "
                           f"{full.get('age_hours')}h old",
    }
    if full.get("stale"):
        # Rule 11: a check that did not run is not a check that passed.
        return {**plan, "targets": [], "count": 0, "dry_run": True,
                "blocked_by": "reply scan",
                "note": "NOT deleting. The positive-reply scan is stale "
                        f"({full.get('reason')}), so a mailbox holding a live "
                        "thread on a paused campaign would look deletable. "
                        "The nightly scan refreshes it."}
    if not confirm:
        return {**plan, "dry_run": True,
                "note": "nothing deleted. Confirm to remove these Smartlead accounts."}
    return {**plan, **io.delete_smartlead(safe)}


FIXES = {7: fix_rule_7, 10: fix_rule_10}


def run(rule: int, io, confirm: bool = False) -> dict:
    fn = FIXES.get(rule)
    if not fn:
        return {"error": f"rule {rule} has no automatic fix — see rule_fixes.py "
                         "for why each one does or does not"}
    return fn(io, confirm)
