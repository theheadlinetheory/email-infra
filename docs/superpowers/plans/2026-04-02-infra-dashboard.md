# Infrastructure Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local web dashboard that shows all THT email infrastructure grouped by SmartLead Client, with live health/warmup data and the ability to reassign groups.

**Architecture:** Python stdlib `http.server` backend (no Flask needed) serving a single HTML page. Backend exposes JSON API endpoints that pull from SmartLead. Frontend is vanilla HTML/CSS/JS with fetch calls. SmartLead's Client feature (`client_id` on accounts) is the source of truth for grouping.

**Tech Stack:** Python 3.9 stdlib (`http.server`, `json`, `urllib`), existing `setup.py` API functions, vanilla HTML/CSS/JS.

---

## File Structure

| File | Responsibility |
|------|---------------|
| `~/email-infra/dashboard.py` | HTTP server + JSON API endpoints. Imports API functions from `setup.py`. |
| `~/email-infra/dashboard.html` | Single-page frontend. Fetched by the server at `/`. |
| `~/email-infra/assign_clients.py` | One-time script to assign all existing accounts to their SmartLead clients. Run once before dashboard works. |

---

### Task 0: Assign Existing Accounts to SmartLead Clients

**Files:**
- Create: `~/email-infra/assign_clients.py`

This is a one-time data migration. We need to set `clientId` on all ~1136 accounts so the dashboard can group by client. We also need to create "Generic A" and "Generic B" as SmartLead clients.

- [ ] **Step 1: Write the assignment script**

```python
#!/usr/bin/env python3
"""One-time script: assign all email accounts to their SmartLead clients.
Uses domain -> client mapping from local configs + SmartLead client list."""

import json
import glob
import time
import requests
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from setup import (
    sl_list_accounts, sl_tag_account, sl_internal_headers,
    SMARTLEAD_API, SMARTLEAD_KEY, SMARTLEAD_INTERNAL_API
)


def get_smartlead_clients():
    """Get all SmartLead clients. Returns {name_lower: {id, name, ...}}."""
    r = requests.get(f"{SMARTLEAD_API}/client?api_key={SMARTLEAD_KEY}", timeout=30)
    clients = r.json()
    return {c["name"].lower().strip(): c for c in clients}


def create_smartlead_client(name):
    """Create a new client in SmartLead. Returns the client dict."""
    r = requests.post(
        f"{SMARTLEAD_API}/client/save?api_key={SMARTLEAD_KEY}",
        json={"name": name},
        timeout=30,
    )
    if r.status_code == 200:
        return r.json()
    # Try internal API if public fails
    r2 = requests.post(
        f"{SMARTLEAD_INTERNAL_API}/client/save",
        headers=sl_internal_headers(),
        json={"name": name},
        timeout=30,
    )
    return r2.json()


def build_domain_client_map():
    """Build domain -> client_name from local config files."""
    domain_map = {}
    configs = glob.glob("clients/*.json")
    for path in configs:
        try:
            c = json.load(open(path))
        except Exception:
            continue
        name = c.get("client_name", "")
        if not name or name == "TEST-Run":
            continue
        for d in c.get("purchased_domains", []):
            domain_map[d["domain"]] = name
    return domain_map


def get_all_accounts():
    """Fetch all SmartLead accounts with pagination."""
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
    return all_accounts


def main():
    print("Building domain -> client map from configs...")
    domain_map = build_domain_client_map()
    print(f"  {len(domain_map)} domains mapped to clients")

    print("\nFetching SmartLead clients...")
    sl_clients = get_smartlead_clients()
    print(f"  {len(sl_clients)} clients in SmartLead")

    # Ensure Generic A and Generic B exist as clients
    for name in ["Generic A", "Generic B"]:
        if name.lower() not in sl_clients:
            print(f"  Creating client: {name}")
            result = create_smartlead_client(name)
            print(f"    Result: {result}")
    # Refresh client list
    sl_clients = get_smartlead_clients()

    # Build client name -> SmartLead client_id mapping (fuzzy)
    def find_client_id(client_name):
        low = client_name.lower().strip()
        # Exact match
        if low in sl_clients:
            return sl_clients[low]["id"]
        # Partial match
        for sl_name, sl_data in sl_clients.items():
            if low in sl_name or sl_name in low:
                return sl_data["id"]
        return None

    print("\nFetching all SmartLead accounts...")
    accounts = get_all_accounts()
    print(f"  {len(accounts)} total accounts")

    # Group accounts by client
    assigned = 0
    skipped = 0
    unmatched = 0
    unmatched_domains = set()

    for acc in accounts:
        email = acc.get("from_email", "")
        domain = email.split("@")[-1] if "@" in email else ""
        acc_id = acc["id"]
        current_client = acc.get("client_id")

        client_name = domain_map.get(domain)
        if not client_name:
            unmatched += 1
            if domain:
                unmatched_domains.add(domain)
            continue

        target_client_id = find_client_id(client_name)
        if not target_client_id:
            print(f"  WARN: No SmartLead client found for '{client_name}'")
            unmatched += 1
            continue

        if current_client == target_client_id:
            skipped += 1
            continue

        # Assign client (preserve existing tags by not sending tags field)
        body = {"id": acc_id, "clientId": target_client_id}
        r = requests.post(
            f"{SMARTLEAD_INTERNAL_API}/email-account/save-management-details",
            headers=sl_internal_headers(),
            json=body,
            timeout=30,
        )
        if r.status_code == 200 and r.json().get("ok"):
            assigned += 1
        else:
            print(f"  FAIL: {email} -> {client_name}: {r.text[:200]}")
        time.sleep(0.2)  # Rate limit

    print(f"\nDone!")
    print(f"  Assigned: {assigned}")
    print(f"  Already correct: {skipped}")
    print(f"  Unmatched: {unmatched}")
    if unmatched_domains:
        print(f"  Unmatched domains: {', '.join(sorted(unmatched_domains)[:10])}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the assignment script**

Run: `cd ~/email-infra && PYTHONUNBUFFERED=1 python3 assign_clients.py`
Expected: All accounts assigned to their clients. Generic A and B created as clients if needed.

- [ ] **Step 3: Verify assignments**

```bash
cd ~/email-infra && python3 -c "
from setup import *
import requests, json
clients = requests.get(f'{SMARTLEAD_API}/client?api_key={SMARTLEAD_KEY}', timeout=30).json()
for c in clients:
    accs = requests.get(f'{SMARTLEAD_API}/email-accounts/?api_key={SMARTLEAD_KEY}&client_id={c[\"id\"]}&offset=0&limit=1', timeout=30).json()
    count = len(accs)
    if count > 0:
        print(f'{c[\"name\"]}: {count}+ accounts')
    else:
        print(f'{c[\"name\"]}: 0 accounts')
"
```

- [ ] **Step 4: Commit**

```bash
cd ~/email-infra && git add assign_clients.py && git commit -m "feat: one-time script to assign accounts to SmartLead clients"
```

---

### Task 1: Dashboard Backend — Python HTTP Server

**Files:**
- Create: `~/email-infra/dashboard.py`

The server exposes these JSON endpoints:
- `GET /` → serves `dashboard.html`
- `GET /api/clients` → list all SmartLead clients with account counts
- `GET /api/client/<id>/accounts` → accounts for a client with warmup details
- `GET /api/overview` → summary stats (total accounts, warming, ready, alerts)
- `GET /api/unassigned` → accounts with no client_id
- `POST /api/assign` → assign accounts to a client (body: `{account_ids: [...], client_id: int}`)
- `POST /api/client/create` → create a new SmartLead client (body: `{name: str}`)

- [ ] **Step 1: Write dashboard.py**

```python
#!/usr/bin/env python3
"""THT Infrastructure Dashboard — local web server."""

import json
import os
import sys
import time
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from setup import (
    sl_list_accounts, sl_internal_headers, load_env,
    SMARTLEAD_API, SMARTLEAD_KEY, SMARTLEAD_INTERNAL_API,
)

ENV = load_env()
SCRIPT_DIR = Path(__file__).parent

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


def get_warmup_stats(account_id):
    r = requests.get(
        f"{SMARTLEAD_API}/email-accounts/{account_id}/warmup-stats"
        f"?api_key={SMARTLEAD_KEY}",
        timeout=30,
    )
    return r.json() if r.status_code == 200 else {}


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


def create_client(name):
    r = requests.post(
        f"{SMARTLEAD_API}/client/save?api_key={SMARTLEAD_KEY}",
        json={"name": name},
        timeout=30,
    )
    return r.json() if r.status_code == 200 else {"error": r.text[:300]}


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

    # Sort: alerts first, then by name
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


# --- HTTP Server ---

class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/" or path == "/dashboard.html":
            self._serve_file("dashboard.html", "text/html")
        elif path == "/api/overview":
            self._json_response(api_overview())
        elif path == "/api/clients":
            self._json_response(get_clients())
        elif path.startswith("/api/client/") and path.endswith("/accounts"):
            client_id = path.split("/")[3]
            self._json_response(api_client_accounts(client_id))
        elif path == "/api/unassigned":
            self._json_response(api_unassigned())
        else:
            self._error(404, "Not found")

    def do_POST(self):
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
        elif self.path == "/api/client/create":
            name = body.get("name", "").strip()
            if not name:
                self._error(400, "name required")
                return
            result = create_client(name)
            self._json_response(result)
        else:
            self._error(404, "Not found")

    def _json_response(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _serve_file(self, filename, content_type):
        filepath = SCRIPT_DIR / filename
        if filepath.exists():
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.end_headers()
            self.wfile.write(filepath.read_bytes())
        else:
            self._error(404, f"{filename} not found")

    def _error(self, status, message):
        self._json_response({"error": message}, status)

    def log_message(self, format, *args):
        pass  # Suppress request logs


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    server = HTTPServer(("127.0.0.1", port), DashboardHandler)
    print(f"Dashboard running at http://localhost:{port}")
    print("Press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify server starts**

Run: `cd ~/email-infra && python3 dashboard.py &`
Then: `curl -s http://localhost:8099/api/clients | python3 -m json.tool | head -20`
Expected: JSON list of SmartLead clients

- [ ] **Step 3: Commit**

```bash
cd ~/email-infra && git add dashboard.py && git commit -m "feat: dashboard backend with SmartLead API endpoints"
```

---

### Task 2: Dashboard Frontend — Single HTML Page

**Files:**
- Create: `~/email-infra/dashboard.html`

The page auto-loads the overview on open, shows client cards with health metrics, and has a drill-down view for individual client accounts. Includes an "Assign" action for moving unassigned accounts to a client.

- [ ] **Step 1: Write dashboard.html**

This is a single self-contained HTML file with embedded CSS and JS. The full content is too large for inline plan display — see the implementation step for the complete file. Key sections:

**Layout:**
- Top bar: "THT Infrastructure Dashboard" + last refreshed time + refresh button
- Summary row: total accounts, warming, in campaigns, unassigned, alerts
- Client cards grid: one card per client showing account count, warmup status, ready date, health
- Alert banner at top if any accounts have SMTP failures or blocked warmup
- Click a client card to expand and show individual accounts
- "Unassigned" section at bottom with assign-to-client dropdown

**Styling:**
- Dark background (#1a1a2e), card-based layout
- Green = healthy, yellow = warming, red = problem
- Responsive, works on laptop screen

**JS behavior:**
- On load: `fetch('/api/overview')` and render everything
- Click client card: `fetch('/api/client/{id}/accounts')` and show detail table
- Assign button: `POST /api/assign` with selected accounts + client
- Refresh button: re-fetch overview
- Auto-refresh every 5 minutes

- [ ] **Step 2: Verify frontend loads**

Run: `open http://localhost:8099` (with dashboard.py already running)
Expected: Dashboard page loads showing client cards with live SmartLead data

- [ ] **Step 3: Commit**

```bash
cd ~/email-infra && git add dashboard.html && git commit -m "feat: dashboard frontend with client cards, health metrics, assign flow"
```

---

### Task 3: Initial Client Assignment + End-to-End Test

**Files:**
- Uses: `~/email-infra/assign_clients.py`

- [ ] **Step 1: Run the client assignment script**

Run: `cd ~/email-infra && PYTHONUNBUFFERED=1 python3 assign_clients.py`
Expected: All accounts with known domains get assigned to their SmartLead clients. Output shows assigned/skipped/unmatched counts.

- [ ] **Step 2: Start dashboard and verify**

Run: `cd ~/email-infra && python3 dashboard.py &` then `open http://localhost:8099`
Expected: Dashboard shows all clients with correct account counts. Generic A shows 51, Generic B shows 51, etc.

- [ ] **Step 3: Test the assign flow**

In the dashboard UI:
1. Check "Unassigned" section — should show any accounts not in configs
2. Test assigning one unassigned account to a client via the dropdown
3. Refresh — account should now appear under that client

- [ ] **Step 4: Final commit**

```bash
cd ~/email-infra && git add -A && git commit -m "feat: infrastructure dashboard MVP complete"
```
