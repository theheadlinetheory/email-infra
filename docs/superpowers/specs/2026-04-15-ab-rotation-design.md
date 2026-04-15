# A/B Group Rotation — Design Spec

## Goal

Every fulfillment client gets two identical sets of email infrastructure (Group A and Group B). Each month, the active group swaps: one group sends in campaigns while the other rests on warmup. This preserves inbox reputation and is a standard industry practice.

## Clients

11 fulfillment clients participate in rotation:

| Client | Domains | Accounts |
|--------|---------|----------|
| Borja Landscaping Construction | 17 | 51 |
| Canopy Land Solutions | 17 | 51 |
| Coastal Lawn Care LLC | 18 | 54 |
| Dallas Land Care | 17 | 51 |
| GM Landscaping & Design | 12 | 36 |
| High Southern Scapes | 12 | 35 |
| Kay's Landscaping | 28 | 84 |
| Lightning Lawn Care | 23 | 68 |
| Pioneer Landscaping | 17 | 51 |
| Timesavers Landscaping Inc. | 17 | 51 |
| Tropical Landscaping | 22 | 66 |
| **Total** | **200** | **598** |

**Excluded:** Rain Environmental, Shade Tree Landscaping, ABC Landscaping, Deeter Landscape, Umbrella Property Services.

## Timeline

- **April 15:** Build out Group B for all 11 clients (14-day warmup)
- **April 15 (off-hours):** Test swap with Kay's Landscaping (already has two warmed sets from old Umbrella inboxes)
- **May 1:** First real swap — all clients flip from Group A to Group B

## Data Model

New Supabase table `client_rotations`:

| Column | Type | Description |
|--------|------|-------------|
| client_name | text (PK) | Client name, matches SmartLead |
| group_a_ids | jsonb | SmartLead account IDs for Group A |
| group_b_ids | jsonb | SmartLead account IDs for Group B |
| active_group | text | `"A"` or `"B"` |
| last_swap_date | text | ISO date of last swap |
| created_at | timestamptz | Row creation timestamp |

One row per client. `active_group` starts as `"A"`. When Group B pipelines complete, their account IDs are written to `group_b_ids`. Existing campaign accounts become `group_a_ids`.

## Swap Operation

When swap is triggered for a client:

1. Read the client's rotation record — get `group_a_ids`, `group_b_ids`, `active_group`
2. Determine `outgoing_ids` (currently active group) and `incoming_ids` (inactive group)
3. Fetch all active and paused campaigns for the client from SmartLead
4. For each campaign that contains any outgoing accounts:
   - Add incoming accounts to the campaign
   - Remove outgoing accounts from the campaign
5. Update rotation record: flip `active_group`, set `last_swap_date` to today

The outgoing group stays on warmup with current settings. No warmup config changes on swap.

The swap is reversible — triggering swap again flips back.

## API Endpoints

### `POST /api/rotation/swap`
Body: `{"client_name": "Kay's Landscaping"}`
Swaps one client. Returns success/failure with details of campaigns updated.

### `POST /api/rotation/swap-all`
No body. Swaps all clients in `client_rotations` table. Returns per-client results.

### `GET /api/rotation/status`
Returns all rotation records with current state.

## Dashboard UI

A **Rotation** section added to the existing SmartLead tab:

- Per-client row: client name, active group badge (A or B), last swap date, account counts for A and B
- "Swap" button per client row — triggers swap for that client
- "Swap All" button at the top — swaps every client

## Group B Buildout

Group B for each client is built using the existing pipeline in `setup.py`. One pipeline run per client with the same domain count and inbox configuration as their Group A. The pipeline:

1. Purchases domains (Spaceship)
2. Sets nameservers
3. Connects to Zapmail, creates 3 inboxes per domain (s.reynolds, sean.r, sean.reynolds)
4. Sets profile photos
5. Exports to SmartLead
6. Configures warmup
7. Tags accounts: Zapmail + ClientName + warmup start date (4/15/26)

After pipeline completes, the new account IDs are written to `group_b_ids` in the rotation record.

## Kay's Landscaping Test

Kay's already has two sets of warmed infrastructure (original + former Umbrella inboxes). To test:

1. Identify which Kay's accounts are currently in campaigns (= Group A)
2. Identify the extra warmed accounts not in campaigns (= Group B)
3. Create the rotation record with both sets
4. Run the swap during non-sending hours
5. Verify: Group B accounts are in campaigns, Group A accounts are out, campaigns are sending correctly

## Out of Scope

- Automated monthly swap scheduler (manual trigger for now)
- Dashboard tab consolidation
- Individual burned inbox replacement during off-month
- Acquisition group rotation
