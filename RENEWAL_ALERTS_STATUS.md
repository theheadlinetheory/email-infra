# Renewal / infra-decision alert system — parked 2026-09-12

Paused deliberately: the alert layer only earns its keep once the fleet is cleaned up.
Resume from here.

## What is done and working

`infra_lifecycle.py` + `test_infra_lifecycle.py` (32 tests, all passing) — the engine.
Run it: `./.venv/bin/python infra_lifecycle.py` (needs `CRM_SUPABASE_KEY`, now in `.env`).

Per client it computes, **per mailbox creation cohort** (one client can carry five
billing anniversaries):

- `effective_end`  — last day the client may send
- `hard_stop`      — first Zapmail billing anniversary on/after that
- `decision_by`    — last useful day for an answer; unanswered ⇒ stop

Plus: seasonal verticals with a season close, custom/shared/pool domain classification
(never recommends cancelling a domain another client sits on), a `stop`/`renew` decision
registry, an override registry for facts the CRM cannot store, and a 7/3/1 Slack countdown
tagging Aidan (`U09B2673A4A`).

State lives in the infra Supabase `state` table:
`infra_lifecycle_overrides`, `infra_lifecycle_decisions`, `infra_lifecycle_pending_msgs`.
Already recorded: Wonderly = holiday_lighting; Vandenberg / Hammer / Urban Growth = stop.

## Edits made to shared files (NOT committed — those files carry other sessions' work)

Re-apply by hand if lost; each is additive and self-contained:

| File | What |
|---|---|
| `api/index.py` | `/api/infra-lifecycle` + `/api/infra-lifecycle/decision` routes; `post_notices()` piggybacked on the `health-snapshot` cron; `retainer-renewals` error path no longer returns `rows: []` |
| `public/health.html` | "Infra decisions" tab — `loadLifecycle()` / `renderLifecycle()` / `#lcrows` |
| `public/index.html` | retainers loader now checks `d.rows.length && !d.error` (an empty array is truthy, so a failed CRM read rendered as "0 active retainers") |
| `retainers.py` | raises on a zero-row CRM read instead of reporting an empty renewal schedule |

## Known-wrong, fix before shipping

1. **Decision timing is inverted.** Built as `decision_by = end − 7`. Aidan's model (2026-09-10
   call) is: ask AT term end, then a **one-week grace period the client is not told about**,
   then cancel. Rework to `ask_on = end`, `grace_until = end + 7`.
2. **Term must come from the CRM onboarding form**, not `DEFAULT_TERM_MONTHS = 3`. Terms are
   2/3/4 (a 7 was pitched), always whole months. Tim now fills that form on deal close.
3. **Season close should be 25 Dec, not 31 Dec** — Christmas is the real end of lighting.
4. ~~`SCHEDULE_BUFFER_DAYS = 3` is a guess.~~ **RESOLVED 2026-09-13** — Zapmail confirmed a
   cancellation filed even **1 day** before the billing date still optimises that cycle.
   There is no long cut-off. Set to 2 days as operational slack only. Hard-stop dates are
   no longer provisional.
5. **Cost model is mailbox-only.** The 2026-09-12 audit shows domain renewals are a second
   ~$22k/yr bill, and per-mailbox removal does **not** reduce the Zapmail bill — the unit of
   cancellation is the domain. `removal_plan()`'s "unpick" pile saves nothing; hard-stop maths
   should become domain-led.
6. **Depends on a health sync that is down** since 2026-09-10 (SmartLead login 401), so the
   client↔mailbox join is stale and drifting.

Confirmed terms to load once (1) and (2) land: Galaxy 3mo, McFarlane Douglass 3mo,
Wonderly 2mo (bi-weekly), Landry's / Merry & Bright / Mary & Brite's prepaid in full.
From The Ground Up has churned.

## Added 2026-09-13 — `domain_expiry_alert.py`

The audit called this "the single highest-value thing to build" and it is now
written. It joins the two signals that were already pulled daily but never
compared:

* registrar (Spaceship + Porkbun) — auto-renew OFF, expiring within 7/30/90 days
* SmartLead — this domain's mailboxes are on an **ACTIVE** campaign

and alerts when both are true. It also reports the mirror case, auto-renew ON on
a domain with zero mailboxes.

Why it exists: on 2026-09-13, 23 domains carrying **69 live senders on active
campaigns** were set to lapse with auto-renew off, one of them five days out.
Total cost to keep all 23 was **$244**. Nobody decided that — auto-renew gets
switched off during an off-boarding while a domain looks empty, its inboxes are
later recycled to a live client, and nothing re-checks.

Verified against live registrars: 1,307 domains (1,055 Spaceship / 252 Porkbun),
798 auto-renew on / 509 off, 229 expiring within 90 days.

Wired into the daily `health-snapshot` cron and exposed at `/api/domain-expiry`
(`?alert=1` to see the Slack text). Both touch points are in `api/index.py`,
which is NOT committed here because that file carries other sessions' work.

**Never read a renewal flag from Zapmail** — `autoRenew` is `false` on all 897
domains and means nothing. The registrar is the only real switch.
