"""Local sync script — fetches SmartLead data and writes to Supabase cache.

Run this locally to populate the dashboard cache. Vercel only reads from cache.

Usage:
    python sync.py          # one-shot sync
    python sync.py --loop   # sync every 2 minutes
"""

import json
import os
import sys
import time
import requests
from datetime import datetime, timedelta
from pathlib import Path

# Load env
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

import db as store
store._CACHE_WRITE_ENABLED = True
from setup import sl_gql, _RateLimiter, SMARTLEAD_GQL, SMARTLEAD_JWT
from tag_utils import parse_group_tag, get_group_tag_from_account

SMARTLEAD_API = "https://server.smartlead.ai/api/v1"
SMARTLEAD_INTERNAL_API = "https://server.smartlead.ai/api"
SMARTLEAD_KEY = os.environ.get("SMARTLEAD_API_KEY", "")

_rate = _RateLimiter(max_requests=180, window_seconds=60)

CRM_SUPABASE_URL = os.environ.get("CRM_SUPABASE_URL", "")
CRM_SUPABASE_KEY = os.environ.get("CRM_SUPABASE_KEY", "")


def sl_internal_headers():
    return {"Authorization": f"Bearer {SMARTLEAD_JWT}", "Content-Type": "application/json"}


def fetch_all_accounts():
    accounts = []
    offset = 0
    while True:
        r = _api_get(
            f"{SMARTLEAD_API}/email-accounts/",
            {"api_key": SMARTLEAD_KEY, "offset": offset, "limit": 100},
            timeout=30,
        )
        if not r or r.status_code != 200:
            break
        batch = r.json() if r.text.strip() else []
        if not isinstance(batch, list):
            break
        accounts.extend(batch)
        if len(batch) < 100:
            break
        offset += 100
    return accounts


def fetch_tag_mappings():
    mappings = {}
    offset = 0
    while True:
        try:
            result = sl_gql(
                '{ email_account_tag_mappings(limit: 1000, offset: %d) '
                '{ email_account_id tag { id name } } }' % offset
            )
        except Exception as e:
            print(f"  GQL error at offset {offset}: {e}")
            break
        rows = (result or {}).get("data", {}).get("email_account_tag_mappings", [])
        for row in rows:
            acc_id = row["email_account_id"]
            tag = row.get("tag", {})
            mappings.setdefault(acc_id, []).append({"id": tag.get("id"), "name": tag.get("name", "")})
        if len(rows) < 1000:
            break
        offset += 1000
    return mappings


def fetch_health_metrics(start_date=None, end_date=None):
    end = end_date or datetime.now().strftime("%Y-%m-%d")
    start = start_date or (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    try:
        r = _api_get(
            f"{SMARTLEAD_INTERNAL_API}/analytics/mailbox/name-wise-health-metrics",
            {"start_date": start, "end_date": end, "timezone": "America/New_York", "full_data": "true"},
            timeout=30,
            headers=sl_internal_headers(),
        )
        if not r or r.status_code != 200:
            return {}
        metrics = r.json().get("data", {}).get("email_health_metrics", [])
        return {m["from_email"]: m for m in metrics}
    except Exception as e:
        print(f"  Health metrics error: {e}")
        return {}


def _api_get(url, params=None, timeout=15, headers=None):
    """SmartLead API GET with exponential backoff on 429. Returns response or None."""
    backoff = 10
    for attempt in range(5):
        _rate.wait()
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            if attempt < 4:
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            return None
        if r.status_code == 429:
            print(f"  429 — backing off {backoff}s (attempt {attempt + 1}/5)")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        return r
    return None


def fetch_campaign_accounts():
    """Fetch all active/paused campaigns and their email accounts.
    Returns email -> list of {id, name, status} campaign info.
    No timeout — retries with backoff until every campaign is fetched.
    """
    email_to_campaigns = {}

    r = _api_get(f"{SMARTLEAD_API}/campaigns", {"api_key": SMARTLEAD_KEY}, timeout=60)
    if not r or r.status_code != 200:
        print("  Failed to fetch campaign list")
        return {}

    campaigns = r.json() if r.text.strip() else []
    active = [c for c in campaigns if c.get("status") in ("ACTIVE", "PAUSED")]
    print(f"  Found {len(active)} active/paused campaigns")

    for i, camp in enumerate(active):
        camp_info = {"id": camp["id"], "name": camp.get("name", ""), "status": camp.get("status", "")}
        cr = _api_get(
            f"{SMARTLEAD_API}/campaigns/{camp['id']}/email-accounts",
            {"api_key": SMARTLEAD_KEY},
            timeout=15,
        )
        if cr and cr.status_code == 200:
            for ca in cr.json():
                email = ca.get("from_email", "")
                if email:
                    email_to_campaigns.setdefault(email, []).append(camp_info)
        if (i + 1) % 20 == 0:
            print(f"  Fetched {i + 1}/{len(active)} campaigns...")

    print(f"  Fetched all {len(active)} campaigns")
    return email_to_campaigns


def fetch_crm_clients():
    if not CRM_SUPABASE_URL or not CRM_SUPABASE_KEY:
        return []
    try:
        r = requests.get(
            f"{CRM_SUPABASE_URL}/rest/v1/clients?select=name",
            headers={"apikey": CRM_SUPABASE_KEY, "Authorization": f"Bearer {CRM_SUPABASE_KEY}"},
            timeout=10,
        )
        if r.status_code == 200:
            return [c["name"].strip() for c in r.json() if c.get("name")]
    except Exception as e:
        print(f"  CRM fetch error: {e}")
    return []


def parse_rate(value):
    if value is None:
        return None
    s = str(value).strip().rstrip("%")
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def build_overview(accounts, health, crm_names, campaign_map):
    """Group accounts by tag into client cards."""
    client_groups = {}  # normalized_name -> {display_name, a_accounts: [], b_accounts: []}
    acq_groups = {}
    generic_groups = {}
    untagged = []

    # Build name alias map for fuzzy matching
    _aliases = {
        "gm landscaping": "gm landscaping & design",
    }

    def _norm(name):
        import re
        n = re.sub(r'\s+(llc|inc\.?|construction)\s*$', '', name.lower().strip(), flags=re.IGNORECASE)
        n = re.sub(r'[,.]', '', n).strip()
        return _aliases.get(n, n)

    for a in accounts:
        tag = get_group_tag_from_account(a)
        if not tag:
            untagged.append(a)
            continue
        parsed = parse_group_tag(tag)
        if parsed["role"] == "acquisition":
            acq_groups.setdefault(parsed["group_letter"], []).append(a)
        elif parsed["role"] == "generic":
            generic_groups.setdefault(parsed["group_letter"], []).append(a)
        else:
            cn = parsed["client_name"] or tag
            letter = parsed["group_letter"]
            key = _norm(cn)
            if key not in client_groups:
                client_groups[key] = {"display_name": cn, "a": [], "b": []}
            elif len(cn) > len(client_groups[key]["display_name"]):
                client_groups[key]["display_name"] = cn
            if letter == "A":
                client_groups[key]["a"].append(a)
            elif letter == "B":
                client_groups[key]["b"].append(a)

    def _group_stats(accts):
        bounce_rates, reply_rates = [], []
        total_sent = smtp_fail = 0
        assigned = 0
        camps = {}
        for a in accts:
            email = a.get("from_email", "")
            if not a.get("is_smtp_success"):
                smtp_fail += 1
            h = health.get(email)
            if h:
                total_sent += h.get("sent", 0)
                br = parse_rate(h.get("bounce_rate"))
                if br is not None:
                    bounce_rates.append(br)
                rr = parse_rate(h.get("reply_rate"))
                if rr is not None:
                    reply_rates.append(rr)
            acct_camps = campaign_map.get(email, [])
            if acct_camps:
                assigned += 1
            for c in acct_camps:
                cid = c["id"]
                if cid not in camps:
                    camps[cid] = {"id": cid, "name": c["name"], "status": c.get("status", ""), "accounts": 0}
                camps[cid]["accounts"] += 1
        domains = set(a.get("from_email", "").split("@")[-1] for a in accts if a.get("from_email"))
        account_details = []
        for a in accts:
            email = a.get("from_email", "")
            domain = email.split("@")[-1] if "@" in email else ""
            h = health.get(email, {})
            acct_camps = campaign_map.get(email, [])
            account_details.append({
                "email": email,
                "domain": domain,
                "bounce_rate": parse_rate(h.get("bounce_rate")),
                "reply_rate": parse_rate(h.get("reply_rate")),
                "sent": h.get("sent", 0),
                "smtp_ok": bool(a.get("is_smtp_success")),
                "warmup_enabled": bool(a.get("warmup_enabled")),
                "in_campaign": len(acct_camps) > 0,
                "campaign_names": [c["name"] for c in acct_camps],
            })
        account_details.sort(key=lambda x: x["email"])
        return {
            "in_campaign": assigned,
            "smtp_failures": smtp_fail,
            "total_domains": len(domains),
            "avg_bounce_rate": round(sum(bounce_rates) / len(bounce_rates), 1) if bounce_rates else None,
            "avg_reply_rate": round(sum(reply_rates) / len(reply_rates), 1) if reply_rates else None,
            "total_sent": total_sent,
            "daily_capacity": len(accts) * 15,
            "campaigns": sorted(camps.values(), key=lambda x: x["name"]),
            "account_details": account_details,
        }

    clients = []
    for key, group in sorted(client_groups.items(), key=lambda x: x[0]):
        all_accts = group["a"] + group["b"]
        combined = _group_stats(all_accts)
        combined["name"] = group["display_name"]
        combined["accounts"] = len(all_accts)
        combined["group_a_count"] = len(group["a"])
        combined["group_b_count"] = len(group["b"])
        combined["group_a"] = _group_stats(group["a"]) if group["a"] else None
        combined["group_b"] = _group_stats(group["b"]) if group["b"] else None
        clients.append(combined)

    # Build acquisition groups
    acq_list = []
    for letter, accts in sorted(acq_groups.items()):
        gs = _group_stats(accts)
        gs["name"] = f"Acquisition {letter}"
        gs["accounts"] = len(accts)
        acq_list.append(gs)

    # Build generic groups
    generic_list = []
    for letter, accts in sorted(generic_groups.items()):
        gs = _group_stats(accts)
        gs["name"] = f"Generic {letter}"
        gs["accounts"] = len(accts)
        generic_list.append(gs)

    total = len(accounts)
    total_in_campaign = sum(c["in_campaign"] for c in clients)

    return {
        "clients": clients,
        "total_accounts": total,
        "in_campaign": total_in_campaign,
        "untagged_count": len(untagged),
        "acquisition_groups": acq_list,
        "generic_groups": generic_list,
        "generated_at": datetime.now().isoformat(),
        "crm_clients": crm_names,
    }


def sync():
    print(f"[sync] Starting at {datetime.now().strftime('%H:%M:%S')}")

    print("  Fetching CRM clients...")
    crm_names = fetch_crm_clients()
    print(f"  Got {len(crm_names)} CRM clients")

    print("  Fetching SmartLead accounts...")
    accounts = fetch_all_accounts()
    print(f"  Got {len(accounts)} accounts")

    if len(accounts) < 100:
        print(f"  ABORT: only {len(accounts)} accounts (rate limited?), skipping cache write")
        return False

    print("  Fetching tag mappings via GQL...")
    tag_map = fetch_tag_mappings()
    print(f"  Got tags for {len(tag_map)} accounts")

    for a in accounts:
        a["tags"] = tag_map.get(a["id"], [])

    print("  Fetching health metrics...")
    health = fetch_health_metrics()
    print(f"  Got {len(health)} health records")

    print("  Fetching campaign mappings...")
    campaign_map = fetch_campaign_accounts()
    print(f"  Got campaigns for {len(campaign_map)} email accounts")

    overview = build_overview(accounts, health, crm_names, campaign_map)
    client_count = len(overview["clients"])
    print(f"  Built overview: {client_count} clients, {overview['total_accounts']} accounts")

    if client_count < 8:
        print(f"  ABORT: only {client_count} clients (need >= 8), skipping cache write")
        return False

    store.cache_set("overview_v2", overview)
    print(f"  Cache written: {client_count} clients")

    # Store daily health snapshot for trend charts
    existing, _ = store.cache_get("health_history")
    history = existing if isinstance(existing, list) else []

    # Backfill past 14 days if history is thin
    if len(history) < 3:
        print("  Backfilling health history for past 14 days...")
        account_groups = {}
        for c in overview["clients"]:
            for ad in (c.get("group_a") or {}).get("account_details", []):
                account_groups[ad["email"]] = (c["name"], "A")
            for ad in (c.get("group_b") or {}).get("account_details", []):
                account_groups[ad["email"]] = (c["name"], "B")
        for g in overview.get("acquisition_groups", []):
            for ad in g.get("account_details", []):
                account_groups[ad["email"]] = (g["name"], None)
        for g in overview.get("generic_groups", []):
            for ad in g.get("account_details", []):
                account_groups[ad["email"]] = (g["name"], None)

        for days_ago in range(14, 0, -1):
            day = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
            if any(h.get("date") == day for h in history):
                continue
            print(f"    Fetching {day}...")
            day_health = fetch_health_metrics(start_date=day, end_date=day)
            if not day_health:
                continue
            snap = {"date": day, "groups": {}}
            group_rates = {}
            for email, metrics in day_health.items():
                info = account_groups.get(email)
                if not info:
                    continue
                gname, letter = info
                br = parse_rate(metrics.get("bounce_rate"))
                rr = parse_rate(metrics.get("reply_rate"))
                sent = metrics.get("sent", 0)
                for key in ([gname, f"{gname} {letter}"] if letter else [gname]):
                    if key not in group_rates:
                        group_rates[key] = {"bounces": [], "replies": [], "sent": 0}
                    if br is not None:
                        group_rates[key]["bounces"].append(br)
                    if rr is not None:
                        group_rates[key]["replies"].append(rr)
                    group_rates[key]["sent"] += sent
            for key, r in group_rates.items():
                snap["groups"][key] = {
                    "bounce": round(sum(r["bounces"]) / len(r["bounces"]), 1) if r["bounces"] else None,
                    "reply": round(sum(r["replies"]) / len(r["replies"]), 1) if r["replies"] else None,
                    "sent": r["sent"],
                }
            history.append(snap)
        history.sort(key=lambda h: h.get("date", ""))
        print(f"  Backfill complete: {len(history)} days")

    # Today's snapshot from current data
    today = datetime.now().strftime("%Y-%m-%d")
    snapshot = {"date": today, "groups": {}}
    for c in overview["clients"]:
        snapshot["groups"][c["name"]] = {"bounce": c.get("avg_bounce_rate"), "reply": c.get("avg_reply_rate"), "sent": c.get("total_sent", 0)}
        if c.get("group_a"):
            snapshot["groups"][c["name"] + " A"] = {"bounce": c["group_a"].get("avg_bounce_rate"), "reply": c["group_a"].get("avg_reply_rate"), "sent": c["group_a"].get("total_sent", 0)}
        if c.get("group_b"):
            snapshot["groups"][c["name"] + " B"] = {"bounce": c["group_b"].get("avg_bounce_rate"), "reply": c["group_b"].get("avg_reply_rate"), "sent": c["group_b"].get("total_sent", 0)}
    for g in overview.get("acquisition_groups", []):
        snapshot["groups"][g["name"]] = {"bounce": g.get("avg_bounce_rate"), "reply": g.get("avg_reply_rate"), "sent": g.get("total_sent", 0)}
    for g in overview.get("generic_groups", []):
        snapshot["groups"][g["name"]] = {"bounce": g.get("avg_bounce_rate"), "reply": g.get("avg_reply_rate"), "sent": g.get("total_sent", 0)}
    history = [h for h in history if h.get("date") != today]
    history.append(snapshot)
    history = history[-90:]
    store.cache_set("health_history", history)
    print(f"  Health snapshot saved ({len(history)} days in history)")

    # Verify
    cached, ts = store.cache_get("overview_v2")
    verified = len((cached or {}).get("clients", []))
    print(f"  Verified: {verified} clients in cache")
    print(f"[sync] Done at {datetime.now().strftime('%H:%M:%S')}")
    return True


if __name__ == "__main__":
    if "--loop" in sys.argv:
        print("Running sync loop (every 2 minutes). Ctrl+C to stop.")
        while True:
            try:
                sync()
            except Exception as e:
                print(f"[sync] Error: {e}")
            time.sleep(120)
    else:
        success = sync()
        sys.exit(0 if success else 1)
