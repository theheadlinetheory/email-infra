# Dashboard Tag Simplification Design

## Problem

The infra dashboard has three overlapping systems for tracking account ownership: SmartLead `client_id`, SmartLead tags (3 per account), and Zapmail domain tags. This makes it hard to tell which accounts belong to which group, and group identity gets lost when generics are assigned to clients. Additionally, generic groups have inconsistent sizes (15-49 accounts when the standard is 42), and group swap operations silently skip critical manual steps (inbox reallocation).

## Design

### Tag Model

Every SmartLead account gets exactly 3 tags:

| Tag | Example | Changes? |
|-----|---------|----------|
| Zapmail | `Zapmail` (ID 262254) | Never |
| Group | `Generic F` or `Kay's Landscaping A` | On assignment only |
| Warmup Date | `4/16/26` | Never |

The group tag is the single source of truth for account identity. The dashboard parses it to determine:

- **Unassigned generic**: tag starts with "Generic" (e.g., `Generic F`)
- **Client A group**: tag ends with ` A` (e.g., `Kay's Landscaping A`)
- **Client B group**: tag ends with ` B` (e.g., `Kay's Landscaping B`)

Client name is derived by stripping the last ` A` or ` B` suffix. Parsing splits on the final space + single letter to avoid false matches in client names.

SmartLead `client_id` still gets set (SmartLead needs it internally) but the dashboard never reads it to determine group membership. Tags drive everything.

Zapmail domain tags must always mirror the SmartLead group tag. When `Generic F` becomes `Kay's Landscaping A` in SmartLead, the Zapmail domain tag changes to `Kay's Landscaping A` too. The Sync Check tab validates they match.

Tag transitions:

- New generic group created: tagged `Generic F`
- Assign to client as A group: tag swaps to `Kay's Landscaping A`
- Assign to client as B group: tag swaps to `Kay's Landscaping B`
- Tags never change after client assignment. Rotation is campaign-level, not tag-level.

### Group Standard

New groups are always 14 domains / 42 accounts (3 inboxes per domain). The pipeline enforces this at creation time. Daily capacity target: ~630/day (42 accounts x 15 sends/account).

Existing groups keep their current sizes and phase out naturally as they get assigned to clients. No rebalancing of existing groups.

### A/B Rotation & Swap Workflow

Rotation is campaign-level only. Tags never change. The dashboard tracks which group (A or B) is currently active in campaigns.

Swap flow:

1. Click "Swap to Group B" on a client card
2. Dashboard removes Group A accounts from campaigns, adds Group B accounts
3. Post-swap reminder appears as a prominent warning banner on the client card: "Inboxes swapped. Reallocate inboxes in SmartLead now or sends will be 0/day."
4. Banner persists until dismissed (survives page refreshes)

Rotation state is tracked in Supabase (`client_rotations` table). The `b_group_assignments.json` file is eliminated since tags carry A/B identity.

Each client card shows both groups:

- Active group: green, with campaign stats and send volume
- Reserve group: dimmed, showing warmup health / resting status
- Last swap date and days since rotation visible

### Dashboard Views

The dashboard parses the group tag to build all views.

**Clients mode:**

- Fetch all SmartLead accounts, read their group tag
- Group by client name (everything before the ` A` or ` B` suffix)
- Each client card shows both A and B groups side by side

**Generic Groups section:**

- Accounts where group tag starts with `Generic` are unassigned
- Each card shows domains, accounts, capacity, warmup progress
- "Assign to Client" button prompts for client name and A/B designation

**Assignment modal:**

- Pick client name + pick A or B
- Dashboard swaps group tag on all accounts (e.g., `Generic F` -> `Client Name A`)
- Updates Zapmail domain tags to match
- Sets SmartLead `client_id` behind the scenes

**Sync Check tab:**

- Compares SmartLead group tag vs Zapmail domain tag per account
- Flags mismatches

### What Gets Eliminated

- `b_group_assignments.json` — A/B identity lives in tags
- Dashboard no longer reads `client_id` for group membership
- No more separate "client name" tag vs "group" concept — they're one tag
- Separate client tag and group tag concepts merged into single group tag

### Migration

Existing accounts need their tags updated to match the new model. For each existing client:

- Find all accounts by current `client_id`
- Determine if they're A or B group (from `client_rotations` table)
- Set group tag to `{ClientName} A` or `{ClientName} B`
- Update Zapmail domain tags to match

For existing generics:

- Verify group tag matches the SmartLead client name (e.g., `Generic F`)
- Fix any that are missing or wrong

This migration runs once. After that, the new model is live.
