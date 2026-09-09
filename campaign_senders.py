"""Empty-campaign guard — ACTIVE campaigns with no senders, and the ones heading there.

A campaign with zero sender accounts is not an error state anywhere in SmartLead.
It stays ACTIVE, it reports cleanly, and it sends nothing. Every roll-up we have
counts inboxes or capacity, so a campaign that has lost all of its senders simply
stops appearing in the numbers instead of raising one.

That is not hypothetical. On 2026-09-10, five days after twelve burned domains were
scheduled for Zapmail removal, three had passed their billing dates and been deleted.
SmartLead dropped their mailboxes from every campaign, and two live client campaigns —
GM Landscaping's snow-removal campaign and Lightning Lawn Care's Tier 2 housing
campaign — were left ACTIVE with zero senders. Lightning's only three senders had all
been mailboxes on `exteriorlandscapecrew.info`. Nothing flagged it.

The deletions land staggered, on each domain's own billing date, so this keeps
happening for weeks after a batch cancellation rather than all at once. Hence a
standing check rather than a one-off sweep.

Two questions, because catching it after the fact is already too late:

  * which ACTIVE campaigns have no senders RIGHT NOW              -> `empty`
  * which ACTIVE campaigns will have none once the already-scheduled removals
    reach their billing dates                                     -> `doomed`

`at_risk` is the softer third case: some senders are scheduled, enough survive that
the campaign keeps running, but its capacity is about to drop.

Read-only. Nothing here changes a campaign, a mailbox or the cache.
"""

from __future__ import annotations

import os
import time

import requests

import db as store
import health_replace as hr
import zapmail_removals as zr

SMARTLEAD_API = "https://server.smartlead.ai/api/v1"

# A campaign this far below its own peak is worth showing even when it still has
# senders — losing 90% of them is an outage in everything but name.
THIN_CAPACITY = 3


def _key() -> str:
    return (os.environ.get("SMARTLEAD_API_KEY") or "").strip()


def _get(session, url, params, tries: int = 4):
    """GET with 429 backoff. Returns None on give-up — callers must NOT read that
    as an empty campaign, which is the whole failure this module exists to catch."""
    for _ in range(tries):
        try:
            r = session.get(url, params=params, timeout=60)
        except Exception:
            time.sleep(5)
            continue
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                return None
        if r.status_code != 429:
            return None
        time.sleep(20)
    return None


def scheduled_emails() -> set:
    """Mailboxes registered for Zapmail removal that have not actually gone yet.

    An entry keeps its row after removal (that is how the Slack alert de-dupes), so
    filter on `removed_date` rather than presence, or every past cancellation would
    look like a pending threat forever.
    """
    reg = (store.get_state(zr.REG_KEY) or {}).get("entries", {}) or {}
    return {e for e, v in reg.items() if not (v or {}).get("removed_date")}


def build() -> dict:
    """Every ACTIVE campaign, worst first: empty now, empty soon, or thinning."""
    key = _key()
    if not key:
        return {"error": "SMARTLEAD_API_KEY is not set"}

    index = hr.campaign_index()
    if not index:
        return {"error": "could not read the campaign list from SmartLead — "
                         "refusing to report campaign health from nothing"}
    active = {n: v for n, v in index.items() if (v or {}).get("status") == "ACTIVE"}
    if not active:
        return {"error": "no ACTIVE campaigns returned — treating as a failed read "
                         "rather than an idle account"}

    doomed_mailboxes = scheduled_emails()
    session = requests.Session()
    rows, unreadable = [], []

    for name, meta in active.items():
        cid = meta.get("id")
        accts = _get(session, f"{SMARTLEAD_API}/campaigns/{cid}/email-accounts",
                     {"api_key": key})
        # A failed read is NOT an empty campaign. Say so and move on: reporting it
        # as empty would send someone hunting a campaign that is sending fine.
        if accts is None or not isinstance(accts, list):
            unreadable.append(name)
            continue

        senders = [(a.get("from_email") or "").lower() for a in accts]
        capacity = sum(int(a.get("message_per_day") or 0) for a in accts)
        going = [e for e in senders if e in doomed_mailboxes]
        surviving = len(senders) - len(going)

        if not senders:
            state, why = "empty", "ACTIVE with no sender accounts — sending nothing"
        elif going and surviving == 0:
            state, why = "doomed", (
                f"all {len(senders)} sender(s) are on domains already scheduled for "
                f"removal — this campaign goes silent on their billing dates")
        elif going:
            state, why = "at_risk", (
                f"{len(going)} of {len(senders)} sender(s) scheduled for removal — "
                f"{surviving} would remain")
        elif len(senders) <= THIN_CAPACITY:
            state, why = "thin", f"only {len(senders)} sender(s)"
        else:
            continue  # healthy campaigns are not the point of this page

        rows.append({
            "campaign": name,
            "id": cid,
            "url": hr.SL_CAMPAIGN_URL.format(id=cid),
            "state": state,
            "why": why,
            "senders": len(senders),
            "capacity": capacity,
            "scheduled_senders": len(going),
            "surviving_senders": surviving,
            "scheduled_emails": sorted(going),
        })

    order = {"empty": 0, "doomed": 1, "at_risk": 2, "thin": 3}
    rows.sort(key=lambda r: (order[r["state"]], r["senders"]))

    return {
        "summary": {
            "active_campaigns": len(active),
            "empty": sum(1 for r in rows if r["state"] == "empty"),
            "doomed": sum(1 for r in rows if r["state"] == "doomed"),
            "at_risk": sum(1 for r in rows if r["state"] == "at_risk"),
            "thin": sum(1 for r in rows if r["state"] == "thin"),
            "unreadable": len(unreadable),
            "pending_removal_mailboxes": len(doomed_mailboxes),
        },
        # Surfaced, not swallowed: an unreadable campaign is an unknown, and the
        # caller should be able to see that the sweep was incomplete.
        "unreadable": unreadable,
        "campaigns": rows,
    }


if __name__ == "__main__":
    import json
    rep = build()
    if rep.get("error"):
        raise SystemExit(rep["error"])
    print(json.dumps(rep["summary"], indent=2))
    for r in rep["campaigns"]:
        print("  [%-7s] senders=%3d cap=%5d  %s" %
              (r["state"], r["senders"], r["capacity"], r["campaign"][:64]))
        print("            %s" % r["why"])
