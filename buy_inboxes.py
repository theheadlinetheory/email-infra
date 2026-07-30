"""Buy inboxes & domains — dashboard-driven purchasing.

The heavy provisioning (register domain -> connect to Zapmail -> wait for DNS ->
create mailboxes -> export to SmartLead -> tag -> start warmup) lives in
`acq_outlook.py` as composable steps and takes 15-60 min per batch (DNS
propagation), with a typed-YES spend gate. That can't run inside one serverless
request, so the dashboard splits it:

  1. PLAN (this module, instant + safe): suggest names (Zapmail AI finder),
     check Spaceship availability, and price it out. No spend, no mutation.
  2. BUY (spend-gated, separate step): register the available domains + connect
     them to Zapmail. Fast enough to run on confirm.
  3. PROVISION (deferred, once DNS resolves): create the mailboxes + warmup.

This module owns step 1 and the order bookkeeping; steps 2-3 wrap acq_outlook.
"""

import setup as S

# --- cost model (previews only; the real charge is confirmed at purchase) ---
MAILBOX_MO = 3                      # Zapmail mailbox slot, $/month
DOMAIN_PRICE = {                    # est. first-year registration by TLD
    "info": 4, "com": 11, "co": 28, "net": 12, "org": 11,
    "xyz": 3, "online": 4, "site": 4, "us": 8,
}
DEFAULT_DOMAIN_PRICE = 15
PROVIDERS = {"google": "GOOGLE", "outlook": "MICROSOFT"}


def _tld(d):
    return d.rsplit(".", 1)[-1].lower() if "." in d else ""


def _domain_price(d):
    return DOMAIN_PRICE.get(_tld(d), DEFAULT_DOMAIN_PRICE)


def _norm(domains):
    seen, out = set(), []
    for d in domains or []:
        d = (d or "").strip().lower()
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def suggest_domains(keywords, count=10):
    """Zapmail AI domain finder -> candidate available domains from keywords.
    Accepts a string ('hvac heating') or a list; the API needs an array."""
    if isinstance(keywords, str):
        kw = [w for w in keywords.replace(",", " ").split() if w]
    else:
        kw = [w for w in (keywords or []) if w]
    if not kw:
        return {"error": "keywords required", "suggestions": []}
    try:
        res = S.zm_ai_domain_finder(kw, count) or {}
    except Exception as e:
        return {"error": str(e)[:200], "suggestions": []}
    if isinstance(res, dict) and res.get("status") in (400, 422, 500):
        return {"error": res.get("message", "ai-finder error"), "suggestions": []}
    data = res.get("data") if isinstance(res, dict) else res
    items = (data.get("domains") if isinstance(data, dict) else data) or []
    out = []
    for x in items:
        nm = x.get("domain") if isinstance(x, dict) else x
        if nm:
            out.append({"domain": nm, "price": _domain_price(nm)})
    # Zapmail's AI finder generates asynchronously — a fresh call returns
    # {status:'generating', domains:[]}. Surface that so the UI can say "type
    # your own or retry" rather than looking broken.
    generating = isinstance(data, dict) and data.get("status") == "generating" and not out
    return {"suggestions": out, "generating": generating}


def check_domains(domains):
    """Per-domain Spaceship availability + est price. Read-only."""
    out = []
    for d in _norm(domains):
        try:
            avail = bool(S.Spaceship.check_domain(d).get("available"))
        except Exception:
            avail = None
        out.append({"domain": d, "tld": _tld(d), "available": avail, "price": _domain_price(d)})
    return {"domains": out}


def _batch_config(owner, client_name, provider, avail_domains, per):
    label = (f"Client: {client_name}" if owner == "client" and client_name
             else "Acquisition (new)")
    return {"batches": [{
        "label": label,
        "provider": PROVIDERS.get(provider, "GOOGLE"),
        "accounts_per_domain": per,
        "owner": owner,
        "client_name": client_name,
        "domains": [c["domain"] for c in avail_domains],
    }]}


def plan(spec):
    """Full cost preview + provisioning batch config for a purchase spec.
    spec = {owner:'acquisition'|'client', client_name?, provider:'google'|'outlook',
            inboxes_per_domain, domains:[...]}.  No spend."""
    owner = spec.get("owner", "acquisition")
    client_name = spec.get("client_name")
    provider = (spec.get("provider") or "google").lower()
    per = max(1, int(spec.get("inboxes_per_domain") or 3))
    checked = check_domains(spec.get("domains") or [])["domains"]
    available = [c for c in checked if c["available"]]
    unavailable = [c for c in checked if c["available"] is False]
    unknown = [c for c in checked if c["available"] is None]

    domain_onetime = sum(c["price"] for c in available)
    mailbox_count = len(available) * per
    mailbox_mo = mailbox_count * MAILBOX_MO
    return {
        "owner": owner, "client_name": client_name,
        "provider": PROVIDERS.get(provider, "GOOGLE"),
        "inboxes_per_domain": per,
        "domains_checked": checked,
        "counts": {
            "available": len(available), "unavailable": len(unavailable),
            "unknown": len(unknown), "mailboxes": mailbox_count,
        },
        "cost": {
            "domains_onetime": domain_onetime,
            "mailboxes_monthly": mailbox_mo,
            "first_month_total": domain_onetime + mailbox_mo,
            "recurring_monthly": mailbox_mo,
        },
        "batch_config": _batch_config(owner, client_name, provider, available, per),
        "ready_to_buy": len(available) > 0,
    }
