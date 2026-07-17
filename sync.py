"""Local sync script — fetches SmartLead data and writes to Supabase cache.

Run this locally to populate the dashboard cache. Vercel only reads from cache.

Usage:
    python sync.py          # one-shot sync
    python sync.py --loop   # sync every 2 minutes
"""

import json
import os
import re
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
        # GraphQL errors return {"errors":[...], "data": null} — `.get("data", {})`
        # would yield None (key present) and crash; coerce null -> {}.
        rows = ((result or {}).get("data") or {}).get("email_account_tag_mappings", [])
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
        metrics = (r.json().get("data") or {}).get("email_health_metrics", [])
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


def fetch_campaign_accounts(progress_cb=None):
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
    active = [c for c in campaigns if c.get("status") in ("ACTIVE", "PAUSED") and "subsequence" not in c.get("name", "").lower()]
    print(f"  Found {len(active)} active/paused campaigns (excluding subsequences)")

    for i, camp in enumerate(active):
        if progress_cb:
            progress_cb(i, len(active))
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
            f"{CRM_SUPABASE_URL}/rest/v1/clients?select=name,client_standing&client_standing=not.is.null",
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


def build_overview(accounts, health, crm_names, campaign_map, health_today=None):
    """Group accounts by tag into client cards."""
    health_today = health_today or {}
    client_groups = {}  # normalized_name -> {display_name, a_accounts: [], b_accounts: []}
    acq_groups = {}
    generic_groups = {}
    untagged = []

    def _norm(name):
        import re
        n = name.lower().strip()
        prev = ''
        while prev != n:
            prev = n
            n = re.sub(r'\s+(group|llc|inc\.?|construction|landscaping|lawn\s*care|hvac|'
                       r'land\s*care|scapes|landscape|heating\s*&?\s*air.*|'
                       r'lawn\s*solutions|land\s*solutions|&\s*design|conditioning)\s*$',
                       '', n, flags=re.IGNORECASE)
            n = re.sub(r'[,.\s&]+$', '', n).strip()
        return re.sub(r'\s+', ' ', n)

    _crm_norm_map = {}
    for cn in crm_names:
        _crm_norm_map[_norm(cn)] = cn

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
        total_sent = daily_sent = smtp_fail = 0
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
            ht = health_today.get(email)
            if ht:
                daily_sent += ht.get("sent", 0)
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
            raw_rep = (a.get("warmup_details") or {}).get("warmup_reputation", "?")
            warmup_reputation = None
            if isinstance(raw_rep, str) and raw_rep.endswith("%"):
                try:
                    warmup_reputation = int(raw_rep[:-1])
                except ValueError:
                    pass
            account_details.append({
                "id": a.get("id"),
                "email": email,
                "domain": domain,
                "bounce_rate": parse_rate(h.get("bounce_rate")),
                "reply_rate": parse_rate(h.get("reply_rate")),
                "sent": h.get("sent", 0),
                "smtp_ok": bool(a.get("is_smtp_success")),
                "warmup_enabled": bool(a.get("warmup_enabled")),
                "in_campaign": len(acct_camps) > 0,
                "campaign_names": [c["name"] for c in acct_camps],
                "warmup_reputation": warmup_reputation,
            })
        account_details.sort(key=lambda x: x["email"])
        rep_values = [ad["warmup_reputation"] for ad in account_details if ad["warmup_reputation"] is not None]
        return {
            "in_campaign": assigned,
            "smtp_failures": smtp_fail,
            "total_domains": len(domains),
            "avg_bounce_rate": round(sum(bounce_rates) / len(bounce_rates), 1) if bounce_rates else None,
            "avg_reply_rate": round(sum(reply_rates) / len(reply_rates), 1) if reply_rates else None,
            "avg_warmup_reputation": round(sum(rep_values) / len(rep_values), 1) if rep_values else None,
            "total_sent": total_sent,
            "daily_sent": daily_sent,
            "daily_capacity": len(accts) * 15,
            "campaigns": sorted(camps.values(), key=lambda x: x["name"]),
            "account_details": account_details,
        }

    clients = []
    matched_crm_norms = set()
    for key, group in sorted(client_groups.items(), key=lambda x: x[0]):
        all_accts = group["a"] + group["b"]
        combined = _group_stats(all_accts)
        display_name = group["display_name"]
        normed = _norm(display_name)
        if normed in _crm_norm_map:
            display_name = _crm_norm_map[normed]
            matched_crm_norms.add(normed)
        combined["name"] = display_name
        combined["tag_name"] = group["display_name"]
        combined["accounts"] = len(all_accts)
        combined["group_a_count"] = len(group["a"])
        combined["group_b_count"] = len(group["b"])
        combined["group_a"] = _group_stats(group["a"]) if group["a"] else None
        combined["group_b"] = _group_stats(group["b"]) if group["b"] else None
        clients.append(combined)

    for crm_name in crm_names:
        if _norm(crm_name) not in matched_crm_norms:
            clients.append({
                "name": crm_name,
                "accounts": 0,
                "group_a_count": 0,
                "group_b_count": 0,
                "group_a": None,
                "group_b": None,
                "in_campaign": 0,
                "smtp_failures": 0,
                "total_domains": 0,
                "avg_bounce_rate": None,
                "avg_reply_rate": None,
                "daily_capacity": 0,
                "total_sent": 0,
                "daily_sent": 0,
                "campaigns": [],
                "account_details": [],
                "crm_only": True,
            })

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
        # Extract warmup start date from date tag (e.g. "6/2/26")
        date_tag = None
        for a in accts:
            for t in a.get("tags", []):
                tn = t.get("name", "")
                if re.match(r'^\d{1,2}/\d{1,2}/\d{2,4}$', tn):
                    if date_tag is None or tn < date_tag:
                        date_tag = tn
                    break
        if date_tag:
            try:
                parts = date_tag.split("/")
                m, d, y = int(parts[0]), int(parts[1]), int(parts[2])
                if y < 100:
                    y += 2000
                start = datetime(y, m, d)
                gs["warmup_start"] = start.strftime("%Y-%m-%d")
                gs["warmup_days"] = (datetime.now() - start).days
            except Exception:
                pass
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


def fetch_acq_campaign_stats(progress_cb=None):
    """Fetch live lead counts and recent daily sends for active acquisition campaigns."""
    r = _api_get(f"{SMARTLEAD_API}/campaigns", {"api_key": SMARTLEAD_KEY}, timeout=60)
    if not r or r.status_code != 200:
        return []
    campaigns = r.json() if r.text.strip() else []
    active_acq = [c for c in campaigns if c.get("status") == "ACTIVE"
                  and "acquisition" in c.get("name", "").lower()
                  and "subsequence" not in c.get("name", "").lower()]
    paused_acq = [c for c in campaigns if c.get("status") in ("PAUSED", "COMPLETED")
                  and "acquisition" in c.get("name", "").lower()
                  and "subsequence" not in c.get("name", "").lower()]

    stats = []
    all_acq = active_acq + paused_acq
    for i, camp in enumerate(all_acq):
        if progress_cb:
            progress_cb(i, len(all_acq))
        cid = camp["id"]
        is_active = camp.get("status") == "ACTIVE"

        all_r = _api_get(f"{SMARTLEAD_API}/campaigns/{cid}/analytics", {"api_key": SMARTLEAD_KEY}, timeout=15)
        if all_r and all_r.status_code == 200:
            ad = all_r.json() or {}
            total_sent = int(ad.get("sent_count", 0))
            total_opened = int(ad.get("unique_open_count", 0))
            total_replied = int(ad.get("reply_count", 0))
            total_bounced = int(ad.get("bounce_count", 0))
            total_leads_count = int(ad.get("total_count", 0))
            unique_sent = int(ad.get("unique_sent_count", 0))
        else:
            total_sent = total_opened = total_replied = total_bounced = total_leads_count = unique_sent = 0

        lead_counts = {"COMPLETED": 0, "INPROGRESS": 0, "STARTED": 0}
        if is_active:
            acct_r = _api_get(f"{SMARTLEAD_API}/campaigns/{cid}/email-accounts", {"api_key": SMARTLEAD_KEY}, timeout=15)
            acct_count = len(acct_r.json()) if acct_r and acct_r.status_code == 200 else 0

            for status_key in ("COMPLETED", "INPROGRESS", "STARTED"):
                lr = _api_get(f"{SMARTLEAD_API}/campaigns/{cid}/leads",
                              {"api_key": SMARTLEAD_KEY, "limit": 1, "offset": 0, "status": status_key}, timeout=15)
                lead_counts[status_key] = int((lr.json() or {}).get("total_leads", 0)) if lr and lr.status_code == 200 else 0
        else:
            acct_count = 0

        contacted = lead_counts["COMPLETED"] + lead_counts["INPROGRESS"]
        active_leads = contacted + lead_counts["STARTED"]
        stats.append({
            "id": cid, "name": camp["name"], "status": camp.get("status", ""),
            "accounts": acct_count,
            "total_leads": active_leads, "completed": contacted, "remaining": lead_counts["STARTED"],
            "total_sent": total_sent, "total_opened": total_opened,
            "total_replied": total_replied, "total_bounced": total_bounced,
        })
    return stats


def sync(progress_cb=None):
    def _report(pct, msg):
        print(f"  [{pct}%] {msg}")
        if progress_cb:
            try:
                progress_cb(pct, msg)
            except Exception:
                pass

    print(f"[sync] Starting at {datetime.now().strftime('%H:%M:%S')}")
    _report(0, "Starting sync...")

    _report(2, "Fetching CRM clients...")
    crm_names = fetch_crm_clients()
    _report(5, f"Got {len(crm_names)} CRM clients")

    _report(6, "Fetching SmartLead accounts...")
    accounts = fetch_all_accounts()
    _report(15, f"Got {len(accounts)} accounts")

    if len(accounts) < 100:
        _report(0, f"Aborted: only {len(accounts)} accounts (rate limited?)")
        return False

    _report(16, "Fetching tag mappings...")
    tag_map = fetch_tag_mappings()
    _report(22, f"Got tags for {len(tag_map)} accounts")

    for a in accounts:
        a["tags"] = tag_map.get(a["id"], [])

    _report(23, "Fetching health metrics...")
    health = fetch_health_metrics()
    _report(30, f"Got {len(health)} health records")

    if len(health) < 50:
        _report(0, f"Aborted: only {len(health)} health records (rate limited?)")
        return False

    today_str = datetime.now().strftime("%Y-%m-%d")
    _report(31, "Fetching today's sent counts...")
    health_today = fetch_health_metrics(start_date=today_str, end_date=today_str)
    _report(35, f"Got {len(health_today)} daily records")

    _report(36, "Fetching campaign account mappings...")
    def _camp_progress(i, total):
        pct = 36 + int((i / max(total, 1)) * 49)
        _report(pct, f"Fetching campaigns ({i + 1}/{total})...")
    campaign_map = fetch_campaign_accounts(progress_cb=_camp_progress)
    _report(85, f"Got campaigns for {len(campaign_map)} accounts")

    _report(86, "Building overview...")
    overview = build_overview(accounts, health, crm_names, campaign_map, health_today)
    client_count = len(overview["clients"])
    _report(88, f"Built: {client_count} clients, {overview['total_accounts']} accounts")

    if client_count < 8:
        _report(0, f"Aborted: only {client_count} clients (need >= 8)")
        return False

    _report(89, "Fetching acquisition campaign stats...")
    def _acq_progress(i, total):
        pct = 89 + int((i / max(total, 1)) * 6)
        _report(pct, f"Fetching acquisition campaign stats ({i + 1}/{total})...")
    overview["acq_campaign_stats"] = fetch_acq_campaign_stats(progress_cb=_acq_progress)
    _report(95, f"Got stats for {len(overview['acq_campaign_stats'])} acquisition campaigns")

    _report(96, "Writing cache...")
    store.cache_set("overview_v2", overview)
    print(f"  Cache written: {client_count} clients")

    _report(97, "Syncing domain renewals...")
    from dashboard import sync_domain_renewals
    try:
        rn = sync_domain_renewals()
        print(f"  Domain renewals: {len(rn.get('domain_renewals', {}))} mapped")
    except Exception as e:
        print(f"  Domain renewals failed: {e}")

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
        snapshot["groups"][c["name"]] = {"bounce": c.get("avg_bounce_rate"), "reply": c.get("avg_reply_rate"), "sent": c.get("total_sent", 0), "daily_sent": c.get("daily_sent", 0)}
        if c.get("group_a"):
            snapshot["groups"][c["name"] + " A"] = {"bounce": c["group_a"].get("avg_bounce_rate"), "reply": c["group_a"].get("avg_reply_rate"), "sent": c["group_a"].get("total_sent", 0), "daily_sent": c["group_a"].get("daily_sent", 0)}
        if c.get("group_b"):
            snapshot["groups"][c["name"] + " B"] = {"bounce": c["group_b"].get("avg_bounce_rate"), "reply": c["group_b"].get("avg_reply_rate"), "sent": c["group_b"].get("total_sent", 0), "daily_sent": c["group_b"].get("daily_sent", 0)}
    for g in overview.get("acquisition_groups", []):
        snapshot["groups"][g["name"]] = {"bounce": g.get("avg_bounce_rate"), "reply": g.get("avg_reply_rate"), "sent": g.get("total_sent", 0), "daily_sent": g.get("daily_sent", 0)}
    for g in overview.get("generic_groups", []):
        snapshot["groups"][g["name"]] = {"bounce": g.get("avg_bounce_rate"), "reply": g.get("avg_reply_rate"), "sent": g.get("total_sent", 0), "daily_sent": g.get("daily_sent", 0)}
    history = [h for h in history if h.get("date") != today]
    history.append(snapshot)
    history = history[-90:]
    store.cache_set("health_history", history)
    print(f"  Health snapshot saved ({len(history)} days in history)")

    # Health V1 — per-inbox daily snapshot + scoring (reads the fresh overview
    # we just built; writes health_fleet cache). No SmartLead/JWT call.
    try:
        import health_snapshot
        hres = health_snapshot.snapshot_daily(overview=overview)
        print(f"  Health V1: {hres.get('inboxes', 0)} inboxes scored — {hres.get('counts')}")
    except Exception as e:
        print(f"  Health V1 snapshot failed (non-fatal): {e}")

    _report(99, "Verifying cache...")
    cached, ts = store.cache_get("overview_v2")
    verified = len((cached or {}).get("clients", []))
    print(f"  Verified: {verified} clients in cache")
    _report(100, f"Sync complete — {verified} clients, {overview['total_accounts']} accounts")
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
