# Dashboard Cleanup & Untagged Inbox Fix — Design Spec

**Date:** 2026-04-16
**Goal:** Declutter the SmartLead dashboard tab, make mode buttons functional filters, unify card design across fulfillment and acquisition, add warmup progress bars to acquisition groups, and detect/fix untagged inboxes.

---

## 1. Mode Buttons as Real Filters

**Current state:** Fulfillment/Acquisition buttons in `.mode-switcher` (dashboard.html lines 299-302) toggle `currentMode` variable and restyle buttons, but never hide/show sections. Both fulfillment and acquisition content are always visible.

**Change:** `switchMode()` must conditionally show/hide sections based on the active mode. Store mode in `localStorage` so it persists across page refreshes.

### Fulfillment mode shows:
- Summary stats row (scoped to fulfillment accounts)
- Fulfillment client cards grid (`#clients-grid`)
- Generic Groups section (`#generic-section`)
- A/B Rotation section (`#rotation-section`)
- Unassigned Accounts section (`#unassigned-section`)
- Active Pipelines section (`#setup-pipeline-section`)
- Generic Group Setup Tracker (`#generic-setup-tracker`)

### Acquisition mode shows:
- Summary stats row (scoped to acquisition accounts)
- Acquisition group cards grid (`#acquisition-section`)
- Active Pipelines section (only if acquisition-type pipelines exist)

### Summary stats row behavior:
Scope the 6-card summary row to the active mode. In fulfillment mode, aggregate from fulfillment client data. In acquisition mode, aggregate from acquisition group data. Show only: Total Accounts, In Campaigns, Avg Bounce Rate, Avg Reply Rate (4 cards, not 6).

---

## 2. Unified Card Design

Both fulfillment client cards and acquisition group cards use the same visual layout. This means modifying `renderClientCards()` and `renderAcquisitionGroups()` to produce identical card structures.

### Card layout:

```
+--------------------------------------------------+
| Client/Group Name                    XX accounts  |
+--------------------------------------------------+
| Capacity    Issues     Bounce    Reply            |
| 250/day     2          1.2%      4.8%             |
+--------------------------------------------------+
| [Batch warmup bars — conditional]                 |
| ● 33 accounts ready              since 2026-02-07 |
| ● 18 new accounts warming          Day 7/14 [===] |
+--------------------------------------------------+
| Ready: 2026-02-21          Rotation: 2026-05-07   |
+--------------------------------------------------+
```

### Stats row (4 items):
- **Capacity:** `daily_capacity`/day
- **Issues:** SMTP failures + blocked combined. Displayed as single number. Red if > 0, green if 0.
- **Bounce Rate:** `avg_bounce_rate`%. Color: > 3% red, > 1% yellow, else green. Null shows "—".
- **Reply Rate:** `avg_reply_rate`%. Color: > 5% green, > 2% yellow, else red. Null shows "—".

### Warmup section (conditional):
Shown only when the client/group has warming accounts. Uses the existing batch progress bar design (purple 6px bar, "Day X/14" label). Multiple batches render multiple bars.

### Footer dates (conditional):
- **Ready Date:** Only shown if account has a ready date and isn't fully ready yet.
- **Rotation Date:** Only shown if rotation date exists. Badge if <= 7 days.

### Removed from cards:
- Health score badge (header)
- Healthy inboxes count (stat)
- Target Volume input + status line
- Warmup Start date (footer)
- Separate SMTP Failures stat
- Separate Blocked stat
- Domains count (acquisition cards only)
- "In Campaign" count (acquisition cards only)
- "Warming" count (acquisition cards only — replaced by progress bars)

---

## 3. Acquisition Warmup Progress Bars

### Backend change (`dashboard.py`):

`api_acquisition()` currently returns per-group stats but no batch warmup data. Add the same batch computation logic used in `_compute_overview()` (lines 542-573) to acquisition groups.

For each acquisition group:
1. Group accounts by warmup start date using 3-day bucketing
2. Classify each batch as "ready" (>= 14 days or in campaign) or "warming"
3. Return `batches` array with: `warmup_start`, `total`, `ready`, `warming`, `days_done`, `status`

### Frontend change (`dashboard.html`):

`renderAcquisitionGroups()` renders the same batch progress bar HTML used in `renderClientCards()`. Extract the batch bar rendering into a shared helper function to avoid duplication.

---

## 4. Untagged Inbox Detection & Fix

### Required tags per account:
Every SmartLead email account must have exactly 3 tags:
1. **Zapmail** (tag ID: 262254)
2. **Client Name** (e.g., "Kay's Landscaping", "A Group (250/day)")
3. **Warmup Start Date** (e.g., "4/9/26")

### One-time fix script:

Create `fix_untagged.py` that:
1. Fetches all SmartLead accounts via public API
2. For each account, calls internal details endpoint to read current tags
3. Identifies accounts missing any of the 3 required tags
4. For accounts with 0 tags: determines correct client name from `client_id` mapping, determines warmup start from `warmup_details.warmup_created_at`, applies all 3 tags
5. For accounts with partial tags: fills in missing ones
6. Reports: total scanned, already correct, fixed, unfixable (no client mapping)

Uses existing helpers from `setup.py`:
- `sl_internal_headers()` for JWT auth
- `sl_tag_account(account_id, tag_ids, client_id)` for applying tags
- `sl_get_all_tags()` for tag ID resolution

Rate limiting: 0.3s between detail fetches, 0.5s between tag writes.

### Dashboard alert:

Add untagged detection to the overview/acquisition API responses. When computing client or group data, check tag counts from the synced Supabase data (if available) or flag accounts where tags are missing.

Display an alert banner at the top of the active mode view:
```
⚠ X accounts have missing tags and may not be tracked correctly. [View Details]
```

The alert appears in whichever mode contains the untagged accounts. Clicking "View Details" could expand to show which accounts are affected, or scroll to the unassigned section.

### Ongoing detection:

The background sync that runs every 2 minutes (`sync_smartlead_to_supabase`) should track tag status. Add a `tag_count` or `is_tagged` field to the synced account data so the dashboard can surface untagged accounts without hitting the internal API on every page load.

---

## 5. Files Changed

| File | Change |
|------|--------|
| `dashboard.html` | Mode filtering, unified card template, shared batch bar helper, untagged alert banner |
| `dashboard.py` | Scoped summary stats, acquisition batch data, untagged detection in sync |
| `web/public/index.html` | Mirror of dashboard.html |
| `fix_untagged.py` | New — one-time tag remediation script |

---

## Out of Scope

- Changing tab structure (SmartLead/ZapMail/Domains/Pipelines/Sync tabs stay as-is)
- Modifying the detail panel that opens when clicking a client card
- Changes to Generic Group card design (already clean enough)
- Pipeline stepper UI changes
