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

import random
import re
from datetime import datetime, timezone

import db as store
import setup as S

# Brandable generic-service roots + suffixes for the in-dashboard suggester.
# Roots deliberately AVOID ones already heavy in our infra (booking/callout/callback/
# contractor/crew/repair/maintain/dispatch/turf/lawn/yard/grounds/landscape) so fresh
# domains don't cluster into a detectable network.
_GEN_ROOTS = [
    "service", "jobsite", "onsite", "fieldwork", "quote", "sameday", "trusted", "local",
    "worksite", "proteam", "project", "install", "upkeep", "schedule", "estimate",
    "frontline", "rapid", "prime", "direct", "ready", "nextday", "core", "apex", "metro",
    "summit", "handy", "taskforce", "fieldpro", "quicktask", "trade", "sitework", "callup",
]
_GEN_SUFFIX = ["pros", "hq", "crew", "team", "hub", "desk", "central", "group", "works",
               "ops", "base", "squad", "point", "co"]
_THEME_ROOTS = {
    "hvac": ["hvac", "heating", "cooling", "climate", "airflow", "comfort", "furnace"],
    "plumbing": ["plumb", "pipe", "drain", "waterworks", "leakfix", "flow"],
    "landscaping": ["lawn", "yard", "turf", "grounds", "greenscape", "outdoor"],
}

ORDERS_KEY = "buy_orders"          # {orders: [ {id, owner, provider, domains, status, ...} ]}

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


def suggest_generic(count=12, tld="info", theme=None, max_checks=None):
    """Generate brandable generic-service domain candidates and return `count`
    that are actually available on Spaceship (with price). This is the reliable
    in-dashboard replacement for the async Zapmail AI finder. Optional theme
    ('hvac'/'plumbing'/'landscaping') flavours the names."""
    tld = (tld or "info").lstrip(".").lower()
    roots = list(_GEN_ROOTS)
    if theme and theme.lower() in _THEME_ROOTS:
        roots = _THEME_ROOTS[theme.lower()] + roots        # bias toward the niche
    combos = []
    for r in roots:
        for s in _GEN_SUFFIX:
            nm = f"{r}{s}"
            if r != s and 6 <= len(nm) <= 24:
                combos.append(nm)
    random.shuffle(combos)
    max_checks = max_checks or max(count * 4, 45)
    out, checked, seen = [], 0, set()
    for nm in combos:
        if len(out) >= count or checked >= max_checks:
            break
        if nm in seen:
            continue
        seen.add(nm)
        d = f"{nm}.{tld}"
        checked += 1
        try:
            if bool(S.Spaceship.check_domain(d).get("available")):
                out.append({"domain": d, "price": _domain_price(d)})
        except Exception:
            pass
    return {"suggestions": out, "checked": checked, "found": len(out)}


# Brand-derived domain suggester (for a specific client's custom infrastructure).
# Cold-email domains never use the client's real domain; they're recognisable
# variants on an alt TLD (getquantumheating.info, quantumheatingpros.info, ...).
_SVC_WORDS = {"hvac", "heating", "cooling", "air", "airconditioning", "plumbing",
              "plumbers", "plumber", "landscaping", "landscape", "lawn", "lawncare",
              "roofing", "electric", "electrical", "services", "service", "solutions",
              "inc", "llc", "co", "company", "group", "the", "and"}
_CLIENT_PREFIX = ["get", "try", "go", "my", "join", "team", "use", "with", "meet",
                  "hello", "the"]
_CLIENT_SUFFIX = ["pros", "hq", "group", "team", "mail", "hub", "co", "care", "works",
                  "direct", "online", "now", "today", "service", "services", "email",
                  "reach", "connect"]


def _brand_roots(brand):
    """Reduce a client brand or real domain to 1-2 root tokens to build variants from.
    'quantumheating.com' / 'Quantum Heating HVAC' -> ['quantumheating', 'quantum']."""
    b = (brand or "").strip().lower()
    b = re.sub(r"^https?://", "", b)
    b = re.sub(r"^www\.", "", b)
    if "." in b and " " not in b:                 # looks like a domain -> take the SLD
        b = b.split("/")[0]
        parts = b.split(".")
        b = parts[-2] if len(parts) >= 2 else parts[0]
    full = re.sub(r"[^a-z0-9]+", "", b)
    if not full:
        return []
    core = full                                    # brand minus a trailing service word
    for w in sorted(_SVC_WORDS, key=len, reverse=True):
        if core.endswith(w) and len(core) - len(w) >= 4:
            core = core[:-len(w)]
            break
    roots = [full]
    if core != full and len(core) >= 4:
        roots.append(core)
    return roots


def suggest_client_domains(brand, count=12, tld="info", max_checks=None):
    """Brand-derived custom domains for one client, seeded from their brand/real
    domain. Returns `count` that are actually available on Spaceship (with price).
    Read-only — no spend. Mirrors suggest_generic but keeps the brand recognisable."""
    roots = _brand_roots(brand)
    if not roots:
        return {"error": "client brand or domain required", "suggestions": []}
    tld = (tld or "info").lstrip(".").lower()
    combos = []
    for r in roots:
        combos.append(r)                           # bare brand on an alt TLD
        combos += [f"{r}{s}" for s in _CLIENT_SUFFIX]
        combos += [f"{p}{r}" for p in _CLIENT_PREFIX]
    seen, cand = set(), []
    for nm in combos:
        if 6 <= len(nm) <= 30 and nm not in seen:
            seen.add(nm)
            cand.append(nm)
    random.shuffle(cand)
    max_checks = max_checks or max(count * 4, 45)
    out, checked, chk_seen = [], 0, set()
    for nm in cand:
        if len(out) >= count or checked >= max_checks:
            break
        d = f"{nm}.{tld}"
        if d in chk_seen:
            continue
        chk_seen.add(d)
        checked += 1
        try:
            if bool(S.Spaceship.check_domain(d).get("available")):
                out.append({"domain": d, "price": _domain_price(d)})
        except Exception:
            pass
    return {"suggestions": out, "checked": checked, "found": len(out), "roots": roots}


def check_domains(domains):
    """Per-domain Spaceship availability + est price. Read-only."""
    out, err = [], None
    configured = True
    try:
        configured = S.Spaceship.is_configured()
    except Exception as e:
        configured, err = False, f"is_configured: {e}"[:160]
    for d in _norm(domains):
        avail = None
        if configured:
            try:
                avail = bool(S.Spaceship.check_domain(d).get("available"))
            except Exception as e:
                err = err or f"{type(e).__name__}: {e}"[:160]
        out.append({"domain": d, "tld": _tld(d), "available": avail, "price": _domain_price(d)})
    res = {"domains": out}
    if not configured:
        res["error"] = "Spaceship not configured — set SPACESHIP_API_KEY / SPACESHIP_SECRET_KEY in Vercel env"
    elif err:
        res["error"] = err
    return res


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


# ─────────────────────────── orders + execution ───────────────────────────

def _now():
    return datetime.now(timezone.utc).isoformat()


def _orders():
    return (store.get_state(ORDERS_KEY) or {}).get("orders", [])


def _save_orders(orders):
    store.set_state(ORDERS_KEY, {"orders": orders})


def _get_order(order_id):
    return next((o for o in _orders() if o.get("id") == order_id), None)


def buy_domains(spec, confirm=False):
    """PHASE 1 — register the available domains on Spaceship + connect them to
    Zapmail, and open an order. Dry-run unless confirm=True. SPENDS on domains
    when confirm=True (the UI's Confirm click is the approval)."""
    p = plan(spec)
    available = [c["domain"] for c in p["domains_checked"] if c["available"]]
    if not available:
        return {"error": "no available domains to register", "plan": p}
    if not confirm:
        return {"dry_run": True, "would_register": available,
                "cost": p["cost"], "plan": p}

    registered, failed = [], []
    for d in available:
        try:
            res = S.Spaceship.purchase_domain(d)
            if res.get("success"):
                registered.append(d)
                try:
                    S.Spaceship.set_nameservers(d)
                except Exception:
                    pass
            else:
                failed.append({"domain": d, "error": str(res.get("error"))[:140]})
        except Exception as e:
            failed.append({"domain": d, "error": str(e)[:140]})

    # connect each registered domain to Zapmail (DNS still needs to propagate
    # before mailboxes can be created — that's what "Provision inboxes" waits on)
    connected = []
    for d in registered:
        try:
            S.zm_connect_domain_single(d)
            connected.append(d)
        except Exception:
            pass

    orders = _orders()
    oid = max([o.get("id", 0) for o in orders], default=0) + 1
    order = {
        "id": oid, "created": _now(),
        "owner": spec.get("owner", "acquisition"),
        "client_name": spec.get("client_name"),
        "provider": p["provider"],
        "inboxes_per_domain": p["inboxes_per_domain"],
        "domains": registered, "connected": connected, "failed": failed,
        "status": "domains_registered",       # -> provisioning -> live
        "batch_config": p["batch_config"],
        "mailboxes_created": [], "exported": [], "tagged": [], "warmed": [],
    }
    orders.append(order)
    _save_orders(orders)
    return {"ok": True, "order_id": oid, "registered": registered,
            "connected": connected, "failed": failed, "status": "domains_registered",
            "note": "Domains registered + connected to Zapmail. Wait ~15-60 min for "
                    "DNS to propagate, then click 'Provision inboxes now' on the order."}


def _zm_domain_state(domains):
    """Look up each domain in Zapmail: id + DNS-ready flag (mailboxes can only be
    created once NS resolve). Returns {domain: {id, dns_ready, mailboxes}}."""
    want = {d.lower() for d in domains}
    out = {}
    try:
        for zd in S.zm_list_domains():
            name = (zd.get("domain") or "").lower()
            if name in want:
                dns_ready = (not zd.get("dnsAuthenticationInProgress")
                             and not zd.get("dnsBoxInvalidNameServers")
                             and (zd.get("status") == "ACTIVE"))
                out[name] = {"id": zd.get("id"), "dns_ready": bool(dns_ready),
                             "mailboxes": len(zd.get("mailboxes") or [])}
    except Exception:
        pass
    return out


def order_readiness(order_id):
    """Per-domain DNS readiness for an order's 'Provision inboxes now' button."""
    o = _get_order(order_id)
    if not o:
        return {"error": "order not found"}
    st = _zm_domain_state(o.get("domains") or [])
    rows = [{"domain": d, "dns_ready": st.get(d.lower(), {}).get("dns_ready", False),
             "in_zapmail": d.lower() in st} for d in o.get("domains") or []]
    return {"order_id": order_id, "domains": rows,
            "all_ready": bool(rows) and all(r["dns_ready"] for r in rows),
            "ready_count": sum(1 for r in rows if r["dns_ready"])}


def list_orders():
    """All buy-orders (most recent first), each with live DNS readiness."""
    orders = sorted(_orders(), key=lambda o: o.get("id", 0), reverse=True)
    for o in orders:
        if o.get("status") == "domains_registered":
            o["readiness"] = order_readiness(o["id"])
    return {"orders": orders}
