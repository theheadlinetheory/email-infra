# The ten rules

Ten statements that are either true of the sending fleet or not. Each one is
here because it was false at some point in September 2026 and cost money or
broke a client's sending while every dashboard showed green.

They are written to be **checked by a script, not by judgement**. Where a rule
says "verify", it means read it back from the source of truth — never trust an
API's own success reply, and never trust a cache.

**Source of truth, in order:**

| Question | Ask | Never ask |
|---|---|---|
| Who owns this inbox? | the Smartlead **tag** | `health_status.client` — it lags and omits idle inboxes |
| Does this mailbox exist? | Zapmail `/v2/domains` | Smartlead — it keeps dead accounts |
| Is this domain renewing? | the **registrar** (Spaceship/Porkbun) | Zapmail `autoRenew` — it reads `false` on all 897 |
| What are we billed for? | Zapmail subscriptions, `ACTIVE` only | the total across all subscriptions |
| What is this client's term? | CRM `initial_term_length` + `_unit` | a 3-month assumption |

---

## 1. Every active client has exactly 42 inboxes — 57 for holiday lighting and snow

**Why.** A client at 44 is paying for two inboxes nobody planned; at 54 they are
short of the capacity they bought. Both happened: Northstar sat at 44 after two
strays were re-tagged in, Merry & Bright read 57 while three of their mailboxes
had already expired at Zapmail, so they were really at 54.

**Check.** Count Smartlead accounts per client tag. Compare to 42, or 57 for a
client whose vertical is holiday lighting or snow removal.

**Exempt.** Free accounts (no retainer AND monthly updates off) — Landy Rose
Media is at 18 by design.

**Fix.** Over: cancel the excess at Zapmail (mailbox-level), re-tag to the
current Cleanup bucket. Under: allocate from the reserve, one whole domain at a
time, and set that domain's forwarding before it sends.

---

## 2. Every inbox carries exactly one client tag. Zero untagged

**Why.** An untagged inbox is invisible to every per-client rollup, so it is
billed forever and belongs to nobody. On 2026-09-18 there were 26 — eighteen of
them Landy Rose's entire allocation, which is why she simultaneously read as
"active client with no inboxes".

**Check.** For every Smartlead account, count tags that are not `Zapmail` and
not an `M/D/YY` warm-up date. Must be exactly 1.

**Fix.** Read the other mailboxes on the same domain — an untagged inbox almost
always belongs where its siblings do. Only use a pool tag when the domain has no
other owner.

**No Group A/B.** One tag per client, no rotation suffix.

---

## 3. Every domain belongs to exactly one client

**Why.** Forwarding is a property of the **domain**, allocation is a property of
the **mailbox**. A domain shared by two clients cannot forward correctly for
both. 53 domains were shared three ways, which is how one client's prospects
were redirected to another client's website.

**Check.** Group Smartlead accounts by domain; the set of client tags on each
domain must have size 1.

**Fix.** Move whole domains, never individual mailboxes, when reallocating.

---

## 4. Every domain forwards to its owning client's website

**Why.** A prospect who types the domain into a browser lands somewhere. On
2026-09-14, 38 live domains pointed at the wrong company — Clear Heating's
prospects landed on Quantum's site, a direct competitor in the same trade, and
Northstar had **zero** domains pointing at northstarhvacr.net.

**Check.** Join Zapmail `forwardTo` against the owning client's website. Pull
domains from BOTH `x-service-provider` values — a GOOGLE-headed call cannot see
MICROSOFT inventory and returns 200 either way.

**Fix.** `POST /api/v2/domains/forwarding`, then read it back.

**Trigger.** Any bulk re-tag or domain re-point must be followed by a forwarding
sweep. Moving mailboxes does not move forwarding, and nothing else reveals it.

---

## 5. Every client with live infrastructure has a CRM row with a launch date and a term

**Why.** No CRM row means no launch date, so no term, so no decision date and no
hard stop — the client is **absent** from the renewal countdown rather than
overdue in it. LightDMV ran 57 inboxes for two days in exactly this state, and
Wonderly did the same in August.

**Check.** Every Smartlead client tag maps to a CRM row with `launch_date` and
`initial_term_length`. And the reverse: every active CRM client has inboxes.

**Fix.** Create the row. `launch_date` is the day of the **first email sent**,
not the contract date — confirmed on the 2026-09-10 call.

**Never ship an assumed term.** If `end_basis` starts with "assumed", the
deadline is one nobody agreed to. Wonderly's real 2-month agreement had been
guessed as 3, putting their decision date a month late.

---

## 6. No domain carrying a live sender expires within 45 days

**Why.** The domain lapses, every mailbox on it dies, and the campaign silently
stops. 8 domains carrying 22 live senders were days from this for the sake of
$244 in renewals.

**Check.** Join registrar expiry + auto-renew against "this domain has a mailbox
on an ACTIVE campaign".

**Fix.** Turn auto-renew ON at the **registrar**. Zapmail's `autoRenew` field is
inert — it reads `false` on all 897 domains and setting it changes nothing.

---

## 7. No empty domain is auto-renewing

**Why.** A domain with zero mailboxes sends nothing and costs $11–31/yr. 186 of
them were renewing after their clients churned — $3,692/yr for nothing.

**Check.** Zapmail domains with zero mailboxes, intersected with registrar
auto-renew ON.

**Fix.** Auto-renew OFF at the registrar, then read it back.

⚠️ **Porkbun's envelope lies.** The response says `{"status":"SUCCESS"}` at the
top level while the per-domain result inside says
`{"status":"ERROR","message":"Domain is not opted in to API access"}`. Reading
the envelope caused 52 silent failures. Read the inner result.

---

## 8. Zapmail's billed mailbox quantity equals the mailboxes that actually exist

**Why.** Cancelling a mailbox stops it existing. **It does not stop the
billing.** Zapmail reduces the subscription quantity only when asked, every
time. 72 phantom slots were billing at $2,592/yr.

**Check.** Sum `totalMailboxQuantity` over subscriptions with
`subscriptionStatus == "ACTIVE"` — including CANCELLED ones overstates it by
hundreds — and compare to the live mailbox count, per provider.

**Fix.** Message Zapmail with the numbers and ask them to reduce the quantity.
The daily alert carries the ready-to-send text. Ask for confirmation: a previous
batch was told "we'll optimise" and the quantity never moved.

---

## 9. The reserve holds at most 84 inboxes; the replacement pool at most 42

**Why.** This is the rule whose absence caused everything else. Off-boarding a
client **recycled** its inboxes into the reserve instead of cancelling them, so
the bill never moved and the spend simply stopped being attributable to anyone.
The pool went 40 → 453 inboxes in five weeks while every client row stayed
correct.

**The numbers.** Reserve = `Generic Landscaping 1` + `Generic Landscaping 2`, 42
each, held for the next two clients we sign. Replacement = a separate
`Replacement Group`, for swapping burned inboxes. They are not the same pool and
are counted separately.

**Acquisition is excluded.** Our own prospecting is a deliberate spend with its
own budget, not idle stock. Counting its ~277 inboxes makes the ceiling
meaningless.

**Check.** Count each pool tag. Over the ceiling means cancel the excess or
raise the ceiling deliberately — never let it drift.

---

## 10. Every mailbox that has expired at Zapmail is gone from Smartlead

**Why.** A Smartlead account whose mailbox no longer exists can never
authenticate. It sits in the "needs attention" backlog forever, still counts
toward its client's total, and can still be recruited onto a campaign.

**Check.** Smartlead accounts whose address has no live Zapmail mailbox.

**Exclude** the externally-hosted domains — `headlinetheory{go,hq,hub,info,now,
one,online,plus,pro,world,yes}.com` were never in Zapmail, so their absence
means nothing.

**Fix.** Purge via `health_replace.purge_removed_accounts(..., force=True)`.
Delete only after the mailbox has actually expired at Zapmail, never on the
strength of a scheduled cancellation.

---

## Two traps that break any check above

**A truncated read looks exactly like a small table.** PostgREST caps an
unbounded `select` at **1000 rows** and returns it as an ordinary 200. A count
of exactly 1000 is the tell. Page every read, or ask for `Content-Range` first.
This bit twice in one day, including inside `inbox-sync`'s own prune, which
therefore measured its safety guard against a table smaller than the real one.

**A paused fleet makes every usage check answer "no".** On 2026-09-17 all 39
live campaigns were paused at one timestamp because the Smartlead account ran
out of credits. Any gate that asks "is this inbox on a live campaign?" then
fails open across the whole fleet and reports everything as safe to delete.

> **Assert `live_campaigns > 0` and `scan_failures == 0` before believing any
> answer derived from campaign membership.** An empty scan is not evidence of
> absence.
