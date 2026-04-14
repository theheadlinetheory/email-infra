# Domain Inventory Management — Design Spec

## Goal

Show always-visible domain pool inventory in the dashboard topbar with low-stock alerts via Slack, so Aidan knows when to purchase more domains.

## Architecture

Two domain pools tracked from the Google Sheet's THT Domains tab:
- **Client pool** — generic landscaping domains (excludes `headlinetheory`)
- **Acquisition pool** — `headlinetheory` domains only

Both pools use existing `sheets.py` functions (`get_available_domains()`, `get_acquisition_domains()`) that filter by `Status="Available"`. Alert threshold is 20 for both pools.

## Components

### 1. Backend: `/api/domain-inventory` endpoint

**File:** `dashboard.py`

**Response:**
```json
{
  "client_available": 23,
  "acquisition_available": 12,
  "client_threshold": 20,
  "acquisition_threshold": 20,
  "client_low": false,
  "acquisition_low": true
}
```

**Logic:**
- Call `get_available_domains()` → count = client pool
- Call `get_acquisition_domains()` → count = acquisition pool
- Compare each against threshold (20)
- If either is low, fire Slack webhook

**Slack alert debouncing:**
- Track last alert time per pool in memory (simple dict)
- Only send one Slack alert per pool per 24 hours
- Message format: `"⚠️ Domain inventory low: [Pool] pool has [N] available (threshold: 20)"`
- Uses existing `SLACK_WEBHOOK_URL` from `.env`

### 2. Frontend: Topbar inventory badges

**File:** `dashboard.html`

Two badges in the topbar, between the mode switcher and the nav tabs:
```
[THT Infrastructure] [Fulfillment | Acquisition] [Client: 23] [Acq: 12] [SmartLead] [ZapMail] ...
```

**Styling:**
- Green background (`--accent-bg` + `--accent` text) when count >= 20
- Red background (`--red-bg` + `--red` text) when count < 20
- Font: `--font-mono`, 11px
- Pill-shaped badges matching existing badge styling

**Data loading:**
- Fetched in parallel with `/api/overview` on dashboard init
- Refreshed on Sync button click
- No polling — manual refresh only

### 3. Alert banner integration

**File:** `dashboard.html` (in `renderOverview()`)

When either pool is low, add alert items to the existing alert banner:
- `"Domain inventory low: Client pool has [N] available domains (need 20+)"`
- `"Domain inventory low: Acquisition pool has [N] available domains (need 20+)"`

Uses existing `alert-banner` styling with `--yellow` color for inventory warnings.

## Domain pool definitions

| Pool | Filter | Source function |
|------|--------|-----------------|
| Client | `Status="Available"` AND domain does NOT contain `headlinetheory` | `get_available_domains()` |
| Acquisition | `Status="Available"` AND domain contains `headlinetheory` | `get_acquisition_domains()` |

## Domain purchase rules (context)

- All domains purchased with auto-renew OFF — lifecycle too short to justify renewal
- NEVER pay renewal fees
- Manual purchase for now (no automated buying)

## Out of scope

- Automated domain purchasing
- Domain lifecycle tracking / history
- Domains tab overhaul
- Push notifications beyond Slack
