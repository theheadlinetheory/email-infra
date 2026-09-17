# Sending infrastructure — SOP

How the fleet is operated day to day. The rules it enforces are in
[INFRA_RULES.md](INFRA_RULES.md); this is who does what, when, and what to do
when one goes red.

**Principle: a person should never be the thing that notices.** Every rule is
checked by a script on a schedule. A human's job is to act on what it found, not
to remember to look.

---

## Daily — automatic, 13:00 UTC

One Vercel cron (`/api/health-snapshot`) runs the whole daily pass. It is one
cron because the Hobby plan allows one; each job is isolated in its own
try/except so a failure in one cannot take down the others.

| Job | Posts when | Channel |
|---|---|---|
| Health snapshot | — | — |
| Zapmail removal watcher | a mailbox actually disappears | #zapmail-billing |
| **Renewal decision countdown** | a client is 7 / 3 / 1 days from their decision date | **#infra-renewals** (tags Aidan) |
| **Unowned infrastructure** | any of rules 1, 2, 5, 9 is red | #infra-renewals (no tag — Tim's to fix) |
| Domain expiry | rule 6 or 7 is red | #zapmail-billing |
| Zapmail billing | rule 8 is red | #zapmail-billing |

**Silence means clean.** Every one of these is silent when there is nothing to
report, so a quiet channel is a real signal — which is why a broken run must
never be silent.

### Confirming the run happened

State key `infra_lifecycle_last_run`, readable with the ordinary Supabase key:

```json
{"ok": true, "at": "...", "rows": 23, "notices": 0, "unowned": 2,
 "webhooks": ["SLACK_INFRA_DECISIONS_WEBHOOK", ...]}
```

- `ok: false` → the countdown did not run. It also posts a 🚨 to Slack.
- Heartbeat older than ~26h → the cron did not fire.
- `SLACK_INFRA_DECISIONS_WEBHOOK` missing from `webhooks` → decision notices are
  falling back to #zapmail-billing.

> This exists because the countdown was dead for five days in September and
> nothing said so — a missing CRM key made it raise on every run, and the
> exception went into a JSON field nobody reads. Silence from a countdown is
> indistinguishable from "nothing is due".

---

## When a notice fires

### Renewal decision (7 / 3 / 1 days) — Aidan
Answer renew or stop in #infra-renewals before the decision date.
**No answer is an answer: the infrastructure is released.** The client gets an
undisclosed one-week grace period after their term ends; the decision date is
the end of that grace, or the last day we can cancel before the next billing
date, whichever comes first.

### Zapmail billing gap — Tim
Copy the message out of the alert, send it to Zapmail, **ask for confirmation**.
Recurring, not one-off: cancelled mailboxes drop on their own billing
anniversaries, so expect it roughly weekly while a cancellation batch drains.

### Unowned infrastructure — Tim
Whatever it names: add the CRM row, fix the tag, trim the pool. It will keep
saying so daily until it is true.

### Domain expiry — Tim
Auto-renew ON at the registrar for anything carrying a live sender; OFF for
anything empty.

---

## Onboarding a client

**The decision is made in the CRM. The execution happens in the infra
dashboard.** Sales onboards the client; infrastructure is then allocated
against that row. The CRM row must exist **first** — without it there is no
launch date, so no term, so no decision date, and the client is invisible to the
countdown (rule 5).

1. **CRM** — create the client. Required before anything else:
   `name`, `billing_model`, `agreement_type`, `payment_terms`,
   `initial_term_length` + `initial_term_unit`, `monthly_retainer`.
   Leave `launch_date` empty; it is set at first send.
2. **Infra dashboard** — allocate 42 inboxes (57 for holiday lighting or snow),
   as **whole domains**, from the reserve. Never split a domain (rule 3).
3. **Forwarding** — set every allocated domain to the client's website, then
   read it back (rule 4).
4. **Tag** — one tag per client, the CRM name, no Group A/B (rule 2).
5. **Launch** — attach inboxes to the campaign, start it.
6. **Back-fill `launch_date`** with the date of the first send. The term clock
   starts then, not at signature.

If the reserve cannot cover it, buy — see below.

---

## Off-boarding a client

**Recycling is not a saving.** Moving inboxes into the reserve re-tags them and
nothing else: the bill does not move, the spend just stops being attributable to
anyone. That single habit grew the pool 40 → 453 and cost roughly $89k/yr.

Every off-board is an explicit choice per domain, with the money shown:

1. **Cancel** — the pool is at ceiling, or the domain is client-branded and
   unusable elsewhere. Cancel at Zapmail, auto-renew OFF at the registrar.
2. **Recycle** — only while the reserve is **under** its ceiling (rule 9), and
   only for generically-named domains.

Then, always:

- **Strip forwarding** on every freed domain, or it keeps redirecting prospects
  to an ex-client's website.
- **Remove the client from the dashboard.** Off-boarding is what takes them off
  the Clients tab.
- **Register the cancellation** with the removal watcher, so Zapmail gets asked
  to reduce the billed quantity when the mailboxes actually drop (rule 8).

---

## Replacing a burned inbox

The flow that works today, unchanged:

1. Health tab flags the burned inbox.
2. **Check it for positive replies first.** An inbox that owns a live
   conversation is held, not swapped — the thread is anchored to that sender
   and reallocating does not move it.
3. Allocate a replacement **from the matching vertical's reserve**.
4. The dashboard gives you the campaign link.
5. **You click "Reallocate Mailboxes" in Smartlead.** There is no API for this
   step. Nothing rebinds in-flight sends until you do.

**Service inboxes have no reserve.** All 129 reserve inboxes across 43 domains
are landscaping — verified 2026-09-18, zero HVAC/plumbing/repair domains. Until
a service pool is bought, a burned service inbox is left in place rather than
replaced from a landscaping domain.

---

## Buying inboxes

**Google only, via Zapmail, on Spaceship domains. Never Outlook** — the Outlook
batch was retired after replying at roughly a quarter of the Google rate.

Two shapes:
- **Custom** — client-branded domains, for a named client.
- **Generic service** — the service-vertical reserve. Not landscaping, not
  holiday lighting, not HVAC-specific. Generic service.

Provisioning routinely takes **over an hour**. `IN_PROGRESS` past 60 minutes is
normal and not a failure; the run resumes rather than re-buying. When mailboxes
exist, they upload to Smartlead and warm-up starts automatically.

**Warm-up is 14 days, and it starts the billing clock.** A client cannot send
until it finishes, but Zapmail bills from the mailbox's `createdAt` — which is
why a 2-month engagement spans 3 billing cycles.

---

## Weekly — 10 minutes

1. Run the invariant check; confirm all ten rules pass.
2. Reserve and replacement pools within ceilings, and the split still makes
   sense against clients likely to sign.
3. Every client's decision date still matches their CRM term.
4. Acquisition capacity: are warmed inboxes actually sending?

---

## Monthly

1. Zapmail billed quantity == actual mailboxes, both providers (rule 8).
2. Registrar bill against the domains actually carrying senders.
3. Every client at target; investigate any drift rather than correcting it
   silently — drift means a process leaked.

---

## Things that have gone wrong, and the habit that prevents each

| Habit | Because |
|---|---|
| Read results back from the source after every write | Porkbun reported SUCCESS on 52 domains while changing nothing |
| Page every database read | A 1000-row cap read as the whole table, twice in one day |
| Assert a scan found something before trusting "not found" | A fleet-wide campaign pause made every inbox look unused |
| Check positive replies before touching an inbox | That distinction alone was worth ~$14k/yr |
| Sweep forwarding after any bulk re-tag | 38 live domains pointed at the wrong company |
| Never let an "assumed" term reach a deadline | Wonderly's decision date was a month late |
| Registrar is the kill switch, not Zapmail | Zapmail's `autoRenew` reads false on all 897 domains |
