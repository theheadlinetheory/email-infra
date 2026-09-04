#!/usr/bin/env python3
"""Reply inside a SmartLead conversation whose sending inbox no longer exists.

When a burned inbox is swapped out and deleted, SmartLead pins the thread to
the dead account forever ("Reallocate mailboxes" only redistributes the UNSENT
queue, never re-binds a replied-to thread). The master inbox then shows
"Email account '<addr>' critical for lead communication has been removed" and
there is no sender to reply through.

This sends the reply directly over SMTP from a live inbox, with In-Reply-To /
References set to the real RFC Message-IDs pulled from SmartLead's
message-history API, so it lands inside the existing conversation on the
lead's side despite the changed From address.

Usage
-----
  # 1. inspect the thread, see the Message-IDs and who to reply to
  python reply_in_thread.py --campaign 3792117 --lead 4332945104 --show

  # 2. dry run: prints the exact MIME that would go out
  python reply_in_thread.py --campaign 3792117 --lead 4332945104 \
      --from sean.r@propertylandscapeservices.info --body reply.txt

  # 3. send it, then verify it landed in Sent with the header intact
  python reply_in_thread.py ... --send --verify

Credentials
-----------
SMARTLEAD_API_KEY from email-infra/.env (CRLF-tolerant).
The sending inbox needs a Google **app password** (16 letters), passed as
--pass-file or $REPLY_SMTP_PASS. See SKILL.md for how to mint one on a
Zapmail-provisioned mailbox.
"""
import argparse, datetime, email.utils, imaplib, os, re, smtplib, sys, time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

SL_BASE = "https://server.smartlead.ai/api/v1"
ENV_PATH = os.environ.get(
    "EMAIL_INFRA_ENV", "/Users/timdwivedi/Desktop/THT/email-infra/.env")
PACIFIC = datetime.timezone(datetime.timedelta(hours=-7))  # PDT; -8 in winter


def load_env(path=ENV_PATH):
    """email-infra/.env is CRLF — strip it or every value keeps a trailing \\r."""
    out = {}
    if not os.path.exists(path):
        return out
    raw = open(path, "rb").read().decode("utf-8", "ignore").replace("\r\n", "\n")
    for line in raw.split("\n"):
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def sl_get(path, key, **params):
    """SmartLead 429s hard; back off rather than hammering."""
    params["api_key"] = key
    for _ in range(5):
        r = requests.get(SL_BASE + path, params=params, timeout=60)
        if r.status_code == 200:
            return r.json()
        if r.status_code != 429:
            raise SystemExit(f"SmartLead {r.status_code}: {r.text[:300]}")
        time.sleep(15)
    raise SystemExit("SmartLead: rate-limited out")


def strip_html(html):
    t = re.sub(r"<br\s*/?>", "\n", str(html or ""))
    t = re.sub(r"</div>|</p>", "\n", t)
    return re.sub(r"<[^>]+>", "", t).replace("&nbsp;", " ").strip()


def fetch_thread(campaign, lead, key):
    d = sl_get(f"/campaigns/{campaign}/leads/{lead}/message-history", key)
    msgs = d.get("history", d) if isinstance(d, dict) else d
    if not isinstance(msgs, list) or not msgs:
        raise SystemExit("no message history for that campaign/lead pair")
    return msgs


def thread_facts(msgs):
    """Subject, who to reply to, and the References chain, straight from the thread.

    Reply to the address that last REPLIED, not the address on the lead record —
    prospects routinely reply from a personal mailbox (and that is the one whose
    Message-ID we thread onto).
    """
    replies = [m for m in msgs if (m.get("type") or "").upper() == "REPLY"]
    if not replies:
        raise SystemExit("no REPLY in this thread — nothing to reply to")
    last = replies[-1]
    subject = next((m.get("subject") for m in msgs if m.get("subject")), "")
    if subject and not subject.lower().startswith("re:"):
        subject = "Re: " + subject.strip()
    return {
        "subject": subject or "Re:",
        "to": last.get("from"),
        "in_reply_to": last.get("message_id"),
        "references": [m.get("message_id") for m in msgs if m.get("message_id")],
        "last_reply": last,
    }


def sender_display_name(campaign, from_email, key):
    """from_name lives on the campaign's email-accounts, never in message-history.

    It is the persona the lead recognises ("Sean Reynolds"); sending bare would
    make an already-changed From address look like a different person entirely.
    """
    for a in sl_get(f"/campaigns/{campaign}/email-accounts", key) or []:
        if (a.get("from_email") or "").lower() == from_email.lower():
            return a.get("from_name") or ""
    return ""


def build(facts, from_email, from_name, body, quote=True):
    msg = MIMEMultipart("alternative")
    msg["From"] = email.utils.formataddr((from_name, from_email))
    msg["To"] = facts["to"]
    msg["Subject"] = facts["subject"]
    # Stamp the sender's business timezone, not this laptop's (+0530 is a tell).
    msg["Date"] = email.utils.format_datetime(datetime.datetime.now(PACIFIC))
    msg["Message-ID"] = email.utils.make_msgid(domain=from_email.split("@")[1])
    msg["In-Reply-To"] = facts["in_reply_to"]
    msg["References"] = " ".join(facts["references"])

    qt = qh = ""
    if quote:
        lr = facts["last_reply"]
        when = str(lr.get("time", ""))[:16].replace("T", " ")
        prev = strip_html(lr.get("email_body"))[:1200]
        qt = "\n\nOn %s, %s wrote:\n%s" % (
            when, lr.get("from"), "\n".join("> " + l for l in prev.split("\n")))
        qh = ('<br><div class="gmail_quote"><div dir="ltr" class="gmail_attr">'
              'On %s, %s wrote:<br></div><blockquote class="gmail_quote" '
              'style="margin:0 0 0 .8ex;border-left:1px #ccc solid;padding-left:1ex">'
              '<div dir="ltr">%s</div></blockquote></div>'
              % (when, lr.get("from"), prev.replace("\n", "<br>")))

    html = ('<div dir="ltr">'
            + body.replace("\n\n", "</div><div><br></div><div>").replace("\n", "<br>")
            + "</div>" + qh)
    msg.attach(MIMEText(body + qt, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    return msg


def get_password(args):
    if args.pass_file:
        return open(args.pass_file).read().strip().replace(" ", "")
    pw = os.environ.get("REPLY_SMTP_PASS", "")
    if not pw:
        raise SystemExit("no app password: pass --pass-file or set $REPLY_SMTP_PASS")
    return pw.strip().replace(" ", "")


def verify(from_email, pw, to_email):
    """Confirm it is really in Sent with In-Reply-To intact, and look for bounces."""
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(from_email, pw)
    M.select('"[Gmail]/Sent Mail"')
    _, d = M.search(None, 'TO "%s"' % to_email)
    ids = d[0].split()
    print(f"\n  Sent Mail: {len(ids)} message(s) to {to_email}")
    if ids:
        _, md = M.fetch(ids[-1], "(BODY.PEEK[HEADER.FIELDS (SUBJECT MESSAGE-ID IN-REPLY-TO)])")
        print("   " + md[0][1].decode("utf-8", "ignore").strip().replace("\r\n", "\n   "))
    M.select("INBOX")
    since = datetime.datetime.now(PACIFIC).strftime("%d-%b-%Y")
    _, d = M.search(None, '(SINCE %s FROM "mailer-daemon")' % since)
    print(f"  bounces today: {len(d[0].split())} "
          "(check they are not for this lead — warmup traffic bounces too)")
    M.logout()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--campaign", required=True)
    p.add_argument("--lead", required=True)
    p.add_argument("--show", action="store_true", help="print the thread and exit")
    p.add_argument("--from", dest="from_email")
    p.add_argument("--from-name", default="")
    p.add_argument("--body", help="file holding the reply copy, verbatim")
    p.add_argument("--to", help="override recipient (default: last replier)")
    p.add_argument("--no-quote", action="store_true")
    p.add_argument("--pass-file")
    p.add_argument("--send", action="store_true")
    p.add_argument("--verify", action="store_true")
    a = p.parse_args()

    key = load_env().get("SMARTLEAD_API_KEY")
    if not key:
        raise SystemExit(f"SMARTLEAD_API_KEY not found in {ENV_PATH}")

    msgs = fetch_thread(a.campaign, a.lead, key)
    if a.show:
        for m in msgs:
            print("=" * 70)
            print(m.get("type"), m.get("time"))
            print("  from:", m.get("from"), "| to:", m.get("to"))
            print("  subject:", m.get("subject"))
            print("  message_id:", m.get("message_id"))
            print("  " + strip_html(m.get("email_body"))[:400].replace("\n", "\n  "))
        return

    if not (a.from_email and a.body):
        raise SystemExit("--from and --body are required unless --show")

    facts = thread_facts(msgs)
    if a.to:
        facts["to"] = a.to
    body = open(a.body).read().rstrip()
    name = a.from_name or sender_display_name(a.campaign, a.from_email, key)
    if not name:
        print("WARNING: no from_name resolved — the reply would send with a bare\n"
              "         address. Pass --from-name explicitly.", file=sys.stderr)
    msg = build(facts, a.from_email, name, body, quote=not a.no_quote)

    print("=" * 72)
    print(msg.as_string()[:3000])
    print("=" * 72)
    if not a.send:
        print("\nDRY RUN — nothing sent. Re-run with --send to deliver.")
        return

    pw = get_password(a)
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=60) as s:
        s.starttls()
        s.login(a.from_email, pw)
        s.sendmail(a.from_email, [facts["to"]], msg.as_string())
    print(f"\nSENT to {facts['to']} from {a.from_email}")
    print("Message-ID:", msg["Message-ID"])
    if a.verify:
        verify(a.from_email, pw, facts["to"])


if __name__ == "__main__":
    main()
