"""Domain-expiry alerting — the join nothing else makes.

On 2026-09-13 twenty-three domains carrying **69 live sending inboxes on ACTIVE
campaigns** were set to expire with registrar auto-renew OFF. One of them,
`headlinetheoryinfo.com`, was five days from lapsing with three senders on it.
Nobody decided that. Auto-renew gets switched off during an off-boarding while a
domain looks empty, its inboxes are later recycled and reallocated to a live
client, and nothing ever re-checks the flag. Total cost to keep all 23: $244.

Both halves of the signal were already being pulled daily. Nothing joined them.
That is all this module is:

    registrar says: auto-renew OFF, expires in N days
    SmartLead says: this domain's mailboxes are on an ACTIVE campaign
    -> we are about to lose live senders

It also reports the mirror image — auto-renew ON for a domain with no mailboxes
at all — which is the slower, quieter leak (186 such domains were costing
$3,692/yr when this was written).

Two traps this exists because of
-------------------------------
* **Zapmail's `autoRenew` field is meaningless.** It reads `false` on all 897
  domains regardless of truth. The registrar is the only real switch, so this
  module talks to Spaceship and Porkbun directly and never to Zapmail.
* **`campaign_count` on a SmartLead account is a dead field** (0 fleet-wide).
  Whether an inbox is really sending has to come from a campaign-by-campaign
  scan plus `daily_sent_count`.
"""

from __future__ import annotations

import os
import time
from collections import defaultdict
from datetime import date, datetime

# Windows we care about, longest first — an alert fires at the first one crossed.
HORIZONS = (7, 30, 90)

# Renewal prices, Porkbun's public rate card. `.info` registers at $3.60 and
# renews at $22.14 — a 6x step-up most of this fleet is about to hit.
PRICE = {"com": 11.08, "info": 22.14, "co": 31.20}

SLACK_WEBHOOK_VARS = ("SLACK_INFRA_DECISIONS_WEBHOOK", "SLACK_ZAPMAIL_WEBHOOK",
                      "SLACK_WEBHOOK_URL")
NOTIFY_MEMBER_IDS = ("U09B2673A4A",)   # Aidan Hutchinson


def renewal_price(domain: str) -> float:
    return PRICE.get(domain.rsplit(".", 1)[-1], 22.14)


# ── registrar truth ───────────────────────────────────────────────────────────

def fetch_registrar_domains() -> dict:
    """{domain: {registrar, auto_renew, expires}} from Spaceship and Porkbun.

    These two are the source of truth for whether a domain renews. Anything that
    reads a renewal flag from Zapmail is reading a constant.
    """
    import requests
    out = {}

    ak = (os.environ.get("SPACESHIP_API_KEY") or "").strip()
    sk = (os.environ.get("SPACESHIP_SECRET_KEY") or "").strip()
    if ak and sk:
        take, skip = 100, 0
        while True:
            r = requests.get("https://spaceship.dev/api/v1/domains",
                             headers={"X-API-Key": ak, "X-API-Secret": sk},
                             params={"take": take, "skip": skip}, timeout=30)
            if r.status_code != 200:
                break
            items = (r.json() or {}).get("items") or []
            for d in items:
                nm = (d.get("name") or "").lower()
                if nm:
                    out[nm] = {"registrar": "spaceship",
                               "auto_renew": bool(d.get("autoRenew")),
                               "expires": str(d.get("expirationDate") or "")[:10]}
            if len(items) < take:
                break
            skip += take
            time.sleep(0.2)

    pk = (os.environ.get("PORKBUN_API_KEY") or "").strip()
    psk = (os.environ.get("PORKBUN_SECRET_KEY") or "").strip()
    if pk and psk:
        try:
            r = requests.post("https://api.porkbun.com/api/json/v3/domain/listAll",
                              json={"apikey": pk, "secretapikey": psk}, timeout=30)
            for d in (r.json() or {}).get("domains", []):
                nm = (d.get("domain") or "").lower()
                if nm:
                    out[nm] = {"registrar": "porkbun",
                               "auto_renew": str(d.get("autoRenew")) in ("1", "True", "true"),
                               "expires": str(d.get("expireDate") or "")[:10]}
        except Exception:
            pass
    return out


# ── which domains are actually carrying live senders ──────────────────────────

# The fleet has been 1,700-1,900 accounts all year. Anything under this is a
# truncated walk, not a smaller fleet.
MIN_PLAUSIBLE_ACCOUNTS = 1000


def fetch_live_domains(getter=None) -> dict:
    """{domain: {"inboxes": n, "active": n, "sent": n, "clients": {...}}}.

    `active` counts mailboxes attached to an ACTIVE campaign — the number that
    decides whether letting the domain lapse takes real sending down with it.
    """
    import requests
    key = (os.environ.get("SMARTLEAD_API_KEY") or "").strip()
    if not key and getter is None:
        raise RuntimeError("SMARTLEAD_API_KEY is not set")

    def _get(url, params, tries=5):
        if getter is not None:            # injected for tests; returns None on failure
            return getter(url, params)
        for i in range(tries):
            r = requests.get(url, params=params, timeout=60)
            if r.status_code == 200:
                return r.json()
            time.sleep(3 * (i + 1))
        return None

    # ALL OR NOTHING. `if not batch: break` treated a failed page exactly like
    # the end of the list, and Smartlead's rate limit is account-wide and shared
    # with every other job here, so that happened routinely. The result is not a
    # slightly-short roster — it is a board that reports live domains as EMPTY
    # and recommends letting them lapse. Read on 2026-09-19 it claimed 286 empty
    # auto-renewing domains costing $6,797/yr; a complete walk on the same data
    # says 0. Wrong in the destructive direction, so a partial walk raises.
    accounts, off = {}, 0
    while True:
        batch = None
        # One attempt when a getter is injected: a test's failure is deliberate,
        # and backing off through it only makes the suite slow.
        attempts = 1 if getter is not None else 5
        for attempt in range(attempts):
            batch = _get("https://server.smartlead.ai/api/v1/email-accounts",
                         {"api_key": key, "limit": 100, "offset": off}, tries=1)
            if batch is not None:
                break
            if getter is None:
                time.sleep(5 * (attempt + 1))
        if batch is None:
            raise RuntimeError(
                "Smartlead account walk failed part way through — refusing to "
                "report domains as empty on an incomplete roster")
        if not batch:
            break
        for a in batch:
            accounts[(a.get("from_email") or "").lower()] = a
        off += 100
        if getter is None:
            time.sleep(0.3)
        if len(batch) < 100:
            break

    # A roster this far below the known fleet size is a truncated walk that
    # happened to return 200s. Cheap backstop against the same failure wearing
    # a different hat.
    if len(accounts) < MIN_PLAUSIBLE_ACCOUNTS:
        raise RuntimeError(
            f"Smartlead returned only {len(accounts)} accounts — implausibly few, "
            "refusing to judge which domains are empty")

    camps = _get("https://server.smartlead.ai/api/v1/campaigns", {"api_key": key})
    if camps is None:
        # Without campaigns every domain reads "no active senders", which is the
        # input to "safe to let lapse".
        raise RuntimeError("Smartlead campaign list unavailable — cannot tell a "
                           "domain with live senders from one without")
    active_emails = set()
    for c in camps:
        if (c.get("status") or "").upper() != "ACTIVE":
            continue
        rows = _get(f"https://server.smartlead.ai/api/v1/campaigns/{c['id']}/email-accounts",
                    {"api_key": key})
        for a in (rows or []):
            active_emails.add((a.get("from_email") or "").lower())
        if getter is None:
            time.sleep(0.05)

    out = defaultdict(lambda: {"inboxes": 0, "active": 0, "sent": 0, "clients": set()})
    for email, a in accounts.items():
        if "@" not in email:
            continue
        d = out[email.split("@")[-1]]
        d["inboxes"] += 1
        d["sent"] += a.get("daily_sent_count") or 0
        if email in active_emails:
            d["active"] += 1
        for t in (a.get("tags") or []):
            n = t.get("tag_name") or t.get("name")
            if n and n != "Zapmail":
                d["clients"].add(n)
    return out


# ── the join ──────────────────────────────────────────────────────────────────

def build(as_of: date | None = None) -> dict:
    today = as_of or date.today()
    reg = fetch_registrar_domains()
    live = fetch_live_domains()
    if not reg:
        raise RuntimeError("no registrar data — check SPACESHIP_*/PORKBUN_* keys")

    at_risk, wasting = [], []
    for dm, r in reg.items():
        try:
            exp = date.fromisoformat(r["expires"]) if r["expires"] else None
        except ValueError:
            exp = None
        days = (exp - today).days if exp else None
        info = live.get(dm) or {"inboxes": 0, "active": 0, "sent": 0, "clients": set()}

        # About to lose live senders.
        if (not r["auto_renew"]) and days is not None and days <= HORIZONS[-1] \
                and info["active"] > 0:
            at_risk.append({
                "domain": dm, "registrar": r["registrar"], "expires": r["expires"],
                "days": days, "inboxes": info["inboxes"], "active": info["active"],
                "sent": info["sent"], "clients": sorted(info["clients"]),
                "cost_to_keep": round(renewal_price(dm), 2),
                "horizon": next(h for h in HORIZONS if days <= h),
            })
        # Paying to renew a domain with nothing on it.
        if r["auto_renew"] and info["inboxes"] == 0:
            wasting.append({"domain": dm, "registrar": r["registrar"],
                            "expires": r["expires"], "days": days,
                            "cost": round(renewal_price(dm), 2)})

    at_risk.sort(key=lambda x: (x["days"], -x["active"]))
    wasting.sort(key=lambda x: (x["days"] if x["days"] is not None else 9999))
    return {
        "as_of": today.isoformat(),
        "at_risk": at_risk,
        "wasting": wasting,
        "summary": {
            "domains_checked": len(reg),
            "at_risk": len(at_risk),
            "senders_at_risk": sum(x["active"] for x in at_risk),
            "cost_to_save_them": round(sum(x["cost_to_keep"] for x in at_risk), 2),
            "empty_renewing": len(wasting),
            "empty_renewing_cost": round(sum(x["cost"] for x in wasting), 2),
        },
    }


# ── output ────────────────────────────────────────────────────────────────────

def format_alert(board: dict) -> str | None:
    """Slack text, or None when there is nothing worth saying."""
    ar, s = board["at_risk"], board["summary"]
    if not ar and not board["wasting"]:
        return None
    men = " ".join(f"<@{u}>" for u in NOTIFY_MEMBER_IDS)
    out = []
    if ar:
        out.append(f"{men} 🔴 *{s['senders_at_risk']} live senders on domains about to lapse* "
                   f"— auto-renew is OFF and they carry mailboxes on ACTIVE campaigns.")
        out.append(f"Cost to keep all of them: *${s['cost_to_save_them']:,.2f}*.\n")
        for x in ar[:25]:
            who = ", ".join(x["clients"][:2]) or "untagged"
            out.append(f"• *{x['domain']}* — expires *{x['expires']}* ({x['days']}d), "
                       f"{x['active']} active sender(s), {who} · {x['registrar']} · "
                       f"${x['cost_to_keep']}")
        if len(ar) > 25:
            out.append(f"…and {len(ar)-25} more.")
    if board["wasting"]:
        out.append(f"\n💸 *{s['empty_renewing']} empty domains are set to auto-renew* "
                   f"— no mailboxes at all, *${s['empty_renewing_cost']:,.0f}/yr*.")
    return "\n".join(out)


def post(board: dict, dry_run: bool = True) -> dict:
    text = format_alert(board)
    if not text:
        return {"posted": False, "reason": "nothing at risk"}
    if dry_run:
        return {"dry_run": True, "text": text}
    import requests
    for var in SLACK_WEBHOOK_VARS:
        hook = (os.environ.get(var) or "").strip()
        if not hook:
            continue
        try:
            r = requests.post(hook, json={"text": text}, timeout=10)
            if r.status_code in (200, 201):
                return {"posted": True, "via": var}
        except requests.RequestException:
            pass
    return {"posted": False, "reason": "no working webhook", "text": text}


if __name__ == "__main__":
    import argparse
    import json as _json

    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--json", action="store_true")
    p.add_argument("--send", action="store_true", help="post to Slack")
    args = p.parse_args()
    try:
        import _envload
        _envload.load()
    except Exception:
        pass

    b = build()
    if args.json:
        print(_json.dumps(b, indent=2, default=str))
    else:
        s = b["summary"]
        print(f"checked {s['domains_checked']} registrar domains as of {b['as_of']}")
        print(f"AT RISK: {s['at_risk']} domains carrying {s['senders_at_risk']} active "
              f"senders — ${s['cost_to_save_them']:,.2f} to keep them all")
        for x in b["at_risk"][:30]:
            print(f"   {x['expires']}  {x['days']:>4}d  {x['domain']:<38} "
                  f"{x['active']:>3} active  ${x['cost_to_keep']:>6}  {x['registrar']}")
        print(f"\nEMPTY BUT RENEWING: {s['empty_renewing']} domains, "
              f"${s['empty_renewing_cost']:,.0f}/yr")
    if args.send:
        print(post(b, dry_run=False))
