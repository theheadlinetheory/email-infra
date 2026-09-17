# API audit — 2026-09-18

Phase 0 of the dashboard revamp: establish which of the existing API actually
works before building a new UI on top of it.

**Method.** The Flask app was booted locally against live credentials and every
read-only route was called and its response inspected — status, latency, payload
size, and whether the numbers match what Smartlead/Zapmail actually hold. The 51
mutating routes were **not executed**: they write to production. They were read
instead, and classified by what they call and whether anything still reaches
them.

**Headline: nothing is broken, but three things make the current dashboard
untrustworthy — a stale cache, response times up to two minutes, and payloads up
to 1.6 MB.**

---

## Surface

| | Count |
|---|---|
| Routes | 83 |
| Read-only (GET) | 32 — 27 testable, 5 are static/catch-all |
| Mutating (POST/PUT/DELETE) | 51 |
| Mutating routes with a `dry_run` option | **5 of 51** |
| Mutating routes no HTML references | 10 |

---

## Read-only: all 27 return 200

No route is dead or erroring. `/api/group/wizard-state` returns 400 with
`"letter required"` when called without a parameter, which is correct.

### But four are too slow to render a dashboard

| Route | Time | Payload |
|---|---|---|
| `/api/infra-lifecycle` | **120s** | 128 KB |
| `/api/domain-expiry` | **98s** | 181 B |
| `/api/generic-capacity` | **77s** | 863 KB |
| `/api/buy-orders` | **47s** | 5.8 KB — for 2 orders |
| `/api/free-capacity` | 37s | 974 B |
| `/api/available-domains` | 18s | 25 KB |
| `/api/acq-capacity` | 13s | 194 KB |

These all rebuild their whole world on every request — a full Zapmail walk, a
full Smartlead walk, sometimes both. Vercel's ceiling is 300s so they do not
time out, but a tab that takes two minutes is a tab nobody opens.

`/api/domain-expiry` is the clearest case: **98 seconds to return 181 bytes.**

### And four ship too much to a browser

| Route | Payload |
|---|---|
| `/api/overview` | **1.59 MB** |
| `/api/health-fleet` | **1.03 MB** |
| `/api/generic-capacity` | 863 KB |
| `/api/health-renewals` | 609 KB — 1,506 rows |

---

## The Clients tab problem, precisely

`/api/overview` returns `_cached: true`, `_synced_at: 2026-09-17T04:07` — a
**day-old cache** — and lists **51 clients** when 21 are active. That is exactly
the complaint: churned clients never leave.

The per-client inbox counts inside it are correct (0 off 42/57), so the
arithmetic is fine. The roster is not. Two separate fixes:

1. Filter to active clients, and make off-boarding the thing that removes them.
2. Stamp the cache age on screen, or serve it live.

---

## Mutating routes

Not executed. Classified by reading them.

### Reachable and load-bearing — keep

The replacement flow that works today, and the buying wizard:

```
health-burn · health-positive-check · health-positive-release · health-replace
health-replace-all · health-replace/advance · health-reallocate
health-reallocate-campaigns · health-recover · health-resolve
health-cancel-domain · health-snapshot
buy-suggest · buy-plan · buy-domains · buy-provision
group/setup-domain · group/setup-tags · group/connect-domains
group/export-smartlead · group/finalize-account · group/find-smartlead-accounts
group/check-mailbox-status · group/save-state
domains/find-available · domains/purchase · domains/purchase-one
domains/auto-renew · domains/sync-registrar
client-offboard · assign-generic-to-client · acq-allocate
```

### Cron or Slack driven, correctly absent from the UI

`billing-followup` · `zapmail-removals` · `infra-lifecycle/decision`

### No caller anywhere — candidates to delete

`buy-check` · `domains/check` · `group/fix-photos` · `health-placement` ·
`health-retire` · `replacements/delete` · `create-generic-group`

`create-generic-group` (155 lines) appears superseded by
`finalize-generic-group` (326 lines). Both exist; only the second is called.

### Three functions are large enough to be a risk on their own

| Route | Lines |
|---|---|
| `finalize-generic-group` | 326 |
| `assign-generic-to-client` | 214 |
| `group/setup-domain` | 196 |

Each performs a multi-step irreversible sequence — buy, connect, tag, export —
with no `dry_run` and no tests. These are the routes most likely to half-complete
and leave the fleet in a state no rule describes.

---

## The `dry_run` gap

**5 of 51 mutating routes support a dry run**: `health-burn`, `health-recover`,
`health-resolve`, `health-retire`, `health-cancel-domain`, `zapmail-removals`.

Everything that buys domains, provisions mailboxes, re-tags inboxes, assigns
generics or off-boards a client commits immediately. In a system where a wrong
write cancels live client inboxes, that is the single largest correctness gap in
the API.

---

## What this means for the revamp

**Keep.** The health/replace flow and the buying wizard are real, complete, and
worth preserving as-is. This is not a rewrite.

**Fix, in order:**

1. **Cache discipline.** Every payload carries its age; the UI shows it. A number
   with no timestamp is how the Clients tab ended up a day stale with 51 clients.
2. **Split the slow routes.** A summary endpoint that answers in under a second,
   and detail fetched on demand. Nothing on first paint should take 120 seconds.
3. **Active-only client roster**, with off-boarding as the removal trigger.
4. **`dry_run` on every mutating route**, and the UI defaults to previewing.
5. **Delete the seven uncalled routes** and the superseded
   `create-generic-group`.

**Note.** The fleet was fully paused on 2026-09-17 (Smartlead out of credits) and
is running again — 41 ACTIVE campaigns at the time of this audit. Any capacity
number captured during a pause is meaningless, which is itself an argument for
the staleness stamps above.
