# Inbox History Tracking — Design Spec

## Goal

Track every change to every SmartLead inbox (client_id, tags, campaign assignments) so issues like lost Borja/Canopy accounts can be traced back to the exact operation that caused them.

## Architecture

### Storage

Supabase table `inbox_history`:

| Column | Type | Description |
|--------|------|-------------|
| id | bigint (auto) | Primary key |
| account_id | integer | SmartLead email account ID |
| email | text | from_email at time of event |
| event_type | text | `client_change`, `tag_change`, `campaign_assign`, `campaign_unassign`, `warmup_change`, `delete` |
| old_value | jsonb | Previous state (null for first event) |
| new_value | jsonb | New state |
| source | text | `dashboard`, `snapshot`, `script` |
| created_at | timestamptz | Auto-set to now() |

Index on `account_id` and `created_at` for fast per-inbox lookups.

### Real-Time Logging (db.py)

Single function: `log_inbox_event(account_id, email, event_type, old_value, new_value, source="dashboard")`

Called at every dashboard mutation point:
- `assign_accounts_to_client()` — logs `client_change` with old/new client_id
- `api_assign_group_campaign()` — logs `campaign_assign` with campaign name/id
- Campaign unassign — logs `campaign_unassign`
- Delete flow — logs `delete`
- Any `save-management-details` call — logs `tag_change` and/or `client_change`

Batch helper: `log_inbox_events(events)` for bulk operations (list of dicts).

### Daily Snapshot Comparison

Function: `snapshot_all_inboxes()` in dashboard.py

- Pulls all SmartLead accounts (client_id, tags via details endpoint is too slow — use cached tag data from dashboard's existing fetch)
- Compares against last snapshot (stored in `state` table as `inbox_snapshot`)
- Logs diffs as `source="snapshot"` events
- Triggered on dashboard startup and via `/api/snapshot`

### UI (Deferred)

Per-inbox: clock icon in detail panel, opens modal timeline.
Global: subtle link in footer, opens same modal unfiltered.
Not implementing now — data layer first.

## Mutation Points in dashboard.py

1. `assign_accounts_to_client()` (~line 279) — client_id change
2. `api_assign_group_campaign()` (~line 1472) — campaign assign
3. Campaign unassign endpoint — campaign unassign
4. Delete flow (SSE pipeline) — delete event
5. `api_pipeline_new_client()` (~line 1723) — new client setup creates accounts
6. `api_pipeline_new_acquisition()` (~line 1757) — acquisition setup

## Non-Goals

- Real-time websocket updates
- Undo/rollback functionality
- UI implementation (deferred)
