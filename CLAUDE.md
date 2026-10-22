# email-infra — working notes

## Deploys: never put a `Co-Authored-By` trailer on a commit here

This project is on Vercel **Hobby**, which refuses to build a commit whose
author *or co-author* is not on the Vercel team. A commit carrying

    Co-Authored-By: Claude <noreply@anthropic.com>

gets **no deployment and no commit status at all** — not a red check, nothing.
The merge looks completely clean on GitHub and production silently keeps
running the previous commit.

This has already cost two days twice over. PR #164 fixed burn tracking, merged
as `1239042` on 2026-10-21, and never shipped: the daily Slack alert kept firing
from the old code, and two sessions "fixed" the bug without noticing nothing had
deployed.

**So, in this repo:**

1. Leave the trailer off the commit **and** off the squash-merge message.
   `gh pr merge --squash --subject ... --body ...` writes the final message —
   pass both explicitly, because the generated default inherits the trailer from
   the branch commits.
2. **After every merge, confirm it deployed.** A merge is not a release:

   ```bash
   gh api repos/theheadlinetheory/email-infra/commits/<sha>/status \
     -q '"\(.state) \(.statuses | length)"'
   ```

   `success 1` is deployed. `pending 0` means Vercel never saw it.
3. To rescue an undeployed commit, push any trailer-free commit to `main`
   (an empty one is enough) or hit Redeploy in the Vercel dashboard.

## Burn tracking: what breaks and what it is not

The daily health snapshot reads Smartlead's internal analytics endpoint
(`/api/analytics/mailbox/name-wise-health-metrics`) with a JWT minted from
Tim's login. Two different failures produce the same "only 0 records — HTTP 200
with an empty result" alert:

- **A blank session.** Smartlead intermittently hands a *team-member* login a
  session that authenticates but sees nothing: the login is a 200, the token
  verifies, and every endpoint including `auth/me` answers
  `{"ok":true,"data":[]}`. Seen 2026-09-24 and 2026-10-22; it cleared itself in
  ~30 minutes with no change on our side. `health_daily.session_sees_account()`
  is the one cheap test that tells this apart from a genuinely empty window —
  a healthy `auth/me` is ~327 bytes of `{"user":{...}}`, a blank one 21 bytes.
- **Production simply not running the fix.** See the section above. Check this
  *first*: it is free, and it has been the real answer more often than not.

**Do not re-debug the credentials when this alert fires with `login_http: 200`
and `minted: true`.** That has been ruled out repeatedly — minting works, two
concurrently minted tokens both work, and the same code and `.env` read 1,000+
rows from a laptop at the moment Vercel sees zero.

## Running the snapshot by hand

Prefer triggering the Vercel route (`workflow_dispatch` on `zapmail-watch`), as
a local run does not do the same thing:

- `db._CACHE_WRITE_ENABLED` is False unless set, and only the Vercel
  `/api/health-snapshot` route turns it on. A local run writes
  `inbox_health_status`, the daily rows and `burn_streaks`, but **not** the
  `health_fleet` cache the Inboxes tab reads — so the dashboard does not move
  and the "snapshot failed" state stays latched.
- Without `today=`, it takes the machine's date. Run from a timezone ahead of
  New York and every burn streak advances a day early.
- The local `.env` has CRLF line endings: `source <(tr -d '\r' < .env)`.

## CI

`zapmail-watch` runs four times a day. One failing step must never skip the
others — see `test_workflow_steps_do_not_block.py`; steps that must be loud are
`continue-on-error` plus a final `always()` gate that fails the job.
