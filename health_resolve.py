"""Auto-resolve SmartLead disconnected / flagged inboxes.

Sits on top of the disconnected alert. It classifies every flagged SmartLead
account, AUTO-RECONNECTS the ones whose Zapmail source is healthy (a re-export
re-auths OAuth on the same SmartLead row — tags are preserved), and ESCALATES
the rest to Tim: source gone/empty (needs a delete decision), a warmup block, or
an unknown error type.

Hard rules (from Aidan Hutchinson's 2026-07-01 resolver design, adapted to this
repo's primitives):
  - NEVER auto-delete. Deletes are only ever surfaced as an approval list.
  - NEVER create/duplicate a row. Operate on existing SmartLead accounts only.
  - Reconnect is non-destructive (re-export re-auths the same row) -> safe to auto-run.
  - Anything outside the taxonomy is escalated, never guessed at.

Reconnect primitive: POST {ZBASE}/v2/exports/mailboxes {apps:["SMARTLEAD"],
contains:<domain>} re-exports every mailbox on the domain, reconnecting OAuth.
"""

import os
import time

import requests

import db as store

SL_KEY = (os.environ.get("SMARTLEAD_API_KEY") or "").strip()
SL_BASE = "https://server.smartlead.ai/api/v1"
ZK = (os.environ.get("ZAPMAIL_API_KEY") or "").strip()
ZBASE = "https://api.zapmail.ai/api"

# error types
ERR_INVALID_GRANT = "invalid_grant"
ERR_MNE = "mail_service_not_enabled"
ERR_SMTP_FAIL = "smtp_fail"
ERR_MAILBOX_GONE = "mailbox_gone"
ERR_UNKNOWN = "unknown"

# buckets -> what we do
B_RECONNECT = "reconnect"          # auto-fix: re-export
B_WARMUP = "warmup"                # escalate: warmup block (needs delete+re-export / Zapmail)
B_DELETE = "delete"                # escalate: source gone/empty (needs delete approval)
B_UNKNOWN = "escalate_unknown"     # escalate: unrecognised error


def _sl(path):
    """Resilient SmartLead GET (survives 429/empty). Returns json or None."""
    sep = "&" if "?" in path else "?"
    url = f"{SL_BASE}{path}{sep}api_key={SL_KEY}"
    for _ in range(6):
        try:
            r = requests.get(url, timeout=30)
            if r.status_code == 200 and (r.text or "").strip():
                return r.json()
            if r.status_code == 429:
                time.sleep(8)
                continue
        except requests.RequestException:
            pass
        time.sleep(5)
    return None


def _domain(a):
    e = a.get("from_email") or a.get("email") or ""
    return e.split("@")[-1].lower() if "@" in e else ""


def is_flagged(a):
    wd = a.get("warmup_details") or {}
    return bool(wd.get("blocked_reason")) or a.get("is_smtp_success") is False \
        or a.get("is_imap_success") is False


def error_type_of(a):
    reason = ((a.get("warmup_details") or {}).get("blocked_reason") or "")
    low = reason.lower()
    if "invalid_grant" in low:
        return ERR_INVALID_GRANT
    if "mail service not enabled" in low:
        return ERR_MNE
    if "does not exist" in low or "mailbox_does_not_exist" in low:
        return ERR_MAILBOX_GONE
    if reason:
        return ERR_UNKNOWN
    if a.get("is_smtp_success") is False or a.get("is_imap_success") is False:
        return ERR_SMTP_FAIL
    return ERR_UNKNOWN


def list_flagged():
    """Every flagged SmartLead account (paginated). Returns [{id,email,domain,smtp,imap,blocked}]."""
    out, offset = [], 0
    for _ in range(40):
        accs = _sl(f"/email-accounts/?offset={offset}&limit=100")
        if not isinstance(accs, list) or not accs:
            break
        for a in accs:
            if is_flagged(a):
                out.append({
                    "id": a.get("id"),
                    "email": (a.get("from_email") or a.get("email") or "").lower(),
                    "domain": _domain(a),
                    "smtp": a.get("is_smtp_success"),
                    "imap": a.get("is_imap_success"),
                    "blocked": (a.get("warmup_details") or {}).get("blocked_reason"),
                    "error_type": error_type_of(a),
                })
        if len(accs) < 100:
            break
        offset += 100
        time.sleep(0.3)
    return out


def _zapmail_domain_index():
    """{domain: mailbox_count} from the (daily-refreshed) Zapmail snapshot. A
    domain not present == source gone; present == ACTIVE source."""
    snap = (store.get_state("zm_mailbox_snapshot") or {}).get("mailboxes", {})
    idx = {}
    for info in snap.values():
        d = (info.get("domain") or "").lower()
        if d:
            idx[d] = idx.get(d, 0) + 1
    return idx


def _fresh_source_check(domains):
    """Live Zapmail mailbox count for specific domains across BOTH providers — so
    an Outlook domain (absent from the Google-only snapshot) isn't mis-flagged as
    source-gone. Small, targeted, early-exits once all found. {domain: count}."""
    want = {d.lower() for d in domains if d}
    found = {}
    if not want:
        return found
    for prov in ("GOOGLE", "MICROSOFT"):
        h = {"x-auth-zapmail": ZK, "Content-Type": "application/json", "x-service-provider": prov}
        page, tot = 1, 99
        while page <= tot and (want - set(found)):
            try:
                r = requests.get(f"{ZBASE}/v2/domains?page={page}&limit=100", headers=h, timeout=60)
                data = r.json().get("data", {}) if r.status_code == 200 else {}
            except requests.RequestException:
                data = {}
            tot = data.get("totalPages", 1)
            for dom in data.get("domains", []):
                nm = (dom.get("domain") or "").lower()
                if nm in want:
                    found[nm] = len(dom.get("mailboxes") or [])
            page += 1
    return found


def _bucket(error_type, in_zapmail):
    if not in_zapmail:                       # source gone/empty
        return B_DELETE
    if error_type == ERR_UNKNOWN:
        return B_UNKNOWN
    if error_type == ERR_MNE:
        return B_WARMUP
    return B_RECONNECT                       # invalid_grant / smtp_fail / mailbox_gone on live source


def diagnose():
    """Classify every flagged account into a bucket. Source existence uses the
    Google snapshot first, then a fresh both-provider check for any domain the
    snapshot doesn't have (avoids false 'source gone' for Outlook / stale cache)."""
    flagged = list_flagged()
    cached = _zapmail_domain_index()
    missing = sorted({f["domain"] for f in flagged if f["domain"] and f["domain"] not in cached})
    fresh = _fresh_source_check(missing) if missing else {}
    for f in flagged:
        cnt = cached.get(f["domain"]) if f["domain"] in cached else fresh.get(f["domain"], 0)
        f["mailbox_count"] = cnt or 0
        f["in_zapmail"] = (cnt or 0) > 0
        f["bucket"] = _bucket(f["error_type"], f["in_zapmail"])
    return flagged


def _reexport(domain):
    """Reconnect every mailbox on a domain via Zapmail re-export (re-auths OAuth)."""
    try:
        r = requests.post(f"{ZBASE}/v2/exports/mailboxes",
                          headers={"x-auth-zapmail": ZK, "Content-Type": "application/json"},
                          json={"apps": ["SMARTLEAD"], "contains": domain}, timeout=60)
        return r.status_code in (200, 201)
    except requests.RequestException:
        return False


def resolve(dry_run=True, verify_wait=12):
    """Diagnose, auto-reconnect the reconnect bucket (re-export per domain), and
    escalate the rest. dry_run classifies only. Returns a structured report."""
    findings = diagnose()
    by_bucket = {}
    for f in findings:
        by_bucket.setdefault(f["bucket"], []).append(f)

    reconnect = by_bucket.get(B_RECONNECT, [])
    report = {
        "flagged": len(findings),
        "counts": {b: len(v) for b, v in by_bucket.items()},
        "reconnect_domains": sorted({f["domain"] for f in reconnect}),
        "escalations": {
            "delete_approvals": by_bucket.get(B_DELETE, []),
            "warmup_blocked": by_bucket.get(B_WARMUP, []),
            "unknown": by_bucket.get(B_UNKNOWN, []),
        },
    }
    if dry_run:
        report["dry_run"] = True
        return report

    # AUTO-FIX: one re-export per affected domain (fixes all its flagged inboxes)
    domains = report["reconnect_domains"]
    triggered = [d for d in domains if _reexport(d)]
    report["reexported_domains"] = triggered

    # verify: re-pull the flagged set; anything previously reconnect-bucketed that
    # is no longer flagged has recovered.
    if triggered:
        time.sleep(verify_wait)
        still = {f["email"] for f in list_flagged()}
        recovered = [f["email"] for f in reconnect if f["email"] not in still]
        report["recovered"] = recovered
        report["reconnect_still_down"] = [f["email"] for f in reconnect if f["email"] in still]

    try:
        store.log_monitor_event("health_resolve", {
            "flagged": len(findings), "reexported": len(triggered),
            "recovered": len(report.get("recovered", []))})
    except Exception:
        pass
    return report
