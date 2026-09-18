"""Retainer renewal tracking for the infrastructure dashboard.

Retainer clients live in the **CRM** Supabase project (vjwkafnlgqidftxbeqjp,
reached at api.theheadlinetheory.com), not in this project's database. They are
the rows with `clients.billing_model = 'retainer'`.

The renewal date is NOT derivable from the launch date. `clients.renewal_day` is
a stored fact: Denair launched on the 23rd and renews on the 29th, Hammer
launched on the 14th and renews on the 2nd. The date maths here mirrors the
fulfillment repo's `_shared/monthly-period.ts` (which drives the 7/3/1 renewal
notices) so this table can never disagree with the notices Aidan actually
receives — same clamping, same "a renewal closing less than one full month is
not a renewal" guard that bit McFarlane Douglass on 2026-08-18.

Deliberately NOT used as an anchor: `retainer_last_billed`. That column records
when payment LANDED, not when we billed (Denair: invoiced the 22nd, paid the
29th), so anchoring on it walks the schedule later every month. It is shown as
"last paid" and nothing more.

Prepaid terms are handled separately: a client who paid N months up front has no
money decision due until that block runs out, so `launch_date + prepaid_months`
wins over the monthly anniversary while it is still in the future.
"""

from __future__ import annotations

import calendar
import os
from datetime import date, timedelta

# CRM Supabase. The anon key is PUBLIC — the CRM web app ships it in
# js/config.js and `client onboarding/fetch_crm_client.py` hardcodes the same
# one. Env wins so Vercel's configured values are used in production.
CRM_URL_DEFAULT = "https://api.theheadlinetheory.com"
CRM_KEY_DEFAULT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InZqd2thZm5sZ3FpZGZ0eGJlcWpwIiwicm9sZSI6"
    "ImFub24iLCJpYXQiOjE3NzU4NTM3MzcsImV4cCI6MjA5MTQyOTczN30."
    "27x_IdhtcJaAr0wdx6RhoWr1d6_o3zfzEPk9uneq1h8"
)

FIELDS = (
    "id,name,status,client_standing,billing_model,agreement_type,"
    "monthly_retainer,retainer_currency,launch_date,activated_date,"
    "renewal_day,prepaid_months,retainer_last_billed,payment_terms,"
    "stripe_customer_id,monthly_update_enabled,has_inbox_mgmt"
)

# The 7/3/1 pre-renewal notices the fulfillment cron sends to Aidan.
NOTICE_DAYS = (7, 3, 1)

CURRENCY_SYMBOLS = {"usd": "$", "cad": "CA$", "aud": "A$", "gbp": "£", "eur": "€"}


# ── date maths (mirrors _shared/monthly-period.ts) ────────────────────────────

def _days_in_month(y: int, m: int) -> int:
    return calendar.monthrange(y, m)[1]


def add_months_clamped(d: date, n: int) -> date:
    """Add n months, clamping the day into the target month.

    Jan 31 + 1mo is Feb 28, not Mar 3 — a naive rollover would bill a
    month-end client twice in March and never in February.
    """
    total = d.year * 12 + (d.month - 1) + n
    y, m = divmod(total, 12)
    m += 1
    return date(y, m, min(d.day, _days_in_month(y, m)))


def months_elapsed(launch: date, on: date) -> int:
    """Whole months from launch to `on` (0 before the first anniversary)."""
    n = (on.year - launch.year) * 12 + (on.month - launch.month)
    if on.day < min(launch.day, _days_in_month(on.year, on.month)):
        n -= 1
    return n


def next_renewal_on_or_after(renewal_day: int, frm: date) -> date:
    """First renewal falling on or after `frm`, clamped into short months."""
    this_month = date(frm.year, frm.month,
                      min(renewal_day, _days_in_month(frm.year, frm.month)))
    if this_month >= frm:
        return this_month
    y = frm.year + 1 if frm.month == 12 else frm.year
    m = 1 if frm.month == 12 else frm.month + 1
    return date(y, m, min(renewal_day, _days_in_month(y, m)))


def _parse(s) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


# ── row building ──────────────────────────────────────────────────────────────

def _anniversary(renewal_day, launch: date | None, today: date):
    """Next monthly anniversary, and which anchor produced it.

    `renewal_day` is authoritative when set. Otherwise the launch day-of-month
    is a reasonable stand-in, but it is labelled `launch_date` so the dashboard
    can show it as a guess rather than a fact.

    A renewal that closes less than one completed month is skipped: McFarlane
    Douglass launched 2026-08-25 with renewal_day 25, so their 25th-of-month
    "renewal" on the launch day itself is not one.
    """
    day = renewal_day if renewal_day else (launch.day if launch else None)
    if not day:
        return None, None
    basis = "renewal_day" if renewal_day else "launch_date"
    r = next_renewal_on_or_after(int(day), today)
    if launch:
        for _ in range(24):
            if months_elapsed(launch, r) >= 1:
                break
            r = next_renewal_on_or_after(int(day), r + timedelta(days=1))
        else:
            return None, None
    return r, basis


def build_row(c: dict, today: date) -> dict:
    launch = _parse(c.get("launch_date"))
    prepaid_months = c.get("prepaid_months")
    agreement = c.get("agreement_type") or "prepaid"
    status = c.get("status") or ""
    renewal_day = c.get("renewal_day")

    anniversary, basis = _anniversary(renewal_day, launch, today)

    # A prepaid block still running defers the next money decision to its end.
    prepaid_through = None
    if launch and prepaid_months:
        prepaid_through = add_months_clamped(launch, int(prepaid_months))
    prepaid_active = bool(prepaid_through and prepaid_through >= today)

    if prepaid_active and (anniversary is None or prepaid_through >= anniversary):
        renewal, basis = prepaid_through, "prepaid_term"
    else:
        renewal = anniversary

    days_until = (renewal - today).days if renewal else None
    month_number = months_elapsed(launch, renewal) if (launch and renewal) else None

    flags = []
    if status != "active":
        flags.append(status or "no status")
    if not launch:
        flags.append("no launch date")
    elif launch > today:
        flags.append(f"launches {launch.isoformat()}")
    if agreement == "month_to_month" and not renewal_day:
        flags.append("no renewal day")
    if c.get("monthly_retainer") in (None, ""):
        flags.append("no retainer amount")
    if c.get("monthly_update_enabled") is False:
        flags.append("notices off")
    if prepaid_through and prepaid_through < today:
        flags.append(f"prepaid term ended {prepaid_through.isoformat()}")
    # The notice cron keys on renewal_day alone and knows nothing about prepaid
    # months, so it will warn about a renewal the client has already paid for.
    if prepaid_active and anniversary and prepaid_through > anniversary and \
            agreement == "month_to_month" and renewal_day:
        flags.append(f"prepaid through {prepaid_through.isoformat()} but notices still fire")
    if not c.get("stripe_customer_id"):
        flags.append("no stripe customer")

    # Does the 7/3/1 pre-renewal notice actually fire for this client?
    notices = (status == "active" and agreement == "month_to_month"
               and bool(renewal_day) and c.get("monthly_update_enabled") is not False)

    return {
        "name": c.get("name") or "",
        "status": status,
        "agreement_type": agreement,
        "monthly_retainer": c.get("monthly_retainer"),
        "currency": (c.get("retainer_currency") or "usd").lower(),
        "payment_terms": c.get("payment_terms") or None,
        "launch_date": launch.isoformat() if launch else None,
        "renewal_day": renewal_day,
        "prepaid_months": prepaid_months,
        "prepaid_through": prepaid_through.isoformat() if prepaid_through else None,
        "prepaid_active": prepaid_active,
        "last_paid": c.get("retainer_last_billed"),
        "renewal_date": renewal.isoformat() if renewal else None,
        "anniversary": anniversary.isoformat() if anniversary else None,
        "days_until": days_until,
        "month_number": month_number,
        "basis": basis,
        "notices": notices,
        "next_notice": _next_notice(anniversary, today) if notices else None,
        "has_inbox_mgmt": bool(c.get("has_inbox_mgmt")),
        "flags": flags,
        "urgency": _urgency(days_until),
    }


def _next_notice(renewal: date | None, today: date):
    """The next 7/3/1 touch still ahead of us.

    Keyed on the monthly ANNIVERSARY, not on a prepaid-adjusted renewal date:
    the fulfillment cron calls `noticeDueOn(renewal_day, launch, today)` and has
    no idea a client prepaid, so this has to report what will really be sent.
    """
    if not renewal:
        return None
    for d in NOTICE_DAYS:
        touch = renewal - timedelta(days=d)
        if touch >= today:
            return {"date": touch.isoformat(), "touch": d}
    return None


def _urgency(days_until):
    if days_until is None:
        return "unknown"
    if days_until <= 3:
        return "crit"
    if days_until <= 7:
        return "warn"
    if days_until <= 14:
        return "soon"
    return ""


# ── CRM fetch ─────────────────────────────────────────────────────────────────

def fetch_retainer_clients() -> list[dict]:
    import requests
    url = (os.environ.get("CRM_SUPABASE_URL", "").strip() or CRM_URL_DEFAULT).rstrip("/")
    key = os.environ.get("CRM_SUPABASE_KEY", "").strip() or CRM_KEY_DEFAULT
    r = requests.get(f"{url}/rest/v1/clients?select={FIELDS}&billing_model=eq.retainer",
                     headers={"apikey": key, "Authorization": f"Bearer {key}"}, timeout=15)
    r.raise_for_status()
    rows = r.json()
    if not rows:
        # A 200 carrying zero rows is what RLS denial looks like — the CRM
        # returns `content-range: */0` rather than an error. Rendering that as
        # "no retainers" is indistinguishable from "no renewals are due", so the
        # 7/3/1 notices would simply stop with nobody told. Fail loudly instead.
        raise RuntimeError(
            f"CRM returned 0 retainer clients from {url}. The key is almost "
            "certainly being denied by RLS (the anon key cannot read `clients` "
            "since the auth hardening). Set CRM_SUPABASE_KEY to a service-role "
            "key. Refusing to report an empty renewal schedule.")
    return rows


def build_renewals(as_of: str | None = None) -> dict:
    """Rows sorted soonest-renewal-first, plus the headline numbers."""
    today = _parse(as_of) or date.today()
    rows = [build_row(c, today) for c in fetch_retainer_clients()]
    rows.sort(key=lambda r: (r["days_until"] if r["days_until"] is not None else 10**6,
                             r["name"].lower()))

    active = [r for r in rows if r["status"] == "active"]
    mrr = {}
    for r in active:
        if r["monthly_retainer"]:
            mrr[r["currency"]] = mrr.get(r["currency"], 0) + float(r["monthly_retainer"])
    return {
        "rows": rows,
        "as_of": today.isoformat(),
        "summary": {
            "total": len(rows),
            "active": len(active),
            "due_7d": sum(1 for r in active
                          if r["days_until"] is not None and 0 <= r["days_until"] <= 7),
            "due_30d": sum(1 for r in active
                           if r["days_until"] is not None and 0 <= r["days_until"] <= 30),
            "needs_attention": sum(1 for r in active if r["flags"]),
            "mrr": mrr,
        },
    }


if __name__ == "__main__":
    import sys
    data = build_renewals(sys.argv[1] if len(sys.argv) > 1 else None)
    s = data["summary"]
    print(f"as of {data['as_of']} — {s['active']} active retainers, "
          f"{s['due_7d']} renewing within 7d, {s['due_30d']} within 30d")
    print("MRR: " + ", ".join(f"{CURRENCY_SYMBOLS.get(k, '')}{v:,.0f} {k.upper()}"
                              for k, v in sorted(s["mrr"].items())))
    print()
    for r in data["rows"]:
        amt = (f"{CURRENCY_SYMBOLS.get(r['currency'], '')}{float(r['monthly_retainer']):,.0f}"
               if r["monthly_retainer"] else "—")
        days = f"{r['days_until']:>4}d" if r["days_until"] is not None else "   ?"
        print(f"{r['renewal_date'] or '----------'} {days}  {amt:>8}  "
              f"{r['agreement_type']:<14} m{r['month_number'] or 0:<2} "
              f"{r['name'][:38]:<38} {'; '.join(r['flags'])}")
