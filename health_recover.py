"""Health V1 — recover at-risk inboxes by putting them back on warm-up.

An at-risk inbox (declining but not dead) can often recover if warm-up is
re-enabled so it rebuilds reputation. This resolves each inbox to its SmartLead
account id (from the overview cache) and calls the existing sl_set_warmup().

NOTE: SmartLead has no API to pull an inbox out of a campaign (reallocation is
UI-only), so re-warming alone doesn't stop it sending — the operator should also
reduce/pause it in the campaign. We return that reminder.
"""

from __future__ import annotations

import db as store


def _account_map() -> dict:
    """email -> SmartLead account id, from the overview cache."""
    ov, _ = store.cache_get("overview_v2")
    m: dict[str, int] = {}
    if not ov:
        return m
    for c in ov.get("clients", []):
        for letter in ("a", "b"):
            for ad in (c.get(f"group_{letter}") or {}).get("account_details", []):
                if ad.get("email") and ad.get("id"):
                    m[ad["email"]] = ad["id"]
    for section in ("generic_groups", "acquisition_groups"):
        for g in ov.get(section, []):
            for ad in g.get("account_details", []):
                if ad.get("email") and ad.get("id"):
                    m[ad["email"]] = ad["id"]
    return m


def recover(emails: list[str], dry_run: bool = True) -> dict:
    """Re-enable warm-up on the given inboxes. dry_run reports the plan only."""
    m = _account_map()
    resolved = [(e, m[e]) for e in emails if e in m]
    unresolved = [e for e in emails if e not in m]

    if dry_run:
        return {"dry_run": True, "would_recover": len(resolved),
                "resolved": [e for e, _ in resolved], "unresolved": unresolved}

    import setup
    done = []
    for email, aid in resolved:
        try:
            setup.sl_set_warmup(aid)
            done.append(email)
        except Exception:
            pass
    try:
        store.log_monitor_event("health_recover", {"emails": done})
    except Exception:
        pass
    return {"dry_run": False, "recovered": len(done), "emails": done,
            "unresolved": unresolved,
            "reminder": "Warm-up re-enabled. Also reduce/pause these in their SmartLead "
                        "campaign so they warm without burning further (reallocation is UI-only)."}
