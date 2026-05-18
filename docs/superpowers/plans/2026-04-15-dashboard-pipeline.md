# Dashboard Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add self-service infrastructure setup to the dashboard with 6-step pill stepper, background execution, and error recovery UI.

**Architecture:** New `pipeline_engine.py` runs infrastructure setup as a background thread. State persists to a `setup_pipelines` Supabase table (separate from the existing `pipelines` table used by the monitor system). Frontend polls `/api/setup-pipeline/{id}` and renders progress as horizontal pill steppers on cards. The engine reuses existing `setup.py` API functions.

**Tech Stack:** Python 3.9, Supabase (PostgREST), SmartLead API, Zapmail API, vanilla HTML/CSS/JS

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `supabase_schema.sql` | Modify | Add `setup_pipelines` table DDL |
| `db.py` | Modify | Add CRUD functions for `setup_pipelines` |
| `pipeline_engine.py` | Create | Pipeline execution engine (6 steps) |
| `dashboard.py` | Modify | API endpoints + background thread launch |
| `dashboard.html` | Modify | Pipeline UI (form, cards, pill stepper) |
| `web/public/index.html` | Modify | Mirror of dashboard.html for Vercel |

---

### Task 1: Supabase Table + CRUD

**Files:**
- Modify: `supabase_schema.sql`
- Modify: `db.py:141-177` (add after rotation CRUD)

- [ ] **Step 1: Add setup_pipelines table to schema**

Add to `supabase_schema.sql` before the RLS section (before line 68):

```sql
-- Infrastructure setup pipelines (dashboard-driven)
create table if not exists setup_pipelines (
    id uuid primary key default gen_random_uuid(),
    name text not null,
    type text not null default 'generic',
    config jsonb not null default '{}',
    status text not null default 'pending',
    current_step int not null default 0,
    steps jsonb not null default '[]',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists idx_setup_pipelines_status on setup_pipelines (status);
```

Also add at the bottom with the other RLS disables:

```sql
alter table setup_pipelines disable row level security;
```

- [ ] **Step 2: Run the DDL in Supabase**

Run the CREATE TABLE + index + RLS statements in the Supabase SQL Editor (or via the dashboard's psql connection).

- [ ] **Step 3: Add CRUD functions to db.py**

Add after the `update_rotation_swap` function (after line 177 in `db.py`):

```python
# --- Setup Pipelines ---

def create_setup_pipeline(name: str, pipeline_type: str, config: dict, steps: list) -> str:
    """Create a new setup pipeline. Returns the generated UUID."""
    row = {
        "name": name, "type": pipeline_type, "config": json.dumps(config),
        "status": "pending", "current_step": 0, "steps": json.dumps(steps),
    }
    result = _request("POST", "/setup_pipelines", json_body=row,
                      headers={"Prefer": "return=representation"})
    return result[0]["id"] if result else ""


def get_setup_pipeline(pipeline_id: str) -> dict | None:
    rows = _request("GET", "/setup_pipelines",
                    params={"select": "*", "id": f"eq.{pipeline_id}"})
    if rows:
        r = rows[0]
        if isinstance(r.get("config"), str):
            r["config"] = json.loads(r["config"])
        if isinstance(r.get("steps"), str):
            r["steps"] = json.loads(r["steps"])
        return r
    return None


def list_setup_pipelines(status: str = None) -> list[dict]:
    params = {"select": "*", "order": "created_at.desc", "limit": "50"}
    if status:
        params["status"] = f"eq.{status}"
    rows = _request("GET", "/setup_pipelines", params=params)
    for r in rows:
        if isinstance(r.get("config"), str):
            r["config"] = json.loads(r["config"])
        if isinstance(r.get("steps"), str):
            r["steps"] = json.loads(r["steps"])
    return rows


def update_setup_pipeline(pipeline_id: str, **fields) -> None:
    """Update arbitrary fields on a setup pipeline. JSON-encodes config/steps if present."""
    body = {}
    for k, v in fields.items():
        if k in ("config", "steps") and not isinstance(v, str):
            body[k] = json.dumps(v)
        else:
            body[k] = v
    body["updated_at"] = "now()"
    _request("PATCH", "/setup_pipelines",
             params={"id": f"eq.{pipeline_id}"}, json_body=body)
```

- [ ] **Step 4: Commit**

```bash
git add supabase_schema.sql db.py
git commit -m "feat: add setup_pipelines table and CRUD functions"
```

---

### Task 2: Pipeline Engine — Core + Step 1-2

**Files:**
- Create: `pipeline_engine.py`

- [ ] **Step 1: Create pipeline_engine.py with imports and constants**

```python
"""Infrastructure setup pipeline engine.

Runs as background threads in the dashboard. State persists to the
setup_pipelines Supabase table so pipelines survive server restarts.
"""
import json
import os
import threading
import time
from datetime import datetime

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import db as store
from setup import (
    zm_connect_domain_single, zm_list_domains, zm_create_mailboxes,
    zm_buy_addon_mailboxes, zm_export_mailboxes, zm_put, zm_get,
    sl_list_accounts, sl_get_all_tags, sl_find_or_create_tag,
    sl_tag_accounts_bulk,
    SMARTLEAD_API, SMARTLEAD_KEY,
    PROFILE_PHOTO_URL, ACQUISITION_PHOTO_URL, ACQUISITION_PHOTO_URLS,
    log,
)
import requests

# Mailbox specs per sender
SENDER_SPECS = {
    "sean_reynolds": [
        {"firstName": "Sean", "lastName": "Reynolds", "mailboxUsername": "s.reynolds"},
        {"firstName": "Sean", "lastName": "Reynolds", "mailboxUsername": "sean.r"},
        {"firstName": "Sean", "lastName": "Reynolds", "mailboxUsername": "sean.reynolds"},
    ],
    "aidan_hutchinson": [
        {"firstName": "Aidan", "lastName": "Hutchinson", "mailboxUsername": "aidan"},
        {"firstName": "Aidan", "lastName": "Hutchinson", "mailboxUsername": "aidanh"},
        {"firstName": "Aidan", "lastName": "Hutchinson", "mailboxUsername": "aidanhutch"},
    ],
    "lars_matthys": [
        {"firstName": "Lars", "lastName": "Matthys", "mailboxUsername": "lars"},
        {"firstName": "Lars", "lastName": "Matthys", "mailboxUsername": "lars.m"},
        {"firstName": "Lars", "lastName": "Matthys", "mailboxUsername": "larsmatthys"},
    ],
}

PHOTO_URLS = {
    "sean_reynolds": PROFILE_PHOTO_URL,
    "aidan_hutchinson": ACQUISITION_PHOTO_URLS.get("aidan_hutchinson", ACQUISITION_PHOTO_URL),
    "lars_matthys": ACQUISITION_PHOTO_URLS.get("lars_matthys", ACQUISITION_PHOTO_URL),
}

STEP_NAMES = [
    "Connect Domains",
    "Create Inboxes",
    "Profile Photos",
    "SmartLead Export",
    "Tag & Assign",
    "Enable Warmup",
]

INBOXES_PER_DOMAIN = 3

WARMUP_CONFIG = {
    "warmup_enabled": True,
    "total_warmup_per_day": 30,
    "daily_rampup": 2,
    "reply_rate_percentage": 30,
}

# Active pipeline threads keyed by pipeline ID
_active_threads: dict[str, threading.Thread] = {}
```

- [ ] **Step 2: Add helper functions for state updates**

Append to `pipeline_engine.py`:

```python
def _init_steps(total_domains: int) -> list[dict]:
    """Build the initial 6-step array."""
    total_mbs = total_domains * INBOXES_PER_DOMAIN
    totals = [total_domains, total_mbs, total_mbs, total_mbs, total_mbs, total_mbs]
    return [
        {"name": name, "status": "pending", "progress": 0, "total": t,
         "error": None, "started_at": None, "completed_at": None}
        for name, t in zip(STEP_NAMES, totals)
    ]


def _update_step(pipeline_id: str, step_idx: int, **fields):
    """Update a single step in the steps array and persist."""
    p = store.get_setup_pipeline(pipeline_id)
    if not p:
        return
    steps = p["steps"]
    steps[step_idx].update(fields)
    store.update_setup_pipeline(pipeline_id, steps=steps, current_step=step_idx)


def _start_step(pipeline_id: str, step_idx: int):
    _update_step(pipeline_id, step_idx,
                 status="running", started_at=datetime.utcnow().isoformat() + "Z")


def _finish_step(pipeline_id: str, step_idx: int):
    p = store.get_setup_pipeline(pipeline_id)
    if not p:
        return
    steps = p["steps"]
    steps[step_idx]["status"] = "completed"
    steps[step_idx]["completed_at"] = datetime.utcnow().isoformat() + "Z"
    store.update_setup_pipeline(pipeline_id, steps=steps, current_step=step_idx)


def _fail_step(pipeline_id: str, step_idx: int, error: str):
    _update_step(pipeline_id, step_idx, status="failed", error=error)
    store.update_setup_pipeline(pipeline_id, status="failed")


def _progress(pipeline_id: str, step_idx: int, progress: int):
    """Update progress count without re-reading full pipeline (lightweight)."""
    p = store.get_setup_pipeline(pipeline_id)
    if not p:
        return
    steps = p["steps"]
    steps[step_idx]["progress"] = progress
    store.update_setup_pipeline(pipeline_id, steps=steps)
```

- [ ] **Step 3: Add Step 1 — Connect Domains**

Append to `pipeline_engine.py`:

```python
def step_connect_domains(pipeline_id: str, config: dict) -> bool:
    """Step 1: Connect domains to Zapmail."""
    _start_step(pipeline_id, 0)
    domains = config["domains"]

    # Check which are already connected
    existing = zm_list_domains()
    existing_names = {d.get("domain", "") for d in existing}

    connected = 0
    failures = []
    for domain in domains:
        if domain in existing_names:
            connected += 1
            _progress(pipeline_id, 0, connected)
            continue
        try:
            result = zm_connect_domain_single(domain)
            if isinstance(result, dict) and result.get("_raw_status", 200) >= 400:
                failures.append(f"{domain}: {result.get('message', 'unknown error')[:80]}")
            else:
                connected += 1
        except Exception as e:
            failures.append(f"{domain}: {e}")
        _progress(pipeline_id, 0, connected)
        time.sleep(0.5)

    if failures and len(failures) > len(domains) * 0.2:
        _fail_step(pipeline_id, 0, f"{len(failures)} domains failed: " + "; ".join(failures[:5]))
        return False

    _finish_step(pipeline_id, 0)
    return True
```

- [ ] **Step 4: Add Step 2 — Create Inboxes**

Append to `pipeline_engine.py`:

```python
def step_create_inboxes(pipeline_id: str, config: dict) -> bool:
    """Step 2: Buy mailbox slots if needed, create mailboxes on each domain."""
    _start_step(pipeline_id, 1)
    domains = config["domains"]
    sender = config.get("sender", "sean_reynolds")
    specs = SENDER_SPECS.get(sender, SENDER_SPECS["sean_reynolds"])

    # Get domain IDs and existing mailbox counts from Zapmail
    all_zm = zm_list_domains()
    zm_by_name = {d.get("domain", ""): d for d in all_zm}

    domains_needing_inboxes = []
    mailbox_ids = []
    already_done = 0
    for domain in domains:
        d = zm_by_name.get(domain)
        if not d:
            continue
        existing_mbs = d.get("mailboxes", [])
        if len(existing_mbs) >= INBOXES_PER_DOMAIN:
            already_done += len(existing_mbs)
            mailbox_ids.extend(m["id"] for m in existing_mbs)
        else:
            domains_needing_inboxes.append((domain, d["id"]))

    _progress(pipeline_id, 1, already_done)

    # Buy slots if needed
    if domains_needing_inboxes:
        needed = len(domains_needing_inboxes) * INBOXES_PER_DOMAIN
        ws = zm_get("/v2/workspaces")
        cw = ws.get("data", {}).get("currentWorkspace", {}) if isinstance(ws, dict) else {}
        total_purchased = int(cw.get("totalMailboxesPurchasedGoogle", "0"))
        total_assigned = int(cw.get("assignedMailboxesCountGoogle", "0"))
        unassigned = total_purchased - total_assigned
        slots_to_buy = max(0, needed - unassigned)

        if slots_to_buy > 0:
            buy_result = zm_buy_addon_mailboxes(slots_to_buy)
            if isinstance(buy_result, dict) and "Insufficient wallet balance" in buy_result.get("message", ""):
                _fail_step(pipeline_id, 1, buy_result["message"])
                return False
            time.sleep(3)  # Let purchase process

    # Create mailboxes
    created = already_done
    failures = []
    for domain, domain_id in domains_needing_inboxes:
        try:
            result = zm_create_mailboxes(domain_id, domain, specs)
            if isinstance(result, dict) and result.get("_raw_status", 200) >= 400:
                msg = result.get("message", "")
                if "don't have enough mailboxes" in msg or "not enough mailboxes" in msg.lower():
                    # Retry after buying more
                    zm_buy_addon_mailboxes(INBOXES_PER_DOMAIN)
                    time.sleep(3)
                    result = zm_create_mailboxes(domain_id, domain, specs)
                if isinstance(result, dict) and result.get("_raw_status", 200) >= 400:
                    failures.append(f"{domain}: {result.get('message', '')[:80]}")
                    continue
            created += INBOXES_PER_DOMAIN
        except Exception as e:
            failures.append(f"{domain}: {e}")
        _progress(pipeline_id, 1, created)
        time.sleep(0.3)

    # Re-fetch to get all mailbox IDs
    all_zm = zm_list_domains()
    zm_by_name = {d.get("domain", ""): d for d in all_zm}
    mailbox_ids = []
    for domain in domains:
        d = zm_by_name.get(domain)
        if d:
            mailbox_ids.extend(m["id"] for m in d.get("mailboxes", []))

    # Store IDs in config
    config["mailbox_ids"] = mailbox_ids
    store.update_setup_pipeline(pipeline_id, config=config)

    if failures and len(failures) > len(domains) * 0.2:
        _fail_step(pipeline_id, 1, f"{len(failures)} domains failed: " + "; ".join(failures[:5]))
        return False

    _finish_step(pipeline_id, 1)
    return True
```

- [ ] **Step 5: Commit**

```bash
git add pipeline_engine.py
git commit -m "feat: pipeline engine with connect domains + create inboxes steps"
```

---

### Task 3: Pipeline Engine — Steps 3-6 + Runner

**Files:**
- Modify: `pipeline_engine.py`

- [ ] **Step 1: Add Step 3 — Profile Photos**

Append to `pipeline_engine.py`:

```python
def step_profile_photos(pipeline_id: str, config: dict) -> bool:
    """Step 3: Set profile photos on all mailboxes."""
    _start_step(pipeline_id, 2)
    mailbox_ids = config.get("mailbox_ids", [])
    sender = config.get("sender", "sean_reynolds")
    photo_url = PHOTO_URLS.get(sender, PROFILE_PHOTO_URL)

    if not mailbox_ids:
        _fail_step(pipeline_id, 2, "No mailbox IDs found in config")
        return False

    done = 0
    batch_size = 50
    for i in range(0, len(mailbox_ids), batch_size):
        batch = mailbox_ids[i:i + batch_size]
        mailbox_data = [{"mailboxId": mid, "profilePicture": photo_url} for mid in batch]
        try:
            result = zm_put("/v2/mailboxes", {"mailboxData": mailbox_data})
            if isinstance(result, dict) and result.get("_raw_status", 200) >= 400:
                log(f"[PIPELINE] Photo batch failed: {result.get('message', '')[:100]}", "WARN")
            else:
                done += len(batch)
        except Exception as e:
            log(f"[PIPELINE] Photo batch error: {e}", "WARN")
        _progress(pipeline_id, 2, done)
        time.sleep(1)

    _finish_step(pipeline_id, 2)
    return True
```

- [ ] **Step 2: Add Step 4 — SmartLead Export**

Append to `pipeline_engine.py`:

```python
def step_smartlead_export(pipeline_id: str, config: dict) -> bool:
    """Step 4: Export mailboxes to SmartLead and verify they appear."""
    _start_step(pipeline_id, 3)
    mailbox_ids = config.get("mailbox_ids", [])
    domains = set(config["domains"])
    expected = len(domains) * INBOXES_PER_DOMAIN

    if not mailbox_ids:
        _fail_step(pipeline_id, 3, "No mailbox IDs found in config")
        return False

    # Export
    result = zm_export_mailboxes(apps=["SMARTLEAD"], mailbox_ids=mailbox_ids)
    log(f"[PIPELINE] Export result: {str(result)[:200]}")

    # Wait for propagation then verify
    time.sleep(180)

    max_attempts = 5
    found = {}
    for attempt in range(max_attempts):
        found = {}
        offset = 0
        while True:
            try:
                batch = sl_list_accounts(offset=offset, limit=100)
            except Exception:
                time.sleep(5)
                break
            if not isinstance(batch, list) or not batch:
                break
            for acc in batch:
                email = acc.get("from_email", acc.get("email", ""))
                domain = email.split("@")[-1] if "@" in email else ""
                if domain in domains:
                    found[email] = acc["id"]
            if len(batch) < 100:
                break
            offset += 100

        _progress(pipeline_id, 3, len(found))
        log(f"[PIPELINE] Export verify attempt {attempt + 1}: {len(found)}/{expected}")

        if len(found) >= expected:
            break

        if attempt < max_attempts - 1:
            # Re-export missing domains
            found_domains = {email.split("@")[-1] for email in found}
            missing = domains - found_domains
            if missing:
                for domain in missing:
                    zm_export_mailboxes(apps=["SMARTLEAD"], contains=domain)
                    time.sleep(2)
            time.sleep(180)

    # Store account IDs
    config["smartlead_account_ids"] = found
    store.update_setup_pipeline(pipeline_id, config=config)

    if len(found) < expected * 0.8:
        _fail_step(pipeline_id, 3,
                   f"Only {len(found)}/{expected} accounts found in SmartLead after {max_attempts} attempts")
        return False

    _finish_step(pipeline_id, 3)
    return True
```

- [ ] **Step 3: Add Step 5 — Tag & Assign**

Append to `pipeline_engine.py`:

```python
def step_tag_assign(pipeline_id: str, config: dict) -> bool:
    """Step 5: Tag accounts and assign to SmartLead client."""
    _start_step(pipeline_id, 4)
    account_map = config.get("smartlead_account_ids", {})
    if not account_map:
        _fail_step(pipeline_id, 4, "No SmartLead account IDs in config")
        return False

    tag_ids = list(config.get("tag_ids", {}).values())
    client_id = config.get("smartlead_client_id")
    account_ids = list(account_map.values())

    if not tag_ids:
        _fail_step(pipeline_id, 4, "No tag IDs in config")
        return False

    log(f"[PIPELINE] Tagging {len(account_ids)} accounts with tags {tag_ids}, client_id={client_id}")
    success, fail = sl_tag_accounts_bulk(account_ids, tag_ids, client_id=client_id)
    _progress(pipeline_id, 4, success)

    if fail > 0:
        # Retry failures individually
        log(f"[PIPELINE] Retrying {fail} failed tags individually...")
        time.sleep(2)
        retry_success = 0
        for aid in account_ids:
            try:
                s, f = sl_tag_accounts_bulk([aid], tag_ids, client_id=client_id)
                retry_success += s
            except Exception:
                pass
            time.sleep(0.3)
        _progress(pipeline_id, 4, success + retry_success)

    _finish_step(pipeline_id, 4)
    return True
```

- [ ] **Step 4: Add Step 6 — Enable Warmup**

Append to `pipeline_engine.py`:

```python
def step_enable_warmup(pipeline_id: str, config: dict) -> bool:
    """Step 6: Enable warmup on all SmartLead accounts."""
    _start_step(pipeline_id, 5)
    account_map = config.get("smartlead_account_ids", {})
    if not account_map:
        _fail_step(pipeline_id, 5, "No SmartLead account IDs in config")
        return False

    done = 0
    failures = 0
    total = len(account_map)
    for email, aid in account_map.items():
        try:
            r = requests.post(
                f"{SMARTLEAD_API}/email-accounts/{aid}/warmup",
                params={"api_key": SMARTLEAD_KEY},
                json=WARMUP_CONFIG,
                timeout=15,
            )
            if r.status_code == 200:
                done += 1
            else:
                failures += 1
        except Exception:
            failures += 1
        _progress(pipeline_id, 5, done)
        time.sleep(0.3)

    log(f"[PIPELINE] Warmup enabled: {done}/{total} ({failures} failed)")

    if failures > total * 0.2:
        _fail_step(pipeline_id, 5, f"Warmup failed for {failures}/{total} accounts")
        return False

    _finish_step(pipeline_id, 5)
    return True
```

- [ ] **Step 5: Add the main runner and public API**

Append to `pipeline_engine.py`:

```python
# Step functions indexed to match STEP_NAMES
_STEP_FUNCTIONS = [
    step_connect_domains,
    step_create_inboxes,
    step_profile_photos,
    step_smartlead_export,
    step_tag_assign,
    step_enable_warmup,
]


def run_pipeline(pipeline_id: str):
    """Execute all pending steps in sequence. Called in a background thread."""
    p = store.get_setup_pipeline(pipeline_id)
    if not p:
        log(f"[PIPELINE] Pipeline {pipeline_id} not found", "ERROR")
        return

    store.update_setup_pipeline(pipeline_id, status="running")
    config = p["config"]
    steps = p["steps"]

    for i, step_fn in enumerate(_STEP_FUNCTIONS):
        # Skip completed steps (for resume after restart)
        if steps[i]["status"] == "completed":
            continue
        # Stop if a previous step failed
        if i > 0 and steps[i - 1]["status"] == "failed":
            break

        log(f"[PIPELINE] {p['name']}: starting step {i + 1}/6 — {STEP_NAMES[i]}")
        success = step_fn(pipeline_id, config)

        if not success:
            log(f"[PIPELINE] {p['name']}: step {STEP_NAMES[i]} failed")
            return

        # Re-read config in case step updated it (mailbox_ids, account_ids)
        p = store.get_setup_pipeline(pipeline_id)
        if not p:
            return
        config = p["config"]
        steps = p["steps"]

    store.update_setup_pipeline(pipeline_id, status="completed")
    log(f"[PIPELINE] {p['name']}: COMPLETE")

    # macOS notification
    try:
        os.system(f'osascript -e \'display notification "{p["name"]} pipeline complete!" '
                  f'with title "Email Infra" sound name "Glass"\'')
    except Exception:
        pass


def create_and_start(name: str, pipeline_type: str, config: dict) -> str:
    """Create a pipeline and start it in a background thread. Returns pipeline ID."""
    steps = _init_steps(len(config["domains"]))
    pipeline_id = store.create_setup_pipeline(name, pipeline_type, config, steps)
    if not pipeline_id:
        raise RuntimeError("Failed to create pipeline in Supabase")

    t = threading.Thread(target=run_pipeline, args=(pipeline_id,), daemon=True,
                         name=f"pipeline-{name}")
    _active_threads[pipeline_id] = t
    t.start()
    return pipeline_id


def retry_failed_step(pipeline_id: str) -> bool:
    """Retry the current failed step. Returns True if retry started."""
    p = store.get_setup_pipeline(pipeline_id)
    if not p or p["status"] != "failed":
        return False

    steps = p["steps"]
    failed_idx = None
    for i, s in enumerate(steps):
        if s["status"] == "failed":
            failed_idx = i
            break

    if failed_idx is None:
        return False

    # Reset the failed step
    steps[failed_idx]["status"] = "running"
    steps[failed_idx]["error"] = None
    store.update_setup_pipeline(pipeline_id, steps=steps, status="running")

    # Re-run from the failed step
    t = threading.Thread(target=run_pipeline, args=(pipeline_id,), daemon=True,
                         name=f"pipeline-retry-{p['name']}")
    _active_threads[pipeline_id] = t
    t.start()
    return True


def resume_running_pipelines():
    """On server start, resume any pipelines that were running when the server stopped."""
    running = store.list_setup_pipelines(status="running")
    for p in running:
        log(f"[PIPELINE] Resuming: {p['name']} (step {p['current_step'] + 1}/6)")
        t = threading.Thread(target=run_pipeline, args=(p["id"],), daemon=True,
                             name=f"pipeline-resume-{p['name']}")
        _active_threads[p["id"]] = t
        t.start()
```

- [ ] **Step 6: Commit**

```bash
git add pipeline_engine.py
git commit -m "feat: complete pipeline engine with all 6 steps and runner"
```

---

### Task 4: Dashboard API Endpoints

**Files:**
- Modify: `dashboard.py`

- [ ] **Step 1: Add import**

At the top of `dashboard.py`, add to the imports (around line 7-19):

```python
import pipeline_engine
```

- [ ] **Step 2: Add API helper for pipeline config building**

Add before the `DashboardHandler` class (around line 2670):

```python
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
            log(f"[PIPELINE] SmartLead client lookup failed: {e}", "WARN")

    photo_url = pipeline_engine.PHOTO_URLS.get(sender, PROFILE_PHOTO_URL)

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
```

- [ ] **Step 3: Add next-available-generic-name helper**

Add right after `build_pipeline_config`:

```python
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
```

- [ ] **Step 4: Add GET endpoints to do_GET**

In `do_GET` method of `DashboardHandler`, add these routes (after the existing `/api/generic-groups` route around line 2750):

```python
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
```

- [ ] **Step 5: Add POST endpoints to do_POST**

In `do_POST` method, add these routes (after the existing POST routes):

```python
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
```

- [ ] **Step 6: Add pipeline resume on server start**

In the `main()` function at the bottom of `dashboard.py`, add after the monitor thread start (around line 3037):

```python
    # Resume any interrupted setup pipelines
    pipeline_engine.resume_running_pipelines()
    print("Setup pipeline resume check complete")
```

- [ ] **Step 7: Commit**

```bash
git add dashboard.py
git commit -m "feat: add setup pipeline API endpoints and server integration"
```

---

### Task 5: Dashboard Frontend — Pipeline UI

**Files:**
- Modify: `dashboard.html`

- [ ] **Step 1: Add pipeline section HTML**

Add after the generic-section div (after line 280 in `dashboard.html`), a new section for active pipelines:

```html
<div id="pipeline-section" style="display:none;margin-top:28px;">
    <div class="section-header" style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;">
        <h2 style="font-size:16px;font-weight:600;color:var(--text-primary);margin:0;">Active Pipelines</h2>
        <button onclick="openNewPipelineModal()" class="btn-accent" style="font-size:12px;padding:6px 14px;">+ New Pipeline</button>
    </div>
    <div class="clients-grid" id="pipeline-grid"></div>
</div>
```

- [ ] **Step 2: Add CSS for pill stepper**

Add to the `<style>` section (after the existing badge styles):

```css
.pill-stepper{display:flex;align-items:center;gap:0;margin:10px 0 6px;}
.pill-step{display:flex;align-items:center;font-size:11px;font-weight:500;padding:4px 10px;border-radius:14px;white-space:nowrap;}
.pill-step.completed{background:var(--accent-bg);color:var(--accent);}
.pill-step.running{background:var(--accent-bg);color:var(--accent);animation:pulse-pill 1.5s ease-in-out infinite;}
.pill-step.pending{background:var(--bg-input);color:var(--text-muted);}
.pill-step.failed{background:#fef2f2;color:var(--red);}
.pill-connector{width:20px;height:2px;flex-shrink:0;}
.pill-connector.done{background:var(--accent);}
.pill-connector.pending{background:var(--border);}
.pill-icon{margin-right:4px;font-size:12px;}
@keyframes pulse-pill{0%,100%{opacity:1;}50%{opacity:.6;}}

.pipeline-status-line{font-size:12px;color:var(--text-secondary);margin-top:4px;}

/* New Pipeline Modal */
.pipeline-modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:1000;display:flex;align-items:center;justify-content:center;}
.pipeline-modal{background:var(--bg-surface);border-radius:12px;padding:28px;width:520px;max-height:80vh;overflow-y:auto;box-shadow:0 20px 60px rgba(0,0,0,.15);}
.pipeline-modal h3{margin:0 0 18px;font-size:17px;font-weight:600;color:var(--text-primary);}
.pipeline-modal label{display:block;font-size:12px;font-weight:500;color:var(--text-secondary);margin:12px 0 4px;}
.pipeline-modal input,.pipeline-modal textarea,.pipeline-modal select{width:100%;padding:8px 12px;border:1px solid var(--border);border-radius:var(--radius);background:var(--bg-input);color:var(--text-primary);font-size:13px;font-family:var(--font-body);}
.pipeline-modal textarea{min-height:120px;resize:vertical;}
.type-pills{display:flex;gap:8px;margin:8px 0 12px;}
.type-pill{padding:6px 16px;border-radius:20px;font-size:12px;font-weight:500;cursor:pointer;border:1px solid var(--border);background:var(--bg-input);color:var(--text-secondary);transition:all .15s;}
.type-pill.active{background:var(--accent-bg);color:var(--accent);border-color:var(--accent);}
.pipeline-modal .btn-start{margin-top:18px;width:100%;padding:10px;background:var(--accent);color:white;border:none;border-radius:var(--radius);font-size:13px;font-weight:600;cursor:pointer;}
.pipeline-modal .btn-start:hover{background:var(--accent-dim);}
```

- [ ] **Step 3: Add renderPipelines() function**

Add to the `<script>` section:

```javascript
function renderPipelineSteps(steps) {
    return steps.map((s, i) => {
        const icon = s.status === 'completed' ? '&#10003;' :
                     s.status === 'running' ? '&#9679;' :
                     s.status === 'failed' ? '&#10007;' : '&#9675;';
        const cls = s.status || 'pending';
        const connector = i < steps.length - 1
            ? `<div class="pill-connector ${s.status === 'completed' ? 'done' : 'pending'}"></div>`
            : '';
        // Shorten names for mini view
        const shortName = s.name.replace('Connect Domains', 'Connect')
            .replace('Create Inboxes', 'Inboxes')
            .replace('Profile Photos', 'Photos')
            .replace('SmartLead Export', 'Export')
            .replace('Tag & Assign', 'Tag')
            .replace('Enable Warmup', 'Warmup');
        return `<span class="pill-step ${cls}"><span class="pill-icon">${icon}</span>${shortName}</span>${connector}`;
    }).join('');
}

function pipelineStatusLine(p) {
    if (p.status === 'completed') return 'Complete';
    if (p.status === 'failed') {
        const failed = p.steps.find(s => s.status === 'failed');
        return failed ? `Failed: ${failed.error || failed.name}` : 'Failed';
    }
    const running = p.steps.find(s => s.status === 'running');
    if (running) return `${running.name}... ${running.progress}/${running.total}`;
    return p.status;
}

function renderPipelines(pipelines) {
    const grid = document.getElementById('pipeline-grid');
    if (!pipelines || !pipelines.length) {
        document.getElementById('pipeline-section').style.display = 'none';
        return;
    }
    document.getElementById('pipeline-section').style.display = '';
    grid.innerHTML = pipelines.map(p => {
        const statusLine = pipelineStatusLine(p);
        const retryBtn = p.status === 'failed'
            ? `<button onclick="retryPipeline('${p.id}')" style="margin-top:8px;font-size:11px;padding:4px 10px;border-radius:6px;border:1px solid var(--red);color:var(--red);background:transparent;cursor:pointer;">Retry</button>`
            : '';
        return `<div class="client-card" onclick="showPipelineDetail('${p.id}')" style="cursor:pointer;">
            <div class="cc-header"><span class="cc-name">${p.name}</span>
                <span class="badge" style="background:${p.status === 'completed' ? 'var(--accent-bg)' : p.status === 'failed' ? '#fef2f2' : 'var(--accent-bg)'};color:${p.status === 'completed' ? 'var(--accent)' : p.status === 'failed' ? 'var(--red)' : 'var(--accent)'};">${p.type}</span>
            </div>
            <div class="pill-stepper">${renderPipelineSteps(p.steps)}</div>
            <div class="pipeline-status-line">${statusLine}</div>
            ${retryBtn}
        </div>`;
    }).join('');
}
```

- [ ] **Step 4: Add the new pipeline modal**

Add to `<script>`:

```javascript
let newPipelineType = 'generic';

async function openNewPipelineModal() {
    newPipelineType = 'generic';
    // Fetch next generic name
    let suggestedName = 'Generic A';
    try {
        const resp = await fetch('/api/next-generic-name');
        const data = await resp.json();
        suggestedName = data.name || 'Generic A';
    } catch(e) {}

    const overlay = document.createElement('div');
    overlay.className = 'pipeline-modal-overlay';
    overlay.id = 'pipeline-modal';
    overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };
    overlay.innerHTML = `
        <div class="pipeline-modal">
            <h3>New Infrastructure Pipeline</h3>
            <label>Type</label>
            <div class="type-pills">
                <span class="type-pill active" onclick="selectPipelineType('generic',this)" data-type="generic">Generic Group</span>
                <span class="type-pill" onclick="selectPipelineType('client',this)" data-type="client">Client</span>
                <span class="type-pill" onclick="selectPipelineType('acquisition',this)" data-type="acquisition">Acquisition</span>
            </div>
            <label>Name</label>
            <input type="text" id="pipeline-name" value="${suggestedName}" placeholder="Generic A">
            <label>Domains (one per line)</label>
            <textarea id="pipeline-domains" placeholder="domain1.info&#10;domain2.info&#10;domain3.info"></textarea>
            <label>Sender</label>
            <select id="pipeline-sender">
                <option value="sean_reynolds">Sean Reynolds</option>
                <option value="aidan_hutchinson">Aidan Hutchinson</option>
                <option value="lars_matthys">Lars Matthys</option>
            </select>
            <button class="btn-start" onclick="startPipeline()">Start Pipeline</button>
        </div>
    `;
    document.body.appendChild(overlay);
}

function selectPipelineType(type, el) {
    newPipelineType = type;
    document.querySelectorAll('.type-pill').forEach(p => p.classList.remove('active'));
    el.classList.add('active');
    // Auto-update name suggestion for generic
    if (type === 'generic') {
        fetch('/api/next-generic-name').then(r => r.json()).then(d => {
            document.getElementById('pipeline-name').value = d.name || '';
        }).catch(() => {});
    } else {
        document.getElementById('pipeline-name').value = '';
        document.getElementById('pipeline-name').placeholder = type === 'client' ? 'Client Name' : 'Group Name';
    }
    // Default sender by type
    const sel = document.getElementById('pipeline-sender');
    sel.value = type === 'acquisition' ? 'aidan_hutchinson' : 'sean_reynolds';
}

async function startPipeline() {
    const name = document.getElementById('pipeline-name').value.trim();
    const domains = document.getElementById('pipeline-domains').value.trim();
    const sender = document.getElementById('pipeline-sender').value;
    if (!name || !domains) { alert('Name and domains are required'); return; }
    try {
        const resp = await fetch('/api/setup-pipeline/create', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ type: newPipelineType, name, domains, sender })
        });
        const data = await resp.json();
        if (data.error) { alert('Error: ' + data.error); return; }
        document.getElementById('pipeline-modal').remove();
        loadPipelines();
    } catch(e) { alert('Failed: ' + e.message); }
}

async function retryPipeline(id) {
    event.stopPropagation();
    await fetch('/api/setup-pipeline/retry', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ pipeline_id: id })
    });
    loadPipelines();
}

function showPipelineDetail(id) {
    // Expanded view — shows full step breakdown
    fetch('/api/setup-pipeline/' + id).then(r => r.json()).then(p => {
        if (p.error) return;
        const overlay = document.createElement('div');
        overlay.className = 'pipeline-modal-overlay';
        overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };
        const stepsHtml = p.steps.map(s => {
            const icon = s.status === 'completed' ? '&#10003;' :
                         s.status === 'running' ? '&#9679;' :
                         s.status === 'failed' ? '&#10007;' : '&#9675;';
            const color = s.status === 'completed' ? 'var(--accent)' :
                          s.status === 'running' ? 'var(--accent)' :
                          s.status === 'failed' ? 'var(--red)' : 'var(--text-muted)';
            const timing = s.completed_at && s.started_at
                ? `${Math.round((new Date(s.completed_at) - new Date(s.started_at)) / 1000)}s`
                : s.status === 'running' ? 'running...' : '';
            const errorLine = s.error ? `<div style="color:var(--red);font-size:11px;margin-top:4px;">${s.error}</div>` : '';
            const retryBtn = s.status === 'failed'
                ? `<button onclick="retryPipeline('${p.id}');this.closest('.pipeline-modal-overlay').remove();" style="margin-top:4px;font-size:11px;padding:3px 8px;border-radius:6px;border:1px solid var(--red);color:var(--red);background:transparent;cursor:pointer;">Retry</button>`
                : '';
            return `<div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border-light);">
                <span style="color:${color};font-size:14px;width:20px;text-align:center;">${icon}</span>
                <span style="flex:1;font-size:13px;color:var(--text-primary);">${s.name}</span>
                <span style="font-size:12px;color:var(--text-muted);font-family:var(--font-mono);">${s.progress}/${s.total}</span>
                <span style="font-size:11px;color:var(--text-muted);width:60px;text-align:right;">${timing}</span>
            </div>
            ${errorLine}${retryBtn}`;
        }).join('');
        overlay.innerHTML = `<div class="pipeline-modal">
            <h3>${p.name} <span style="font-size:12px;font-weight:400;color:var(--text-muted);">${p.type}</span></h3>
            <div class="pill-stepper" style="margin-bottom:16px;">${renderPipelineSteps(p.steps)}</div>
            ${stepsHtml}
        </div>`;
        document.body.appendChild(overlay);
    });
}
```

- [ ] **Step 5: Add pipeline loading to the main load flow**

Add the `loadPipelines` function and wire it into the existing page load:

```javascript
let pipelinePollInterval = null;

async function loadPipelines() {
    try {
        const resp = await fetch('/api/setup-pipelines');
        const data = await resp.json();
        renderPipelines(data.pipelines || []);
        // Poll if any are running
        const hasRunning = (data.pipelines || []).some(p => p.status === 'running');
        if (hasRunning && !pipelinePollInterval) {
            pipelinePollInterval = setInterval(loadPipelines, 5000);
        } else if (!hasRunning && pipelinePollInterval) {
            clearInterval(pipelinePollInterval);
            pipelinePollInterval = null;
        }
    } catch(e) { console.error('Pipeline load error:', e); }
}
```

Then add `loadPipelines();` call to the existing `loadAll()` / page load function (where the other `/api/*` fetches happen, around line 530).

- [ ] **Step 6: Commit**

```bash
git add dashboard.html
git commit -m "feat: add pipeline UI with pill stepper, new pipeline modal, and detail view"
```

---

### Task 6: Sync + Final Verification

**Files:**
- Modify: `web/public/index.html`

- [ ] **Step 1: Copy dashboard.html to Vercel**

```bash
cp dashboard.html web/public/index.html
```

- [ ] **Step 2: Deploy to Vercel**

```bash
cd web && npx vercel --prod
```

- [ ] **Step 3: Test end-to-end**

1. Open the dashboard
2. Click "+ New Pipeline"
3. Verify type pills work, generic name auto-suggests
4. Enter test domains, click Start
5. Verify card appears with pill stepper
6. Verify status line updates with progress
7. Click card to see expanded detail view
8. Verify polling stops when pipeline completes

- [ ] **Step 4: Commit sync**

```bash
git add web/public/index.html
git commit -m "chore: sync dashboard to Vercel deployment"
```

---

## Self-Review

**Spec coverage:**
- Supabase table: Task 1 ✓
- Pipeline engine (6 steps): Tasks 2-3 ✓
- Error handling + auto-retry: Tasks 2-3 (each step has try/except + threshold) ✓
- Dashboard API endpoints: Task 4 ✓
- Frontend pill stepper: Task 5 ✓
- Mini card + expanded view: Task 5 ✓
- "+ New Pipeline" form: Task 5 ✓
- Auto-suggest generic name: Task 4 (next_generic_name) + Task 5 (modal) ✓
- Fuzzy tag matching: Task 4 (build_pipeline_config uses sl_find_or_create_tag) ✓
- Client search/create: Task 4 (build_pipeline_config) ✓
- Resume on restart: Task 4 (resume_running_pipelines in main) ✓

**Placeholder scan:** No TBDs, TODOs, or vague steps found.

**Type consistency:**
- `pipeline_id` used consistently (not `pid` in functions, only in URL parsing)
- `config["mailbox_ids"]`, `config["smartlead_account_ids"]` consistent across steps
- `_update_step`, `_start_step`, `_finish_step`, `_fail_step`, `_progress` used consistently
- `STEP_NAMES` array order matches `_STEP_FUNCTIONS` array order
- `store.create_setup_pipeline` / `store.get_setup_pipeline` / `store.update_setup_pipeline` / `store.list_setup_pipelines` — all match db.py function names
