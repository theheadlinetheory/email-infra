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

import bisect
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
HEARTBEAT_KEY = "zm_removal_last_check"      # {ts, ok, scanned, error, fails} — see _beat()

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


DOMID_KEY = "zm_domain_ids"   # cached {domain: zapmail_domain_id}, refreshed on each full scan


def snapshot_mailboxes():
    """Full scan of every Zapmail mailbox. Returns
    {email: {"domain": d, "id": mbx_id, "status": s}} or None if the scan
    could not be completed (never overwrite a good snapshot with a partial one).
    Side effect: on a complete scan, refreshes the cached domain-name -> id map
    (DOMID_KEY) so cancel_domains() can resolve ids instantly."""
    out, dom_ids, page, tot, failed = {}, {}, 1, 99, 0
    while page <= tot:
        d = _get_page(page)
        if not d:
            failed += 1
            page += 1
            continue
        tot = d.get("totalPages", 1)
        for dom in d.get("domains", []):
            dn = dom.get("domain")
            if dn and dom.get("id"):
                dom_ids[dn] = dom.get("id")
            for m in (dom.get("mailboxes") or []):
                email = f"{m.get('username')}@{dn}"
                out[email] = {"domain": dn, "id": m.get("id"), "status": m.get("status"),
                              # createdAt is what lets us derive the deletion date
                              # (see derive_removal_dates)
                              "created": m.get("createdAt")}
        page += 1
    if failed:
        # Partial scan -> unsafe to diff (would look like mass removals).
        return None
    if dom_ids:
        store.set_state(DOMID_KEY, {"map": dom_ids, "taken": _now_iso()})
    return out


def resolve_domain_ids(domains):
    """Map domain names -> Zapmail ids. Reads the cached map first; live-scans
    (paged, early-exit) only for domains not in the cache."""
    cached = (store.get_state(DOMID_KEY) or {}).get("map", {})
    want = {d.lower(): d for d in domains}
    found, missing = {}, []
    for low, orig in want.items():
        # cache keys are the canonical domain names
        hit = next((v for k, v in cached.items() if k.lower() == low), None)
        if hit:
            found[orig] = hit
        else:
            missing.append(orig)
    if missing:
        miss = {m.lower() for m in missing}
        page, tot = 1, 99
        while page <= tot and miss:
            d = _get_page(page)
            if not d:
                page += 1
                continue
            tot = d.get("totalPages", 1)
            for dom in d.get("domains", []):
                dn = (dom.get("domain") or "")
                if dn.lower() in miss and dom.get("id"):
                    found[want[dn.lower()]] = dom.get("id")
                    miss.discard(dn.lower())
            page += 1
    return {"found": found, "missing": [d for d in domains if d not in found]}


def cancel_domains(domains, dry_run=True, source="dashboard-cancel"):
    """Schedule a Zapmail whole-domain removal (mailboxes deleted on next billing
    date) AND register the domains with the removal watcher so the Slack bot alerts
    when they actually cancel. `domains` is a list of domain names.

    Dry-run resolves ids and reports what would be cancelled without calling Zapmail."""
    if not domains:
        return {"error": "no domains given"}
    res = resolve_domain_ids(domains)
    found, missing = res["found"], res["missing"]
    plan = {"resolved": found, "missing": missing,
            "count": len(found), "not_in_zapmail": missing}
    if dry_run:
        return {"dry_run": True, **plan}
    if not found:
        return {"error": "none of these domains resolve to a Zapmail id "
                         "(external / already removed?)", **plan}
    r = requests.put(f"{ZBASE}/mailboxes/scheduled-removal", headers=ZH,
                     json={"domainIds": list(found.values()), "remove": True}, timeout=90)
    ok = r.status_code in (200, 201)
    reg = register_domains(list(found.keys()), source=source) if ok else {"added": 0}
    return {"ok": ok, "status_code": r.status_code, "response": r.text[:200],
            "scheduled": list(found.keys()), "missing": missing,
            "registered": reg.get("added", 0), **({} if ok else {"error": "zapmail rejected the request"})}


# ---------------------------------------------------------------------------
# Deletion dates
#
# Zapmail's UI shows "scheduled for deletion on <date>" per mailbox, but NO
# public API field carries it — /domains omits it entirely and there is no
# subscription->domain join (probed: /mailboxes 500s, /subscriptions/{id},
# ?includeDomains, /domains/{id}/mailboxes all 404).
#
# It is derivable, though. Mailboxes are provisioned seconds after the
# subscription that pays for them, and Zapmail deletes a scheduled mailbox on
# that subscription's billing date (`periodEnd`). So: match each mailbox's
# createdAt to the newest subscription created at-or-before it.
#
# Verified against the UI: Lars' 18 mailboxes (subscription created
# 2026-04-10T01:46:36, mailboxes 01:46:40) derive to 2026-08-09 — exactly the
# date the Zapmail dashboard shows.
# ---------------------------------------------------------------------------

def fetch_subscriptions():
    """ACTIVE subscriptions sorted by creation time, or None if the call failed."""
    try:
        r = requests.get(f"{ZBASE}/subscriptions", headers=ZH, timeout=60)
    except requests.RequestException:
        return None
    if r.status_code != 200 or not r.text.strip():
        return None
    subs = [s for s in (r.json().get("data") or [])
            if s.get("subscriptionStatus") == "ACTIVE"
            and s.get("subscriptionCreationDate") and s.get("periodEnd")]
    subs.sort(key=lambda s: s["subscriptionCreationDate"])
    return subs


def derive_removal_dates(current, subs=None):
    """{email: 'YYYY-MM-DD'} — the date each mailbox is due to be deleted.

    Best-effort: a mailbox created long after its subscription (e.g. a
    replacement slot) can match a newer subscription and come out wrong, so the
    absence-detection diff stays the source of truth for what ACTUALLY happened.
    This only drives the heads-up alert."""
    if subs is None:
        subs = fetch_subscriptions()
    if not subs:
        return {}
    keys = [s["subscriptionCreationDate"] for s in subs]
    out = {}
    for email, info in (current or {}).items():
        created = (info or {}).get("created")
        if not created:
            continue
        i = bisect.bisect_right(keys, created) - 1
        if i >= 0:
            out[email] = subs[i]["periodEnd"][:10]
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
                "expected_date": None, "date_notified": False,
            }
            added += 1
    # stamp the derived deletion dates so the pending list shows them immediately
    try:
        for email, d in derive_removal_dates(current).items():
            if email in reg and not reg[email].get("expected_date"):
                reg[email]["expected_date"] = d
    except Exception:
        pass
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


def _format_due_alert(due):
    """The alert Tim acts on: these mailboxes hit their Zapmail billing date
    today, so message Zapmail to cut the slot billing."""
    n = len(due)
    by_dom = {}
    for r in due:
        by_dom.setdefault(r["domain"], []).append(r)
    dates = sorted({r["date"] for r in due})
    when = dates[0] if len(dates) == 1 else f"{dates[0]}–{dates[-1]}"
    lines = [
        f":calendar: *Zapmail deletion date reached* — {n} mailbox"
        f"{'es' if n != 1 else ''} scheduled for deletion on {when}.",
        "*Message Zapmail now to optimise the billing for these slots.*",
        "",
    ]
    for dom in sorted(by_dom):
        rs = by_dom[dom]
        lines.append(f"• *{dom}* ({len(rs)} mbx, {rs[0].get('source') or 'unknown'})"
                     f" — due {rs[0]['date']}")
    lines += ["", "_Confirmation that they actually disappeared follows in a "
                  "separate alert once the inventory scan sees them gone._"]
    return "\n".join(lines)


def check_due(reg, dates, dry_run=False):
    """Fire once per mailbox on/after its scheduled deletion date.

    Uses `<= today` rather than `== today` so a missed run doesn't skip the
    alert entirely — it arrives late instead of never."""
    today = _today()
    due = []
    for email, e in reg.items():
        if e.get("removed_date") or e.get("date_notified"):
            continue
        d = e.get("expected_date") or dates.get(email)
        if d and d <= today:
            due.append({"email": email, "domain": e.get("domain") or email.split("@")[-1],
                        "date": d, "source": e.get("source")})
    if due and not dry_run:
        post_slack(_format_due_alert(due))
        for x in due:
            reg[x["email"]]["date_notified"] = True
    return due


# ---------------------------------------------------------------------------
# Main daily check
# ---------------------------------------------------------------------------

def _beat(ok, dry_run, scanned=None, error=None):
    """Record the outcome of a daily check so a silent watcher is distinguishable
    from a quiet one. Without this, 'no Slack alert' means either 'nothing was
    removed' or 'the check has been dead for a week' — and we'd act on the wrong
    one. Alerts once a failure looks sustained (2 in a row), then weekly."""
    if dry_run:
        return
    prev = store.get_state(HEARTBEAT_KEY) or {}
    fails = 0 if ok else int(prev.get("fails") or 0) + 1
    store.set_state(HEARTBEAT_KEY, {
        "ts": _now_iso(), "ok": ok, "scanned": scanned, "error": error,
        "fails": fails, "last_ok": _now_iso() if ok else prev.get("last_ok"),
    })
    if fails == 2 or (fails > 2 and fails % 7 == 0):
        post_slack(
            f":warning: Zapmail removal watcher has failed {fails} runs in a row "
            f"— removals may be going undetected. Last error: {error or 'unknown'}. "
            f"Last good run: {prev.get('last_ok') or 'never'}.")


def check_removals(dry_run=False):
    """Snapshot the fleet, diff against the last snapshot, notify on removals.
    A mailbox counts as removed if it was ACTIVE last time and is now gone or
    EXPIRED. Safe to run repeatedly (already-notified removals aren't re-sent)."""
    try:
        current = snapshot_mailboxes()
    except Exception as e:                       # network/API blowup, not just a bad page
        _beat(False, dry_run, error=f"{type(e).__name__}: {e}")
        return {"error": f"zapmail scan raised: {e}"}
    if current is None:
        _beat(False, dry_run, error="incomplete scan (a page failed)")
        return {"error": "zapmail scan incomplete — skipped (snapshot not updated)"}

    prev = store.get_state(SNAP_KEY) or {}
    prev_mbx = prev.get("mailboxes", {})

    result = {"scanned": len(current), "removed": [], "first_run": not prev_mbx}

    if not prev_mbx:
        # First run: establish the baseline, notify nothing.
        if not dry_run:
            store.set_state(SNAP_KEY, {"mailboxes": current, "taken": _now_iso()})
        result["note"] = "baseline established"
        _beat(True, dry_run, scanned=len(current))
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

    # Refresh the derived deletion dates, then alert on anything that has
    # reached its date. This is the alert Tim acts on — absence-detection only
    # confirms afterwards, which is too late to tell Zapmail on the day.
    try:
        dates = derive_removal_dates(current)
    except Exception as e:                       # never let this break the diff
        dates, result["dates_error"] = {}, f"{type(e).__name__}: {e}"
    for email, e in reg.items():
        d = dates.get(email)
        if d and e.get("expected_date") != d:
            e["expected_date"] = d
    result["due"] = check_due(reg, dates, dry_run=dry_run)

    if not dry_run:
        _save_registry(reg)
        store.set_state(SNAP_KEY, {"mailboxes": current, "taken": _now_iso()})

    _beat(True, dry_run, scanned=len(current))
    return result


def pending_summary(current=None):
    """Registry entries not yet removed, grouped by domain — 'what's still
    scheduled to cancel'. Verifies against a live snapshot if provided."""
    reg = _registry()
    live = current if current is not None else (store.get_state(SNAP_KEY) or {}).get("mailboxes", {})
    pending, removed = {}, {}
    by_date, dom_date = {}, {}
    for email, r in reg.items():
        bucket = removed if r.get("removed_date") else pending
        bucket.setdefault(r["domain"], []).append(email)
        if not r.get("removed_date"):
            d = r.get("expected_date") or "unknown"
            by_date.setdefault(d, []).append(email)
            dom_date[r["domain"]] = d
    return {
        "pending_domains": len(pending),
        "pending_mailboxes": sum(len(v) for v in pending.values()),
        "pending": pending,
        # when each pending mailbox is due to be deleted (derived — see
        # derive_removal_dates); this is what the on-the-date alert fires on
        "due_by_date": {d: len(v) for d, v in sorted(by_date.items())},
        "domain_due_date": dict(sorted(dom_date.items())),
        "removed_domains": len(removed),
        "removed_mailboxes": sum(len(v) for v in removed.values()),
        "removed": removed,
        # Proof the watcher is alive — an empty 'removed' means nothing if the
        # daily check hasn't actually run.
        "last_check": store.get_state(HEARTBEAT_KEY) or {"ts": None, "note": "never run"},
        "snapshot_taken": (store.get_state(SNAP_KEY) or {}).get("taken"),
    }
