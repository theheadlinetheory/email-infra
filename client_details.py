"""Per-client inbox and domain detail for the Clients cards.

WHY THIS IS ONE BUILD FOR EVERY CLIENT. The first version answered one client
per request and re-fetched the world each time. Measured on 2026-09-19:

    fetch_smartlead_tags        28.9s
    fetch_zapmail_inventory      9.3s   (and it was called TWICE)
    get_health_status_all        4.3s
    fetch_crm_clients            0.8s

That is roughly fifty seconds to open one card, and the same fifty seconds again
for the next one, because nothing was shared between them. None of those reads
is per-client — they all return the whole fleet and then get filtered. So the
work is done once, for everybody, and cached; opening a card is then a dict
lookup.

READS THAT COME BACK SHORT RETURN None. If the Smartlead walk or the Zapmail
walk fails, `inboxes` is None for every client rather than an empty list — the
card must say "could not read" and keep showing the count, not render zero
inboxes for a client that has fifty-seven. See docs/INFRA_RULES.md rule 11.
"""
from __future__ import annotations

from datetime import date, timedelta

# Zapmail/Smartlead warm-up is exactly 14 days. STRICTLY less than — an inbox
# created 14 days ago has FINISHED and can send.
WARMUP_DAYS = 14


def _age_days(created, today: date) -> int | None:
    try:
        return (today - date.fromisoformat(str(created)[:10])).days
    except (ValueError, TypeError):
        return None


def warm_state(started, today: date) -> dict:
    """Per-inbox warm-up readiness, from when THAT inbox started warming.

    Two things this gets right that the old display did not.

    Per inbox, not per group. The old dashboard derived one `warmup_days` for a
    whole group from its earliest date tag, so a group bought in two batches got
    a single verdict covering both — McFarlane holds 27 at 17 days and 15 at 13,
    and no one number is true for both.

    And the SMARTLEAD clock, not the Zapmail one. Warm-up runs in Smartlead, and
    a mailbox can exist in Zapmail long before it is exported. Those same 15 were
    created in Zapmail on 2026-08-12 but only started warming on 2026-09-08 —
    nearly four weeks apart. Reading the Zapmail date calls all 42 ready when 15
    have another day to go, which is precisely the wrong direction: it puts an
    unwarmed inbox into a live campaign.

    `started` is the Smartlead warm-up start. None means unknown, never ready.
    """
    created = started
    age = _age_days(created, today)
    if age is None:
        # No creation date is not evidence of readiness. Say unknown.
        return {"age_days": None, "ready": None, "ready_on": None}
    if age >= WARMUP_DAYS:
        return {"age_days": age, "ready": True, "ready_on": None}
    return {"age_days": age, "ready": False,
            "ready_on": (today + timedelta(days=WARMUP_DAYS - age)).isoformat()}


def build(board: dict, zm_mailboxes: dict | None, tags: dict | None,
          health: dict | None, crm_rows: list[dict] | None,
          norm, is_free, today: date | None = None,
          warm_starts: dict | None = None) -> dict:
    """{normalised client name: detail}. Pure — every input is injected."""
    # An inbox roster needs BOTH halves: what exists (Zapmail) and who owns it
    # (the Smartlead tag). Either one missing makes the list unknowable, not
    # empty.
    readable = bool(zm_mailboxes) and bool(tags)
    today = today or date.today()

    by_bucket: dict[str, list] = {}
    if readable:
        for email, mb in zm_mailboxes.items():
            owner = tags.get(email)
            if not owner:
                continue
            # `created` is the ZAPMAIL date — the billing clock, and what the
            # Created column shows. Warm-up is a different clock entirely.
            created = str(mb.get("created_at") or "")[:10]
            started = (warm_starts or {}).get(email)
            by_bucket.setdefault(owner, []).append({
                "email": email,
                "domain": mb.get("domain"),
                "created": created,
                "warm_started": str(started)[:10] if started else None,
                "health": (health or {}).get(email),
                **warm_state(started, today),
            })

    crm_by = {norm(c.get("name")): c for c in (crm_rows or []) if c.get("name")}

    out = {}
    for row in (board.get("rows") or []):
        name = row.get("crm_name") or row.get("client")
        if not name:
            continue
        crm_row = crm_by.get(norm(row.get("crm_name") or name))
        exempt = bool(crm_row and is_free(crm_row))

        inboxes = None
        if readable:
            inboxes = []
            for bucket in (row.get("infra_buckets") or []):
                inboxes.extend(by_bucket.get(bucket, []))
            inboxes.sort(key=lambda i: (i["domain"] or "", i["email"]))

        warm = None
        if inboxes is not None:
            ready = [i for i in inboxes if i.get("ready") is True]
            warming = [i for i in inboxes if i.get("ready") is False]
            unknown = [i for i in inboxes if i.get("ready") is None]
            warm = {
                "ready": len(ready),
                "warming": len(warming),
                "unknown": len(unknown),
                # When the last of them becomes usable. Reported as a date
                # rather than a countdown so it does not silently go stale.
                "all_ready_on": (max(i["ready_on"] for i in warming)
                                 if warming and all(i.get("ready_on") for i in warming)
                                 else None),
            }

        out[norm(name)] = {
            "client": name,
            "inboxes": inboxes,
            "inboxes_unreadable": inboxes is None,
            "warmup": warm,
            "inbox_count": row.get("mailboxes"),
            "domains": row.get("domains") or {},
            "domain_count": row.get("domain_count"),
            "monthly_cost": row.get("monthly_cost"),
            "yearly_cost": round((row.get("monthly_cost") or 0) * 12, 2),
            # A free account has no contract to run out, so it gets no invented
            # dates — same rule the Clients list follows.
            "term_ends": None if exempt else row.get("effective_end"),
            "term_basis": "free account — no term" if exempt else row.get("end_basis"),
            "decide_by": None if exempt else row.get("decision_by"),
            "days_to_decision": None if exempt else row.get("days_to_decision"),
            "hard_stop": None if exempt else row.get("hard_stop"),
            "launch_date": row.get("launch_date"),
            "billing_model": row.get("billing_model"),
            "agreement_type": row.get("agreement_type"),
            "seasonal": row.get("seasonal"),
            "vertical": row.get("vertical_label") or row.get("vertical"),
            "exempt": exempt,
            "cohorts": row.get("cohorts") or [],
            "website": (crm_row or {}).get("website"),
            "status": row.get("status"),
        }
    return out
