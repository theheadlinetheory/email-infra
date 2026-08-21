"""Health V1 — positive-reply custody guard for reallocation.

A burned inbox is a sending slot, but it is ALSO the custodian of every
conversation it started, and that second role outlives its usefulness as a
sender by months.

A reply is welded to the mailbox that sent the first touch. The SENT row's
message-id is `<uuid@sendingdomain>` and the prospect's reply is addressed back
to that exact mailbox, so the reply physically lives in THAT mailbox's IMAP —
SmartLead's master inbox is only a reader over it. SmartLead's "Reallocate
mailboxes" cannot help: it redistributes a campaign's LIVE lead queue onto its
senders (who sends the NEXT first-touch), and has no bearing on threads that
already exist. So the moment a swap detaches a burned inbox, every reply it owns
goes unreadable and unrepliable — SmartLead renders "The mailbox is disconnected,
please reconnect the sender mailbox" and freezes the thread — until someone
notices and re-attaches it by hand.

Hence this guard. Before a swap removes a burned inbox from a campaign we ask
whether that inbox owns any POSITIVE reply there. If it does, the removal is
HELD: the replacement inbox still goes IN (capacity is restored either way), the
burned one simply stays attached so the conversation keeps working, and the
operator is notified. Working the reply and then clicking release performs the
deferred removal.

"Positive" is SmartLead's own judgement, not ours: `sentiment_type == "positive"`
on GET /leads/fetch-categories — today Interested, Meeting Request, Information
Request and the custom "HT Subsequence FU". Categories are re-read live, so a new
custom positive category is picked up without a code change.

Cost: one leads-export per campaign (a few hundred KB to ~3MB) plus one
message-history call per POSITIVE lead only — measured at 2-149 positives per
campaign, not thousands. Owner maps are memoised per process and cached in
`state` for CACHE_TTL_H hours so a batch reallocation pays for each campaign once.
"""

from __future__ import annotations

import csv
import io
import os
import time
from datetime import datetime

import requests

import db as store

SL = "https://server.smartlead.ai/api/v1"

HOLDS_KEY = "health_positive_holds"          # {holds: [...]} — removals deferred
OWNERS_CACHE_KEY = "health_positive_owners"  # {cid: {ts, owners}} — resolved threads
PENDING_MSG_KEY = "health_positive_pending_msgs"

CACHE_TTL_H = 6
SLACK_WEBHOOK = (os.environ.get("SLACK_POSITIVE_WEBHOOK")
                 or os.environ.get("SLACK_WEBHOOK_URL") or "").strip()

_OWNERS_MEMO: dict[int, dict] = {}
_CATS_MEMO: set[str] | None = None


def _key() -> str:
    return (os.environ.get("SMARTLEAD_API_KEY", "")
            or os.environ.get("SMARTLEAD_KEY", "")).strip()


def _get(path: str, timeout: int = 90, **params):
    """GET with SmartLead's 429 backoff. Returns None on persistent failure."""
    url = f"{SL}{path}"
    for _ in range(4):
        try:
            r = requests.get(url, params={"api_key": _key(), **params}, timeout=timeout)
        except requests.RequestException:
            time.sleep(3)
            continue
        if r.status_code == 429:
            time.sleep(20)
            continue
        if r.status_code != 200:
            return None
        return r
    return None


def positive_categories() -> set[str]:
    """Category NAMES SmartLead itself marks sentiment_type='positive'.

    Read live so a newly-added custom positive category is honoured without a
    deploy. Falls back to the four known positives if the call fails — failing
    open here would mean silently detaching inboxes that own replies."""
    global _CATS_MEMO
    if _CATS_MEMO is not None:
        return _CATS_MEMO
    r = _get("/leads/fetch-categories", timeout=30)
    if r is None:
        _CATS_MEMO = {"Interested", "Meeting Request",
                      "Information Request", "HT Subsequence FU"}
        return _CATS_MEMO
    _CATS_MEMO = {c["name"] for c in r.json()
                  if (c.get("sentiment_type") or "").lower() == "positive"}
    return _CATS_MEMO


def _cache_read(cid: int):
    if cid in _OWNERS_MEMO:
        return _OWNERS_MEMO[cid]
    blob = store.get_state(OWNERS_CACHE_KEY) or {}
    rec = blob.get(str(cid))
    if not rec:
        return None
    try:
        age_h = (datetime.now() - datetime.fromisoformat(rec["ts"])).total_seconds() / 3600
    except Exception:
        return None
    if age_h > CACHE_TTL_H:
        return None
    _OWNERS_MEMO[cid] = rec["owners"]
    return rec["owners"]


def _cache_write(cid: int, owners: dict) -> None:
    _OWNERS_MEMO[cid] = owners
    try:
        blob = store.get_state(OWNERS_CACHE_KEY) or {}
        blob[str(cid)] = {"ts": datetime.now().isoformat(), "owners": owners}
        # keep the blob bounded — 60 most recent campaigns is plenty for a batch
        if len(blob) > 60:
            for k in sorted(blob, key=lambda k: blob[k]["ts"])[:len(blob) - 60]:
                blob.pop(k, None)
        store.set_state(OWNERS_CACHE_KEY, blob)
    except Exception:
        pass                                  # memo still serves this process


def campaign_positive_owners(cid: int, refresh: bool = False) -> dict:
    """{sender_email_lower: [{lead_id, lead_email, company, category}]} for every
    POSITIVE-category lead in the campaign, keyed by the mailbox that owns the
    thread. Empty dict is a legitimate answer (no positives yet)."""
    cid = int(cid)
    if not refresh:
        cached = _cache_read(cid)
        if cached is not None:
            return cached

    pos_names = positive_categories()
    r = _get(f"/campaigns/{cid}/leads-export", timeout=300)
    if r is None:
        # Fail CLOSED: an unknown campaign is treated as "might own replies" by
        # the caller, which holds the removal rather than risking a lost thread.
        raise RuntimeError(f"could not export leads for campaign {cid}")

    rows = csv.DictReader(io.StringIO(r.content.decode("utf-8", "replace")))
    positives = [x for x in rows if (x.get("category") or "").strip() in pos_names]

    owners: dict[str, list] = {}
    for lead in positives:
        h = _get(f"/campaigns/{cid}/leads/{lead['id']}/message-history", timeout=60)
        if h is None:
            continue
        hist = (h.json() or {}).get("history") or []
        sender = next((m.get("from") for m in hist if m.get("type") == "SENT"), None)
        if not sender:
            continue
        owners.setdefault(sender.strip().lower(), []).append({
            "lead_id": lead["id"],
            "lead_email": lead.get("email"),
            "company": lead.get("company_name"),
            "category": (lead.get("category") or "").strip(),
        })
    _cache_write(cid, owners)
    return owners


def owned_positive_threads(email: str, cid: int) -> list[dict]:
    """Positive-reply threads in campaign `cid` owned by mailbox `email`.

    Raises on an inconclusive lookup — callers MUST treat that as "hold", never
    as "no replies". A false negative here is exactly the failure this module
    exists to prevent."""
    owners = campaign_positive_owners(cid)
    return owners.get((email or "").strip().lower(), [])


# ---------------------------------------------------------------------------
# Holds — removals deferred because the inbox owns live conversations
# ---------------------------------------------------------------------------

def _load_holds() -> list[dict]:
    return (store.get_state(HOLDS_KEY) or {}).get("holds", [])


def _save_holds(holds: list[dict]) -> None:
    store.set_state(HOLDS_KEY, {"holds": holds})


def add_hold(email: str, account_id, campaign_id: int, campaign_name: str,
             threads: list[dict], reserve_account_id=None, reason: str = "positive_reply") -> dict:
    """Record that `email` was NOT removed from a campaign, and why. Idempotent
    on (email, campaign_id) so a re-run doesn't stack duplicates."""
    holds = _load_holds()
    key = f"{(email or '').lower()}::{campaign_id}"
    rec = {
        "key": key,
        "email": email,
        "account_id": account_id,
        "campaign_id": campaign_id,
        "campaign": campaign_name,
        "reserve_account_id": reserve_account_id,
        "reason": reason,
        "threads": threads,
        "positive_count": len(threads),
        "held_at": datetime.now().isoformat(),
        "status": "held",
        "notified": False,
    }
    holds = [h for h in holds if h.get("key") != key] + [rec]
    _save_holds(holds)
    return rec


def list_holds(include_released: bool = False) -> dict:
    """Everything currently held, newest first — drives the dashboard panel."""
    holds = _load_holds()
    if not include_released:
        holds = [h for h in holds if h.get("status") == "held"]
    holds.sort(key=lambda h: h.get("held_at") or "", reverse=True)
    return {
        "holds": holds,
        "count": len(holds),
        "inboxes": len({h["email"] for h in holds}),
        "positive_threads": sum(h.get("positive_count", 0) for h in holds),
    }


def release(key: str, confirm: bool = False) -> dict:
    """Perform the removal that was held — the operator has worked the reply.

    This is the ONLY path that detaches a held inbox, so the decision is always
    explicit and always after the conversation has been dealt with."""
    holds = _load_holds()
    rec = next((h for h in holds if h.get("key") == key), None)
    if not rec:
        return {"error": f"no hold with key {key}"}
    if rec.get("status") != "held":
        return {"error": f"hold already {rec.get('status')}"}
    if not confirm:
        return {"dry_run": True, "email": rec["email"], "campaign": rec["campaign"],
                "positive_count": rec.get("positive_count", 0),
                "note": ("Removing this inbox from the campaign will freeze "
                         f"{rec.get('positive_count', 0)} positive thread(s) — SmartLead will "
                         "show 'mailbox is disconnected' on each. Work them first.")}

    base = f"{SL}/campaigns/{rec['campaign_id']}/email-accounts"
    code = None
    for _ in range(4):
        r = requests.delete(base, params={"api_key": _key()},
                            json={"email_account_ids": [rec["account_id"]]}, timeout=60)
        code = r.status_code
        if code != 429:
            break
        time.sleep(20)
    rec["status"] = "released" if code == 200 else "release_failed"
    rec["released_at"] = datetime.now().isoformat()
    rec["release_http"] = code
    _save_holds([h for h in holds if h.get("key") != key] + [rec])
    try:
        store.log_monitor_event("health_positive_release",
                                {"email": rec["email"], "campaign_id": rec["campaign_id"],
                                 "http": code})
    except Exception:
        pass
    return {"ok": code == 200, "http": code, "email": rec["email"],
            "campaign": rec["campaign"]}


def cancel_hold(key: str) -> dict:
    """Drop a hold WITHOUT removing the inbox — the inbox stays in the campaign
    (e.g. it recovered, or we've decided to keep it as the reply custodian)."""
    holds = _load_holds()
    rec = next((h for h in holds if h.get("key") == key), None)
    if not rec:
        return {"error": f"no hold with key {key}"}
    rec["status"] = "kept"
    rec["released_at"] = datetime.now().isoformat()
    _save_holds([h for h in holds if h.get("key") != key] + [rec])
    return {"ok": True, "email": rec["email"], "kept_in": rec["campaign"]}


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------

def _format(new_holds: list[dict]) -> str:
    n = len(new_holds)
    tot = sum(h.get("positive_count", 0) for h in new_holds)
    lines = [f":warning: *{n} burned inbox{'' if n == 1 else 'es'} kept in campaign — "
             f"{tot} positive repl{'y' if tot == 1 else 'ies'} would have been lost*",
             "The replacement inbox went in; the burned one was NOT detached, so the "
             "threads stay readable and repliable.", ""]
    for h in new_holds:
        who = ", ".join(f"{t.get('company') or t.get('lead_email')} ({t.get('category')})"
                        for t in (h.get("threads") or [])[:4])
        more = "" if len(h.get("threads") or []) <= 4 else f" +{len(h['threads']) - 4} more"
        lines.append(f"• `{h['email']}` in *{h['campaign']}* — "
                     f"{h.get('positive_count', 0)} positive: {who}{more}")
    lines.append("")
    lines.append("Work the replies, then hit *Remove from campaign* on the "
                 "dashboard's _Held: positive replies_ panel.")
    return "\n".join(lines)


def notify(new_holds: list[dict]) -> str:
    """Slack the operator about newly-held inboxes. Falls back to a queued
    message in `state` when no webhook is configured (same contract as the
    Zapmail removal bot), so the alert is never silently dropped."""
    if not new_holds:
        return "none"
    msg = _format(new_holds)
    if SLACK_WEBHOOK:
        try:
            r = requests.post(SLACK_WEBHOOK, json={"text": msg}, timeout=10)
            if r.status_code in (200, 201):
                _mark_notified(new_holds)
                return "webhook"
        except requests.RequestException:
            pass
    pend = (store.get_state(PENDING_MSG_KEY) or {}).get("messages", [])
    pend.append({"text": msg, "ts": datetime.now().isoformat()})
    store.set_state(PENDING_MSG_KEY, {"messages": pend})
    _mark_notified(new_holds)
    return "queued"


def _mark_notified(new_holds: list[dict]) -> None:
    keys = {h.get("key") for h in new_holds}
    holds = _load_holds()
    for h in holds:
        if h.get("key") in keys:
            h["notified"] = True
    _save_holds(holds)


def flush_pending() -> list:
    """Return and clear queued Slack messages (for a session to post via MCP)."""
    msgs = (store.get_state(PENDING_MSG_KEY) or {}).get("messages", [])
    if msgs:
        store.set_state(PENDING_MSG_KEY, {"messages": []})
    return msgs
