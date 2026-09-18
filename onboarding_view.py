"""The CRM -> infrastructure handoff: what a newly-won client is still owed.

THE SPLIT. The decision is made in the CRM — the Won modal creates the client
row, and that row IS the instruction to build infrastructure. The execution
happens here. Nothing in this module decides whether a client should exist; it
only reports what the CRM has already decided and infrastructure has not yet
delivered.

WHY IT IS NEEDED. The CRM's own onboarding checklist (client-setup-status.js)
tracks exactly two things: the Lead Tracker sheet and the SmartLead portal. Its
comment says why nothing else is there — "only DB-backed pieces are listed" —
so the CRM has never been able to see whether a client actually has inboxes.
A client could be won, invoiced and marked fully onboarded while owning no
sending infrastructure at all, and no screen anywhere would say so.

The specific failure that motivated this: LightDMV was won, tagged with all 57
inboxes, and launched with 15 of its 19 domains forwarding nowhere. Every
individual system was "fine". Nobody was looking at the join.

FOUR THINGS A LIVE CLIENT IS OWED, in the order they have to happen:

  infra       inboxes exist and are tagged to them
  target      enough of them — 57 seasonal, 42 standard
  forwarding  every domain points at the client's own website
  launch      a launch date, which is what starts the billing clock

STEPS ARE REPORTED, NEVER PERFORMED. Buying inboxes and setting forwarding both
spend money or change live sending, so this module returns a queue and the
operator presses the button. A screen that silently fixed things would be a
screen nobody could trust to tell them what is wrong.

AND IT NEVER GUESSES. If the Zapmail read failed there is no forwarding map, so
forwarding is reported as unknown rather than as correct — a client whose
forwarding we could not check must not read as onboarded. Same rule as
everywhere else in this repo: a check that cannot see its input skips.

Pure functions. All I/O lives in the route.
"""
from __future__ import annotations

import re

SEASONAL_TARGET, STANDARD_TARGET = 57, 42

# Steps in the order they must happen. A client blocked on an earlier step is
# not also reported as failing the later ones — telling someone their forwarding
# is wrong when they have no inboxes yet is noise.
STEP_ORDER = ("infra", "target", "forwarding", "launch")

STEP_LABEL = {
    "infra": "No inboxes at all",
    "target": "Below target",
    "forwarding": "Domains not forwarding to their website",
    "launch": "No launch date",
}


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def _host(url) -> str:
    """The bare host of a client's website, for comparing forwarding targets."""
    u = str(url or "").strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    return u.split("/")[0].strip()


def build(crm_clients: list[dict], board: dict,
          domains_by_client: dict | None, target_of, today: str,
          match=None) -> dict:
    """The onboarding queue.

    `domains_by_client`  {client name: [{"domain":…, "forward_to":…}, …]}, or
                         None when Zapmail could not be read. None means the
                         forwarding step is UNKNOWN, never "done".
    `target_of`          (client_name, crm_row) -> int. Injected rather than
                         reimplemented: this repo already has one answer to
                         "how many inboxes does this client get", and a second
                         copy here drifted immediately — it read 42 for
                         LightDMV, a 57-inbox seasonal client.
    `match`              (infra_name, crm_names) -> crm_name | None. Also
                         injected, for the same reason: the board labels rows
                         with the SmartLead tag ("LightDMV") and the CRM spells
                         it differently ("Light Dmv"), so a plain normalised
                         compare reported a fully-built client as having no
                         infrastructure at all.
    """
    crm_names = [c["name"] for c in crm_clients if c.get("name")]
    rows_by_name = {}
    for r in (board.get("rows") or []):
        label = r.get("crm_name") or r.get("client")
        if not label:
            continue
        resolved = (match(label, crm_names) if match else None) or label
        rows_by_name[_norm(resolved)] = r

    queue, done = [], []
    fwd_checkable = domains_by_client is not None

    for c in crm_clients:
        name = c.get("name")
        if not name or (c.get("status") or "") != "active":
            continue
        row = rows_by_name.get(_norm(name))
        held = (row or {}).get("mailboxes") or 0
        target = target_of(name, c)
        site = _host(c.get("website"))

        steps, notes = [], []

        if not row or held == 0:
            steps.append("infra")
            notes.append(f"CRM says active; infrastructure has none. Needs {target}.")
        elif held < target:
            steps.append("target")
            notes.append(f"{held} of {target} — {target - held} short.")

        # Forwarding is only meaningful once inboxes exist.
        fwd_unknown = False
        if row and held:
            if not fwd_checkable:
                fwd_unknown = True
                notes.append("Forwarding could not be checked — Zapmail was unreadable.")
            elif not site:
                notes.append("No website in the CRM, so there is nothing to forward to. "
                             "Fill in the client's website first.")
                steps.append("forwarding")
            else:
                doms = domains_by_client.get(name) or domains_by_client.get(_norm(name)) or []
                wrong = [d for d in doms if _host(d.get("forward_to")) != site]
                if wrong:
                    steps.append("forwarding")
                    missing = sum(1 for d in wrong if not d.get("forward_to"))
                    notes.append(
                        f"{len(wrong)} of {len(doms)} domains wrong"
                        + (f" ({missing} with none set)" if missing else "")
                        + f" — should be {site}.")

        if not c.get("launch_date"):
            steps.append("launch")
            notes.append("No launch date, so nothing starts the billing clock.")

        entry = {
            "client": name,
            "inboxes": held,
            "target": target,
            "short": max(0, target - held),
            "website": c.get("website"),
            "launch_date": c.get("launch_date"),
            "billing_model": c.get("billing_model"),
            "steps": [s for s in STEP_ORDER if s in steps],
            "notes": notes,
            "forwarding_unknown": fwd_unknown,
            "blocking": ([s for s in STEP_ORDER if s in steps] or [None])[0],
        }
        (queue if steps or fwd_unknown else done).append(entry)

    queue.sort(key=lambda e: (STEP_ORDER.index(e["blocking"]) if e["blocking"] else 9,
                              -e["short"], e["client"]))

    return {
        "as_of": today,
        "queue": queue,
        "complete": sorted(e["client"] for e in done),
        "summary": {
            "active_clients": len(queue) + len(done),
            "needs_work": len(queue),
            "complete": len(done),
            "no_infra": sum(1 for e in queue if "infra" in e["steps"]),
            "below_target": sum(1 for e in queue if "target" in e["steps"]),
            "bad_forwarding": sum(1 for e in queue if "forwarding" in e["steps"]),
            "no_launch_date": sum(1 for e in queue if "launch" in e["steps"]),
            "inboxes_to_buy": sum(e["short"] for e in queue),
            "forwarding_checked": fwd_checkable,
        },
        "step_labels": STEP_LABEL,
    }
