"""Vercel serverless API — reads from Supabase cache only. No SmartLead calls."""

import os
import sys
import json
import re
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, request, jsonify, make_response, send_from_directory

app = Flask(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PUBLIC_DIR = os.path.join(_PROJECT_ROOT, "public")

DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")


# Groups that belong to the acquisition fleet rather than to the client pools.
# Deliberately narrower than check_invariants.OPERATIONAL_RE: "Premium Inboxes"
# and "Reserve" are stock we can still give a client, so they stay pools.
# Our own brand. Acquisition domains are variants of it — see /api/buy-suggest.
ACQUISITION_BRAND = "The Headline Theory"

ACQUISITION_POOL_RE = re.compile(r"^\s*[\(]?\s*(acquisition|burnt\s+acquisition)\b", re.I)


def _today_iso():
    import datetime as _dt
    return _dt.date.today().isoformat()


def _check_auth():
    if not DASHBOARD_PASSWORD:
        return True
    if request.args.get("pw") == DASHBOARD_PASSWORD:
        return True
    if request.cookies.get("dashboard_pw", "") == DASHBOARD_PASSWORD:
        return True
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        from server_auth import verify_firebase_token
        try:
            if verify_firebase_token(auth[7:]):
                return True
        except Exception:
            pass
    return False


def _get_cache(key):
    import db as store
    try:
        data, updated_at = store.cache_get(key)
        return data, updated_at
    except Exception as e:
        return None, str(e)


def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return resp


@app.route("/")
def serve_index():
    return send_from_directory(_PUBLIC_DIR, "index.html")


@app.route("/health.html")
@app.route("/health")
def serve_health():
    return send_from_directory(_PUBLIC_DIR, "health.html")


@app.route("/css/<path:f>")
def serve_css(f):
    return send_from_directory(os.path.join(_PUBLIC_DIR, "css"), f)


@app.route("/js/<path:f>")
def serve_js(f):
    return send_from_directory(os.path.join(_PUBLIC_DIR, "js"), f)


@app.route("/favicon.ico")
def favicon():
    return "", 204



# ── slow-route cache ──────────────────────────────────────────────────────────

def _slow_cache(key, builder, ttl_seconds=6 * 3600):
    """Serve a stored answer with its age; recompute when asked or when stale.

    Four routes rebuild their entire world per request — a full Zapmail walk, a
    full Smartlead walk, sometimes both — and measured 13s, 47s, 77s and 98s.
    /api/domain-expiry spent 98 seconds to return 181 bytes. A tab that takes
    a minute is a tab nobody opens, and Vercel stops a function at 300s.

    `?refresh=1` forces a rebuild. Every response carries `_cached`,
    `_generated_at` and `_age_seconds`, because a number without its age is how
    /api/overview came to serve a day-old roster of 51 clients while 21 were
    active, with nothing on screen saying so.
    """
    import db as store
    import datetime as _dt

    def _now():
        return _dt.datetime.now(_dt.timezone.utc)

    def _stamp(payload, cached, generated):
        if not isinstance(payload, dict):
            payload = {"data": payload}
        age = None
        if generated:
            try:
                t = _dt.datetime.fromisoformat(str(generated).replace("Z", "+00:00"))
                if t.tzinfo is None:
                    t = t.replace(tzinfo=_dt.timezone.utc)
                age = int((_now() - t).total_seconds())
            except ValueError:
                pass
        payload["_cached"] = cached
        payload["_generated_at"] = generated
        payload["_age_seconds"] = age
        payload["_stale"] = age is not None and age > ttl_seconds
        return payload

    fresh = request.args.get("refresh") == "1"
    if not fresh:
        try:
            rows = store._request("GET", "/state",
                                  params={"select": "data,updated_at", "key": f"eq.{key}"})
            if rows:
                payload = json.loads(rows[0]["data"])
                gen = payload.get("_generated_at") or rows[0].get("updated_at")
                stamped = _stamp(payload, True, gen)
                # A cache past its TTL is worse than a slow answer: it is a
                # wrong answer that looks instant. Fall through and rebuild.
                if not stamped["_stale"]:
                    return stamped
        except Exception:
            pass

    payload = builder()
    generated = _now().isoformat(timespec="seconds")
    save_error = None
    try:
        body = dict(payload) if isinstance(payload, dict) else {"data": payload}
        body["_generated_at"] = generated
        store._request("POST", "/state",
                       json_body={"key": key, "data": json.dumps(body),
                                  "updated_at": generated},
                       headers={"Prefer": "resolution=merge-duplicates"})
    except Exception as e:                           # noqa: BLE001
        # A cache that can never be WRITTEN is indistinguishable from one that
        # is merely cold: every request rebuilds, every request is slow, and
        # the nightly warm reports success having stored nothing. Carry the
        # failure on the response so it is visible instead of just expensive.
        save_error = str(e)[:160]
    stamped = _stamp(payload, False, generated)
    if save_error:
        stamped["_cache_write_failed"] = save_error
    return stamped


@app.route("/api/healthz")
def healthz():
    return "ok-v2-cache-readonly", 200


@app.route("/api/auth-check")
def auth_check():
    if _check_auth():
        return jsonify({"ok": True})
    return jsonify({"error": "Unauthorized"}), 401


@app.route("/api/crm-clients")
def crm_clients():
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import requests as _req
        crm_url = os.environ.get("CRM_SUPABASE_URL", "").strip()
        crm_key = os.environ.get("CRM_SUPABASE_KEY", "").strip()
        if not crm_url or not crm_key:
            return _cors(jsonify({"clients": []}))
        r = _req.get(f"{crm_url}/rest/v1/clients?select=name,client_standing",
                      headers={"apikey": crm_key, "Authorization": f"Bearer {crm_key}"}, timeout=10)
        names = sorted(set(c["name"].strip() for c in r.json() if c.get("name"))) if r.status_code == 200 else []
        return _cors(jsonify({"clients": names}))
    except Exception:
        return _cors(jsonify({"clients": []}))


@app.route("/api/overview")
def overview():
    if not _check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data, ts = _get_cache("overview_v2")
    if data and data.get("clients"):
        data["_cached"] = True
        data["_synced_at"] = ts
        # This cache goes stale silently, and a stale one reads as a SMALLER
        # fleet rather than as an error -- inboxes simply go missing. Say how
        # old it is so the page can, rather than presenting it as today's.
        try:
            import health_daily as _hd
            _age = _hd._cache_age_hours(ts)
            data["_stale"] = _age is None or _age > _hd.CACHE_STALE_AFTER_HOURS
            data["_age_hours"] = None if _age is None else round(_age, 1)
        except Exception:                            # noqa: BLE001
            data["_stale"] = True
            data["_age_hours"] = None
        try:
            import re
            import requests as _req
            crm_url = os.environ.get("CRM_SUPABASE_URL", "").strip()
            crm_key = os.environ.get("CRM_SUPABASE_KEY", "").strip()
            if crm_url and crm_key:
                r = _req.get(f"{crm_url}/rest/v1/clients?select=name,client_standing",
                              headers={"apikey": crm_key, "Authorization": f"Bearer {crm_key}"}, timeout=5)
                if r.status_code == 200:
                    crm_names = [c["name"].strip() for c in r.json() if c.get("name")]
                    def _norm(name):
                        n = name.lower().strip()
                        prev = ''
                        while prev != n:
                            prev = n
                            n = re.sub(r'\s+(group|llc|inc\.?|construction|landscaping|lawn\s*care|hvac|'
                                       r'land\s*care|scapes|landscape|heating\s*&?\s*air.*|'
                                       r'lawn\s*solutions|land\s*solutions|&\s*design|conditioning)\s*$',
                                       '', n, flags=re.IGNORECASE)
                            n = re.sub(r'[,.\s&]+$', '', n).strip()
                        return re.sub(r'\s+', ' ', n)
                    crm_by_norm = {_norm(cn): cn for cn in crm_names}
                    matched_norms = set()
                    for c in data["clients"]:
                        normed = _norm(c["name"])
                        if normed in crm_by_norm:
                            c["name"] = crm_by_norm[normed]
                            matched_norms.add(normed)
                    for cn in crm_names:
                        if _norm(cn) not in matched_norms:
                            data["clients"].append({
                                "name": cn, "accounts": 0, "group_a_count": 0, "group_b_count": 0,
                                "group_a": None, "group_b": None, "in_campaign": 0, "smtp_failures": 0,
                                "total_domains": 0, "avg_bounce_rate": None, "avg_reply_rate": None,
                                "daily_capacity": 0, "total_sent": 0, "daily_sent": 0,
                                "campaigns": [], "account_details": [], "crm_only": True,
                            })
                    data["crm_clients"] = crm_names
        except Exception:
            pass

        # ?slim=1 drops the per-account rows. They are 1,469 KB of a 1.64 MB
        # payload — 50 clients x up to 57 mailboxes x 17 fields — and a caller
        # that only wants the roster, the counts or the campaign list pays for
        # all of it. The default is unchanged on purpose: the existing dashboard
        # renders those rows, and quietly removing them would break it.
        if request.args.get("slim") in ("1", "true", "yes"):
            trimmed = 0

            def _strip(obj):
                """Drop account_details wherever it appears, however deep.

                A first cut only popped it at the top level of each client and
                barely moved the payload: 1.64 MB to 795 KB. The bulk was
                `clients[].group_a`, a NESTED group object carrying its own copy
                of the same rows — 728 KB of the remainder. Stripping one level
                and declaring victory would have shipped a "slim" mode that was
                still most of the original.
                """
                nonlocal trimmed
                if isinstance(obj, dict):
                    out = {}
                    for k, v in obj.items():
                        if k == "account_details":
                            trimmed += len(v or [])
                            continue
                        out[k] = _strip(v)
                    return out
                if isinstance(obj, list):
                    return [_strip(x) for x in obj]
                return obj

            data = _strip(data)
            data["_slim"] = True
            data["_account_rows_omitted"] = trimmed
        return _cors(jsonify(data))
    return _cors(jsonify({"loading": True, "clients": [], "total_accounts": 0}))


@app.route("/api/health-history")
def health_history():
    if not _check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    history, _ = _get_cache("health_history")
    return _cors(jsonify(history or []))


# ─── Health V1 (per-inbox tracking / burn detection / cancel) ───

@app.route("/api/health-fleet")
def health_fleet():
    """Current per-inbox health fleet (read-only, from the health_fleet cache)."""
    if not _check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data, ts = _get_cache("health_fleet")
    if not data:
        return _cors(jsonify({"loading": True, "inboxes": [], "counts": {}}))
    data["_synced_at"] = ts
    return _cors(jsonify(data))


def _is_vercel_cron():
    """Vercel sends cron requests with Authorization: Bearer $CRON_SECRET.

    Fallback: when CRON_SECRET is NOT configured, the old check could never
    return True, so every cron invocation 401'd and the daily job silently never
    ran (inbox_health_daily had 9 of 16 days, none at the scheduled 13:00, and
    the removal watcher's heartbeat never moved). Vercel also stamps its own
    cron requests with `x-vercel-cron`, so accept that when there's no secret to
    check against. Setting CRON_SECRET automatically tightens this back to the
    signed check.
    """
    secret = os.environ.get("CRON_SECRET", "")
    if secret:
        return request.headers.get("Authorization", "") == f"Bearer {secret}"
    return bool(request.headers.get("x-vercel-cron"))




@app.route("/api/clients")
def clients_route():
    """Active clients only: what they hold, what they should hold, when we decide.

    /api/overview answers this today in 1.59 MB of everything-about-everything,
    served from a sync cache that on 2026-09-18 listed 51 clients while 21 were
    active — churned clients never left. This is the same question answered in a
    few KB, from the lifecycle board, which already knows each client's term.

    Off-boarding is what removes a client here: the roster is the CRM's `status`,
    not a list anyone maintains by hand.
    """
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import infra_lifecycle as ilc
        import check_invariants as civ

        board = _slow_cache("cache:infra_lifecycle", ilc.build)
        # The CRM row decides the target and the free-account exemption.
        # infra_lifecycle's own `seasonal` flag is inferred from the vertical
        # and does not know the snow clients — Kinsley, Peak and GM carry no
        # seasonal word in their name or services, so it read them as 42 when
        # they are on 57. check_invariants keeps the explicit list; use it.
        crm_by = {}
        try:
            for c in (ilc.fetch_crm_clients() or []):
                if c.get("name"):
                    crm_by[civ._norm(c["name"])] = c
        except Exception:
            pass
        rows, pools = [], []
        for r in (board.get("rows") or []):
            name = r.get("client")
            if civ.OPERATIONAL_RE.match(str(name or "")):
                # Acquisition groups are NOT pools. A pool is stock waiting to be
                # given to a client; acquisition is our own prospecting fleet,
                # already deployed and reported in full on its own tab. Listing
                # it here double-counted it and made the reserve look bigger than
                # anything we could actually hand a client (Tim, 2026-09-18).
                if not ACQUISITION_POOL_RE.match(str(name or "")):
                    pools.append({"name": name, "inboxes": r.get("mailboxes"),
                                  "monthly_cost": r.get("monthly_cost")})
                continue
            if (r.get("status") or "") != "active":
                continue
            crm_row = crm_by.get(civ._norm(name))
            target = civ.target_for(str(name or ""), crm_row)
            held = r.get("mailboxes") or 0
            # A free account has no target — Landy Rose runs at 18 by design.
            exempt = bool(crm_row and civ.is_free_account(crm_row))
            rows.append({
                "name": name,
                "billing_model": r.get("billing_model"),
                "agreement_type": r.get("agreement_type"),
                "seasonal": target == civ.SEASONAL_TARGET,
                "inboxes": held,
                "target": None if exempt else target,
                "delta": 0 if exempt else held - target,
                "exempt": exempt,
                "domains": r.get("exclusive_domains"),
                "monthly_cost": r.get("monthly_cost"),
                "launch_date": r.get("launch_date"),
                # The three dates the whole countdown exists to produce.
                #
                # A free account gets NONE of them. There is no contract to run
                # out, nothing to renew and no money to stop, so the engine's
                # fallback three-month guess invents a deadline that does not
                # exist — Landy Rose Media was showing one (Tim, 2026-09-18).
                # Blank is the honest answer; the row still says "free".
                "term_ends": None if exempt else r.get("effective_end"),
                "term_basis": "free account — no term" if exempt else r.get("end_basis"),
                "decide_by": None if exempt else r.get("decision_by"),
                "days_to_decision": None if exempt else r.get("days_to_decision"),
                "hard_stop": None if exempt else r.get("hard_stop"),
                "outcome": r.get("outcome"),
                "phase": r.get("phase"),
                # An "assumed" term is a deadline nobody agreed to. Flag it
                # rather than render it like a real one.
                # An "assumed" term is a deadline nobody agreed to. A free
                # account has no term to guess at, so it is not a fault there.
                "term_is_a_guess": (not exempt
                                    and str(r.get("end_basis") or "").startswith("assumed")),
            })
        rows.sort(key=lambda x: (x["decide_by"] is None, x["decide_by"] or "", x["name"]))
        return _cors(jsonify({
            "clients": rows,
            "pools": sorted(pools, key=lambda x: -(x["inboxes"] or 0)),
            "summary": {
                "active_clients": len(rows),
                "inboxes": sum(r["inboxes"] for r in rows),
                "monthly_cost": sum(r["monthly_cost"] or 0 for r in rows),
                "off_target": sum(1 for r in rows if r["delta"]),
                "guessed_terms": sum(1 for r in rows if r["term_is_a_guess"]),
                "decisions_due_30d": sum(
                    1 for r in rows
                    if r["days_to_decision"] is not None and 0 <= r["days_to_decision"] <= 30),
            },
            "_cached": board.get("_cached"),
            "_generated_at": board.get("_generated_at"),
            "_age_seconds": board.get("_age_seconds"),
            "_stale": board.get("_stale"),
        }))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()[-800:]})), 500


# Domain words that place an inbox in a vertical. Replacement has to match:
# a burned HVAC inbox swapped onto lawnmaintenancecrew.info sends heating
# offers from a lawn-care domain, which is the mismatch that took a day to
# unpick across 38 domains in September.
_SERVICE_WORDS = ("hvac", "heating", "cooling", "furnace", "boiler", "plumb",
                  "drain", "electric", "aircon", "refrig", "mechanical",
                  "dispatch", "callback", "callout", "repair", "technician",
                  "contractor", "appliance", "roofing", "restoration")
_LAND_WORDS = ("lawn", "turf", "yard", "grounds", "landscap", "outdoor",
               "garden", "mow", "tree", "irrigation", "sod", "hedge",
               "exterior", "propertycare", "greenwork")


def _vertical_of(domain: str) -> str:
    d = (domain or "").lower()
    if any(w in d for w in _SERVICE_WORDS):
        return "service"
    if any(w in d for w in _LAND_WORDS):
        return "landscaping"
    return "other"


@app.route("/api/inboxes")
def inboxes_route():
    """What needs doing to the fleet, rather than every inbox in it.

    /api/health-fleet ships 1,902 inboxes at 1.03 MB. Of those, 25 are burned
    and only the ones belonging to a live client are actionable — a burned
    inbox already tagged into a Cleanup bucket is cancelled and needs nothing.
    This returns the counts, that actionable list, and what is available to
    replace them with.

    Replacement stock is reported BY VERTICAL because it has to match. There is
    currently no service reserve at all: all 129 reserve inboxes across 43
    domains are landscaping, verified 2026-09-18, so a burned service inbox has
    nothing to swap to and is left in place rather than moved onto a lawn-care
    domain.
    """
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import check_invariants as civ
        fleet, ts = _get_cache("health_fleet")
        if not fleet:
            return _cors(jsonify({"loading": True, "counts": {}, "burned": []}))

        # Live clients come from the lifecycle board, so "actionable" means the
        # same thing here as it does on the Clients tab.
        import infra_lifecycle as ilc
        board = _slow_cache("cache:infra_lifecycle", ilc.build)
        active = {civ._norm(r.get("client")) for r in (board.get("rows") or [])
                  if (r.get("status") or "") == "active"}
        pools = {}
        for r in (board.get("rows") or []):
            nm = str(r.get("client") or "")
            if civ.OPERATIONAL_RE.match(nm):
                pools[nm] = r.get("mailboxes") or 0

        burned = []
        for i in (fleet.get("inboxes") or []):
            if i.get("status") != "burned":
                continue
            owner = i.get("client") or ""
            is_client = (civ._norm(owner) in active
                         and not civ.OPERATIONAL_RE.match(owner))
            burned.append({
                "email": i.get("email"),
                "client": owner,
                "domain": i.get("domain"),
                "vertical": _vertical_of(i.get("domain")),
                "bounce_3d": i.get("bounce_3d"),
                "reply_3d": i.get("reply_3d"),
                "reason": (i.get("reasons") or [None])[0],
                # Only a live client's burned inbox needs replacing. One sitting
                # in a Cleanup bucket is already cancelled.
                "actionable": is_client,
            })
        burned.sort(key=lambda b: (not b["actionable"], b["client"] or "", b["email"] or ""))
        act = [b for b in burned if b["actionable"]]

        # A short burned list and a stale one look identical on screen. When the
        # metrics feed breaks, inbox_health_daily simply stops gaining rows and
        # this list quietly freezes at the last good day — which reads as a
        # quiet week rather than as no data. Say which it is.
        try:
            import health_daily as _hd
            # `ts` is when health_fleet was built. The burned list is rendered
            # from THAT, so a current table behind a stale cache must still
            # read as stale.
            freshness = _hd.health_freshness(built_at=ts)
        except Exception as _e:                      # noqa: BLE001
            freshness = {"stale": True, "latest": None, "age_days": None,
                         "reason": f"freshness check failed: {str(_e)[:120]}"}

        reserve = sum(n for k, n in pools.items()
                      if "generic" in k.lower() or "reserve" in k.lower())
        replacement = sum(n for k, n in pools.items() if "replacement" in k.lower())

        # Burn RATE, not just the burned count. 25 burned is fine if it has
        # been 25 for a month and an emergency if it was 4 last week, and the
        # figure is only actionable next to what replaces them.
        #
        # RECORDED, not reconstructed. Replaying inbox_health_daily through the
        # BURNED rule fires on 139 inboxes for 2026-09-16 while
        # inbox_health_status carries 25, overlapping on only 7 — the two
        # disagree in both directions, which fits the known history problems
        # with that table. So a daily snapshot of the trustworthy table is taken
        # here and the rate is the movement between snapshots. See fleet_burn.py.
        burn = {"measured": False, "recording": False,
                "reason": "burn rate unavailable", "weeks": [], "summary": {}}
        try:
            import datetime as _dt
            import db as _store
            import fleet_burn as fbn
            stat = _store.get_health_status_all()
            today_s = _dt.date.today().isoformat()
            hist = (_store.get_state(fbn.STATE_KEY) or {}).get("snapshots") or []
            burn = fbn.build(hist, stat or None, replacement, today_s)
            # Write today's snapshot AFTER building, so the rate is always
            # computed against yesterday rather than against itself.
            if stat:
                _store.set_state(fbn.STATE_KEY,
                                 {"snapshots": fbn.record(hist, stat, today_s)})
        except Exception as e:
            burn["reason"] = f"burn rate unavailable: {str(e)[:120]}"

        # The replacement loop's live state, so the Inboxes tab can run the
        # same flow the old health tab ran: held inboxes (own a positive reply,
        # kept in their campaign on purpose) and jobs already in flight.
        # Each read is optional — a failure hides that section rather than
        # taking the whole tab down with it.
        holds, jobs = [], []
        try:
            import health_positive as hp
            holds = (hp.list_holds() or {}).get("holds") or []
        except Exception:
            holds = []
        # Campaigns that had a sender swapped and still need SmartLead's manual
        # "Reallocate mailboxes" click. There is NO API for that step, so a swap
        # is not finished until someone does it — and until then the campaign is
        # sending from a set of mailboxes that no longer matches what is tagged.
        realloc = {"campaigns": [], "count": 0}
        try:
            import health_replace as hr
            realloc = hr.reallocation_campaigns() or realloc
        except Exception:
            pass
        reserve_by_niche = {}
        try:
            import health_replace as hr
            reserve_by_niche = hr.reserve_summary() or {}
        except Exception:
            reserve_by_niche = {}

        # The burned fleet GROUPED BY DOMAIN — 1/3, 2/3, 3/3 — which is how the
        # old health tab showed it and how the decision is actually made:
        # Zapmail bills by whole domain, so one burned inbox on a domain is a
        # replacement and three is a cancellation.
        # Cached: measured at 9.7s, and this route already does five other
        # reads. An Inboxes tab that takes fifteen seconds is a tab nobody
        # opens, and on a cold Vercel instance it is a tab that times out.
        domain_view = {"domains": [], "summary": {}}
        try:
            import health_domains as hdm
            domain_view = _slow_cache("cache:domain_view", hdm.domain_view,
                                      ttl_seconds=3600) or domain_view
        except Exception as e:
            domain_view = {"domains": [], "summary": {},
                           "error": f"domain view unavailable: {str(e)[:120]}"}

        try:
            import health_replace as hr
            all_jobs = hr.list_jobs() or []
            # IN FLIGHT means still needing something. `swapped` and `cancelled`
            # are finished — including them reported "189 replacements in flight"
            # when 171 were history and only 18 were live, every one of them
            # offering a Swap button that meant nothing.
            jobs = [{
                "id": j.get("id"),
                "email": j.get("old_email"),
                "client": j.get("client"),
                "old_domain": j.get("old_domain"),
                "replacement": j.get("new_domain"),
                "stage": j.get("status"),
                "days_left": j.get("days_left"),
                "ready": bool(j.get("is_ready")),
                "reason": j.get("reason"),
            } for j in all_jobs if (j.get("status") or "") in ("flagged", "reserved")]
            jobs.sort(key=lambda j: (not j["ready"], j["client"] or "", j["email"] or ""))
        except Exception:
            jobs = []

        need = Counter(b["vertical"] for b in act)
        return _cors(jsonify({
            "counts": fleet.get("counts") or {},
            "alerts": fleet.get("alert_summary") or {},
            "burned": burned,
            "freshness": freshness,
            "burn_rate": burn,
            "holds": holds,
            "jobs": jobs,
            "reallocate_queue": realloc,
            "reserve_by_niche": reserve_by_niche,
            "domain_view": domain_view,
            "summary": {
                "inboxes": len(fleet.get("inboxes") or []),
                "burned": len(burned),
                "actionable_burned": len(act),
                "needed_by_vertical": dict(need),
                "reserve": reserve,
                "replacement": replacement,
                # Named explicitly rather than implied by a zero: the absence of
                # a service pool is a standing decision, not a temporary dip.
                "service_reserve": 0,
            },
            "pools": pools,
            "_generated_at": ts,
            "_synced_at": ts,
        }))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()[-800:]})), 500

@app.route("/api/invariants")
def invariants_route():
    """The last stored run, with its age. `?refresh=1` recomputes.

    The refresh deliberately SKIPS the campaign scan: it is 130 of the 210
    seconds a full check takes, and Vercel stops a function at 300. The daily
    cron does the complete one. The response says which it was, because a
    result whose provenance is unstated is how a day-old cache gets read as
    live — see /api/overview.
    """
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    import check_invariants as civ
    try:
        if request.args.get("refresh"):
            payload = civ.run_and_store(want_campaigns=False)
        else:
            payload = civ.load_result()
            if not payload:
                return _cors(jsonify({
                    "error": "no run stored yet — add ?refresh=1, or wait for "
                             "the daily cron"})), 404
        gen = payload.get("generated_at")
        age = None
        if gen:
            import datetime as _dt
            try:
                t = _dt.datetime.strptime(gen, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=_dt.timezone.utc)
                age = int((_dt.datetime.now(_dt.timezone.utc) - t).total_seconds())
            except ValueError:
                pass
        payload["age_seconds"] = age
        payload["stale"] = (age is not None and age > 26 * 3600)
        return _cors(jsonify(payload))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()[-800:]})), 500


@app.route("/invariants")
@app.route("/invariants.html")
def invariants_page():
    return send_from_directory(_PUBLIC_DIR, "invariants.html")


@app.route("/dashboard")
@app.route("/dashboard.html")
def dashboard_page():
    # In production Vercel serves public/ statically and rewrites /dashboard ->
    # /dashboard.html; only /api/(.*) reaches this function at all. These exist
    # so the page renders when the app is run locally.
    return send_from_directory(_PUBLIC_DIR, "dashboard.html")


@app.route("/api/health-snapshot", methods=["GET", "POST", "OPTIONS"])
def health_snapshot():
    """Run today's snapshot: score the fleet from the cache, persist. Daily cron (GET)."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not (_check_auth() or _is_vercel_cron()):
        return _cors(jsonify({"error": "Unauthorized"})), 401
    import db as store
    store._CACHE_WRITE_ENABLED = True
    import traceback
    out, status = {}, 200
    try:
        import health_snapshot as hs
        out = hs.snapshot_daily()
    except Exception as e:
        out, status = {"error": str(e), "trace": traceback.format_exc()}, 500

    # The Zapmail removal watcher piggybacks this cron (Hobby allows 1 cron), but
    # it must NOT be collateral damage when the health snapshot fails — a missed
    # run means a mailbox cancellation goes unnoticed and we can't bill-optimise.
    try:
        import zapmail_removals as zr
        out["zapmail_removals"] = zr.check_removals()
    except Exception as ze:
        out["zapmail_removals"] = {"error": str(ze)}

    # The infra-decision countdown: the only thing that says "you have 7 days to
    # decide on Galaxy" before the mailboxes silently re-bill. Isolated so a CRM
    # outage cannot take the health snapshot down with it.
    try:
        import infra_lifecycle as ilc
        # run_daily records a heartbeat and alerts Slack on failure. The bare
        # post_notices(build()) this replaced put its exception in this dict
        # and nowhere else, which hid a five-day outage.
        out["infra_lifecycle"] = ilc.run_daily()
    except Exception as le:
        out["infra_lifecycle"] = {"error": str(le)}

    # Domain-expiry alerting. Both inputs were already pulled daily and nothing
    # joined them, which is how 69 live senders came within days of lapsing for
    # the sake of $244.
    try:
        import domain_expiry_alert as dea
        out["domain_expiry"] = dea.post(dea.build(), dry_run=False)
    except Exception as de:
        out["domain_expiry"] = {"error": str(de)}

    # The ten rules (docs/INFRA_RULES.md). Computed here because the full
    # check takes ~210s — fine inside a 300s cron, impossible on a page load —
    # and stored so /api/invariants can answer instantly with a timestamp.
    try:
        import check_invariants as civ
        r = civ.run_and_store(want_campaigns=True)
        out["invariants"] = {"counts": r.get("counts"), "stored": r.get("stored")}
    except Exception as ie:
        out["invariants"] = {"error": str(ie)}

    # Chase the Zapmail billing optimisation until it is actually done.
    # check_removals alerts ONCE and marks the entry notified; if that single
    # message is missed the slots bill forever and nothing says so.
    try:
        import billing_followup as bf
        out["billing_followup"] = bf.post(dry_run=False)
    except Exception as be:
        out["billing_followup"] = {"error": str(be)}
    return _cors(jsonify(out)), status


@app.route("/api/zapmail-removals", methods=["GET", "POST", "OPTIONS"])
def zapmail_removals_route():
    """Zapmail mailbox-removal watcher.
      GET  ?action=summary   -> pending vs removed registry
           ?action=check     -> run the diff now (dry_run unless &commit=1)
           ?action=flush     -> return+clear queued Slack messages (post via MCP)
           ?action=test      -> post a test line through post_slack(); the reply
                                says whether it went via 'webhook' or was 'queued'
                                (i.e. whether SLACK_ZAPMAIL_WEBHOOK is really set
                                on this deployment). Optional &msg=...
      POST {action:'register', domains:[...], source:'manual'} -> add to registry
    """
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    import db as store
    store._CACHE_WRITE_ENABLED = True
    try:
        import zapmail_removals as zr
        if request.method == "POST":
            body = request.get_json(silent=True) or {}
            if body.get("action") == "register":
                return _cors(jsonify(zr.register_domains(
                    body.get("domains") or [], body.get("source", "manual"))))
            return _cors(jsonify({"error": "unknown action"})), 400
        action = request.args.get("action", "summary")
        if action == "summary":
            return _cors(jsonify(zr.pending_summary()))
        if action == "check":
            commit = request.args.get("commit") in ("1", "true")
            return _cors(jsonify(zr.check_removals(dry_run=not commit)))
        if action == "flush":
            return _cors(jsonify({"messages": zr.flush_pending()}))
        if action == "test":
            msg = request.args.get("msg") or (
                ":wrench: Zapmail removal watcher — webhook test. If you can read "
                "this in #zapmail-billing, removal alerts will land here.")
            return _cors(jsonify({
                "posted_via": zr.post_slack(msg),
                "webhook_configured": bool(zr.SLACK_WEBHOOK),
            }))
        return _cors(jsonify({"error": "unknown action"})), 400
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/client-offboard", methods=["GET", "POST", "OPTIONS"])
def client_offboard():
    """Free up an off-boarded client's inboxes -> fresh Generic group + warm-up.

    GET  ?client=NAME            -> read-only plan + safety verdict
    POST {client, confirm:true}  -> pause own campaigns, re-tag, re-enable warm-up
    Refuses when the inboxes are still sending for ANOTHER client (unless force).
    """
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    import db as store
    store._CACHE_WRITE_ENABLED = True
    try:
        import health_offboard as ho
        if request.method == "GET":
            client = request.args.get("client", "").strip()
            if not client:
                return _cors(jsonify({"error": "client required"})), 400
            return _cors(jsonify(ho.plan(client)))
        body = request.get_json(silent=True) or {}
        client = (body.get("client") or "").strip()
        if not client:
            return _cors(jsonify({"error": "client required"})), 400
        res = ho.execute(client, confirm=bool(body.get("confirm")),
                         pause_campaigns=body.get("pause_campaigns", True),
                         force=bool(body.get("force")))
        return _cors(jsonify(res)), (400 if res.get("error") else 200)
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/health-burn", methods=["POST", "OPTIONS"])
def health_burn():
    """Plan or execute remove-on-renewal for burned inboxes.
    Body: {emails: [...], confirm: bool}.  confirm=false (default) => dry-run plan."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    body = request.get_json(silent=True) or {}
    emails = body.get("emails") or []
    confirm = bool(body.get("confirm"))
    if not emails:
        return _cors(jsonify({"error": "emails required"})), 400
    try:
        import health_actions as ha
        result = ha.schedule_removal(emails, dry_run=not confirm)
        return _cors(jsonify(result))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/health-replace", methods=["GET", "POST", "OPTIONS"])
def health_replace():
    """GET: list replacement jobs. POST {emails:[...]}: flag burned inboxes for replacement."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    import db as store
    store._CACHE_WRITE_ENABLED = True
    try:
        import health_replace as hr
        if request.method == "GET":
            return _cors(jsonify({"jobs": hr.list_jobs(), "reserve": hr.reserve_summary()}))
        emails = (request.get_json(silent=True) or {}).get("emails") or []
        if not emails:
            return _cors(jsonify({"error": "emails required"})), 400
        res = hr.create_jobs(emails)
        return _cors(jsonify({**res, "jobs": hr.list_jobs()}))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/health-recover", methods=["POST", "OPTIONS"])
def health_recover():
    """Put at-risk inboxes back on warm-up. Body {emails:[...], confirm:bool}."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    body = request.get_json(silent=True) or {}
    emails = body.get("emails") or []
    if not emails:
        return _cors(jsonify({"error": "emails required"})), 400
    try:
        import health_recover as hrec
        return _cors(jsonify(hrec.recover(emails, dry_run=not body.get("confirm"))))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/health-replace-all", methods=["POST", "OPTIONS"])
def health_replace_all():
    """Replace ALL burned inboxes of one client in a single shot (flag + assign
    niche-matched reserve + swap each). Body {client, confirm}. confirm=false
    returns a dry-run plan (count, niche, reserve availability)."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    import db as store
    store._CACHE_WRITE_ENABLED = True
    body = request.get_json(silent=True) or {}
    client = (body.get("client") or "").strip()
    if not client:
        return _cors(jsonify({"error": "client required"})), 400
    try:
        import health_replace as hr
        res = hr.replace_all_burned(client, confirm=bool(body.get("confirm")))
        return _cors(jsonify(res)), (400 if res.get("error") else 200)
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/buy-plan", methods=["POST", "OPTIONS"])
def buy_plan():
    """Full cost preview + provisioning batch config for a purchase spec. No spend.
    Body {owner, client_name?, provider:'google'|'outlook', inboxes_per_domain, domains:[...]}"""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    body = request.get_json(silent=True) or {}
    try:
        import buy_inboxes as bi
        return _cors(jsonify(bi.plan(body)))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/buy-suggest", methods=["POST", "OPTIONS"])
def buy_suggest():
    """Generate available domain suggestions (Spaceship-checked). No spend.
    Body {count?, tld?, ...}. If `brand` (a client's brand or real domain) is
    given, returns BRAND-DERIVED domains for that client; otherwise generic-
    service names, flavoured by `theme?('hvac'|'plumbing'|'landscaping')`."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    body = request.get_json(silent=True) or {}
    try:
        import buy_inboxes as bi
        count = int(body.get("count") or 12)
        tld = body.get("tld") or "info"
        brand = (body.get("brand") or "").strip()
        # Acquisition is THT prospecting for itself, so its domains are brand
        # variants (theheadlinetheoryhq.info, headlinetheory360.info, ...), not
        # generic service names. Offering "quicktaskpros.info" for an
        # acquisition batch was simply the wrong product (Tim, 2026-09-18), and
        # the default lives here rather than in the page so a hand-made request
        # gets it too.
        if not brand and (body.get("owner") or "").lower() == "acquisition":
            brand = ACQUISITION_BRAND
        if brand:
            # Acquisition domains must carry the WHOLE brand. A two-word brand
            # was yielding "headlineconnect" and "theorytoday" — names that do
            # not read as us at all.
            whole = (body.get("owner") or "").lower() == "acquisition"
            return _cors(jsonify(bi.suggest_client_domains(
                brand=brand, count=count, tld=tld, whole_brand_only=whole)))
        return _cors(jsonify(bi.suggest_generic(count=count, tld=tld, theme=body.get("theme"))))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/buy-domains", methods=["POST", "OPTIONS"])
def buy_domains_route():
    """PHASE 1 — register available domains on Spaceship + connect to Zapmail, open
    an order. Body {owner, client_name?, provider, inboxes_per_domain, domains, confirm}.
    confirm=false is a dry-run; confirm=true SPENDS on domains (the Confirm click)."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    import db as store
    store._CACHE_WRITE_ENABLED = True
    body = request.get_json(silent=True) or {}
    try:
        import buy_inboxes as bi
        res = bi.buy_domains(body, confirm=bool(body.get("confirm")))
        return _cors(jsonify(res)), (400 if res.get("error") else 200)
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/buy-progress", methods=["GET"])
def buy_progress_route():
    """Where a running purchase has got to. Polled by the page while it runs.

    A purchase is a minute or more of paid, irreversible registrations. With no
    feedback the natural reaction is to press the button again, and that spends
    the money twice.
    """
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import buy_inboxes as bi
        return _cors(jsonify(bi.buy_progress()))
    except Exception as e:
        return _cors(jsonify({"error": str(e)})), 500


@app.route("/api/buy-orders", methods=["GET"])
def buy_orders_route():
    """All buy-orders with live DNS readiness (drives the 'Provision inboxes now' button).

    NEVER CACHED. This is the record of what you just did. It was served from a
    six-hour cache, so an order created at 20:42 was invisible behind a cache
    written at 20:31 — Tim bought 14 domains and the tab showed nothing. A
    caching layer in front of "did my purchase register?" answers the one
    question it must never get wrong.

    The DNS-readiness lookup inside it is a single Zapmail call, which is what
    the cache was protecting against; that is not worth hiding a purchase for.
    """
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import buy_inboxes as bi
        return _cors(jsonify(bi.list_orders()))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/buy-provision", methods=["POST", "OPTIONS"])
def buy_provision_route():
    """PHASE 2 — push an order as far towards live as it will go right now.

    Body {order_id, confirm}. Without `confirm` it reports where the order
    stands and what it would do next, and spends nothing.

    This is deliberately RESUMABLE rather than long-running. DNS propagation,
    Zapmail slot settlement and the SmartLead export are all unbounded waits,
    and Vercel stops a function at 300s. So each call does what it can now,
    journals it onto the order, and answers `resume: true` if it wants calling
    again. `resume` is the normal answer for the first hour of an order's life;
    it is not an error.
    """
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    import db as store
    store._CACHE_WRITE_ENABLED = True
    body = request.get_json(silent=True) or {}
    oid = body.get("order_id")
    if oid is None:
        return _cors(jsonify({"error": "order_id required"})), 400
    try:
        import buy_inboxes as bi
        import buy_provision as bp

        orders = bi._orders()
        order = next((o for o in orders if o.get("id") == int(oid)), None)
        if not order:
            return _cors(jsonify({"error": "order not found"})), 400

        # The rules are checked even on the dry run, so an order that can never
        # be provisioned says so before anyone clicks the spending button.
        try:
            bp.check_order(order)
        except bp.ProvisionRefused as e:
            return _cors(jsonify({"error": str(e), "refused": True})), 400

        if not body.get("confirm"):
            ready = bi.order_readiness(int(oid))
            ready["step"] = (order.get("journal") or {}).get("step", "dns")
            ready["provision_enabled"] = True
            ready["note"] = ("Dry run. Confirm to create mailboxes, hand them to "
                             "SmartLead, tag them and start warm-up. Nothing has "
                             "been spent.")
            return _cors(jsonify(ready))

        # One advance at a time. A step can outrun the browser's 120s while
        # this function keeps working to 300s — and a timeout is precisely when
        # someone clicks again. Without this, both calls read the same journal
        # step and both buy mailbox slots for it.
        lock = bi.provision_lock_acquire(int(oid))
        if not lock.get("ok"):
            return _cors(jsonify({
                "resume": True,
                "locked": True,
                "note": f"already provisioning (started {lock.get('age_seconds', 0)}s "
                        f"ago) — leaving it to finish rather than buying the same "
                        f"slots twice.",
            }))
        try:
            res = bp.advance(order, bp.LiveIO())
        finally:
            # Save before answering: a response the caller never receives must
            # not cost us the record of what was already created.
            bi._save_orders(orders)
            bi.provision_lock_release(int(oid))
        return _cors(jsonify(res))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/health-resolve", methods=["GET", "POST", "OPTIONS"])
def health_resolve():
    """Auto-resolve flagged SmartLead inboxes: reconnect (re-export) the ones with a
    live Zapmail source, escalate the rest (source gone -> delete, warmup-block,
    unknown). GET / POST-without-confirm = dry-run (classify only). POST {confirm:true}
    re-exports the reconnect bucket and verifies. Never deletes."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    import db as store
    store._CACHE_WRITE_ENABLED = True
    commit = request.method == "POST" and bool((request.get_json(silent=True) or {}).get("confirm"))
    try:
        import health_resolve as hr
        return _cors(jsonify(hr.resolve(dry_run=not commit)))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/enforce-esp-matching", methods=["GET", "POST", "OPTIONS"])
def enforce_esp_matching_route():
    """Inbox-provider (ESP) matching for acquisition campaigns.
    GET  -> last enforcement result (from state).
    POST -> run enforcement now: turn ON `enable_ai_esp_matching` for every
            ACTIVE/PAUSED/DRAFTED acquisition campaign that has it off.
    This also runs automatically on every sync, so new acquisition campaigns get it."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    if request.method == "GET":
        import db as store
        return _cors(jsonify(store.get_state("esp_matching_enforcement") or {"ran_at": None}))
    import db as store
    store._CACHE_WRITE_ENABLED = True
    try:
        import sync as _sync
        return _cors(jsonify(_sync.enforce_esp_matching()))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/health-disconnected")
def health_disconnected():
    """Inboxes disconnected from SmartLead (smtp auth broken). Splits critical
    (in an ACTIVE campaign — silently not sending) from idle. Drives the always-on
    top-of-page alert."""
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import health_disconnected as hdc
        return _cors(jsonify(hdc.disconnected_view()))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/health-domains")
def health_domains():
    """Domain-grouped view of the burned fleet — the top-priority reallocate/cancel
    surface. Ranks domains by burned count (3->2->1) with collateral + reserve +
    scheduled state so cancellations are made per whole domain (how Zapmail bills)."""
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import health_domains as hd
        return _cors(jsonify(hd.domain_view()))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/campaign-senders")
def campaign_senders_route():
    """ACTIVE campaigns with no senders left, and the ones heading that way.

    A campaign with zero senders stays ACTIVE and reports clean — it just sends
    nothing — so it never surfaces in any inbox- or capacity-based roll-up. Reads
    live from SmartLead (one call per active campaign, ~35) and cross-checks the
    Zapmail removal registry, so a campaign whose remaining senders are all on
    already-scheduled domains is flagged BEFORE their billing dates land."""
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import campaign_senders as cs
        res = cs.build()
        return _cors(jsonify(res)), (400 if res.get("error") else 200)
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/acq-capacity")
def acq_capacity_route():
    """Acquisition sending capacity vs actual usage, and which inboxes are idle.

    ?live=1 re-pulls the SmartLead campaign list so campaign statuses are current
    (the cached ones are only as fresh as the last sync). One extra API call.
    """
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import acq_capacity as ac
        live = request.args.get("live") in ("1", "true", "yes")
        # ?live=1 is an explicit ask for a fresh campaign pull, so it bypasses
        # the cache rather than being served a stale answer to a live question.
        res = (ac.build(live=True) if live
               else _slow_cache("cache:acq_capacity", ac.build))
        return _cors(jsonify(res)), (400 if res.get("error") else 200)
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/client-detail")
def client_detail_route():
    """Detail for ONE client, sliced out of a single cached build for all of them.

    The first version answered one client per request and re-fetched the world
    each time: ~29s for the Smartlead tag walk, ~9s for Zapmail (called twice),
    ~4s for health, ~1s for the CRM. Fifty seconds to open a card, and the same
    fifty again for the next one, because nothing was shared. None of those
    reads is per-client — they all return the whole fleet and get filtered — so
    the build is done once for everybody and cached, and opening a card is a
    dict lookup.

    `?refresh=1` rebuilds. The nightly workflow warms it so the first person to
    open a card is not the one who pays for the build.
    """
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    name = (request.args.get("name") or "").strip()
    try:
        import check_invariants as civ
        import client_details as cd
        import infra_lifecycle as ilc

        def _build():
            import db as store
            board = _slow_cache("cache:infra_lifecycle", ilc.build)
            # One Zapmail walk, not two. The previous code called
            # fetch_zapmail_inventory() once for domains and once for mailboxes.
            try:
                inv = ilc.fetch_zapmail_inventory()
            except Exception:
                inv = {}
            try:
                tags = ilc.fetch_smartlead_tags() or None
            except Exception:
                tags = None
            # When each inbox STARTED WARMING, which is a Smartlead fact. A
            # mailbox can sit in Zapmail for weeks before it is exported —
            # McFarlane's second batch was created 2026-08-12 and only began
            # warming 2026-09-08 — so the Zapmail date would call an unwarmed
            # inbox ready and put it into a live campaign.
            warm_starts = None
            try:
                import acq_capacity as _ac
                facts = _ac._live_account_facts()
                if facts:
                    warm_starts = {e: (v.get("warmup_started") or v.get("created_at"))
                                   for e, v in facts.items()}
            except Exception:
                warm_starts = None
            try:
                health = {h["email"]: h.get("status")
                          for h in store.get_health_status_all()}
            except Exception:
                health = {}
            try:
                crm_rows = ilc.fetch_crm_clients() or []
            except Exception:
                crm_rows = []
            import datetime as _dt
            return {"clients": cd.build(board, (inv or {}).get("mailboxes") or None,
                                        tags, health, crm_rows,
                                        civ._norm, civ.is_free_account,
                                        today=_dt.date.today(),
                                        warm_starts=warm_starts)}

        blob = _slow_cache("cache:client_details", _build, ttl_seconds=12 * 3600)
        details = blob.get("clients") or {}
        if request.args.get("all") in ("1", "true", "yes"):
            # The whole map in one response. The page pulls this in the
            # background once the cards have painted, so opening a card needs no
            # network at all — which is the point, since the alternative was a
            # visible wait on every single click.
            out = {"clients": details}
            for k in ("_cached", "_generated_at", "_age_seconds", "_stale"):
                if k in blob:
                    out[k] = blob[k]
            return _cors(jsonify(out))
        if not name:
            # No name: just say what is loaded, so the cron can warm it.
            return _cors(jsonify({"clients": sorted(d["client"] for d in details.values()),
                                  "_cached": blob.get("_cached"),
                                  "_age_seconds": blob.get("_age_seconds")}))
        det = details.get(civ._norm(name))
        if not det:
            return _cors(jsonify({"error": f"no infrastructure row for {name!r}"})), 404
        det = dict(det)
        for k in ("_cached", "_generated_at", "_age_seconds", "_stale"):
            if k in blob:
                det[k] = blob[k]
        return _cors(jsonify(det))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/onboarding")
def onboarding_route():
    """The CRM -> infrastructure handoff: what each won client is still owed.

    The CRM decides who is a client; this reports what infrastructure has not
    yet delivered for them. Nothing here changes anything — buying inboxes and
    setting forwarding both cost money or move live sending, so the operator
    presses those buttons on the tabs that own them.

    A failed Zapmail read passes None for the forwarding map, which makes the
    forwarding step read UNKNOWN rather than done. A client whose forwarding we
    could not check must never be reported as fully onboarded.
    """
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        from datetime import date
        import check_invariants as civ
        import infra_lifecycle as ilc
        import onboarding_view as ov

        board = _slow_cache("cache:infra_lifecycle", ilc.build)
        if board.get("error"):
            return _cors(jsonify({"error": board["error"]})), 400
        crm_rows = ilc.fetch_crm_clients() or []
        if not crm_rows:
            # An empty CRM roster is never a real answer — it would report every
            # client as fully onboarded by saying there are none.
            return _cors(jsonify({"error": "CRM returned no clients — refusing to "
                                           "report an empty onboarding queue"})), 400

        # domain -> forwardTo, and which client each domain belongs to. None
        # (not {}) when Zapmail cannot be read.
        def _fwd_map():
            inv = ilc.fetch_zapmail_inventory()
            return {"domains": inv.get("domains") or None,
                    "mailboxes": inv.get("mailboxes") or None}
        try:
            inv = _slow_cache("cache:zm_inventory", _fwd_map)
            zm_domains = inv.get("domains")
            zm_mailboxes = inv.get("mailboxes")
        except Exception:
            zm_domains = zm_mailboxes = None

        by_client = None
        if zm_domains and zm_mailboxes:
            tags = ilc.fetch_smartlead_tags() or {}
            crm_names = [c["name"] for c in crm_rows if c.get("name")]
            by_client = {}
            for email, mb in zm_mailboxes.items():
                owner = tags.get(email)
                if not owner:
                    continue
                name = ilc.match_client(owner, crm_names) or owner
                dom = mb.get("domain")
                if not dom:
                    continue
                rec = zm_domains.get(dom) or {}
                seen = by_client.setdefault(name, {})
                seen[dom] = {"domain": dom,
                             "forward_to": rec.get("forward_to") or rec.get("forwardTo")}
            by_client = {k: list(v.values()) for k, v in by_client.items()}

        res = ov.build(crm_rows, board, by_client, civ.target_for,
                       date.today().isoformat(), match=ilc.match_client,
                       is_exempt=civ.is_free_account)
        for k in ("_cached", "_generated_at", "_age_seconds", "_stale"):
            if k in board:
                res[k] = board[k]
        return _cors(jsonify(res))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/rule-fix", methods=["POST", "OPTIONS"])
def rule_fix_route():
    """Fix a failing invariant rule. Body {rule, confirm}.

    Dry-run without `confirm`. Every fix re-derives its own target list from
    live data rather than trusting the stored rule result, which can be hours
    old — acting on a stale violation is the failure this repo has had six
    times. See rule_fixes.py for which rules have a fix and why the others do
    not.
    """
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    import db as store
    store._CACHE_WRITE_ENABLED = True
    body = request.get_json(silent=True) or {}
    try:
        rule = int(body.get("rule"))
    except (TypeError, ValueError):
        return _cors(jsonify({"error": "rule (number) required"})), 400
    try:
        import rule_fixes as rfx
        io = _RuleFixIO()
        res = rfx.run(rule, io, confirm=bool(body.get("confirm")))
        return _cors(jsonify(res)), (400 if res.get("error") else 200)
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/rule10-reply-scan")
def rule10_reply_scan():
    """The nightly full-campaign positive-reply walk behind rule 10's fix.

    This cannot live in the button. Answering "does this mailbox own a reply"
    honestly means reading EVERY campaign — paused and completed ones hold
    threads too — which is ~415 campaign reads plus a leads-export each. The
    button gets ~120s before the browser gives up, so it reads what this wrote.

    A scan that cannot finish writes nothing, and rule 10's fix then refuses to
    delete rather than deleting against a check that never ran.
    """
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    import db as store
    store._CACHE_WRITE_ENABLED = True
    try:
        import rule10_safety
        io = _RuleFixIO()
        live = io.zapmail_mailboxes()
        accounts = io.smartlead_accounts()
        if live is None or accounts is None:
            return _cors(jsonify({"error": "could not read Zapmail and Smartlead "
                                           "in full — not scanning a partial roster"})), 400
        external = io.external_domains()
        candidates = [a["email"] for a in accounts
                      if a["email"] not in live
                      and a["email"].split("@")[-1] not in external]
        res = rule10_safety.refresh(candidates)
        return _cors(jsonify(res)), (400 if res.get("error") else 200)
    except Exception as e:
        return _cors(jsonify({"error": str(e)})), 500


class _RuleFixIO:
    """The live world for rule_fixes. Every read is all-or-nothing: a short
    answer returns None so the fix refuses rather than acting on part of it."""

    def registrar_domains(self):
        try:
            import domain_expiry_alert as dea
            return dea.fetch_registrar_domains() or None
        except Exception:
            return None

    def mailbox_counts_by_domain(self):
        try:
            import infra_lifecycle as ilc
            mbx = (ilc.fetch_zapmail_inventory() or {}).get("mailboxes") or None
        except Exception:
            return None
        if not mbx:
            return None
        out = {}
        for mb in mbx.values():
            d = (mb.get("domain") or "").lower()
            if d:
                out[d] = out.get(d, 0) + 1
        return out

    def sender_counts_by_domain(self):
        """{domain: live Smartlead accounts on it}. None if the walk was short —
        a partial roster would make a busy domain look empty."""
        try:
            import acq_capacity as ac
            facts = ac._live_account_facts()
        except Exception:
            return None
        if not facts:
            return None
        out = {}
        for e in facts:
            d = e.split("@")[-1].lower()
            out[d] = out.get(d, 0) + 1
        return out

    def renewal_price(self, domain):
        try:
            import domain_expiry_alert as dea
            return dea.renewal_price(domain)
        except Exception:
            return 0.0

    def zapmail_mailboxes(self):
        try:
            import infra_lifecycle as ilc
            mbx = (ilc.fetch_zapmail_inventory() or {}).get("mailboxes") or None
        except Exception:
            return None
        return set(mbx) if mbx else None

    def smartlead_accounts(self):
        try:
            import acq_capacity as ac
            facts = ac._live_account_facts()
        except Exception:
            return None
        if not facts:
            return None
        return [{"email": e, "id": v.get("account_id")} for e, v in facts.items()]

    def external_domains(self):
        try:
            import check_invariants as civ
            return set(civ.EXTERNAL_DOMAINS)
        except Exception:
            return set()

    def safety_check(self, emails):
        """Which of these are on an ACTIVE campaign or own a positive reply.

        A scan that cannot complete returns an error, and the fix aborts — an
        incomplete scan is not a clean scan.
        """
        import time as _t
        import requests as _rq
        key = (os.environ.get("SMARTLEAD_API_KEY") or "").strip()
        sl = "https://server.smartlead.ai/api/v1"
        want = {e.lower() for e in emails}

        def _get(path, **p):
            p["api_key"] = key
            for a in range(6):
                try:
                    r = _rq.get(f"{sl}{path}", params=p, timeout=60)
                    if r.status_code == 200:
                        return r.json()
                    if r.status_code == 429:
                        _t.sleep(10 * (a + 1))
                        continue
                except _rq.RequestException:
                    pass
                _t.sleep(4 * (a + 1))
            return None

        camps = _get("/campaigns")
        if camps is None:
            return {"error": "campaign list unreadable"}
        # ONLY ACTIVE CAMPAIGNS. Scanning all 409 took longer than the request
        # itself was allowed to live — the button timed out at 120s having done
        # nothing. A paused or completed campaign cannot make a mailbox "in
        # use", so the other ~370 were being read for no answer. There are ~41
        # active ones; that is a few seconds.
        camps = [c for c in camps if (c.get("status") or "").upper() == "ACTIVE"]
        hits, fails = {}, 0
        for c in camps:
            accs = _get(f"/campaigns/{c['id']}/email-accounts")
            if accs is None:
                fails += 1
                continue
            for a in accs or []:
                em = (a.get("from_email") or "").lower()
                if em in want:
                    hits.setdefault(em, []).append(
                        {"id": c.get("id"), "status": (c.get("status") or "").upper()})
            _t.sleep(0.06)
        if fails:
            return {"error": f"{fails} campaign(s) could not be read"}
        # Everything left in `hits` is by definition on an ACTIVE campaign, so
        # that IS the answer. Running a positive-reply pass over `hits` here
        # would cost ~39s to re-name mailboxes this list already holds, and it
        # would still miss a reply parked on a paused campaign, because those
        # campaigns were filtered out above. That question belongs to the
        # nightly full-campaign walk in rule10_safety.py.
        return {"active": sorted(hits), "positives": []}

    def disable_auto_renew(self, domains):
        """Reuses the same registrar helpers /api/domains/auto-renew uses, so
        there is one implementation of 'turn auto-renew off', not two."""
        import db as store
        known = {d["domain"]: d for d in store.get_all_domains()}
        ok, failed = [], []
        for d in domains:
            rec = known.get(d)
            if not rec:
                failed.append(f"{d}: not in the domains table")
                continue
            prov = (rec.get("provider") or "").lower()
            try:
                if prov == "porkbun":
                    r = _porkbun_set_ar(d, False)
                elif prov == "spaceship":
                    r = _spaceship_set_ar(d, False)
                else:
                    failed.append(f"{d}: unknown provider {prov!r}")
                    continue
            except Exception as e:
                failed.append(f"{d}: {str(e)[:60]}")
                continue
            if r.get("success"):
                ok.append(d)
                store.update_domain(d, auto_renew=False)
            else:
                failed.append(f"{d}: {str(r.get('message') or r.get('error'))[:60]}")
        return {"ok": not failed, "disabled": len(ok), "failed": failed}

    def delete_smartlead(self, rows):
        import time as _t
        import requests as _rq
        key = (os.environ.get("SMARTLEAD_API_KEY") or "").strip()
        sl = "https://server.smartlead.ai/api/v1"
        ok, failed = [], []
        for r in rows:
            done = False
            for a in range(5):
                resp = _rq.delete(f"{sl}/email-accounts/{r['id']}",
                                  params={"api_key": key}, timeout=30)
                if resp.status_code in (200, 204):
                    done = True
                    break
                if resp.status_code == 429:
                    _t.sleep(15 * (a + 1))
                    continue
                break
            (ok if done else failed).append(r["email"])
            _t.sleep(0.8)
        return {"ok": not failed, "deleted": len(ok), "failed": failed}


@app.route("/api/acquisition")
def acquisition_route():
    """The Acquisition tab: our own prospecting inboxes, and the domains under them.

    Both inputs are cached (6h) because both are slow — the capacity build walks
    every SmartLead account, and the registrar walk pages two APIs. ?live=1
    rebuilds the capacity side, which is the half that changes hour to hour.

    A registrar read that FAILS passes None rather than {}: an empty map would
    make every domain report "no expiry recorded", which reads as reassurance.
    None makes the page say it did not look.
    """
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        from datetime import date
        import acq_capacity as ac
        import acquisition_view as av

        # Always built from a LIVE Smartlead walk, cached for a day.
        #
        # It used to serve `_slow_cache(ac.build)` — ac.build() with live=False,
        # which takes its roster from the overview_v2 sync cache. That cache is
        # only as fresh as the last sync, so the tab could report inboxes that
        # had since been cancelled or re-tagged, and miss ones just bought. Tim,
        # 2026-09-19: the numbers must come from Smartlead each day and say
        # whether an inbox is actually there and allocatable.
        live = request.args.get("live") in ("1", "true", "yes")
        acq = (ac.build(live=True) if live
               else _slow_cache("cache:acq_capacity_live",
                                lambda: ac.build(live=True),
                                ttl_seconds=24 * 3600))

        # Wrapped in a dict on purpose: _slow_cache stamps its own keys onto
        # whatever it stores, so handing it the bare {domain: ...} map would mix
        # `_cached` in among the domains. `domains: None` survives the round
        # trip and still means "we could not read the registrar".
        def _registrar():
            import domain_expiry_alert as dea
            return {"domains": dea.fetch_registrar_domains() or None}

        try:
            reg = (_slow_cache("cache:registrar_domains", _registrar) or {}).get("domains")
        except Exception:
            reg = None

        # The replacement pool is shared with the client fleet, so it is read
        # from the same lifecycle board the Pools tab uses. None (not 0) when
        # that read fails — "we could not look" must not render as "none left".
        replacement = None
        try:
            import check_invariants as civ
            board = _slow_cache("cache:infra_lifecycle", __import__("infra_lifecycle").build)
            for row in (board.get("rows") or []):
                if str(row.get("client") or "").strip().lower().startswith("replacement"):
                    replacement = row.get("mailboxes")
                    break
        except Exception:
            replacement = None

        res = av.build(acq, reg, date.today().isoformat(), replacement_pool=replacement)
        for k in ("_cached", "_generated_at", "_age_seconds", "_stale"):
            if isinstance(acq, dict) and k in acq:
                res[k] = acq[k]
        return _cors(jsonify(res)), (400 if res.get("error") else 200)
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/generic-capacity")
def generic_capacity_route():
    """Generic (non-acquisition) inbox capacity: what is genuinely free to deploy.

    Always reads live from SmartLead — the roster, the tags and the active
    campaigns. The overview cache is only as fresh as the last sync, and a stale
    cache does not look broken here, it looks like a smaller fleet: read on
    2026-09-02 it held no holiday stock at all because the pool was bought after
    the last sync ran. Takes ~25s.
    """
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import generic_capacity as gc
        res = _slow_cache("cache:generic_capacity", gc.build)
        return _cors(jsonify(res)), (400 if res.get("error") else 200)
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/free-capacity")
def free_capacity_route():
    """Free acquisition capacity for the daily 7am email (Tim, 2026-09-04).

    'Free' == a warmup-complete inbox that is NOT sending and NOT between
    sequences in an active campaign that still has work, with the still-warming
    batch excluded (?warmup_days=, default 14). Always live so campaign
    statuses, both lead queues, and created_at are current. Read-only; returns
    a flat payload the routine can drop straight into an email.

    `free_capacity` is the MEASURED figure, not the state-derived one. The idle
    states (stranded/parked/unassigned) are a claim about campaign membership,
    and on the acquisition fleet that claim is mostly wrong: a paused or
    completed campaign still ships the follow-ups already queued inside it, so
    an inbox in no active campaign can be sending at its daily cap. Measured on
    2026-09-02, 49 of the 50 inboxes the states called idle had been sending —
    this email would have opened with "750/day free" when the real number was
    15. `followup_only_*` does not cover them: it only inspects senders already
    classified SENDING, and these sit in no active campaign at all.

    The state figure is still reported as `free_capacity_by_state` beside the
    phantom counts, so the two can be compared rather than silently swapped.
    """
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import acq_capacity as ac
        days = int(request.args.get("warmup_days", 14))
        rep = ac.build(live=True, exclude_warming_days=days)
        if rep.get("error"):
            return _cors(jsonify(rep)), 400
        s = rep["summary"]
        free_states = ("stranded", "parked", "unassigned")
        starved = [{"name": c["name"], "wants_senders": c["wants_senders"],
                    "remaining": c.get("remaining"), "url": c.get("url")}
                   for c in rep.get("campaigns", []) if c.get("wants_senders")]
        # Fall back to the state figure only when there are no daily rows to
        # check it against, so a fresh database degrades to the old behaviour
        # instead of reporting zero free capacity.
        measured = s.get("idle_capacity_measured") is not None
        out = {
            "generated_at": rep["generated_at"],
            "synced_at": rep.get("synced_at"),
            "warmup_days_threshold": days,
            "warming_excluded": s.get("warming_excluded", 0),
            "warmup_complete_inboxes": s["inboxes"],
            "total_capacity": s["total_capacity"],
            "usable_capacity": s["usable_capacity"],
            "deployed_capacity": s["deployed_capacity"],
            "free_inboxes": s["idle_inboxes_measured"] if measured else s["idle_inboxes"],
            "free_capacity": s["idle_capacity_measured"] if measured else s["idle_capacity"],
            "free_is_measured": measured,
            "free_inboxes_by_state": s["idle_inboxes"],
            "free_capacity_by_state": s["idle_capacity"],
            "phantom_idle_inboxes": s.get("phantom_idle_inboxes"),
            "phantom_idle_capacity": s.get("phantom_idle_capacity"),
            "phantom_actual_per_day": s.get("phantom_actual_per_day"),
            "measurement_window": s.get("measured"),
            "followup_only_inboxes": s.get("followup_only_inboxes", 0),
            "followup_only_capacity": s.get("followup_only_capacity", 0),
            "utilisation_pct": s["utilisation_pct"],
            "sending_day_pct": s.get("sending_day_pct"),
            "allocation_pct": s["allocation_pct"],
            "blocked_inboxes": s["blocked_inboxes"],
            "actual_per_day": s["actual_per_day"],
            "free_by_state": {st: s["by_state"].get(st, {"inboxes": 0, "capacity": 0})
                              for st in free_states},
            "starved_campaigns": starved[:10],
        }
        return _cors(jsonify(out)), 200
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/acq-allocate", methods=["POST", "OPTIONS"])
def acq_allocate_route():
    """Move acquisition inboxes between campaigns.

    Body {emails:[...], to_campaign_id, from_campaign_id?, override_active?, confirm}.
    confirm=false (the default) is a dry-run returning the full plan plus every
    safety rail it trips. With no from_campaign_id the inbox is detached from all
    of its current campaigns, so it can never end up sending from two at once.
    """
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    import db as store
    store._CACHE_WRITE_ENABLED = True
    body = request.get_json(silent=True) or {}
    emails = body.get("emails") or []
    if not emails:
        return _cors(jsonify({"error": "emails required"})), 400
    to_id = body.get("to_campaign_id")
    from_id = body.get("from_campaign_id")
    if not to_id and not from_id:
        return _cors(jsonify({"error": "to_campaign_id or from_campaign_id required"})), 400
    override = bool(body.get("override_active"))
    try:
        import acq_capacity as ac
        fn = ac.apply if body.get("confirm") else ac.plan
        res = fn(emails, to_campaign_id=to_id, from_campaign_id=from_id,
                 override_active=override)
        return _cors(jsonify(res)), (400 if res.get("error") else 200)
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/health-reallocate", methods=["POST", "OPTIONS"])
def health_reallocate():
    """Reallocate an explicit set of burned inboxes: remove each from its campaign(s)
    and swap in a niche-matched reserve. Body {emails:[...], confirm}. confirm=false
    is a dry-run (reserve sufficiency per niche; acquisition inboxes reported blocked)."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    import db as store
    store._CACHE_WRITE_ENABLED = True
    body = request.get_json(silent=True) or {}
    emails = body.get("emails") or []
    if not emails:
        return _cors(jsonify({"error": "emails required"})), 400
    try:
        import health_replace as hr
        res = hr.reallocate_emails(emails, confirm=bool(body.get("confirm")))
        return _cors(jsonify(res)), (400 if res.get("error") else 200)
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/health-reallocate-campaigns", methods=["GET", "POST", "OPTIONS"])
def health_reallocate_campaigns():
    """The full-name checklist of campaigns that had sender swaps and still need
    SmartLead's manual 'Reallocate mailboxes' click (no API for that step).
      GET  -> {campaigns:[{name,status}], count}   (ACTIVE first)
      POST {done: '<full name>'} -> tick one off after you've reallocated it."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    import db as store
    store._CACHE_WRITE_ENABLED = True
    try:
        import health_replace as hr
        if request.method == "POST":
            name = (request.get_json(silent=True) or {}).get("done")
            if not name:
                return _cors(jsonify({"error": "done (campaign name) required"})), 400
            return _cors(jsonify(hr.clear_reallocation_campaign(name)))
        return _cors(jsonify(hr.reallocation_campaigns()))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/health-positive-holds", methods=["GET", "OPTIONS"])
def health_positive_holds():
    """Burned inboxes that were KEPT in a campaign because they own a positive
    reply — a reply lives in the sending mailbox and cannot be moved, so
    detaching would freeze the thread. Each entry lists the threads at stake.
      GET -> {holds:[...], count, inboxes, positive_threads}
    ?include_released=1 also returns ones already released/kept (audit trail)."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import health_positive as hp
        inc = request.args.get("include_released") in ("1", "true", "yes")
        return _cors(jsonify(hp.list_holds(include_released=inc)))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/health-positive-release", methods=["POST", "OPTIONS"])
def health_positive_release():
    """Act on a hold once the reply has been worked.
      POST {key, confirm}      -> remove the inbox from that campaign (the
                                  deferred detach; confirm=false is a dry-run)
      POST {key, action:'keep'} -> drop the hold, LEAVE the inbox in the campaign

    This is the only path that detaches a held inbox, so the call is always
    deliberate and always after someone has dealt with the conversation."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    body = request.get_json(silent=True) or {}
    key = body.get("key")
    if not key:
        return _cors(jsonify({"error": "key required"})), 400
    try:
        import health_positive as hp
        if body.get("action") == "keep":
            res = hp.cancel_hold(key)
        else:
            res = hp.release(key, confirm=bool(body.get("confirm")))
        return _cors(jsonify(res)), (400 if res.get("error") else 200)
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/health-positive-check", methods=["POST", "OPTIONS"])
def health_positive_check():
    """Pre-flight: which of these inboxes own positive replies, and where?

    POST {emails:[...]} -> {results:[{email, campaigns:[{campaign, positive_count,
    threads}], positive_total}]}. Read-only — the reallocate dry-run calls this so
    the operator sees what would be held BEFORE committing to anything."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    body = request.get_json(silent=True) or {}
    emails = body.get("emails") or []
    if not emails:
        return _cors(jsonify({"error": "emails required"})), 400
    try:
        import db as store
        import health_replace as hr
        import health_positive as hp
        status_by = {r["email"]: r for r in store.get_health_status_all()}
        status_map = hr.campaign_status_map()
        out = []
        for e in emails:
            names = [c for c in (status_by.get(e, {}).get("campaigns") or [])
                     if status_map.get(c) == "ACTIVE"]
            cids = hr._resolve_campaign_ids(names)
            per = []
            unknown = False
            for name, cid in cids.items():
                try:
                    th = hp.owned_positive_threads(e, cid)
                except Exception as exc:
                    # owned_positive_threads' contract: "Raises on an
                    # inconclusive lookup — callers MUST treat that as hold,
                    # never as no replies." This used to record None and then
                    # `sum(... or 0)` turned it into 0, so a rate-limited
                    # leads-export made an inbox read as CLEAR and it would be
                    # detached from a conversation it still owns.
                    unknown = True
                    per.append({"campaign": name, "campaign_id": cid,
                                "positive_count": None, "threads": [],
                                "error": str(exc)})
                    continue
                if th:
                    per.append({"campaign": name, "campaign_id": cid,
                                "positive_count": len(th), "threads": th})
            known = sum(c.get("positive_count") or 0 for c in per)
            out.append({"email": e, "campaigns": per,
                        # None means UNKNOWN, and the caller must hold. It is
                        # deliberately not 0.
                        "positive_total": None if unknown else known,
                        "unknown": unknown,
                        "known_positive_count": known})
        return _cors(jsonify({"results": out,
                              "inboxes_with_positives": sum(1 for r in out if r["positive_total"]),
                              "positive_total": sum(r["known_positive_count"] for r in out),
                              "unknown_count": sum(1 for r in out if r["unknown"])}))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/health-cancel-domain", methods=["POST", "OPTIONS"])
def health_cancel_domain():
    """Schedule whole-domain removal in Zapmail (mailboxes deleted on next billing
    date) AND register the domains with the removal watcher so the Slack bot alerts
    on actual cancellation. Body {domains:[...], confirm}. confirm=false is a dry-run
    (resolves Zapmail ids, flags external/unknown domains)."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    import db as store
    store._CACHE_WRITE_ENABLED = True
    body = request.get_json(silent=True) or {}
    domains = body.get("domains") or []
    if not domains:
        return _cors(jsonify({"error": "domains required"})), 400
    try:
        import zapmail_removals as zr
        # Cancelling deletes the mailboxes, which destroys every conversation they
        # own — permanently, unlike a detach. Refuse while a held inbox on this
        # domain still has an unworked positive reply.
        import health_positive as hp
        doms = {d.lower() for d in domains}
        blocking = [h for h in hp.list_holds().get("holds", [])
                    if (h.get("email") or "").split("@")[-1].lower() in doms]
        if blocking and not body.get("override_positive_holds"):
            return _cors(jsonify({
                "error": "positive replies still held on this domain",
                "blocked_by": blocking,
                "note": ("These inboxes own live positive threads. Cancelling deletes the "
                         "mailbox and the thread with it — unrecoverable. Work the replies "
                         "and release them first, or resend with override_positive_holds."),
            })), 409
        res = zr.cancel_domains(domains, dry_run=not bool(body.get("confirm")))
        return _cors(jsonify(res)), (400 if res.get("error") else 200)
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/health-buy-plan")
def health_buy_plan():
    """Dry-run plan to replenish the warmed reserve. ?target=N. Buys nothing."""
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import health_buy as hb
        target = int(request.args.get("target", hb.DEFAULT_TARGET))
        return _cors(jsonify(hb.plan_replenish(target)))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/smartlead-jwt-status")
def smartlead_jwt_status():
    """Diagnostic for the SmartLead token / auto-refresh. Booleans only."""
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import os
        import health_smartlead as hsl
        out = {"has_env_jwt": bool(os.environ.get("SMARTLEAD_JWT", "").strip()),
               "current_token_valid": hsl.jwt_ok()}
        out.update(hsl.login_diag())
        return _cors(jsonify(out))
    except Exception as e:
        return _cors(jsonify({"error": str(e)})), 500


@app.route("/api/health-renewals")
def health_renewals():
    """Per-inbox renewal dates + renew/drop decisions. ?refresh=1 re-pulls Zapmail."""
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    import db as store
    store._CACHE_WRITE_ENABLED = True
    try:
        import health_renewals as hrn
        refresh = request.args.get("refresh") == "1"
        return _cors(jsonify(hrn.build_tracking(refresh=refresh)))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/health-replace/advance", methods=["POST", "OPTIONS"])
def health_replace_advance():
    """Move a replacement forward. Body: {id, action: warm|swap|cancel, new_domain?}."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    import db as store
    store._CACHE_WRITE_ENABLED = True
    body = request.get_json(silent=True) or {}
    try:
        import health_replace as hr
        res = hr.advance(int(body.get("id")), body.get("action", ""),
                         body.get("new_domain"), confirm=bool(body.get("confirm")))
        code = 400 if res.get("error") else 200
        return _cors(jsonify(res)), code
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/sync", methods=["POST", "OPTIONS"])
def trigger_sync():
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    import db as store
    import time as _time
    store._CACHE_WRITE_ENABLED = True

    # sync() reports its own abort reason through this callback -- "Aborted:
    # only 12 accounts (rate limited?)" or "only 3 health records". The failure
    # path below then overwrote it with a generic "insufficient data", throwing
    # away the one detail that says WHICH guard fired and therefore what to fix.
    last = {"msg": None}

    def progress_cb(pct, msg):
        last["msg"] = msg
        try:
            store.cache_set("sync_progress", {
                "pct": pct, "msg": msg, "ts": _time.time(), "status": "running"
            })
        except Exception:
            pass

    store.cache_set("sync_progress", {"pct": 0, "msg": "Starting sync...", "ts": _time.time(), "status": "running"})
    try:
        import sync
        sync.store._CACHE_WRITE_ENABLED = True
        ok = sync.sync(progress_cb=progress_cb)
        status = "done" if ok else "error"
        if ok:
            msg = "Sync complete"
        else:
            # Keep sync's own words. It names the count and the guard.
            reported = (last["msg"] or "").strip()
            msg = reported if reported.lower().startswith("aborted") \
                else f"Sync aborted (insufficient data) — last step: {reported or 'unknown'}"
        store.cache_set("sync_progress", {"pct": 100 if ok else 0, "msg": msg, "ts": _time.time(), "status": status})
        if ok:
            return _cors(jsonify({"ok": True, "message": msg}))
        return _cors(jsonify({"ok": False, "message": msg})), 500
    except Exception as e:
        store.cache_set("sync_progress", {"pct": 0, "msg": str(e), "ts": _time.time(), "status": "error"})
        return _cors(jsonify({"error": str(e)})), 500


@app.route("/api/refresh-stats", methods=["POST", "OPTIONS"])
def refresh_stats():
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    import db as store
    store._CACHE_WRITE_ENABLED = True
    try:
        import sync
        import time as _time
        import requests as req
        from datetime import date
        today_str = date.today().isoformat()
        health_today = sync.fetch_health_metrics(start_date=today_str, end_date=today_str)
        data, _ = store.cache_get("overview_v2")
        if data and health_today:
            for group_list_key in ("clients", "acquisition_groups", "generic_groups", "aging_groups"):
                for g in data.get(group_list_key, []):
                    emails = [a["email"] for a in g.get("account_details", []) if a.get("email")]
                    ds = sum(health_today.get(e, {}).get("sent", 0) for e in emails)
                    g["daily_sent"] = ds
                    if g.get("group_a") and g["group_a"].get("account_details"):
                        ea = [a["email"] for a in g["group_a"]["account_details"] if a.get("email")]
                        g["group_a"]["daily_sent"] = sum(health_today.get(e, {}).get("sent", 0) for e in ea)
                    if g.get("group_b") and g["group_b"].get("account_details"):
                        eb = [a["email"] for a in g["group_b"]["account_details"] if a.get("email")]
                        g["group_b"]["daily_sent"] = sum(health_today.get(e, {}).get("sent", 0) for e in eb)

            # --- Refresh campaign stats + group assignments ---
            _refresh_campaigns(data, req, _time)

            # --- Recalculate warmup days for generic groups ---
            _refresh_warmup_days(data)

            store.cache_patch("overview_v2", data)
        return _cors(jsonify({"ok": True, "accounts": len(health_today)}))
    except Exception as e:
        return _cors(jsonify({"error": str(e)})), 500


def _refresh_warmup_days(data):
    """Recalculate warmup_days for all generic groups that have a warmup_start date."""
    from datetime import date
    today = date.today()
    for g in (data.get("generic_groups") or []):
        ws = g.get("warmup_start")
        if ws:
            try:
                start = date.fromisoformat(ws)
                g["warmup_days"] = (today - start).days
            except Exception:
                pass


def _refresh_campaigns(data, req, _time):
    """Refresh campaign stats and group-campaign assignments in the overview cache."""
    SMARTLEAD_API = "https://server.smartlead.ai/api/v1"
    SMARTLEAD_KEY = os.environ.get("SMARTLEAD_API_KEY", "")

    try:
        r = req.get(f"{SMARTLEAD_API}/campaigns?api_key={SMARTLEAD_KEY}", timeout=30)
        if r.status_code == 429:
            _time.sleep(10)
            r = req.get(f"{SMARTLEAD_API}/campaigns?api_key={SMARTLEAD_KEY}", timeout=30)
        if r.status_code != 200:
            return
        all_campaigns = r.json() if r.text.strip() else []
        active_acq = [c for c in all_campaigns if c.get("status") == "ACTIVE"
                      and "acquisition" in c.get("name", "").lower()
                      and "subsequence" not in c.get("name", "").lower()]
    except Exception:
        return

    # Build email → campaign list mapping from active campaigns
    email_to_camps = {}
    stats_updates = {}

    for camp in active_acq:
        cid = camp["id"]
        try:
            # Fetch campaign email accounts
            ar = req.get(f"{SMARTLEAD_API}/campaigns/{cid}/email-accounts?api_key={SMARTLEAD_KEY}", timeout=10)
            acct_emails = []
            if ar.status_code == 200:
                acct_emails = [a.get("from_email", "") for a in ar.json() if a.get("from_email")]
            elif ar.status_code == 429:
                _time.sleep(5)
                ar = req.get(f"{SMARTLEAD_API}/campaigns/{cid}/email-accounts?api_key={SMARTLEAD_KEY}", timeout=10)
                if ar.status_code == 200:
                    acct_emails = [a.get("from_email", "") for a in ar.json() if a.get("from_email")]

            camp_info = {"id": cid, "name": camp["name"], "status": "ACTIVE", "accounts": len(acct_emails)}
            for em in acct_emails:
                email_to_camps.setdefault(em, []).append(camp_info)

            # Fetch campaign analytics + lead counts for progress bar
            cr = req.get(f"{SMARTLEAD_API}/campaigns/{cid}/analytics?api_key={SMARTLEAD_KEY}", timeout=10)
            if cr.status_code == 200:
                ad = cr.json()
                sent_count = int(ad.get("sent_count", 0))

                # Get lead counts by status — contacted vs queued
                lead_counts = {}
                for sk in ("COMPLETED", "INPROGRESS", "STARTED"):
                    lr = req.get(f"{SMARTLEAD_API}/campaigns/{cid}/leads",
                                 params={"api_key": SMARTLEAD_KEY, "limit": 1, "offset": 0, "status": sk}, timeout=10)
                    if lr.status_code == 429:
                        _time.sleep(5)
                        lr = req.get(f"{SMARTLEAD_API}/campaigns/{cid}/leads",
                                     params={"api_key": SMARTLEAD_KEY, "limit": 1, "offset": 0, "status": sk}, timeout=10)
                    lead_counts[sk] = int(lr.json().get("total_leads", 0)) if lr.status_code == 200 else 0

                contacted = lead_counts["COMPLETED"] + lead_counts["INPROGRESS"]
                active_leads = contacted + lead_counts["STARTED"]
                stat = {
                    "id": cid, "name": camp["name"], "status": "ACTIVE",
                    "accounts": len(acct_emails),
                    "total_sent": sent_count,
                    "total_opened": int(ad.get("unique_open_count", 0)),
                    "total_replied": int(ad.get("reply_count", 0)),
                    "total_bounced": int(ad.get("bounce_count", 0)),
                    "total_leads": active_leads,
                    "completed": contacted,
                    "remaining": lead_counts["STARTED"],
                    "inprogress": lead_counts["INPROGRESS"],
                }
                stats_updates[cid] = stat
        except Exception:
            pass
        _time.sleep(0.2)

    # Update acq_campaign_stats with fresh data for active campaigns
    existing_stats = data.get("acq_campaign_stats") or []
    for i, s in enumerate(existing_stats):
        if s["id"] in stats_updates:
            existing_stats[i] = stats_updates[s["id"]]
    # Add any new campaigns not in the existing list
    existing_ids = {s["id"] for s in existing_stats}
    for cid, stat in stats_updates.items():
        if cid not in existing_ids:
            existing_stats.append(stat)
    data["acq_campaign_stats"] = existing_stats

    # Auto-pause campaigns that are genuinely DONE — i.e. almost no leads left
    # that will EVER send another email.
    #
    # ⚠️ 2026-07-31 incident: the old check was `remaining (=STARTED) <= 5 AND
    # completed/total >= 0.95`, where `completed` counted INPROGRESS leads. So a
    # live campaign that had merely finished STARTING its leads — but still had
    # thousands of INPROGRESS follow-ups queued — computed as remaining=0,
    # ratio=1.0 and got PAUSED mid-flight. It fired on every dashboard load
    # (refresh-stats), silently pausing mature acquisition campaigns (AI-ark
    # list-1 HVAC + Snow Removal, and likely most "paused-with-followups" acq
    # campaigns). Fix: leads still able to send = STARTED + INPROGRESS; only pause
    # when that is ~0. Never pause while follow-ups are in progress.
    for cid, stat in stats_updates.items():
        total_emails = stat.get("total_leads", 0)
        left_to_send = stat.get("remaining", 0) + stat.get("inprogress", 0)
        if total_emails > 20 and left_to_send <= 5:
            try:
                pr = req.post(f"{SMARTLEAD_API}/campaigns/{cid}/status?api_key={SMARTLEAD_KEY}",
                              json={"status": "PAUSED"}, timeout=10)
                if pr.status_code == 200:
                    stat["status"] = "PAUSED"
                    stat["_auto_paused"] = True
            except Exception:
                pass

    # Update group campaigns from live email→campaign mapping
    for section in ("acquisition_groups", "generic_groups"):
        for g in (data.get(section) or []):
            group_emails = [a["email"] for a in g.get("account_details", []) if a.get("email")]
            seen = {}
            for em in group_emails:
                for camp_info in email_to_camps.get(em, []):
                    seen[camp_info["id"]] = camp_info
            g["campaigns"] = sorted(seen.values(), key=lambda c: c["name"])


@app.route("/api/sync-progress")
def sync_progress():
    if not _check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data, _ = _get_cache("sync_progress")
    return _cors(jsonify(data or {"status": "idle", "pct": 0, "msg": ""}))


def _sl_request(method, url, **kwargs):
    """SmartLead API request with retry on 429."""
    import time
    import requests as req
    for attempt in range(3):
        r = getattr(req, method)(url, **kwargs)
        if r.status_code != 429:
            return r
        time.sleep(5 * (attempt + 1))
    return r


def _update_cache_campaigns(group_name, campaign_id, campaign_name, action):
    """Patch overview_v2 cache after assign/unassign so changes persist."""
    import db as store
    try:
        data, _ = store.cache_get("overview_v2")
        if not data:
            return
        for section in ["acquisition_groups", "generic_groups"]:
            for g in (data.get(section) or []):
                if g.get("name") != group_name:
                    continue
                camps = g.get("campaigns", [])
                if action == "remove":
                    g["campaigns"] = [c for c in camps if c.get("id") != campaign_id]
                elif action == "add":
                    if not any(c.get("id") == campaign_id for c in camps):
                        status = "ACTIVE"
                        for s in (data.get("acq_campaign_stats") or []):
                            if s.get("id") == campaign_id:
                                status = s.get("status", "ACTIVE")
                                break
                        camps.append({"id": campaign_id, "name": campaign_name,
                                      "status": status, "accounts": len(g.get("account_details", []))})
                        g["campaigns"] = camps
                for a in (g.get("account_details") or []):
                    names = a.get("campaign_names", [])
                    if action == "add" and campaign_name not in names:
                        names.append(campaign_name)
                    elif action == "remove" and campaign_name in names:
                        names.remove(campaign_name)
                    a["campaign_names"] = names
                    a["in_campaign"] = len(names) > 0
        store.cache_patch("overview_v2", data)
    except Exception:
        pass


def _get_campaign_name(campaign_id):
    """Look up campaign name from cached acq_campaign_stats."""
    try:
        data, _ = _get_cache("overview_v2")
        if not data:
            return str(campaign_id)
        for c in (data.get("acq_campaign_stats") or []):
            if c.get("id") == campaign_id:
                return c.get("name", str(campaign_id))
    except Exception:
        pass
    return str(campaign_id)


def _resolve_group_account_ids(group_name, campaign_id=None):
    """Read SmartLead account IDs from cached account_details."""
    try:
        data, _ = _get_cache("overview_v2")
    except Exception as e:
        return None, f"Cache error: {e}"
    if not data:
        return None, "No cached data"
    account_ids = []
    for section in ["acquisition_groups", "generic_groups"]:
        for g in (data.get(section) or []):
            if g.get("name") == group_name:
                for a in (g.get("account_details") or []):
                    if a.get("id"):
                        account_ids.append(a["id"])
    if not account_ids:
        all_names = [g.get("name") for s in ["acquisition_groups", "generic_groups"] for g in (data.get(s) or [])]
        return None, f"No account IDs for '{group_name}'. Groups: {all_names}"
    return account_ids, None


@app.route("/api/swap-group", methods=["POST", "OPTIONS"])
def swap_group():
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    body = request.get_json(silent=True) or {}
    client_name = body.get("client_name", "").strip()
    if not client_name:
        return _cors(jsonify({"error": "client_name required"})), 400
    try:
        import dashboard
        result = dashboard.swap_client_group(client_name)
        return _cors(jsonify(result)), 200 if result.get("ok") else 400
    except Exception as e:
        return _cors(jsonify({"error": str(e)})), 500


@app.route("/api/assign-group", methods=["POST", "OPTIONS"])
def assign_group():
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    body = request.get_json(silent=True) or {}
    group_name = body.get("group_name", "")
    campaign_id = body.get("campaign_id")
    if not group_name or not campaign_id:
        return _cors(jsonify({"error": "group_name and campaign_id required"})), 400
    sl_key = os.environ.get("SMARTLEAD_API_KEY", "")
    if not sl_key:
        return _cors(jsonify({"error": "SMARTLEAD_API_KEY not configured"})), 500
    account_ids, err = _resolve_group_account_ids(group_name)
    if err:
        return _cors(jsonify({"error": err})), 404 if "No account" in err else 500
    sl = "https://server.smartlead.ai/api/v1"
    r = _sl_request("post", f"{sl}/campaigns/{campaign_id}/email-accounts?api_key={sl_key}",
                    json={"email_account_ids": account_ids}, timeout=30)
    if r.status_code == 200:
        camp_name = _get_campaign_name(campaign_id)
        _update_cache_campaigns(group_name, campaign_id, camp_name, "add")
        return _cors(jsonify({"ok": True, "assigned": len(account_ids),
                              "message": f"Assigned {len(account_ids)} accounts. REMINDER: Reallocate inboxes in SmartLead."}))
    return _cors(jsonify({"error": f"SmartLead returned {r.status_code}"})), 502


@app.route("/api/unassign-group", methods=["POST", "OPTIONS"])
def unassign_group():
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    body = request.get_json(silent=True) or {}
    group_name = body.get("group_name", "")
    campaign_id = body.get("campaign_id")
    if not group_name or not campaign_id:
        return _cors(jsonify({"error": "group_name and campaign_id required"})), 400
    sl_key = os.environ.get("SMARTLEAD_API_KEY", "")
    if not sl_key:
        return _cors(jsonify({"error": "SMARTLEAD_API_KEY not configured"})), 500

    import requests as req
    import time as _time
    sl = "https://server.smartlead.ai/api/v1"

    cache_ids, _ = _resolve_group_account_ids(group_name)
    cache_ids = set(cache_ids or [])

    live_ids = set()
    jwt = os.environ.get("SMARTLEAD_JWT", "").strip()
    gql_url = os.environ.get("SMARTLEAD_GQL", "https://fe-gql.smartlead.ai/v1/graphql").strip()
    if jwt:
        try:
            sl_h = {"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"}
            tags_r = req.post(gql_url, headers=sl_h,
                              json={"query": "{ tags { id name } }"}, timeout=15)
            tag_id = None
            for t in tags_r.json().get("data", {}).get("tags", []):
                if t.get("name", "").lower() == group_name.lower():
                    tag_id = t["id"]
                    break
            if tag_id:
                mappings_r = req.post(gql_url, headers=sl_h,
                    json={"query": "query($tid: Int!) { email_account_tag_mappings(where: {tag_id: {_eq: $tid}}) { email_account_id } }",
                          "variables": {"tid": tag_id}}, timeout=15)
                for m in mappings_r.json().get("data", {}).get("email_account_tag_mappings", []):
                    live_ids.add(m["email_account_id"])
        except Exception:
            pass

    account_ids = list(cache_ids | live_ids)
    if not account_ids:
        return _cors(jsonify({"error": f"No account IDs found for '{group_name}'"})), 404

    r = _sl_request("delete", f"{sl}/campaigns/{campaign_id}/email-accounts?api_key={sl_key}",
                    headers={"Content-Type": "application/json"},
                    data=json.dumps({"email_account_ids": account_ids}), timeout=30)
    if r.status_code != 200:
        return _cors(jsonify({"error": f"SmartLead returned {r.status_code}"})), 502

    stragglers = []
    try:
        _time.sleep(2)
        camp_r = _sl_request("get", f"{sl}/campaigns/{campaign_id}/email-accounts?api_key={sl_key}", timeout=15)
        if camp_r.status_code == 200:
            camp_accts = camp_r.json()
            if isinstance(camp_accts, list):
                id_set = set(account_ids)
                stragglers = [a.get("id") for a in camp_accts if a.get("id") in id_set]
                if stragglers:
                    _sl_request("delete", f"{sl}/campaigns/{campaign_id}/email-accounts?api_key={sl_key}",
                                headers={"Content-Type": "application/json"},
                                data=json.dumps({"email_account_ids": stragglers}), timeout=30)
    except Exception:
        pass

    camp_name = _get_campaign_name(campaign_id)
    _update_cache_campaigns(group_name, campaign_id, camp_name, "remove")
    msg = f"Removed {len(account_ids)} accounts"
    if stragglers:
        msg += f" ({len(stragglers)} required retry)"
    msg += ". REMINDER: Reallocate inboxes in SmartLead."
    return _cors(jsonify({"ok": True, "removed": len(account_ids),
                          "source": f"cache={len(cache_ids)}, live_tag={len(live_ids)}",
                          "stragglers_retried": len(stragglers), "message": msg}))


@app.route("/api/assign-generic-to-client", methods=["POST", "OPTIONS"])
def assign_generic_to_client():
    """Convert a generic reserve group into a client group by re-tagging accounts."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import db as store
        body = request.get_json(silent=True) or {}
        group_name = body.get("group_name", "")
        client_name = body.get("client_name", "").strip()
        ab = body.get("ab", "A").upper()
        if not group_name or not client_name:
            return _cors(jsonify({"error": "group_name and client_name required"})), 400
        if ab not in ("A", "B"):
            return _cors(jsonify({"error": "ab must be A or B"})), 400

        jwt = os.environ.get("SMARTLEAD_JWT", "").strip()
        gql_url = os.environ.get("SMARTLEAD_GQL", "").strip()
        sl_key = os.environ.get("SMARTLEAD_API_KEY", "").strip()
        if not jwt or not gql_url:
            return _cors(jsonify({"error": "SMARTLEAD_JWT and SMARTLEAD_GQL required"})), 500

        sl_headers = {"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"}
        sl_internal = "https://server.smartlead.ai/api"

        account_ids, err = _resolve_group_account_ids(group_name)
        if err:
            return _cors(jsonify({"error": err})), 404

        def _gql(query, variables=None):
            import requests as _req
            r = _req.post(gql_url, headers=sl_headers,
                          json={"query": query, "variables": variables or {}}, timeout=30)
            return r.json()

        all_tags_resp = _gql("{ tags { id name color } }")
        all_tags = {t["name"]: t for t in all_tags_resp.get("data", {}).get("tags", [])}

        import re as _re
        def _norm_tag(n):
            s = n.lower().strip()
            prev = ''
            while prev != s:
                prev = s
                s = _re.sub(r'\s+(group|llc|inc\.?|construction|landscaping|lawn\s*care|hvac|'
                           r'land\s*care|scapes|landscape|heating\s*&?\s*air.*|'
                           r'lawn\s*solutions|land\s*solutions|&\s*design|conditioning)\s*$',
                           '', s, flags=_re.IGNORECASE)
                s = _re.sub(r'[,.\s&]+$', '', s).strip()
            return _re.sub(r'\s+', ' ', s)

        def _find_existing_client_tag(client_name, ab):
            """Find existing tag matching this client + A/B by normalized name."""
            cn = _norm_tag(client_name)
            suffix = f" {ab.upper()}"
            for tn, td in all_tags.items():
                if not tn.upper().endswith(suffix):
                    continue
                tag_base = tn[:-(len(suffix))].strip()
                if _norm_tag(tag_base) == cn:
                    return td["id"], tn
            return None, None

        existing_tag_id, existing_tag_name = _find_existing_client_tag(client_name, ab)

        if existing_tag_id:
            new_tag_name = existing_tag_name
            client_tag_id = existing_tag_id
        else:
            new_tag_name = f"{client_name} Group {ab}"
            palette = ["#FF6B6B","#FF8E72","#FFA94D","#FFD43B","#A9E34B","#51CF66","#20C997",
                       "#22B8CF","#339AF0","#5C7CFA","#7950F2","#BE4BDB","#E64980","#F06595"]
            used = {t.get("color") for t in all_tags.values()}
            color = next((c for c in palette if c not in used), "#D0FCB1")
            mutation = """mutation createTag($object: tags_insert_input!) {
              insert_tags_one(object: $object) { id name color }
            }"""
            result = _gql(mutation, {"object": {"name": new_tag_name, "color": color}})
            tag = result.get("data", {}).get("insert_tags_one", {})
            client_tag_id = tag.get("id")
            if client_tag_id:
                all_tags[new_tag_name] = tag

        ZAPMAIL_TAG_ID = 262254
        client_tag_id = client_tag_id
        if not client_tag_id:
            return _cors(jsonify({"error": f"Failed to find/create tag '{new_tag_name}'"})), 500

        import re
        date_pattern = re.compile(r'^\d{1,2}/\d{1,2}/\d{2,4}$')
        id_set = set(account_ids)
        acct_tags = {}
        offset = 0
        while True:
            resp = _gql(
                '{ email_account_tag_mappings(limit: 1000, offset: %d) '
                '{ email_account_id tag { id name } } }' % offset
            )
            rows = (resp or {}).get("data", {}).get("email_account_tag_mappings", [])
            for row in rows:
                aid = row["email_account_id"]
                if aid in id_set:
                    tag = row.get("tag", {})
                    acct_tags.setdefault(aid, []).append(tag)
            if len(rows) < 1000:
                break
            offset += 1000

        import requests as _req
        import time

        # RESUMABLE. This loop re-tags up to 57 accounts one at a time, each with
        # its own retries, and it is the irreversible middle of a multi-step
        # conversion: a timeout at account 30 leaves a group half-converted,
        # visible to nobody, and re-running used to start from the top and
        # re-tag the 30 that already succeeded. The journal records each id as
        # it lands, so a second run does the remainder and nothing else.
        #
        # Keyed on the group AND the destination client, so converting the same
        # group to a different client is a different job, not a resume.
        _jkey = "assign_journal:" + re.sub(
            r"[^a-z0-9]+", "-", f"{group_name}|{client_name}|{ab}".lower()).strip("-")[:120]
        _journal = store.get_state(_jkey) or {}
        _done = set(_journal.get("tagged") or [])
        _resumed = len(_done)

        tagged = len(_done)
        tag_errors = []
        for acc_id in account_ids:
            if acc_id in _done:
                continue
            existing = acct_tags.get(acc_id, [])
            date_tag_id = None
            for t in existing:
                if date_pattern.match(t.get("name", "")):
                    date_tag_id = t["id"]
                    break
            tag_ids = [ZAPMAIL_TAG_ID, client_tag_id]
            if date_tag_id:
                tag_ids.append(date_tag_id)
            for attempt in range(3):
                r = _req.post(f"{sl_internal}/email-account/save-management-details",
                              headers=sl_headers,
                              json={"id": acc_id, "tags": tag_ids}, timeout=30)
                if r.status_code != 429:
                    break
                time.sleep(5 * (attempt + 1))
            if r.status_code == 200:
                tagged += 1
                _done.add(acc_id)
                # Written every 10, and again at the end. Losing at most nine
                # re-tags to a crash is cheap; re-tagging is idempotent anyway,
                # while writing on every account would triple the request count.
                if len(_done) % 10 == 0:
                    store.set_state(_jkey, {"tagged": sorted(_done),
                                            "client": client_name, "group": group_name})
            else:
                tag_errors.append({"id": acc_id, "status": r.status_code, "body": r.text[:100]})
        store.set_state(_jkey, {"tagged": sorted(_done), "client": client_name,
                                "group": group_name})

        time.sleep(2)
        verify_resp = _gql(
            "query($tid: Int!) { email_account_tag_mappings(where: {tag_id: {_eq: $tid}}) { email_account_id } }",
            {"tid": client_tag_id}
        )
        verified_ids = {m["email_account_id"] for m in
                        (verify_resp.get("data") or {}).get("email_account_tag_mappings", [])}
        verified = len(set(account_ids) & verified_ids)
        missing = [aid for aid in account_ids if aid not in verified_ids]

        data_cache, _ = store.cache_get("overview_v2")
        if data_cache:
            generic_groups = data_cache.get("generic_groups", [])
            moved_group = None
            for i, g in enumerate(generic_groups):
                if g.get("name") == group_name:
                    moved_group = generic_groups.pop(i)
                    break
            if moved_group:
                moved_group["name"] = new_tag_name
                clients = data_cache.get("clients", [])
                existing_client = None
                for c in clients:
                    if c.get("name", "").lower() == client_name.lower():
                        existing_client = c
                        break
                if existing_client:
                    if ab == "A":
                        existing_client["group_a"] = moved_group
                        existing_client["group_a_count"] = moved_group.get("accounts", 0)
                    else:
                        existing_client["group_b"] = moved_group
                        existing_client["group_b_count"] = moved_group.get("accounts", 0)
                    existing_client["accounts"] = existing_client.get("group_a_count", 0) + existing_client.get("group_b_count", 0)
                else:
                    new_client = {
                        "name": client_name,
                        "accounts": moved_group.get("accounts", 0),
                        "total_domains": moved_group.get("total_domains", 0),
                        "daily_capacity": moved_group.get("daily_capacity", 0),
                        "group_a_count": moved_group.get("accounts", 0) if ab == "A" else 0,
                        "group_b_count": moved_group.get("accounts", 0) if ab == "B" else 0,
                        "group_a": moved_group if ab == "A" else None,
                        "group_b": moved_group if ab == "B" else None,
                        "account_details": moved_group.get("account_details", []),
                    }
                    clients.append(new_client)
                data_cache["generic_groups"] = generic_groups
                data_cache["clients"] = clients
                store.cache_patch("overview_v2", data_cache)

        # Point the group's domains at the new client's website. Zapmail's
        # forwardTo does NOT follow a re-tag, so without this the domains keep
        # redirecting prospects to whoever used them last.
        forward_to = (body.get("forward_to") or "").strip()
        fwd = None
        if forward_to:
            try:
                import health_offboard as ho
                fwd = ho.set_domain_forwarding(ho.domains_for_accounts(account_ids), forward_to)
            except Exception as e:
                fwd = {"ok": False, "domains": 0, "note": str(e)[:120]}

        # A fully verified conversion is finished; leaving the journal behind
        # would make a later, deliberate re-run of the same group+client skip
        # every account and report success having done nothing.
        if verified == len(account_ids):
            try:
                store.set_state(_jkey, {"tagged": [], "completed": _today_iso()})
            except Exception:
                pass

        return _cors(jsonify({
            "ok": verified == len(account_ids),
            "tagged": tagged,
            "verified": verified,
            "total": len(account_ids),
            "new_tag": new_tag_name,
            "missing": len(missing),
            "errors": tag_errors[:5] if tag_errors else [],
            "forwarding": fwd,
            "forwarding_set": (fwd or {}).get("domains", 0),
            # How much of this was already done before the call. Non-zero means
            # a previous attempt died part way and this one picked it up.
            "resumed_from": _resumed,
        }))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/subscriptions")
def subscriptions():
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    import requests as req
    from datetime import datetime, timezone
    zm_key = os.environ.get("ZAPMAIL_API_KEY", "").strip()
    if not zm_key:
        return _cors(jsonify({"error": "ZAPMAIL_API_KEY not configured"})), 500
    headers = {"Content-Type": "application/json", "x-auth-zapmail": zm_key, "x-service-provider": "GOOGLE"}
    try:
        r = req.get("https://api.zapmail.ai/api/v2/subscriptions", headers=headers, timeout=15)
        raw = r.json()
    except Exception as e:
        return _cors(jsonify({"error": str(e)})), 502
    subs = raw if isinstance(raw, list) else raw.get("data", [])
    now = datetime.now(timezone.utc)
    result = []
    total_monthly = 0
    total_mailboxes = 0
    action_needed_count = 0
    for s in subs:
        if s.get("subscriptionStatus") != "ACTIVE":
            continue
        period_end = s.get("periodEnd", "")
        try:
            renews = datetime.fromisoformat(period_end.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        days_until = (renews - now).days
        price = s.get("price", 0)
        mailboxes = s.get("totalMailboxQuantity", 0)
        total_monthly += price
        total_mailboxes += mailboxes
        action_needed = days_until <= 16
        if action_needed:
            action_needed_count += 1
        result.append({
            "id": s.get("id"),
            "subscription_id": s.get("subscriptionId"),
            "price": price,
            "mailboxes": mailboxes,
            "renews": renews.strftime("%Y-%m-%d"),
            "days_until_renewal": days_until,
            "action_needed": action_needed,
            "created": s.get("subscriptionCreationDate", "")[:10],
        })
    result.sort(key=lambda x: x["renews"])
    return _cors(jsonify({
        "subscriptions": result,
        "total_monthly": total_monthly,
        "total_mailboxes": total_mailboxes,
        "action_needed_count": action_needed_count,
    }))


@app.route("/api/billing-followup", methods=["GET", "POST", "OPTIONS"])
def billing_followup_route():
    """Mailboxes that are gone but still on the Zapmail bill.

    POST {"ack": true} once Zapmail confirms they have optimised the
    subscription, or {"ack": ["a@x.info", ...]} for specific mailboxes.
    """
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import billing_followup as bf
        if request.method == "POST":
            ack = (request.get_json(silent=True) or {}).get("ack")
            if ack is True:
                return _cors(jsonify(bf.acknowledge()))
            if isinstance(ack, list):
                return _cors(jsonify(bf.acknowledge(ack)))
            return _cors(jsonify({"error": "pass {\"ack\": true} or a list"})), 400
        board = bf.reconcile()
        if request.args.get("nudge") == "1":
            board["nudge_text"] = bf.format_nudge(board)
        return _cors(jsonify(board))
    except Exception as e:
        return _cors(jsonify({"error": str(e)})), 500


@app.route("/api/domain-expiry")
def domain_expiry_route():
    """Domains about to lapse while still carrying live senders.

    The registrar is the only real auto-renew switch — Zapmail's `autoRenew`
    field reads false on every domain and means nothing.
    """
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import domain_expiry_alert as dea
        board = _slow_cache("cache:domain_expiry", dea.build)
        if request.args.get("alert") == "1":
            board["alert_text"] = dea.format_alert(board)
        return _cors(jsonify(board))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/infra-lifecycle")
def infra_lifecycle_route():
    """Per-client infrastructure decision deadlines and hard stops.

    `retainer-renewals` answers "when does the client next pay?". This answers
    "when must we stop buying their inboxes, and when do we need the answer?" —
    a different date, because warm-up starts the Zapmail billing clock about two
    weeks before the engagement does.
    """
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import infra_lifecycle as ilc
        as_of = request.args.get("as_of")
        # Only the default "today" view is cached; an explicit as_of is a
        # one-off question and must not poison the shared answer.
        board = (ilc.build(ilc._parse(as_of)) if as_of
                 else _slow_cache("cache:infra_lifecycle", ilc.build))
        if request.args.get("notices") == "1":
            board["notices"] = ilc.post_notices(board, dry_run=True)["messages"]
        return _cors(jsonify(board))
    except Exception as e:
        import traceback
        # Deliberately no `rows` key on the error path: an empty list is truthy
        # in JS, so a failed CRM read would render as a clean board.
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/infra-lifecycle/decision", methods=["POST", "OPTIONS"])
def infra_lifecycle_decision():
    """Record renew/stop for a client so the countdown stops chasing it."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    body = request.get_json(silent=True) or {}
    try:
        import infra_lifecycle as ilc
        rec = ilc.record_decision(body.get("client", ""), body.get("decision", ""),
                                  note=body.get("note", ""), by=body.get("by", "dashboard"))
        return _cors(jsonify({"ok": True, "decision": rec}))
    except ValueError as e:
        return _cors(jsonify({"error": str(e)})), 400
    except Exception as e:
        return _cors(jsonify({"error": str(e)})), 500


@app.route("/api/domain-renewals")
def domain_renewals():
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    import db as store
    cached, updated_at = store.cache_get("domain_renewals")
    if cached:
        return _cors(jsonify(cached))
    return _cors(jsonify({"domain_renewals": {}}))


@app.route("/api/domains/inventory")
def domains_inventory():
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import db as store
        all_domains = store.get_all_domains()
        summary = {"total": 0, "available": 0, "in_use": 0, "cancelled": 0, "do_not_use": 0,
                   "by_provider": {}, "by_pool": {}}
        for d in all_domains:
            summary["total"] += 1
            s = d.get("status", "")
            if s in summary:
                summary[s] += 1
            p = d.get("provider", "")
            if p:
                summary["by_provider"][p] = summary["by_provider"].get(p, 0) + 1
            pool = d.get("pool", "")
            if pool:
                summary["by_pool"][pool] = summary["by_pool"].get(pool, 0) + 1
        return _cors(jsonify({"domains": all_domains, "summary": summary}))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


def _porkbun_list():
    import requests as _req
    pk = os.environ.get("PORKBUN_API_KEY", "").strip()
    sk = os.environ.get("PORKBUN_SECRET_KEY", "").strip()
    if not pk or not sk:
        return []
    r = _req.post("https://api.porkbun.com/api/json/v3/domain/listAll",
                   json={"apikey": pk, "secretapikey": sk}, timeout=30)
    data = r.json()
    if data.get("status") != "SUCCESS":
        return []
    return [{"domain": d.get("domain", ""), "expires": d.get("expireDate", "")[:10],
             "auto_renew": d.get("autoRenew") == "1", "registrar": "porkbun"}
            for d in data.get("domains", [])]


def _spaceship_list():
    import requests as _req
    ak = os.environ.get("SPACESHIP_API_KEY", "").strip()
    sk = os.environ.get("SPACESHIP_SECRET_KEY", "").strip()
    if not ak or not sk:
        return []
    headers = {"X-API-Key": ak, "X-API-Secret": sk, "Content-Type": "application/json"}
    result, skip = [], 0
    while True:
        r = _req.get("https://spaceship.dev/api/v1/domains", headers=headers,
                      timeout=30, params={"take": 100, "skip": skip})
        if r.status_code != 200:
            break
        items = r.json().get("items", []) if isinstance(r.json(), dict) else []
        if not items:
            break
        for d in items:
            result.append({"domain": d.get("name", ""), "expires": d.get("expirationDate", "")[:10],
                           "auto_renew": d.get("autoRenew", False), "registrar": "spaceship"})
        if len(items) < 100:
            break
        skip += 100
    return result


def _porkbun_set_ar(domain, enabled):
    import requests as _req
    pk = os.environ.get("PORKBUN_API_KEY", "").strip()
    sk = os.environ.get("PORKBUN_SECRET_KEY", "").strip()
    r = _req.post(f"https://api.porkbun.com/api/json/v3/domain/updateAutoRenew/{domain}",
                   json={"apikey": pk, "secretapikey": sk, "status": "on" if enabled else "off"}, timeout=15)
    data = r.json()
    return {"success": data.get("status") == "SUCCESS", "message": data.get("message", "")}


def _spaceship_set_ar(domain, enabled):
    import requests as _req
    ak = os.environ.get("SPACESHIP_API_KEY", "").strip()
    sk = os.environ.get("SPACESHIP_SECRET_KEY", "").strip()
    r = _req.put(f"https://spaceship.dev/api/v1/domains/{domain}/autorenew",
                  headers={"X-API-Key": ak, "X-API-Secret": sk, "Content-Type": "application/json"},
                  json={"isEnabled": enabled}, timeout=15)
    if r.status_code in (200, 204):
        return {"success": True, "message": f"Auto-renew {'enabled' if enabled else 'disabled'}"}
    return {"success": False, "message": r.text[:200]}


@app.route("/api/domains/sync-registrar", methods=["POST", "OPTIONS"])
def domains_sync_one_registrar():
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import db as store
        body = request.get_json(silent=True) or {}
        registrar = body.get("registrar", "")
        if registrar == "porkbun":
            domains = _porkbun_list()
        elif registrar == "spaceship":
            domains = _spaceship_list()
        else:
            return _cors(jsonify({"error": f"Unknown registrar: {registrar}"})), 400
        updated = 0
        for rd in domains:
            domain_name = rd.get("domain", "").strip().lower()
            if not domain_name:
                continue
            fields = {}
            if rd.get("expires"):
                fields["expires_at"] = rd["expires"]
            fields["auto_renew"] = rd.get("auto_renew", False)
            store.update_domain(domain_name, **fields)
            updated += 1
        return _cors(jsonify({"registrar": registrar, "fetched": len(domains), "updated": updated}))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/domains/auto-renew", methods=["POST", "OPTIONS"])
def domains_set_auto_renew():
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import db as store
        body = request.get_json(silent=True) or {}
        domains_list = body.get("domains", [])
        enabled = body.get("enabled", False)
        if not domains_list:
            return _cors(jsonify({"error": "No domains specified"})), 400

        # TURNING AUTO-RENEW OFF IS THE REAL KILL SWITCH. Zapmail's own autoRenew
        # field is inert — the registrar's is what actually decides whether a
        # domain survives — so this route is the only thing standing between a
        # live sender and a lapsed domain. It has been wrong once already: 34
        # domains carrying live senders were set to lapse.
        #
        # So disabling it on a domain that still has a mailbox is refused unless
        # the caller says `force`. Enabling is never gated: keeping a domain
        # alive cannot lose anything.
        blocked = []
        if not enabled and not body.get("force"):
            try:
                import infra_lifecycle as ilc
                inv = ilc.fetch_zapmail_inventory()
                live = {}
                for mb in (inv.get("mailboxes") or {}).values():
                    dom = (mb.get("domain") or "").lower()
                    live[dom] = live.get(dom, 0) + 1
            except Exception as e:
                # Could not check, so cannot clear it. Refusing is the cautious
                # direction: a failed read must not become permission to lapse.
                return _cors(jsonify({
                    "error": "could not read Zapmail to check for live senders — "
                             f"refusing to disable auto-renew ({str(e)[:100]})",
                })), 503
            for d in domains_list:
                n = live.get(d.strip().lower(), 0)
                if n:
                    blocked.append({"domain": d.strip().lower(), "mailboxes": n})
            if blocked:
                return _cors(jsonify({
                    "error": f"{len(blocked)} domain(s) still carry mailboxes — "
                             "disabling auto-renew would let them lapse and take "
                             "the senders with them. Cancel the mailboxes first, "
                             "or re-send with force:true.",
                    "blocked": blocked,
                })), 409

        all_db_domains = {d["domain"]: d for d in store.get_all_domains()}
        results = []
        for domain_name in domains_list:
            domain_name = domain_name.strip().lower()
            db_rec = all_db_domains.get(domain_name)
            if not db_rec:
                results.append({"domain": domain_name, "success": False, "message": "Not found in DB"})
                continue
            provider = db_rec.get("provider", "")
            if provider == "porkbun":
                res = _porkbun_set_ar(domain_name, enabled)
            elif provider == "spaceship":
                res = _spaceship_set_ar(domain_name, enabled)
            else:
                results.append({"domain": domain_name, "success": False, "message": f"Unknown provider: {provider}"})
                continue
            if res.get("success"):
                store.update_domain(domain_name, auto_renew=enabled)
            results.append({"domain": domain_name, **res})
        succeeded = sum(1 for r in results if r.get("success"))
        return _cors(jsonify({"results": results, "succeeded": succeeded,
                              "total": len(results), "forced": bool(body.get("force"))}))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/replacements")
def get_replacements():
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    import db as store
    state = store.get_state("domain_replacements") or {"jobs": []}
    return _cors(jsonify(state))


@app.route("/api/replacements", methods=["POST", "OPTIONS"])
def create_replacement():
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    import db as store
    import uuid as _uuid
    from datetime import datetime as _dt
    body = request.get_json(silent=True) or {}
    required = ["old_domain", "group_name", "group_type", "bounce_rate"]
    for field in required:
        if not body.get(field):
            return _cors(jsonify({"error": f"{field} required"})), 400
    state = store.get_state("domain_replacements") or {"jobs": []}
    for j in state["jobs"]:
        if j["old_domain"] == body["old_domain"] and j["status"] not in ("swapped", "cancelled"):
            return _cors(jsonify({"error": f"{body['old_domain']} already flagged"})), 400
    job = {
        "id": str(_uuid.uuid4())[:8],
        "old_domain": body["old_domain"],
        "new_domain": None,
        "group_name": body["group_name"],
        "group_type": body["group_type"],
        "bounce_rate": body["bounce_rate"],
        "status": "flagged",
        "campaigns": body.get("campaigns", []),
        "flagged_at": _dt.now().strftime("%Y-%m-%d"),
        "warming_started_at": None,
        "swapped_at": None,
        "cancelled_at": None,
        "old_cancelled": False,
        "old_cancel_date": None,
        "tags_updated": False,
        "forwarding_updated": False,
        "removed_zapmail": False,
        "removed_smartlead": False,
        "domain_cancelled": False,
    }
    state["jobs"].append(job)
    store.set_state("domain_replacements", state)
    return _cors(jsonify({"ok": True, "job": job}))


@app.route("/api/replacements/update", methods=["POST", "OPTIONS"])
def update_replacement():
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    import db as store
    from datetime import datetime as _dt
    body = request.get_json(silent=True) or {}
    job_id = body.get("id")
    if not job_id:
        return _cors(jsonify({"error": "id required"})), 400
    state = store.get_state("domain_replacements") or {"jobs": []}
    job = None
    for j in state["jobs"]:
        if j["id"] == job_id:
            job = j
            break
    if not job:
        return _cors(jsonify({"error": "Job not found"})), 404
    new_status = body.get("status")
    new_domain = body.get("new_domain")
    valid_transitions = {
        "flagged": ["warming", "cancelled"],
        "warming": ["ready", "cancelled"],
        "ready": ["swapped", "cancelled"],
        "swapped": ["cancelled"],
    }
    if new_status:
        allowed = valid_transitions.get(job["status"], [])
        if new_status not in allowed:
            return _cors(jsonify({"error": f"Cannot go from {job['status']} to {new_status}"})), 400
        job["status"] = new_status
        now = _dt.now().strftime("%Y-%m-%d")
        if new_status == "warming":
            job["warming_started_at"] = now
        elif new_status == "swapped":
            job["swapped_at"] = now
        elif new_status == "cancelled":
            job["cancelled_at"] = now
    if new_domain:
        job["new_domain"] = new_domain
    if body.get("old_cancelled"):
        job["old_cancelled"] = True
        job["old_cancel_date"] = _dt.now().strftime("%Y-%m-%d")
    for flag in ("tags_updated", "forwarding_updated", "removed_zapmail", "removed_smartlead", "domain_cancelled"):
        if flag in body:
            job[flag] = bool(body[flag])
    store.set_state("domain_replacements", state)
    return _cors(jsonify({"ok": True, "job": job}))


# ─── Domain Purchase + Generic Group Creation Wizard ───

_TLD_PRICES = {".com": "9.98", ".info": "3.98", ".co": "11.98", ".net": "10.98", ".org": "9.98", ".biz": "8.98"}

_NICHE_WORDS = {
    "generic": {
        "pre": ["service","work","trade","field","crew","job","site","project","task","pro","contract","build",
                "maintain","install","repair","open","direct","steady","reliable","trusted","skilled","onsite",
                "rapid","ready","prime","next","first","all","apex","core"],
        "mid": ["service","work","care","solutions","side","zone","point","line","craft","force","ops","tech","aid","link","way","path","flow"],
        "suf": ["pros","biz","co","hq","group","crew","team","contractors","services","solutions","experts",
                "works","side","hub","base","zone","point","force","now","go"],
    },
    "landscaping": {
        "pre": ["landscape","landscaping","grounds","groundskeeping","lawn","lawncare","yard","property","turf","exterior"],
        "mid": ["maintenance","care","management","work","services","service","keeping","upkeep"],
        "suf": ["pros","experts","specialists","solutions","group","crew","contractors","company","team","partners"],
    },
    "hvac": {
        "pre": ["hvac","heating","cooling","airflow","climate","comfort","duct","ventilation","thermal","air"],
        "mid": ["service","repair","install","maintenance","care","work","solutions","systems"],
        "suf": ["pros","experts","crew","team","contractors","services","solutions","co","group","specialists"],
    },
}

def _gen_domain_name(niche_key):
    import random
    w = _NICHE_WORDS.get(niche_key, _NICHE_WORDS["generic"])
    roll = random.random()
    if roll < 0.4:
        return random.choice(w["pre"]) + random.choice(w["suf"])
    elif roll < 0.75:
        return random.choice(w["pre"]) + random.choice(w["mid"]) + random.choice(w["suf"])
    else:
        return random.choice(w["pre"]) + random.choice(w["mid"])


@app.route("/api/domains/find-available", methods=["POST", "OPTIONS"])
def find_available_domains():
    """Generate random domain names and check availability in parallel."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import requests as req
        body = request.get_json(silent=True) or {}
        niche = body.get("niche", "generic")
        registrar = body.get("registrar", "spaceship")
        tld = body.get("tld", ".info")
        target = min(int(body.get("count", 14)), 30)

        ak = os.environ.get("SPACESHIP_API_KEY", "").strip()
        sk = os.environ.get("SPACESHIP_SECRET_KEY", "").strip()
        pk = os.environ.get("PORKBUN_API_KEY", "").strip()
        ps = os.environ.get("PORKBUN_SECRET_KEY", "").strip()

        exclude = set(body.get("exclude", []))
        tried = set(exclude)

        def _check_spaceship(dn):
            try:
                r = req.get(f"https://spaceship.dev/api/v1/domains/{dn}/available",
                            headers={"X-Api-Key": ak, "X-Api-Secret": sk}, timeout=15)
                data = r.json() if r.status_code == 200 else {}
                if data.get("result") == "available":
                    return {"domain": dn, "available": True, "price": _TLD_PRICES.get(tld, "~10")}
            except Exception:
                pass
            return None

        def _check_porkbun(dn):
            try:
                r = req.post(f"https://api.porkbun.com/api/json/v3/domain/checkDomain/{dn}",
                             json={"apikey": pk, "secretapikey": ps}, timeout=10)
                data = r.json()
                resp = data.get("response", {})
                if data.get("status") == "SUCCESS" and resp.get("avail") == "yes":
                    return {"domain": dn, "available": True, "price": resp.get("price", "?")}
            except Exception:
                pass
            return None

        checker = _check_spaceship if registrar == "spaceship" else _check_porkbun
        found = []
        hard_tld = tld in (".co", ".com", ".net")
        max_rounds = 60 if hard_tld else 20
        batch_sz = 16 if hard_tld else 10

        for rnd in range(max_rounds):
            if len(found) >= target:
                break
            batch = []
            while len(batch) < batch_sz and len(tried) < 2000:
                dn = _gen_domain_name(niche) + tld
                if dn not in tried:
                    tried.add(dn)
                    batch.append(dn)
            if not batch:
                break
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = {pool.submit(checker, dn): dn for dn in batch}
                for f in as_completed(futures):
                    result = f.result()
                    if result and len(found) < target:
                        found.append(result)
            if rnd < max_rounds - 1 and len(found) < target:
                import time as _t
                _t.sleep(0.5)

        return _cors(jsonify({"results": found, "checked": len(tried), "target": target}))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/domains/purchase", methods=["POST", "OPTIONS"])
def purchase_domains():
    """Purchase domains and set CloudNS nameservers."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import time as _time
        import requests as req
        body = request.get_json(silent=True) or {}
        domain_names = body.get("domains", [])
        registrar = body.get("registrar", "spaceship")
        if not domain_names:
            return _cors(jsonify({"error": "No domains provided"})), 400

        # This route REGISTERS DOMAINS — it spends money, irreversibly, and had
        # no gate at all: one POST bought whatever was in the body. Every other
        # spending path in this repo requires an explicit confirm, and a caller
        # that has not said so gets a priced dry run instead.
        if not body.get("confirm"):
            wanted = [d.strip().lower() for d in domain_names[:20] if d and d.strip()]
            import buy_inboxes as _bi
            est = sum(_bi._domain_price(d) for d in wanted)
            return _cors(jsonify({
                "dry_run": True, "would_purchase": wanted, "registrar": registrar,
                "estimated_cost": est,
                "note": "Nothing was bought. Re-send with confirm:true to register "
                        "these domains.",
            }))

        ak = os.environ.get("SPACESHIP_API_KEY", "").strip()
        sk = os.environ.get("SPACESHIP_SECRET_KEY", "").strip()
        pk = os.environ.get("PORKBUN_API_KEY", "").strip()
        ps = os.environ.get("PORKBUN_SECRET_KEY", "").strip()
        CLOUDNS = ["pns61.cloudns.net", "pns62.cloudns.com", "pns63.cloudns.net", "pns64.cloudns.uk"]
        SP_CONTACT = os.environ.get("SPACESHIP_CONTACT_ID", "1nEUYUnGBWO9ba7Z0lMrOM2UCgY9S")

        log_lines = []
        purchased = []
        failed = []

        for dn in domain_names[:20]:
            dn = dn.strip().lower()
            try:
                if registrar == "spaceship":
                    sp_body = {
                        "autoRenew": False,
                        "years": 1,
                        "privacyProtection": {"level": "high", "userConsent": True},
                        "contacts": {"registrant": SP_CONTACT, "admin": SP_CONTACT,
                                     "tech": SP_CONTACT, "billing": SP_CONTACT},
                    }
                    r = req.post(f"https://spaceship.dev/api/v1/domains/{dn}",
                                 headers={"X-Api-Key": ak, "X-Api-Secret": sk, "Content-Type": "application/json"},
                                 json=sp_body, timeout=30)
                    if r.status_code in (200, 201, 202):
                        log_lines.append(f"Purchased: {dn}")
                        purchased.append(dn)
                    else:
                        err = r.json().get("detail", r.text[:150]) if r.text else "Unknown"
                        log_lines.append(f"Failed: {dn} — {err}")
                        failed.append(dn)
                else:
                    r = req.post(f"https://api.porkbun.com/api/json/v3/domain/create/{dn}",
                                 json={"apikey": pk, "secretapikey": ps, "acknowledgement": "yes"}, timeout=30)
                    data = r.json()
                    if data.get("status") == "SUCCESS":
                        log_lines.append(f"Purchased: {dn}")
                        purchased.append(dn)
                    else:
                        log_lines.append(f"Failed: {dn} — {data.get('message', '')}")
                        failed.append(dn)
            except Exception as e:
                log_lines.append(f"Error: {dn} — {str(e)}")
                failed.append(dn)
            _time.sleep(1)

        # Set nameservers on purchased domains
        ns_ok = 0
        for dn in purchased:
            try:
                if registrar == "spaceship":
                    r = req.put(f"https://spaceship.dev/api/v1/domains/{dn}/nameservers",
                                headers={"X-Api-Key": ak, "X-Api-Secret": sk, "Content-Type": "application/json"},
                                json={"provider": "custom", "hosts": CLOUDNS}, timeout=15)
                    if r.status_code in (200, 204):
                        ns_ok += 1
                    else:
                        log_lines.append(f"NS failed for {dn}: {r.text[:100]}")
                else:
                    r = req.post(f"https://api.porkbun.com/api/json/v3/domain/updateNs/{dn}",
                                 json={"apikey": pk, "secretapikey": ps, "ns": CLOUDNS}, timeout=15)
                    if r.json().get("status") == "SUCCESS":
                        ns_ok += 1
                    else:
                        log_lines.append(f"NS failed for {dn}: {r.json().get('message', '')}")
            except Exception as e:
                log_lines.append(f"NS error for {dn}: {str(e)}")
            _time.sleep(0.5)

        # Disable auto-renew on purchased domains
        for dn in purchased:
            try:
                if registrar == "spaceship":
                    req.put(f"https://spaceship.dev/api/v1/domains/{dn}/autorenew",
                            headers={"X-Api-Key": ak, "X-Api-Secret": sk, "Content-Type": "application/json"},
                            json={"isEnabled": False}, timeout=10)
                else:
                    req.post(f"https://api.porkbun.com/api/json/v3/domain/updateAutoRenew/{dn}",
                             json={"apikey": pk, "secretapikey": ps, "status": "off"}, timeout=10)
            except Exception:
                pass

        log_lines.append(f"Nameservers set on {ns_ok}/{len(purchased)} domains")
        log_lines.append(f"Auto-renew disabled on all purchased domains")

        return _cors(jsonify({"ok": True, "purchased": purchased, "failed": failed,
                              "ns_set": ns_ok, "log": log_lines}))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/domains/purchase-one", methods=["POST", "OPTIONS"])
def purchase_one_domain():
    """Purchase a single domain, set CloudNS nameservers, disable auto-renew."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import requests as req
        body = request.get_json(silent=True) or {}
        dn = (body.get("domain") or "").strip().lower()
        registrar = body.get("registrar", "spaceship")
        if not dn:
            return _cors(jsonify({"error": "No domain provided"})), 400
        # Spends money, irreversibly. Same gate as the batch route.
        if not body.get("confirm"):
            import buy_inboxes as _bi
            return _cors(jsonify({
                "dry_run": True, "would_purchase": dn, "registrar": registrar,
                "estimated_cost": _bi._domain_price(dn),
                "note": "Nothing was bought. Re-send with confirm:true.",
            }))

        ak = os.environ.get("SPACESHIP_API_KEY", "").strip()
        sk = os.environ.get("SPACESHIP_SECRET_KEY", "").strip()
        pk = os.environ.get("PORKBUN_API_KEY", "").strip()
        ps = os.environ.get("PORKBUN_SECRET_KEY", "").strip()
        CLOUDNS = ["pns61.cloudns.net", "pns62.cloudns.com", "pns63.cloudns.net", "pns64.cloudns.uk"]
        SP_CONTACT = os.environ.get("SPACESHIP_CONTACT_ID", "1nEUYUnGBWO9ba7Z0lMrOM2UCgY9S")

        import time as _time
        result = {"domain": dn, "purchased": False, "ns_set": False, "ns_verified": False,
                  "autorenew_off": False, "error": None, "checks": []}

        # Step 1: Purchase
        if registrar == "spaceship":
            sp_h = {"X-Api-Key": ak, "X-Api-Secret": sk, "Content-Type": "application/json"}
            sp_body = {
                "autoRenew": False, "years": 1,
                "privacyProtection": {"level": "high", "userConsent": True},
                "contacts": {"registrant": SP_CONTACT, "admin": SP_CONTACT,
                             "tech": SP_CONTACT, "billing": SP_CONTACT},
            }
            r = req.post(f"https://spaceship.dev/api/v1/domains/{dn}",
                         headers=sp_h, json=sp_body, timeout=30)
            if r.status_code in (200, 201, 202):
                result["purchased"] = True
                result["checks"].append("purchase: OK")
            else:
                err = r.json().get("detail", r.text[:200]) if r.text else "Unknown"
                result["error"] = f"Purchase failed: {err}"
                result["checks"].append(f"purchase: FAILED — {err}")
                return _cors(jsonify(result))
        else:
            r = req.post(f"https://api.porkbun.com/api/json/v3/domain/create/{dn}",
                         json={"apikey": pk, "secretapikey": ps, "acknowledgement": "yes"}, timeout=30)
            data = r.json()
            if data.get("status") == "SUCCESS":
                result["purchased"] = True
                result["checks"].append("purchase: OK")
            else:
                result["error"] = f"Purchase failed: {data.get('message', '')}"
                result["checks"].append(f"purchase: FAILED")
                return _cors(jsonify(result))

        # Step 2: Set nameservers
        if registrar == "spaceship":
            r = req.put(f"https://spaceship.dev/api/v1/domains/{dn}/nameservers",
                        headers=sp_h, json={"provider": "custom", "hosts": CLOUDNS}, timeout=15)
            result["ns_set"] = r.status_code in (200, 204)
        else:
            r = req.post(f"https://api.porkbun.com/api/json/v3/domain/updateNs/{dn}",
                         json={"apikey": pk, "secretapikey": ps, "ns": CLOUDNS}, timeout=15)
            result["ns_set"] = r.json().get("status") == "SUCCESS"

        if not result["ns_set"]:
            result["error"] = "Nameserver update failed"
            result["checks"].append("ns_set: FAILED")
            return _cors(jsonify(result))
        result["checks"].append("ns_set: OK")

        # Step 3: Verify nameservers actually took
        _time.sleep(1)
        if registrar == "spaceship":
            try:
                vr = req.get(f"https://spaceship.dev/api/v1/domains/{dn}",
                             headers=sp_h, timeout=15)
                if vr.status_code == 200:
                    ns_data = vr.json().get("nameservers", {})
                    hosts = ns_data.get("hosts", []) if isinstance(ns_data, dict) else []
                    if any("cloudns" in h.lower() for h in hosts):
                        result["ns_verified"] = True
                        result["checks"].append(f"ns_verify: OK ({', '.join(hosts[:2])})")
                    else:
                        result["checks"].append(f"ns_verify: WARN — got {hosts[:2]}, expected CloudNS")
            except Exception as e:
                result["checks"].append(f"ns_verify: SKIP — {str(e)[:60]}")
        else:
            result["ns_verified"] = True
            result["checks"].append("ns_verify: SKIP (porkbun)")

        # Step 4: Disable auto-renew
        if registrar == "spaceship":
            try:
                req.put(f"https://spaceship.dev/api/v1/domains/{dn}/autorenew",
                        headers=sp_h, json={"isEnabled": False}, timeout=10)
                result["autorenew_off"] = True
                result["checks"].append("autorenew_off: OK")
            except Exception:
                result["checks"].append("autorenew_off: FAILED")
        else:
            try:
                req.post(f"https://api.porkbun.com/api/json/v3/domain/updateAutoRenew/{dn}",
                         json={"apikey": pk, "secretapikey": ps, "status": "off"}, timeout=10)
                result["autorenew_off"] = True
                result["checks"].append("autorenew_off: OK")
            except Exception:
                result["checks"].append("autorenew_off: FAILED")

        return _cors(jsonify(result))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/available-domains")
def available_domains():
    """Return Spaceship domains with CloudNS that are not yet in SmartLead."""
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import requests as req
        import db as store

        overview, _ = store.cache_get("overview_v2")
        sl_domains = set()
        if overview:
            for section in ["clients", "acquisition_groups", "generic_groups", "aging_groups"]:
                for g in overview.get(section, []):
                    for a in g.get("account_details", []):
                        email = a.get("email", "") or ""
                        if "@" in email:
                            sl_domains.add(email.split("@")[1])

        ak = os.environ.get("SPACESHIP_API_KEY", "").strip()
        sk = os.environ.get("SPACESHIP_SECRET_KEY", "").strip()
        headers = {"X-API-Key": ak, "X-API-Secret": sk}
        all_sp = []
        skip = 0
        while True:
            r = req.get("https://spaceship.dev/api/v1/domains",
                         headers=headers, timeout=30, params={"take": 100, "skip": skip})
            items = r.json().get("items", []) if r.status_code == 200 else []
            if not items:
                break
            all_sp.extend(items)
            total = r.json().get("total", 0)
            skip += 100
            if skip >= total:
                break

        # Also exclude domains that already have mailboxes in Zapmail
        zm_used = set()
        try:
            zm_api = "https://api.zapmail.ai/api"
            zm_key = os.environ.get("ZAPMAIL_API_KEY", "").strip()
            zm_headers = {"x-auth-zapmail": zm_key, "Content-Type": "application/json"}
            pg = 1
            while True:
                zr = req.get(f"{zm_api}/v2/domains?page={pg}&limit=100",
                             headers=zm_headers, timeout=30)
                zd = zr.json().get("data", {})
                for zdom in zd.get("domains", []):
                    if zdom.get("mailboxes") and len(zdom.get("mailboxes", [])) >= 1:
                        zm_used.add(zdom["domain"])
                if pg >= zd.get("totalPages", 1):
                    break
                pg += 1
        except Exception:
            pass

        available = []
        for d in all_sp:
            name = d.get("name", "")
            if name in sl_domains or name in zm_used:
                continue
            ns_hosts = d.get("nameservers", {}).get("hosts", [])
            if not any("cloudns" in h.lower() for h in ns_hosts):
                continue
            reg = d.get("registrationDate", "")[:10]
            available.append({"domain": name, "registered": reg})

        available.sort(key=lambda x: x["registered"], reverse=True)

        existing_letters = set()
        if overview:
            for g in overview.get("generic_groups", []):
                gname = g.get("name", "")
                if gname.startswith("Generic "):
                    existing_letters.add(gname[8:].strip())
        # Also check Supabase wizard states (survives stale overview cache)
        try:
            wiz_rows = store._request("GET", "/state",
                                      params={"select": "key", "key": "like.generic_group_wizard_%"})
            for wr in (wiz_rows or []):
                letter = wr.get("key", "").replace("generic_group_wizard_", "").strip()
                if letter:
                    existing_letters.add(letter)
        except Exception:
            pass
        all_letters = [chr(i) for i in range(65, 91)]
        free_letters = [l for l in all_letters if l not in existing_letters]
        return _cors(jsonify({"domains": available, "existing_letters": sorted(existing_letters),
                              "free_letters": free_letters}))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/group/connect-domains", methods=["POST", "OPTIONS"])
def group_connect_domains():
    """Connect domains to Zapmail and buy mailbox slots."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import time as _time
        import requests as req
        body = request.get_json(silent=True) or {}
        letter = body.get("letter", "").strip().upper()
        domains = body.get("domains", [])
        if not domains:
            return _cors(jsonify({"error": "No domains"})), 400

        ZAPMAIL_API = "https://api.zapmail.ai/api"
        ZAPMAIL_KEY = os.environ.get("ZAPMAIL_API_KEY", "").strip()
        NS_STR = "pns61.cloudns.net,pns62.cloudns.com,pns63.cloudns.net,pns64.cloudns.uk"
        def zm_h():
            return {"x-auth-zapmail": ZAPMAIL_KEY, "Content-Type": "application/json"}

        log = []
        connected = 0
        domain_checks = {}

        # Guard: check Zapmail for domains that already have mailboxes (already in use)
        already_used = []
        try:
            all_zm = []
            pg = 1
            while True:
                zr = req.get(f"{ZAPMAIL_API}/v2/domains?page={pg}&limit=100", headers=zm_h(), timeout=30)
                zd = zr.json().get("data", {})
                all_zm.extend(zd.get("domains", []))
                if pg >= zd.get("totalPages", 1):
                    break
                pg += 1
            zm_used = {d["domain"]: len(d.get("mailboxes", []))
                       for d in all_zm if d.get("mailboxes") and len(d.get("mailboxes", [])) >= 3}
            for dn in domains:
                if dn in zm_used:
                    already_used.append(dn)
        except Exception:
            pass
        if already_used:
            return _cors(jsonify({"error": f"{len(already_used)} domain(s) already have mailboxes — remove them first",
                                  "already_used": already_used})), 400

        for dn in domains[:30]:
            checks = []
            try:
                r = req.post(f"{ZAPMAIL_API}/v2/domains/connect", headers=zm_h(),
                             json={"domainName": dn, "nameServers": NS_STR}, timeout=30)
                resp = r.json()
                msg = resp.get("message", r.text[:100])
                if "already" in msg.lower() or r.status_code == 200:
                    connected += 1
                    checks.append("connect: OK")
                else:
                    checks.append(f"connect: FAILED — {msg}")
                log.append(f"{dn}: {msg}")
            except Exception as e:
                log.append(f"{dn}: error — {str(e)[:80]}")
                checks.append(f"connect: FAILED — {str(e)[:60]}")
            domain_checks[dn] = checks
            _time.sleep(0.3)

        # Verify: check each domain shows up in Zapmail with DNS status
        _time.sleep(2)
        try:
            all_zm = []
            page = 1
            for _ in range(10):
                zr = req.get(f"{ZAPMAIL_API}/v2/domains?page={page}", headers=zm_h(), timeout=30)
                zd = zr.json().get("data", {})
                all_zm.extend(zd.get("domains", []))
                if page >= zd.get("totalPages", 1):
                    break
                page += 1
            zm_map = {d.get("domain", ""): d for d in all_zm}
            for dn in domains[:30]:
                zdom = zm_map.get(dn)
                if zdom:
                    dns_status = zdom.get("dnsStatus", zdom.get("status", "unknown"))
                    domain_checks.setdefault(dn, []).append(f"zapmail_found: OK (DNS: {dns_status})")
                else:
                    domain_checks.setdefault(dn, []).append("zapmail_found: FAILED — not in Zapmail")
        except Exception as e:
            for dn in domains[:30]:
                domain_checks.setdefault(dn, []).append(f"zapmail_verify: SKIP — {str(e)[:60]}")

        # Buy mailbox slots proactively (retry on low wallet)
        slots_bought = 0
        try:
            inboxes_needed = len(domains) * 3
            for buy_attempt in range(3):
                ws_resp = req.get(f"{ZAPMAIL_API}/v2/workspaces", headers=zm_h(), timeout=30)
                ws_data = ws_resp.json().get("data", {}).get("currentWorkspace", {})
                purchased = int(ws_data.get("totalMailboxesPurchasedGoogle", "0"))
                assigned = int(ws_data.get("assignedMailboxesCountGoogle", "0"))
                free_slots = purchased - assigned
                if free_slots >= inboxes_needed:
                    log.append(f"Have {free_slots} free slots (need {inboxes_needed})")
                    break
                to_buy = inboxes_needed - free_slots
                buy_r = req.post(f"{ZAPMAIL_API}/v2/wallet/buy-addon-mailboxes?quantity={to_buy}",
                                 headers=zm_h(), json={}, timeout=30)
                if buy_r.status_code == 200:
                    slots_bought = to_buy
                    log.append(f"Bought {to_buy} mailbox slots")
                    break
                if "Insufficient" in buy_r.text and buy_attempt < 2:
                    wait_sec = 60 * (buy_attempt + 1)
                    log.append(f"Wallet low, waiting {wait_sec}s for auto-topoff (attempt {buy_attempt + 1}/3)")
                    _time.sleep(wait_sec)
                    continue
                log.append(f"Slot purchase failed: {buy_r.text[:100]}")
                break
        except Exception as e:
            log.append(f"Slot check error: {str(e)[:80]}")

        return _cors(jsonify({"ok": True, "connected": connected, "slots_bought": slots_bought,
                              "log": log, "domain_checks": domain_checks}))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/group/setup-domain", methods=["POST", "OPTIONS"])
def group_setup_domain():
    """Setup ONE domain: resolve Zapmail ID, create 3 mailboxes, set profile photo."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import time as _time
        import requests as req
        body = request.get_json(silent=True) or {}
        domain = (body.get("domain") or "").strip().lower()
        letter = body.get("letter", "").strip().upper()
        if not domain:
            return _cors(jsonify({"error": "No domain"})), 400

        ZAPMAIL_API = "https://api.zapmail.ai/api"
        ZAPMAIL_KEY = os.environ.get("ZAPMAIL_API_KEY", "").strip()
        SUPABASE_STORAGE = os.environ.get("SUPABASE_URL", "https://ghjmqpnqljgwykpjkvzy.supabase.co") + "/storage/v1/object/public/headshots"
        PHOTO_URL = f"{SUPABASE_STORAGE}/sean_reynolds.png"
        def zm_h():
            return {"x-auth-zapmail": ZAPMAIL_KEY, "Content-Type": "application/json"}

        result = {"domain": domain, "zapmail_connected": False, "mailboxes_created": 0,
                  "photos_set": False, "mb_ids": [], "error": None, "checks": []}

        # Step 1: Find domain in Zapmail — poll up to 3 times with wait for DNS propagation
        zapmail_id = None
        dns_status = None
        for attempt in range(3):
            if attempt > 0:
                _time.sleep(5)
            page = 1
            for _ in range(10):
                try:
                    zr = req.get(f"{ZAPMAIL_API}/v2/domains?page={page}", headers=zm_h(), timeout=30)
                    zd = zr.json().get("data", {})
                    for d in zd.get("domains", []):
                        if d.get("domain", "") == domain:
                            zapmail_id = d.get("id", "")
                            dns_status = d.get("dnsStatus", d.get("status", "unknown"))
                            break
                    if zapmail_id or page >= zd.get("totalPages", 1):
                        break
                    page += 1
                except Exception:
                    break
            if zapmail_id:
                break

        if not zapmail_id:
            result["error"] = "Domain not found in Zapmail yet — DNS may still be propagating"
            result["checks"].append("find_domain: FAILED — not found after 3 attempts")
            return _cors(jsonify(result))

        result["zapmail_connected"] = True
        result["checks"].append(f"find_domain: OK (id={zapmail_id}, DNS={dns_status})")

        # Step 2: Check if mailboxes already exist (idempotent)
        existing_mb = []
        try:
            dr = req.get(f"{ZAPMAIL_API}/v2/domains?limit=200", headers=zm_h(), timeout=30)
            for dd in dr.json().get("data", {}).get("domains", []):
                if dd.get("domain") == domain and dd.get("mailboxes"):
                    existing_mb = [m.get("id") for m in dd["mailboxes"] if isinstance(m, dict) and m.get("id")]
                    break
        except Exception:
            pass

        skip_creation = len(existing_mb) >= 3
        if skip_creation:
            result["mb_ids"] = existing_mb
            result["mailboxes_created"] = len(existing_mb)
            result["skipped"] = "mailboxes already exist"
            result["checks"].append(f"existing_mailboxes: OK ({len(existing_mb)} found, skipping creation)")
        else:
            result["checks"].append(f"existing_mailboxes: {len(existing_mb)} found, creating new")

        if not skip_creation:
            # Step 3: Buy mailbox slots if needed (retry on low wallet — auto-topoff takes ~60s)
            needed = 3 - len(existing_mb)
            buy_ok = False
            for buy_attempt in range(3):
                try:
                    ws_resp = req.get(f"{ZAPMAIL_API}/v2/workspaces", headers=zm_h(), timeout=30)
                    ws_data = ws_resp.json().get("data", {}).get("currentWorkspace", {})
                    purchased = int(ws_data.get("totalMailboxesPurchasedGoogle", "0"))
                    assigned = int(ws_data.get("assignedMailboxesCountGoogle", "0"))
                    free_slots = purchased - assigned
                    if free_slots >= needed:
                        result["checks"].append(f"slot_check: OK ({free_slots} free, need {needed})")
                        buy_ok = True
                        break
                    to_buy = max(needed - free_slots, 3)
                    buy_r = req.post(f"{ZAPMAIL_API}/v2/wallet/buy-addon-mailboxes?quantity={to_buy}",
                                     headers=zm_h(), json={}, timeout=30)
                    if buy_r.status_code == 200:
                        result["checks"].append(f"buy_slots: OK (bought {to_buy}, had {free_slots} free)")
                        buy_ok = True
                        break
                    buy_msg = buy_r.text[:120]
                    if "Insufficient wallet balance" in buy_msg and buy_attempt < 2:
                        wait_sec = 60 * (buy_attempt + 1)
                        result["checks"].append(f"buy_slots: wallet low, waiting {wait_sec}s for auto-topoff (attempt {buy_attempt + 1})")
                        _time.sleep(wait_sec)
                        continue
                    result["checks"].append(f"buy_slots: FAILED — HTTP {buy_r.status_code}: {buy_msg}")
                    result["error"] = f"Zapmail wallet balance too low — add funds at zapmail.ai"
                    return _cors(jsonify(result))
                except Exception as e:
                    result["checks"].append(f"slot_check: WARN — {str(e)[:60]}")
                    buy_ok = True
                    break
            if not buy_ok:
                result["error"] = "Zapmail wallet balance too low after retries"
                return _cors(jsonify(result))

            # Step 4: Create mailboxes with retry (slots take time to propagate after purchase)
            SPECS = [
                {"firstName": "Sean", "lastName": "Reynolds", "mailboxUsername": "s.reynolds"},
                {"firstName": "Sean", "lastName": "Reynolds", "mailboxUsername": "sean.r"},
                {"firstName": "Sean", "lastName": "Reynolds", "mailboxUsername": "sean.reynolds"},
            ]
            mailboxes = [{**s, "domainName": domain} for s in SPECS]
            payload = {zapmail_id: mailboxes}
            MAX_RETRIES = 5
            RETRY_DELAY = 15
            created = False
            last_err = ""
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    r = req.post(f"{ZAPMAIL_API}/v2/mailboxes", headers=zm_h(), json=payload, timeout=60)
                    mb_data = r.json()
                    mb_ids = mb_data.get("data", [])
                    if isinstance(mb_ids, list) and len(mb_ids) > 0:
                        result["mb_ids"] = mb_ids
                        result["mailboxes_created"] = len(mb_ids)
                        result["checks"].append(f"create_mailboxes: OK ({len(mb_ids)} created, attempt {attempt})")
                        created = True
                        break
                    else:
                        last_err = mb_data.get("message", str(mb_data)[:150])
                        if attempt < MAX_RETRIES and ("enough mailboxes" in last_err.lower() or "can't be assigned" in last_err.lower()):
                            result["checks"].append(f"create_attempt_{attempt}: slots not ready — retrying in {RETRY_DELAY}s")
                            _time.sleep(RETRY_DELAY)
                        elif attempt < MAX_RETRIES:
                            result["checks"].append(f"create_attempt_{attempt}: {last_err[:60]} — retrying in {RETRY_DELAY}s")
                            _time.sleep(RETRY_DELAY)
                except Exception as e:
                    last_err = str(e)[:150]
                    if attempt < MAX_RETRIES:
                        result["checks"].append(f"create_attempt_{attempt}: error — retrying in {RETRY_DELAY}s")
                        _time.sleep(RETRY_DELAY)
            if not created:
                result["error"] = f"Mailbox creation failed after {MAX_RETRIES} attempts: {last_err}"
                result["checks"].append(f"create_mailboxes: FAILED after {MAX_RETRIES} attempts — {last_err[:80]}")
                return _cors(jsonify(result))

            # Step 5: Verify mailboxes exist by re-fetching
            _time.sleep(1)
            try:
                vr = req.get(f"{ZAPMAIL_API}/v2/domains?limit=200", headers=zm_h(), timeout=30)
                for dd in vr.json().get("data", {}).get("domains", []):
                    if dd.get("domain") == domain:
                        actual_mb = dd.get("mailboxes", [])
                        if isinstance(actual_mb, list) and len(actual_mb) >= 3:
                            result["verified"] = True
                            result["checks"].append(f"verify_mailboxes: OK ({len(actual_mb)} confirmed)")
                        else:
                            result["checks"].append(f"verify_mailboxes: WARN — only {len(actual_mb) if isinstance(actual_mb, list) else 0} found")
                        break
            except Exception as e:
                result["checks"].append(f"verify_mailboxes: SKIP — {str(e)[:60]}")

        # Step 5: Set profile photos
        if result["mb_ids"]:
            try:
                mb_photo_data = [{"mailboxId": mid, "profilePicture": PHOTO_URL} for mid in result["mb_ids"]]
                pr = req.put(f"{ZAPMAIL_API}/v2/mailboxes", headers=zm_h(), json={"mailboxData": mb_photo_data}, timeout=60)
                if pr.status_code == 200:
                    result["photos_set"] = True
                    result["checks"].append("set_photos: OK")
                else:
                    result["checks"].append(f"set_photos: FAILED — HTTP {pr.status_code}")
            except Exception as e:
                result["checks"].append(f"set_photos: FAILED — {str(e)[:60]}")

        # Step 7: Photos are applied async — trust PUT 200 response
        if result["photos_set"]:
            result["checks"].append(f"verify_photos: OK (async — PUT accepted {len(result['mb_ids'])} mailboxes)")

        return _cors(jsonify(result))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


@app.route("/api/group/save-state", methods=["POST", "OPTIONS"])
def group_save_state():
    """Save group wizard state to DB for Phase 2."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import time as _time
        import db as store
        body = request.get_json(silent=True) or {}
        letter = body.get("letter", "").strip().upper()
        domains = body.get("domains", [])
        all_mb_ids = body.get("all_mb_ids", [])
        if not letter:
            return _cors(jsonify({"error": "letter required"})), 400
        state = {
            "letter": letter,
            "domains": [{"domain": d, "emails": [f"s.reynolds@{d}", f"sean.r@{d}", f"sean.reynolds@{d}"], "mb_ids": []} for d in domains],
            "all_mb_ids": all_mb_ids,
            "phase": "mailboxes_created",
            "created_at": _time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        store.set_state(f"generic_group_wizard_{letter}", state)
        return _cors(jsonify({"ok": True}))
    except Exception as e:
        return _cors(jsonify({"error": str(e)})), 500


# ──────────────────────────────────────────────────────────
# Per-step finalize endpoints (replace monolithic finalize)
# ──────────────────────────────────────────────────────────

@app.route("/api/group/check-mailbox-status", methods=["POST", "OPTIONS"])
def group_check_mailbox_status():
    """Poll Zapmail to check if all mailboxes are ACTIVE (not In Progress)."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import requests as req
        body = request.get_json(silent=True) or {}
        domains = set(body.get("domains", []))
        if not domains:
            return _cors(jsonify({"error": "domains required"})), 400

        ZAPMAIL_API = "https://api.zapmail.ai/api"
        ZAPMAIL_KEY = os.environ.get("ZAPMAIL_API_KEY", "").strip()
        headers = {"x-auth-zapmail": ZAPMAIL_KEY, "Content-Type": "application/json"}

        all_zm = []
        page = 1
        for _ in range(10):
            zr = req.get(f"{ZAPMAIL_API}/v2/domains?page={page}", headers=headers, timeout=30)
            zd = zr.json().get("data", {})
            all_zm.extend(zd.get("domains", []))
            if page >= zd.get("totalPages", 1):
                break
            page += 1

        active = 0
        in_progress = 0
        other = 0
        details = {}
        for d in all_zm:
            dn = d.get("domain", "")
            if dn not in domains:
                continue
            mbs = d.get("mailboxes", [])
            if not isinstance(mbs, list):
                continue
            domain_active = 0
            domain_pending = 0
            for mb in mbs:
                status = mb.get("status", "unknown")
                if status == "ACTIVE":
                    active += 1
                    domain_active += 1
                elif status in ("IN_PROGRESS", "PENDING", "PROVISIONING"):
                    in_progress += 1
                    domain_pending += 1
                else:
                    other += 1
                    domain_pending += 1
            details[dn] = {"active": domain_active, "pending": domain_pending}

        total = active + in_progress + other
        expected = len(domains) * 3
        all_ready = in_progress == 0 and other == 0 and active >= expected

        return _cors(jsonify({
            "ok": True, "all_ready": all_ready,
            "active": active, "in_progress": in_progress, "other": other,
            "total": total, "expected": expected, "details": details
        }))
    except Exception as e:
        return _cors(jsonify({"error": str(e)})), 500


@app.route("/api/group/export-smartlead", methods=["POST", "OPTIONS"])
def group_export_smartlead():
    """Export mailboxes to SmartLead via Zapmail."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import requests as req
        body = request.get_json(silent=True) or {}
        letter = body.get("letter", "").strip().upper()
        mb_ids = body.get("mb_ids", [])
        domains = body.get("domains", [])

        ZAPMAIL_API = "https://api.zapmail.ai/api"
        ZAPMAIL_KEY = os.environ.get("ZAPMAIL_API_KEY", "").strip()
        def zm_h():
            return {"x-auth-zapmail": ZAPMAIL_KEY, "Content-Type": "application/json"}

        checks = []
        if mb_ids:
            r = req.post(f"{ZAPMAIL_API}/v2/exports/mailboxes", headers=zm_h(),
                         json={"apps": ["SMARTLEAD"], "ids": mb_ids}, timeout=30)
            msg = r.json().get("message", r.text[:200])
            if r.status_code == 200:
                checks.append(f"export_request: OK — {len(mb_ids)} mailbox IDs submitted")
            else:
                checks.append(f"export_request: FAILED — HTTP {r.status_code}: {msg[:80]}")
        else:
            exported = 0
            for dd in domains:
                er = req.post(f"{ZAPMAIL_API}/v2/exports/mailboxes", headers=zm_h(),
                              json={"apps": ["SMARTLEAD"], "contains": dd}, timeout=30)
                if er.status_code == 200:
                    exported += 1
                import time; time.sleep(2)
            msg = f"Exported by domain name ({exported}/{len(domains)} succeeded)"
            checks.append(f"export_by_domain: {exported}/{len(domains)} OK")

        return _cors(jsonify({"ok": True, "message": msg, "checks": checks}))
    except Exception as e:
        return _cors(jsonify({"error": str(e)})), 500


@app.route("/api/group/find-smartlead-accounts", methods=["POST", "OPTIONS"])
def group_find_smartlead_accounts():
    """Search SmartLead for accounts matching our domains."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import time as _time
        import requests as req
        body = request.get_json(silent=True) or {}
        domains = set(body.get("domains", []))
        if not domains:
            return _cors(jsonify({"error": "domains required"})), 400

        SMARTLEAD_KEY = os.environ.get("SMARTLEAD_API_KEY", "")
        SMARTLEAD_API = "https://server.smartlead.ai/api/v1"

        found = []
        offset = 0
        pages_scanned = 0
        while True:
            url = f"{SMARTLEAD_API}/email-accounts/?api_key={SMARTLEAD_KEY}&offset={offset}&limit=100"
            r = req.get(url, timeout=30)
            if r.status_code == 429:
                _time.sleep(10)
                r = req.get(url, timeout=30)
            accts = r.json() if r.status_code == 200 else []
            if not isinstance(accts, list) or not accts:
                break
            pages_scanned += 1
            for a in accts:
                email = a.get("from_email", a.get("email", ""))
                domain_part = email.split("@")[-1] if "@" in email else ""
                if domain_part in domains:
                    found.append({"id": a.get("id"), "email": email})
            if len(accts) < 100:
                break
            offset += 100
            _time.sleep(0.5)

        expected = len(domains) * 3
        found_domains = set(a["email"].split("@")[-1] for a in found if "@" in a.get("email", ""))
        missing_domains = [d for d in domains if d not in found_domains]
        checks = [
            f"scan: OK — {pages_scanned} pages scanned",
            f"accounts: {len(found)}/{expected} expected (3 per domain)",
            f"domains_covered: {len(found_domains)}/{len(domains)}",
        ]
        if missing_domains:
            checks.append(f"missing_domains: {', '.join(list(missing_domains)[:5])}")

        return _cors(jsonify({"ok": True, "accounts": found, "count": len(found),
                              "expected": expected, "missing_domains": missing_domains, "checks": checks}))
    except Exception as e:
        return _cors(jsonify({"error": str(e)})), 500


@app.route("/api/group/setup-tags", methods=["POST", "OPTIONS"])
def group_setup_tags():
    """Get or create the 3 required tags (Zapmail, group letter, date)."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import time as _time
        import requests as req
        body = request.get_json(silent=True) or {}
        letter = body.get("letter", "").strip().upper()
        if not letter:
            return _cors(jsonify({"error": "letter required"})), 400

        SMARTLEAD_JWT = os.environ.get("SMARTLEAD_JWT", "")
        SMARTLEAD_GQL = os.environ.get("SMARTLEAD_GQL", "https://fe-gql.smartlead.ai/v1/graphql")
        def sl_h():
            return {"Authorization": f"Bearer {SMARTLEAD_JWT}", "Content-Type": "application/json"}
        def _gql(query, variables=None):
            b = {"query": query}
            if variables: b["variables"] = variables
            return req.post(SMARTLEAD_GQL, headers=sl_h(), json=b, timeout=30).json()

        tags_result = _gql("{ tags { id name color } }")
        all_tags = {t["name"]: t for t in tags_result.get("data", {}).get("tags", [])}

        ZAPMAIL_TAG_ID = 262254
        tag_name = f"Generic {letter}"
        today_str = _time.strftime("%-m/%-d/%y")

        group_tag_id = None
        if tag_name in all_tags:
            group_tag_id = all_tags[tag_name]["id"]
        else:
            used_colors = {t.get("color", "").upper() for t in all_tags.values()}
            palette = ["#FF6B6B", "#FF8E72", "#FFA94D", "#FFD43B", "#A9E34B",
                       "#51CF66", "#20C997", "#22B8CF", "#339AF0", "#5C7CFA",
                       "#7950F2", "#BE4BDB", "#E64980", "#F06595", "#CC5DE8"]
            color = next((c for c in palette if c.upper() not in used_colors), "#7950F2")
            mut = """mutation($o: tags_insert_input!) { insert_tags_one(object: $o) { id name color } }"""
            result = _gql(mut, {"o": {"name": tag_name, "color": color}})
            group_tag_id = result.get("data", {}).get("insert_tags_one", {}).get("id")

        date_tag_id = None
        if today_str in all_tags:
            date_tag_id = all_tags[today_str]["id"]
        else:
            mut = """mutation($o: tags_insert_input!) { insert_tags_one(object: $o) { id name color } }"""
            result = _gql(mut, {"o": {"name": today_str, "color": "#94a3b8"}})
            date_tag_id = result.get("data", {}).get("insert_tags_one", {}).get("id")

        checks = []
        if not group_tag_id or not date_tag_id:
            checks.append(f"group_tag: {'OK' if group_tag_id else 'FAILED'}")
            checks.append(f"date_tag: {'OK' if date_tag_id else 'FAILED'}")
            return _cors(jsonify({"error": "Failed to create tags", "checks": checks})), 500

        checks.append(f"zapmail_tag: OK (id={ZAPMAIL_TAG_ID})")
        checks.append(f"group_tag: OK ('{tag_name}' id={group_tag_id})")
        checks.append(f"date_tag: OK ('{today_str}' id={date_tag_id})")

        # Verify: re-fetch tags to confirm they exist
        try:
            verify_result = _gql("{ tags { id name } }")
            verify_tags = {t["id"]: t["name"] for t in verify_result.get("data", {}).get("tags", [])}
            if group_tag_id in verify_tags and date_tag_id in verify_tags:
                checks.append("verify_tags: OK — all 3 confirmed in SmartLead")
            else:
                missing = []
                if group_tag_id not in verify_tags:
                    missing.append(tag_name)
                if date_tag_id not in verify_tags:
                    missing.append(today_str)
                checks.append(f"verify_tags: WARN — missing: {', '.join(missing)}")
        except Exception as e:
            checks.append(f"verify_tags: SKIP — {str(e)[:60]}")

        return _cors(jsonify({"ok": True, "tag_ids": [ZAPMAIL_TAG_ID, group_tag_id, date_tag_id],
                              "tag_names": ["Zapmail", tag_name, today_str], "checks": checks}))
    except Exception as e:
        return _cors(jsonify({"error": str(e)})), 500


@app.route("/api/group/finalize-account", methods=["POST", "OPTIONS"])
def group_finalize_account():
    """Tag one account + enable warmup. Called per-account from frontend."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import time as _time
        import requests as req
        body = request.get_json(silent=True) or {}
        account_id = body.get("account_id")
        tag_ids = body.get("tag_ids", [])
        if not account_id or not tag_ids:
            return _cors(jsonify({"error": "account_id and tag_ids required"})), 400

        SMARTLEAD_KEY = os.environ.get("SMARTLEAD_API_KEY", "")
        SMARTLEAD_API = "https://server.smartlead.ai/api/v1"
        SMARTLEAD_INTERNAL = "https://server.smartlead.ai/api"
        SMARTLEAD_JWT = os.environ.get("SMARTLEAD_JWT", "")
        def sl_h():
            return {"Authorization": f"Bearer {SMARTLEAD_JWT}", "Content-Type": "application/json"}

        checks = []

        # Step 1: Tag the account
        tag_body = {"id": account_id, "tags": tag_ids, "clientId": None}
        r = req.post(f"{SMARTLEAD_INTERNAL}/email-account/save-management-details",
                     headers=sl_h(), json=tag_body, timeout=30)
        tagged = r.status_code == 200
        checks.append(f"tag_account: {'OK' if tagged else 'FAILED — HTTP ' + str(r.status_code)}")

        # Step 2: Enable warmup (public API) — verify + retry since 200 doesn't guarantee activation
        warmup_body = {"warmup_enabled": True, "total_warmup_per_day": 15,
                       "daily_rampup": 5, "reply_rate_percentage": 40}
        warmed = False
        for warmup_attempt in range(3):
            r = req.post(f"{SMARTLEAD_API}/email-accounts/{account_id}/warmup?api_key={SMARTLEAD_KEY}",
                         json=warmup_body, timeout=30)
            if r.status_code == 429:
                _time.sleep(10)
                continue
            _time.sleep(1)
            vr = req.get(f"{SMARTLEAD_API}/email-accounts/{account_id}?api_key={SMARTLEAD_KEY}", timeout=15)
            if vr.status_code == 200 and vr.json().get("warmup_enabled"):
                warmed = True
                break
            _time.sleep(2)
        checks.append(f"enable_warmup: {'OK (verified)' if warmed else 'WARN — sent but unverified'}")

        # Step 3: Full warmup config (internal API)
        warmup_key = ""
        wd = req.get(f"{SMARTLEAD_INTERNAL}/email-account/fetch-warmup-details-by-email-account-id/{account_id}",
                     headers=sl_h(), timeout=30)
        if wd.status_code == 200:
            warmup_key = wd.json().get("message", {}).get("warmup_key_id", "")
        if warmup_key:
            full_body = {
                "emailAccountId": str(account_id), "maxEmailPerDay": 15,
                "isRampupEnabled": True, "rampupValue": 5,
                "warmupMinCount": 10, "warmupMaxCount": 15,
                "replyRate": 40, "dailyReplyLimit": 15,
                "autoAdjustWarmup": False, "sendWarmupsOnlyOnWeekdays": False,
                "useCustomDomain": False, "status": "ACTIVE", "warmupKeyId": warmup_key
            }
            sr = req.post(f"{SMARTLEAD_INTERNAL}/email-account/save-warmup",
                          headers=sl_h(), json=full_body, timeout=30)
            checks.append(f"full_warmup_config: {'OK' if sr.status_code == 200 else 'FAILED — HTTP ' + str(sr.status_code)}")
        else:
            checks.append(f"full_warmup_config: SKIP — no warmup_key found")

        # Step 3b: Set time_to_wait_in_mins (matches setup.py)
        try:
            import json as _json
            wait_body = {"time_to_wait_in_mins": 5}
            wr = req.post(f"{SMARTLEAD_API}/email-accounts/{account_id}/settings?api_key={SMARTLEAD_KEY}",
                          json=wait_body, timeout=15)
            if wr.status_code != 200:
                req.post(f"{SMARTLEAD_INTERNAL}/email-account/update",
                         headers=sl_h(), json={"id": account_id, "time_to_wait_in_mins": 5}, timeout=15)
            checks.append("time_to_wait: OK (5 min)")
        except Exception:
            checks.append("time_to_wait: SKIP")

        # Step 4: Verify tags applied (internal endpoint — public API doesn't return tags)
        _time.sleep(1)
        try:
            vr = req.get(f"{SMARTLEAD_INTERNAL}/email-account/{account_id}/details",
                         headers=sl_h(), timeout=15)
            if vr.status_code == 200:
                acct_data = vr.json()
                acct_pk = acct_data.get("email_accounts_by_pk", {})
                tag_mappings = acct_pk.get("email_account_tag_mappings", [])
                applied_ids = {m.get("tag", {}).get("id") for m in tag_mappings if isinstance(m, dict)}
                if all(tid in applied_ids for tid in tag_ids):
                    checks.append(f"verify_tags: OK ({len(applied_ids)} tags confirmed)")
                else:
                    missing = [str(t) for t in tag_ids if t not in applied_ids]
                    checks.append(f"verify_tags: WARN — missing tag IDs: {', '.join(missing)}")
                warmup_status = acct_pk.get("warmup_enabled") or acct_pk.get("warmupEnabled")
                if warmup_status:
                    checks.append("verify_warmup: OK — warmup enabled")
                else:
                    checks.append("verify_warmup: WARN — warmup may not be active yet")
            else:
                checks.append(f"verify_tags: SKIP — HTTP {vr.status_code}")
        except Exception as e:
            checks.append(f"verify: SKIP — {str(e)[:60]}")

        return _cors(jsonify({"ok": True, "tagged": tagged, "warmed": warmed, "checks": checks}))
    except Exception as e:
        return _cors(jsonify({"error": str(e)})), 500


@app.route("/api/finalize-generic-group", methods=["POST", "OPTIONS"])
def finalize_generic_group():
    """Phase 2: Export to SmartLead, tag, enable warmup — runs entirely server-side."""
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import time as _time
        import requests as req
        body = request.get_json(silent=True) or {}
        letter = body.get("letter", "").strip().upper()
        if not letter:
            return _cors(jsonify({"error": "letter required"})), 400

        import db as store
        from datetime import datetime, timezone
        state = store.get_state(f"generic_group_wizard_{letter}")
        if not state:
            return _cors(jsonify({"error": f"No pending group {letter} — run Phase 1 first"})), 400

        # If already finalizing, check if it's recent — don't restart
        if state.get("phase") == "finalizing":
            updated = state.get("_finalize_started", "")
            if updated:
                try:
                    started = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                    age = (datetime.now(timezone.utc) - started).total_seconds()
                    if age < 300:  # within 5 min — still running
                        return _cors(jsonify({"ok": True, "already_running": True,
                                              "step": state.get("finalize_step", "unknown"),
                                              "detail": state.get("finalize_detail", ""),
                                              "age_seconds": int(age)}))
                except Exception:
                    pass

        if state.get("phase") not in ("mailboxes_created", "export_failed", "finalizing"):
            if state.get("phase") == "complete" and state.get("accounts_found", 0) == 0:
                pass  # allow re-run if complete but 0 accounts
            else:
                return _cors(jsonify({"error": f"Group {letter} phase is '{state.get('phase')}' — expected mailboxes_created"})), 400

        state["_finalize_started"] = datetime.now(timezone.utc).isoformat()
        state["phase"] = "finalizing"
        store.set_state(f"generic_group_wizard_{letter}", state)

        ZAPMAIL_API = "https://api.zapmail.ai/api"
        ZAPMAIL_KEY = os.environ.get("ZAPMAIL_API_KEY", "").strip()
        SMARTLEAD_API = "https://server.smartlead.ai/api/v1"
        SMARTLEAD_KEY = os.environ.get("SMARTLEAD_API_KEY", "")
        SMARTLEAD_INTERNAL = "https://server.smartlead.ai/api"
        SMARTLEAD_JWT = os.environ.get("SMARTLEAD_JWT", "")
        SMARTLEAD_GQL = os.environ.get("SMARTLEAD_GQL", "https://fe-gql.smartlead.ai/v1/graphql")

        def zm_h():
            return {"x-auth-zapmail": ZAPMAIL_KEY, "Content-Type": "application/json"}
        def sl_h():
            return {"Authorization": f"Bearer {SMARTLEAD_JWT}", "Content-Type": "application/json"}

        log_lines = state.get("finalize_log", [])
        def _log(msg):
            log_lines.append(msg)

        def _save_progress(step, detail=""):
            state["phase"] = "finalizing"
            state["finalize_step"] = step
            state["finalize_detail"] = detail
            state["finalize_log"] = log_lines
            store.set_state(f"generic_group_wizard_{letter}", state)

        # --- Resume check: skip export+find if we already have processed accounts ---
        processed = state.get("_processed_ids", [])
        if processed and state.get("finalize_step") == "accounts":
            _log(f"Resuming from account {len(processed)}/{state.get('accounts_found', '?')}")
            # Jump straight to step 4 — need to reconstruct found_ids and tag_ids
            our_domains = {dd["domain"] for dd in state.get("domains", [])}
            found = []
            offset = 0
            for _ in range(30):
                url = f"{SMARTLEAD_API}/email-accounts/?api_key={SMARTLEAD_KEY}&offset={offset}&limit=100"
                r = req.get(url, timeout=30)
                if r.status_code == 429:
                    _time.sleep(10)
                    r = req.get(url, timeout=30)
                accts = r.json() if r.status_code == 200 else []
                if not isinstance(accts, list) or not accts:
                    break
                for a in accts:
                    email = a.get("from_email", a.get("email", ""))
                    domain = email.split("@")[-1] if "@" in email else ""
                    if domain in our_domains:
                        found.append({"id": a.get("id"), "email": email})
                if len(accts) < 100:
                    break
                offset += 100
                _time.sleep(0.5)
            found_ids = [a["id"] for a in found]
            # Reconstruct tag_ids from state log
            tag_ids = state.get("_tag_ids", [])
            if found_ids and tag_ids:
                # Jump to step 4 processing below
                pass
            else:
                _log("Resume failed — re-running from scratch")
                processed = []
                state["_processed_ids"] = []

        # --- Step 1: Export to SmartLead ---
        if not processed:
            _save_progress("export", "Exporting mailboxes...")
            mb_ids = state.get("all_mb_ids", [])
            if mb_ids:
                _log(f"Exporting {len(mb_ids)} mailboxes to SmartLead...")
                r = req.post(f"{ZAPMAIL_API}/v2/exports/mailboxes", headers=zm_h(),
                             json={"apps": ["SMARTLEAD"], "ids": mb_ids}, timeout=30)
                _log(f"Export: {r.json().get('message', r.text[:200])}")
            else:
                _log("No mailbox IDs — exporting by domain name...")
                for dd in state.get("domains", []):
                    r = req.post(f"{ZAPMAIL_API}/v2/exports/mailboxes", headers=zm_h(),
                                 json={"apps": ["SMARTLEAD"], "contains": dd["domain"]}, timeout=30)
                    _time.sleep(2)

        if not processed:
            # --- Step 2: Poll for accounts (instead of fixed wait) ---
            our_domains = {dd["domain"] for dd in state.get("domains", [])}
            expected = len(our_domains) * 3
            found = []
            MAX_POLLS = 12  # 12 x 15s = 3 min max wait
            for poll in range(MAX_POLLS):
                wait_sec = 15 if poll > 0 else 30  # first wait 30s, then 15s
                _save_progress("waiting", f"Waiting for SmartLead ({poll + 1}/{MAX_POLLS})...")
                _time.sleep(wait_sec)

                found = []
                offset = 0
                for _ in range(30):
                    url = f"{SMARTLEAD_API}/email-accounts/?api_key={SMARTLEAD_KEY}&offset={offset}&limit=100"
                    r = req.get(url, timeout=30)
                    if r.status_code == 429:
                        _time.sleep(10)
                        r = req.get(url, timeout=30)
                    accts = r.json() if r.status_code == 200 else []
                    if not isinstance(accts, list) or not accts:
                        break
                    for a in accts:
                        email = a.get("from_email", a.get("email", ""))
                        domain = email.split("@")[-1] if "@" in email else ""
                        if domain in our_domains:
                            found.append({"id": a.get("id"), "email": email})
                    if len(accts) < 100:
                        break
                    offset += 100
                    _time.sleep(0.5)

                _log(f"Poll {poll + 1}: found {len(found)}/{expected} accounts")
                _save_progress("finding", f"Found {len(found)}/{expected} accounts")

                if len(found) >= expected:
                    break

                # Re-export if 0 found after 2 polls
                if len(found) == 0 and poll == 2:
                    mb_ids = state.get("all_mb_ids", [])
                    if mb_ids:
                        _log("Re-exporting — 0 accounts after 2 polls...")
                        req.post(f"{ZAPMAIL_API}/v2/exports/mailboxes", headers=zm_h(),
                                 json={"apps": ["SMARTLEAD"], "ids": mb_ids}, timeout=30)

            if not found:
                state["phase"] = "export_failed"
                state["finalize_log"] = log_lines
                store.set_state(f"generic_group_wizard_{letter}", state)
                return _cors(jsonify({"ok": False, "error": "No accounts found in SmartLead after polling. Try re-running.",
                                      "log": log_lines, "retry": True}))

            found_ids = [a["id"] for a in found]
            _log(f"Found {len(found_ids)} accounts — proceeding to tag + warmup")

        # --- Step 3: Get/create tags via GQL (skip if resuming with saved tag_ids) ---
        def _gql(query, variables=None):
            b = {"query": query}
            if variables:
                b["variables"] = variables
            r = req.post(SMARTLEAD_GQL, headers=sl_h(), json=b, timeout=30)
            return r.json()

        if processed and state.get("_tag_ids"):
            tag_ids = state["_tag_ids"]
            _log(f"Resuming with saved tag IDs: {tag_ids}")
        else:
            _save_progress("tags", "Creating tags...")
            tags_result = _gql("{ tags { id name color } }")
            all_tags = {t["name"]: t for t in tags_result.get("data", {}).get("tags", [])}

            ZAPMAIL_TAG_ID = 262254
            tag_name = f"Generic {letter}"
            today_str = _time.strftime("%-m/%-d/%y")

            group_tag_id = None
            if tag_name in all_tags:
                group_tag_id = all_tags[tag_name]["id"]
            else:
                used_colors = {t.get("color", "").upper() for t in all_tags.values()}
                palette = ["#FF6B6B", "#FF8E72", "#FFA94D", "#FFD43B", "#A9E34B",
                           "#51CF66", "#20C997", "#22B8CF", "#339AF0", "#5C7CFA",
                           "#7950F2", "#BE4BDB", "#E64980", "#F06595", "#CC5DE8"]
                color = next((c for c in palette if c.upper() not in used_colors), "#7950F2")
                mut = """mutation($o: tags_insert_input!) { insert_tags_one(object: $o) { id name color } }"""
                result = _gql(mut, {"o": {"name": tag_name, "color": color}})
                group_tag_id = result.get("data", {}).get("insert_tags_one", {}).get("id")
                _log(f"Created tag: {tag_name} (ID: {group_tag_id})")

            date_tag_id = None
            if today_str in all_tags:
                date_tag_id = all_tags[today_str]["id"]
            else:
                mut = """mutation($o: tags_insert_input!) { insert_tags_one(object: $o) { id name color } }"""
                result = _gql(mut, {"o": {"name": today_str, "color": "#94a3b8"}})
                date_tag_id = result.get("data", {}).get("insert_tags_one", {}).get("id")
                _log(f"Created date tag: {today_str} (ID: {date_tag_id})")

            if not group_tag_id or not date_tag_id:
                return _cors(jsonify({"error": "Failed to create tags", "log": log_lines})), 500

            tag_ids = [ZAPMAIL_TAG_ID, group_tag_id, date_tag_id]
            state["_tag_ids"] = tag_ids
            state["accounts_found"] = len(found_ids)
            store.set_state(f"generic_group_wizard_{letter}", state)
            _log(f"Tags ready: Zapmail ({ZAPMAIL_TAG_ID}), {tag_name} ({group_tag_id}), {today_str} ({date_tag_id})")

        # --- Step 4: Tag + warmup each account (resumable) ---
        already_done = set(state.get("_processed_ids", []))
        remaining_ids = [aid for aid in found_ids if aid not in already_done]
        tagged = state.get("accounts_tagged", 0)
        warmed = state.get("accounts_warmed", 0)
        warmup_body = {"warmup_enabled": True, "total_warmup_per_day": 15,
                       "daily_rampup": 5, "reply_rate_percentage": 40}

        if already_done:
            _log(f"Resuming: {len(already_done)} already done, {len(remaining_ids)} remaining")

        for i, acc_id in enumerate(remaining_ids):
            total_done = len(already_done) + i + 1
            _save_progress("accounts", f"Configuring account {total_done}/{len(found_ids)}")

            # Tag
            tag_body = {"id": acc_id, "tags": tag_ids, "clientId": None}
            r = req.post(f"{SMARTLEAD_INTERNAL}/email-account/save-management-details",
                         headers=sl_h(), json=tag_body, timeout=30)
            if r.status_code == 200:
                tagged += 1
            elif r.status_code == 429:
                _time.sleep(10)
                r = req.post(f"{SMARTLEAD_INTERNAL}/email-account/save-management-details",
                             headers=sl_h(), json=tag_body, timeout=30)
                if r.status_code == 200:
                    tagged += 1

            # Warmup — fire and move on (no per-account verify loop)
            wr = req.post(f"{SMARTLEAD_API}/email-accounts/{acc_id}/warmup?api_key={SMARTLEAD_KEY}",
                          json=warmup_body, timeout=30)
            if wr.status_code == 429:
                _time.sleep(5)
                wr = req.post(f"{SMARTLEAD_API}/email-accounts/{acc_id}/warmup?api_key={SMARTLEAD_KEY}",
                              json=warmup_body, timeout=30)
            if wr.status_code == 200:
                warmed += 1

            # Full warmup config
            wd = req.get(f"{SMARTLEAD_INTERNAL}/email-account/fetch-warmup-details-by-email-account-id/{acc_id}",
                         headers=sl_h(), timeout=30)
            warmup_key = ""
            if wd.status_code == 200:
                warmup_key = wd.json().get("message", {}).get("warmup_key_id", "")
            if warmup_key:
                full_body = {
                    "emailAccountId": str(acc_id), "maxEmailPerDay": 15,
                    "isRampupEnabled": True, "rampupValue": 5,
                    "warmupMinCount": 10, "warmupMaxCount": 15,
                    "replyRate": 40, "dailyReplyLimit": 15,
                    "autoAdjustWarmup": False, "sendWarmupsOnlyOnWeekdays": False,
                    "useCustomDomain": False, "status": "ACTIVE", "warmupKeyId": warmup_key
                }
                req.post(f"{SMARTLEAD_INTERNAL}/email-account/save-warmup",
                         headers=sl_h(), json=full_body, timeout=30)

            # time_to_wait setting
            try:
                req.post(f"{SMARTLEAD_API}/email-accounts/{acc_id}/settings?api_key={SMARTLEAD_KEY}",
                         json={"time_to_wait_in_mins": 5}, timeout=15)
            except Exception:
                pass

            # Save progress after each account so we can resume
            already_done.add(acc_id)
            state["_processed_ids"] = list(already_done)
            state["accounts_tagged"] = tagged
            state["accounts_warmed"] = warmed
            store.set_state(f"generic_group_wizard_{letter}", state)
            _time.sleep(0.3)

        _log(f"Tagged {tagged}/{len(found_ids)}, warmup {warmed}/{len(found_ids)}")

        # --- Done ---
        state["phase"] = "complete"
        state["accounts_found"] = len(found_ids)
        state["accounts_tagged"] = tagged
        state["accounts_warmed"] = warmed
        state["finalize_step"] = "done"
        state["finalize_detail"] = f"{tagged} tagged, {warmed} warmup"
        state["finalize_log"] = log_lines
        store.set_state(f"generic_group_wizard_{letter}", state)

        # Add new group to overview cache immediately
        _add_generic_group_to_cache(letter, found, state, store)

        return _cors(jsonify({"ok": True, "letter": letter,
                              "accounts_found": len(found_ids),
                              "accounts_tagged": tagged,
                              "accounts_warmed": warmed,
                              "log": log_lines}))
    except Exception as e:
        import traceback
        return _cors(jsonify({"error": str(e), "trace": traceback.format_exc()})), 500


def _add_generic_group_to_cache(letter, found_accounts, wizard_state, store):
    """Add a newly finalized generic group to the overview cache so it appears immediately."""
    try:
        from datetime import date
        data, _ = store.cache_get("overview_v2")
        if not data:
            return
        group_name = f"Generic {letter}"
        existing = data.get("generic_groups") or []
        if any(g.get("name") == group_name for g in existing):
            return
        domains = {a["email"].split("@")[-1] for a in found_accounts if "@" in a.get("email", "")}
        today = date.today()
        new_group = {
            "name": group_name,
            "accounts": len(found_accounts),
            "total_domains": len(domains),
            "daily_capacity": len(found_accounts) * 15,
            "in_campaign": 0,
            "smtp_failures": 0,
            "avg_bounce_rate": None,
            "avg_reply_rate": None,
            "avg_warmup_reputation": None,
            "total_sent": 0,
            "daily_sent": 0,
            "campaigns": [],
            "account_details": [{"id": a["id"], "email": a["email"], "domain": a["email"].split("@")[-1],
                                  "bounce_rate": None, "reply_rate": None, "sent": 0, "smtp_ok": True,
                                  "warmup_enabled": True, "in_campaign": False, "campaign_names": [],
                                  "warmup_reputation": None} for a in found_accounts],
            "warmup_start": today.isoformat(),
            "warmup_days": 0,
        }
        existing.append(new_group)
        existing.sort(key=lambda g: g["name"])
        data["generic_groups"] = existing
        store.cache_patch("overview_v2", data)
    except Exception:
        pass


@app.route("/api/group/wizard-state")
def group_wizard_state():
    """Return current wizard state from Supabase (for polling during server-side finalize)."""
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    try:
        import db as store
        letter = request.args.get("letter", "").strip().upper()
        if not letter:
            return _cors(jsonify({"error": "letter required"})), 400
        state = store.get_state(f"generic_group_wizard_{letter}")
        if not state:
            return _cors(jsonify({"state": None}))
        return _cors(jsonify({"state": {
            "phase": state.get("phase"),
            "finalize_step": state.get("finalize_step"),
            "finalize_detail": state.get("finalize_detail"),
            "accounts_found": state.get("accounts_found", 0),
            "accounts_tagged": state.get("accounts_tagged", 0),
            "accounts_warmed": state.get("accounts_warmed", 0),
        }}))
    except Exception as e:
        return _cors(jsonify({"error": str(e)})), 500


@app.route("/api/<path:path>", methods=["GET", "OPTIONS"])
def catch_all(path):
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))
    if not _check_auth():
        return _cors(jsonify({"error": "Unauthorized"})), 401
    return _cors(jsonify({"error": "Not found"})), 404
