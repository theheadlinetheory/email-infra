"""The Acquisition tab's payload: our own prospecting inboxes, and the domains
under them.

Tim, 2026-09-18, on the dashboard revamp: "Acquisition = just inboxes + domains".
So this is deliberately narrow. Allocation planning, campaign targets and the
reallocate flow stay where they are; this answers two questions only:

  1. How much acquisition sending do we own, and how much of it is genuinely
     free to point at something new?
  2. Which domains carry it, are any over the 3-inbox cap, and is any of them
     about to lapse?

WHAT COUNTS AS IN USE. Tim's rule: an inbox is in use if it is sending to new
leads, OR sending follow-ups, OR sitting in the gap between sequence steps.
`acq_capacity._classify` covers the first two from the campaign queues. The
third is not visible in campaign state at all — a lead waiting two days for its
next step leaves both queues looking momentarily quiet — so it is caught the
only way it can be: by what the mailbox actually DID. `measured_idle` overrules
the state machine with the send rows, and only an inbox that sent NOTHING across
the whole window is called free.

That is strictly more conservative than Tim's two days, on purpose. The failure
this guards against is one-directional: freeing a mailbox mid-sequence cuts the
sequence off and orphans every reply welded to it, while leaving one idle for an
extra week costs 15 sends a day. So the measurement can only ever move an inbox
from free to in-use, never the other way.

AND WHEN WE CANNOT MEASURE. If the daily send rows are unavailable the free
figure is None, not 0 and not the state machine's optimistic number. A check
that cannot see its input must say so rather than answer — the same rule the
invariants script follows. See docs/INFRA_RULES.md.

Pure functions. All I/O lives in the route.
"""
from __future__ import annotations

# Never more than this many mailboxes on one domain (Tim, 2026-09-17). Zapmail
# allows five; putting five on a domain means one bounce pattern takes out the
# whole thing, and the admin mailbox cannot be removed while siblings remain.
MAX_INBOXES_PER_DOMAIN = 3

# How far ahead a lapsing domain is worth showing on this tab.
EXPIRY_HORIZON_DAYS = 60

STATE_ORDER = ("sending", "stranded", "parked", "unassigned", "blocked")


def _days_until(expires: str | None, today: str) -> int | None:
    """Whole days from `today` to `expires`, both YYYY-MM-DD. None if unknown."""
    from datetime import date
    if not expires or not today:
        return None
    try:
        e = date.fromisoformat(str(expires)[:10])
        t = date.fromisoformat(str(today)[:10])
    except (ValueError, TypeError):
        return None
    return (e - t).days


def build(acq: dict, registrar: dict | None, today: str) -> dict:
    """The tab payload.

    `acq`       the result of acq_capacity.build()
    `registrar` {domain: {expires, auto_renew, registrar}}, or None when the
                registrar could not be read. None and {} mean different things:
                None is "we did not look", and every domain then reports an
                unknown expiry rather than an implied healthy one.
    """
    if acq.get("error"):
        return {"error": acq["error"]}

    inboxes = acq.get("inboxes") or []
    s = acq.get("summary") or {}
    meas = s.get("measured") or {}
    measured = bool(meas.get("measured"))

    # The free set, measured. `state in IDLE_STATES` is the claim; `sent_measured
    # == 0` is the check. Both must hold.
    idle_states = ("stranded", "parked", "unassigned")
    free = [i for i in inboxes
            if i.get("state") in idle_states and (i.get("sent_measured") or 0) == 0] \
        if measured else []

    by_state = []
    for st in STATE_ORDER:
        row = (s.get("by_state") or {}).get(st) or {"inboxes": 0, "capacity": 0}
        by_state.append({"state": st, "inboxes": row["inboxes"], "capacity": row["capacity"]})

    # -- domains ----------------------------------------------------------
    dom: dict[str, dict] = {}
    for i in inboxes:
        d = (i.get("domain") or "").lower()
        if not d:
            continue
        r = dom.setdefault(d, {"domain": d, "inboxes": 0, "sending": 0, "free": 0,
                               "blocked": 0, "capacity": 0})
        r["inboxes"] += 1
        r["capacity"] += i.get("per_day") or 0
        if i.get("state") == "sending":
            r["sending"] += 1
        elif i.get("state") == "blocked":
            r["blocked"] += 1
        if measured and i.get("state") in idle_states and (i.get("sent_measured") or 0) == 0:
            r["free"] += 1

    for d, r in dom.items():
        reg = (registrar or {}).get(d)
        r["registrar"] = (reg or {}).get("registrar")
        r["expires"] = (reg or {}).get("expires") or None
        # Tri-state on purpose. False means the registrar says it will lapse;
        # None means we could not read the registrar, which is not the same
        # thing and must not be rendered as "off".
        r["auto_renew"] = (reg or {}).get("auto_renew") if reg else None
        r["days_to_expiry"] = _days_until(r["expires"], today)
        r["over_cap"] = r["inboxes"] > MAX_INBOXES_PER_DOMAIN
        r["known_to_registrar"] = reg is not None

    domains = sorted(dom.values(), key=lambda r: (-r["inboxes"], r["domain"]))
    over_cap = [r for r in domains if r["over_cap"]]
    # Lapsing and still carrying senders. auto_renew is False, not None — an
    # unknown flag is reported separately rather than alarmed on.
    lapsing = [r for r in domains
               if r["auto_renew"] is False and r["days_to_expiry"] is not None
               and r["days_to_expiry"] <= EXPIRY_HORIZON_DAYS and r["sending"] > 0]
    lapsing.sort(key=lambda r: r["days_to_expiry"])
    unknown_registrar = [r["domain"] for r in domains if not r["known_to_registrar"]]

    notes = []
    if not measured:
        notes.append("No daily send rows, so free capacity could not be measured. "
                     "The figure is withheld rather than guessed.")
    if meas.get("phantom_inboxes"):
        notes.append(f"{meas['phantom_inboxes']} inbox(es) look idle from campaign state "
                     f"but are still sending — excluded from free.")
    if s.get("followup_only_inboxes"):
        notes.append(f"{s['followup_only_inboxes']} inbox(es) are sending follow-ups only "
                     "(new-lead queue empty). In use, not free — moving one cuts a live sequence.")
    if s.get("warming_excluded"):
        notes.append(f"{s['warming_excluded']} inbox(es) still in warm-up, excluded entirely.")
    if unknown_registrar:
        notes.append(f"{len(unknown_registrar)} domain(s) are not in the registrar list, so their "
                     "expiry is unknown — not assumed safe.")

    summary = {
        "inboxes": s.get("inboxes", len(inboxes)),
        "domains": len(domains),
        "capacity": s.get("total_capacity"),
        "actual_per_day": s.get("actual_per_day"),
        "utilisation_pct": s.get("utilisation_pct"),
        # None = could not measure. The page must print that, not a zero.
        "free_inboxes": len(free) if measured else None,
        "free_capacity": sum(i.get("per_day") or 0 for i in free) if measured else None,
        "measured": measured,
        "window_from": meas.get("window_from"),
        "window_to": meas.get("window_to"),
        "window_sending_days": meas.get("window_sending_days"),
        "blocked_inboxes": s.get("blocked_inboxes"),
        "followup_only_inboxes": s.get("followup_only_inboxes"),
        "warming_excluded": s.get("warming_excluded"),
        "over_cap_domains": len(over_cap),
        "lapsing_domains": len(lapsing),
        "registrar_read": registrar is not None,
    }

    return {
        "generated_at": acq.get("generated_at"),
        "synced_at": acq.get("synced_at"),
        "summary": summary,
        "by_state": by_state,
        "free": [{"email": i["email"], "domain": i.get("domain"), "group": i.get("group"),
                  "per_day": i.get("per_day"), "state": i.get("state"),
                  "age_days": i.get("age_days"), "health": i.get("health"),
                  "why": (i.get("why") or [None])[0]} for i in free],
        "domains": domains,
        "over_cap": over_cap,
        "lapsing": lapsing,
        "notes": notes,
        "max_per_domain": MAX_INBOXES_PER_DOMAIN,
    }
