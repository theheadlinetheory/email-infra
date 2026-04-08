#!/usr/bin/env python3
"""THT Infrastructure Dashboard — local web server.

Works both locally (reads .env file) and hosted (reads environment variables).
"""

import gc
import json
import os
import sys
import time
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timedelta
from pathlib import Path
import threading

# Limit concurrent heavy API calls to prevent OOM on 512MB Render
_api_lock = threading.Lock()
from pipeline import (
    create_pipeline, load_pipeline, load_all_pipelines,
    run_pipeline_steps, save_pipeline, start_monitor_thread,
)
from zapmail_ops import (
    zm_get_wallet_balance, zm_get_domain_health,
    zm_get_subscriptions, zm_get_subscription_mailboxes,
    zm_get_placement_results, zm_get_placement_eligible_mailboxes,
    zm_run_placement_test, zm_get_placement_credits,
)
from sheets import get_available_domains, get_domain_summary, get_all_master_domains, write_range, setup_client_tab
from setup import (
    sl_get_all_tags, sl_find_or_create_tag, sl_tag_account,
    zm_list_domain_tags, zm_create_domain_tag, zm_assign_domain_tag,
    zm_set_forwarding, zm_list_domains,
    SMARTLEAD_API, SMARTLEAD_KEY, SMARTLEAD_INTERNAL_API,
    sl_internal_headers as setup_sl_internal_headers,
)

SCRIPT_DIR = Path(__file__).parent
ENV_PATH = SCRIPT_DIR / ".env"


def load_env():
    """Load from .env file if present, then fall back to os.environ."""
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


load_env()

import db as store

SMARTLEAD_API = "https://server.smartlead.ai/api/v1"
SMARTLEAD_INTERNAL_API = "https://server.smartlead.ai/api"
SMARTLEAD_KEY = os.environ.get("SMARTLEAD_API_KEY", "")
SMARTLEAD_JWT = os.environ.get("SMARTLEAD_JWT", "").strip().replace("\n", "").replace(" ", "")
ZAPMAIL_API = "https://api.zapmail.ai/api"
ZAPMAIL_KEY = os.environ.get("ZAPMAIL_API_KEY", "")
PORKBUN_API = "https://api.porkbun.com/api/json/v3"
PORKBUN_KEY = os.environ.get("PORKBUN_API_KEY", "")
PORKBUN_SECRET = os.environ.get("PORKBUN_SECRET_KEY", "")
SPACESHIP_API = "https://spaceship.dev/api/v1"
SPACESHIP_KEY = os.environ.get("SPACESHIP_API_KEY", "")
SPACESHIP_SECRET = os.environ.get("SPACESHIP_SECRET_KEY", "")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")


def sl_internal_headers():
    return {"Authorization": f"Bearer {SMARTLEAD_JWT}", "Content-Type": "application/json"}


def sl_list_accounts(offset=0, limit=100):
    r = requests.get(
        f"{SMARTLEAD_API}/email-accounts/?api_key={SMARTLEAD_KEY}&offset={offset}&limit={limit}",
        timeout=30,
    )
    return r.json()


# --- ZapMail API helpers ---

def zm_headers():
    return {"x-auth-zapmail": ZAPMAIL_KEY, "Content-Type": "application/json"}


def zm_list_domains():
    """List all ZapMail domains with pagination."""
    all_domains = []
    page = 1
    while True:
        r = requests.get(f"{ZAPMAIL_API}/v2/domains?page={page}", headers=zm_headers(), timeout=30)
        data = r.json() if r.status_code == 200 else {}
        if isinstance(data, dict) and "data" in data:
            domains = data["data"].get("domains", [])
            all_domains.extend(domains)
            if page >= data["data"].get("totalPages", 1):
                break
            page += 1
        else:
            break
    return all_domains


def zm_delete_domains(domain_ids):
    """Delete domains from ZapMail (stops billing). Domains stay on Spaceship."""
    r = requests.delete(
        f"{ZAPMAIL_API}/v2/domains",
        headers=zm_headers(),
        json={"domainIds": domain_ids},
        timeout=30,
    )
    return r.json() if r.status_code == 200 else {"error": r.text[:300], "status": r.status_code}


# --- Domain Registrar helpers ---

def porkbun_list_domains():
    """List all Porkbun domains with expiry dates."""
    if not PORKBUN_KEY or not PORKBUN_SECRET:
        return []
    r = requests.post(
        f"{PORKBUN_API}/domain/listAll",
        json={"apikey": PORKBUN_KEY, "secretapikey": PORKBUN_SECRET},
        timeout=30,
    )
    data = r.json()
    if data.get("status") != "SUCCESS":
        return []
    result = []
    for d in data.get("domains", []):
        result.append({
            "domain": d.get("domain", ""),
            "registrar": "porkbun",
            "status": d.get("status", "UNKNOWN"),
            "expires": d.get("expireDate", "")[:10],
            "auto_renew": d.get("autoRenew") == "1",
            "created": d.get("createDate", "")[:10],
        })
    return result


def spaceship_list_domains():
    """List all Spaceship domains with expiry dates."""
    if not SPACESHIP_KEY or not SPACESHIP_SECRET:
        return []
    headers = {
        "X-API-Key": SPACESHIP_KEY,
        "X-API-Secret": SPACESHIP_SECRET,
        "Content-Type": "application/json",
    }
    all_domains = []
    skip = 0
    while True:
        r = requests.get(
            f"{SPACESHIP_API}/domains",
            headers=headers, timeout=30,
            params={"take": 100, "skip": skip},
        )
        if r.status_code != 200:
            break
        data = r.json()
        items = data.get("items", []) if isinstance(data, dict) else []
        if not items:
            break
        for d in items:
            all_domains.append({
                "domain": d.get("name", ""),
                "registrar": "spaceship",
                "status": d.get("lifecycleStatus", "UNKNOWN"),
                "expires": d.get("expirationDate", "")[:10],
                "auto_renew": d.get("autoRenew", False),
                "created": d.get("registrationDate", "")[:10],
            })
        if len(items) < 100:
            break
        skip += 100
    return all_domains


def porkbun_set_auto_renew(domain, enabled):
    """Toggle auto-renew on a Porkbun domain. Returns success dict."""
    r = requests.post(
        f"{PORKBUN_API}/domain/updateAutoRenew/{domain}",
        json={
            "apikey": PORKBUN_KEY,
            "secretapikey": PORKBUN_SECRET,
            "status": "on" if enabled else "off",
        },
        timeout=15,
    )
    data = r.json()
    return {"success": data.get("status") == "SUCCESS", "message": data.get("message", "")}


# --- SmartLead API helpers ---

def get_clients():
    r = requests.get(f"{SMARTLEAD_API}/client?api_key={SMARTLEAD_KEY}", timeout=30)
    return r.json() if r.status_code == 200 else []


def get_accounts_by_client(client_id):
    accounts = []
    offset = 0
    while True:
        r = requests.get(
            f"{SMARTLEAD_API}/email-accounts/?api_key={SMARTLEAD_KEY}"
            f"&client_id={client_id}&offset={offset}&limit=100",
            timeout=30,
        )
        batch = r.json() if r.status_code == 200 else []
        if not isinstance(batch, list):
            break
        accounts.extend(batch)
        if len(batch) < 100:
            break
        offset += 100
    return accounts


_accounts_cache = {"data": None, "time": 0}

def get_all_accounts():
    """Fetch all accounts with 30-second cache to prevent duplicate fetches."""
    now = time.time()
    if _accounts_cache["data"] is not None and now - _accounts_cache["time"] < 30:
        return _accounts_cache["data"]
    accounts = []
    offset = 0
    while True:
        batch = sl_list_accounts(offset=offset, limit=100)
        if isinstance(batch, list):
            accounts.extend(batch)
            if len(batch) < 100:
                break
            offset += 100
        else:
            break
    _accounts_cache["data"] = accounts
    _accounts_cache["time"] = now
    return accounts


def assign_accounts_to_client(account_ids, client_id):
    success = 0
    fail = 0
    for acc_id in account_ids:
        body = {"id": acc_id, "clientId": client_id}
        r = requests.post(
            f"{SMARTLEAD_INTERNAL_API}/email-account/save-management-details",
            headers=sl_internal_headers(),
            json=body,
            timeout=30,
        )
        if r.status_code == 200 and r.json().get("ok"):
            success += 1
        else:
            fail += 1
        time.sleep(0.15)
    return {"success": success, "fail": fail}


_health_cache = {"data": None, "time": 0}

def get_health_metrics(days=7):
    """Get per-inbox health metrics with 30-second cache."""
    now = time.time()
    if _health_cache["data"] is not None and now - _health_cache["time"] < 30:
        return _health_cache["data"]
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    r = requests.get(
        f"{SMARTLEAD_INTERNAL_API}/analytics/mailbox/name-wise-health-metrics",
        headers=sl_internal_headers(),
        params={"start_date": start, "end_date": end, "timezone": "America/New_York", "full_data": "true"},
        timeout=30,
    )
    if r.status_code != 200:
        return _health_cache["data"] or {}
    data = r.json()
    metrics = data.get("data", {}).get("email_health_metrics", [])
    result = {m["from_email"]: m for m in metrics}
    _health_cache["data"] = result
    _health_cache["time"] = now
    return result


def get_warmup_start_dates():
    """Read warmup start dates from client configs in Supabase."""
    dates = {}
    try:
        for c in store.load_all_client_configs():
            name = c.get("client_name", "")
            ws = c.get("infrastructure", {}).get("warmup_start_date", "")
            if name and ws:
                dates[name.lower()] = ws
    except Exception as e:
        print(f"WARN: Could not load client configs: {e}")
    return dates


def parse_rate(value):
    """Parse a rate value that may be a string like '0.00%' or a float."""
    if value is None:
        return None
    s = str(value).strip().rstrip("%")
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def calculate_health_score(account, health_data, in_warmup_period=False):
    """Calculate health score (0-100) for an inbox.

    During warmup (first 14 days): score = reputation only.
    After warmup: reputation (80%) + campaign reply rate (20%).
    No-data defaults to 100 (new/unused accounts are healthy).
    Returns dict with score and list of flag reasons.
    """
    email = account.get("from_email", "")
    h = health_data.get(email, {})
    wd = account.get("warmup_details") or {}
    flags = []

    # Warmup reputation — primary replacement signal
    # 100pts at ≥99%, linear 0-100 across 95-99%, 0pts at ≤95%
    rep_raw = wd.get("warmup_reputation", "?")
    try:
        rep = float(rep_raw)
        if rep >= 99:
            rep_score = 100
        elif rep <= 95:
            rep_score = 0
            flags.append("reputation")
        else:
            rep_score = ((rep - 95) / 4) * 100
            flags.append("reputation")
    except (ValueError, TypeError):
        rep_score = 100  # no data = healthy

    # Reply rate only matters with enough send volume (100+ emails)
    # Below that, rates swing wildly from single events
    total_sent = h.get("sent", 0) or 0
    if in_warmup_period or total_sent < 100:
        score = round(rep_score)
        return {"score": score, "flags": flags}

    # Sufficient data: add campaign reply rate (20%)
    # 100pts at ≥2%, linear 0-100 across 0.5-2%, 0pts at ≤0.5%
    rr = parse_rate(h.get("reply_rate"))
    if rr is not None:
        if rr >= 2:
            reply_score = 100
        elif rr <= 0.5:
            reply_score = 0
            flags.append("reply")
        else:
            reply_score = ((rr - 0.5) / 1.5) * 100
    else:
        reply_score = 100

    score = round(rep_score * 0.80 + reply_score * 0.20)

    return {"score": score, "flags": flags}


def group_accounts_by_domain(accounts_with_scores):
    """Group accounts by domain. If ANY account on a domain is flagged,
    mark ALL accounts on that domain as flagged (domain-level rollup)."""
    by_domain = {}
    for acc in accounts_with_scores:
        domain = acc["email"].split("@")[-1] if "@" in acc["email"] else ""
        if domain not in by_domain:
            by_domain[domain] = []
        by_domain[domain].append(acc)

    for domain, accs in by_domain.items():
        domain_has_flags = any(a["health_flags"] for a in accs)
        for a in accs:
            a["domain_flagged"] = domain_has_flags

    return by_domain


# --- API endpoint logic ---

def api_overview():
    gc.collect()  # Free memory before heavy operation
    clients = get_clients()
    all_accounts = get_all_accounts()
    warmup_dates = get_warmup_start_dates()
    health = get_health_metrics()

    total = len(all_accounts)
    in_campaign = sum(1 for a in all_accounts if a.get("campaign_count", 0) > 0)
    smtp_fail = sum(1 for a in all_accounts if not a.get("is_smtp_success"))
    imap_fail = sum(1 for a in all_accounts if not a.get("is_imap_success"))
    unassigned = sum(1 for a in all_accounts if not a.get("client_id"))
    blocked = [
        {
            "email": a["from_email"],
            "reason": (a.get("warmup_details") or {}).get("blocked_reason", "Unknown"),
        }
        for a in all_accounts
        if (a.get("warmup_details") or {}).get("status") not in ("ACTIVE", None)
        and (a.get("warmup_details") or {}).get("blocked_reason")
    ]

    # Client summaries — exclude acquisition groups and generic groups (shown in their own sections)
    def _is_acquisition_group(name):
        nl = name.lower()
        return "group" in nl and ("/" in name or "day" in nl)

    def _is_generic_group(name):
        return name.lower().startswith("generic")

    client_summaries = []
    for cl in clients:
        if _is_acquisition_group(cl.get("name", "")) or _is_generic_group(cl.get("name", "")):
            continue
        cl_accounts = [a for a in all_accounts if a.get("client_id") == cl["id"]]
        if not cl_accounts:
            continue
        ws_date = warmup_dates.get(cl["name"].lower(), "")
        ready_date = ""
        days_left = None
        if ws_date:
            try:
                ws = datetime.strptime(ws_date, "%Y-%m-%d")
                ready = ws + timedelta(days=14)
                ready_date = ready.strftime("%Y-%m-%d")
                days_left = (ready - datetime.now()).days
            except Exception:
                pass

        rotation_date = ""
        rotation_days = None
        if ws_date:
            try:
                ws = datetime.strptime(ws_date, "%Y-%m-%d")
                rot = ws + timedelta(weeks=6)
                rotation_date = rot.strftime("%Y-%m-%d")
                rotation_days = (rot - datetime.now()).days
            except Exception:
                pass

        # "Warming up" = still in 14-day warmup period (date-based, not warmup-enabled)
        cl_still_warming = len(cl_accounts) if days_left is not None and days_left > 0 else 0
        cl_campaigns = sum(1 for a in cl_accounts if a.get("campaign_count", 0) > 0)
        cl_smtp_fail = sum(1 for a in cl_accounts if not a.get("is_smtp_success"))
        cl_blocked = sum(
            1 for a in cl_accounts
            if (a.get("warmup_details") or {}).get("status") not in ("ACTIVE", None)
        )

        # Aggregate health metrics for this client
        cl_sent = 0
        cl_bounced = 0
        cl_replied = 0
        cl_health_count = 0
        cl_bounce_rates = []
        cl_reply_rates = []
        for a in cl_accounts:
            email = a.get("from_email", "")
            h = health.get(email)
            if h:
                cl_health_count += 1
                cl_sent += h.get("sent", 0)
                cl_bounced += h.get("bounced", 0)
                cl_replied += h.get("replied", 0)
                br_val = parse_rate(h.get("bounce_rate"))
                if br_val is not None:
                    cl_bounce_rates.append(br_val)
                rr_val = parse_rate(h.get("reply_rate"))
                if rr_val is not None:
                    cl_reply_rates.append(rr_val)

        avg_bounce = round(sum(cl_bounce_rates) / len(cl_bounce_rates), 1) if cl_bounce_rates else None
        avg_reply = round(sum(cl_reply_rates) / len(cl_reply_rates), 1) if cl_reply_rates else None

        # Health scores and domain flagging
        cl_in_warmup = days_left is not None and days_left > 0
        cl_scores = []
        flagged_domains = set()
        for a in cl_accounts:
            hs = calculate_health_score(a, health, in_warmup_period=cl_in_warmup)
            cl_scores.append(hs["score"])
            if hs["flags"]:
                domain = a.get("from_email", "").split("@")[-1]
                flagged_domains.add(domain)

        all_cl_domains = set(
            a.get("from_email", "").split("@")[-1] for a in cl_accounts
        )
        total_domains = len(all_cl_domains)
        flagged_pct = (len(flagged_domains) / total_domains * 100) if total_domains > 0 else 0
        avg_health = round(sum(cl_scores) / len(cl_scores)) if cl_scores else 0

        # Warmup progress — date-based (14-day warmup period)
        if days_left is not None and days_left > 0:
            warmup_days_done = 14 - days_left
            warmup_progress = f"Day {warmup_days_done}/14"
        elif ws_date:
            warmup_days_done = 14
            warmup_progress = "Complete"
        else:
            warmup_days_done = None
            warmup_progress = "—"

        client_summaries.append({
            "id": cl["id"],
            "name": cl["name"],
            "accounts": len(cl_accounts),
            "warming": cl_still_warming,
            "in_campaign": cl_campaigns,
            "smtp_failures": cl_smtp_fail,
            "blocked": cl_blocked,
            "warmup_start": ws_date,
            "ready_date": ready_date,
            "days_until_ready": days_left,
            "rotation_date": rotation_date,
            "days_until_rotation": rotation_days,
            "health_accounts": cl_health_count,
            "total_sent": cl_sent,
            "total_bounced": cl_bounced,
            "total_replied": cl_replied,
            "avg_bounce_rate": avg_bounce,
            "avg_reply_rate": avg_reply,
            "health_score": avg_health,
            "total_domains": total_domains,
            "flagged_domains": len(flagged_domains),
            "flagged_pct": round(flagged_pct, 1),
            "needs_attention": flagged_pct >= 15,
            "warmup_progress": warmup_progress,
            "warmup_days_done": warmup_days_done,
        })

    client_summaries.sort(
        key=lambda c: (
            0 if c["needs_attention"] else 1,
            0 if c["blocked"] > 0 or c["smtp_failures"] > 0 else 1,
            c["name"].lower(),
        )
    )

    attention_count = sum(1 for c in client_summaries if c["needs_attention"])

    # Global "warming up" = sum of accounts across clients still in 14-day warmup period
    total_warming = sum(c["warming"] for c in client_summaries)

    # Load paused clients list
    try:
        paused_state = store.get_state("paused_clients") or {"clients": []}
        paused_clients = paused_state.get("clients", [])
    except Exception:
        paused_clients = []

    # Load target volumes per client
    try:
        target_volumes = store.get_state("target_volumes") or {}
    except Exception:
        target_volumes = {}

    # Add capacity info to each client summary
    for cs in client_summaries:
        healthy = cs["accounts"] - cs["smtp_failures"] - cs["blocked"]
        capacity = healthy * 15
        target = target_volumes.get(cs["name"], 0)
        cs["healthy_inboxes"] = healthy
        cs["daily_capacity"] = capacity
        cs["target_volume"] = target
        if target > 0:
            shortfall = target - capacity
            cs["inboxes_needed"] = max(0, -(-shortfall // 15))  # ceiling division
            cs["capacity_status"] = "on_track" if capacity >= target else "need_more"
        else:
            cs["inboxes_needed"] = 0
            cs["capacity_status"] = "no_target"

    return {
        "total_accounts": total,
        "warming": total_warming,
        "in_campaign": in_campaign,
        "unassigned": unassigned,
        "smtp_failures": smtp_fail,
        "imap_failures": imap_fail,
        "blocked_accounts": blocked[:20],
        "clients": client_summaries,
        "attention_count": attention_count,
        "paused_clients": paused_clients,
        "generated_at": datetime.now().isoformat(),
    }


def get_campaign_counts_for_client(client_id):
    """Get per-email campaign count by checking actual campaigns (list API field is unreliable)."""
    counts = {}
    r = requests.get(
        f"{SMARTLEAD_API}/campaigns?api_key={SMARTLEAD_KEY}&client_id={client_id}",
        timeout=30,
    )
    campaigns = r.json() if r.status_code == 200 else []
    for camp in campaigns:
        if camp.get("status") not in ("ACTIVE", "PAUSED"):
            continue
        cr = requests.get(
            f"{SMARTLEAD_API}/campaigns/{camp['id']}/email-accounts?api_key={SMARTLEAD_KEY}",
            timeout=30,
        )
        camp_accounts = cr.json() if cr.status_code == 200 else []
        if isinstance(camp_accounts, list):
            for ca in camp_accounts:
                email = ca.get("from_email", "")
                counts[email] = counts.get(email, 0) + 1
    return counts


def api_client_accounts(client_id):
    accounts = get_accounts_by_client(int(client_id))
    health = get_health_metrics()
    campaign_counts = get_campaign_counts_for_client(int(client_id))

    # Determine if this client is still in warmup period
    clients = get_clients()
    client_name = ""
    for c in clients:
        if c["id"] == int(client_id):
            client_name = c["name"]
            break
    warmup_dates = get_warmup_start_dates()
    ws_date = warmup_dates.get(client_name.lower(), "")
    in_warmup = False
    if ws_date:
        try:
            ready = datetime.strptime(ws_date, "%Y-%m-%d") + timedelta(days=14)
            in_warmup = ready > datetime.now()
        except Exception:
            pass

    result = []
    for a in accounts:
        wd = a.get("warmup_details") or {}
        email = a.get("from_email", "")
        h = health.get(email, {})
        hs = calculate_health_score(a, health, in_warmup_period=in_warmup)
        result.append({
            "id": a["id"],
            "email": email,
            "domain": email.split("@")[-1],
            "warmup_status": wd.get("status", "UNKNOWN"),
            "warmup_sent": wd.get("total_sent_count", 0),
            "warmup_spam": wd.get("total_spam_count", 0),
            "warmup_reputation": wd.get("warmup_reputation", "?"),
            "blocked_reason": wd.get("blocked_reason"),
            "campaign_count": campaign_counts.get(email, 0),
            "daily_sent": a.get("daily_sent_count", 0),
            "smtp_ok": a.get("is_smtp_success", False),
            "imap_ok": a.get("is_imap_success", False),
            "bounce_rate": h.get("bounce_rate"),
            "reply_rate": h.get("reply_rate"),
            "health_sent": h.get("sent", 0),
            "health_bounced": h.get("bounced", 0),
            "health_replied": h.get("replied", 0),
            "health_score": hs["score"],
            "health_flags": hs["flags"],
        })

    # Domain-level rollup
    by_domain = group_accounts_by_domain(result)
    flagged_domains = [d for d, accs in by_domain.items() if any(a["health_flags"] for a in accs)]
    flagged_inbox_count = sum(len(by_domain[d]) for d in flagged_domains)

    return {
        "client_id": int(client_id),
        "accounts": result,
        "flagged_domains": flagged_domains,
        "flagged_inbox_count": flagged_inbox_count,
        "replacement_domains_needed": len(flagged_domains),
        "replacement_inboxes": len(flagged_domains) * 3,
    }


def api_unassigned():
    all_accounts = get_all_accounts()
    unassigned = [a for a in all_accounts if not a.get("client_id")]
    result = []
    for a in unassigned:
        wd = a.get("warmup_details") or {}
        result.append({
            "id": a["id"],
            "email": a.get("from_email", ""),
            "domain": a.get("from_email", "").split("@")[-1],
            "warmup_status": wd.get("status", "UNKNOWN"),
            "warmup_reputation": wd.get("warmup_reputation", "?"),
            "campaign_count": a.get("campaign_count", 0),
            "smtp_ok": a.get("is_smtp_success", False),
        })
    return {"accounts": result, "count": len(result)}


def api_debug_supabase():
    """Debug Supabase connection — shows key diagnostics and test result."""
    import base64
    key = store.SUPABASE_KEY
    url = store.SUPABASE_URL
    info = {
        "url": url,
        "key_length": len(key),
        "key_first10": key[:10] if key else "",
        "key_last10": key[-10:] if key else "",
        "key_has_newline": "\n" in key,
        "key_has_space": " " in key,
    }
    try:
        payload = key.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        decoded = json.loads(base64.b64decode(payload))
        info["jwt_role"] = decoded.get("role", "unknown")
        info["jwt_ref"] = decoded.get("ref", "unknown")
    except Exception as e:
        info["jwt_decode_error"] = str(e)
    try:
        result = store.get_state("paused_clients")
        info["test_query"] = "SUCCESS"
        info["test_result"] = result
    except Exception as e:
        info["test_query"] = "FAILED"
        info["test_error"] = str(e)
    return info


def api_acquisition():
    """Acquisition inbox groups with health metrics."""
    clients = get_clients()
    all_accounts = get_all_accounts()  # uses 30s cache — no extra API calls
    health = get_health_metrics()

    # Find group-named clients (e.g. "A Group (250/day)")
    group_clients = [
        c for c in clients
        if "group" in c.get("name", "").lower() and ("/" in c.get("name", "") or "day" in c.get("name", "").lower())
    ]

    groups = []
    total_accounts = 0
    for cl in sorted(group_clients, key=lambda x: x.get("name", "")):
        cl_accounts = [a for a in all_accounts if a.get("client_id") == cl["id"]]
        if not cl_accounts:
            continue

        total_accounts += len(cl_accounts)
        cl_scores = []
        warming = 0
        in_campaign = 0
        smtp_fail = 0
        cl_sent = 0
        cl_bounced = 0
        cl_replied = 0
        cl_bounce_rates = []
        cl_reply_rates = []
        flagged_domains = set()
        all_domains = set()

        for acc in cl_accounts:
            email = acc.get("from_email", "")
            domain = email.split("@")[-1] if "@" in email else ""
            all_domains.add(domain)

            hs = calculate_health_score(acc, health)
            cl_scores.append(hs["score"])
            if hs["flags"]:
                flagged_domains.add(domain)

            h = health.get(email, {})
            sent = h.get("sent", 0) or 0
            bounced = h.get("bounced", 0) or 0
            replied = h.get("replied", 0) or 0
            cl_sent += sent
            cl_bounced += bounced
            cl_replied += replied
            br_val = parse_rate(h.get("bounce_rate"))
            if br_val is not None:
                cl_bounce_rates.append(br_val)
            rr_val = parse_rate(h.get("reply_rate"))
            if rr_val is not None:
                cl_reply_rates.append(rr_val)

            if (acc.get("warmup_details") or {}).get("status") == "ACTIVE":
                warming += 1
            if (acc.get("campaign_count", 0) or 0) > 0:
                in_campaign += 1
            if not acc.get("is_smtp_success"):
                smtp_fail += 1

        avg_health = round(sum(cl_scores) / len(cl_scores)) if cl_scores else 100
        avg_bounce = round(sum(cl_bounce_rates) / len(cl_bounce_rates), 2) if cl_bounce_rates else 0
        avg_reply = round(sum(cl_reply_rates) / len(cl_reply_rates), 2) if cl_reply_rates else 0
        total_domains = len(all_domains)

        groups.append({
            "id": cl["id"],
            "name": cl["name"],
            "accounts": len(cl_accounts),
            "warming": warming,
            "in_campaign": in_campaign,
            "smtp_failures": smtp_fail,
            "total_sent": cl_sent,
            "total_bounced": cl_bounced,
            "total_replied": cl_replied,
            "avg_bounce_rate": avg_bounce,
            "avg_reply_rate": avg_reply,
            "health_score": avg_health,
            "total_domains": total_domains,
            "flagged_domains": len(flagged_domains),
            "flagged_pct": round(len(flagged_domains) / total_domains * 100) if total_domains else 0,
            "needs_attention": len(flagged_domains) / total_domains >= 0.15 if total_domains else False,
        })

    return {
        "groups": groups,
        "total_accounts": total_accounts,
        "total_groups": len(groups),
        "generated_at": datetime.now().isoformat(),
    }


def api_zapmail():
    """ZapMail domains grouped by tag (client), with renewal alerts."""
    domains = zm_list_domains()
    now = datetime.now()

    # Group by tag (client name)
    by_client = {}
    for d in domains:
        tags = [t.get("name", "") for t in d.get("tags", [])]
        client = tags[0] if tags else "Untagged"
        if client not in by_client:
            by_client[client] = []

        # Calculate next renewal: createdAt + N months
        created = d.get("createdAt", "")
        next_renewal = None
        days_until_renewal = None
        if created:
            try:
                created_dt = datetime.fromisoformat(created.replace("Z", "+00:00")).replace(tzinfo=None)
                # Monthly renewal on same day of month as creation
                renewal = created_dt
                while renewal <= now:
                    month = renewal.month + 1
                    year = renewal.year
                    if month > 12:
                        month = 1
                        year += 1
                    renewal = renewal.replace(year=year, month=month)
                next_renewal = renewal.strftime("%Y-%m-%d")
                days_until_renewal = (renewal - now).days
            except Exception:
                pass

        mailboxes = d.get("mailboxes", [])
        by_client[client].append({
            "id": d["id"],
            "domain": d["domain"],
            "status": d.get("status", "UNKNOWN"),
            "mailbox_count": int(d.get("assignedMailboxesCount", 0) or len(mailboxes)),
            "mailboxes": [m.get("username", "") + "@" + d["domain"] for m in mailboxes],
            "auto_renew": d.get("autoRenew", False),
            "created": created[:10] if created else "",
            "next_renewal": next_renewal,
            "days_until_renewal": days_until_renewal,
            "tags": tags,
        })

    # Build summary per client
    clients = []
    for client_name, client_domains in sorted(by_client.items()):
        total_mailboxes = sum(d["mailbox_count"] for d in client_domains)
        renewing_soon = [d for d in client_domains if d["days_until_renewal"] is not None and d["days_until_renewal"] <= 3]
        clients.append({
            "name": client_name,
            "domains": len(client_domains),
            "mailboxes": total_mailboxes,
            "renewing_soon": len(renewing_soon),
            "domain_list": client_domains,
        })

    # Sort: renewal alerts first
    clients.sort(key=lambda c: (0 if c["renewing_soon"] > 0 else 1, c["name"].lower()))

    return {
        "total_domains": len(domains),
        "total_mailboxes": sum(c["mailboxes"] for c in clients),
        "clients": clients,
        "generated_at": now.isoformat(),
    }


def api_domains():
    """All registered domains from Porkbun + Spaceship with expiry alerts."""
    now = datetime.now()
    all_domains = porkbun_list_domains() + spaceship_list_domains()

    for d in all_domains:
        if d["expires"]:
            try:
                exp = datetime.strptime(d["expires"], "%Y-%m-%d")
                d["days_until_expiry"] = (exp - now).days
            except Exception:
                d["days_until_expiry"] = None
        else:
            d["days_until_expiry"] = None

    # Sort by expiry (soonest first)
    all_domains.sort(key=lambda d: d.get("days_until_expiry") or 9999)

    expiring_soon = [d for d in all_domains if d["days_until_expiry"] is not None and d["days_until_expiry"] <= 14]
    no_auto_renew = [d for d in all_domains if not d["auto_renew"] and d["days_until_expiry"] is not None and d["days_until_expiry"] <= 30]

    # Group by registrar
    by_registrar = {}
    for d in all_domains:
        reg = d["registrar"]
        if reg not in by_registrar:
            by_registrar[reg] = []
        by_registrar[reg].append(d)

    return {
        "total_domains": len(all_domains),
        "expiring_soon": len(expiring_soon),
        "no_auto_renew_30d": len(no_auto_renew),
        "by_registrar": by_registrar,
        "alerts": [d for d in no_auto_renew if d["days_until_expiry"] <= 14],
        "generated_at": now.isoformat(),
    }


def api_zapmail_sync():
    """Check ZapMail tags vs SmartLead client assignments for mismatches."""
    zm_domains = zm_list_domains()
    sl_accounts = get_all_accounts()
    sl_clients = get_clients()

    # Build SmartLead client_id -> name map
    client_map = {c["id"]: c["name"] for c in sl_clients}

    # Build domain -> ZapMail tag
    zm_tag_by_domain = {}
    for d in zm_domains:
        tags = [t.get("name", "") for t in d.get("tags", [])]
        if tags:
            zm_tag_by_domain[d["domain"]] = tags[0]

    # Build domain -> SmartLead client name
    sl_client_by_domain = {}
    for a in sl_accounts:
        email = a.get("from_email", "")
        domain = email.split("@")[-1] if "@" in email else ""
        client_id = a.get("client_id")
        if domain and client_id and client_id in client_map:
            sl_client_by_domain[domain] = client_map[client_id]

    # Find mismatches (fuzzy: "Deeter Landscape" matches "Deeter Landscape LLC")
    mismatches = []
    all_domains = set(zm_tag_by_domain.keys()) | set(sl_client_by_domain.keys())
    for domain in sorted(all_domains):
        zm_client = zm_tag_by_domain.get(domain)
        sl_client = sl_client_by_domain.get(domain)
        if zm_client and sl_client:
            zm_lower = zm_client.lower().strip()
            sl_lower = sl_client.lower().strip()
            if zm_lower != sl_lower and zm_lower not in sl_lower and sl_lower not in zm_lower:
                mismatches.append({
                    "domain": domain,
                    "zapmail_tag": zm_client,
                    "smartlead_client": sl_client,
                })

    # Domains in ZapMail but not SmartLead
    zm_only = [d for d in zm_tag_by_domain if d not in sl_client_by_domain]
    # Domains in SmartLead but not ZapMail
    sl_only = [d for d in sl_client_by_domain if d not in zm_tag_by_domain]

    return {
        "mismatches": mismatches,
        "zapmail_only_count": len(zm_only),
        "smartlead_only_count": len(sl_only),
        "zapmail_only": sorted(zm_only)[:20],
        "smartlead_only": sorted(sl_only)[:20],
        "total_checked": len(all_domains),
    }


# --- Pipeline API logic ---

def api_pipeline_new_client(body):
    """Start a new client setup pipeline."""
    client_name = body.get("client_name", "")
    domain_count = body.get("domain_count", 0)
    forwarding_url = body.get("forwarding_url", "")
    selected_domains = body.get("domains", [])

    if not client_name or (not domain_count and not selected_domains):
        return {"error": "client_name and domain_count (or domains) required"}

    available = get_available_domains()
    if not available:
        return {"error": "No domains available in inventory"}

    if selected_domains:
        chosen = [d for d in available if d["domain"] in selected_domains]
    else:
        available.sort(key=lambda d: d.get("purchase_date", "9999"))
        chosen = available[:domain_count]

    if len(chosen) < (len(selected_domains) if selected_domains else domain_count):
        return {"error": f"Only {len(chosen)} domains available, need {domain_count}"}

    pipeline = create_pipeline("new_setup", client_name, chosen, forwarding_url)
    threading.Thread(target=run_pipeline_steps, args=(pipeline,), daemon=True).start()

    return {"pipeline_id": pipeline["id"], "status": "started", "domains": list(pipeline["domains"].keys())}


def api_pipeline_replacement(body):
    """Manually trigger replacement for a client/domain."""
    client_name = body.get("client_name", "")
    old_domain = body.get("old_domain", "")
    old_emails = body.get("old_emails", [])
    old_account_ids = body.get("old_account_ids", [])

    if not client_name:
        return {"error": "client_name required"}

    available = get_available_domains()
    if not available:
        return {"error": "No domains available in inventory"}

    available.sort(key=lambda d: d.get("purchase_date", "9999"))
    chosen = available[:1]

    pipeline = create_pipeline("replacement", client_name, chosen)
    if old_domain:
        pipeline["old_domains"] = [{
            "domain": old_domain,
            "emails": old_emails,
            "smartlead_account_ids": old_account_ids,
        }]
        save_pipeline(pipeline)

    threading.Thread(target=run_pipeline_steps, args=(pipeline,), daemon=True).start()

    return {"pipeline_id": pipeline["id"], "status": "started"}


def api_pipeline_active():
    """List all active pipelines."""
    try:
        all_p = load_all_pipelines()
    except Exception as e:
        print(f"WARN: Could not load pipelines: {e}")
        all_p = []
    result = []
    for p in all_p:
        result.append({
            "id": p["id"],
            "type": p["type"],
            "client_name": p["client_name"],
            "status": p["status"],
            "current_step": p.get("current_step", ""),
            "domains": list(p["domains"].keys()),
            "created_at": p.get("created_at", ""),
            "updated_at": p.get("updated_at", ""),
            "errors": p.get("errors", []),
            "pending_removals": p.get("pending_removals", {}),
        })
    result.sort(key=lambda p: p["created_at"], reverse=True)
    return {"pipelines": result}


def api_pipeline_detail(pipeline_id):
    """Get detailed status for a specific pipeline."""
    p = load_pipeline(pipeline_id)
    if not p:
        return {"error": "Pipeline not found"}
    return p


def api_inbox_campaigns(email):
    """List active campaigns containing this inbox."""
    r = requests.get(f"{SMARTLEAD_API}/campaign?api_key={SMARTLEAD_KEY}", timeout=30)
    campaigns = r.json() if r.status_code == 200 else []
    active = [c for c in campaigns if c.get("status") == "ACTIVE"]

    found = []
    for camp in active:
        cr = requests.get(
            f"{SMARTLEAD_API}/campaigns/{camp['id']}/email-accounts?api_key={SMARTLEAD_KEY}",
            timeout=30,
        )
        if cr.status_code == 200:
            accs = cr.json() if isinstance(cr.json(), list) else []
            for a in accs:
                if a.get("from_email") == email:
                    found.append({"campaign_id": camp["id"], "campaign_name": camp["name"]})
                    break

    return {"email": email, "campaigns": found}


def api_remove_from_campaign(body):
    """Remove an email account from a specific campaign."""
    email = body.get("email", "")
    campaign_id = body.get("campaign_id")
    if not email or not campaign_id:
        return {"error": "email and campaign_id required"}

    cr = requests.get(
        f"{SMARTLEAD_API}/campaigns/{campaign_id}/email-accounts?api_key={SMARTLEAD_KEY}",
        timeout=30,
    )
    if cr.status_code != 200:
        return {"error": "Failed to get campaign accounts"}

    accs = cr.json() if isinstance(cr.json(), list) else []
    acc_id = None
    for a in accs:
        if a.get("from_email") == email:
            acc_id = a.get("id")
            break

    if not acc_id:
        return {"error": f"{email} not found in campaign {campaign_id}"}

    dr = requests.delete(
        f"{SMARTLEAD_API}/campaigns/{campaign_id}/email-accounts/{acc_id}?api_key={SMARTLEAD_KEY}",
        timeout=30,
    )
    return {"success": dr.status_code == 200, "email": email, "campaign_id": campaign_id}


def api_remove_from_all_campaigns(body):
    """Remove an email account from all active campaigns."""
    email = body.get("email", "")
    if not email:
        return {"error": "email required"}

    campaigns_data = api_inbox_campaigns(email)
    results = []
    for camp in campaigns_data["campaigns"]:
        result = api_remove_from_campaign({"email": email, "campaign_id": camp["campaign_id"]})
        results.append(result)

    # If this inbox is in a pipeline awaiting removal, resume the pipeline
    all_p = load_all_pipelines()
    for p in all_p:
        if p.get("status") == "awaiting_removal" and p.get("pending_removals"):
            if email in p["pending_removals"]:
                del p["pending_removals"][email]
                if not p["pending_removals"]:
                    p["status"] = "running"
                    next_idx = p["steps"].index("check_campaigns") + 1
                    if next_idx < len(p["steps"]):
                        p["current_step"] = p["steps"][next_idx]
                    save_pipeline(p)
                    threading.Thread(target=run_pipeline_steps, args=(p,), daemon=True).start()
                else:
                    save_pipeline(p)

    return {"email": email, "removed_from": len(results), "results": results}


def api_wallet():
    """Get ZapMail wallet balance."""
    return zm_get_wallet_balance()


def api_domain_inventory():
    """Get available domain inventory from THT spreadsheet."""
    available = get_available_domains()
    summary = get_domain_summary()
    return {
        "available_count": len(available),
        "available_domains": [
            {
                "domain": d["domain"],
                "provider": d.get("provider", ""),
                "purchase_date": d.get("purchase_date", ""),
                "notes": d.get("notes", ""),
            }
            for d in available
        ],
        "summary": summary,
    }


def api_placement_tests():
    """Get placement test results."""
    return zm_get_placement_results()


def api_subscriptions():
    """Get ZapMail subscription/billing data."""
    return zm_get_subscriptions()


# --- Generic Groups ---

def api_generic_groups():
    """Generic group status with warmup progress for the dashboard."""
    clients = get_clients()
    all_accounts = get_all_accounts()
    health = get_health_metrics()

    # Find generic clients (e.g. "Generic A", "Generic B", etc.)
    generic_clients = [
        c for c in clients
        if c.get("name", "").lower().startswith("generic")
    ]

    # Try to load pipeline data for pipeline IDs
    try:
        all_pipelines = load_all_pipelines()
    except Exception:
        all_pipelines = []

    groups = []
    total_accounts = 0
    total_capacity = 0
    for cl in sorted(generic_clients, key=lambda x: x.get("name", "")):
        cl_accounts = [a for a in all_accounts if a.get("client_id") == cl["id"]]
        if not cl_accounts:
            continue

        total_accounts += len(cl_accounts)
        domains = set()
        smtp_fail = 0
        daily_cap = 0
        warmup_dates = []
        health_scores = []

        for acc in cl_accounts:
            email = acc.get("from_email", "")
            domain = email.split("@")[-1] if "@" in email else ""
            if domain:
                domains.add(domain)
            daily_cap += acc.get("message_per_day", 0) or 0
            if not acc.get("is_smtp_success"):
                smtp_fail += 1
            # Warmup start date
            wd = (acc.get("warmup_details") or {}).get("warmup_created_at", "")
            if wd:
                warmup_dates.append(wd[:10])
            # Health score
            hs = calculate_health_score(acc, health)
            health_scores.append(hs["score"])

        total_capacity += daily_cap
        avg_health = round(sum(health_scores) / len(health_scores)) if health_scores else 100

        # Calculate warmup progress
        earliest_warmup = min(warmup_dates) if warmup_dates else None
        days_warming = 0
        days_left = 0
        ready_date_str = ""
        warmup_start_str = ""
        status = "warming"

        if earliest_warmup:
            try:
                ws = datetime.strptime(earliest_warmup, "%Y-%m-%d")
                warmup_start_str = ws.strftime("%-m/%-d")
                days_warming = (datetime.now() - ws).days
                ready = ws + timedelta(days=14)
                ready_date_str = ready.strftime("%-m/%-d")
                days_left = max(0, (ready - datetime.now()).days)
                if days_left <= 0:
                    status = "ready"
            except Exception:
                pass

        # Find matching pipeline ID
        pipeline_id = ""
        cl_name_lower = cl["name"].lower().strip()
        for p in all_pipelines:
            if p.get("client_name", "").lower().strip() == cl_name_lower:
                pipeline_id = p.get("id", "")
                break

        groups.append({
            "name": cl["name"],
            "client_id": cl["id"],
            "pipeline_id": pipeline_id,
            "accounts": len(cl_accounts),
            "domains": len(domains),
            "daily_capacity": daily_cap,
            "smtp_failures": smtp_fail,
            "health_score": avg_health,
            "warmup_start": warmup_start_str,
            "ready_date": ready_date_str,
            "days_warming": days_warming,
            "days_left": days_left,
            "status": status,
        })

    return {
        "groups": groups,
        "total_accounts": total_accounts,
        "total_daily_capacity": total_capacity,
        "generated_at": datetime.now().isoformat(),
    }


# --- Client Reassignment ---

def api_clients_list():
    """Deduplicated client list from SmartLead + Google Sheet for the dropdown."""
    names = set()
    # SmartLead clients
    try:
        sl_clients = requests.get(
            f"{SMARTLEAD_API}/client?api_key={SMARTLEAD_KEY}", timeout=30
        ).json()
        for c in sl_clients:
            if c.get("name"):
                names.add(c["name"].strip())
    except Exception:
        pass
    # Google Sheet notes column (column D = client names for in-use domains)
    try:
        _, all_domains = get_all_master_domains()
        for d in all_domains:
            note = d.get("notes", "").strip()
            if note and d.get("status", "").lower().strip() == "in use":
                names.add(note)
    except Exception:
        pass
    # Remove generic names
    return {"clients": sorted(n for n in names if n and not n.lower().startswith("generic"))}


def assign_client_sse(pipeline_id, client_name, forwarding_domain, is_new_client):
    """Generator that yields SSE events for each step of the reassignment process."""
    import traceback

    def event(step, status, message=""):
        data = json.dumps({"step": step, "status": status, "message": message})
        return f"data: {data}\n\n"

    # Load the pipeline
    pipeline = store.load_pipeline(pipeline_id)
    if not pipeline:
        yield event(0, "error", "Pipeline not found")
        return

    domains = [d["domain"] for d in pipeline.get("purchased_domains", [])]
    if not domains:
        yield event(0, "error", "No domains in pipeline")
        return

    original_name = pipeline.get("client_name", "")

    # ── Step 1: SmartLead client ──
    yield event(1, "running")
    sl_client_id = None
    try:
        sl_clients = requests.get(
            f"{SMARTLEAD_API}/client?api_key={SMARTLEAD_KEY}", timeout=30
        ).json()
        client_lower = client_name.lower().strip()
        for c in sl_clients:
            if c["name"].lower().strip() == client_lower:
                sl_client_id = c["id"]
                break
        if not sl_client_id and not is_new_client:
            # Fuzzy match
            for c in sl_clients:
                cn = c["name"].lower().strip()
                if client_lower in cn or cn in client_lower:
                    sl_client_id = c["id"]
                    break
        if not sl_client_id:
            slug = client_name.lower().replace("'", "").replace(" ", "").replace("&", "")
            cl_email = f"tht.{slug}.client@gmail.com"
            cr = requests.post(
                f"{SMARTLEAD_API}/client/save?api_key={SMARTLEAD_KEY}",
                json={"name": client_name, "email": cl_email, "password": "THTclient2026!"},
                timeout=30,
            )
            if cr.status_code == 201:
                sl_client_id = cr.json().get("clientId")
            else:
                yield event(1, "error", f"Failed to create client: {cr.status_code} {cr.text[:200]}")
                return
        yield event(1, "done")
    except Exception as e:
        yield event(1, "error", str(e))
        return

    # ── Step 2: SmartLead tags ──
    yield event(2, "running")
    try:
        all_tags = sl_get_all_tags()
        # Find client tag (create if needed)
        client_tag_id = sl_find_or_create_tag(client_name, existing_tags=all_tags)
        # Find the generic tag to remove
        generic_tag_id = None
        for tag_name, tag_data in all_tags.items():
            if tag_name.lower().strip() == original_name.lower().strip():
                generic_tag_id = tag_data["id"]
                break

        # Get all SmartLead accounts for these domains
        our_domains = set(domains)
        our_accounts = []
        offset = 0
        while True:
            batch = requests.get(
                f"{SMARTLEAD_API}/email-accounts/?api_key={SMARTLEAD_KEY}&offset={offset}&limit=100",
                timeout=30,
            ).json()
            if isinstance(batch, list):
                for acc in batch:
                    email = acc.get("from_email", acc.get("email", ""))
                    domain = email.split("@")[-1] if "@" in email else ""
                    if domain in our_domains:
                        our_accounts.append(acc)
                if len(batch) < 100:
                    break
                offset += 100
            else:
                break

        if not our_accounts:
            yield event(2, "error", "No SmartLead accounts found for these domains")
            return

        # For each account, get current tags and replace generic with client
        for acc in our_accounts:
            acc_id = acc["id"]
            current_tags = [t["id"] for t in acc.get("tags", [])] if acc.get("tags") else []
            # Remove generic tag, add client tag
            new_tags = [t for t in current_tags if t != generic_tag_id]
            if client_tag_id and client_tag_id not in new_tags:
                new_tags.append(client_tag_id)
            sl_tag_account(acc_id, new_tags, client_id=sl_client_id)

        yield event(2, "done")
    except Exception as e:
        yield event(2, "error", f"{e}\n{traceback.format_exc()}")
        return

    # ── Step 3: SmartLead client assignment verification ──
    yield event(3, "running")
    try:
        # Already handled in step 2 via client_id param in sl_tag_account
        # Verify a sample account
        if our_accounts:
            sample = requests.get(
                f"{SMARTLEAD_API}/email-accounts/{our_accounts[0]['id']}/?api_key={SMARTLEAD_KEY}",
                timeout=30,
            ).json()
            assigned_client = sample.get("client_id") or sample.get("clientId")
            if assigned_client != sl_client_id:
                yield event(3, "error", f"Client assignment mismatch: expected {sl_client_id}, got {assigned_client}")
                return
        yield event(3, "done")
    except Exception as e:
        yield event(3, "error", str(e))
        return

    # ── Step 4: Zapmail domain tags ──
    yield event(4, "running")
    try:
        # Get all Zapmail domains and find ours
        zm_domains = zm_list_domains()
        zm_domain_ids = []
        for d in zm_domains:
            if d.get("domain") in our_domains:
                zm_domain_ids.append(d["id"])

        if zm_domain_ids:
            # Check if client tag already exists
            existing_tags = zm_list_domain_tags()
            tag_list = existing_tags.get("data", []) if isinstance(existing_tags, dict) else []
            client_zm_tag_id = None
            for t in tag_list:
                if t.get("name", "").lower().strip() == client_name.lower().strip():
                    client_zm_tag_id = t["id"]
                    break
            if not client_zm_tag_id:
                result = zm_create_domain_tag(client_name)
                if isinstance(result, dict) and "data" in result:
                    created_tags = result["data"]
                    if isinstance(created_tags, list) and created_tags:
                        client_zm_tag_id = created_tags[0].get("id")
                    elif isinstance(created_tags, dict):
                        client_zm_tag_id = created_tags.get("id")
            if client_zm_tag_id:
                zm_assign_domain_tag(zm_domain_ids, [client_zm_tag_id])
        yield event(4, "done")
    except Exception as e:
        yield event(4, "error", str(e))
        return

    # ── Step 5: Zapmail forwarding ──
    yield event(5, "running")
    try:
        if zm_domain_ids and forwarding_domain:
            fwd = forwarding_domain if forwarding_domain.startswith("http") else f"https://{forwarding_domain}"
            zm_set_forwarding(zm_domain_ids, fwd)
        yield event(5, "done")
    except Exception as e:
        yield event(5, "error", str(e))
        return

    # ── Step 6: Google Sheet ──
    yield event(6, "running")
    try:
        _, all_sheet_domains = get_all_master_domains()
        for sd in all_sheet_domains:
            if sd["domain"] in our_domains:
                write_range("THT Domains ", f"D{sd['row_number']}", [[client_name]])
        # Set up client tab
        setup_client_tab(client_name, list(our_domains))
        yield event(6, "done")
    except Exception as e:
        yield event(6, "error", str(e))
        return

    # ── Step 7: Pipeline record ──
    yield event(7, "running")
    try:
        pipeline["client_name"] = client_name
        pipeline["original_group"] = original_name
        pipeline["updated_at"] = datetime.now().isoformat()
        store.save_pipeline(pipeline)
        yield event(7, "done")
    except Exception as e:
        yield event(7, "error", str(e))
        return

    yield event(0, "complete")


# --- HTTP Server ---

class DashboardHandler(BaseHTTPRequestHandler):
    def _check_auth(self):
        """Simple password check via query param or cookie."""
        if not DASHBOARD_PASSWORD:
            return True
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if params.get("pw", [None])[0] == DASHBOARD_PASSWORD:
            return True
        cookie = self.headers.get("Cookie", "")
        if f"dashboard_pw={DASHBOARD_PASSWORD}" in cookie:
            return True
        self.send_response(401)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"""<!DOCTYPE html><html><body style="background:#1a1a2e;color:#eee;font-family:sans-serif;display:flex;justify-content:center;align-items:center;height:100vh;">
        <form method="GET" style="text-align:center"><h2>THT Dashboard</h2><input name="pw" type="password" placeholder="Password" style="padding:8px;font-size:16px;border-radius:6px;border:1px solid #0f3460;background:#16213e;color:#eee;"><br><br>
        <button type="submit" style="padding:8px 24px;background:#0f3460;color:#eee;border:1px solid #1a5276;border-radius:6px;cursor:pointer;">Login</button></form></body></html>""")
        return False

    def do_GET(self):
        if not self._check_auth():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        # Set auth cookie if password provided in URL
        pw = params.get("pw", [None])[0]

        if path == "/" or path == "/dashboard.html":
            self._serve_file("dashboard.html", "text/html", set_cookie=pw)
        elif path == "/headshots/sean_reynolds.png":
            self._serve_file("headshots/sean_reynolds.png", "image/png")
        elif path.startswith("/api/"):
            try:
                if path == "/api/overview":
                    self._json_response(api_overview())
                elif path == "/api/clients":
                    self._json_response(get_clients())
                elif path.startswith("/api/client/") and path.endswith("/accounts"):
                    client_id = path.split("/")[3]
                    self._json_response(api_client_accounts(client_id))
                elif path == "/api/unassigned":
                    self._json_response(api_unassigned())
                elif path == "/api/zapmail":
                    self._json_response(api_zapmail())
                elif path == "/api/zapmail/sync":
                    self._json_response(api_zapmail_sync())
                elif path == "/api/domains":
                    self._json_response(api_domains())
                elif path == "/api/pipeline/active":
                    self._json_response(api_pipeline_active())
                elif path.startswith("/api/pipeline/") and len(path.split("/")) == 4:
                    pid = path.split("/")[3]
                    self._json_response(api_pipeline_detail(pid))
                elif path.startswith("/api/inbox/") and path.endswith("/campaigns"):
                    email = path.split("/")[3]
                    self._json_response(api_inbox_campaigns(email))
                elif path == "/api/wallet":
                    self._json_response(api_wallet())
                elif path == "/api/domain-inventory":
                    self._json_response(api_domain_inventory())
                elif path == "/api/placement-tests":
                    self._json_response(api_placement_tests())
                elif path == "/api/subscriptions":
                    self._json_response(api_subscriptions())
                elif path == "/api/acquisition":
                    self._json_response(api_acquisition())
                elif path == "/api/generic-groups":
                    self._json_response(api_generic_groups())
                elif path == "/api/clients/list":
                    self._json_response(api_clients_list())
                elif path == "/api/debug/supabase":
                    self._json_response(api_debug_supabase())
                else:
                    self._error(404, "Not found")
            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                print(f"API ERROR on {path}: {tb}")
                self._json_response({"error": str(e), "traceback": tb}, 500)
        else:
            self._error(404, "Not found")

    def do_POST(self):
        if not self._check_auth():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length)) if content_length else {}

        if path == "/api/assign":
            account_ids = body.get("account_ids", [])
            client_id = body.get("client_id")
            if not account_ids or not client_id:
                self._error(400, "account_ids and client_id required")
                return
            result = assign_accounts_to_client(account_ids, client_id)
            self._json_response(result)
        elif path == "/api/zapmail/cancel":
            domain_ids = body.get("domain_ids", [])
            if not domain_ids:
                self._error(400, "domain_ids required")
                return
            result = zm_delete_domains(domain_ids)
            self._json_response(result)
        elif path == "/api/domains/auto-renew":
            domain = body.get("domain", "")
            registrar = body.get("registrar", "")
            enabled = body.get("enabled", False)
            if not domain or not registrar:
                self._error(400, "domain and registrar required")
                return
            if registrar == "porkbun":
                result = porkbun_set_auto_renew(domain, enabled)
            else:
                result = {"success": False, "message": f"{registrar} auto-renew toggle not supported via API"}
            self._json_response(result)
        elif path == "/api/pipeline/new-client":
            result = api_pipeline_new_client(body)
            self._json_response(result, 400 if "error" in result else 200)
        elif path == "/api/pipeline/replacement":
            result = api_pipeline_replacement(body)
            self._json_response(result, 400 if "error" in result else 200)
        elif path == "/api/client/pause-monitor":
            client_name = body.get("client_name", "")
            paused = body.get("paused", True)
            if not client_name:
                self._error(400, "client_name required")
                return
            try:
                state = store.get_state("paused_clients") or {"clients": []}
                clients_list = state.get("clients", [])
                if paused and client_name not in clients_list:
                    clients_list.append(client_name)
                elif not paused and client_name in clients_list:
                    clients_list.remove(client_name)
                store.set_state("paused_clients", {"clients": clients_list})
                self._json_response({"ok": True, "paused_clients": clients_list})
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
        elif path == "/api/client/set-target-volume":
            client_name = body.get("client_name", "")
            volume = body.get("target_volume", 0)
            if not client_name:
                self._error(400, "client_name required")
                return
            try:
                targets = store.get_state("target_volumes") or {}
                targets[client_name] = int(volume)
                store.set_state("target_volumes", targets)
                self._json_response({"ok": True, "client_name": client_name, "target_volume": int(volume)})
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
        elif path == "/api/pipeline/assign-client":
            pipeline_id = body.get("pipeline_id")
            client_name = body.get("client_name", "").strip()
            forwarding_domain = body.get("forwarding_domain", "").strip()
            is_new_client = body.get("is_new_client", False)
            if not pipeline_id or not client_name or not forwarding_domain:
                self._error(400, "pipeline_id, client_name, and forwarding_domain required")
                return
            # SSE streaming response
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                for chunk in assign_client_sse(pipeline_id, client_name, forwarding_domain, is_new_client):
                    self.wfile.write(chunk.encode())
                    self.wfile.flush()
            except Exception as e:
                error_data = json.dumps({"step": 0, "status": "error", "message": str(e)})
                self.wfile.write(f"data: {error_data}\n\n".encode())
                self.wfile.flush()
        elif path == "/api/inbox/remove-from-campaign":
            result = api_remove_from_campaign(body)
            self._json_response(result)
        elif path == "/api/inbox/remove-from-all-campaigns":
            result = api_remove_from_all_campaigns(body)
            self._json_response(result)
        else:
            self._error(404, "Not found")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _json_response(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _serve_file(self, filename, content_type, set_cookie=None):
        filepath = SCRIPT_DIR / filename
        if filepath.exists():
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            if set_cookie and DASHBOARD_PASSWORD:
                self.send_header("Set-Cookie", f"dashboard_pw={set_cookie}; Path=/; Max-Age=2592000; SameSite=Strict")
            self.end_headers()
            self.wfile.write(filepath.read_bytes())
        else:
            self._error(404, f"{filename} not found")

    def _error(self, status, message):
        self._json_response({"error": message}, status)

    def log_message(self, format, *args):
        pass


def cleanup_stuck_pipelines():
    """Mark any 'running' pipelines as error on startup (they were abandoned by a previous instance)."""
    try:
        rows = store._request("GET", "/pipelines", params={"select": "id,data,status", "status": "eq.running"})
        for row in rows:
            data = json.loads(row["data"])
            data["status"] = "error"
            data["errors"] = data.get("errors", []) + ["Server restarted — pipeline interrupted"]
            store._request("POST", "/pipelines", json_body={
                "id": row["id"],
                "data": json.dumps(data),
                "status": "error",
                "client_name": data.get("client_name", ""),
                "pipeline_type": data.get("type", ""),
                "updated_at": data.get("updated_at", ""),
            }, headers={"Prefer": "resolution=merge-duplicates"})
        if rows:
            print(f"Cleaned up {len(rows)} stuck pipelines from previous instance")
    except Exception as e:
        print(f"Warning: could not clean up stuck pipelines: {e}")


def main():
    port = int(os.environ.get("PORT", sys.argv[1] if len(sys.argv) > 1 else 8099))
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"

    # Clean up pipelines stuck from previous instance
    cleanup_stuck_pipelines()

    # Start background infrastructure monitor (auto-disabled on Render to save memory)
    is_render = bool(os.environ.get("PORT")) and not os.environ.get("ENABLE_MONITOR")
    if not is_render:
        monitor = start_monitor_thread()
        print("Infrastructure monitor started (checking every 4 hours)")
    else:
        print("Infrastructure monitor DISABLED (Render free tier — set ENABLE_MONITOR=1 to override)")

    server = HTTPServer((host, port), DashboardHandler)
    print(f"Dashboard running at http://{host}:{port}")
    print("Press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
