# Autonomous Infrastructure Dashboard

## Overview
Transform the email-infra dashboard from a monitoring-only tool into a fully autonomous infrastructure management system. The dashboard will handle new client setup, automated inbox replacement based on warmup reputation, billing-optimized removal scheduling, and weekly placement testing — all without leaving the browser.

## Architecture
Single-process Python backend (`dashboard.py`) with a background automation thread. Pipeline state persisted to JSON files for crash recovery. Frontend is vanilla HTML/CSS/JS (existing pattern). All operations execute via ZapMail, SmartLead, and registrar APIs.

---

## 1. Automation Engine (Background Thread)

### Reputation Monitor (runs every 4 hours)
- Pull all SmartLead accounts with warmup details
- Flag any inbox with warmup reputation < 99
- When flagged, flag the entire domain (domain-level rollup — all 3 inboxes per domain)
- Skip domains that already have an active replacement pipeline
- Log all flag events with timestamps

### Replacement Scheduler (billing-aware, volume-protected)
When a domain is flagged:
1. Query ZapMail subscriptions (`GET /v2/subscriptions`) and map mailboxes to subscriptions (`GET /v2/subscriptions/{id}/mailboxes`) to determine next renewal date
2. Start replacement pipeline immediately regardless of billing cycle (new domain begins warming)
3. **Volume protection rule:** Never remove old inboxes until replacements are fully warmed (reputation 99+) and ready to take their place. Sending volume is more important than billing optimization.
4. Removal timing (only after replacements are ready):
   - If replacement is ready AND old renewal is within 7 days → schedule `remove-on-renewal`, let subscription expire naturally
   - If replacement is ready AND old renewal just happened → schedule `remove-on-renewal` before next charge, swap immediately
   - If replacement is NOT ready AND old renewal is imminent → **let old inboxes renew**. Pay for the overlap month. Losing volume for up to 14 days is worse than one extra billing cycle.
   - The system always calculates: "Will the replacement be warmed before the old inbox renews?" If no → let the old inbox renew and keep sending.

### Weekly Placement Tests (every Monday)
- Trigger `POST /v2/placement-test/purchase` for a sample of inboxes per client
- Store results, surface on dashboard
- Use placement data as supplementary health signal

### Pipeline State Persistence
- Directory: `pipelines/`
- One JSON file per active pipeline run
- Fields: pipeline type (`new_setup` | `replacement`), client name, domain list, current step per domain, timestamps, errors, billing alignment data
- On server restart, background thread reads `pipelines/` and resumes any in-progress pipelines

---

## 2. Replacement Pipeline (Fully Autonomous)

### Trigger
Warmup reputation drops below 99 on any inbox → entire domain flagged.

### Steps

**Step 1: Claim domain from inventory**
- Pull available domains from THT Google Spreadsheet (via `sheets.py`)
- Select the oldest domain (most aged = best reputation potential)
- Mark as claimed in the spreadsheet
- If inventory < 5 domains remaining → surface dashboard alert

**Step 2: DNS/Nameserver setup**
- Set nameservers via registrar API (Spaceship or Porkbun, based on where domain was purchased — determined from spreadsheet metadata)
- Verify propagation via `POST /v2/domains/verify-nameservers`
- Connect to ZapMail via `POST /v2/domains/connect-domain`

**Step 3: Create mailboxes**
- Create 3 inboxes per domain via `POST /v2/mailboxes`: s.reynolds, sean.r, sean.reynolds
- On failure, auto-retry via `POST /v2/mailboxes/retry-failed`

**Step 4: Upload profile photos**
- `PUT /v2/mailboxes` with `profilePicture` field pointing to hosted headshot URL
- Verify `profileUrl` returned in response
- No manual step required

**Step 5: Tag & configure**
- Assign client tag to domain via `POST /v2/domains/assign-tags`
- Set forwarding URL via `POST /v2/domains/forwarding`

**Step 6: Export to SmartLead**
- `POST /v2/exports/mailboxes` to push to SmartLead
- Poll `GET /v2/export/status` until complete
- If accounts don't appear in SmartLead within 10 minutes, re-export automatically

**Step 7: Enable warmup**
- Configure warmup settings via SmartLead internal API (existing `save_warmup` logic)
- Assign SmartLead client tag to new accounts

**Step 8: Wait for warmup (14 days)**
- Background thread monitors warmup reputation daily
- Pipeline stays in `warming` state until reputation reaches 99+

**Step 9: Campaign removal safeguard**
- Before removing old inboxes, check all active campaigns for those inboxes
- If any old inbox is in an active campaign → **do not auto-remove**
- Surface alert on dashboard: "{email} is flagged for removal but is still in {N} active campaigns: [campaign names]"
- Provide per-campaign "Remove from campaign" buttons and "Remove from all campaigns" one-click option
- Only proceed with deletion after inbox is removed from all campaigns

**Step 10: Delete old infrastructure**
- Remove old email accounts from SmartLead entirely (delete, not just deactivate)
- Schedule old ZapMail mailboxes for removal at renewal (`POST /v2/mailboxes/remove-on-renewal`)
- Once subscription expires, clean up domain via `DELETE /v2/domains`
- Update pipeline state to `complete`

---

## 3. New Client Setup From Dashboard

### UI: Setup Form
- Client name (text input)
- Number of domains needed (number input, or auto-calculate from desired daily send volume)
- Domain selection from inventory (pulled from THT spreadsheet, showing domain name, age, registrar)
- Forwarding URL (text input)

### Pipeline
Identical to replacement pipeline steps 1-7:
Claim domains → DNS → connect ZapMail → create mailboxes → upload photos → tag & configure → export to SmartLead → enable warmup

### Dashboard Display
Real-time step-by-step progress tracker per domain, shown in a dedicated "Active Pipelines" section.

---

## 4. Dashboard Operational Enhancements

### New Data Sources (from unused ZapMail endpoints)

| Endpoint | Display Location | Purpose |
|----------|-----------------|---------|
| `GET /v2/wallet/balance` | Dashboard header | Wallet balance, alert when low |
| `GET /v2/domains/{id}/health-score` | Detail panel per domain | ZapMail-side domain reputation |
| `GET /v2/subscriptions` + `GET /v2/subscriptions/{id}/mailboxes` | Detail panel per inbox | Real billing/renewal dates |
| `GET /v2/export/status` | Pipeline progress tracker | Live export progress |
| `POST /v2/mailboxes/retry-failed` | Pipeline error handling | Auto-retry failed provisioning |
| `DELETE /v2/domains/unused` | Maintenance | Automated cleanup of empty domains |
| Placement test endpoints | New section or tab | Weekly inbox placement results per client |

### Profile Photo Automation
- `PUT /v2/mailboxes` with `profilePicture` field
- Headshot image (`headshots/sean_reynolds.png`) must be hosted at a publicly accessible URL (e.g., uploaded to the Render static files, S3, or any CDN). The pipeline references this URL for every new mailbox.
- Eliminates the only remaining manual step in the pipeline

---

## 5. Dashboard UI Changes

### Header
- Wallet balance indicator (green/yellow/red based on threshold)
- Active pipelines count badge

### SmartLead Tab Additions
- **"New Client Setup"** button → opens setup form
- **"Active Pipelines"** section → step-by-step progress for all running pipelines
- **"Pending Removals"** section in client detail panel → flagged inboxes with campaign removal buttons

### Client Detail Panel Additions
- Real billing/renewal dates per inbox (from subscriptions API)
- ZapMail domain health score alongside SmartLead reputation
- Pending removal alerts with campaign info and action buttons
- Pipeline progress for any active replacement for this client

### New: Placement Tests Section
- Weekly placement test results per client
- Inbox-level placement rates (inbox vs spam)
- Trend over time

---

## 6. Safeguards

| Safeguard | Trigger | Action |
|-----------|---------|--------|
| Low domain inventory | < 5 domains in THT spreadsheet | Dashboard alert |
| Low wallet balance | Below $50 (configurable in dashboard settings) | Dashboard alert, block pipeline start |
| Inbox in active campaign | Flagged for removal but in active campaigns | Alert + manual removal buttons, blocks auto-deletion |
| Billing alignment | Domain flagged for replacement | Schedule removal at renewal via ZapMail API, never double-pay |
| Export retry | Accounts don't appear in SmartLead within 10 min | Auto re-export |
| Mailbox creation failure | ZapMail provisioning fails | Auto retry via `retry-failed` endpoint |
| Pipeline crash recovery | Server restart with in-progress pipelines | Resume from persisted state in `pipelines/` directory |

---

## 7. Endpoints (New)

### Pipeline Execution
- `POST /api/pipeline/new-client` — Start new client setup pipeline
- `POST /api/pipeline/replacement` — Manually trigger replacement for a client/domain
- `GET /api/pipeline/active` — List all active pipelines with current step
- `GET /api/pipeline/{id}` — Detailed status for a specific pipeline

### Campaign Safeguard
- `GET /api/inbox/{email}/campaigns` — List active campaigns containing this inbox
- `POST /api/inbox/{email}/remove-from-campaign` — Remove inbox from specific campaign
- `POST /api/inbox/{email}/remove-from-all-campaigns` — Remove inbox from all campaigns

### Operational
- `GET /api/wallet` — ZapMail wallet balance
- `GET /api/placement-tests` — Latest placement test results
- `GET /api/domain/{id}/health` — ZapMail domain health score
- `GET /api/subscriptions` — Billing/renewal data for all domains

---

## 8. Implementation Approach

Follow Ian McGahren method throughout: analyze existing code before every change, report on current state, then make surgical edits. Code review at every checkpoint.

### Phases (each is a separate implementation chunk)
1. **Pipeline engine & state persistence** — background thread, JSON state files, resume logic
2. **Replacement pipeline** — steps 1-10, billing-aware scheduling
3. **New client setup** — dashboard form, pipeline execution
4. **ZapMail operational endpoints** — wallet, subscriptions, domain health, profile photos
5. **Campaign safeguard** — inbox-in-campaign detection, removal UI
6. **Placement tests** — weekly scheduling, results display
7. **Dashboard UI** — all new panels, forms, progress trackers, alerts

### Quality Gates
- Code review after each phase
- Analyze before changing (read → report → edit)
- SOLID/DRY/KISS/YAGNI audit at each checkpoint
- Test each pipeline step against live APIs before proceeding
