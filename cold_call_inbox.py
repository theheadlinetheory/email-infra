"""The cold-calling inbox: one contact@<brand>.info mailbox per client.

When Aidan closes a client with "Include cold calling" ticked, the CRM's Won
modal calls `start()`. It buys one .info domain, connects it to Zapmail and
opens a buy-order for a single `contact@` mailbox. That is the inbox a cold-call
lead's follow-up email goes from (the CRM's `clients.cold_call_email`).

The rest — DNS, the mailbox, forwarding, Zapmail tag, SmartLead export, tags,
warm-up, profile picture — is the ordinary buy_provision step machine. It takes
an hour or more, so nobody waits on it: `advance_all()` is called on a schedule
(.github/workflows/cold-call-inbox.yml) and pushes every unfinished
cold-calling order as far as it will go.

Built exactly like the seven made by hand on 2026-10-27 (contact@woodyslandcare
.info and the rest), which Tim pointed at as the template:
  - domain: the client's own website name on .info
  - mailbox name: the COMPANY name, split brand / trade ("Woody's" / "Land
    Care"), never Sean Reynolds; SmartLead's from_name follows it
  - photo: the client's logo (client_logo.py)
  - Zapmail: forwarded to their website, filed under their Zapmail tag
  - SmartLead: tags [client, "Zapmail", M/D/YY], assigned to their SmartLead
    client, warm-up on — and no Group tag, so it never joins a campaign
"""
from __future__ import annotations

import re
import time

import buy_inboxes as bi
import buy_provision as bp
import setup as S

PURPOSE = "cold_call_contact"
USERNAME = "contact"
TLD = "info"
# The words a company name's trade half starts at: "Mighty Oak | Landscaping",
# "GM | Landscaping & Design", "Clear | Heating & Air".
TRADE = {
    "landscape", "landscapes", "landscaping", "landcare", "land", "lawn", "lawncare",
    "care", "heating", "air", "hvac", "plumbing", "refrigeration", "lighting",
    "lights", "christmas", "holiday", "services", "service", "property", "outdoors",
    "outdoor", "design", "snow", "tree", "trees", "irrigation", "pave", "paving",
    "excavation", "excavations", "construction", "roofing", "electric", "electrical",
    "environmental", "solutions", "gardening", "garden", "maintenance", "pest",
    "pool", "pools", "cleaning", "exterior", "contracting", "group", "companies",
}
_LEGAL_TAIL = re.compile(r"[\s,]+(llc|l\.l\.c\.|inc\.?|ltd\.?|corp\.?|co\.?|limited)\s*$", re.I)

# One scheduled call must answer inside Vercel's 300s.
ADVANCE_BUDGET_SECONDS = 220


def display_name(client_name: str) -> str:
    """"Clear Heating & Air, Inc." -> "Clear Heating & Air"; "Gm Landscaping"
    -> "GM Landscaping" (the CRM title-cases initialisms)."""
    n = (client_name or "").strip()
    while _LEGAL_TAIL.search(n):
        n = _LEGAL_TAIL.sub("", n).strip()
    words = []
    for w in n.split():
        core = re.sub(r"[^A-Za-z]", "", w)
        if core.lower() == "hvac" or (2 <= len(core) <= 3 and not re.search(r"[aeiouy]", core, re.I)):
            w = w.upper()
        words.append(w)
    return " ".join(words)


def sender_name(client_name: str, website: str = "") -> dict:
    """The mailbox's first/last name: the company name, split brand / trade."""
    words = display_name(client_name).split()
    if not words:
        return {}
    cut = next((i for i, w in enumerate(words)
                if i >= 1 and re.sub(r"[^a-z]", "", w.lower()) in TRADE), None)
    if cut is None and len(words) >= 2:
        cut = 1
    if cut is not None:
        return {"firstName": " ".join(words[:cut]), "lastName": " ".join(words[cut:])}
    # One word ("Timesavers"): the trade from their domain if it carries one
    # (timesaverslandscaping), else a neutral "Team".
    one = words[0]
    rest = whole_brand(website)[len(one):] if whole_brand(website).startswith(one.lower()) else ""
    return {"firstName": one, "lastName": rest.title() if len(rest) >= 3 else "Team"}


def date_tag(today=None) -> str:
    """SmartLead's batch tag, M/D/YY like "10/27/26" (setup.py's format)."""
    import datetime as _dt
    d = today or _dt.date.today()
    return f"{d.month}/{d.day}/{d.strftime('%y')}"


def normalise_site(website: str) -> str:
    """Zapmail forwarding wants a full URL; the CRM holds 'lightdmv.com'."""
    w = (website or "").strip().rstrip("/")
    if not w:
        return ""
    return w if re.match(r"^https?://", w, re.I) else f"https://{w}"


def find_order(crm_client_id: str) -> dict | None:
    if not crm_client_id:
        return None
    return next((o for o in bi._orders()
                 if o.get("purpose") == PURPOSE
                 and str(o.get("crm_client_id")) == str(crm_client_id)), None)


def summary(order: dict) -> dict:
    j = order.get("journal") or {}
    domain = (order.get("domains") or [None])[0]
    return {
        "order_id": order.get("id"),
        "domain": domain,
        "email": f"{USERNAME}@{domain}" if domain else None,
        "status": order.get("status"),
        "step": j.get("step", "dns"),
        "blocked_reason": j.get("blocked_reason"),
        "name": " ".join(filter(None, [(order.get("mailbox_specs") or [{}])[0].get("firstName"),
                                         (order.get("mailbox_specs") or [{}])[0].get("lastName")])),
        "tags": order.get("tags"),
    }


def whole_brand(brand: str) -> str:
    """'https://www.lightdmv.com/' -> 'lightdmv'; "Woody's Landcare LLC" -> 'woodyslandcare'."""
    b = re.sub(r"^https?://", "", (brand or "").strip().lower())
    b = re.sub(r"^www\.", "", b).split("/")[0]
    if "." in b and not re.search(r"[\s&]", b):
        b = b.split(".")[-2]
    b = re.sub(r"\b(llc|inc|ltd|corp|co)\b\.?", "", b)
    return re.sub(r"[^a-z0-9]+", "", b)


def candidate_domains(brand: str) -> list[str]:
    """Cleanest first: the whole brand, then its roots. contact@lightdmv.info
    reads as the client; contact@trylightpros.info does not."""
    w = whole_brand(brand)
    variants = [f"{w}hq", f"get{w}", f"{w}team", f"{w}group", f"my{w}"] if len(w) >= 3 else []
    ordered = []
    # Not bi._brand_roots: its single words make "care.info" out of Woody's
    # Landcare, and a contact@ has to read as the client.
    for r in [w, *variants]:
        if r and 3 <= len(r) <= 30 and r not in ordered:
            ordered.append(r)
    return [f"{r}.{TLD}" for r in ordered]


def pick_domain(brand: str, check=None) -> str | None:
    check = check or (lambda d: bool(S.Spaceship.check_domain(d).get("available")))
    for d in candidate_domains(brand):
        try:
            if check(d):
                return d
        except Exception:                            # noqa: BLE001 — try the next one
            continue
    # Every clean name is taken: fall back to the brand variants (getX, Xhq, ...).
    got = bi.suggest_client_domains(brand=brand, count=1, tld=TLD,
                                    whole_brand_only=True).get("suggestions") or []
    return got[0]["domain"] if got else None


def start(body: dict, buy=None, pick=None) -> dict:
    """Buy the domain and open the order. Idempotent per CRM client: a Retry
    returns the order that already exists instead of buying a second domain."""
    client_name = (body.get("client_name") or "").strip()
    crm_client_id = str(body.get("crm_client_id") or "").strip()
    forward_to = normalise_site(body.get("website") or "")
    if not client_name or not crm_client_id:
        return {"error": "client_name and crm_client_id are required"}
    if not forward_to:
        # Forwarding blocks provisioning without a target, so refuse now, while
        # someone is looking, not an hour later in a cron log.
        return {"error": "the client has no website — add it, then retry. The "
                         "new domain forwards there."}

    existing = find_order(crm_client_id)
    if existing:
        return {"ok": True, "existed": True, **summary(existing)}

    name = sender_name(client_name, body.get("website") or "")
    if not name:
        return {"error": f"cannot make a sender name from {client_name!r}"}
    brand = body.get("website") or client_name
    domain = (pick or pick_domain)(brand)
    if not domain:
        return {"error": f"no available .{TLD} domain found for {brand!r}"}
    site_brand = whole_brand(body.get("website") or "")

    spec = {
        "owner": "client",
        "client_name": client_name,
        "provider": "google",
        "inboxes_per_domain": 1,
        "domains": [domain],
        "forward_to": forward_to,
        "mailbox_specs": [{**name, "mailboxUsername": USERNAME}],
        "tags": [client_name, "Zapmail", date_tag()],
        "sl_client_name": client_name,
        "zapmail_tag": display_name(client_name),
        "logo": {"slug": site_brand or whole_brand(client_name),
                 "website": forward_to,
                 "brand": [b for b in {site_brand, whole_brand(name["firstName"])} if b]},
        "purpose": PURPOSE,
        "crm_client_id": crm_client_id,
    }
    res = (buy or bi.buy_domains)(spec, confirm=True)
    if res.get("error"):
        return {"error": res["error"]}
    if not res.get("registered"):
        failed = (res.get("failed") or [{}])[0]
        return {"error": f"could not register {domain}: {failed.get('error') or 'unknown'}"}
    order = bi._get_order(res["order_id"]) or {"id": res["order_id"], "domains": [domain],
                                                "status": res.get("status"), **spec}
    return {"ok": True, "existed": False, **summary(order)}


def _retry_nameservers(order: dict) -> None:
    """Spaceship registration is asynchronous, so the nameserver update made
    straight after purchase usually fails. Nothing in the step machine redoes
    it (the dashboard has a button for that), and an unattended order would
    sit at `dns` forever. Retry it here, and move the domain back to
    connect-pending so the dns step reconnects it."""
    failed = order.get("nameserver_failed") or []
    still = []
    for x in failed:
        d = x.get("domain") if isinstance(x, dict) else x
        try:
            r = S.Spaceship.set_nameservers(d)
            ok = not (isinstance(r, dict) and not r.get("success", True))
        except Exception:                            # noqa: BLE001
            ok = False
        if ok:
            pend = set(order.get("connect_pending") or [])
            pend.add(d)
            order["connect_pending"] = sorted(pend)
        else:
            still.append(x)
    order["nameserver_failed"] = still
    # A connect refused for any other reason gets retried too: one domain,
    # nobody watching, so "needs looking at" would mean never.
    cf = order.get("connect_failed") or []
    if cf:
        pend = set(order.get("connect_pending") or [])
        pend |= {x.get("domain") for x in cf if isinstance(x, dict) and x.get("domain")}
        order["connect_pending"] = sorted(pend)
        order["connect_failed"] = []


def advance_all(io=None, clock=time.monotonic) -> dict:
    """Push every unfinished cold-calling order forward. Called on a schedule."""
    orders = bi._orders()
    todo = [o for o in orders if o.get("purpose") == PURPOSE and o.get("status") != "live"]
    started = clock()
    results, skipped = [], []
    for order in todo:
        left = ADVANCE_BUDGET_SECONDS - (clock() - started)
        if left < 30:
            skipped.append(order.get("id"))
            continue
        lock = bi.provision_lock_acquire(int(order["id"]))
        if not lock.get("ok"):
            skipped.append(order.get("id"))
            continue
        try:
            _retry_nameservers(order)
            res = bp.advance(order, io or bp.LiveIO(), budget_seconds=int(left) - 20)
            results.append({**summary(order), "resume": res.get("resume"),
                            "log": res.get("log", [])[-3:]})
        except Exception as e:                       # noqa: BLE001 — one order must not stop the rest
            results.append({**summary(order), "error": str(e)[:200]})
        finally:
            bi._save_orders(orders)
            bi.provision_lock_release(int(order["id"]))
    return {"ok": True, "pending": len(todo), "advanced": results, "skipped": skipped}
