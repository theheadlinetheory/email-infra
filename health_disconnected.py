"""Disconnected-inbox detection for the top-of-page critical alert.

An inbox disconnects from SmartLead when its SMTP/IMAP auth breaks (password
change, revoked OAuth, provider lockout). SmartLead flags it `is_smtp_success =
false`; sync surfaces that as `smtp_ok` on every fleet row. A disconnected inbox
in an ACTIVE campaign has silently stopped sending — the most urgent failure on
the whole dashboard — so it gets a very visible, always-on alert.

We split disconnections into:
  - critical  : smtp_ok false AND in an ACTIVE campaign (production, not sending)
  - idle      : smtp_ok false but only in paused/completed campaigns or none
Newly-provisioning warming inboxes (never yet connected) are excluded from the
critical count so setup churn doesn't cry wolf.
"""

import db as store
import health_replace as hr


def disconnected_view() -> dict:
    data, ts = store.cache_get("health_fleet")
    fleet = (data or {}).get("inboxes", []) if isinstance(data, dict) else []
    status_map = hr.campaign_status_map()

    def _active(camps):
        return [c for c in (camps or []) if status_map.get(c) == "ACTIVE"]

    critical, idle = [], []
    for r in fleet:
        # smtp_ok can be True / False / None. Only an explicit False is a
        # disconnection; None = unknown (skip, don't false-alarm).
        if r.get("smtp_ok") is not False:
            continue
        camps = r.get("campaigns") or []
        active = _active(camps)
        row = {
            "email": r.get("email"),
            "domain": r.get("domain"),
            "client": r.get("client"),
            "status": r.get("status"),
            "campaigns": camps,
            "active_campaigns": active,
            "in_warmup": r.get("status") == "warming",
        }
        if active:
            critical.append(row)
        else:
            idle.append(row)

    # worst first: critical by client then email
    critical.sort(key=lambda x: (x.get("client") or "", x.get("email") or ""))
    return {
        "generated_at": ts,
        "critical_count": len(critical),
        "idle_count": len(idle),
        "critical": critical,
        "idle": idle,
    }
