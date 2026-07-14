# Inbox Health V1

Per-inbox deliverability tracking, burn detection, and one-click cancel — built
to the decisions from the 2026-07-10 call. New code lives alongside the existing
dashboard; nothing old was changed except two small hooks (`sync.py`, `api/index.py`).

## What it does
- Snapshots **every inbox's** reply / bounce / OOO + send volume **daily** into
  Supabase (`inbox_health_daily`) — the time-series the old dashboard never kept.
- Scores each inbox on a **3-day window** into one 0–100 number + a status:
  **Healthy / Watch / At-risk / Burned / Warming / No-data**.
- **Reply is the anchor, bounce #2, OOO 15%, SmartLead reputation dropped.**
  Placement is supported but off until seed testing is wired (phase 1.5).
- **Single-metric tripwires** override the blend (e.g. bounce ≥3% ⇒ Burned even
  if everything else is fine) — this is what made the old weighted-only score
  confusing.
- **Volume gate**: inboxes with <30 sends over 3 days are "No-data", so a new
  campaign bouncing 1-of-17 never trips a false alarm. Warming inboxes (<14 days)
  aren't judged on production rates.
- **Burned → cancel** in one click: schedules Zapmail **remove-on-renewal** (stop
  paying, zero wasted days). Defaults to a dry-run plan; executes only on confirm.

## Files
| File | Role |
|---|---|
| `health_model.py` | pure scoring brain (weights, tripwires, bands). `python health_model.py` runs its self-test. |
| `health_schema.sql` | Supabase migration: `inbox_health_daily`, `inbox_health_status`, `inbox_health_config`. |
| `health_snapshot.py` | daily job: SmartLead → attribute → persist → score → `health_fleet` cache. |
| `health_actions.py` | burned → Zapmail remove-on-renewal (dry-run by default). |
| `db.py` | +health helpers (`upsert_health_daily`, `get_health_daily`, `upsert_health_status`, `get_health_status_all`, `get_health_config`). |
| `api/index.py` | +`/api/health-fleet`, `/api/health-snapshot`, `/api/health-burn`. |
| `public/health.html` | the dashboard view (served at `/health.html`). |
| `sync.py` | +one hook: runs the snapshot at the end of each daily sync. |

## Deploy (≈10 min)
1. **Migration** — paste `health_schema.sql` into the Supabase SQL editor (same
   project as `supabase_schema.sql`) and run it. Safe to re-run.
2. **Ship the code** — commit/push; Vercel redeploys `api/index.py` and serves
   `public/health.html`. (Nothing else changes behaviour.)
3. **First snapshot** — open **`/health.html?pw=YOUR_DASHBOARD_PASSWORD`** and
   click **Run snapshot** (or `POST /api/health-snapshot`). It pulls today's
   SmartLead metrics, scores, and fills the table. After that the daily `sync`
   keeps it fresh automatically.
4. That's it for tracking. The cancel button stays in **dry-run** until you
   confirm, so it's safe to demo immediately.

## Two live-data checks before trusting deletes (10 min, needs prod creds)
Two field names can't be confirmed without one live record — both degrade
gracefully, but pin them for full function:
1. **OOO field** — `health_snapshot._extract_ooo()` tries several keys. Print one
   record from `sync.fetch_health_metrics(start,end)` and, if OOO exists under a
   different key, add it. Until then the score runs on reply+bounce (renormalised)
   — exactly the two you prioritised, so this is not blocking.
2. **Zapmail mailbox↔email match** — `health_actions._mailbox_index()` matches on
   `email/emailAddress/username`. Confirm against one `zm_get_subscription_mailboxes`
   record so remove-on-renewal resolves mailbox IDs. Unresolved emails are reported,
   never silently skipped.

## Tuning without a deploy
Edit the `default` row in `inbox_health_config` (weights + thresholds). The
snapshot reads it each run. Current weights: reply .35 / bounce .30 / ooo .15 /
placement .20 (placement redistributed when absent).

## Tue → Fri test plan
- **Tue:** run migration + deploy + first snapshot. Eyeball the table against what
  you already know — do the obviously-bad inboxes read Burned/At-risk, good ones
  Healthy? Adjust `inbox_health_config` if a threshold feels off.
- **Wed–Thu:** let the daily sync accumulate real 3-day windows. Watch whether a
  declining inbox crosses Watch→At-risk *before* it fully craters (the whole point).
- **Fri review:** confirm the statuses match reality, then test one real
  remove-on-renewal on a genuinely burned inbox (Plan → Confirm) and verify in
  Zapmail. If it's trustworthy, we start handing the daily glance to a VA.

## Not in V1 (next, per the call)
- Seed-list **inbox placement** testing (~$0 DIY via Gmail/Graph API) → feeds the
  placement weight.
- **Auto-reallocate** in SmartLead on inbox swap (no API — UI only today).
- Setting up new generic groups + the 2-week warmup pipeline reminder (existing
  `domain_replacements` state machine is the starting point).
