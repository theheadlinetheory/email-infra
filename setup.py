#!/usr/bin/env python3
"""
THT Email Infrastructure Automation Pipeline
=============================================
Fully automated: Form input → Domain purchase (multi-provider) → DNS → Zapmail → SmartLead

Supports multiple domain registrars: Porkbun, Spaceship, and others.
Domains can come from your tracking sheet with a "Provider" column, or be purchased fresh.
DNS (CloudNS nameservers) is set automatically on any supported provider.

Usage:
  python3 setup.py                                              # Interactive mode — full pipeline
  python3 setup.py --auto "Client Name" 1000 forward.com        # Fully automated, no prompts
  python3 setup.py --math 1000                                  # Just calculate infrastructure math
  python3 setup.py --run clients/config.json                    # Resume/run from saved config
  python3 setup.py --dns-only domains.csv                       # Just set nameservers on existing domains
"""

import json
import os
import sys
import csv
import math
import time
import random
import string
import subprocess
import requests
from datetime import datetime, timedelta
from pathlib import Path
import sheets  # Google Sheets integration

# ─── CONFIG ───

SCRIPT_DIR = Path(__file__).parent
ENV_PATH = SCRIPT_DIR / ".env"

def load_env():
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
                os.environ[k.strip()] = v.strip()
    return env

ENV = load_env()

# API Config
ZAPMAIL_API = "https://api.zapmail.ai/api"
ZAPMAIL_KEY = ENV.get("ZAPMAIL_API_KEY", "")
SMARTLEAD_API = "https://server.smartlead.ai/api/v1"
SMARTLEAD_KEY = ENV.get("SMARTLEAD_API_KEY", "")
SMARTLEAD_INTERNAL_API = "https://server.smartlead.ai/api"
SMARTLEAD_GQL = ENV.get("SMARTLEAD_GQL", "https://fe-gql.smartlead.ai/v1/graphql")
SMARTLEAD_JWT = ENV.get("SMARTLEAD_JWT", "")

# Domain Registrars
PORKBUN_API = "https://api.porkbun.com/api/json/v3"
PORKBUN_KEY = ENV.get("PORKBUN_API_KEY", "")
PORKBUN_SECRET = ENV.get("PORKBUN_SECRET_KEY", "")
SPACESHIP_API = "https://spaceship.dev/api/v1"
SPACESHIP_KEY = ENV.get("SPACESHIP_API_KEY", "")
SPACESHIP_SECRET = ENV.get("SPACESHIP_SECRET_KEY", "")

CLOUDNS_NAMESERVERS = [
    "pns61.cloudns.net",
    "pns62.cloudns.com",
    "pns63.cloudns.net",
    "pns64.cloudns.uk"
]

GOOGLE_WARMUP = {
    "warmup_enabled": True,
    "total_warmup_per_day": 15,
    "daily_rampup": 5,
    "reply_rate_percentage": 40
}

EMAILS_PER_ACCOUNT_PER_DAY = 15
ACCOUNTS_PER_DOMAIN = 3


# ─── HELPERS ───

def log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"  [{ts}] {level}: {msg}")

def log_step(step_num, total, msg):
    print(f"\n{'='*60}")
    print(f"  STEP {step_num}/{total}: {msg}")
    print(f"{'='*60}")

def save_config(config, filepath):
    filepath = Path(filepath)
    filepath.parent.mkdir(parents=True, exist_ok=True)
    filepath.write_text(json.dumps(config, indent=2))

def load_config(filepath):
    return json.loads(Path(filepath).read_text())


# ─── INFRASTRUCTURE MATH ───

def calculate_infra(daily_volume):
    accounts_needed = math.ceil(daily_volume / EMAILS_PER_ACCOUNT_PER_DAY)
    domains_needed = math.ceil(accounts_needed / ACCOUNTS_PER_DOMAIN)
    actual_accounts = domains_needed * ACCOUNTS_PER_DOMAIN
    actual_daily = actual_accounts * EMAILS_PER_ACCOUNT_PER_DAY
    warmup_start = datetime.now().strftime("%Y-%m-%d")
    launch_date = (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d")
    return {
        "daily_volume_target": daily_volume,
        "accounts_needed": accounts_needed,
        "domains_needed": domains_needed,
        "actual_accounts": actual_accounts,
        "actual_daily_capacity": actual_daily,
        "warmup_start_date": warmup_start,
        "estimated_launch_date": launch_date
    }


# ─── DOMAIN REGISTRAR ABSTRACTION ───
# Supports: Porkbun, Spaceship (add more by implementing the 4 methods below)

class Porkbun:
    name = "porkbun"

    @staticmethod
    def _auth():
        return {"apikey": PORKBUN_KEY, "secretapikey": PORKBUN_SECRET}

    @staticmethod
    def is_configured():
        return bool(PORKBUN_KEY and PORKBUN_SECRET)

    @staticmethod
    def ping():
        r = requests.post(f"{PORKBUN_API}/ping", json=Porkbun._auth(), timeout=15)
        data = r.json()
        return data.get("status") == "SUCCESS"

    @staticmethod
    def check_domain(domain):
        r = requests.post(f"{PORKBUN_API}/domain/checkDomain/{domain}",
                          json=Porkbun._auth(), timeout=15)
        data = r.json()
        if data.get("status") == "SUCCESS" and data.get("avail") == "yes":
            pricing = data.get("pricing", {})
            return {"available": True, "price": pricing.get("registration", "?")}
        return {"available": False}

    @staticmethod
    def purchase_domain(domain):
        body = {**Porkbun._auth(), "acknowledgement": "yes"}
        r = requests.post(f"{PORKBUN_API}/domain/create/{domain}", json=body, timeout=30)
        data = r.json()
        if data.get("status") == "SUCCESS":
            return {"success": True, "order_id": data.get("orderId"),
                    "cost": data.get("cost"), "balance": data.get("balance")}
        return {"success": False, "error": data.get("message", str(data))}

    @staticmethod
    def set_nameservers(domain):
        body = {**Porkbun._auth(), "ns": CLOUDNS_NAMESERVERS}
        r = requests.post(f"{PORKBUN_API}/domain/updateNs/{domain}", json=body, timeout=15)
        data = r.json()
        return data.get("status") == "SUCCESS"

    @staticmethod
    def list_domains():
        r = requests.post(f"{PORKBUN_API}/domain/listAll", json=Porkbun._auth(), timeout=30)
        data = r.json()
        if data.get("status") == "SUCCESS":
            return [d.get("domain", "") for d in data.get("domains", [])]
        return []


class Spaceship:
    name = "spaceship"

    @staticmethod
    def _headers():
        return {
            "X-API-Key": SPACESHIP_KEY,
            "X-API-Secret": SPACESHIP_SECRET,
            "Content-Type": "application/json"
        }

    @staticmethod
    def is_configured():
        return bool(SPACESHIP_KEY and SPACESHIP_SECRET)

    @staticmethod
    def ping():
        try:
            r = requests.get(f"{SPACESHIP_API}/domains",
                             headers=Spaceship._headers(), timeout=15,
                             params={"take": 1, "skip": 0})
            return r.status_code == 200
        except Exception:
            return False

    @staticmethod
    def check_domain(domain):
        r = requests.get(f"{SPACESHIP_API}/domains/{domain}/available",
                         headers=Spaceship._headers(), timeout=15)
        if r.status_code == 200:
            data = r.json()
            return {"available": True, "price": data.get("price", "?")}
        return {"available": False}

    @staticmethod
    def purchase_domain(domain):
        r = requests.post(f"{SPACESHIP_API}/domains/{domain}",
                          headers=Spaceship._headers(), json={}, timeout=30)
        if r.status_code in (200, 201, 202):
            data = r.json() if r.text else {}
            op_id = r.headers.get("spaceship-async-operationid")
            return {"success": True, "order_id": op_id or data.get("id"),
                    "async_operation": op_id}
        return {"success": False, "error": r.text[:200]}

    @staticmethod
    def set_nameservers(domain):
        body = {"provider": "custom", "hosts": CLOUDNS_NAMESERVERS}
        r = requests.put(f"{SPACESHIP_API}/domains/{domain}/nameservers",
                         headers=Spaceship._headers(), json=body, timeout=15)
        return r.status_code in (200, 204)

    @staticmethod
    def list_domains():
        domains = []
        skip = 0
        while True:
            r = requests.get(f"{SPACESHIP_API}/domains",
                             headers=Spaceship._headers(), timeout=30,
                             params={"take": 100, "skip": skip})
            if r.status_code != 200:
                break
            data = r.json()
            items = data.get("items", data.get("domains", data)) if isinstance(data, dict) else data
            if not isinstance(items, list) or not items:
                break
            for d in items:
                name = d.get("name", d.get("domain", "")) if isinstance(d, dict) else str(d)
                if name:
                    domains.append(name)
            if len(items) < 100:
                break
            skip += 100
        return domains


# Registry of all supported providers
REGISTRARS = {
    "porkbun": Porkbun,
    "spaceship": Spaceship,
}

def get_registrar(provider_name):
    """Get registrar class by name (case-insensitive)."""
    return REGISTRARS.get(provider_name.lower().strip())

def get_configured_registrars():
    """Return list of registrars that have API keys configured."""
    return [r for r in REGISTRARS.values() if r.is_configured()]

def set_nameservers_for_domain(domain, provider_name):
    """Set CloudNS nameservers on a domain using the correct registrar API."""
    registrar = get_registrar(provider_name)
    if not registrar:
        return False, f"Unknown provider: {provider_name}"
    if not registrar.is_configured():
        return False, f"{provider_name} API keys not configured in .env"
    try:
        success = registrar.set_nameservers(domain)
        return success, "OK" if success else "API call failed"
    except Exception as e:
        return False, str(e)


# ─── ZAPMAIL API ───

def zm_headers(workspace_key=None):
    h = {
        "Content-Type": "application/json",
        "x-auth-zapmail": ZAPMAIL_KEY,
        "User-Agent": "tht-infra-automation/1.0"
    }
    if workspace_key:
        h["x-workspace-key"] = workspace_key
    h["x-service-provider"] = "GOOGLE"
    return h

def zm_get(path, workspace_key=None):
    r = requests.get(f"{ZAPMAIL_API}{path}", headers=zm_headers(workspace_key), timeout=30)
    try:
        return r.json()
    except Exception:
        return {"_raw_status": r.status_code, "_raw_text": r.text[:500]}

def zm_post(path, body=None, workspace_key=None):
    r = requests.post(f"{ZAPMAIL_API}{path}", headers=zm_headers(workspace_key),
                      json=body or {}, timeout=60)
    try:
        return r.json()
    except Exception:
        return {"_raw_status": r.status_code, "_raw_text": r.text[:500]}

def zm_put(path, body=None, workspace_key=None):
    r = requests.put(f"{ZAPMAIL_API}{path}", headers=zm_headers(workspace_key),
                     json=body or {}, timeout=30)
    try:
        return r.json()
    except Exception:
        return {"_raw_status": r.status_code, "_raw_text": r.text[:500]}

def zm_list_workspaces():
    return zm_get("/v2/workspaces")

def zm_list_domains(workspace_key=None):
    """List all domains, handling Zapmail's paginated {data: {domains: [...]}} response."""
    all_domains = []
    page = 1
    while True:
        result = zm_get(f"/v2/domains?page={page}", workspace_key)
        if isinstance(result, dict) and "data" in result:
            domains = result["data"].get("domains", [])
            all_domains.extend(domains)
            total_pages = result["data"].get("totalPages", 1)
            if page >= total_pages:
                break
            page += 1
        else:
            break
    return all_domains

def zm_ai_domain_finder(keywords, count=10):
    """Use Zapmail's AI to generate available domain names."""
    body = {"keywords": keywords, "count": count}
    return zm_post("/v2/domains/ai-finder", body)

def zm_check_domain_availability_batch(domains):
    """Check availability of multiple domains at once (max 20)."""
    body = {"domains": domains}
    return zm_post("/v2/domains/available/batch", body)

def zm_connect_domains(domain_names, workspace_key=None):
    """Upload/connect existing domains to Zapmail (NOT purchase — domains already owned on Spaceship).
    Endpoint: POST /v2/domains/connect-domain
    Body: {domainNames: ["domain1.co", "domain2.co"]}
    NS must be set to CloudNS before calling this. Poll this endpoint to check status.
    """
    body = {"domainNames": domain_names}
    return zm_post("/v2/domains/connect-domain", body, workspace_key)

def zm_buy_addon_mailboxes(quantity, workspace_key=None):
    """Buy add-on mailbox slots. Uses wallet balance ($3/mailbox on Pro plan).
    Endpoint: POST /v2/wallet/buy-addon-mailboxes?quantity=N
    """
    return zm_post(f"/v2/wallet/buy-addon-mailboxes?quantity={quantity}", {}, workspace_key)

def zm_create_mailboxes(domain_id, domain_name, mailbox_specs, workspace_key=None):
    """Create mailboxes on a domain.

    mailbox_specs: list of dicts with firstName, lastName, mailboxUsername
    Payload format: {domainId: [{firstName, lastName, mailboxUsername, domainName}, ...]}
    """
    mailboxes = [{
        "firstName": m["firstName"],
        "lastName": m["lastName"],
        "mailboxUsername": m["mailboxUsername"],
        "domainName": domain_name,
    } for m in mailbox_specs]
    body = {domain_id: mailboxes}
    return zm_post("/v2/mailboxes", body, workspace_key)

def zm_list_mailboxes(workspace_key=None, domain_id=None):
    path = "/v2/mailboxes"
    if domain_id:
        path += f"?domainId={domain_id}"
    return zm_get(path, workspace_key)

def zm_update_mailbox(mailbox_id, data, workspace_key=None):
    return zm_put(f"/v2/mailboxes/{mailbox_id}", data, workspace_key)

def zm_set_forwarding(domain_ids, forward_to, workspace_key=None):
    """Set domain forwarding. domain_ids is a list.
    Endpoint: POST /v2/domains/forwarding
    Body: {domainIds: [...], forwardTo: "https://..."}
    """
    body = {"domainIds": domain_ids, "forwardTo": forward_to}
    return zm_post("/v2/domains/forwarding", body, workspace_key)

ZAPMAIL_TAG_COLORS = [
    "#2196F3", "#4CAF50", "#FF9800", "#9C27B0", "#F44336",
    "#00BCD4", "#E91E63", "#3F51B5", "#009688", "#FF5722",
    "#8BC34A", "#673AB7", "#CDDC39", "#795548", "#607D8B",
    "#FFC107", "#03A9F4", "#7C4DFF", "#FF6E40", "#64DD17",
]

def zm_pick_unique_color(workspace_key=None):
    """Pick a tag color not already in use by existing Zapmail tags."""
    existing = zm_list_domain_tags(workspace_key)
    tag_list = existing.get("data", []) if isinstance(existing, dict) else []
    used_colors = {t.get("tagColor", "").upper() for t in tag_list}
    for color in ZAPMAIL_TAG_COLORS:
        if color.upper() not in used_colors:
            return color
    # All preset colors used — generate a random one
    import random
    return f"#{random.randint(0, 0xFFFFFF):06X}"

def zm_create_domain_tag(tag_name, tag_color=None, workspace_key=None):
    """Create a domain tag with a unique color."""
    if tag_color is None:
        tag_color = zm_pick_unique_color(workspace_key)
    body = [{"name": tag_name, "tagColor": tag_color}]
    return zm_post("/v2/domains/tags", body, workspace_key)

def zm_assign_domain_tag(domain_ids, tag_ids, workspace_key=None):
    """Assign tags to domains.
    Endpoint: POST /v2/domains/assign-tag
    Body: {domainIds: [...], tagIds: [...]}
    """
    body = {"domainIds": domain_ids, "tagIds": tag_ids}
    return zm_post("/v2/domains/assign-tag", body, workspace_key)

def zm_list_domain_tags(workspace_key=None):
    return zm_get("/v2/domains/tags", workspace_key)

def zm_add_third_party_account(email, password, app="SMARTLEAD"):
    body = {"email": email, "password": password, "app": app}
    return zm_post("/v2/exports/accounts/third-party", body)

def zm_export_mailboxes(apps, mailbox_ids=None, contains=None):
    body = {"apps": apps}
    if mailbox_ids:
        body["ids"] = mailbox_ids
    if contains:
        body["contains"] = contains
    return zm_post("/v2/exports/mailboxes", body)


# ─── SMARTLEAD API ───

def sl_url(path):
    return f"{SMARTLEAD_API}{path}?api_key={SMARTLEAD_KEY}"

def sl_create_email_account(from_name, from_email, username, password,
                             smtp_host="smtp.gmail.com", smtp_port=465,
                             imap_host="imap.gmail.com", imap_port=993):
    body = {
        "from_name": from_name,
        "from_email": from_email,
        "user_name": username,
        "password": password,
        "smtp_host": smtp_host,
        "smtp_port": smtp_port,
        "smtp_port_type": "SSL",
        "imap_host": imap_host,
        "imap_port": imap_port,
        "imap_port_type": "SSL",
        "type": "SMTP",
        "max_email_per_day": EMAILS_PER_ACCOUNT_PER_DAY,
        "warmup_enabled": False
    }
    r = requests.post(sl_url("/email-accounts/save"), json=body, timeout=30)
    return r.json()

def sl_set_warmup(account_id):
    """Set warmup via internal save-warmup endpoint (full control over all settings)."""
    # First enable warmup via public API
    r = requests.post(sl_url(f"/email-accounts/{account_id}/warmup"),
                      json=GOOGLE_WARMUP, timeout=30)
    result = r.json()

    # Then configure all settings via internal API (rampup toggle, reply limit, etc.)
    if SMARTLEAD_JWT:
        headers = sl_internal_headers()
        # Get warmup key ID
        wd = requests.get(
            f"{SMARTLEAD_INTERNAL_API}/email-account/fetch-warmup-details-by-email-account-id/{account_id}",
            headers=headers, timeout=30
        )
        warmup_key = ""
        if wd.status_code == 200:
            warmup_key = wd.json().get("message", {}).get("warmup_key_id", "")

        if warmup_key:
            body = {
                "emailAccountId": str(account_id),
                "maxEmailPerDay": 15,
                "isRampupEnabled": True,
                "rampupValue": 5,
                "warmupMinCount": 10,
                "warmupMaxCount": 15,
                "replyRate": 40,
                "dailyReplyLimit": 15,
                "autoAdjustWarmup": False,
                "sendWarmupsOnlyOnWeekdays": False,
                "useCustomDomain": False,
                "status": "ACTIVE",
                "warmupKeyId": warmup_key
            }
            r2 = requests.post(
                f"{SMARTLEAD_INTERNAL_API}/email-account/save-warmup",
                headers=headers, json=body, timeout=30
            )
            if r2.status_code == 200:
                result["full_config"] = True

    return result


# ─── SMARTLEAD INTERNAL API (tags, requires JWT) ───

def sl_internal_headers():
    return {
        "Authorization": f"Bearer {SMARTLEAD_JWT}",
        "Content-Type": "application/json"
    }

def sl_gql(query, variables=None):
    """Execute a GraphQL query against SmartLead's Hasura endpoint."""
    body = {"query": query}
    if variables:
        body["variables"] = variables
    r = requests.post(SMARTLEAD_GQL, headers=sl_internal_headers(), json=body, timeout=30)
    return r.json()

def sl_get_all_tags():
    """Get all tags from SmartLead via GraphQL. Returns {name: {id, name, color}}."""
    result = sl_gql("{ tags { id name color } }")
    tags = result.get("data", {}).get("tags", [])
    return {t["name"]: t for t in tags}

def sl_create_tag(name, color="#D0FCB1"):
    """Create a new tag via GraphQL. Returns the tag dict with id."""
    mutation = """mutation createTag($object: tags_insert_input!) {
      insert_tags_one(object: $object) { id name color }
    }"""
    result = sl_gql(mutation, {"object": {"name": name, "color": color}})
    return result.get("data", {}).get("insert_tags_one", {})

def sl_find_or_create_tag(name, color="#D0FCB1", existing_tags=None):
    """Find an existing tag by name or create a new one. Returns tag ID."""
    if existing_tags is None:
        existing_tags = sl_get_all_tags()
    if name in existing_tags:
        return existing_tags[name]["id"]
    tag = sl_create_tag(name, color)
    tag_id = tag.get("id")
    if tag_id:
        log(f"  Created new tag: '{name}' (ID: {tag_id})")
    return tag_id

def sl_tag_account(account_id, tag_ids, client_id=None):
    """Apply tags to an email account via the internal save-management-details endpoint."""
    body = {"id": account_id, "tags": tag_ids, "clientId": client_id}
    r = requests.post(f"{SMARTLEAD_INTERNAL_API}/email-account/save-management-details",
                      headers=sl_internal_headers(), json=body, timeout=30)
    return r.json()

def sl_tag_accounts_bulk(account_ids, tag_ids, client_id=None):
    """Apply the same tags to multiple accounts. Returns (success_count, fail_count)."""
    success = 0
    fail = 0
    for acc_id in account_ids:
        result = sl_tag_account(acc_id, tag_ids, client_id)
        if result.get("ok"):
            success += 1
        else:
            fail += 1
            log(f"  Tag failed for {acc_id}: {result}", "WARN")
    return success, fail

def sl_list_accounts(offset=0, limit=100):
    url = f"{SMARTLEAD_API}/email-accounts/?api_key={SMARTLEAD_KEY}&offset={offset}&limit={limit}"
    r = requests.get(url, timeout=30)
    return r.json()

def sl_get_account(account_id):
    url = f"{SMARTLEAD_API}/email-accounts/{account_id}/?api_key={SMARTLEAD_KEY}"
    r = requests.get(url, timeout=30)
    return r.json()

def sl_update_account(account_id, data):
    r = requests.post(sl_url(f"/email-accounts/{account_id}"), json=data, timeout=30)
    return r.json()


def sl_verify_warmup(account_id):
    """Verify warmup settings via internal API. Returns (ok, issues_dict)."""
    if not SMARTLEAD_JWT:
        return True, {}
    headers = sl_internal_headers()
    r = requests.get(
        f"{SMARTLEAD_INTERNAL_API}/email-account/fetch-warmup-details-by-email-account-id/{account_id}",
        headers=headers, timeout=30
    )
    if r.status_code != 200:
        return False, {"error": f"HTTP {r.status_code}"}
    data = r.json().get("message", {})
    checks = {
        "is_rampup_enabled": data.get("is_rampup_enabled") == True,
        "rampup_value": data.get("rampup_value") == 5,
        "daily_reply_limit": data.get("daily_reply_limit") == 15,
        "reply_rate": data.get("reply_rate") == 40,
        "status": data.get("status") == "ACTIVE",
    }
    issues = {k: data.get(k.replace("is_rampup", "is_rampup").replace("_enabled", "_enabled"))
              for k, v in checks.items() if not v}
    all_ok = all(checks.values())
    return all_ok, issues


# ─── ZAPMAIL HELPERS ───

# Standard 3 inbox usernames per domain (same names across all domains)
INBOX_SPECS = [
    {"firstName": "Sean", "lastName": "Reynolds", "mailboxUsername": "s.reynolds"},
    {"firstName": "Sean", "lastName": "Reynolds", "mailboxUsername": "sean.r"},
    {"firstName": "Sean", "lastName": "Reynolds", "mailboxUsername": "sean.reynolds"},
]

def generate_mailbox_specs(domain_name, count=3, offset=0):
    """Return the standard 3 inbox specs for a domain."""
    return INBOX_SPECS[:count]


def zm_get_workspace_id():
    """Get the primary workspace ID."""
    result = zm_list_workspaces()
    if isinstance(result, dict) and "data" in result:
        data = result["data"]
        if isinstance(data, dict) and "currentWorkspace" in data:
            return data["currentWorkspace"]["id"]
        if isinstance(data, list) and data:
            return data[0].get("id", "")
    return ""


def zm_find_domain(domain_name, workspace_key=None):
    """Find a domain by name in Zapmail. Returns domain dict or None."""
    all_domains = zm_list_domains(workspace_key)
    for d in all_domains:
        if d.get("domain") == domain_name:
            return d
    return None


def zm_wait_for_domains_active(domain_names, workspace_key=None, timeout_minutes=120, poll_interval=30):
    """Poll connect-domain endpoint + domain list until all domains are ACTIVE.
    Returns dict of {domain_name: domain_dict} for domains that went ACTIVE.
    """
    start = time.time()
    deadline = start + (timeout_minutes * 60)
    attempt = 0
    active_domains = {}
    remaining = set(domain_names)

    while remaining and time.time() < deadline:
        attempt += 1
        elapsed = int(time.time() - start)

        # Re-poll via connect-domain endpoint (triggers Zapmail to re-check DNS)
        connect_result = zm_connect_domains(list(remaining), workspace_key)
        connect_statuses = {}
        if isinstance(connect_result, dict) and "data" in connect_result:
            connect_statuses = connect_result.get("data", {}).get("domains", {})

        # Also check the domain list for ACTIVE status
        all_domains = zm_list_domains(workspace_key)
        domain_map = {d.get("domain", ""): d for d in all_domains}

        newly_active = []
        for dn in list(remaining):
            # Check domain list
            if dn in domain_map and domain_map[dn].get("status") == "ACTIVE":
                active_domains[dn] = domain_map[dn]
                newly_active.append(dn)
                remaining.discard(dn)
                continue

            # Log connect-domain status
            cs = connect_statuses.get(dn, "NOT_IN_RESPONSE")
            if cs == "SUCCESS":
                # Re-fetch to get domain ID
                d = zm_find_domain(dn, workspace_key)
                if d:
                    active_domains[dn] = d
                    newly_active.append(dn)
                    remaining.discard(dn)

        if newly_active:
            log(f"  [{elapsed}s] Now ACTIVE: {', '.join(newly_active)}")

        if remaining:
            statuses = {dn: connect_statuses.get(dn, "UNKNOWN") for dn in remaining}
            log(f"  [{elapsed}s] Waiting (check #{attempt}): {statuses}")
            time.sleep(poll_interval)

    if remaining:
        log(f"  Timed out waiting for: {', '.join(remaining)}", "ERROR")

    return active_domains


# ─── DOMAIN NAME GENERATION (fallback if AI finder unavailable) ───

def generate_domain_names(keywords, count, tlds=None):
    """Generate plausible domain names from keywords."""
    if tlds is None:
        tlds = [".com", ".co", ".net", ".org", ".io"]

    prefixes = ["my", "go", "the", "get", "pro", "top", "best", "prime", "elite", "all"]
    suffixes = ["hub", "now", "pro", "hq", "co", "zone", "team", "crew", "works", "group",
                "pros", "experts", "solutions", "services", "local"]

    names = set()
    for kw in keywords:
        kw = kw.lower().strip().replace(" ", "")
        # keyword + suffix
        for s in suffixes:
            for tld in tlds:
                names.add(f"{kw}{s}{tld}")
        # prefix + keyword
        for p in prefixes:
            for tld in tlds:
                names.add(f"{p}{kw}{tld}")
        # two keywords combined
        for kw2 in keywords:
            kw2 = kw2.lower().strip().replace(" ", "")
            if kw != kw2:
                for tld in tlds:
                    names.add(f"{kw}{kw2}{tld}")

    return list(names)[:count * 3]  # generate 3x to account for unavailable ones


# ─── CSV EXPORT ───

def export_for_sheet(config, output_path=None):
    if not output_path:
        name = config["client_name"].lower().replace(" ", "_")
        output_path = SCRIPT_DIR / "exports" / f"{name}_{datetime.now().strftime('%Y%m%d')}.csv"

    rows = []
    for domain_data in config.get("purchased_domains", []):
        domain = domain_data["domain"]
        # Handle both new format (specs with firstName/lastName) and email list
        inboxes = domain_data.get("inboxes", [])
        emails = domain_data.get("inbox_emails", [])

        for i, inbox in enumerate(inboxes):
            email = emails[i] if i < len(emails) else f"{inbox.get('mailboxUsername', '')}@{domain}"
            rows.append({
                "Domain": domain,
                "Email": email,
                "First Name": inbox.get("firstName", inbox.get("first_name", "")),
                "Last Name": inbox.get("lastName", inbox.get("last_name", "")),
                "Client": config["client_name"],
                "Vendor": "Zapmail",
                "Status": "In use",
                "Warmup Start": config["infrastructure"]["warmup_start_date"],
                "Launch Date": config["infrastructure"]["estimated_launch_date"],
                "Notes": ""
            })

    if rows:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        log(f"Sheet export saved: {output_path}")
    return output_path, rows


# ─── MAIN PIPELINE ───

TOTAL_STEPS = 11

# Google Calendar config for rotation reminders
GCAL_CALENDAR_ID = "c_86c4f6b9ef436cb6e4570df1d4d445331d2453ccf357935df8503209023cd58a@group.calendar.google.com"
GCAL_ROTATION_WEEKS = 6  # weeks from warmup start to schedule infra rotation
GOOGLE_CLIENT_ID = ENV.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = ENV.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REFRESH_TOKEN = ENV.get("GOOGLE_REFRESH_TOKEN", "")

def run_pipeline(config, config_path):
    """Execute the full infrastructure setup pipeline."""

    completed = config.get("steps_completed", [])
    client = config["client_name"]
    infra = config["infrastructure"]

    # ── STEP 1: Validate APIs ──
    if "validate_apis" not in completed:
        log_step(1, TOTAL_STEPS, "VALIDATE API CONNECTIONS")

        errors = []
        warnings = []

        # Check domain registrars
        configured_registrars = get_configured_registrars()
        if not configured_registrars:
            errors.append("No domain registrar API keys configured in .env (need at least one: Porkbun, Spaceship)")
        else:
            for reg in configured_registrars:
                try:
                    if reg.ping():
                        log(f"{reg.name.title()} OK")
                    else:
                        warnings.append(f"{reg.name.title()}: ping failed (keys may be wrong)")
                except Exception as e:
                    warnings.append(f"{reg.name.title()} connection failed: {e}")

        # Check Zapmail
        if ZAPMAIL_KEY:
            try:
                result = zm_list_workspaces()
                if isinstance(result, list) or isinstance(result, dict):
                    log(f"Zapmail OK — Response received")
                    config["zapmail_workspaces"] = result
                else:
                    log(f"Zapmail response: {result}", "WARN")
            except Exception as e:
                errors.append(f"Zapmail connection failed: {e}")
        else:
            errors.append("Zapmail API key missing in .env")

        # Check SmartLead
        if SMARTLEAD_KEY:
            try:
                result = sl_list_accounts(limit=1)
                log(f"SmartLead OK — Response received")
            except Exception as e:
                errors.append(f"SmartLead connection failed: {e}")
        else:
            errors.append("SmartLead API key missing in .env")

        for w in warnings:
            log(w, "WARN")

        if errors:
            for e in errors:
                log(e, "ERROR")
            print("\nFix the above errors in .env and re-run.")
            save_config(config, config_path)
            return False

        config["configured_registrars"] = [r.name for r in configured_registrars]
        completed.append("validate_apis")
        config["steps_completed"] = completed
        save_config(config, config_path)

    # ── STEP 2: Pull Available Domains from Sheet ──
    if "claim_domains" not in completed:
        log_step(2, TOTAL_STEPS, "PULL AVAILABLE DOMAINS FROM SHEET")

        domains_needed = infra["domains_needed"]

        # Read available generic domains from the master sheet
        available = sheets.get_available_domains()
        log(f"Found {len(available)} available generic domains in sheet")
        log(f"Need {domains_needed} domains for {client}")

        # Cross-check: exclude domains that already exist in Zapmail with active mailboxes
        workspace_id = config.get("workspace_id") or zm_get_workspace_id()
        config["workspace_id"] = workspace_id
        existing_zm = zm_list_domains(workspace_id)
        if isinstance(existing_zm, dict):
            existing_zm = existing_zm.get("data", existing_zm.get("domains", []))
        existing_zm_domains = set()
        for d in existing_zm:
            name = d.get("domain", "")
            mbs = d.get("mailboxes", [])
            if isinstance(mbs, list) and len(mbs) > 0:
                existing_zm_domains.add(name)
        if existing_zm_domains:
            before = len(available)
            available = [d for d in available if d["domain"] not in existing_zm_domains]
            skipped = before - len(available)
            if skipped:
                log(f"Excluded {skipped} domains already active in Zapmail (belong to other clients)")

        # Account for domains we already have from a prior partial run
        existing_domains = config.get("purchased_domains", [])
        still_needed = domains_needed - len(existing_domains)

        if still_needed <= 0:
            log(f"Already have {len(existing_domains)} domains — no more needed")
        elif len(available) >= still_needed:
            selected = available[:still_needed]

            log(f"Claiming {still_needed} new domains for {client}:")
            for i, d in enumerate(selected, 1):
                log(f"  {i}. {d['domain']} ({d['provider']})")

            log("Marking domains as 'In use' in master sheet...")
            sheets.mark_domains_in_use_batch(selected, client)
            log(f"Claimed {len(selected)} domains for {client}")

            new_domains = [
                {"domain": d["domain"], "provider": d["provider"], "row_number": d["row_number"]}
                for d in selected
            ]
            config["purchased_domains"] = existing_domains + new_domains
        else:
            shortfall = still_needed - len(available)
            log(f"Only {len(available)} available, need {shortfall} more", "WARN")

            if available:
                log(f"Claiming {len(available)} available domains (need {shortfall} more):")
                for i, d in enumerate(available, 1):
                    log(f"  {i}. {d['domain']} ({d['provider']})")

                sheets.mark_domains_in_use_batch(available, client)
                new_domains = [
                    {"domain": d["domain"], "provider": d["provider"], "row_number": d["row_number"]}
                    for d in available
                ]
                config["purchased_domains"] = existing_domains + new_domains

            config["domains_to_buy"] = shortfall
            log(f"You need to purchase {shortfall} more domains.")
            log("Buy them on whichever provider has the best sale, add to the sheet as 'Available', then re-run.")
            save_config(config, config_path)
            return False

        # Update the client tab in the sheet
        log("Updating client tab in sheet...")
        sheets.setup_client_tab(client, config["purchased_domains"])
        log(f"Client tab '{client}' updated")

        completed.append("claim_domains")
        config["steps_completed"] = completed
        save_config(config, config_path)

    # ── STEP 3: Set CloudNS Nameservers (per provider) ──
    if "set_nameservers" not in completed:
        log_step(3, TOTAL_STEPS, "SET CLOUDNS NAMESERVERS")

        for d in config["purchased_domains"]:
            domain = d["domain"]
            provider = d.get("provider", config.get("purchase_provider", "porkbun"))

            # Retry with backoff for rate limits
            max_retries = 3
            for attempt in range(max_retries):
                success, msg = set_nameservers_for_domain(domain, provider)
                if success:
                    log(f"  NS set: {domain} → CloudNS ({provider})")
                    d["nameservers_set"] = True
                    break
                elif "rate" in msg.lower() or "429" in msg:
                    wait = 60 * (attempt + 1)
                    log(f"  Rate limited on {domain}, waiting {wait}s...", "WARN")
                    time.sleep(wait)
                else:
                    log(f"  NS failed: {domain} ({provider}) — {msg}", "WARN")
                    d["nameservers_set"] = False
                    break
            time.sleep(0.5)

        # --- Verification gate: retry any failures ---
        failed = [d for d in config["purchased_domains"] if not d.get("nameservers_set")]
        if failed:
            log(f"{len(failed)} domain(s) failed NS — running retry pass...")
            for d in failed:
                domain = d["domain"]
                provider = d.get("provider", config.get("purchase_provider", "porkbun"))
                for attempt in range(3):
                    time.sleep(5 * (attempt + 1))
                    success, msg = set_nameservers_for_domain(domain, provider)
                    if success:
                        log(f"  Retry OK: {domain}")
                        d["nameservers_set"] = True
                        break
                    log(f"  Retry {attempt+1} failed: {domain} — {msg}", "WARN")

        still_failed = [d["domain"] for d in config["purchased_domains"] if not d.get("nameservers_set")]
        if still_failed:
            log(f"BLOCKED: {len(still_failed)} domain(s) cannot set NS: {', '.join(still_failed)}", "ERROR")
            log("Fix manually and re-run the pipeline.", "ERROR")
            save_config(config, config_path)
            return False

        completed.append("set_nameservers")
        config["steps_completed"] = completed
        save_config(config, config_path)

    # ── STEP 4: Upload Domains to Zapmail ──
    if "zapmail_domains" not in completed:
        log_step(4, TOTAL_STEPS, "UPLOAD DOMAINS TO ZAPMAIL")

        workspace_id = config.get("workspace_id") or zm_get_workspace_id()
        config["workspace_id"] = workspace_id

        # Check which domains already exist in Zapmail
        existing = zm_list_domains(workspace_id)
        existing_map = {d.get("domain", ""): d for d in existing}

        domains_to_connect = []
        for d in config["purchased_domains"]:
            domain = d["domain"]
            if domain in existing_map:
                log(f"  Already in Zapmail: {domain} (status: {existing_map[domain].get('status')})")
                d["zapmail_domain_id"] = existing_map[domain].get("id")
                d["zapmail_connected"] = True
            else:
                domains_to_connect.append(domain)

        # Connect existing domains (NOT purchase — they're already on Spaceship)
        if domains_to_connect:
            log(f"Uploading {len(domains_to_connect)} domain(s) to Zapmail...")
            result = zm_connect_domains(domains_to_connect, workspace_key=workspace_id)
            log(f"  Connect result: {json.dumps(result)[:300]}")

            # --- Verification: poll until all domains appear in Zapmail ---
            remaining = set(domains_to_connect)
            max_polls = 20  # 20 * 15s = 5 minutes
            for poll in range(max_polls):
                time.sleep(15)
                current = zm_list_domains(workspace_id)
                current_names = {d.get("domain", "") for d in current}
                newly_found = remaining & current_names
                if newly_found:
                    for dn in newly_found:
                        for cd in current:
                            if cd.get("domain") == dn:
                                for pd in config["purchased_domains"]:
                                    if pd["domain"] == dn:
                                        pd["zapmail_domain_id"] = cd.get("id")
                                        pd["zapmail_connected"] = True
                    remaining -= newly_found
                    log(f"  Confirmed in Zapmail: {', '.join(newly_found)}")
                if not remaining:
                    break
                # Re-trigger connect for stragglers
                zm_connect_domains(list(remaining), workspace_key=workspace_id)
                log(f"  Poll {poll+1}: waiting for {len(remaining)} domain(s)...")

            if remaining:
                log(f"BLOCKED: {len(remaining)} domain(s) never appeared in Zapmail: {', '.join(remaining)}", "ERROR")
                log("Check DNS settings and re-run the pipeline.", "ERROR")
                save_config(config, config_path)
                return False

        completed.append("zapmail_domains")
        config["steps_completed"] = completed
        save_config(config, config_path)

    # ── STEP 5: Wait for DNS + Create Inboxes ──
    if "zapmail_inboxes" not in completed:
        log_step(5, TOTAL_STEPS, "WAIT FOR DNS PROPAGATION + CREATE INBOXES")

        workspace_id = config.get("workspace_id", "")
        domain_names = [d["domain"] for d in config["purchased_domains"]]

        # Wait for all domains to go ACTIVE (polls connect-domain endpoint)
        log(f"Waiting for {len(domain_names)} domain(s) to go ACTIVE...")
        active_map = zm_wait_for_domains_active(domain_names, workspace_id,
                                                  timeout_minutes=120, poll_interval=30)

        # Check which domains already have inboxes (from domain list data)
        existing_by_domain = {}
        all_zm_domains = zm_list_domains(workspace_id)
        if isinstance(all_zm_domains, dict):
            all_zm_domains = all_zm_domains.get("data", all_zm_domains.get("domains", []))
        for zd in all_zm_domains:
            zd_name = zd.get("domain", "")
            zd_mbs = zd.get("mailboxes", [])
            if isinstance(zd_mbs, list) and len(zd_mbs) > 0:
                existing_by_domain[zd_name] = zd_mbs

        # Only buy slots for domains that don't already have inboxes
        domains_needing_inboxes = [d["domain"] for d in config["purchased_domains"]
                                   if len(existing_by_domain.get(d["domain"], [])) < ACCOUNTS_PER_DOMAIN]
        new_inboxes_needed = len(domains_needing_inboxes) * ACCOUNTS_PER_DOMAIN

        if new_inboxes_needed > 0:
            # Check how many unassigned slots we already have (always fetch fresh)
            total_purchased = 0
            total_assigned = 0
            ws_result = zm_get("/v2/workspaces", workspace_id)
            if isinstance(ws_result, dict):
                ws_data = ws_result.get("data", {}).get("currentWorkspace", {})
                if ws_data:
                    total_purchased = int(ws_data.get("totalMailboxesPurchasedGoogle", "0"))
                    total_assigned = int(ws_data.get("assignedMailboxesCountGoogle", "0"))

            unassigned = total_purchased - total_assigned
            slots_to_buy = max(0, new_inboxes_needed - unassigned)

            if slots_to_buy <= 0:
                log(f"Have {unassigned} unassigned mailbox slots — enough for {new_inboxes_needed} new inboxes")
            else:
                log(f"Need {slots_to_buy} more slots ({unassigned} unassigned, {new_inboxes_needed} needed)")
                buy_result = zm_buy_addon_mailboxes(slots_to_buy, workspace_id)
                if isinstance(buy_result, dict) and buy_result.get("status") == 400:
                    msg = buy_result.get("message", "")
                    if "Insufficient wallet balance" in msg:
                        log(f"  {msg}", "ERROR")
                        log("  Please add funds to your Zapmail wallet and re-run the pipeline.", "ERROR")
                        sys.exit(1)
                    else:
                        log(f"  Mailbox purchase issue: {msg}", "WARN")
                else:
                    log(f"  Mailbox slots: {json.dumps(buy_result)[:200]}")
        else:
            log("All domains already have inboxes — no new slots needed")

        # Create inboxes on active domains (skip if already exist)
        for d in config["purchased_domains"]:
            domain_name = d["domain"]
            if domain_name not in active_map:
                log(f"  {domain_name} not ACTIVE — skipping inbox creation", "ERROR")
                d["inboxes"] = []
                continue

            domain_info = active_map[domain_name]
            domain_id = domain_info.get("id")
            d["zapmail_domain_id"] = domain_id

            # Check if inboxes already exist for this domain
            existing = existing_by_domain.get(domain_name, [])
            if len(existing) >= ACCOUNTS_PER_DOMAIN:
                log(f"  {domain_name} already has {len(existing)} inboxes — skipping creation")
                d["inbox_emails"] = []
                for mb in existing[:ACCOUNTS_PER_DOMAIN]:
                    username = mb.get("username", mb.get("email", "").split("@")[0])
                    d["inbox_emails"].append(f"{username}@{domain_name}")
                d["inboxes"] = [{"mailboxUsername": e.split("@")[0]} for e in d["inbox_emails"]]
                continue

            log(f"  {domain_name} is ACTIVE — creating {ACCOUNTS_PER_DOMAIN} inboxes...")
            specs = generate_mailbox_specs(domain_name, ACCOUNTS_PER_DOMAIN)

            max_retries = 5
            retry_delay = 15  # seconds
            created = False
            for attempt in range(1, max_retries + 1):
                result = zm_create_mailboxes(domain_id, domain_name, specs, workspace_id)
                log(f"  Create result: {json.dumps(result)[:300]}")

                if isinstance(result, dict) and result.get("status") not in (400, 422, 500):
                    d["inboxes"] = specs
                    d["inbox_emails"] = [f"{s['mailboxUsername']}@{domain_name}" for s in specs]
                    log(f"  Created: {', '.join(d['inbox_emails'])}")
                    created = True
                    break
                else:
                    msg = result.get("message", "") if isinstance(result, dict) else str(result)
                    if attempt < max_retries and ("don't have enough mailboxes" in msg or "not enough mailboxes" in msg.lower()):
                        log(f"  Mailbox slots not ready yet — retrying in {retry_delay}s (attempt {attempt}/{max_retries})...", "WARN")
                        time.sleep(retry_delay)
                    elif attempt < max_retries:
                        log(f"  Inbox creation failed — retrying in {retry_delay}s (attempt {attempt}/{max_retries})...", "WARN")
                        time.sleep(retry_delay)
                    else:
                        log(f"  Inbox creation failed after {max_retries} attempts: {json.dumps(result)[:200]}", "ERROR")

            if not created:
                d["inboxes"] = []

            time.sleep(1)

        completed.append("zapmail_inboxes")
        config["steps_completed"] = completed
        save_config(config, config_path)

    # ── STEP 6: Set Forwarding Domain + Tag in Zapmail ──
    if "zapmail_config" not in completed:
        log_step(6, TOTAL_STEPS, "CONFIGURE FORWARDING + TAGS IN ZAPMAIL")

        workspace_id = config.get("workspace_id", "")
        forwarding = config.get("forwarding_domain", "")

        # Only operate on domains that have inboxes (i.e. were actually set up by this pipeline)
        domains_with_inboxes = [d for d in config["purchased_domains"]
                                if d.get("zapmail_domain_id") and len(d.get("inboxes", [])) > 0]
        domain_ids = [d["zapmail_domain_id"] for d in domains_with_inboxes]
        log(f"Operating on {len(domain_ids)} domain(s) with inboxes")

        # Set forwarding on our domains only
        if forwarding and domain_ids:
            fwd_result = zm_set_forwarding(domain_ids, forwarding, workspace_id)
            log(f"Forwarding set on {len(domain_ids)} domain(s) → {forwarding}")
            log(f"  Result: {json.dumps(fwd_result)[:200]}")

        # Find existing tag (fuzzy match) or create new one
        tag_id = None
        tags_result = zm_list_domain_tags(workspace_id)
        tag_list = tags_result.get("data", []) if isinstance(tags_result, dict) else []

        # Score each tag: exact > contains (length-weighted) > word overlap
        client_lower = client.lower().strip()
        client_words = set(client_lower.split())
        best_match = None
        best_score = 0
        for t in tag_list:
            tag_name = t.get("name", "").lower().strip()
            tag_words = set(tag_name.split())
            if tag_name == client_lower:
                # Exact match
                best_match = t
                best_score = 100
                break
            elif client_lower in tag_name or tag_name in client_lower:
                # Contains match — score higher when lengths are closer
                shorter = min(len(client_lower), len(tag_name))
                longer = max(len(client_lower), len(tag_name))
                score = 70 + (shorter / longer) * 25  # 70-95 range
                if score > best_score:
                    best_match = t
                    best_score = score
            else:
                # Word overlap — require majority of client words to match
                overlap = len(client_words & tag_words)
                if overlap > 0:
                    score = (overlap / len(client_words)) * 50  # based on client words covered
                    if score > best_score:
                        best_match = t
                        best_score = score

        if best_match and best_score >= 40:
            tag_id = best_match.get("id")
            log(f"Matched existing tag: '{best_match.get('name')}' (score: {best_score}, ID: {tag_id})")

        if not tag_id:
            tag_result = zm_create_domain_tag(client, workspace_key=workspace_id)
            if isinstance(tag_result, dict):
                data = tag_result.get("data", {})
                if isinstance(data, dict) and "tagIds" in data:
                    tag_id = data["tagIds"][0] if data["tagIds"] else None
                if tag_id:
                    log(f"Created new tag: {client} (ID: {tag_id})")

        # Assign tag to all domains
        if tag_id and domain_ids:
            assign_result = zm_assign_domain_tag(domain_ids, [tag_id], workspace_id)
            log(f"Tagged {len(domain_ids)} domain(s) with '{client}'")
            log(f"  Result: {json.dumps(assign_result)[:200]}")

        completed.append("zapmail_config")
        config["steps_completed"] = completed
        save_config(config, config_path)

    # ── STEP 7: Export to SmartLead ──
    if "smartlead_export" not in completed:
        log_step(7, TOTAL_STEPS, "EXPORT INBOXES TO SMARTLEAD")

        our_domains = [d["domain"] for d in config["purchased_domains"]]
        our_domain_set = set(our_domains)
        workspace_id = config.get("workspace_id", "")
        expected_total = len(our_domains) * ACCOUNTS_PER_DOMAIN

        # --- Wait for all mailboxes to finish provisioning in Zapmail ---
        log("Waiting for all mailboxes to reach ACTIVE status in Zapmail...")
        max_provision_polls = 48  # 48 * 5min = 4 hours max
        provision_poll_interval = 300  # 5 minutes

        for poll in range(max_provision_polls):
            all_zm_domains = zm_list_domains(workspace_id)
            in_progress = []
            active_count = 0
            for d in all_zm_domains:
                if d.get("domain") not in our_domain_set:
                    continue
                for mb in d.get("mailboxes", []):
                    if mb.get("status") == "ACTIVE":
                        active_count += 1
                    else:
                        in_progress.append(f"{mb.get('username', '?')}@{d.get('domain', '?')}")

            if not in_progress:
                log(f"  All {active_count}/{expected_total} mailboxes are ACTIVE!")
                break

            log(f"  Poll {poll+1}/{max_provision_polls}: {active_count}/{expected_total} ACTIVE, {len(in_progress)} still provisioning")
            if poll < max_provision_polls - 1:
                log(f"  Next check in {provision_poll_interval // 60} minutes...")
                time.sleep(provision_poll_interval)
        else:
            log(f"WARNING: {len(in_progress)} mailboxes still not ACTIVE after {max_provision_polls * provision_poll_interval // 3600}h. Proceeding with export anyway.", "WARN")
            log(f"  Still in progress: {', '.join(in_progress[:10])}", "WARN")

        # --- Verify DNS propagation before exporting ---
        log("Verifying DNS propagation (SPF, DKIM, DMARC) before SmartLead export...")
        max_dns_polls = 12  # 12 * 60s = 12 minutes max
        dns_poll_interval = 60  # 1 minute

        for dns_poll in range(max_dns_polls):
            dns_failures = []
            for domain_name in our_domains:
                missing = []
                # Check SPF
                try:
                    result = subprocess.run(
                        ["dig", "+short", "TXT", domain_name],
                        capture_output=True, text=True, timeout=10
                    )
                    if "v=spf1" not in result.stdout:
                        missing.append("SPF")
                except Exception:
                    missing.append("SPF")

                # Check DMARC
                try:
                    result = subprocess.run(
                        ["dig", "+short", "TXT", f"_dmarc.{domain_name}"],
                        capture_output=True, text=True, timeout=10
                    )
                    if "v=DMARC1" not in result.stdout:
                        missing.append("DMARC")
                except Exception:
                    missing.append("DMARC")

                # Check DKIM (google selector)
                try:
                    result = subprocess.run(
                        ["dig", "+short", "TXT", f"google._domainkey.{domain_name}"],
                        capture_output=True, text=True, timeout=10
                    )
                    if "v=DKIM1" not in result.stdout:
                        missing.append("DKIM")
                except Exception:
                    missing.append("DKIM")

                if missing:
                    dns_failures.append(f"{domain_name} (missing: {', '.join(missing)})")

            if not dns_failures:
                log(f"  All {len(our_domains)} domains have SPF, DKIM, and DMARC records resolving!")
                break

            log(f"  DNS poll {dns_poll+1}/{max_dns_polls}: {len(dns_failures)}/{len(our_domains)} domains still missing records")
            for f in dns_failures:
                log(f"    {f}")
            if dns_poll < max_dns_polls - 1:
                log(f"  Waiting {dns_poll_interval}s for DNS propagation...")
                time.sleep(dns_poll_interval)
        else:
            log(f"WARNING: {len(dns_failures)} domains still missing DNS records after {max_dns_polls} minutes. Proceeding anyway.", "WARN")

        # --- Export to SmartLead ---
        log("Exporting mailboxes to SmartLead...")

        # Collect all mailbox IDs across all domains
        all_mailbox_ids = []
        for d in config["purchased_domains"]:
            domain_id = d.get("zapmail_domain_id")
            if domain_id:
                mailboxes = zm_list_mailboxes(workspace_id, domain_id)
                if isinstance(mailboxes, dict) and "data" in mailboxes:
                    for mb in mailboxes["data"].get("mailboxes", mailboxes["data"] if isinstance(mailboxes["data"], list) else []):
                        mb_id = mb.get("id")
                        if mb_id:
                            all_mailbox_ids.append(mb_id)

        if all_mailbox_ids:
            log(f"Bulk exporting {len(all_mailbox_ids)} mailboxes to SmartLead...")
            result = zm_export_mailboxes(apps=["SMARTLEAD"], mailbox_ids=all_mailbox_ids)
            log(f"  Export result: {json.dumps(result)[:300]}")
        else:
            # Fallback: export by domain name
            log("No mailbox IDs found, falling back to per-domain export...")
            for domain_name in our_domains:
                result = zm_export_mailboxes(apps=["SMARTLEAD"], contains=domain_name)
                log(f"  Export {domain_name}: {json.dumps(result)[:200]}")
                time.sleep(2)

        config["smartlead_export_result"] = {"exported_domains": our_domains}

        log("Waiting 3 minutes for export to process...")
        time.sleep(180)

        # --- Verification: confirm at least some accounts appeared in SmartLead ---
        expected_total = len(our_domain_set) * ACCOUNTS_PER_DOMAIN
        found_count = 0
        max_polls = 6  # 6 * 5min = 30 minutes
        sl_poll_interval = 300  # 5 minutes

        for poll in range(max_polls):
            all_accounts = []
            offset = 0
            while True:
                batch = sl_list_accounts(offset=offset, limit=100)
                if isinstance(batch, list):
                    all_accounts.extend(batch)
                    if len(batch) < 100:
                        break
                    offset += 100
                else:
                    break

            found_count = sum(1 for acc in all_accounts
                              if acc.get("from_email", "").split("@")[-1] in our_domain_set)
            log(f"  SmartLead verification poll {poll+1}/{max_polls}: {found_count}/{expected_total} accounts found")
            if found_count >= expected_total:
                log(f"  All {expected_total} accounts confirmed in SmartLead!")
                break
            elif found_count > 0:
                log(f"  {found_count} accounts found so far — continuing to wait for remaining...")
            if poll < max_polls - 1:
                log(f"  Next check in {sl_poll_interval // 60} minutes...")
                time.sleep(sl_poll_interval)

        if found_count == 0:
            log("No accounts appeared in SmartLead yet. Re-exporting...", "WARN")
            for domain_name in our_domains:
                result = zm_export_mailboxes(apps=["SMARTLEAD"], contains=domain_name)
                log(f"  Re-export {domain_name}: {json.dumps(result)[:200]}")
                time.sleep(2)
            log("Waiting 5 minutes after re-export...")
            time.sleep(300)
            # Final check
            all_accounts = []
            offset = 0
            while True:
                batch = sl_list_accounts(offset=offset, limit=100)
                if isinstance(batch, list):
                    all_accounts.extend(batch)
                    if len(batch) < 100:
                        break
                    offset += 100
                else:
                    break
            found_count = sum(1 for acc in all_accounts
                              if acc.get("from_email", "").split("@")[-1] in our_domain_set)
            log(f"  After re-export: {found_count}/{expected_total} accounts in SmartLead")
            if found_count == 0:
                log("BLOCKED: Still zero accounts after re-export. Check Zapmail SmartLead integration and re-run.", "ERROR")
                save_config(config, config_path)
                return False

        log(f"Export confirmed: {found_count} account(s) visible in SmartLead (full sync in Step 9)")
        config["smartlead_export_result"]["initial_count"] = found_count

        completed.append("smartlead_export")
        config["steps_completed"] = completed
        save_config(config, config_path)

    # ── STEP 9: Configure Warmup in SmartLead ──
    if "smartlead_warmup" not in completed:
        log_step(8, TOTAL_STEPS, "CONFIGURE WARMUP IN SMARTLEAD")

        our_domains = {d["domain"] for d in config["purchased_domains"]}
        expected_total = len(our_domains) * ACCOUNTS_PER_DOMAIN
        log(f"Expecting {expected_total} accounts across {len(our_domains)} domains")

        warmup_done_emails = set()
        max_retries = 10  # max re-export cycles
        poll_interval = 30  # seconds between polls
        polls_before_reexport = 20  # 20 polls × 30s = 10 minutes

        for retry in range(max_retries):
            if retry > 0:
                log(f"\n  Re-export attempt {retry}: re-exporting missing domains to SmartLead...")
                # Find which domains are still missing accounts
                missing_domains = set()
                for domain_name in our_domains:
                    found = len([e for e in warmup_done_emails if e.endswith(f"@{domain_name}")])
                    if found < ACCOUNTS_PER_DOMAIN:
                        missing_domains.add(domain_name)
                for domain_name in missing_domains:
                    result = zm_export_mailboxes(apps=["SMARTLEAD"], contains=domain_name)
                    log(f"  Re-exported {domain_name}: {json.dumps(result)[:150]}")
                    time.sleep(2)
                log("  Waiting 15s for re-export to process...")
                time.sleep(15)

            # Poll until all accounts appear or 10 minutes pass
            for poll in range(polls_before_reexport):
                all_accounts = []
                offset = 0
                while True:
                    batch = sl_list_accounts(offset=offset, limit=100)
                    if isinstance(batch, list):
                        all_accounts.extend(batch)
                        if len(batch) < 100:
                            break
                        offset += 100
                    else:
                        log(f"  SmartLead list error: {batch}", "WARN")
                        break

                our_accounts = []
                for acc in all_accounts:
                    email = acc.get("from_email", acc.get("email", ""))
                    domain = email.split("@")[-1] if "@" in email else ""
                    if domain in our_domains:
                        our_accounts.append(acc)

                # Enable warmup on any new accounts we haven't processed yet
                for acc in our_accounts:
                    email = acc.get("from_email", acc.get("email", ""))
                    acc_id = acc.get("id")
                    if email not in warmup_done_emails and acc_id:
                        try:
                            sl_set_warmup(acc_id)
                            sl_update_account(acc_id, {"time_to_wait_in_mins": 5})
                            warmup_done_emails.add(email)
                            log(f"  Warmup ON + time_gap=5: {email}")
                        except Exception as e:
                            log(f"  Warmup error on {email}: {e}", "WARN")
                        time.sleep(0.3)

                log(f"  Poll {poll+1}: {len(warmup_done_emails)}/{expected_total} accounts warmed up")

                if len(warmup_done_emails) >= expected_total:
                    break
                time.sleep(poll_interval)

            if len(warmup_done_emails) >= expected_total:
                log(f"All {expected_total} accounts found and warmup enabled!")
                break
            else:
                log(f"  Only {len(warmup_done_emails)}/{expected_total} after 10 min — will re-export missing domains")

        log(f"Warmup configured on {len(warmup_done_emails)}/{expected_total} accounts")
        config["smartlead_accounts"] = list(warmup_done_emails)

        # --- Hard gate: all accounts must be present ---
        if len(warmup_done_emails) < expected_total:
            log(f"BLOCKED: Only {len(warmup_done_emails)}/{expected_total} accounts warmed up after all retries", "ERROR")
            log("Check Zapmail export and SmartLead, then re-run.", "ERROR")
            save_config(config, config_path)
            return False

        # --- Verification: confirm warmup settings + time_to_wait on ALL accounts ---
        log("Verifying warmup settings on all accounts...")
        all_accounts = []
        offset = 0
        while True:
            batch = sl_list_accounts(offset=offset, limit=100)
            if isinstance(batch, list):
                all_accounts.extend(batch)
                if len(batch) < 100:
                    break
                offset += 100
            else:
                break

        our_accounts = [acc for acc in all_accounts
                        if acc.get("from_email", "").split("@")[-1] in our_domains]

        bad_accounts = []
        for acc in our_accounts:
            acc_id = acc.get("id")
            email = acc.get("from_email", "")

            # Verify + fix time_to_wait_in_mins
            sl_update_account(acc_id, {"time_to_wait_in_mins": 5})

            # Verify internal warmup settings
            ok, issues = sl_verify_warmup(acc_id)
            if not ok:
                log(f"  Warmup wrong on {email}: {issues} — re-applying...", "WARN")
                sl_set_warmup(acc_id)
                time.sleep(1)
                ok2, issues2 = sl_verify_warmup(acc_id)
                if not ok2:
                    bad_accounts.append(email)
                    log(f"  STILL wrong after re-apply: {email} — {issues2}", "ERROR")
            time.sleep(0.3)

        if bad_accounts:
            log(f"BLOCKED: {len(bad_accounts)} account(s) have incorrect warmup: {', '.join(bad_accounts)}", "ERROR")
            save_config(config, config_path)
            return False

        log(f"All {len(our_accounts)} accounts verified: warmup correct, time_to_wait=5min")

        completed.append("smartlead_warmup")
        config["steps_completed"] = completed
        save_config(config, config_path)

    # ── STEP 10: Tag Accounts in SmartLead ──
    if "smartlead_tags" not in completed:
        log_step(9, TOTAL_STEPS, "TAG ACCOUNTS IN SMARTLEAD")

        if not SMARTLEAD_JWT:
            log("SMARTLEAD_JWT not set in .env — cannot tag accounts.", "ERROR")
            log("Add JWT from SmartLead browser dev tools to .env as SMARTLEAD_JWT=<token>")
            sys.exit(1)
        else:
            # Get all existing tags to avoid creating duplicates
            existing_tags = sl_get_all_tags()
            log(f"Found {len(existing_tags)} existing tags in SmartLead")

            # Date tag in M/D/YY format
            warmup_date = datetime.strptime(infra["warmup_start_date"], "%Y-%m-%d")
            date_tag_name = f"{warmup_date.month}/{warmup_date.day}/{warmup_date.strftime('%y')}"

            # Find or create the 3 tags: client name, "Zapmail", date
            tag_ids = []
            for tag_name, color in [(client, "#B1C4FC"), ("Zapmail", "#B1FCB3"), (date_tag_name, "#D0FCB1")]:
                tag_id = sl_find_or_create_tag(tag_name, color, existing_tags)
                if tag_id:
                    tag_ids.append(tag_id)
                    log(f"  Tag: '{tag_name}' → ID {tag_id}")
                else:
                    log(f"  Failed to find/create tag: '{tag_name}'", "WARN")

            # Get our account IDs from SmartLead
            our_domains = {d["domain"] for d in config["purchased_domains"]}
            our_account_ids = []
            offset = 0
            while True:
                batch = sl_list_accounts(offset=offset, limit=100)
                if isinstance(batch, list):
                    for acc in batch:
                        email = acc.get("from_email", acc.get("email", ""))
                        domain = email.split("@")[-1] if "@" in email else ""
                        if domain in our_domains:
                            our_account_ids.append(acc["id"])
                    if len(batch) < 100:
                        break
                    offset += 100
                else:
                    break

            if tag_ids and our_account_ids:
                log(f"Tagging {len(our_account_ids)} accounts with {len(tag_ids)} tags...")
                success, fail = sl_tag_accounts_bulk(our_account_ids, tag_ids)
                log(f"  Tagged: {success} success, {fail} failed")
            else:
                log(f"  Skipping: {len(tag_ids)} tags, {len(our_account_ids)} accounts", "WARN")

        completed.append("smartlead_tags")
        config["steps_completed"] = completed
        save_config(config, config_path)

    # ── STEP 11: Export CSV for Google Sheet ──
    if "export_csv" not in completed:
        log_step(10, TOTAL_STEPS, "FINAL SUMMARY")

        csv_path, rows = export_for_sheet(config)
        log(f"Exported {len(rows)} rows to {csv_path}")
        config["export_csv_path"] = str(csv_path)

        completed.append("export_csv")
        config["steps_completed"] = completed
        save_config(config, config_path)

    # ── STEP 12: Schedule Infrastructure Rotation in Google Calendar ──
    if "gcal_rotation" not in completed:
        log_step(11, TOTAL_STEPS, "SCHEDULE INFRA ROTATION REMINDER")

        warmup_start = datetime.strptime(infra["warmup_start_date"], "%Y-%m-%d")
        rotation_date = warmup_start + timedelta(weeks=GCAL_ROTATION_WEEKS)
        rotation_str = rotation_date.strftime("%Y-%m-%d")
        # All-day events need end = start + 1 day
        rotation_end_str = (rotation_date + timedelta(days=1)).strftime("%Y-%m-%d")

        event_title = f"{client} — Cancel old inboxes and set up new ones"
        event_body = {
            "calendarId": GCAL_CALENDAR_ID,
            "summary": event_title,
            "start": {"date": rotation_str},
            "end": {"date": rotation_end_str},
            "description": (
                f"Client: {client}\n"
                f"Domains: {len(config.get('purchased_domains', []))}\n"
                f"Accounts: {infra['actual_accounts']}\n"
                f"Warmup started: {infra['warmup_start_date']}\n"
                f"Config: {config_path}\n\n"
                f"Action: Cancel the current Zapmail inboxes for this client "
                f"and run a fresh infrastructure setup."
            ),
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": 1440},  # 1 day before
                    {"method": "popup", "minutes": 0},     # day of
                ]
            },
        }

        # Use Google Calendar API via OAuth refresh token
        if not all([GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN]):
            missing = []
            if not GOOGLE_CLIENT_ID: missing.append("GOOGLE_CLIENT_ID")
            if not GOOGLE_CLIENT_SECRET: missing.append("GOOGLE_CLIENT_SECRET")
            if not GOOGLE_REFRESH_TOKEN: missing.append("GOOGLE_REFRESH_TOKEN")
            log(f"Missing in .env: {', '.join(missing)} — skipping calendar event.", "WARN")
            log(f"Manual reminder: schedule '{event_title}' on {rotation_str}")
        else:
            try:
                # Exchange refresh token for access token
                token_resp = requests.post(
                    "https://oauth2.googleapis.com/token",
                    data={
                        "client_id": GOOGLE_CLIENT_ID,
                        "client_secret": GOOGLE_CLIENT_SECRET,
                        "refresh_token": GOOGLE_REFRESH_TOKEN,
                        "grant_type": "refresh_token",
                    },
                    timeout=15,
                )
                if token_resp.status_code != 200:
                    raise Exception(f"Token refresh failed ({token_resp.status_code}): {token_resp.text[:200]}")
                access_token = token_resp.json()["access_token"]

                # Create the calendar event
                gcal_url = f"https://www.googleapis.com/calendar/v3/calendars/{GCAL_CALENDAR_ID}/events"
                gcal_payload = {
                    "summary": event_body["summary"],
                    "description": event_body["description"],
                    "start": event_body["start"],
                    "end": event_body["end"],
                    "reminders": event_body["reminders"],
                }
                resp = requests.post(
                    gcal_url,
                    headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
                    json=gcal_payload,
                    timeout=30,
                )
                if resp.status_code in (200, 201):
                    event_data = resp.json()
                    log(f"Calendar event created: {event_title}")
                    log(f"  Date: {rotation_str} (6 weeks from warmup start)")
                    log(f"  Link: {event_data.get('htmlLink', 'N/A')}")
                    config["gcal_rotation_event"] = {
                        "event_id": event_data.get("id"),
                        "date": rotation_str,
                        "link": event_data.get("htmlLink"),
                    }
                else:
                    log(f"Calendar API error ({resp.status_code}): {resp.text[:300]}", "ERROR")
                    log(f"Manual reminder: schedule '{event_title}' on {rotation_str}", "WARN")
            except Exception as e:
                log(f"Calendar event failed: {e}", "ERROR")
                log(f"Manual reminder: schedule '{event_title}' on {rotation_str}", "WARN")

        completed.append("gcal_rotation")
        config["steps_completed"] = completed
        save_config(config, config_path)

    # ── DONE ──
    print(f"\n{'='*60}")
    print(f"  INFRASTRUCTURE SETUP COMPLETE")
    print(f"{'='*60}")
    print(f"  Client:           {client}")
    print(f"  Domains:          {len(config.get('purchased_domains', []))}")
    print(f"  Total inboxes:    {infra['actual_accounts']}")
    print(f"  Daily capacity:   {infra['actual_daily_capacity']}")
    print(f"  Warmup start:     {infra['warmup_start_date']}")
    print(f"  Est. launch:      {infra['estimated_launch_date']}")
    print(f"  Config:           {config_path}")
    print(f"  CSV export:       {config.get('export_csv_path', 'N/A')}")
    gcal_info = config.get("gcal_rotation_event", {})
    if gcal_info:
        print(f"  Rotation date:    {gcal_info.get('date', 'N/A')} (calendar event set)")
    print(f"\n  All steps completed automatically.")
    print(f"  DNS propagation may take 24-48 hours.")
    print(f"{'='*60}\n")

    config["status"] = "complete"
    save_config(config, config_path)
    return True


# ─── DNS-ONLY MODE ───
# For setting nameservers on existing domains from a CSV
# CSV format: domain,provider (e.g. "example.com,porkbun")

def dns_only(csv_path):
    """Set CloudNS nameservers on domains listed in a CSV file."""
    print(f"\n  Setting CloudNS nameservers from: {csv_path}\n")

    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if not rows:
        print("  No rows found in CSV.")
        return

    # Detect column names (flexible)
    domain_col = None
    provider_col = None
    for col in rows[0].keys():
        cl = col.lower().strip()
        if cl in ("domain", "domains", "domain name"):
            domain_col = col
        if cl in ("provider", "registrar", "vendor"):
            provider_col = col

    if not domain_col:
        print("  ERROR: CSV must have a 'Domain' column.")
        return
    if not provider_col:
        print("  WARN: No 'Provider' column found — defaulting to porkbun for all.")

    success_count = 0
    fail_count = 0

    for row in rows:
        domain = row[domain_col].strip()
        provider = row[provider_col].strip().lower() if provider_col else "porkbun"

        if not domain:
            continue

        ok, msg = set_nameservers_for_domain(domain, provider)
        if ok:
            log(f"  NS set: {domain} → CloudNS ({provider})")
            success_count += 1
        else:
            log(f"  NS failed: {domain} ({provider}) — {msg}", "WARN")
            fail_count += 1
        time.sleep(0.5)

    print(f"\n  Done: {success_count} success, {fail_count} failed\n")


# ─── INTERACTIVE INTAKE ───

def interactive(client_name=None, daily_volume=None, forwarding_domain=None):
    print("\n" + "="*60)
    print("  THT EMAIL INFRASTRUCTURE SETUP PIPELINE")
    print("="*60 + "\n")

    # Show domain inventory
    summary = None
    try:
        summary = sheets.get_domain_summary()
        print(f"  Domain inventory: {summary['available_for_clients']} available for clients")
        print(f"  Total in sheet: {summary['total']} ({summary['by_status']})\n")
    except Exception as e:
        print(f"  Could not read sheet: {e}\n")

    if not client_name:
        client_name = input("  Client name: ").strip()
    if not client_name:
        print("  Client name required.")
        return

    if not daily_volume:
        daily_volume = int(input("  Daily sending volume: ").strip())
    if forwarding_domain is None:
        forwarding_domain = input("  Forwarding domain: ").strip()

    infra = calculate_infra(daily_volume)

    print(f"\n  --- INFRASTRUCTURE MATH ---")
    print(f"  Client:                {client_name}")
    print(f"  Daily volume target:   {infra['daily_volume_target']}")
    print(f"  Accounts needed:       {infra['accounts_needed']} ({EMAILS_PER_ACCOUNT_PER_DAY}/day each)")
    print(f"  Domains needed:        {infra['domains_needed']} ({ACCOUNTS_PER_DOMAIN} accounts each)")
    print(f"  Total accounts:        {infra['actual_accounts']}")
    print(f"  Actual daily capacity: {infra['actual_daily_capacity']}")
    print(f"  Warmup start:          {infra['warmup_start_date']}")
    print(f"  Est. launch date:      {infra['estimated_launch_date']}")

    if summary:
        try:
            avail = summary['available_for_clients']
            if avail >= infra['domains_needed']:
                print(f"\n  Sheet has {avail} available — enough for this client")
            else:
                print(f"\n  WARNING: Sheet has {avail} available but need {infra['domains_needed']}")
                print(f"  You'll need to buy {infra['domains_needed'] - avail} more domains first")
        except Exception:
            pass

    # Build config
    config = {
        "client_name": client_name,
        "created_date": datetime.now().strftime("%Y-%m-%d"),
        "infrastructure": infra,
        "forwarding_domain": forwarding_domain,
        "status": "in_progress",
        "steps_completed": []
    }

    filename = f"{client_name.lower().replace(' ', '_')}_{datetime.now().strftime('%Y%m%d')}.json"
    config_path = SCRIPT_DIR / "clients" / filename
    save_config(config, config_path)

    print(f"\n  Config saved: {config_path}")
    print(f"  Starting pipeline...\n")

    run_pipeline(config, config_path)


def just_math(volume):
    infra = calculate_infra(int(volume))
    print(f"\n  --- INFRASTRUCTURE MATH for {volume}/day ---")
    print(f"  Accounts:  {infra['accounts_needed']} ({EMAILS_PER_ACCOUNT_PER_DAY} emails/day each)")
    print(f"  Domains:   {infra['domains_needed']} ({ACCOUNTS_PER_DOMAIN} accounts each)")
    print(f"  Total:     {infra['actual_accounts']} accounts across {infra['domains_needed']} domains")
    print(f"  Capacity:  {infra['actual_daily_capacity']} emails/day")
    print(f"  Warmup:    {infra['warmup_start_date']}")
    print(f"  Launch:    {infra['estimated_launch_date']}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        if sys.argv[1] == "--math" and len(sys.argv) > 2:
            just_math(sys.argv[2])
        elif sys.argv[1] == "--run" and len(sys.argv) > 2:
            config = load_config(sys.argv[2])
            run_pipeline(config, sys.argv[2])
        elif sys.argv[1] == "--dns-only" and len(sys.argv) > 2:
            dns_only(sys.argv[2])
        elif sys.argv[1] == "--auto" and len(sys.argv) >= 4:
            # Fully automated:
            #   python3 setup.py --auto "Client Name" 1000 forwardingdomain.com
            #   python3 setup.py --auto "Client Name" 1000 --no-forward
            fwd_arg = sys.argv[4] if len(sys.argv) >= 5 else None
            if fwd_arg == "--no-forward":
                fwd_arg = ""
            interactive(
                client_name=sys.argv[2],
                daily_volume=int(sys.argv[3]),
                forwarding_domain=fwd_arg,
            )
        else:
            print("Usage:")
            print("  python3 setup.py                                              # Interactive — full pipeline")
            print("  python3 setup.py --auto 'Client Name' 1000 forward.com        # Fully automated, no prompts")
            print("  python3 setup.py --auto 'Client Name' 1000 --no-forward       # Automated, skip forwarding")
            print("  python3 setup.py --math 1000                                  # Just do the math")
            print("  python3 setup.py --run clients/config.json                    # Resume from config")
            print("  python3 setup.py --dns-only domains.csv                       # Set NS on existing domains")
            print("")
            print("  DNS-only CSV format: Domain,Provider")
            print("  Supported providers: porkbun, spaceship")
    else:
        interactive()
