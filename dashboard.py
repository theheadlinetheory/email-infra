#!/usr/bin/env python3
"""THT Infrastructure Dashboard — local web server.

Works both locally (reads .env file) and hosted (reads environment variables).
"""

import gc
import json
import os
import re
import sys
import time
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading
from concurrent.futures import ThreadPoolExecutor

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
from sheets import get_available_domains, get_acquisition_domains, get_all_master_domains, write_range, setup_client_tab, get_service as get_sheets_service, SHEET_ID, MASTER_TAB
from setup import (
    sl_get_all_tags, sl_find_or_create_tag, sl_tag_account,
    zm_list_domain_tags, zm_create_domain_tag, zm_assign_domain_tag,
    zm_set_forwarding, zm_list_domains,
    SMARTLEAD_API, SMARTLEAD_KEY, SMARTLEAD_INTERNAL_API,
    sl_internal_headers as setup_sl_internal_headers,
    calculate_infra, find_existing_config,
    Spaceship, SPACESHIP_API, SPACESHIP_KEY, SPACESHIP_SECRET,
)
import db as store
import pipeline_engine

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
    if r.status_code != 200 or not r.text.strip():
        return []
    try:
        data = r.json()
        return data if isinstance(data, list) else []
    except (ValueError, requests.exceptions.JSONDecodeError):
        return []


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


def spaceship_set_auto_renew(domain, enabled):
    """Toggle auto-renew on a Spaceship domain."""
    r = requests.put(
        f"{SPACESHIP_API}/domains/{domain}/autorenew",
        headers=Spaceship._headers(),
        json={"isEnabled": enabled},
        timeout=15,
    )
    if r.status_code in (200, 204):
        return {"success": True, "message": f"Auto-renew {'enabled' if enabled else 'disabled'}"}
    return {"success": False, "message": r.text[:200]}


# --- SmartLead API helpers ---

_clients_cache = {"data": None, "time": 0}

def get_clients():
    now = time.time()
    if _clients_cache["data"] is not None and now - _clients_cache["time"] < 120:
        return _clients_cache["data"]
    r = requests.get(f"{SMARTLEAD_API}/client?api_key={SMARTLEAD_KEY}", timeout=30)
    result = r.json() if r.status_code == 200 else []
    _clients_cache["data"] = result
    _clients_cache["time"] = now
    return result


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
    if _accounts_cache["data"] is not None and now - _accounts_cache["time"] < 120:
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


def assign_accounts_to_client(account_ids, client_id, old_client_id=None,
                               client_name="", old_client_name=""):
    success = 0
    fail = 0
    history_events = []
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
            history_events.append({
                "account_id": acc_id,
                "email": "",
                "event_type": "client_change",
                "old_value": {"client_id": old_client_id, "client_name": old_client_name},
                "new_value": {"client_id": client_id, "client_name": client_name},
            })
        else:
            fail += 1
        time.sleep(0.15)
    if history_events:
        store.log_inbox_events(history_events)
    return {"success": success, "fail": fail}


_health_cache = {"data": None, "time": 0}

def get_health_metrics(days=7):
    """Get per-inbox health metrics with 120-second cache."""
    now = time.time()
    if _health_cache["data"] is not None and now - _health_cache["time"] < 120:
        return _health_cache["data"]
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        r = requests.get(
            f"{SMARTLEAD_INTERNAL_API}/analytics/mailbox/name-wise-health-metrics",
            headers=sl_internal_headers(),
            params={"start_date": start, "end_date": end, "timezone": "America/New_York", "full_data": "true"},
            timeout=60,
        )
        if r.status_code != 200:
            return _health_cache["data"] or {}
        data = r.json()
        metrics = data.get("data", {}).get("email_health_metrics", [])
        result = {m["from_email"]: m for m in metrics}
        _health_cache["data"] = result
        _health_cache["time"] = now
        return result
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        print(f"[health] Timeout/connection error fetching health metrics: {e}")
        return _health_cache["data"] or {}


_warmup_dates_cache = {"data": None, "time": 0}

def get_warmup_start_dates():
    """Read warmup start dates from client configs in Supabase."""
    now = time.time()
    if _warmup_dates_cache["data"] is not None and now - _warmup_dates_cache["time"] < 120:
        return _warmup_dates_cache["data"]
    dates = {}
    try:
        for c in store.load_all_client_configs():
            name = c.get("client_name", "")
            ws = c.get("infrastructure", {}).get("warmup_start_date", "")
            if name and ws:
                dates[name.lower()] = ws
    except Exception as e:
        print(f"WARN: Could not load client configs: {e}")
    _warmup_dates_cache["data"] = dates
    _warmup_dates_cache["time"] = now
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


# --- Background SmartLead → Supabase sync ---

_SYNC_INTERVAL = 120  # seconds between sync cycles
_sync_running = False  # prevent overlapping syncs


def _compute_overview():
    """Compute the full overview payload from live SmartLead data."""
    gc.collect()
    with ThreadPoolExecutor(max_workers=5) as ex:
        f_clients = ex.submit(get_clients)
        f_accounts = ex.submit(get_all_accounts)
        f_warmup = ex.submit(get_warmup_start_dates)
        f_health = ex.submit(get_health_metrics)
        f_campaigns = ex.submit(get_global_campaign_counts)
    clients = f_clients.result()
    all_accounts = f_accounts.result()
    warmup_dates = f_warmup.result()
    health = f_health.result()
    campaign_counts = f_campaigns.result()

    # Enrich accounts with accurate campaign counts
    for a in all_accounts:
        email = a.get("from_email", "")
        a["campaign_count"] = campaign_counts.get(email, 0)

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
        return ("group" in nl and ("/" in name or "day" in nl)) or nl == "acquisition inboxes"

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

        # Per-account warmup classification
        # An account is "production-ready" if it has completed its 14-day warmup
        # OR is already in a campaign. Only these count toward daily capacity.
        now_dt = datetime.now()
        cl_production = 0
        cl_still_warming = 0
        cl_idle = 0
        for a in cl_accounts:
            wd = a.get("warmup_details") or {}
            warmup_created = wd.get("warmup_created_at", "")
            in_campaign = a.get("campaign_count", 0) > 0

            # Determine if this individual account has completed warmup
            account_warmup_done = False
            if in_campaign:
                account_warmup_done = True
            elif warmup_created:
                try:
                    created = datetime.strptime(warmup_created[:10], "%Y-%m-%d")
                    account_warmup_done = (now_dt - created).days >= 14
                except (ValueError, TypeError):
                    account_warmup_done = False

            if account_warmup_done:
                cl_production += 1
                if not in_campaign:
                    cl_idle += 1
            else:
                cl_still_warming += 1

        cl_campaigns = sum(1 for a in cl_accounts if a.get("campaign_count", 0) > 0)
        cl_smtp_fail = sum(1 for a in cl_accounts if not a.get("is_smtp_success"))
        cl_blocked = sum(
            1 for a in cl_accounts
            if (a.get("warmup_details") or {}).get("status") not in ("ACTIVE", None)
        )

        # Group accounts into batches by warmup start date (within 3 days = same batch)
        _batch_buckets = {}
        for a in cl_accounts:
            wd = a.get("warmup_details") or {}
            wc = wd.get("warmup_created_at", "")
            if not wc:
                continue
            try:
                d = datetime.strptime(wc[:10], "%Y-%m-%d")
                bucket = (d - datetime(2020, 1, 1)).days // 3
                if bucket not in _batch_buckets:
                    _batch_buckets[bucket] = {"date": d, "total": 0, "ready": 0, "warming": 0}
                _batch_buckets[bucket]["total"] += 1
                if (now_dt - d).days >= 14 or a.get("campaign_count", 0) > 0:
                    _batch_buckets[bucket]["ready"] += 1
                else:
                    _batch_buckets[bucket]["warming"] += 1
            except (ValueError, TypeError):
                pass

        batches = []
        for bucket in sorted(_batch_buckets.keys()):
            b = _batch_buckets[bucket]
            days_since = (now_dt - b["date"]).days
            batches.append({
                "warmup_start": b["date"].strftime("%Y-%m-%d"),
                "total": b["total"],
                "ready": b["ready"],
                "warming": b["warming"],
                "days_done": min(14, days_since),
                "status": "ready" if days_since >= 14 else "warming",
            })

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
            "production_ready": cl_production,
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
            "idle_inboxes": cl_idle,
            "batches": batches,
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

    # Global idle inboxes: warmed but not in any campaign
    total_idle = sum(c["idle_inboxes"] for c in client_summaries)
    idle_clients = sum(1 for c in client_summaries if c["idle_inboxes"] > 0)

    # Load paused clients list
    try:
        paused_state = store.get_state("paused_clients") or {"clients": []}
        paused_clients = paused_state.get("clients", [])
    except Exception:
        paused_clients = []

    # Load archived clients list
    try:
        archived_state = store.get_state("archived_clients") or {"clients": []}
        archived_clients = archived_state.get("clients", [])
    except Exception:
        archived_clients = []

    # Load target volumes per client
    try:
        target_volumes = store.get_state("target_volumes") or {}
    except Exception:
        target_volumes = {}

    # Add capacity info to each client summary
    # Only production-ready accounts (warmup complete or in campaign) count toward capacity
    for cs in client_summaries:
        production = cs["production_ready"]
        # Subtract SMTP failures and blocked from production accounts only
        prod_smtp_fail = min(cs["smtp_failures"], production)
        prod_blocked = min(cs["blocked"], production)
        healthy = max(0, production - prod_smtp_fail - prod_blocked)
        capacity = healthy * 15
        target = target_volumes.get(cs["name"], 0)
        cs["healthy_inboxes"] = healthy
        cs["daily_capacity"] = capacity
        cs["warming_capacity"] = cs["warming"] * 15  # future capacity once warmup completes
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
        "archived_clients": archived_clients,
        "idle_inboxes": total_idle,
        "idle_clients": idle_clients,
        "generated_at": datetime.now().isoformat(),
    }


def _compute_client_accounts(client_id):
    """Compute client accounts payload from live SmartLead data."""
    cid = int(client_id)
    with ThreadPoolExecutor(max_workers=5) as ex:
        f_accounts = ex.submit(get_accounts_by_client, cid)
        f_health = ex.submit(get_health_metrics)
        f_campaigns = ex.submit(get_global_campaign_counts)
        f_clients = ex.submit(get_clients)
        f_warmup = ex.submit(get_warmup_start_dates)
    accounts = f_accounts.result()
    health = f_health.result()
    campaign_counts = f_campaigns.result()
    clients = f_clients.result()
    client_name = ""
    for c in clients:
        if c["id"] == cid:
            client_name = c["name"]
            break
    warmup_dates = f_warmup.result()
    ws_date = warmup_dates.get(client_name.lower(), "")
    in_warmup = False
    if ws_date:
        try:
            ready = datetime.strptime(ws_date, "%Y-%m-%d") + timedelta(days=14)
            in_warmup = ready > datetime.now()
        except Exception:
            pass
    warmup_days_elapsed = None
    if ws_date:
        try:
            ws = datetime.strptime(ws_date, "%Y-%m-%d")
            warmup_days_elapsed = (datetime.now() - ws).days
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
            "warmup_days": warmup_days_elapsed,
        })
    by_domain = group_accounts_by_domain(result)
    flagged_domains = [d for d, accs in by_domain.items() if any(a["health_flags"] for a in accs)]
    flagged_inbox_count = sum(len(by_domain[d]) for d in flagged_domains)
    return {
        "client_id": int(client_id),
        "client_name": client_name,
        "accounts": result,
        "flagged_domains": flagged_domains,
        "flagged_inbox_count": flagged_inbox_count,
        "replacement_domains_needed": len(flagged_domains),
        "replacement_inboxes": len(flagged_domains) * 3,
    }


def _sync_smartlead_data():
    """Fetch all SmartLead data and write to Supabase cache."""
    global _sync_running
    if _sync_running:
        return
    _sync_running = True
    try:
        print(f"[sync] Starting SmartLead → Supabase sync at {datetime.now().strftime('%H:%M:%S')}")
        overview = _compute_overview()
        store.cache_set("overview", overview)

        # Pre-cache client accounts for each client
        for cl in overview.get("clients", []):
            try:
                cl_data = _compute_client_accounts(cl["id"])
                store.cache_set(f"client_accounts_{cl['id']}", cl_data)
            except Exception as e:
                print(f"[sync] Error caching client {cl.get('name')}: {e}")

        print(f"[sync] Sync complete at {datetime.now().strftime('%H:%M:%S')} — "
              f"{len(overview.get('clients', []))} clients cached")
    except Exception as e:
        print(f"[sync] Sync error: {e}")
    finally:
        _sync_running = False


def _sync_loop():
    """Background loop that syncs SmartLead data every _SYNC_INTERVAL seconds."""
    while True:
        try:
            _sync_smartlead_data()
        except Exception as e:
            print(f"[sync] Loop error: {e}")
        # Also sync Spaceship → Sheet on each cycle
        try:
            sync_spaceship_to_sheet()
        except Exception as e:
            print(f"[sync] Spaceship sync error: {e}")
        time.sleep(_SYNC_INTERVAL)


def start_sync_thread():
    """Start the background sync as a daemon thread."""
    t = threading.Thread(target=_sync_loop, daemon=True, name="smartlead-sync")
    t.start()
    return t


def invalidate_cache():
    """Force an immediate re-sync (call after mutations like delete/add)."""
    threading.Thread(target=_sync_smartlead_data, daemon=True, name="cache-invalidate").start()


# --- Spaceship → Sheet sync ---

_last_spaceship_sync = 0
_SPACESHIP_MIN_INTERVAL = 60  # Don't sync more than once per minute

def sync_spaceship_to_sheet():
    """Add any Spaceship domains missing from the Google Sheet.

    Runs on every dashboard sync/load. Debounced to once per minute minimum.
    """
    global _last_spaceship_sync
    now = time.time()
    if now - _last_spaceship_sync < _SPACESHIP_MIN_INTERVAL:
        return
    _last_spaceship_sync = now

    try:
        if not Spaceship.is_configured():
            return

        # Get all Spaceship domains
        all_ss = []
        skip = 0
        ss_headers = Spaceship._headers()
        while True:
            r = requests.get(f"{SPACESHIP_API}/domains",
                             headers=ss_headers, params={"take": 100, "skip": skip}, timeout=30)
            items = r.json().get("items", [])
            if not items:
                break
            all_ss.extend(items)
            skip += 100
            if len(items) < 100:
                break

        ss_map = {d["name"]: d for d in all_ss}

        # Get sheet domains
        _, sheet_domains = get_all_master_domains()
        sheet_names = set(d["domain"].lower().strip() for d in sheet_domains)

        # Find missing
        missing = sorted(set(ss_map.keys()) - sheet_names)
        if not missing:
            return

        # Check Zapmail for status
        try:
            zm_domains = zm_list_domains()
            zm_names = set(d.get("domain", "") for d in zm_domains)
        except Exception:
            zm_names = set()

        # Build rows: Domain | Status | Provider | Client | Pool
        rows = []
        for domain in missing:
            in_zapmail = domain in zm_names
            is_branded = "headlinetheory" in domain

            status = "In use" if in_zapmail else "Available"
            pool = "Acquisition" if is_branded else "Client"

            rows.append([domain, status, "Spaceship", "", pool])

        # Append to sheet
        service = get_sheets_service()
        service.spreadsheets().values().append(
            spreadsheetId=SHEET_ID,
            range=f"{MASTER_TAB}!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
        ).execute()

        print(f"[spaceship-sync] Added {len(rows)} domains to sheet "
              f"({sum(1 for r in rows if r[1] == 'Available')} available, "
              f"{sum(1 for r in rows if r[1] == 'In use')} in use)")

    except Exception as e:
        print(f"[spaceship-sync] Error: {e}")


# --- API endpoint logic (cache-first) ---

def api_overview():
    """Return overview from Supabase cache, falling back to live computation."""
    try:
        cached, synced_at = store.cache_get("overview")
        if cached:
            cached["_cached"] = True
            cached["_synced_at"] = synced_at
            return cached
    except Exception:
        pass
    try:
        return _compute_overview()
    except Exception as e:
        print(f"[api_overview] Live computation failed: {e}")
        return {"error": "Data is still loading. The background sync will populate the cache shortly — please refresh in ~30 seconds.",
                "clients": [], "total_accounts": 0, "in_campaign": 0,
                "smtp_failures": 0, "imap_failures": 0, "unassigned": 0,
                "blocked": [], "acquisition_groups": [], "generic_groups": []}


_global_campaign_counts = {"data": {}, "time": 0}
_global_campaign_details = {"data": {}, "time": 0}

def get_global_campaign_counts():
    """Build a global email → campaign count mapping from ALL campaigns.

    SmartLead campaigns often have client_id=null, so filtering by client
    doesn't work. Instead, fetch all campaigns, get their email accounts,
    and build the full mapping. Cached for 120 seconds.
    Also builds email → campaign details mapping for conflict detection.
    """
    now = time.time()
    if _global_campaign_counts["data"] and now - _global_campaign_counts["time"] < 120:
        return _global_campaign_counts["data"]
    counts = {}
    details = {}  # email → list of {id, name, status}
    try:
        r = requests.get(
            f"{SMARTLEAD_API}/campaigns?api_key={SMARTLEAD_KEY}",
            timeout=60,
        )
        campaigns = r.json() if r.status_code == 200 else []
        for camp in campaigns:
            if camp.get("status") not in ("ACTIVE", "PAUSED"):
                continue
            camp_info = {
                "id": camp["id"],
                "name": camp.get("name", ""),
                "status": camp.get("status", ""),
            }
            try:
                cr = requests.get(
                    f"{SMARTLEAD_API}/campaigns/{camp['id']}/email-accounts?api_key={SMARTLEAD_KEY}",
                    timeout=30,
                )
                camp_accounts = cr.json() if cr.status_code == 200 else []
                if isinstance(camp_accounts, list):
                    for ca in camp_accounts:
                        email = ca.get("from_email", "")
                        if email:
                            counts[email] = counts.get(email, 0) + 1
                            details.setdefault(email, []).append(camp_info)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                continue
        _global_campaign_counts["data"] = counts
        _global_campaign_counts["time"] = now
        _global_campaign_details["data"] = details
        _global_campaign_details["time"] = now
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        print(f"[campaigns] Timeout fetching campaign list: {e}")
    return _global_campaign_counts["data"] or counts


def get_global_campaign_details():
    """Return email → campaign details mapping. Calls get_global_campaign_counts to populate cache."""
    now = time.time()
    if not _global_campaign_details["data"] or now - _global_campaign_details["time"] >= 120:
        get_global_campaign_counts()
    return _global_campaign_details["data"]


def api_client_accounts(client_id):
    """Return client accounts from Supabase cache, falling back to live computation."""
    try:
        cached, synced_at = store.cache_get(f"client_accounts_{client_id}")
        if cached:
            cached["_cached"] = True
            cached["_synced_at"] = synced_at
            return cached
    except Exception:
        pass
    try:
        return _compute_client_accounts(client_id)
    except Exception as e:
        print(f"[api_client_accounts] Live computation failed for {client_id}: {e}")
        return {"error": "Data is still loading — please refresh in ~30 seconds.",
                "client_id": int(client_id), "client_name": "", "accounts": [],
                "flagged_domains": [], "flagged_inbox_count": 0,
                "replacement_domains_needed": 0, "replacement_inboxes": 0}


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


def snapshot_all_inboxes():
    """Compare current inbox state against last snapshot and log diffs."""
    accounts = get_all_accounts()
    if not accounts:
        return {"error": "No accounts fetched"}

    # Build current state: {account_id: {client_id, email}}
    current = {}
    for a in accounts:
        current[a["id"]] = {
            "client_id": a.get("client_id"),
            "email": a.get("from_email", ""),
        }

    # Load previous snapshot
    prev = store.get_state("inbox_snapshot") or {}
    prev_map = prev.get("accounts", {})

    # Find diffs
    events = []
    for acc_id_str, cur in current.items():
        acc_id = int(acc_id_str) if isinstance(acc_id_str, str) else acc_id_str
        old = prev_map.get(str(acc_id))
        if old is None:
            # New account
            events.append({
                "account_id": acc_id, "email": cur["email"],
                "event_type": "snapshot_new",
                "old_value": None,
                "new_value": {"client_id": cur["client_id"]},
                "source": "snapshot",
            })
        elif old.get("client_id") != cur["client_id"]:
            events.append({
                "account_id": acc_id, "email": cur["email"],
                "event_type": "client_change",
                "old_value": {"client_id": old.get("client_id")},
                "new_value": {"client_id": cur["client_id"]},
                "source": "snapshot",
            })

    # Check for deleted accounts
    for acc_id_str, old in prev_map.items():
        if int(acc_id_str) not in current:
            events.append({
                "account_id": int(acc_id_str), "email": old.get("email", ""),
                "event_type": "snapshot_deleted",
                "old_value": {"client_id": old.get("client_id")},
                "new_value": None,
                "source": "snapshot",
            })

    if events:
        store.log_inbox_events(events)

    # Save current snapshot
    store.set_state("inbox_snapshot", {
        "accounts": {str(a_id): v for a_id, v in current.items()},
        "taken_at": datetime.now().isoformat(),
        "account_count": len(current),
    })

    return {"diffs": len(events), "accounts": len(current)}


def api_inbox_history(params):
    """Get inbox history. ?account_id=X for per-inbox, or ?limit=N for global feed."""
    account_id = params.get("account_id", [None])[0]
    limit = int(params.get("limit", ["100"])[0])
    if account_id:
        return store.get_inbox_history(int(account_id), limit=limit)
    return store.get_recent_inbox_events(limit=limit)


def _load_sr_groups():
    """Load SR group domain mapping from clients/sr_groups.json."""
    sr_path = SCRIPT_DIR / "clients" / "sr_groups.json"
    if sr_path.exists():
        try:
            return json.loads(sr_path.read_text())
        except Exception:
            pass
    return None


def _warmup_days(account, now_dt):
    """Return number of days since warmup started for an account."""
    wc = (account.get("warmup_details") or {}).get("warmup_created_at", "")
    if not wc:
        return 999
    try:
        d = datetime.strptime(wc[:10], "%Y-%m-%d")
        return (now_dt - d).days
    except (ValueError, TypeError):
        return 999


def _compute_group_stats(group_name, group_id, accounts, health):
    """Compute health/performance stats for a list of accounts."""
    cl_scores = []
    warming = 0
    in_campaign = 0
    smtp_fail = 0
    blocked = 0
    cl_sent = 0
    cl_bounced = 0
    cl_replied = 0
    cl_bounce_rates = []
    cl_reply_rates = []
    flagged_domains = set()
    all_domains = set()

    for acc in accounts:
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
        warmup_status = (acc.get("warmup_details") or {}).get("status")
        if warmup_status is not None and warmup_status != "ACTIVE":
            blocked += 1

    now_dt = datetime.now()

    # Batch warmup computation — group accounts by warmup start date (3-day buckets)
    _batch_buckets = {}
    for a in accounts:
        wd = a.get("warmup_details") or {}
        wc = wd.get("warmup_created_at", "")
        if not wc:
            continue
        try:
            d = datetime.strptime(wc[:10], "%Y-%m-%d")
            bucket = (d - datetime(2020, 1, 1)).days // 3
            if bucket not in _batch_buckets:
                _batch_buckets[bucket] = {"date": d, "total": 0, "ready": 0, "warming": 0}
            _batch_buckets[bucket]["total"] += 1
            if (now_dt - d).days >= 14 or a.get("campaign_count", 0) > 0:
                _batch_buckets[bucket]["ready"] += 1
            else:
                _batch_buckets[bucket]["warming"] += 1
        except (ValueError, TypeError):
            pass

    batches = []
    for bucket in sorted(_batch_buckets.keys()):
        b = _batch_buckets[bucket]
        days_since = (now_dt - b["date"]).days
        batches.append({
            "warmup_start": b["date"].strftime("%Y-%m-%d"),
            "total": b["total"],
            "ready": b["ready"],
            "warming": b["warming"],
            "days_done": min(14, days_since),
            "status": "ready" if days_since >= 14 else "warming",
        })

    # Daily capacity — production-ready accounts minus failures
    production = sum(1 for a in accounts
                     if (a.get("campaign_count", 0) or 0) > 0
                     or _warmup_days(a, now_dt) >= 14)
    healthy = max(0, production - min(smtp_fail, production) - min(blocked, production))
    daily_capacity = healthy * 15

    # Projected capacity — all accounts with working SMTP once warmup finishes
    total_healthy = max(0, len(accounts) - smtp_fail - blocked)
    projected_capacity = total_healthy * 15

    avg_health = round(sum(cl_scores) / len(cl_scores)) if cl_scores else 100
    avg_bounce = round(sum(cl_bounce_rates) / len(cl_bounce_rates), 2) if cl_bounce_rates else 0
    avg_reply = round(sum(cl_reply_rates) / len(cl_reply_rates), 2) if cl_reply_rates else 0
    total_domains = len(all_domains)

    # Warmup completion date — latest batch's estimated finish (warmup_start + 14 days)
    warmup_done_date = None
    still_warming = any(b["status"] == "warming" for b in batches)
    if still_warming:
        latest_warming = max(
            (b for b in batches if b["status"] == "warming"),
            key=lambda b: b["warmup_start"],
        )
        try:
            ws = datetime.strptime(latest_warming["warmup_start"], "%Y-%m-%d")
            warmup_done_date = (ws + timedelta(days=14)).strftime("%m/%d")
        except (ValueError, TypeError):
            pass

    return {
        "id": group_id,
        "name": group_name,
        "accounts": len(accounts),
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
        "blocked": blocked,
        "daily_capacity": daily_capacity,
        "projected_capacity": projected_capacity,
        "still_warming": still_warming,
        "warmup_done_date": warmup_done_date,
        "batches": batches,
    }


def api_untagged_count():
    """Count accounts with no client assignment (proxy for untagged)."""
    all_accounts = get_all_accounts()
    no_client = [a for a in all_accounts if not a.get("client_id")]
    return {
        "untagged_count": len(no_client),
        "accounts": [{"id": a["id"], "email": a.get("from_email", "")} for a in no_client[:20]],
    }


def _enrich_groups_with_campaigns(groups, group_emails):
    """Add campaign assignment data to each group for conflict detection.

    Args:
        groups: list of group stat dicts (mutated in place)
        group_emails: dict mapping group name → list of email addresses
    """
    campaign_details = get_global_campaign_details()
    conflicts = []

    for g in groups:
        emails = group_emails.get(g["name"], [])
        # Collect only acquisition campaigns (skip client fulfillment campaigns)
        seen = {}  # campaign_id → campaign info
        for email in emails:
            for camp in campaign_details.get(email, []):
                # Client campaigns (e.g. "Borja... - DM Matches - client") are legitimate
                # generic group assignments, not acquisition conflicts
                if "acquisition" in camp.get("name", "").lower():
                    seen[camp["id"]] = camp

        active = [c for c in seen.values() if c["status"] == "ACTIVE"]
        paused = [c for c in seen.values() if c["status"] == "PAUSED"]

        g["active_campaigns"] = [{"id": c["id"], "name": c["name"]} for c in active]
        g["paused_campaigns"] = [{"id": c["id"], "name": c["name"]} for c in paused]
        g["campaign_conflict"] = len(active) > 1

        if g["campaign_conflict"]:
            conflicts.append({
                "group": g["name"],
                "campaigns": [c["name"] for c in active],
            })

    return conflicts


def api_acquisition():
    """Acquisition inbox groups with health metrics and campaign assignments."""
    clients = get_clients()
    all_accounts = get_all_accounts()  # uses 30s cache — no extra API calls
    health = get_health_metrics()
    sr_mapping = _load_sr_groups()
    domain_to_group = (sr_mapping or {}).get("domain_to_group", {})

    # Find acquisition clients (e.g. "A Group (250/day)", "Acquisition Inboxes")
    group_clients = [
        c for c in clients
        if ("group" in c.get("name", "").lower() and ("/" in c.get("name", "") or "day" in c.get("name", "").lower()))
        or c.get("name", "").lower() == "acquisition inboxes"
    ]

    groups = []
    group_emails = {}  # group name → list of emails (for campaign cross-ref)
    total_accounts = 0
    for cl in sorted(group_clients, key=lambda x: x.get("name", "")):
        cl_accounts = [a for a in all_accounts if a.get("client_id") == cl["id"]]
        if not cl_accounts:
            continue

        # Split "Acquisition Inboxes" into SR sub-groups by domain
        if cl.get("name", "").lower() == "acquisition inboxes" and domain_to_group:
            sr_buckets = {}
            remainder = []
            for acc in cl_accounts:
                email = acc.get("from_email", "")
                domain = email.split("@")[-1] if "@" in email else ""
                sr_group = domain_to_group.get(domain)
                if sr_group:
                    sr_buckets.setdefault(sr_group, []).append(acc)
                else:
                    remainder.append(acc)

            for sr_name in sorted(sr_buckets):
                sr_accounts = sr_buckets[sr_name]
                total_accounts += len(sr_accounts)
                groups.append(_compute_group_stats(sr_name, cl["id"], sr_accounts, health))
                group_emails[sr_name] = [a.get("from_email", "") for a in sr_accounts]

            # Remaining accounts (e.g. .info domains) stay under original name
            if remainder:
                total_accounts += len(remainder)
                groups.append(_compute_group_stats(cl["name"], cl["id"], remainder, health))
                group_emails[cl["name"]] = [a.get("from_email", "") for a in remainder]
            continue

        total_accounts += len(cl_accounts)
        groups.append(_compute_group_stats(cl["name"], cl["id"], cl_accounts, health))
        group_emails[cl["name"]] = [a.get("from_email", "") for a in cl_accounts]

    # Enrich groups with campaign assignment data
    conflicts = _enrich_groups_with_campaigns(groups, group_emails)

    # Find active acquisition campaigns with no inboxes assigned
    empty_campaigns = _find_empty_acquisition_campaigns()

    # Find unassigned acquisition inboxes (headlinetheory domains not in any acq group)
    acq_client_ids = {cl["id"] for cl in group_clients}
    unassigned_acq = []
    for a in all_accounts:
        email = a.get("from_email", "")
        domain = email.split("@")[-1] if "@" in email else ""
        if "headlinetheory" not in domain:
            continue
        name_lower = (a.get("from_name") or email.split("@")[0]).lower()
        if "aidan" not in name_lower and "lars" not in name_lower:
            continue
        if a.get("client_id") and a["client_id"] in acq_client_ids:
            continue
        wd = a.get("warmup_details") or {}
        unassigned_acq.append({
            "id": a["id"],
            "email": email,
            "domain": domain,
            "from_name": a.get("from_name", ""),
            "client_id": a.get("client_id"),
            "warmup_status": wd.get("status", "UNKNOWN"),
            "warmup_reputation": wd.get("warmup_reputation", "?"),
            "smtp_ok": a.get("is_smtp_success", False),
        })

    return {
        "groups": groups,
        "total_accounts": total_accounts,
        "total_groups": len(groups),
        "campaign_conflicts": conflicts,
        "empty_campaigns": empty_campaigns,
        "unassigned_acq": unassigned_acq,
        "generated_at": datetime.now().isoformat(),
    }


def _find_empty_acquisition_campaigns():
    """Find ACTIVE acquisition campaigns that have zero email accounts assigned."""
    campaign_details = get_global_campaign_details()

    # Invert: campaign_id → set of emails
    campaign_accounts = {}
    for email, camps in campaign_details.items():
        for c in camps:
            campaign_accounts.setdefault(c["id"], set()).add(email)

    # Get all campaigns to find acquisition ones
    try:
        r = requests.get(
            f"{SMARTLEAD_API}/campaigns?api_key={SMARTLEAD_KEY}",
            timeout=60,
        )
        campaigns = r.json() if r.status_code == 200 else []
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
        return []

    empty = []
    for c in campaigns:
        if c.get("status") != "ACTIVE":
            continue
        if "acquisition" not in c.get("name", "").lower():
            continue
        account_count = len(campaign_accounts.get(c["id"], set()))
        if account_count == 0:
            empty.append({"id": c["id"], "name": c.get("name", "")})
    return empty


def api_acquisition_campaigns():
    """Return campaigns with 'acquisition' in the name, for dropdown assignment."""
    try:
        r = requests.get(
            f"{SMARTLEAD_API}/campaigns?api_key={SMARTLEAD_KEY}",
            timeout=60,
        )
        campaigns = r.json() if r.status_code == 200 else []
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
        return {"campaigns": [], "error": "timeout"}

    acq_campaigns = []
    for c in campaigns:
        name = c.get("name", "")
        if "acquisition" not in name.lower():
            continue
        acq_campaigns.append({
            "id": c["id"],
            "name": name,
            "status": c.get("status", ""),
        })

    # Sort: ACTIVE first, then PAUSED, then others, alphabetically within each
    status_order = {"ACTIVE": 0, "PAUSED": 1}
    acq_campaigns.sort(key=lambda c: (status_order.get(c["status"], 9), c["name"]))
    return {"campaigns": acq_campaigns}


def api_assign_group_campaign(body):
    """Assign a group's accounts to a campaign (or unassign from one).

    Body: {group_client_id, group_name, campaign_id, action: "assign"|"unassign"}
    """
    group_client_id = body.get("group_client_id")
    group_name = body.get("group_name", "")
    campaign_id = body.get("campaign_id")
    action = body.get("action", "assign")

    if not group_client_id or not campaign_id:
        return {"error": "group_client_id and campaign_id required"}

    # Get all accounts for this group
    all_accounts = get_all_accounts()
    group_accounts = [a for a in all_accounts if a.get("client_id") == group_client_id]

    # For SR sub-groups within "Acquisition Inboxes", filter by domain mapping
    sr_mapping = _load_sr_groups()
    domain_to_group = (sr_mapping or {}).get("domain_to_group", {})
    if domain_to_group and group_name:
        filtered = []
        for acc in group_accounts:
            email = acc.get("from_email", "")
            domain = email.split("@")[-1] if "@" in email else ""
            sr_group = domain_to_group.get(domain, "")
            if sr_group == group_name or (not sr_group and group_name.lower() == "acquisition inboxes"):
                filtered.append(acc)
        if filtered:
            group_accounts = filtered

    if not group_accounts:
        return {"error": f"No accounts found for group {group_name or group_client_id}"}

    account_ids = [a["id"] for a in group_accounts]

    if action == "assign":
        # Conflict check: ensure no account is already in another ACTIVE acquisition campaign
        # (client fulfillment campaigns are legitimate generic group assignments, not conflicts)
        campaign_details = get_global_campaign_details()
        conflicts = set()
        for a in group_accounts:
            email = a.get("from_email", "")
            for camp in campaign_details.get(email, []):
                if (camp["status"] == "ACTIVE" and camp["id"] != campaign_id
                        and "acquisition" in camp.get("name", "").lower()):
                    conflicts.add(camp["name"])
        if conflicts:
            return {
                "error": f"Cannot assign — group is already in active campaign(s): {', '.join(conflicts)}",
                "conflicts": list(conflicts),
            }

        # Add accounts to campaign
        r = requests.post(
            f"{SMARTLEAD_API}/campaigns/{campaign_id}/email-accounts?api_key={SMARTLEAD_KEY}",
            json={"email_account_ids": account_ids},
            timeout=60,
        )
        if r.status_code == 200:
            _global_campaign_counts["time"] = 0
            _global_campaign_details["time"] = 0
            store.log_inbox_events([{
                "account_id": a["id"], "email": a.get("from_email", ""),
                "event_type": "campaign_assign",
                "old_value": None,
                "new_value": {"campaign_id": campaign_id, "group": group_name},
            } for a in group_accounts])
            return {"ok": True, "action": "assigned", "accounts": len(account_ids)}
        return {"error": f"SmartLead error: {r.status_code} {r.text[:200]}"}

    elif action == "unassign":
        # Remove accounts from campaign
        r = requests.delete(
            f"{SMARTLEAD_API}/campaigns/{campaign_id}/email-accounts?api_key={SMARTLEAD_KEY}",
            json={"email_account_ids": account_ids},
            timeout=60,
        )
        if r.status_code == 200:
            _global_campaign_counts["time"] = 0
            _global_campaign_details["time"] = 0
            store.log_inbox_events([{
                "account_id": a["id"], "email": a.get("from_email", ""),
                "event_type": "campaign_unassign",
                "old_value": {"campaign_id": campaign_id, "group": group_name},
                "new_value": None,
            } for a in group_accounts])
            return {"ok": True, "action": "unassigned", "accounts": len(account_ids)}
        return {"error": f"SmartLead error: {r.status_code} {r.text[:200]}"}

    return {"error": f"Unknown action: {action}"}


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
    """1-for-1 replacement disabled — transitioning to A/B group rotation model."""
    return {"error": "1-for-1 replacement is disabled. Replacements now go through A/B group rotation."}


def api_pipeline_new_acquisition(body):
    """Start a new acquisition group pipeline."""
    group_name = body.get("group_name", "").strip()
    daily_volume = body.get("daily_volume", 250)
    sender = body.get("sender", "aidan_hutchinson")

    if not group_name:
        return {"error": "group_name required"}
    if not daily_volume or daily_volume < 1:
        return {"error": "daily_volume must be a positive number"}

    # Check for existing config with this name
    existing_cfg, existing_path = find_existing_config(group_name)
    if existing_cfg:
        if existing_cfg.get("status") == "complete":
            return {"error": f"'{group_name}' already has a completed pipeline. Rename or delete the existing config first."}
        return {"error": f"'{group_name}' already has an in-progress pipeline."}

    infra = calculate_infra(int(daily_volume))

    available = get_available_domains()
    if not available:
        return {"error": "No domains available in inventory"}

    available.sort(key=lambda d: d.get("purchase_date", "9999"))
    chosen = available[:infra["domains_needed"]]

    if len(chosen) < infra["domains_needed"]:
        return {"error": f"Only {len(chosen)} domains available, need {infra['domains_needed']}"}

    pipeline = create_pipeline("acquisition", group_name, chosen, "https://theheadlinetheory.com/")
    pipeline["mode"] = "acquisition"
    pipeline["sender"] = sender
    save_pipeline(pipeline)
    threading.Thread(target=run_pipeline_steps, args=(pipeline,), daemon=True).start()

    return {
        "pipeline_id": pipeline["id"],
        "status": "started",
        "group_name": group_name,
        "domains": list(pipeline["domains"].keys()),
        "infra": infra,
    }


def api_pipeline_active():
    """List all pipelines with per-domain status."""
    try:
        all_p = load_all_pipelines()
    except Exception as e:
        print(f"WARN: Could not load pipelines: {e}")
        all_p = []
    result = []
    for p in all_p:
        # Build per-domain summary
        domain_details = {}
        for domain, info in p.get("domains", {}).items():
            domain_details[domain] = {
                "step": info.get("step", ""),
                "step_status": info.get("step_status", ""),
                "error": info.get("error"),
                "attempt": info.get("attempt", 1),
                "max_attempts": info.get("max_attempts", 3),
                "step_history": info.get("step_history", []),
            }

        result.append({
            "id": p["id"],
            "type": p["type"],
            "client_name": p["client_name"],
            "status": p["status"],
            "current_step": p.get("current_step", ""),
            "steps": p.get("steps", []),
            "domains": list(p["domains"].keys()),
            "domain_details": domain_details,
            "created_at": p.get("created_at", ""),
            "updated_at": p.get("updated_at", ""),
            "errors": p.get("errors", []),
            "pending_removals": p.get("pending_removals", {}),
            "retry_info": p.get("retry_info"),
        })
    result.sort(key=lambda p: p["created_at"], reverse=True)
    return {"pipelines": result}


def api_pipeline_detail(pipeline_id):
    """Get detailed status for a specific pipeline."""
    p = load_pipeline(pipeline_id)
    if not p:
        return {"error": "Pipeline not found"}
    return p


def api_pipeline_retry(body):
    """Retry failed domains on the current step of an errored pipeline."""
    pipeline_id = body.get("pipeline_id", "")
    retry_domains = body.get("domains", [])  # empty = retry all failed

    if not pipeline_id:
        return {"error": "pipeline_id required"}

    p = load_pipeline(pipeline_id)
    if not p:
        return {"error": "Pipeline not found"}
    if p["status"] != "error":
        return {"error": f"Pipeline is '{p['status']}', not 'error' — cannot retry"}

    # Find domains to retry
    retrying = []
    for domain, info in p["domains"].items():
        if info.get("step_status") == "error":
            if retry_domains and domain not in retry_domains:
                continue
            info["step_status"] = "pending"
            info["error"] = None
            info["attempt"] = 1
            retrying.append(domain)

    if not retrying:
        return {"error": "No failed domains to retry"}

    p["status"] = "running"
    p["errors"] = []  # clear stale error list
    p["updated_at"] = datetime.now().isoformat()
    save_pipeline(p)

    threading.Thread(target=run_pipeline_steps, args=(p,), daemon=True).start()

    return {"pipeline_id": p["id"], "status": "running", "retrying_domains": retrying}


def api_pipeline_skip_step(body):
    """Skip the current step and advance to the next one."""
    pipeline_id = body.get("pipeline_id", "")

    if not pipeline_id:
        return {"error": "pipeline_id required"}

    p = load_pipeline(pipeline_id)
    if not p:
        return {"error": "Pipeline not found"}
    if p["status"] != "error":
        return {"error": f"Pipeline is '{p['status']}', not 'error' — cannot skip"}

    current_step = p["current_step"]
    steps = p["steps"]
    current_idx = steps.index(current_step)

    if current_idx >= len(steps) - 1:
        return {"error": "Already on the last step — cannot skip"}

    # Mark all domains as complete for skipped step, log the skip
    for domain, info in p["domains"].items():
        info.setdefault("step_history", []).append({
            "attempt": info.get("attempt", 0),
            "result": "skipped",
            "message": f"Step '{current_step}' manually skipped",
            "at": datetime.now().isoformat(),
        })
        info["step"] = current_step
        info["step_status"] = "complete"
        info["error"] = None

    next_step = steps[current_idx + 1]
    p["current_step"] = next_step
    p["status"] = "running"
    p["errors"] = []
    p["updated_at"] = datetime.now().isoformat()
    save_pipeline(p)

    threading.Thread(target=run_pipeline_steps, args=(p,), daemon=True).start()

    return {
        "pipeline_id": p["id"],
        "skipped_step": current_step,
        "next_step": next_step,
        "warning": "Step skipped — some domains may have incomplete setup",
    }


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


DOMAIN_INVENTORY_THRESHOLD = 20

_inventory_alert_times = {}  # {"Client": datetime, "Acquisition": datetime}


def _send_inventory_alert(pool_name, count):
    """Send Slack alert for low domain inventory, max once per pool per 24h."""
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "")
    if not webhook_url:
        return

    now = datetime.now(timezone.utc)
    last_sent = _inventory_alert_times.get(pool_name)
    if last_sent and (now - last_sent) < timedelta(hours=24):
        return  # Already alerted recently

    try:
        resp = requests.post(
            webhook_url,
            json={"text": f"\u26a0\ufe0f Domain inventory low: {pool_name} pool has {count} available (threshold: {DOMAIN_INVENTORY_THRESHOLD})"},
            timeout=10,
        )
        resp.raise_for_status()
        _inventory_alert_times[pool_name] = now
    except Exception as e:
        print(f"[SLACK] Failed to send inventory alert: {e}")


def api_domain_inventory():
    """Get available domain inventory split by client and acquisition pools."""
    client_domains = get_available_domains()
    acquisition_domains = get_acquisition_domains()
    client_count = len(client_domains)
    acq_count = len(acquisition_domains)
    client_low = client_count < DOMAIN_INVENTORY_THRESHOLD
    acq_low = acq_count < DOMAIN_INVENTORY_THRESHOLD

    # Send Slack alerts for low inventory (debounced)
    if client_low:
        _send_inventory_alert("Client", client_count)
    if acq_low:
        _send_inventory_alert("Acquisition", acq_count)

    return {
        "client_available": client_count,
        "acquisition_available": acq_count,
        "client_threshold": DOMAIN_INVENTORY_THRESHOLD,
        "acquisition_threshold": DOMAIN_INVENTORY_THRESHOLD,
        "client_low": client_low,
        "acquisition_low": acq_low,
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


# --- Reply Rate Trends ---

_campaigns_cache = {"data": None, "time": 0}

def _get_all_campaigns():
    """Fetch all campaigns with 120s cache."""
    now = time.time()
    if _campaigns_cache["data"] is not None and now - _campaigns_cache["time"] < 120:
        return _campaigns_cache["data"]
    try:
        r = requests.get(f"{SMARTLEAD_API}/campaigns?api_key={SMARTLEAD_KEY}", timeout=30)
        campaigns = r.json() if r.status_code == 200 else []
        if not isinstance(campaigns, list):
            campaigns = []
    except Exception as e:
        print(f"TRENDS: campaigns fetch exception: {e}")
        campaigns = []
    _campaigns_cache["data"] = campaigns
    _campaigns_cache["time"] = now
    return campaigns

def debug_client_trends(client_id):
    """Debug endpoint: show raw API responses for trends troubleshooting."""
    clients = get_clients()
    client_name = ""
    for c in clients:
        if c["id"] == int(client_id):
            client_name = c["name"]
            break

    # Raw campaign list via public API
    try:
        camp_resp = requests.get(
            f"{SMARTLEAD_API}/campaigns?api_key={SMARTLEAD_KEY}",
            timeout=30,
        )
        camp_status = camp_resp.status_code
        camp_raw = camp_resp.text[:500]
    except Exception as e:
        camp_status = -1
        camp_raw = str(e)

    # Match campaigns
    client_lower = client_name.lower().strip()
    core_name = re.sub(r'\b(inc\.?|llc\.?|co\.?|ltd\.?|corp\.?|services?)\b', '', client_lower).strip().rstrip('.')
    core_name = re.sub(r'\s+', ' ', core_name).strip()
    first_word = core_name.split()[0] if core_name.split() else ""
    match_candidates = [client_lower, core_name]
    if len(first_word) >= 4 and first_word != core_name:
        match_candidates.append(first_word)

    matched = []
    if camp_status == 200:
        try:
            all_campaigns = json.loads(camp_resp.text)
            if not isinstance(all_campaigns, list):
                all_campaigns = []
            for camp in all_campaigns:
                camp_name = (camp.get("name") or "").lower()
                if "acquisition" not in camp_name and any(c in camp_name for c in match_candidates):
                    matched.append({"id": camp["id"], "name": camp.get("name")})
        except Exception as e:
            matched = [{"error": str(e)}]

    # Raw day-wise stats
    matched_ids = [str(m["id"]) for m in matched if "id" in m]
    start_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    end_date = datetime.now().strftime("%Y-%m-%d")
    try:
        stats_resp = requests.get(
            f"{SMARTLEAD_INTERNAL_API}/analytics/day-wise-overall-stats",
            headers=sl_internal_headers(),
            params={
                "campaign_ids": ",".join(matched_ids[:3]),
                "start_date": start_date,
                "end_date": end_date,
                "timezone": "America/New_York",
            },
            timeout=30,
        )
        stats_status = stats_resp.status_code
        stats_raw = stats_resp.text[:1000]
    except Exception as e:
        stats_status = -1
        stats_raw = str(e)

    return {
        "client_id": client_id,
        "client_name": client_name,
        "match_candidates": match_candidates,
        "campaign_list_status": camp_status,
        "campaign_list_raw": camp_raw,
        "matched_campaigns": matched[:10],
        "stats_params": {
            "campaign_ids": ",".join(matched_ids[:3]),
            "start_date": start_date,
            "end_date": end_date,
        },
        "stats_status": stats_status,
        "stats_raw": stats_raw,
    }


def api_client_trends(client_id, days):
    """Get day-wise reply rate trend for a client's infrastructure across all campaigns."""
    # Find client name
    clients = get_clients()
    client_name = ""
    for c in clients:
        if c["id"] == int(client_id):
            client_name = c["name"]
            break
    if not client_name:
        return {"error": "Client not found", "data": [], "summary": {}}

    # Get all campaigns via public API (cached)
    all_campaigns = _get_all_campaigns()

    # Fuzzy match: campaign name contains client name (or shortened variants), exclude acquisition
    # Build match candidates: full name, name without suffixes, and progressively shorter prefixes
    client_lower = client_name.lower().strip()
    core_name = re.sub(r'\b(inc\.?|llc\.?|co\.?|ltd\.?|corp\.?|services?)\b', '', client_lower).strip().rstrip('.')
    core_name = re.sub(r'\s+', ' ', core_name).strip()
    # Also try just the first word(s) — e.g. "Timesavers" from "Timesavers Landscaping Inc."
    # Use first word only if it's 4+ chars to avoid overly broad matches
    first_word = core_name.split()[0] if core_name.split() else ""
    match_candidates = [client_lower, core_name]
    if len(first_word) >= 4 and first_word != core_name:
        match_candidates.append(first_word)

    matched_ids = []
    earliest_date = None
    for camp in all_campaigns:
        camp_name = (camp.get("name") or "").lower()
        if "acquisition" in camp_name:
            continue
        # Match if any candidate appears in campaign name
        if any(candidate in camp_name for candidate in match_candidates):
            matched_ids.append(str(camp["id"]))
            created = camp.get("created_at", "")[:10]
            if created and (earliest_date is None or created < earliest_date):
                earliest_date = created

    if not matched_ids:
        return {"client_name": client_name, "days": days, "data": [], "summary": {
            "total_sent": 0, "total_replied": 0, "avg_reply_rate": 0,
            "recent_7d_rate": 0, "prior_7d_rate": 0, "trend": "flat",
        }}

    # Determine date range
    end_date = datetime.now().strftime("%Y-%m-%d")
    if days == 0:
        start_date = earliest_date or (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    else:
        start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    # Fetch day-wise stats from SmartLead
    try:
        stats_resp = requests.get(
            f"{SMARTLEAD_INTERNAL_API}/analytics/day-wise-overall-stats",
            headers=sl_internal_headers(),
            params={
                "campaign_ids": ",".join(matched_ids),
                "start_date": start_date,
                "end_date": end_date,
                "timezone": "America/New_York",
            },
            timeout=30,
        )
        stats_data = stats_resp.json() if stats_resp.status_code == 200 else {}
        if stats_resp.status_code != 200:
            print(f"TRENDS: day-wise-overall-stats returned {stats_resp.status_code}: {stats_resp.text[:200]}")
    except Exception as e:
        print(f"TRENDS: day-wise-overall-stats exception: {e}")
        stats_data = {}

    day_stats = []
    if isinstance(stats_data, dict):
        day_stats = stats_data.get("data", {}).get("day_wise_stats", [])
    print(f"TRENDS: client={client_name}, campaigns={len(matched_ids)}, days_returned={len(day_stats)}")

    # Build raw daily data
    raw_days = []
    total_sent = 0
    total_replied = 0
    total_bounced = 0
    for day in day_stats:
        metrics = day.get("email_engagement_metrics", {})
        sent = metrics.get("sent", 0)
        replied = metrics.get("replied", 0)
        bounced = metrics.get("bounced", 0)
        raw_days.append({
            "date": day.get("date", ""),
            "sent": sent,
            "replied": replied,
            "bounced": bounced,
        })
        total_sent += sent
        total_replied += replied
        total_bounced += bounced

    # Compute rolling 7-day rates (sum of last 7 sending days / sum of sends)
    # This prevents spikes from low-send days getting inflated by delayed replies
    data_points = []
    for i, day in enumerate(raw_days):
        if day["sent"] == 0:
            data_points.append({**day, "reply_rate": None, "bounce_rate": None})
            continue
        # Look back up to 7 sending days (including current)
        window_sent = 0
        window_replied = 0
        window_bounced = 0
        sending_count = 0
        for j in range(i, -1, -1):
            if raw_days[j]["sent"] > 0:
                window_sent += raw_days[j]["sent"]
                window_replied += raw_days[j]["replied"]
                window_bounced += raw_days[j]["bounced"]
                sending_count += 1
                if sending_count >= 7:
                    break
        reply_rate = round(window_replied / window_sent * 100, 2) if window_sent > 0 else None
        bounce_rate = round(window_bounced / window_sent * 100, 2) if window_sent > 0 else None
        data_points.append({**day, "reply_rate": reply_rate, "bounce_rate": bounce_rate})

    # Compute summary
    avg_reply_rate = round(total_replied / total_sent * 100, 2) if total_sent > 0 else 0
    avg_bounce_rate = round(total_bounced / total_sent * 100, 2) if total_sent > 0 else 0

    # Trend: last 7 sending days vs prior 7 sending days
    sending_days = [d for d in data_points if d["sent"] > 0]
    recent_7 = sending_days[-7:] if len(sending_days) >= 7 else sending_days
    prior_7 = sending_days[-14:-7] if len(sending_days) >= 14 else []

    recent_sent = sum(d["sent"] for d in recent_7)
    recent_replied = sum(d["replied"] for d in recent_7)
    recent_rate = round(recent_replied / recent_sent * 100, 2) if recent_sent > 0 else 0

    prior_sent = sum(d["sent"] for d in prior_7)
    prior_replied = sum(d["replied"] for d in prior_7)
    prior_rate = round(prior_replied / prior_sent * 100, 2) if prior_sent > 0 else 0

    if prior_rate == 0:
        trend = "flat"
    elif recent_rate >= prior_rate:
        trend = "up"
    else:
        trend = "down"

    return {
        "client_name": client_name,
        "days": days,
        "campaigns_matched": len(matched_ids),
        "data": data_points,
        "summary": {
            "total_sent": total_sent,
            "total_replied": total_replied,
            "total_bounced": total_bounced,
            "avg_reply_rate": avg_reply_rate,
            "avg_bounce_rate": avg_bounce_rate,
            "recent_7d_rate": recent_rate,
            "prior_7d_rate": prior_rate,
            "trend": trend,
        },
    }


# --- Delete Client Infrastructure ---

def delete_client_infra_sse(client_id, client_name):
    """Generator that yields SSE events for each step of infrastructure deletion."""
    import traceback

    def event(step, status, message=""):
        data = json.dumps({"step": step, "status": status, "message": message})
        return f"data: {data}\n\n"

    # Get all accounts for this client
    accounts = get_accounts_by_client(int(client_id))
    if not accounts:
        yield event(0, "error", "No accounts found for this client")
        return

    domains = set()
    account_ids = []
    for acc in accounts:
        account_ids.append(acc["id"])
        email = acc.get("from_email", "")
        domain = email.split("@")[-1] if "@" in email else ""
        if domain:
            domains.add(domain)

    # ── Step 1: Remove from campaigns ──
    yield event(1, "running")
    try:
        campaign_removals = 0
        for acc in accounts:
            email = acc.get("from_email", "")
            if (acc.get("campaign_count", 0) or 0) > 0:
                campaigns_data = api_inbox_campaigns(email)
                for camp in campaigns_data.get("campaigns", []):
                    api_remove_from_campaign({"email": email, "campaign_id": camp["campaign_id"]})
                    campaign_removals += 1
        yield event(1, "done", f"Removed from {campaign_removals} campaign(s)")
    except Exception as e:
        yield event(1, "error", str(e))
        return

    # ── Step 2: Delete SmartLead accounts ──
    yield event(2, "running")
    try:
        # Log deletion history before deleting
        store.log_inbox_events([{
            "account_id": acc["id"], "email": acc.get("from_email", ""),
            "event_type": "delete",
            "old_value": {"client_id": client_id, "client_name": client_name},
            "new_value": None,
        } for acc in accounts])
        # Bulk delete via API
        for acc_id in account_ids:
            r = requests.delete(
                f"{SMARTLEAD_API}/email-accounts/{acc_id}?api_key={SMARTLEAD_KEY}",
                timeout=30,
            )
            if r.status_code not in (200, 204):
                yield event(2, "error", f"Failed to delete account {acc_id}: {r.status_code}")
                return
        yield event(2, "done", f"Deleted {len(account_ids)} account(s)")
    except Exception as e:
        yield event(2, "error", str(e))
        return

    # ── Step 3: Cancel Zapmail domains ──
    yield event(3, "running")
    try:
        zm_domains = zm_list_domains()
        zm_domain_ids = [d["id"] for d in zm_domains if d.get("domain") in domains]
        if zm_domain_ids:
            # Delete one at a time as fallback if bulk fails
            result = zm_delete_domains(zm_domain_ids)
            if isinstance(result, dict) and result.get("error"):
                # Try one-by-one
                cancelled = 0
                for did in zm_domain_ids:
                    r2 = zm_delete_domains([did])
                    if not (isinstance(r2, dict) and r2.get("error")):
                        cancelled += 1
                yield event(3, "done", f"Cancelled {cancelled}/{len(zm_domain_ids)} domain(s)")
            else:
                yield event(3, "done", f"Cancelled {len(zm_domain_ids)} domain(s)")
        else:
            yield event(3, "done", "No Zapmail domains found (already removed or not connected)")
    except Exception as e:
        # Don't block deletion on Zapmail errors — continue with remaining steps
        yield event(3, "done", f"Zapmail step skipped ({e})")

    # ── Step 4: Update Google Sheet ──
    yield event(4, "running")
    try:
        _, all_sheet_domains = get_all_master_domains()
        updated = 0
        for sd in all_sheet_domains:
            if sd["domain"] in domains:
                write_range("THT Domains ", f"B{sd['row_number']}", [["Cancelled"]])
                write_range("THT Domains ", f"D{sd['row_number']}", [[f"{client_name} (deleted)"]])
                updated += 1
        yield event(4, "done", f"Updated {updated} domain(s)")
    except Exception as e:
        yield event(4, "error", str(e))
        return

    # ── Step 5: Delete SmartLead client ──
    yield event(5, "running")
    try:
        r = requests.post(
            f"{SMARTLEAD_INTERNAL_API}/client/delete",
            headers=sl_internal_headers(),
            json={"id": int(client_id)},
            timeout=30,
        )
        if r.status_code == 200:
            yield event(5, "done")
        else:
            yield event(5, "done", "Client kept (may have other accounts)")
    except Exception as e:
        yield event(5, "error", str(e))
        return

    invalidate_cache()
    yield event(0, "complete")


def transition_client_sse(client_id, client_name, new_client_name, forwarding_domain, is_new_client):
    """Generator that yields SSE events for transitioning infrastructure from one client to another."""
    import traceback

    def event(step, status, message=""):
        data = json.dumps({"step": step, "status": status, "message": message})
        return f"data: {data}\n\n"

    # Get all accounts for the source client
    accounts = get_accounts_by_client(int(client_id))
    if not accounts:
        yield event(0, "error", "No accounts found for this client")
        return

    domains = set()
    for acc in accounts:
        email = acc.get("from_email", "")
        domain = email.split("@")[-1] if "@" in email else ""
        if domain:
            domains.add(domain)

    # ── Step 1: SmartLead client (find or create new) ──
    yield event(1, "running")
    sl_client_id = None
    try:
        sl_clients = requests.get(
            f"{SMARTLEAD_API}/client?api_key={SMARTLEAD_KEY}", timeout=30
        ).json()
        client_lower = new_client_name.lower().strip()
        for c in sl_clients:
            if c["name"].lower().strip() == client_lower:
                sl_client_id = c["id"]
                break
        if not sl_client_id and not is_new_client:
            for c in sl_clients:
                cn = c["name"].lower().strip()
                if client_lower in cn or cn in client_lower:
                    sl_client_id = c["id"]
                    break
        if not sl_client_id:
            slug = new_client_name.lower().replace("'", "").replace(" ", "").replace("&", "")
            cl_email = f"tht.{slug}.client@gmail.com"
            cr = requests.post(
                f"{SMARTLEAD_API}/client/save?api_key={SMARTLEAD_KEY}",
                json={"name": new_client_name, "email": cl_email, "password": "THTclient2026!"},
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

    # ── Step 2: SmartLead tags (replace old client tag with new) ──
    yield event(2, "running")
    try:
        all_tags = sl_get_all_tags()
        new_tag_id = sl_find_or_create_tag(new_client_name, existing_tags=all_tags)
        old_tag_id = None
        for tag_name, tag_data in all_tags.items():
            if tag_name.lower().strip() == client_name.lower().strip():
                old_tag_id = tag_data["id"]
                break

        for acc in accounts:
            acc_id = acc["id"]
            current_tags = [t["id"] for t in acc.get("tags", [])] if acc.get("tags") else []
            new_tags = [t for t in current_tags if t != old_tag_id]
            if new_tag_id and new_tag_id not in new_tags:
                new_tags.append(new_tag_id)
            sl_tag_account(acc_id, new_tags, client_id=sl_client_id)

        yield event(2, "done")
    except Exception as e:
        yield event(2, "error", f"{e}\n{traceback.format_exc()}")
        return

    # ── Step 3: Verify client assignment ──
    yield event(3, "running")
    try:
        if accounts:
            sample = requests.get(
                f"{SMARTLEAD_API}/email-accounts/{accounts[0]['id']}/?api_key={SMARTLEAD_KEY}",
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
        zm_domains = zm_list_domains()
        zm_domain_ids = [d["id"] for d in zm_domains if d.get("domain") in domains]
        if zm_domain_ids:
            existing_tags = zm_list_domain_tags()
            tag_list = existing_tags.get("data", []) if isinstance(existing_tags, dict) else []
            client_zm_tag_id = None
            for t in tag_list:
                if t.get("name", "").lower().strip() == new_client_name.lower().strip():
                    client_zm_tag_id = t["id"]
                    break
            if not client_zm_tag_id:
                result = zm_create_domain_tag(new_client_name)
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
        zm_domains = zm_list_domains()
        zm_domain_ids = [d["id"] for d in zm_domains if d.get("domain") in domains]
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
            if sd["domain"] in domains:
                write_range("THT Domains ", f"D{sd['row_number']}", [[new_client_name]])
        setup_client_tab(new_client_name, list(domains))
        yield event(6, "done")
    except Exception as e:
        yield event(6, "error", str(e))
        return

    invalidate_cache()
    yield event(0, "complete")


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
    # Required format: [Zapmail, ClientName, WarmupStartDate] — always all 3
    yield event(2, "running")
    try:
        all_tags = sl_get_all_tags()

        # 1. Find/create client tag
        client_tag_id = sl_find_or_create_tag(client_name, existing_tags=all_tags)

        # 2. Find Zapmail tag (ID 262254)
        zapmail_tag_id = None
        for tag_name, tag_data in all_tags.items():
            if tag_name.lower().strip() == "zapmail":
                zapmail_tag_id = tag_data["id"]
                break

        # 3. Find warmup start date tag from pipeline config
        warmup_date = pipeline.get("infrastructure", {}).get("warmup_start_date", "")
        date_tag_id = None
        if warmup_date:
            # Convert "2026-03-28" to "3/28/26" format for tag lookup
            from datetime import datetime as dt
            try:
                d = dt.strptime(warmup_date, "%Y-%m-%d")
                date_tag_name = f"{d.month}/{d.day}/{d.strftime('%y')}"
            except ValueError:
                date_tag_name = warmup_date
            date_tag_id = sl_find_or_create_tag(date_tag_name, existing_tags=all_tags)

        # Build the required 3-tag list
        required_tags = []
        if zapmail_tag_id:
            required_tags.append(zapmail_tag_id)
        if client_tag_id:
            required_tags.append(client_tag_id)
        if date_tag_id:
            required_tags.append(date_tag_id)

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

        # Set all accounts to exactly the 3 required tags
        for acc in our_accounts:
            sl_tag_account(acc["id"], required_tags, client_id=sl_client_id)

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

    invalidate_cache()
    yield event(0, "complete")


# ---------------------------------------------------------------------------
# A/B Rotation
# ---------------------------------------------------------------------------

def swap_client_group(client_name):
    """Swap a client's active group in all their campaigns.

    1. Read rotation record
    2. Find campaigns with outgoing accounts
    3. Add incoming accounts, remove outgoing accounts
    4. Update rotation record
    Returns dict with results.
    """
    rotation = store.get_rotation(client_name)
    if not rotation:
        return {"error": f"No rotation record for '{client_name}'"}

    active = rotation["active_group"]
    a_ids = rotation["group_a_ids"]
    b_ids = rotation["group_b_ids"]
    if isinstance(a_ids, str):
        a_ids = json.loads(a_ids)
    if isinstance(b_ids, str):
        b_ids = json.loads(b_ids)

    if active == "A":
        outgoing_ids, incoming_ids, new_active = set(a_ids), b_ids, "B"
    else:
        outgoing_ids, incoming_ids, new_active = set(b_ids), a_ids, "A"

    if not incoming_ids:
        return {"error": f"Group {new_active} has no accounts — cannot swap"}

    # Find all campaigns
    r = requests.get(f"{SMARTLEAD_API}/campaigns?api_key={SMARTLEAD_KEY}", timeout=30)
    all_campaigns = r.json() if r.status_code == 200 and isinstance(r.json(), list) else []
    active_campaigns = [c for c in all_campaigns if c.get("status") in ("ACTIVE", "PAUSED")]

    # Find campaigns containing outgoing accounts
    campaigns_updated = []
    for camp in active_campaigns:
        cr = requests.get(
            f"{SMARTLEAD_API}/campaigns/{camp['id']}/email-accounts?api_key={SMARTLEAD_KEY}",
            timeout=30,
        )
        if cr.status_code != 200:
            continue
        camp_accounts = cr.json() if isinstance(cr.json(), list) else []
        camp_account_ids = {ca["id"] for ca in camp_accounts}
        if camp_account_ids & outgoing_ids:
            campaigns_updated.append({"id": camp["id"], "name": camp.get("name", "")})
        time.sleep(0.2)

    if not campaigns_updated:
        return {"error": f"No campaigns found with Group {active} accounts for {client_name}"}

    # Add incoming accounts to campaigns
    for camp in campaigns_updated:
        requests.post(
            f"{SMARTLEAD_API}/campaigns/{camp['id']}/email-accounts?api_key={SMARTLEAD_KEY}",
            json={"email_account_ids": incoming_ids},
            timeout=30,
        )
        time.sleep(0.3)

    # Remove outgoing accounts from campaigns
    for camp in campaigns_updated:
        for old_id in outgoing_ids:
            requests.delete(
                f"{SMARTLEAD_API}/campaigns/{camp['id']}/email-accounts/{old_id}?api_key={SMARTLEAD_KEY}",
                timeout=30,
            )
            time.sleep(0.3)

    # Update rotation record
    today = datetime.now().strftime("%Y-%m-%d")
    store.update_rotation_swap(client_name, new_active, today)

    return {
        "ok": True,
        "client_name": client_name,
        "previous_group": active,
        "new_group": new_active,
        "campaigns_updated": len(campaigns_updated),
        "campaign_names": [c["name"] for c in campaigns_updated],
        "accounts_added": len(incoming_ids),
        "accounts_removed": len(outgoing_ids),
    }


def api_rotation_status():
    """GET /api/rotation/status — return all rotation records."""
    rotations = store.get_all_rotations()
    for r in rotations:
        if isinstance(r.get("group_a_ids"), str):
            r["group_a_ids"] = json.loads(r["group_a_ids"])
        if isinstance(r.get("group_b_ids"), str):
            r["group_b_ids"] = json.loads(r["group_b_ids"])
    return {"rotations": rotations}


def build_pipeline_config(body: dict) -> dict:
    """Build a pipeline config dict from the POST body."""
    pipeline_type = body.get("type", "generic")
    name = body.get("name", "")
    domains = [d.strip() for d in body.get("domains", "").split("\n") if d.strip()]
    sender = body.get("sender", "sean_reynolds")

    # Resolve tags
    existing_tags = sl_get_all_tags()
    zapmail_tag = sl_find_or_create_tag("Zapmail", existing_tags=existing_tags)
    date_str = datetime.now().strftime("%-m/%-d/%y")
    date_tag = sl_find_or_create_tag(date_str, existing_tags=existing_tags)
    group_tag = sl_find_or_create_tag(name, existing_tags=existing_tags)

    tag_ids = {"zapmail": zapmail_tag, "date": date_tag, "group": group_tag}

    # Resolve or create SmartLead client
    sl_client_id = body.get("smartlead_client_id")
    if not sl_client_id:
        try:
            sl_clients = requests.get(
                f"{SMARTLEAD_API}/client", params={"api_key": SMARTLEAD_KEY}, timeout=30
            ).json()
            name_lower = name.lower().strip()
            for c in sl_clients:
                cn = c["name"].lower().strip()
                if cn == name_lower or name_lower in cn or cn in name_lower:
                    sl_client_id = c["id"]
                    break
            if not sl_client_id:
                slug = name.lower().replace("'", "").replace(" ", "").replace("&", "")
                cr = requests.post(
                    f"{SMARTLEAD_API}/client/save",
                    params={"api_key": SMARTLEAD_KEY},
                    json={"name": name, "email": f"tht.{slug}.client@gmail.com",
                          "password": "THTclient2026!"},
                    timeout=30,
                )
                if cr.status_code == 201:
                    sl_client_id = cr.json().get("clientId")
        except Exception as e:
            print(f"[PIPELINE] SmartLead client lookup failed: {e}")

    photo_url = pipeline_engine.PHOTO_URLS.get(sender, pipeline_engine.PROFILE_PHOTO_URL)

    return {
        "domains": domains,
        "sender": sender,
        "group_name": name,
        "smartlead_client_id": sl_client_id,
        "tag_ids": tag_ids,
        "mailbox_ids": [],
        "smartlead_account_ids": {},
        "profile_photo_url": photo_url,
    }


def next_generic_name() -> str:
    """Return the next available 'Generic X' name."""
    # Check existing SmartLead clients
    try:
        sl_clients = requests.get(
            f"{SMARTLEAD_API}/client", params={"api_key": SMARTLEAD_KEY}, timeout=30
        ).json()
    except Exception:
        sl_clients = []
    used = set()
    for c in sl_clients:
        n = c.get("name", "")
        if n.lower().startswith("generic ") and len(n) > 8:
            used.add(n.split(" ")[-1].upper())

    # Also check active pipelines
    active = store.list_setup_pipelines()
    for p in active:
        n = p.get("name", "")
        if n.lower().startswith("generic ") and len(n) > 8:
            used.add(n.split(" ")[-1].upper())

    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        if letter not in used:
            return f"Generic {letter}"
    return "Generic Z2"


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
                elif path.startswith("/api/client/") and path.endswith("/trends-debug"):
                    client_id = path.split("/")[3]
                    self._json_response(debug_client_trends(client_id))
                elif path.startswith("/api/client/") and path.endswith("/trends"):
                    client_id = path.split("/")[3]
                    params = parse_qs(parsed.query)
                    days = int(params.get("days", [30])[0])
                    self._json_response(api_client_trends(client_id, days))
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
                elif path == "/api/untagged-count":
                    self._json_response(api_untagged_count())
                elif path == "/api/acquisition":
                    self._json_response(api_acquisition())
                elif path == "/api/acquisition-campaigns":
                    self._json_response(api_acquisition_campaigns())
                elif path == "/api/generic-groups":
                    self._json_response(api_generic_groups())
                elif path == "/api/clients/list":
                    self._json_response(api_clients_list())
                elif path == "/api/rotation/status":
                    self._json_response(api_rotation_status())
                elif path == "/api/debug/supabase":
                    self._json_response(api_debug_supabase())
                elif path == "/api/inbox-history":
                    self._json_response(api_inbox_history(params))
                elif path == "/api/snapshot":
                    self._json_response(snapshot_all_inboxes())
                elif path == "/api/setup-pipelines":
                    pipelines = store.list_setup_pipelines()
                    self._json_response({"pipelines": pipelines})
                elif path.startswith("/api/setup-pipeline/"):
                    pid = path.split("/")[-1]
                    p = store.get_setup_pipeline(pid)
                    if p:
                        self._json_response(p)
                    else:
                        self._json_response({"error": "not found"}, 404)
                elif path == "/api/next-generic-name":
                    self._json_response({"name": next_generic_name()})
                elif path == "/api/supabase-config":
                    self._json_response({"url": store.SUPABASE_URL, "key": store.SUPABASE_KEY})
                elif path == "/api/generic-groups-status":
                    status_file = os.path.join(os.path.dirname(__file__), "generic_groups_status.json")
                    state_file = os.path.join(os.path.dirname(__file__), "generic_groups_state.json")
                    result = {"running": False, "step": "unknown", "progress": 0, "detail": "", "completed_steps": []}
                    if os.path.exists(state_file):
                        with open(state_file) as f:
                            state = json.load(f)
                        result["completed_steps"] = state.get("completed_steps", [])
                    if os.path.exists(status_file):
                        with open(status_file) as f:
                            status = json.load(f)
                        result.update(status)
                        result["running"] = status.get("step") != "complete"
                    self._json_response(result)
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
            registrar = body.get("registrar", "").lower()
            enabled = body.get("enabled", False)
            if not domain or not registrar:
                self._error(400, "domain and registrar required")
                return
            if registrar == "porkbun":
                result = porkbun_set_auto_renew(domain, enabled)
            elif registrar == "spaceship":
                result = spaceship_set_auto_renew(domain, enabled)
            else:
                result = {"success": False, "message": f"{registrar} auto-renew toggle not supported via API"}
            self._json_response(result)
        elif path == "/api/domains/bulk-auto-renew":
            domains = body.get("domains", [])
            enabled = body.get("enabled", False)
            if not domains:
                self._error(400, "domains list required")
                return
            def _bulk_toggle():
                results = {"success": 0, "failed": 0, "errors": []}
                for d in domains:
                    name = d.get("domain", "")
                    reg = d.get("registrar", "").lower()
                    try:
                        if reg == "spaceship":
                            r = spaceship_set_auto_renew(name, enabled)
                        elif reg == "porkbun":
                            r = porkbun_set_auto_renew(name, enabled)
                        else:
                            r = {"success": False, "message": "unsupported"}
                        if r["success"]:
                            results["success"] += 1
                        else:
                            results["failed"] += 1
                            if len(results["errors"]) < 5:
                                results["errors"].append(f"{name}: {r.get('message','')}")
                    except Exception as e:
                        results["failed"] += 1
                        if len(results["errors"]) < 5:
                            results["errors"].append(f"{name}: {e}")
                    time.sleep(0.5)
                return results
            result = _bulk_toggle()
            self._json_response(result)
        elif path == "/api/pipeline/new-client":
            result = api_pipeline_new_client(body)
            self._json_response(result, 400 if "error" in result else 200)
        elif path == "/api/pipeline/replacement":
            result = api_pipeline_replacement(body)
            self._json_response(result, 400 if "error" in result else 200)
        elif path == "/api/pipeline/new-acquisition":
            result = api_pipeline_new_acquisition(body)
            self._json_response(result, 400 if "error" in result else 200)
        elif path == "/api/pipeline/retry":
            result = api_pipeline_retry(body)
            self._json_response(result, 400 if "error" in result else 200)
        elif path == "/api/pipeline/skip-step":
            result = api_pipeline_skip_step(body)
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
        elif path == "/api/client/archive":
            client_name = body.get("client_name", "")
            archived = body.get("archived", True)
            if not client_name:
                self._error(400, "client_name required")
                return
            try:
                state = store.get_state("archived_clients") or {"clients": []}
                clients_list = state.get("clients", [])
                if archived and client_name not in clients_list:
                    clients_list.append(client_name)
                    # Also pause monitor when archiving
                    pause_state = store.get_state("paused_clients") or {"clients": []}
                    pause_list = pause_state.get("clients", [])
                    if client_name not in pause_list:
                        pause_list.append(client_name)
                        store.set_state("paused_clients", {"clients": pause_list})
                elif not archived and client_name in clients_list:
                    clients_list.remove(client_name)
                store.set_state("archived_clients", {"clients": clients_list})
                invalidate_cache()
                self._json_response({"ok": True, "archived_clients": clients_list})
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
        elif path == "/api/client/delete-infra":
            client_id = body.get("client_id")
            client_name = body.get("client_name", "").strip()
            if not client_id or not client_name:
                self._error(400, "client_id and client_name required")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                for chunk in delete_client_infra_sse(client_id, client_name):
                    self.wfile.write(chunk.encode())
                    self.wfile.flush()
            except Exception as e:
                error_data = json.dumps({"step": 0, "status": "error", "message": str(e)})
                self.wfile.write(f"data: {error_data}\n\n".encode())
                self.wfile.flush()
        elif path == "/api/client/transition":
            client_id = body.get("client_id")
            client_name = body.get("client_name", "").strip()
            new_client_name = body.get("new_client_name", "").strip()
            forwarding_domain = body.get("forwarding_domain", "").strip()
            is_new_client = body.get("is_new_client", False)
            if not client_id or not client_name or not new_client_name or not forwarding_domain:
                self._error(400, "client_id, client_name, new_client_name, and forwarding_domain required")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                for chunk in transition_client_sse(client_id, client_name, new_client_name, forwarding_domain, is_new_client):
                    self.wfile.write(chunk.encode())
                    self.wfile.flush()
            except Exception as e:
                error_data = json.dumps({"step": 0, "status": "error", "message": str(e)})
                self.wfile.write(f"data: {error_data}\n\n".encode())
                self.wfile.flush()
        elif path == "/api/inbox/remove-from-campaign":
            result = api_remove_from_campaign(body)
            invalidate_cache()
            self._json_response(result)
        elif path == "/api/inbox/remove-from-all-campaigns":
            result = api_remove_from_all_campaigns(body)
            invalidate_cache()
            self._json_response(result)
        elif path == "/api/rotation/swap":
            client_name = body.get("client_name", "")
            if not client_name:
                self._error(400, "client_name required")
                return
            result = swap_client_group(client_name)
            self._json_response(result, 400 if "error" in result else 200)
        elif path == "/api/rotation/swap-all":
            rotations = store.get_all_rotations()
            # Skip archived clients
            try:
                arch_state = store.get_state("archived_clients") or {"clients": []}
                arch_set = set(arch_state.get("clients", []))
            except Exception:
                arch_set = set()
            results = []
            for rot in rotations:
                if rot["client_name"] in arch_set:
                    continue
                result = swap_client_group(rot["client_name"])
                results.append(result)
            self._json_response({"results": results})
        elif path == "/api/setup-pipeline/create":
            try:
                config = build_pipeline_config(body)
                pid = pipeline_engine.create_and_start(
                    body.get("name", ""), body.get("type", "generic"), config
                )
                self._json_response({"id": pid, "status": "running"})
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
        elif path == "/api/setup-pipeline/retry":
            pid = body.get("pipeline_id", "")
            ok = pipeline_engine.retry_failed_step(pid)
            self._json_response({"ok": ok})
        elif path == "/api/acquisition/assign-campaign":
            result = api_assign_group_campaign(body)
            self._json_response(result, 400 if "error" in result else 200)
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
    print("Cleaning up stuck pipelines...", flush=True)
    cleanup_stuck_pipelines()
    print("Cleanup done", flush=True)

    # Start background infrastructure monitor (auto-disabled on Render to save memory)
    is_render = bool(os.environ.get("PORT")) and not os.environ.get("ENABLE_MONITOR")
    print("Starting monitor...", flush=True)
    if not is_render:
        monitor = start_monitor_thread()
        print("Infrastructure monitor started (checking every 4 hours)", flush=True)
    else:
        print("Infrastructure monitor DISABLED (Render free tier — set ENABLE_MONITOR=1 to override)", flush=True)

    # Start background SmartLead → Supabase sync (every 2 minutes)
    print("Starting sync thread...", flush=True)
    start_sync_thread()
    print("SmartLead + Spaceship background sync started (every 2 minutes)", flush=True)

    # Resume any interrupted setup pipelines
    print("Resuming pipelines...", flush=True)
    pipeline_engine.resume_running_pipelines()
    print("Setup pipeline resume check complete", flush=True)

    # Take initial inbox snapshot for history tracking
    print("Taking inbox snapshot...", flush=True)
    try:
        snap = snapshot_all_inboxes()
        print(f"Inbox snapshot: {snap.get('accounts', 0)} accounts, {snap.get('diffs', 0)} diffs", flush=True)
    except Exception as e:
        print(f"Inbox snapshot failed (non-critical): {e}", flush=True)

    print(f"Binding to {host}:{port}...", flush=True)
    server = HTTPServer((host, port), DashboardHandler)
    print(f"Dashboard running at http://{host}:{port}", flush=True)
    print("Press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
