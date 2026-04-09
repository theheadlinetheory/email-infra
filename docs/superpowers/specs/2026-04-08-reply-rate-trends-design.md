# Reply Rate Trend Charts + Idle Inbox Alerts

## Goal
Add per-client reply rate trend charts to the client detail panel, showing infrastructure-wide sending performance over time. Also surface alerts for warmed inboxes sitting idle (not in any campaign).

## Chart Location

Embedded in the existing client detail panel (opened by clicking a client card in SmartLead tab). Order of elements in the detail panel:

1. Replacement recommendation (existing, if applicable)
2. **Reply rate trend chart** (new)
3. **Idle inbox alert** (new, if any warmed inboxes not in campaigns)
4. Account table (existing)
5. Delete / Transition buttons (existing)

## Chart Behavior

- Loads when the detail panel opens, alongside account data
- Zoom preset buttons: **7D | 14D | 30D | 90D | All**
- Default view: **30D**
- Chart.js line chart: X-axis = dates, Y-axis = reply rate %
- Days with 0 sends are gaps in the line (not plotted as 0%)
- Hover tooltip shows: date, reply rate %, sent count, reply count
- Summary below chart: average reply rate for the selected period, trend arrow comparing last 7 days vs prior 7 days

## Backend Endpoint

`GET /api/client/<id>/trends?days=30`

Returns:
```json
{
  "client_name": "Tropical Landscaping Inc.",
  "days": 30,
  "data": [
    {"date": "2026-03-10", "sent": 840, "replied": 12, "reply_rate": 1.43},
    {"date": "2026-03-11", "sent": 0, "replied": 0, "reply_rate": null},
    {"date": "2026-03-12", "sent": 393, "replied": 6, "reply_rate": 1.53}
  ],
  "summary": {
    "total_sent": 12450,
    "total_replied": 187,
    "avg_reply_rate": 1.5,
    "recent_7d_rate": 1.2,
    "prior_7d_rate": 1.8,
    "trend": "down"
  }
}
```

### Campaign-to-Client Matching

Many campaigns have `client_id: null` in SmartLead. Match by name:
- Get the client name from SmartLead client list
- Fetch all campaigns via `get_analytics_campaign_list()`
- Match campaigns where the campaign name contains the client name (case-insensitive)
- Exclude campaigns containing "Acquisition" in the name
- Pass matched campaign IDs to `get_day_wise_overall_stats(campaign_ids, start_date, end_date)`

### Aggregation

- Sum `sent` and `replied` across all matched campaigns per day
- `reply_rate = (replied / sent) * 100` for days where `sent > 0`
- `reply_rate = null` for days where `sent == 0` (not counted as 0%)
- Summary `avg_reply_rate` = total_replied / total_sent * 100 (only counting days with sends)
- Trend: compare avg reply rate of last 7 days vs the 7 days before that

## Idle Inbox Alert

### In Detail Panel

Shown between the chart and the account table. Uses data already returned by `api_client_accounts()`:
- Find accounts where warmup status is not "ACTIVE" (warmup complete) AND warmup_days >= 14 AND campaign_count == 0
- Yellow warning card: "X warmed inbox(es) not in any campaign" with email addresses listed
- Only shown if idle inboxes exist

### In SmartLead Tab Alert Banner

Also surface idle inboxes in the main alert banner (top of SmartLead tab) so they're visible without clicking into each client:
- Aggregate across all clients
- "X warmed inbox(es) across Y client(s) are not in any campaign"

## Tech Stack

- **Chart.js** loaded from CDN in dashboard.html (`<script src="https://cdn.jsdelivr.net/npm/chart.js">`)
- One new API route in `dashboard.py`: `GET /api/client/<id>/trends`
- SmartLead API calls: `get_analytics_campaign_list()` + `get_day_wise_overall_stats()`
- Chart rendering in `renderDetailTable()` in dashboard.html
- No new files, no Supabase storage, no daily snapshot jobs

## Edge Cases

- Client has no campaigns: show empty chart with "No campaign data" message
- Campaign name doesn't match any client: excluded (acquisition campaigns, orphaned campaigns)
- SmartLead API timeout: show error state in chart area, account table still renders
- All days have 0 sends (e.g., campaigns just started): show "Not enough data yet" message
- "All" time range: use earliest campaign creation date for that client as start_date
