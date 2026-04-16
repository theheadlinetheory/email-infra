# Dashboard Pipeline — Self-Service Infrastructure Setup

## Problem

Setting up email infrastructure (generic groups, client infra, acquisition groups) requires running manual scripts step-by-step, with no visibility into progress, no error recovery UI, and no way to walk away while it runs. The process should be a single action from the dashboard with live progress tracking.

## Solution

Add a pipeline execution engine to the dashboard that runs infrastructure setup as a background state machine with 6 visible steps, persisted to Supabase. The UI shows progress via a horizontal pill stepper (matching the CRM's pipeline step style) with a status line for granular within-step progress.

## Pipeline Types

### Generic Group
- **Trigger:** "+ New Pipeline" → Generic Group
- **Name:** Auto-suggested next available letter (e.g. "Generic J" if A-I exist). Checks SmartLead clients + active pipelines to avoid collisions. User can override.
- **Sender:** Sean Reynolds (s.reynolds, sean.r, sean.reynolds)
- **Domains:** Pasted into textarea, one per line
- **Tags:** Zapmail + Generic X + warmup start date
- **SmartLead client:** Auto-created

### Client
- **Trigger:** "+ New Pipeline" → Client
- **Name:** Fuzzy-search dropdown against existing SmartLead clients. If match found, selects existing client ID and tag. If no match, typed name creates new SmartLead client + tag.
- **Sender:** Sean Reynolds (default for client type)
- **Domains:** Pasted into textarea
- **Tags:** Zapmail + Client Name + warmup start date
- **SmartLead client:** Existing (matched) or auto-created

### Acquisition
- **Trigger:** "+ New Pipeline" → Acquisition
- **Name:** Manual entry (group name)
- **Sender:** Selectable — Aidan Hutchinson or Lars Matthys
- **Domains:** Pasted into textarea
- **Tags:** Acquisition Inbox + Zapmail + warmup start date + group name
- **SmartLead client:** Auto-created

## 6 Pipeline Steps

Each step is a pill in the stepper. Steps run sequentially.

### Step 1: Connect Domains
- Input: list of domain names from config
- Action: Call `zm_connect_domain_single()` for each domain not already in Zapmail
- Progress: N/total domains connected
- Skip logic: domains already in Zapmail are skipped (counted as done)

### Step 2: Create Inboxes
- Input: connected domain IDs from Zapmail
- Action: Check workspace mailbox quota, buy addon slots if needed via `zm_buy_addon_mailboxes()`, then call `zm_create_mailboxes()` per domain
- Progress: N/total mailboxes created
- Skip logic: domains with 3+ existing mailboxes are skipped
- Error: "Insufficient wallet balance" → mark step failed with message, user adds funds and retries

### Step 3: Profile Photos
- Input: all mailbox IDs
- Action: Batch `zm_put("/v2/mailboxes", ...)` with profile photo URL (Sean Reynolds for generic/client, sender-specific for acquisition)
- Progress: N/total photos set
- Batch size: 50 per API call

### Step 4: SmartLead Export
- Input: all mailbox IDs
- Action: `zm_export_mailboxes(apps=["SMARTLEAD"], mailbox_ids=...)` then poll SmartLead to verify accounts appeared
- Progress: N/expected accounts found in SmartLead
- Auto-retry: if accounts missing after 3 min, re-export missing domains. Up to 5 attempts.
- Note: Export works even if mailboxes are still IN_PROGRESS status in Zapmail

### Step 5: Tag & Assign
- Input: SmartLead account IDs (found in step 4)
- Action: `sl_tag_accounts_bulk()` with tag IDs + client_id per group
- Tags resolved using `sl_find_or_create_tag()` — fuzzy matches existing tags before creating new ones
- Progress: N/total accounts tagged

### Step 6: Enable Warmup
- Input: SmartLead account IDs
- Action: POST to SmartLead warmup endpoint per account
- Config: warmup_enabled=true, total_warmup_per_day=30, daily_rampup=2, reply_rate_percentage=30
- Progress: N/total accounts with warmup enabled

## Data Model

### Supabase table: `pipelines`

```sql
create table if not exists pipelines (
    id uuid primary key default gen_random_uuid(),
    name text not null,
    type text not null check (type in ('generic', 'client', 'acquisition')),
    config jsonb not null default '{}',
    status text not null default 'pending' check (status in ('pending', 'running', 'completed', 'failed', 'paused')),
    current_step int not null default 0,
    steps jsonb not null default '[]',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
alter table pipelines disable row level security;
```

### Config shape (jsonb)

```json
{
  "domains": ["domain1.info", "domain2.info"],
  "sender": "sean_reynolds",
  "group_name": "Generic F",
  "smartlead_client_id": 352787,
  "tag_ids": {
    "group": 370966,
    "zapmail": 262254,
    "date": 370969
  },
  "mailbox_ids": [],
  "smartlead_account_ids": {},
  "profile_photo_url": "https://...sean_reynolds.png"
}
```

### Steps array shape (jsonb)

```json
[
  {
    "name": "Connect Domains",
    "status": "completed",
    "progress": 16,
    "total": 16,
    "error": null,
    "started_at": "2026-04-15T20:00:00Z",
    "completed_at": "2026-04-15T20:00:30Z"
  }
]
```

Step statuses: `pending` → `running` → `completed` | `failed`

## Backend Architecture

### New file: `pipeline_engine.py`

Separate from existing `pipeline.py` (monitor/health system). Contains:

- `create_pipeline(type, name, config)` — validates, inserts into Supabase, initializes 6 steps as pending, returns pipeline ID
- `run_pipeline(pipeline_id)` — main executor. Reads pipeline from Supabase, loops through steps, calls step functions, updates Supabase after each individual operation (not just per step)
- `retry_step(pipeline_id, step_index)` — resets step to running, re-executes from where progress left off
- Step functions: `step_connect_domains(pipeline, config)`, `step_create_inboxes(...)`, etc.

### Error handling

- Each individual operation (single domain connect, single mailbox create, etc.) is wrapped in try/except
- Transient errors (timeouts, 429, 500, JSONDecodeError): 3 retries with exponential backoff
- If a single operation fails after retries: log error, continue with remaining operations
- Step marked `failed` if failure count > 20% of total operations, OR if a critical error occurs (e.g. "Insufficient wallet balance", auth failures)
- Failed step stores error details: which domains/accounts failed and why
- Pipeline stops at failed step — does not proceed to next step

### Dashboard integration

`dashboard.py` additions:
- Import `pipeline_engine`
- On server start: check for any pipelines with status `running` and resume them in background threads
- API endpoints:
  - `POST /api/pipeline/create` — create + start pipeline in background thread
  - `GET /api/pipeline/{id}` — return pipeline row (status, steps, config)
  - `GET /api/pipelines` — list all pipelines ordered by created_at desc
  - `POST /api/pipeline/{id}/retry` — retry failed step

## Frontend

### Mini card view (in section grid)

Pipeline cards appear alongside existing group/client cards in their respective sections.

```
┌─────────────────────────────────────────────────┐
│ Generic F                                       │
│ ✓ Connect ── ✓ Inboxes ── ● Photos ── ○ ── ○ ──○│
│ Setting profile photos... 96/192                │
└─────────────────────────────────────────────────┘
```

- Green filled pill + checkmark = completed
- Green filled pill + dot = running (current step)
- Gray outline pill = pending
- Red filled pill = failed
- Pills connected by horizontal lines (solid green for completed, gray for pending)
- Status line below: "{step name}... {progress}/{total}" or "Complete" or "Failed: {error summary}"
- Polls every 5 seconds while status is `running`

### Expanded card view (click to open)

Full step breakdown:

```
Generic F — Pipeline Details
─────────────────────────────────────────
✓ Connect Domains          16/16    8s
✓ Create Inboxes          192/192   45s
● Profile Photos           96/192   running...
○ SmartLead Export           —
○ Tag & Assign               —
○ Enable Warmup              —

Current: Setting profile photos...
  ✓ exteriorcarepros.info (3 photos)
  ✓ exteriorgroundscare.info (3 photos)
  → exteriorgroundscontractors.info...
```

If a step failed:
```
✗ SmartLead Export          0/192   failed
  Error: Zapmail returned empty export ID
  [Retry Step]
```

### "+ New Pipeline" form

Button at top of dashboard opens a modal/drawer:

1. **Type pills:** Generic Group | Client | Acquisition
2. **Name:**
   - Generic: auto-filled "Generic J" (next available), editable
   - Client: fuzzy-search input against SmartLead clients, can type new name
   - Acquisition: free text
3. **Domains:** textarea, one domain per line
4. **Sender:** pre-filled based on type, selectable for acquisition
5. **[Start Pipeline]** button

On submit: POST to `/api/pipeline/create`, modal closes, card appears in section with step 1 running.

## What This Does NOT Include

- Domain purchasing (stays external on Spaceship for now)
- DNS verification polling (Zapmail handles this internally)
- Automated scheduling / cron-based pipeline triggers
- Pipeline deletion / cleanup UI
- Batch pipeline creation (one at a time)
