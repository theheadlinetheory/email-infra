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

- **Acquisition group**: tag starts with "Acquisition" (e.g., `Acquisition A`, `Acquisition H`)

Client name is derived by stripping the last ` A` or ` B` suffix. Parsing splits on the final space + single letter to avoid false matches in client names. Acquisition groups are identified by the "Acquisition" prefix.

SmartLead `client_id` still gets set (SmartLead needs it internally) but the dashboard never reads it to determine group membership. Tags drive everything.

Zapmail domain tags must always mirror the SmartLead group tag. When `Generic F` becomes `Kay's Landscaping A` in SmartLead, the Zapmail domain tag changes to `Kay's Landscaping A` too. The Sync Check tab validates they match.

Tag transitions:

- New generic group created: tagged `Generic F`
- Assign to client as A group: tag swaps to `Kay's Landscaping A`
- Assign to client as B group: tag swaps to `Kay's Landscaping B`
- Tags never change after client assignment. Rotation is campaign-level, not tag-level.

### Acquisition Groups

Acquisition is a separate mode in the dashboard with its own tab. Acquisition groups do NOT use A/B rotation — each group (A through L) is independently assigned to campaigns.

Each acquisition account gets 3 tags:

| Tag | Example | Changes? |
|-----|---------|----------|
| Zapmail | `Zapmail` (ID 262254) | Never |
| Group | `Acquisition A` | Never |
| Warmup Date | `4/16/26` | Never |

Current groups (A through L, G reserved for Lars) keep their letter identity. The group tag replaces the current SmartLead client name (e.g., "A Group (250/day)") with a cleaner `Acquisition A` format.

The Acquisition tab reads group tags to build its view, separate from the Clients tab. Each acquisition group card shows accounts, capacity, and which campaign it's assigned to. No A/B rotation, no swap workflow — groups just get plugged into campaigns directly.

Zapmail domain tags mirror the SmartLead group tag: `Acquisition A`, `Acquisition H`, etc.

### Group Standard

**Client/generic groups**: New groups are always 14 domains / 42 accounts (3 inboxes per domain). The pipeline enforces this at creation time. Daily capacity target: ~630/day (42 accounts x 15 sends/account).

**Acquisition groups**: ~5-6 domains / ~17-18 accounts per group, targeting ~250/day capacity. This is the existing standard and stays unchanged.

Existing groups keep their current sizes and phase out naturally. No rebalancing.

### A/B Rotation & Swap Workflow (Clients Only)

A/B rotation applies to client fulfillment groups only, not acquisition. Rotation is campaign-level — tags never change. The dashboard tracks which group (A or B) is currently active in campaigns.

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

### Campaign Tracking & Group Exclusivity (Supabase)

The `inbox_groups` table in Supabase is the authoritative record of where every group is. Each row tracks:

| Field | Purpose |
|-------|---------|
| `group_tag` | The SmartLead group tag (e.g., `Kay's Landscaping A`, `Acquisition H`, `Generic F`) |
| `account_ids` | SmartLead account IDs in this group |
| `campaign_ids` | Active campaign IDs this group is currently assigned to |
| `status` | `warming`, `ready`, `active`, `resting` |
| `role` | `generic`, `client`, `acquisition` |

**Exclusivity rule: a group can only be in one active campaign at a time. Multiple groups CAN be in the same campaign.**

Enforcement happens at two levels:

1. **Before assignment**: When assigning a group to a campaign, the dashboard checks `campaign_ids` on the group's Supabase row. If it's already in an active campaign, the assignment is blocked with a clear error: "Group is already active in {campaign name}. Remove it first."

2. **On every sync**: The daily audit and the dashboard's Sync Check compare SmartLead's live campaign data against Supabase's `campaign_ids`. If a group's accounts appear in a campaign that Supabase doesn't know about (or vice versa), it's flagged as drift.

**State transitions:**

- **Generic warming** → `status: warming`, `campaign_ids: []`
- **Generic ready** → `status: ready`, `campaign_ids: []`
- **Assigned to client** → `status: active`, `campaign_ids: [123]`, group tag updated
- **Swapped out (A/B rotation)** → `status: resting`, `campaign_ids: []`, accounts removed from campaign
- **Swapped in** → `status: active`, `campaign_ids: [123]`, accounts added to campaign

**Every campaign change goes through Supabase first.** The dashboard writes to `inbox_groups.campaign_ids` before touching SmartLead. If SmartLead fails, the Supabase record reflects intent and the error is surfaced — no silent partial state.

For acquisition groups, the same rule applies: one group, one campaign. The Acquisition tab reads `campaign_ids` from Supabase to show which campaign each group is in, and blocks double-assignment.

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

For existing acquisition groups:

- Find all accounts by current `client_id` (e.g., client "A Group (250/day)")
- Set group tag to `Acquisition {letter}` (e.g., `Acquisition A`)
- Update Zapmail domain tags to match

This migration runs once. After that, the new model is live.
