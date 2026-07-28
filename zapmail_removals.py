"""Zapmail mailbox-removal watcher + Slack notifier.

Why this exists
---------------
When we schedule a domain's mailboxes for removal (via the scheduled-removal
API *or* manually in the Zapmail UI), Zapmail deletes them on the domain's next
monthly billing date. Zapmail support then wants us to *confirm once they are
actually removed* so they can optimise (reduce) our mailbox-slot billing.

Zapmail's domains API does NOT expose a "scheduled for removal" flag — a
scheduled domain looks identical to an active one (status ACTIVE, autoRenew is
false fleet-wide, mailboxes ACTIVE). So we cannot poll for "what's scheduled".

The only reliable signal is the mailbox *actually disappearing* (or flipping to
EXPIRED) from the inventory on the billing date. That is exactly the event we
want to tell Zapmail about, and it catches API-scheduled and manually-cancelled
mailboxes equally. So the tool is:

  1. A daily full snapshot of every Zapmail mailbox.
  2. A diff against yesterday's snapshot -> any mailbox that vanished / expired
     was removed -> post a Slack notification ("tell Zapmail to optimise billing").
  3. A registry of what we've *told* Zapmail to cancel, so a pending list is
     available and each removal can be labelled expected vs. unexpected.

Runs daily by piggybacking the existing health-snapshot cron (Vercel Hobby only
allows one cron). Also exposed at /api/zapmail-removals for manual runs.
"""

import os
import time
from datetime import datetime, timezone

import requests

import db as store

ZK = (os.environ.get("ZAPMAIL_API_KEY") or "").strip()
ZBASE = "https://api.zapmail.ai/api/v2"
ZH = {
    "Content-Type": "application/json",
    "x-auth-zapmail": ZK,
    "x-service-provider": "GOOGLE",
}

# Dedicated channel webhook, else fall back to the repo-wide alerts webhook.
SLACK_WEBHOOK = (
    os.environ.get("SLACK_ZAPMAIL_WEBHOOK")
    or os.environ.get("SLACK_WEBHOOK_URL")
    or ""
).strip()

SNAP_KEY = "zm_mailbox_snapshot"      # last full inventory {email: {...}}
REG_KEY = "zm_removal_registry"       # {email: {domain, source, first_seen, removed_date, notified}}
PENDING_MSG_KEY = "zm_removal_pending_msgs"  # queued Slack msgs when no webhook set

# Domains we scheduled for removal via the API (seed for the registry).
API_SCHEDULED_DOMAINS = [
    # 16 fully-dead client domains
    "exteriorgroundswork.info", "groundscarebase.co", "groundscarefocus.co",
    "groundskeepingexperts.info", "groundsmaintenanceservices.info",
    "landscapeworkpros.info", "landscapingservicecrew.info", "lawncarepartners.info",
    "lawncarepros.info", "outdoorcareplus.co", "turfmanagementpros.info",
    "turfservicefocus.co", "turfservicegroup.info", "turfservicepros.info",
    "yardmanagementcrew.info", "yardworkexperts.info",
    # 2 acquisition domains
    "headlinetheory360group.info", "headlinetheory360hub.info",
]


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Zapmail inventory
# ---------------------------------------------------------------------------

def _get_page(page):
    for _ in range(8):
        try:
            r = requests.get(f"{ZBASE}/domains?page={page}&limit=100", headers=ZH, timeout=90)
            if r.status_code == 200 and r.text.strip():
                return r.json().get("data", {})
        except requests.RequestException:
            pass
        time.sleep(5)
    return None


def snapshot_mailboxes():
    """Full scan of every Zapmail mailbox. Returns
    {email: {"domain": d, "id": mbx_id, "status": s}} or None if the scan
    could not be completed (never overwrite a good snapshot with a partial one).
    """
    out, page, tot, failed = {}, 1, 99, 0
    while page <= tot:
        d = _get_page(page)
        if not d:
            failed += 1
            page += 1
            continue
        tot = d.get("totalPages", 1)
        for dom in d.get("domains", []):
            dn = dom.get("domain")
            for m in (dom.get("mailboxes") or []):
                email = f"{m.get('username')}@{dn}"
                out[email] = {"domain": dn, "id": m.get("id"), "status": m.get("status")}
        page += 1
    if failed:
        # Partial scan -> unsafe to diff (would look like mass removals).
        return None
    return out


# ---------------------------------------------------------------------------
# Registry (what we've told Zapmail to cancel)
# ---------------------------------------------------------------------------

def _registry():
    return (store.get_state(REG_KEY) or {}).get("entries", {})


def _save_registry(entries):
    store.set_state(REG_KEY, {"entries": entries, "updated": _now_iso()})


def register_domains(domains, source="manual", current=None):
    """Add every current mailbox on the given domains to the pending-cancellation
    registry. `current` is a snapshot dict (fetched if not supplied)."""
    if current is None:
        current = snapshot_mailboxes() or {}
    reg = _registry()
    added = 0
    dset = {d.lower() for d in domains}
    for email, info in current.items():
        if info.get("domain", "").lower() in dset and email not in reg:
            reg[email] = {
                "domain": info["domain"], "source": source,
                "first_seen": _today(), "removed_date": None, "notified": False,
            }
            added += 1
    _save_registry(reg)
    return {"added": added, "registry_size": len(reg)}


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

def post_slack(message):
    """Post to the Zapmail-billing Slack channel. If no webhook is configured,
    queue the message in state so a Claude session can flush it via the Slack MCP.
    Returns 'webhook' | 'queued'."""
    if SLACK_WEBHOOK:
        try:
            r = requests.post(SLACK_WEBHOOK, json={"text": message}, timeout=10)
            if r.status_code in (200, 201):
                return "webhook"
        except requests.RequestException:
            pass
    pend = (store.get_state(PENDING_MSG_KEY) or {}).get("messages", [])
    pend.append({"text": message, "ts": _now_iso()})
    store.set_state(PENDING_MSG_KEY, {"messages": pend})
    return "queued"


def flush_pending():
    """Return and clear queued Slack messages (for a session to post via MCP)."""
    msgs = (store.get_state(PENDING_MSG_KEY) or {}).get("messages", [])
    if msgs:
        store.set_state(PENDING_MSG_KEY, {"messages": []})
    return msgs


def _format_alert(removed):
    """removed: list of {email, domain, source, client}."""
    n = len(removed)
    by_dom = {}
    for r in removed:
        by_dom.setdefault(r["domain"], []).append(r)
    lines = [
        f":wastebasket: *Zapmail cancellation confirmed* — {n} mailbox"
        f"{'es' if n != 1 else ''} removed on {_today()}.",
        "Message Zapmail so they can optimise billing for these slots:",
        "",
    ]
    for dom in sorted(by_dom):
        rs = by_dom[dom]
        src = rs[0].get("source") or "unknown"
        tag = "" if src != "unexpected" else "  :warning: *not in our cancel list — check!*"
        lines.append(f"• *{dom}* ({len(rs)} mbx, {src}){tag}")
        for r in rs:
            lines.append(f"      – {r['email']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main daily check
# ---------------------------------------------------------------------------

def check_removals(dry_run=False):
    """Snapshot the fleet, diff against the last snapshot, notify on removals.
    A mailbox counts as removed if it was ACTIVE last time and is now gone or
    EXPIRED. Safe to run repeatedly (already-notified removals aren't re-sent)."""
    current = snapshot_mailboxes()
    if current is None:
        return {"error": "zapmail scan incomplete — skipped (snapshot not updated)"}

    prev = store.get_state(SNAP_KEY) or {}
    prev_mbx = prev.get("mailboxes", {})

    result = {"scanned": len(current), "removed": [], "first_run": not prev_mbx}

    if not prev_mbx:
        # First run: establish the baseline, notify nothing.
        if not dry_run:
            store.set_state(SNAP_KEY, {"mailboxes": current, "taken": _now_iso()})
        result["note"] = "baseline established"
        return result

    reg = _registry()
    removed = []
    for email, info in prev_mbx.items():
        was_active = (info or {}).get("status") == "ACTIVE"
        cur = current.get(email)
        gone = cur is None
        expired = cur is not None and cur.get("status") == "EXPIRED"
        if was_active and (gone or expired):
            r = reg.get(email)
            if r and r.get("notified"):
                continue  # already told about this one
            removed.append({
                "email": email,
                "domain": info.get("domain") or email.split("@")[-1],
                "source": (r or {}).get("source", "unexpected"),
                "reason": "expired" if expired else "gone",
            })

    result["removed"] = removed

    if removed and not dry_run:
        delivery = post_slack(_format_alert(removed))
        result["slack"] = delivery
        # mark registry entries removed+notified
        for r in removed:
            e = r["email"]
            if e in reg:
                reg[e]["removed_date"] = _today()
                reg[e]["notified"] = True
            else:
                reg[e] = {"domain": r["domain"], "source": "unexpected",
                          "first_seen": _today(), "removed_date": _today(), "notified": True}
        _save_registry(reg)

    if not dry_run:
        store.set_state(SNAP_KEY, {"mailboxes": current, "taken": _now_iso()})

    return result


def pending_summary(current=None):
    """Registry entries not yet removed, grouped by domain — 'what's still
    scheduled to cancel'. Verifies against a live snapshot if provided."""
    reg = _registry()
    live = current if current is not None else (store.get_state(SNAP_KEY) or {}).get("mailboxes", {})
    pending, removed = {}, {}
    for email, r in reg.items():
        bucket = removed if r.get("removed_date") else pending
        bucket.setdefault(r["domain"], []).append(email)
    return {
        "pending_domains": len(pending),
        "pending_mailboxes": sum(len(v) for v in pending.values()),
        "pending": pending,
        "removed_domains": len(removed),
        "removed_mailboxes": sum(len(v) for v in removed.values()),
        "removed": removed,
    }
