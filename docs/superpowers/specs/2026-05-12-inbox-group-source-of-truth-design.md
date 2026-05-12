# Inbox Group Source of Truth

**Date:** 2026-05-12
**Problem:** SmartLead has no concept of group ownership or assignment intent. The dashboard rebuilds group state from SmartLead on every API call, so when SmartLead's state drifts (e.g. Generic K accounts in Kay's campaign), nothing catches it.
**Solution:** Supabase-backed source of truth that tracks every inbox group's identity, assignment, campaigns, and tags — with an append-only audit log for history.

## Tables

### `inbox_groups` (live state)

One row per group letter + batch. This is the single source of truth the dashboard reads.

| Column | Type | Nullable | Description |
|---|---|---|---|
| `id` | serial | PK | Auto-increment |
| `group_letter` | text | not null | "A", "B", "K", "G2", etc. |
| `batch` | int | not null, default 1 | Which generation of inboxes (letter reused, batch increments) |
| `smartlead_client_id` | int | not null | SmartLead client container ID |
| `smartlead_client_name` | text | not null | SmartLead client name (e.g. "Generic K", "Kay's Landscaping B") |
| `assigned_client` | text | nullable | null = generic/unassigned. "Kay's Landscaping" = assigned |
| `role` | text | not null, default 'generic' | "generic", "A", or "B" |
| `status` | text | not null, default 'warming' | "warming", "ready", "active", "retired" |
| `account_ids` | jsonb | not null, default '[]' | SmartLead email account IDs |
| `account_emails` | jsonb | not null, default '[]' | Email addresses (for quick lookup) |
| `domains` | jsonb | not null, default '[]' | Unique domains in the group |
| `campaign_ids` | jsonb | not null, default '[]' | Campaign IDs this group SHOULD be on |
| `tag_ids` | jsonb | not null, default '[]' | SmartLead tag IDs |
| `daily_capacity` | int | not null, default 0 | Estimated emails/day |
| `warmup_started` | date | nullable | When warmup began |
| `warmup_ready` | date | nullable | When warmup expected/confirmed complete |
| `drift_flags` | jsonb | not null, default '[]' | List of detected mismatches |
| `updated_at` | timestamptz | not null, default now() | Last modified |

**Unique constraint:** `(group_letter, batch)`

### `inbox_group_history` (audit log)

Append-only. Never updated, never deleted. One row per change event.

| Column | Type | Nullable | Description |
|---|---|---|---|
| `id` | serial | PK | Auto-increment |
| `group_id` | int | FK → inbox_groups.id | Which group changed |
| `event` | text | not null | Event type (see below) |
| `details` | jsonb | not null | What changed |
| `previous_state` | jsonb | not null | Snapshot of relevant fields before the change |
| `created_at` | timestamptz | not null, default now() | When it happened |

**Event types:**
- `created` — group row first inserted
- `assigned_to_client` — generic group assigned to a client (role changed to A or B)
- `campaign_added` — campaign ID added to campaign_ids
- `campaign_removed` — campaign ID removed
- `status_changed` — warming/ready/active/retired transition
- `tags_changed` — tag_ids modified
- `accounts_changed` — account_ids modified (accounts added/removed)
- `drift_detected` — sync found mismatch between Supabase and SmartLead
- `drift_resolved` — mismatch cleared

## Lifecycle

```
warming → ready → active → retired
(generic)  (generic)  (assigned, role=A or B)  (done, never reused)
```

1. New inboxes created and warming: `status=warming, role=generic, assigned_client=null`
2. Warmup hits 14 days: `status=ready` (available for assignment)
3. Client signs, group assigned: `assigned_client="Kay's Landscaping", role="A", status=active, campaign_ids=[...]`
4. Group burns out or client done: `status=retired`
5. Letter reused: new row with same `group_letter`, incremented `batch`, fresh `smartlead_client_id`

## Drift Detection

Runs inside the existing `_sync_loop()` every 120 seconds:

1. For each `inbox_groups` row where `status` in ("active", "ready"):
   - Fetch campaign's actual account list from SmartLead
   - Compare `account_ids` in Supabase vs actual
   - If mismatch: populate `drift_flags`, log `drift_detected` event to history
2. Dashboard shows drift alerts on Acquisition tab
3. Marsha posts to Slack on new drift

Drift checks respect the rate limiter — if rate-limited mid-check, skip remaining groups and retry next cycle.

## Seeding

One-time script to populate from current SmartLead state:
- Pull all clients and their accounts
- Match against known group letters (A-L for acquisition, Generic A-M, client B groups)
- Insert rows with current state
- Migrate existing `client_rotations` data into this table
- `sr_groups.json` becomes unnecessary (data lives in Supabase)

## Dashboard Integration

- Acquisition tab reads from `inbox_groups` instead of rebuilding from SmartLead API
- Drift flags shown as alert banners
- Assignment changes update Supabase FIRST, then push to SmartLead
- History queryable via a "Group History" panel (click a group → see timeline)

## What This Replaces

- `client_rotations` table → merged into `inbox_groups` (role + assigned_client)
- `sr_groups.json` → domains + tag_ids on each row
- `acquisition_assignments` state key → campaign_ids on each row
- Live SmartLead-derived group state → Supabase is the source, SmartLead is verified against it
