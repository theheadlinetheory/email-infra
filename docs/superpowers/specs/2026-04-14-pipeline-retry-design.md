# Pipeline Retry/Resume from Frontend

**Date:** 2026-04-14
**Status:** Approved

## Problem

When pipeline steps fail (timeouts, API errors, Zapmail outages), the only recovery path is CLI intervention. Many "errors" are just timing-based — domains take hours to activate, exports take time to propagate — but the engine gives up after one timeout cycle and surfaces a false error. This creates unnecessary manual work and blocks generic group creation.

## Goals

1. Pipeline engine retries automatically before surfacing any error
2. When a step truly fails, the dashboard shows per-domain status with retry/skip controls
3. Slack notification fires only when manual intervention is genuinely needed
4. No false alarms — timing-based delays are handled transparently

## Non-Goals

- Full pipeline management admin panel
- Editing pipeline configuration after creation
- Domain purchasing from the dashboard

---

## 1. Automatic Retry with Backoff (Pipeline Engine)

### Retry Policies

Each step has a retry policy defining max attempts and wait times between attempts:

| Step | Max Attempts | Wait Between Attempts |
|------|-------------|----------------------|
| `connect_zapmail` | 3 | 30-min poll window per attempt |
| `create_mailboxes` | 3 | 5 min, then 10 min |
| `export_to_smartlead` | 3 | 15-min poll window per attempt |
| `enable_warmup` | 3 | 2 min, then 5 min |
| All other steps | 2 | 2 min |

### Per-Domain State Additions

Each domain info dict in the pipeline gains:

```python
"attempt": 1,            # current attempt number
"max_attempts": 3,       # from retry policy
"last_attempt_at": "",   # ISO timestamp
"step_history": []       # list of {"attempt": N, "result": "error", "message": "...", "at": "..."}
```

### Behavior

- Pipeline status stays `"running"` during automatic retries — NOT `"error"`
- A new field `"retry_info"` on the pipeline tracks current attempt state for frontend display
- Only after ALL attempts are exhausted on ANY domain does the pipeline set `status = "error"`
- Step executors themselves do not change — the retry wrapper calls them repeatedly

### Step Categories

Steps fall into two categories for retry purposes:

**Per-domain steps** (iterate over each domain individually, skip already-complete):
`set_dns`, `connect_zapmail`, `create_mailboxes`

**Batch steps** (operate on all domains at once, then mark results):
`claim_domains`, `upload_photos`, `tag_and_configure`, `export_to_smartlead`, `enable_warmup`, `smartlead_tags`, `export_csv`, `gcal_rotation`

For per-domain steps, retry only re-processes failed domains (the executor already skips complete ones).
For batch steps, retry re-runs the entire operation but only updates status on domains that were pending/errored.

### Implementation

Add a `_run_step_with_retry()` wrapper in `pipeline.py` that:

1. Looks up the retry policy for the current step
2. Calls the step executor
3. If it fails, checks which domains errored
4. For errored domains: resets `step_status` to "pending", increments `attempt`, waits per policy
5. Re-calls the step executor (per-domain steps skip complete domains; batch steps re-run but only update incomplete domains)
6. Repeats until max attempts reached or all domains succeed
7. Saves pipeline state after each attempt

`run_pipeline_steps()` calls `_run_step_with_retry()` instead of calling executors directly.

### Step Executor Adjustments

Some step executors use `_mark_all_domains_complete()` which overwrites all domains regardless of status. These need to change to only mark domains that are `"pending"` or `"error"` as `"complete"`, preserving already-complete domains. This ensures retry only touches what's broken.

Affected executors:
- `step_upload_photos`
- `step_tag_and_configure`
- `step_smartlead_tags`
- `step_export_csv`
- `step_gcal_rotation`

Change `_mark_all_domains_complete()` to skip domains already marked complete for the current step.

---

## 2. Frontend — Expanded Pipeline Card

### Running State (retry in progress)

- Step progress bar shows current step in purple
- Step label includes attempt info: "Connect ZapMail (attempt 2/3)"
- Domain table visible below the step bar, showing per-domain status updating in real-time

### Error State (all retries exhausted)

- Step progress bar shows current step in red
- Domain table expanded by default
- Table columns: Domain | Status | Error | Attempts

Status badges:
- Green "Complete" — domain passed this step
- Red "Error" — domain failed after all retries
- Gray "Pending" — domain hasn't been attempted yet
- Purple "Running" — domain is being processed right now

### Action Buttons (error state only)

**"Retry Failed" (primary, teal)**
- Retries only the domains that errored on the current step
- Resets their attempt counters
- Pipeline goes back to "running"

**"Skip Step" (subtle link, requires confirmation)**
- Confirm dialog: "This will skip [step name] and move to the next step. Domains that failed this step may have incomplete setup. Are you sure?"
- Marks all domains as complete for the skipped step
- Advances pipeline to next step
- Logs the skip for audit

### Auto-Polling

- Any pipeline with status `"running"` → frontend polls `GET /api/pipeline/{id}` every 10 seconds
- Updates domain table and step progress bar in place
- Stops polling when pipeline reaches `"complete"` or `"error"`

---

## 3. Backend API Additions

### `POST /api/pipeline/retry`

```json
{
  "pipeline_id": "20260414-103000-abc123",
  "domains": ["example1.com", "example2.com"]  // empty array = retry all failed
}
```

Response:
```json
{
  "pipeline_id": "...",
  "status": "running",
  "retrying_domains": ["example1.com", "example2.com"]
}
```

Behavior:
1. Load pipeline from Supabase
2. Validate pipeline is in "error" status
3. Reset specified failed domains: `step_status = "pending"`, `attempt = 1`, clear `error`
4. Set pipeline status to "running"
5. Save to Supabase
6. Kick off `run_pipeline_steps` in background thread
7. Return immediately

### `POST /api/pipeline/skip-step`

```json
{
  "pipeline_id": "20260414-103000-abc123"
}
```

Response:
```json
{
  "pipeline_id": "...",
  "skipped_step": "connect_zapmail",
  "next_step": "create_mailboxes",
  "warning": "Step skipped — some domains may have incomplete setup"
}
```

Behavior:
1. Load pipeline from Supabase
2. Mark all domains as complete for the current step
3. Advance `current_step` to the next step in the sequence
4. Set pipeline status to "running"
5. Log the skip to `step_history` on each domain
6. Save to Supabase
7. Kick off `run_pipeline_steps` in background thread
8. Return immediately

### Enhanced `GET /api/pipeline/{id}`

Already returns the full pipeline dict. Frontend just needs to use the per-domain data that's already there, plus the new `retry_info` and `attempt` fields.

---

## 4. Slack Notification on True Failure

### When It Fires

Only when `run_pipeline_steps` sets `status = "error"` after all automatic retries are exhausted. No other condition triggers a notification.

### Message Content

```
Pipeline Error: Generic F
Step: Connect ZapMail (3/3 attempts exhausted)

Failed domains:
  - example1.com: Domain did not become active within 30 min
  - example2.com: Domain did not become active within 30 min

14/16 domains completed successfully.

Dashboard: <link>
```

### Implementation

- Create a `#tht-infra-alerts` Slack channel with Aidan and Lars using Slack MCP tools
- Set up an incoming webhook for that channel
- Add `SLACK_WEBHOOK_URL` to `.env`
- Add `notify_pipeline_error(pipeline)` function in `pipeline.py`
- Called from `run_pipeline_steps` at the single point where `status = "error"` is set
- Simple `requests.post()` to the webhook URL with the formatted message

---

## File Changes Summary

| File | Changes |
|------|---------|
| `pipeline.py` | Add retry policies, `_run_step_with_retry()` wrapper, per-domain attempt tracking, `notify_pipeline_error()`, adjust `_mark_all_domains_complete()` |
| `dashboard.py` | Add `api_pipeline_retry()`, `api_pipeline_skip_step()`, wire up POST routes |
| `dashboard.html` | Expand pipeline card with domain table, retry/skip buttons, auto-polling, attempt display |
| `.env` | Add `SLACK_WEBHOOK_URL` |
