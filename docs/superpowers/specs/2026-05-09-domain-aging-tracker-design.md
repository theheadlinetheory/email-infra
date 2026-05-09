# Domain Aging Tracker

## Problem

70 .info domains purchased 2026-05-08 are aging with CloudNS nameservers, destined for 5 future B groups (14 domains each). Currently tracked only by memory. Need systematic tracking with dashboard visibility, and a general system that supports future batches.

## State File: `state/aging_pool.json`

Single source of truth for all aging domain batches. Migrated from the existing `generic_aging_domains.json`.

```json
{
  "threshold_days": 30,
  "batches": [
    {
      "id": "2026-05-08-info-70",
      "name": "Service Industry .info",
      "purchased": "2026-05-08",
      "cost": 231.70,
      "ns_provider": "CloudNS",
      "status": "aging",
      "domains": ["workflowpros.info", "crewsolutions.info", "..."]
    }
  ]
}
```

### Batch fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique ID, format: `YYYY-MM-DD-<suffix>` |
| `name` | string | Human-readable batch label |
| `purchased` | string | ISO date of purchase |
| `cost` | number | Total cost in USD |
| `ns_provider` | string | Where NS records point (e.g. "CloudNS") |
| `status` | string | `"aging"` or `"activated"` |
| `domains` | string[] | List of domain names still in the pool |

When domains are activated (moved to Zapmail pipeline), they are removed from the `domains` array. When all domains are removed, `status` becomes `"activated"`.

## Backend: `dashboard.py`

### `api_aging_pool()` — GET `/api/aging-pool`

Reads `state/aging_pool.json`, computes derived fields per batch:

- `days_aged`: days since `purchased`
- `days_remaining`: max(0, `threshold_days` - `days_aged`)
- `progress_pct`: min(100, round(`days_aged` / `threshold_days` * 100))
- `ready`: boolean, true when `days_aged` >= `threshold_days`
- `b_groups_possible`: `len(domains)` // 14

Returns:

```json
{
  "threshold_days": 30,
  "total_domains": 70,
  "total_ready": 0,
  "total_b_groups_possible": 5,
  "batches": [
    {
      "id": "2026-05-08-info-70",
      "name": "Service Industry .info",
      "purchased": "2026-05-08",
      "cost": 231.70,
      "ns_provider": "CloudNS",
      "status": "aging",
      "domain_count": 70,
      "domains": ["..."],
      "days_aged": 1,
      "days_remaining": 29,
      "progress_pct": 3,
      "ready": false,
      "b_groups_possible": 5
    }
  ]
}
```

### `api_aging_pool_add(body)` — POST `/api/aging-pool/add`

Adds a new batch. Body:

```json
{
  "name": "Service Industry .info",
  "purchased": "2026-05-08",
  "cost": 231.70,
  "ns_provider": "CloudNS",
  "domains": ["domain1.info", "domain2.info"]
}
```

Generates `id` from `purchased` date + domain count. Writes to `aging_pool.json`.

### `api_aging_pool_activate(body)` — POST `/api/aging-pool/activate`

Moves domains from aging pool into active infrastructure. Body:

```json
{
  "batch_id": "2026-05-08-info-70",
  "count": 14
}
```

Takes the first `count` domains from the batch, removes them from the `domains` array, and returns the removed domains. Does NOT trigger any Zapmail/SmartLead operations — that's handled by the existing pipeline. If all domains are removed, sets batch `status` to `"activated"`.

## Frontend: Domains Tab

### Placement

New "Aging Pool" section at the top of the Domains tab (`tab-domains`), above the existing registrar domain lists.

### Summary card

Shows at a glance:
- Total domains aging across all batches
- B groups possible (total / 14)
- Nearest readiness date (shortest `days_remaining` across batches)

Example: `70 domains aging | 5 future B groups | Ready in 29 days`

### Batch rows

Each batch renders as a card with:
- Batch name and purchase date
- Domain count and cost
- Progress bar (0% to 100% over threshold_days)
- Status badge: "Aging" (yellow) or "Ready" (green)
- Days aged / threshold display (e.g. "1 / 30 days")
- Expandable domain list (collapsed by default)
- "Activate 14" button (visible only when `ready` is true) — removes 14 domains from pool

### Add Batch form

Button "+ Add Batch" opens a modal:
- Name (text input)
- Purchase date (date input, defaults to today)
- Cost (number input)
- NS Provider (text input, defaults to "CloudNS")
- Domains (textarea, one per line)

On submit, calls `POST /api/aging-pool/add`.

## Migration

On first load, if `aging_pool.json` doesn't exist but `generic_aging_domains.json` does, the backend migrates automatically:
- Reads the old file
- Creates `aging_pool.json` with the old domains as one batch
- Old file is left in place (not deleted)

## Not included

- No DNS verification polling — NS is set at purchase time, trusted
- No auto-activation — always manual
- No Spaceship API integration for purchase tracking
- No Slack notifications for aging milestones
