#!/usr/bin/env python3
"""THT Infrastructure Dashboard — local web server.

Works both locally (reads .env file) and hosted (reads environment variables).
"""

import json
import os
import sys
import time
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timedelta
from pathlib import Path

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
SMARTLEAD_JWT = os.environ.get("SMARTLEAD_JWT", "")
ZAPMAIL_API = "https://api.zapmail.ai/api"
ZAPMAIL_KEY = os.environ.get("ZAPMAIL_API_KEY", "")
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


def get_all_accounts():
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


def get_warmup_start_dates():
    """Read warmup start dates from local config files."""
    dates = {}
    for path in (SCRIPT_DIR / "clients").glob("*.json"):
        try:
            c = json.loads(path.read_text())
            name = c.get("client_name", "")
            ws = c.get("infrastructure", {}).get("warmup_start_date", "")
            if name and ws:
                dates[name.lower()] = ws
        except Exception:
            continue
    return dates


# --- API endpoint logic ---

def api_overview():
    clients = get_clients()
    all_accounts = get_all_accounts()
    warmup_dates = get_warmup_start_dates()

    total = len(all_accounts)
    warming = sum(1 for a in all_accounts
                  if a.get("warmup_details", {}).get("status") == "ACTIVE")
    in_campaign = sum(1 for a in all_accounts if a.get("campaign_count", 0) > 0)
    smtp_fail = sum(1 for a in all_accounts if not a.get("is_smtp_success"))
    imap_fail = sum(1 for a in all_accounts if not a.get("is_imap_success"))
    unassigned = sum(1 for a in all_accounts if not a.get("client_id"))
    blocked = [
        {
            "email": a["from_email"],
            "reason": a.get("warmup_details", {}).get("blocked_reason", "Unknown"),
        }
        for a in all_accounts
        if a.get("warmup_details", {}).get("status") not in ("ACTIVE", None)
        and a.get("warmup_details", {}).get("blocked_reason")
    ]

    # Client summaries
    client_summaries = []
    for cl in clients:
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

        cl_warming = sum(1 for a in cl_accounts
                         if a.get("warmup_details", {}).get("status") == "ACTIVE")
        cl_campaigns = sum(1 for a in cl_accounts if a.get("campaign_count", 0) > 0)
        cl_smtp_fail = sum(1 for a in cl_accounts if not a.get("is_smtp_success"))
        cl_blocked = sum(
            1 for a in cl_accounts
            if a.get("warmup_details", {}).get("status") not in ("ACTIVE", None)
        )

        client_summaries.append({
            "id": cl["id"],
            "name": cl["name"],
            "accounts": len(cl_accounts),
            "warming": cl_warming,
            "in_campaign": cl_campaigns,
            "smtp_failures": cl_smtp_fail,
            "blocked": cl_blocked,
            "warmup_start": ws_date,
            "ready_date": ready_date,
            "days_until_ready": days_left,
            "rotation_date": rotation_date,
            "days_until_rotation": rotation_days,
        })

    client_summaries.sort(
        key=lambda c: (
            0 if c["blocked"] > 0 or c["smtp_failures"] > 0 else 1,
            c["name"].lower(),
        )
    )

    return {
        "total_accounts": total,
        "warming": warming,
        "in_campaign": in_campaign,
        "unassigned": unassigned,
        "smtp_failures": smtp_fail,
        "imap_failures": imap_fail,
        "blocked_accounts": blocked[:20],
        "clients": client_summaries,
        "generated_at": datetime.now().isoformat(),
    }


def api_client_accounts(client_id):
    accounts = get_accounts_by_client(int(client_id))
    result = []
    for a in accounts:
        wd = a.get("warmup_details", {})
        result.append({
            "id": a["id"],
            "email": a.get("from_email", ""),
            "domain": a.get("from_email", "").split("@")[-1],
            "warmup_status": wd.get("status", "UNKNOWN"),
            "warmup_sent": wd.get("total_sent_count", 0),
            "warmup_spam": wd.get("total_spam_count", 0),
            "warmup_reputation": wd.get("warmup_reputation", "?"),
            "blocked_reason": wd.get("blocked_reason"),
            "campaign_count": a.get("campaign_count", 0),
            "daily_sent": a.get("daily_sent_count", 0),
            "smtp_ok": a.get("is_smtp_success", False),
            "imap_ok": a.get("is_imap_success", False),
        })
    return {"client_id": int(client_id), "accounts": result}


def api_unassigned():
    all_accounts = get_all_accounts()
    unassigned = [a for a in all_accounts if not a.get("client_id")]
    result = []
    for a in unassigned:
        wd = a.get("warmup_details", {})
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

    # Find mismatches
    mismatches = []
    all_domains = set(zm_tag_by_domain.keys()) | set(sl_client_by_domain.keys())
    for domain in sorted(all_domains):
        zm_client = zm_tag_by_domain.get(domain)
        sl_client = sl_client_by_domain.get(domain)
        if zm_client and sl_client and zm_client.lower() != sl_client.lower():
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
        elif path == "/api/overview":
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
        else:
            self._error(404, "Not found")

    def do_POST(self):
        if not self._check_auth():
            return
        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length)) if content_length else {}

        if self.path == "/api/assign":
            account_ids = body.get("account_ids", [])
            client_id = body.get("client_id")
            if not account_ids or not client_id:
                self._error(400, "account_ids and client_id required")
                return
            result = assign_accounts_to_client(account_ids, client_id)
            self._json_response(result)
        elif self.path == "/api/zapmail/cancel":
            domain_ids = body.get("domain_ids", [])
            if not domain_ids:
                self._error(400, "domain_ids required")
                return
            result = zm_delete_domains(domain_ids)
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


def main():
    port = int(os.environ.get("PORT", sys.argv[1] if len(sys.argv) > 1 else 8099))
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
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
