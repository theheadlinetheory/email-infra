# Zapmail Removal Watcher + Slack Notifier — Handoff

**Status: LIVE — built, deployed, seeded, and the Slack webhook is wired + verified (2026-07-29).**
Point a new Claude session at this file to extend it.

Last worked: 2026-07-28/29. Repo: `email-infra` (this repo). Owner: Tim (tim@theheadlinetheory.com).

---

## 1. What it is / why it exists

When we schedule a Zapmail domain's mailboxes for removal (via the scheduled-removal
API **or** manually in the Zapmail UI), Zapmail deletes them on the domain's next
**monthly billing date**. Zapmail support then wants us to **confirm once they're
actually removed** so they can optimise (reduce) our mailbox-slot billing.

Zapmail's reply to Tim:
> "we can see that the mailboxes are scheduled to be deleted on the last billing date.
> Please let us know once they are removed on the scheduled date, and we can optimize
> the billing for the mailbox slots from your subscriptions."

So the bot notifies us **when a scheduled mailbox actually disappears**, so Tim can
tell Zapmail → they cut the billing.

## 1b. The deletion date IS knowable (added 2026-07-30)

Tim needs the alert **on the scheduled date**, so he can message Zapmail that day — not
after the fact. The Zapmail UI shows "Mailbox is scheduled for deletion. It will be deleted
on 09 Aug, 2026" on hover, so the data exists; it just isn't in the public API.

**No API field carries it.** `/domains` mailbox objects hold only
`id/username/domain/firstName/lastName/status/createdAt`; the domain level has only the
annual `expireOn`. Probed and dead: `/mailboxes` + `?domainId=` (**500**),
`/subscriptions/{id}`, `/subscriptions/{id}/domains`, `?includeDomains=true`,
`?includeSubscription=true` (params silently ignored), `/domains/{id}/mailboxes`,
`/domains/scheduled-removal`, `/mailboxes/scheduled`, `/invoices`, v1 equivalents (404/500).

**But it's derivable.** `GET /api/v2/subscriptions` returns `periodEnd` — the monthly
billing date, which is exactly when Zapmail deletes a scheduled mailbox. Mailboxes are
provisioned *seconds after* the subscription that pays for them, so matching each
mailbox's `createdAt` to the newest subscription created at-or-before it recovers the link
the API won't give you.

**Validated:** subscription `sub_1TKU9Z…` created `2026-04-10T01:46:36` (qty 18);
Lars' mailboxes created `2026-04-10T01:46:40` — 4s later; its `periodEnd` is **2026-08-09**,
exactly the UI tooltip. All 6 Lars domains derive to that date.

⚠️ **Best-effort, not authoritative.** A mailbox created long after its subscription (a
replacement slot) can match a newer subscription and derive a wrong date — across the fleet
only 42 of 76 subscriptions have matched-count == declared quantity (unused slots +
replacements). So the date drives the *heads-up* alert; absence-detection remains the
source of truth for what actually happened. Both alerts fire.

## 2. Key design decision (don't re-litigate this)

**Zapmail's domains API exposes NO scheduled-removal flag.** Verified live:
- `autoRenew` is `false` on **all 704** domains (fleet-wide, useless as a signal).
- A known-scheduled domain (`landscapeworkpros.info`) looks **identical** to an active
  one: `status: ACTIVE`, mailboxes `ACTIVE`, no removal date. `expireOn` is the annual
  domain-registration date (2027), NOT the monthly mailbox removal date.

Therefore the **only reliable trigger is the mailbox actually vanishing / flipping to
`EXPIRED`** in the inventory. This is also better because it catches **manual UI
cancellations**, not just API-scheduled ones — which is exactly what Tim asked for.

So: **daily full mailbox snapshot → diff vs yesterday → alert on anything that
disappeared or expired.**

## 3. How it works

- `zapmail_removals.py` (repo root):
  - `snapshot_mailboxes()` — paginated scan of every Zapmail mailbox (8 pages, ~1,563
    mailboxes). Returns `{email: {domain, id, status}}`, or `None` if any page failed
    (never diffs a partial scan — that would look like mass removals).
  - `check_removals(dry_run=False)` — diff current vs stored snapshot. A mailbox counts
    as removed if it was `ACTIVE` last time and is now **gone or `EXPIRED`**. Posts a
    Slack alert for newly-removed ones, marks them notified (no dupes), updates snapshot.
  - `register_domains(domains, source)` — add every current mailbox on those domains to
    the pending-cancellation registry.
  - `pending_summary()` — pending vs removed, grouped by domain.
  - `post_slack(msg)` — POSTs to `SLACK_ZAPMAIL_WEBHOOK` (falls back to `SLACK_WEBHOOK_URL`).
    If neither is set, **queues the message in DB state** (`zm_removal_pending_msgs`) so a
    Claude session can flush + post it via the Slack MCP. Nothing is ever lost.
  - `flush_pending()` — return + clear queued messages.
- **Runs daily** by piggybacking the existing health-snapshot cron
  (`api/index.py` → `health_snapshot()` calls `zr.check_removals()` in a try/except).
  Vercel Hobby allows only 1 cron; it's `/api/health-snapshot` at `0 13 * * *` (see `vercel.json`).
  ⚠️ **The Vercel cron has never actually fired** — see §10. Scheduling now comes from
  GitHub Actions (`.github/workflows/zapmail-watch.yml`, 4x/day).
  ⚠️ **Decoupled (PR #40):** `check_removals()` used to sit inside the same `try` as
  `hs.snapshot_daily()`, *after* it — so any health-snapshot exception 500'd the request
  before the watcher ran, silently disabling removal detection. It now runs regardless.
- **Heartbeat (PR #40):** every run writes `zm_removal_last_check`
  `{ts, ok, scanned, error, fails, last_ok}`. Two consecutive failures post a `:warning:`
  to Slack, then weekly. Without this, "no alert" was ambiguous between *nothing was
  removed* and *the watcher has been dead for a week*.
- **State keys** (Supabase `state` table, via `db.py`):
  - `zm_mailbox_snapshot` — last full inventory `{mailboxes:{...}, taken}`.
  - `zm_removal_registry` — `{entries:{email:{domain,source,first_seen,removed_date,notified}}}`.
  - `zm_removal_pending_msgs` — queued Slack messages when no webhook set.
  - `zm_removal_last_check` — heartbeat / consecutive-failure counter.

## 4. Endpoints  (auth: append `?pw=WwShyM1ESmYTqy9cC5Q0jZnp`)

Base: `https://email-infra.vercel.app`
- `GET /api/zapmail-removals?action=summary` — pending vs removed registry, plus
  `last_check` (heartbeat) and `snapshot_taken` — check these before trusting an empty
  `removed` list.
- `GET /api/zapmail-removals?action=check` — dry-run diff now (add `&commit=1` to persist + alert).
- `GET /api/zapmail-removals?action=flush` — return + clear queued Slack messages.
- `GET /api/zapmail-removals?action=test` — post a line through `post_slack()`; replies
  `{posted_via: webhook|queued, webhook_configured: bool}`. Optional `&msg=...`. (PR #39.)
- `POST /api/zapmail-removals` body `{"action":"register","domains":[...],"source":"manual"}` — add to pending.

## 5. Current live state (as of handoff)

- **Deployed:** PR #35 merged to `main` (health-v1 → main). Live + verified.
- **Seeded:** baseline snapshot of 1,563 mailboxes saved; **54 mailboxes across 18 domains
  registered as pending** (all still present, awaiting billing date; 0 removed so far).
- The 18 scheduled domains:
  - **Client (16):** exteriorgroundswork.info, groundscarebase.co, groundscarefocus.co,
    groundskeepingexperts.info, groundsmaintenanceservices.info, landscapeworkpros.info,
    landscapingservicecrew.info, lawncarepartners.info, lawncarepros.info, outdoorcareplus.co,
    turfmanagementpros.info, turfservicefocus.co, turfservicegroup.info, turfservicepros.info,
    yardmanagementcrew.info, yardworkexperts.info
  - **Acquisition (2):** headlinetheory360group.info, headlinetheory360hub.info
- **Slack channel created:** `#zapmail-billing` (ID `C0BLBD7UYDB`) in the-headline-theory
  workspace. Baseline summary already posted there.

## 6. ✅ DONE — the Slack webhook

Completed 2026-07-29. Tim created the `Zapmail Billing Notifier - THT` Slack app and added
its incoming webhook to **#zapmail-billing**; `SLACK_ZAPMAIL_WEBHOOK` is set in Vercel and
the deployment is live. Verified with
`GET /api/zapmail-removals?action=test&pw=...` → `{"posted_via":"webhook","webhook_configured":true}`
and the test line was confirmed present in the channel. (`post_slack()` only returns
`webhook` on a 200/201, so that response is a genuine Slack accept.)

Kept for reference — how it was set up:

**In Slack (api.slack.com/apps):**
1. **Create an App** → **Blank app** → Continue.
2. Name `Zapmail Billing Notifier`, workspace **The Headline Theory** → Create App.
3. Left sidebar → **Incoming Webhooks** → toggle **On**.
4. **Add New Webhook to Workspace** → pick **#zapmail-billing** → Allow.
5. Copy the **Webhook URL** (`https://hooks.slack.com/services/...`).

**In Vercel (email-infra project):**
6. Settings → Environment Variables → add `SLACK_ZAPMAIL_WEBHOOK` = that URL → Save.
7. Deployments → latest → ⋯ → **Redeploy** (env vars need a fresh deploy).

**Test it:**
- `GET /api/zapmail-removals?action=check&commit=1&pw=...` — should be safe (0 removals now).
- Or ask the new Claude session to POST a test message through `post_slack()` to confirm it
  lands in #zapmail-billing before relying on it.

## 6b. ⚠️ The Vercel cron never fired — scheduling now lives in GitHub Actions

Discovered 2026-07-30. `vercel.json` declares `/api/health-snapshot` at `0 13 * * *`, but:
- `inbox_health_daily` had **9 of the last 16 days**, with a 4-day hole (Jul 25–28).
- Not one run was at 13:00 — actual `created_at`s are 14:24, 22:04, 21:57, 00:36, 17:34,
  22:45, 22:02, 00:19, 19:28 UTC. That's the fingerprint of manual dashboard runs.
- The watcher heartbeat never moved off a hand-run.

**Root cause (likely):** `_is_vercel_cron()` required `CRON_SECRET` to be set *and* match.
With no secret configured it could never return True, so every cron request 401'd silently.

**Fixes (both shipped, deliberately redundant):**
1. `_is_vercel_cron()` falls back to Vercel's `x-vercel-cron` header when `CRON_SECRET`
   is unset. Setting `CRON_SECRET` re-tightens it to the signed check automatically.
2. `.github/workflows/zapmail-watch.yml` — hits `?action=check&commit=1` at 02/08/14/20 UTC,
   independent of Vercel entirely, and **fails the job** on an `{"error": …}` payload rather
   than logging it. Runs the health snapshot once daily too (the job the dead cron owed).
   **Requires repo secret `DASHBOARD_PW`.**

Alerts are idempotent (`date_notified` / `notified` per mailbox), so 4x/day costs nothing.
The due-date check uses `<= today`, so a skipped run makes an alert *late*, never *missing*.

## 7. Known gaps / TODO for the next session

- **`headlinetheoryinfo.com`** (the 3rd acquisition 2-burned domain) is **NOT a Zapmail
  domain** — it's external (Spaceship / Google direct). This watcher only sees Zapmail, so
  it will never alert on that one. Cancel + confirm it manually.
- **Manual cancellations:** if Tim cancelled any domains in the Zapmail UI outside the 18
  above, register them so the "pending" view is complete:
  `POST /api/zapmail-removals {"action":"register","domains":["x.info"],"source":"manual"}`.
  (Not required for alerts — absence-detection catches them regardless — only for the pending list.)
- **Optional dashboard UI:** a "Pending Zapmail cancellations" panel reading
  `?action=summary`, + a "cancel fully-dead domains" button using
  `PUT https://api.zapmail.ai/api/v2/mailboxes/scheduled-removal` (see below).
- **Before the webhook exists,** the new session can post queued alerts to #zapmail-billing
  via the Slack MCP: call `?action=flush` then post the returned messages, OR just post
  `?action=summary` output directly.

## 8. Reference — creds, gotchas, deploy

- **Env / creds:** `email-infra/.env` (ZAPMAIL_API_KEY, SMARTLEAD_API_KEY, SUPABASE creds).
  Load with `set -a; source .env; set +a` (bash) before running scripts.
- **Zapmail API:** base `https://api.zapmail.ai/api/v2`, headers
  `x-auth-zapmail: <key>`, `x-service-provider: GOOGLE`.
  - Schedule removal (domain-level, cancels whole domains): 
    `PUT /mailboxes/scheduled-removal` body `{"domainIds":[...],"remove":true}` → 200.
    (`remove:false` cancels a scheduled removal. The old `remove-on-renewal` POST and
    `DELETE /mailboxes` both 404 — dead endpoints.)
  - Domain list GET is **slow/flaky** (page 1 often times out) — use per-page retries.
- **PYTHONPATH gotcha (Windows):** running `python /abs/path/script.py` sets sys.path to the
  script dir, so `import db`/`import zapmail_removals` fail. Run scripts with
  `PYTHONPATH="c:/Users/TBKDV/Downloads/THT/email-infra"` (or cd into repo and run by module).
- **Deploy flow:** commit on branch `health-v1`, PR → `main`, merge. Standing OK to merge
  email-infra PRs via `gh` CLI. **Must** `gh auth switch --user timdwivedi` first (reverts to
  timsloan123 otherwise → misleading "Repository not found").
- **Slack webhook fallback:** `post_slack()` uses `SLACK_ZAPMAIL_WEBHOOK` else `SLACK_WEBHOOK_URL`
  else queues. Same pattern as `marsha.py` / `pipeline.py` in this repo.

## 9. Files touched

- `zapmail_removals.py` — NEW, the whole tool.
- `api/index.py` — added `/api/zapmail-removals` route + piggybacked `check_removals()` into
  `health_snapshot()`.
- (No change needed to `vercel.json` — reuses the existing daily cron.)
