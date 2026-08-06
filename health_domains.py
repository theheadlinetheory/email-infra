"""Domain-level view of the burned fleet — the top-priority cancel/reallocate surface.

Zapmail cancels whole DOMAINS, not individual mailboxes, so decisions have to be
made per domain: a domain with all 3 mailboxes burned is free to cancel; one with
2 burned + 1 healthy loses the healthy one as collateral (reallocate/replace it
first); one with only 1 burned should keep the domain and just reallocate that inbox.

This module groups the current health fleet by domain, ranks domains by how many
of their mailboxes are burned (3-day-confirmed via the snapshot hysteresis), and
attaches everything the UI needs to reallocate (swap in reserve) and cancel
(schedule Zapmail removal + register with the removal bot).
"""

import db as store
import health_replace as hr

# Client field markers set by health_snapshot.build_fleet_from_overview
ACQ_MARK = "(acquisition)"
RESERVE_MARK = "(generic reserve)"

BURNED = "burned"
AT_RISK = "at_risk"


def _source_of(client: str) -> str:
    if client == ACQ_MARK:
        return "acquisition"
    if client == RESERVE_MARK:
        return "reserve"
    return "client"


def _pool_for(niche: str, rs: dict) -> int:
    """Compatible reserve count for a niche (exact niche + generic; generic can
    draw from anything). Mirrors health_replace.replace_all_burned."""
    by = rs.get("available_by_niche", {}) or {}
    if niche == "generic":
        return by.get("generic", 0) + by.get("landscaping", 0) + by.get("hvac", 0)
    return by.get(niche, 0) + by.get("generic", 0)


def domain_view() -> dict:
    """Every domain that has at least one burned mailbox, ranked by burned count
    (3 → 2 → 1), with reallocate/cancel readiness."""
    data, _ = store.cache_get("health_fleet")
    fleet = (data or {}).get("inboxes", []) if isinstance(data, dict) else []

    streaks = store.get_state("burn_streaks") or {}
    jobs = hr.list_jobs()
    handled = {j["old_email"] for j in jobs if j.get("status") not in ("cancelled", None)}
    rs = hr.reserve_summary()

    # Campaign STATUS map — only ACTIVE campaigns are actively sending; inboxes that
    # sit only in paused/completed campaigns aren't really in use, so they don't need
    # reallocating and aren't collateral (they can be cancelled directly).
    status_map = hr.campaign_status_map()

    def _active(camps):
        return [c for c in (camps or []) if status_map.get(c) == "ACTIVE"]

    # Acquisition reserve = idle acquisition inboxes (in no active campaign). This is
    # why acquisition is reserve-driven, not "cancel only".
    acq_pool = len(hr.acq_reserve_candidates(status_map))

    # what's already scheduled for cancellation (removal bot registry)
    try:
        import zapmail_removals as zr
        reg = (store.get_state(zr.REG_KEY) or {}).get("entries", {})
        scheduled_domains = {v["domain"] for v in reg.values()}
    except Exception:
        scheduled_domains = set()

    # group fleet rows by domain
    by_dom: dict[str, list] = {}
    for r in fleet:
        dom = r.get("domain") or (r.get("email", "").split("@")[-1])
        if not dom:
            continue
        by_dom.setdefault(dom, []).append(r)

    domains = []
    for dom, rows in by_dom.items():
        # A burned inbox only matters here if it's ACTUALLY SENDING (in an active
        # campaign). One that was burned and then had its campaign paused/completed
        # is IDLE now — its bounce/reply are just stale metrics; it doesn't need
        # reallocating and the domain shouldn't be cancel-flagged just because it
        # stopped sending. Those show as idle in the All-inboxes view, not here.
        burned = [r for r in rows if r.get("status") == BURNED and _active(r.get("campaigns"))]
        if not burned:
            continue  # no actively-sending burned inbox on this domain — skip
        at_risk = [r for r in rows if r.get("status") == AT_RISK]
        healthy = [r for r in rows if r.get("status") not in (BURNED, AT_RISK)]
        niche = hr._niche(dom)
        clients = sorted({r.get("client") for r in rows if r.get("client")})
        source = _source_of(clients[0]) if len(clients) == 1 else (
            _source_of(clients[0]) if clients else "client")
        if any(_source_of(c) == "acquisition" for c in clients):
            source = "acquisition"

        def _mb(r):
            camps = r.get("campaigns") or []
            active = _active(camps)
            return {
                "email": r.get("email"),
                "status": r.get("status"),
                "client": r.get("client"),
                "in_campaign": bool(active),           # in an ACTIVE (sending) campaign
                "active_campaigns": active,
                "campaigns": camps,
                "idle": bool(camps) and not active,    # only in paused/completed campaigns
                "bounce_3d": r.get("bounce_3d"),
                "reply_3d": r.get("reply_3d"),
                "streak_days": (streaks.get(r.get("email")) or [None])[0]
                if isinstance(streaks.get(r.get("email")), list) else streaks.get(r.get("email")),
                "has_job": r.get("email") in handled,
            }

        burned_mb = [_mb(r) for r in burned]
        # collateral = non-burned mailboxes still in an ACTIVE campaign that a domain-
        # cancel would also delete. Idle/reserve/warming inboxes aren't collateral.
        collateral = [_mb(r) for r in (at_risk + healthy) if _active(r.get("campaigns"))]
        # burned mailboxes still actively sending need reallocating before cancel;
        # burned mailboxes only in paused/completed campaigns are cancel-ready as-is.
        need_realloc = [b for b in burned_mb if b["in_campaign"] and not b["has_job"]]

        is_acq = source == "acquisition"
        pool = acq_pool if is_acq else _pool_for(niche, rs)

        domains.append({
            "domain": dom,
            "niche": niche,
            "source": source,
            "clients": clients,
            "total": len(rows),
            "counts": {
                "burned": len(burned), "at_risk": len(at_risk),
                "healthy": len(healthy), "collateral": len(collateral),
            },
            "burned_mailboxes": burned_mb,
            "collateral": collateral,
            "need_reallocate": need_realloc,
            "reserve_pool": pool,
            "reserve_ok": pool >= len(need_realloc),
            "scheduled": dom in scheduled_domains,
            # clean cancel = nothing healthy actively sending would be lost AND no
            # burned still actively sending (idle/paused burned don't block a cancel)
            "clean_cancel": len(collateral) == 0 and len(need_realloc) == 0,
        })

    # priority: most-burned first; within that, clean cancels first, then worst bounce
    def _avg_bounce(d):
        bs = [m["bounce_3d"] for m in d["burned_mailboxes"] if m["bounce_3d"] is not None]
        return sum(bs) / len(bs) if bs else 0

    domains.sort(key=lambda d: (d["counts"]["burned"], d["clean_cancel"], _avg_bounce(d)),
                 reverse=True)

    summary = {
        "domains": len(domains),
        "burned_mailboxes": sum(d["counts"]["burned"] for d in domains),
        "fully_dead": sum(1 for d in domains if d["counts"]["burned"] == d["total"]),
        "cancelable_now": sum(1 for d in domains if d["clean_cancel"] and not d["scheduled"]),
        "already_scheduled": sum(1 for d in domains if d["scheduled"]),
    }
    return {"summary": summary, "domains": domains}
