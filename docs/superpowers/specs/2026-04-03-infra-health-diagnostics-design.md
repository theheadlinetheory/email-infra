# Infrastructure Health Diagnostics & Rotation System

## Overview
Add health scoring, alerting, and automated infrastructure rotation to the email-infra dashboard. Goal: Aidan spends ≤10 min/day monitoring all client infrastructure health and executing replacements.

## Health Scoring

### Per-Inbox Composite Score (0-100)
All rate metrics use **7-day rolling averages**.

| Factor | Weight | Healthy (100pts) | Flagged (0pts) |
|--------|--------|-------------------|----------------|
| Bounce rate | 30% | ≤ 1% | > 3% |
| Reply rate | 25% | ≥ 5% | < 2% |
| Reputation score | 25% | ≥ 99 | < 99 |
| Inbox placement rate | 10% | ≥ 99% | < 99% |
| SMTP/IMAP status | 5% | Connected | Any failure |
| Blocked status | 5% | Not blocked | Blocked |

Linear interpolation between healthy/flagged for continuous metrics. Binary for SMTP and blocked.

### Additional Flag: Warmup Off
Warmup must ALWAYS be enabled on every active account. If warmup is OFF, the inbox is flagged regardless of other scores.

### Domain-Level Rollup
If ANY inbox on a domain is flagged → ALL inboxes on that domain are flagged. The entire domain is marked for replacement. Infrastructure problems are domain-level, not inbox-level.

### Client-Level Health
Percentage of healthy domains. Alert triggers when ≥15% of a client's domains are flagged.

## Dashboard UI

### Alert Banner (top of SmartLead tab)
Appears when any client has ≥15% flagged domains. Shows count of clients needing attention. Clickable to scroll to first flagged client.

### Client Cards
- Health score badge (0-100), color: green ≥85, yellow 60-84, red <60
- Warmup progress summary for new accounts: "12/15 fully warmed"

### Detail Panel (per-client)
- Health score column per inbox, color-coded
- Flagged rows highlighted with reason icons (bounce, reply, reputation, SMTP, blocked, warmup off)
- Warmup progress bar per inbox for accounts still in initial 14-day warmup period

### Action Panel (bottom of detail panel for flagged clients)
Shows recommended replacement action:
> "4 inboxes across 2 domains unhealthy. Set up 2 new domains (6 inboxes). [Start Replacement]"

## Automated Rotation Pipeline

### Triggered via "Start Replacement" button
1. Buy new domains (Spaceship API)
2. Set up DNS/nameservers (Spaceship API)
3. Connect to ZapMail — create inboxes (s.reynolds, sean.r, sean.reynolds per domain)
4. **PAUSE** — show direct links to ZapMail inbox pages for profile photo setup (manual step)
5. User clicks "Continue" after photos are set
6. Export to SmartLead
7. Enable warmup (14-day period begins)
8. After warmup complete + confirmed healthy: assign to client campaigns
9. Remove old unhealthy inboxes from campaigns
10. Delete old inboxes + domains from ZapMail

### Pipeline Status Tracker
Step-by-step progress display in the detail panel showing current state of any active rotation.

## Data Changes

### Backend (dashboard.py)
- Change health metrics window from 14 days to 7 days
- Add health score calculation function (weighted composite)
- Add domain grouping logic (group inboxes by domain, flag whole domain if any inbox unhealthy)
- Add warmup-off detection
- New endpoint: `/api/client/{id}/health` — returns flagged domains with reasons and replacement recommendation
- New endpoint: `/api/rotation/start` — kicks off replacement pipeline
- New endpoint: `/api/rotation/continue` — resumes after profile photo pause
- New endpoint: `/api/rotation/status` — returns current pipeline state

### Frontend (dashboard.html)
- Alert banner component
- Health score badge on client cards
- Health score column + flag indicators in detail table
- Action panel with replacement recommendation
- Rotation pipeline progress tracker
- Warmup progress bars for new accounts

## Modular Implementation
This will be built in phases to keep changes surgical:
1. Health scoring backend (calculation + endpoint)
2. Health UI on client cards and detail panel
3. Alert banner
4. Rotation pipeline backend (endpoints + automation)
5. Rotation UI (action panel + progress tracker)
