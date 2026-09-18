# Testing the infrastructure dashboard

Fifteen minutes, in order, at **https://email-infra.vercel.app/dashboard**.
Sign in once when prompted; the password is stored in a cookie, never in a link.

Each item says what to do, what you should see, and **what it means if you see
something else** — that last column is the point. A dashboard that looks fine
tells you nothing; these checks are chosen because each one has failed before.

---

## 0. Before anything — is what you are reading current?

| Check | Expected | If not |
|---|---|---|
| The grey line beside the title | `clients Xm ago · rules Xh ago` | If "rules" is older than ~24h, the nightly GitHub Action has not run. The invariants are served **from storage**; a stale run showed rule 4 failing for a day after the thing it checks was fixed. Run the `Zapmail removal watch` workflow manually. |
| Any yellow banner | none, or one you understand | A banner means a data source could not be read. **Nothing below a banner is a clean bill of health** — it is the absence of an answer. |

---

## 1. Overview

- **Rules failing** should be a small number you recognise. Click through and
  read each failure; the violation lines name the exact client or domain.
- **Onboarding owed** and **Acq. free** are pulled from other tabs and appear a
  moment after the page paints. That delay is deliberate — the page no longer
  waits on the slowest feed before showing you anything.

> A rule that reads **SKIP** has not passed. It means the check could not see
> its input. Treat SKIP as "unknown", never as "fine".

---

## 2. Clients

| Check | Expected |
|---|---|
| Every row's **Δ** | `0`, or a number you can explain |
| **Term basis** highlighted in yellow | that deadline was *assumed*, not recorded — worth fixing in the CRM |
| A free account (Landy Rose Media) | `free account — no term`, no dates, no Δ |

If a client shows a term you never agreed, that is the engine's fallback guess
showing through. Put the real term in the CRM rather than trusting the date.

---

## 3. Onboarding — the CRM→infra handoff

This answers "what has infrastructure not yet delivered for a client the CRM
says is live". It **reports and never acts**; the buttons hand you to the tab
that owns each fix.

| Check | Expected |
|---|---|
| Fully onboarded | most of the roster |
| Anything blocked on **No inboxes at all** | investigate immediately — a client is live and cannot send |
| Anything blocked on **forwarding** | usually a missing `website` in the CRM |

> Order matters: a client with no inboxes is **not** also reported for
> forwarding. Fix the earlier step and the later one re-evaluates.

---

## 4. Inboxes — burn rate and the replacement loop

| Check | Expected | If not |
|---|---|---|
| **Burn rate** | a number per week, or "not yet known" | "not yet known" is correct until two daily snapshots exist. It is deliberately **not** shown as 0 — a rate nobody has measured is not the same as nothing burning. |
| **Runway** | pool ÷ rate, in weeks | under 4 weeks means you will run out of replacements before the burn stops |
| **Needs replacing** | the burned inboxes on a **live** client | burned inboxes already in a Cleanup bucket are listed separately and need nothing |

### Test the replacement loop without changing anything

1. Tick one burned inbox.
2. Press **1 · Check positive replies** — read-only, always safe.
3. The **Positives** column fills in, and the banner says how many would be held.

Stop there. Step 2 (*Flag for replacement*) stays disabled until step 1 has run
for the current selection, and re-locks if you change the selection. That lock
is the safety: a reply lives in the mailbox that sent it and is welded to it by
message-id, so detaching a mailbox that owns one freezes the conversation.

---

## 5. Acquisition

| Check | Expected |
|---|---|
| **In use** | ~99% |
| **Free to reallocate** | small; these are genuinely idle |
| **Over 3 / domain** | `0` |

> **In use** counts every working state: sending to new leads, sending
> follow-ups, *and* sitting between sequence steps. Low daily volume is not idle
> capacity — a sequence spaces its touches on purpose. If this number drops, ask
> whether campaigns ran out of **leads** before you buy more inboxes.

Numbers come from a live Smartlead walk rebuilt daily. `?refresh=1` forces it.

---

## 6. Buy — without spending anything

1. Owner **Acquisition** → press **Suggest**.
   Every candidate must contain **both** words of the brand
   (`headlinetheoryhq.info`, not `headlineconnect.info`).
   There is no vertical picker here; acquisition buys our own brand.
2. Tick two, press **Price it up**. You get counts and a cost. Nothing is bought.
3. **Stop.** *Register* spends money and names the figure first.

Provisioning answers `resume` while DNS propagates — that is the normal state
for the first hour, not a failure. It picks up where it left off.

---

## 7. Pools

Stock that can still be given to a client. **Acquisition is deliberately not
here** — it is our own prospecting fleet, already deployed, with its own tab.
Listing it in both places double-counted it.

---

## 8. Once a month

- Run the `Zapmail removal watch` workflow manually and read its summary.
- Check **#zapmail-billing** for removal confirmations; a scheduled mailbox that
  never confirms means Zapmail did not act. Zapmail exposes **no**
  scheduled-for-removal flag, so the registry and that channel are the only
  record that we asked.
- Re-read `INFRA_RULES.md` rule 11 before trusting any new number that suggests
  cancelling something.

---

## What a failure looks like

Almost every real bug found in this system looked like a **confident wrong
number**, not an error message:

- 214 acquisition inboxes when there were 307
- 286 empty domains costing $6,797/yr when the true answer was 0
- 0 mis-forwarded domains while 38 pointed at the wrong company
- a 57-inbox client reported as having no infrastructure at all

So the most useful thing you can do on this dashboard is notice when a number
*moves without a reason*, and say so.
