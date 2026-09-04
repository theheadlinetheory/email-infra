---
name: smartlead-orphaned-thread-reply
description: Use when a lead replied but we can no longer reply to them in SmartLead because the sending inbox was burned, swapped or removed — the master inbox shows "Email account '<addr>' critical for lead communication has been removed. To resume communication, please add the mailbox", clicking Add Account leads nowhere, or a reply fails after a burned-inbox replacement. Also covers "reallocate mailboxes didn't reattach the thread", orphaned/stuck threads, and hot leads unreachable after an inbox swap.
---

# Replying in a thread whose sending inbox is gone

## The situation

A lead replied. The inbox that sent the original email has since been burned,
swapped out and deleted from SmartLead. The master inbox now shows a red banner:

> Email account 'sean.r@callbackpros.info' critical for lead communication has
> been removed. To resume communication, please add the mailbox.

"Add Account" opens the add-mailbox modal, which is useless — the mailbox itself
no longer exists at the provider. **The thread is read-only inside SmartLead,
permanently.** There is no setting that fixes this.

## Why "Reallocate mailboxes" does not fix it

This is the part everyone gets wrong, so state it plainly when asked:

**"Reallocate mailboxes" redistributes a campaign's LIVE UNSENT lead queue onto
its current senders. It never re-binds an existing conversation.** Our own code
says so — see `reallocation_campaigns()` in `health_replace.py`.

A lead who has already replied is *out of the sequence*. Their thread stays
bound to the account that sent step 1, for the life of the thread. Delete that
account and the conversation is orphaned. Swapping senders, re-tagging, and
hitting Reallocate all operate on future sends and do nothing here.

## Can we just re-add the old mailbox?

Almost never, but check — it is the cleanest fix when it works:

```bash
dig @8.8.8.8 MX <burned-domain>      # empty MX  -> mailbox is gone
dig @8.8.8.8 NS <burned-domain>      # ClouDNS but empty zone -> Zapmail stripped it
```

Zapmail deletes mailboxes on the domain's **monthly billing date** and strips
MX/SPF/DKIM/DMARC, leaving the domain registered with an empty zone. No MX means
no mailbox to reconnect and no inbound path — do not waste time on Add Account.

Note SMTP send does not need MX, so a mailbox that still exists could in theory
send. But with MX gone the lead's reply would vanish, so it is never the answer.

## The fix: send it yourself, with the threading headers

Gmail and Outlook thread on the `References` / `In-Reply-To` headers, not on the
From address. So a message sent from a *different* live inbox still lands inside
the existing conversation — as long as it carries the original Message-IDs.
SmartLead's message-history API hands them over.

`reply_in_thread.py` (this folder) does the whole thing.

### 1. Find the campaign and lead

The banner names the dead inbox but not the thread. Find it by the lead's reply:

```bash
# campaigns for the client
GET /campaigns                                    -> match on name
# who replied, with stats_id and subject
GET /campaigns/{cid}/statistics?email_status=replied&limit=500
# resolve an email to a lead id
GET /leads/?email=<addr>                          -> .id
```

### 2. Read the thread

```bash
python reply_in_thread.py --campaign <cid> --lead <lead_id> --show
```

Prints every message with its real `message_id`. Confirm the burned address is
the sender and note who actually replied.

### 3. Pick a live sending inbox

`GET /campaigns/{cid}/email-accounts`. Choose one that is **already on this
campaign**, with the **same persona name** and ideally the same local part
(`sean.r@`) as the burned inbox. Verify it is healthy before sending:

```bash
dig @8.8.8.8 MX <domain>       # expect smtp.google.com
dig @8.8.8.8 TXT <domain>      # expect v=spf1 include:_spf.google.com
```

**Whoever manages replies must be able to open that mailbox in Gmail** — see the
handoff section. Confirm this before choosing, not after.

### 4. Get the copy from whoever owns reply voice

Put it in a plain text file and send it **verbatim**. This is their copy, not
ours. Raise factual conflicts once (a proposed date that is a public holiday, a
phone number the lead asked for and never received) and then send what they
wrote.

### 5. Dry run, then send

```bash
python reply_in_thread.py --campaign <cid> --lead <lead_id> \
    --from <live-inbox> --body reply.txt                 # prints the MIME
python reply_in_thread.py ... --send --verify --pass-file /path/to/pw
```

The script auto-resolves recipient, subject, `In-Reply-To`, `References` and the
sender display name from SmartLead. `--verify` re-opens the mailbox over IMAP and
confirms the message is in Sent with `In-Reply-To` intact.

## The app password (the step that actually blocks you)

SMTP needs a Google **app password** — 16 lowercase letters, no symbols. The
mailbox's normal password fails with:

```
534 5.7.9 Application-specific password required
```

On a **Zapmail-provisioned mailbox**, Zapmail's dashboard shows the account
password and a 30-second rotating code, and no app-password option — because
app passwords are issued by Google, not Zapmail. That rotating code is the 2FA
token, which is what you use to get in:

1. `accounts.google.com` in an incognito window
2. the mailbox address + the password from Zapmail
3. at the 2-step prompt, paste Zapmail's 30-second code
4. `myaccount.google.com/apppasswords` -> create -> copy the 16 letters

Write it to a file for `--pass-file` rather than pasting it into chat. **Revoke
it once the send is done**, and rotate the account password if it was exposed.

If `apppasswords` 404s, the Workspace admin has them blocked — fall back below.

## Fallback when SMTP is impossible

Have the reply manager send it by hand from the live inbox in Gmail, with
subject `Re: <original subject>` and the lead's last message quoted underneath.
Gmail cannot set `In-Reply-To`, so it starts a **new** conversation on the
lead's side rather than joining the old one — but same subject plus the quote
still reads as a continuation. In this case **add a line acknowledging the new
address**, since it arrives as a fresh thread from an unfamiliar sender.

## Handoff — do not skip this

The send is invisible to SmartLead. Two consequences, both of which have to be
briefed to whoever manages replies:

- **The lead's reply lands in the sending mailbox's real Gmail inbox, NOT the
  SmartLead master inbox.** If nobody is watching that mailbox, the reply is
  lost and the whole exercise was pointless.
- **The SmartLead thread still looks dead**, ending at the last failed send.
  Note it somewhere so nobody follows up on top of the manual reply.

From the lead's next reply onward the conversation lives in a mailbox we
control, so normal Gmail Reply works — the header surgery is one-time.

## Prevention

Before deleting a burned inbox, check whether any of its threads have live
replies. A swap is safe for the unsent queue; it orphans every open
conversation. Bridge those first, or keep the domain alive until they close.

## Worked example

Quantum HVAC, Sept 2026. `sean.r@callbackpros.info` burned and swapped; Zapmail
deleted it Sept 1 (empty MX, SOA serial `2026090112`). Lead had replied Aug 29
naming three restaurants and asking for a callback; the Sept 2 follow-up failed
with "Email credits exhausted or plan end date expired". Recovered by sending
from `sean.r@propertylandscapeservices.info` (campaign 3792117, lead 4332945104)
with `In-Reply-To` set to the lead's Gmail Message-ID. Verified in Sent, no
bounce.
