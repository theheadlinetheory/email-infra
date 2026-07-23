"""Health V1 — replacement tracking.

Turns "this inbox is burned" into a tracked replacement job with an enforced
2-week warmup, so a replacement can't be (a) forgotten mid-flight or (b) put
into a campaign before it's warmed.

Lifecycle:  flagged -> warming -> (14 days) -> ready -> swapped   (or cancelled)
  * flagged : we've decided to replace it; replacement not started yet.
  * warming : a fresh inbox is provisioned and warming (warming_started_at set).
              It CANNOT send during this window.
  * ready   : computed — warming_started_at + WARMUP_DAYS has elapsed.
  * swapped : replacement assigned to the campaign; old inbox can now be cancelled.

Stored as a JSON list in the `state` table (key `health_replacements`) — no new
migration. Uses only stdlib datetime (server-side, not the workflow sandbox).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import db as store

WARMUP_DAYS = 14
STATE_KEY = "health_replacements"
_ACTIVE = ("flagged", "warming", "ready", "reserved")


def _sl_key() -> str:
    import os
    return (os.environ.get("SMARTLEAD_API_KEY", "") or os.environ.get("SMARTLEAD_KEY", "")).strip()


def _overview_account_id(email: str):
    """Resolve an email to its SmartLead account id from the overview cache."""
    ov, _ = store.cache_get("overview_v2")
    for c in (ov or {}).get("clients", []):
        for L in ("a", "b"):
            for ad in (c.get(f"group_{L}") or {}).get("account_details", []):
                if ad.get("email") == email and ad.get("id"):
                    return ad["id"]
    for sec in ("generic_groups", "acquisition_groups"):
        for g in (ov or {}).get(sec, []):
            for ad in g.get("account_details", []):
                if ad.get("email") == email and ad.get("id"):
                    return ad["id"]
    return None


import re as _re
_HVAC_RE = _re.compile(r"hvac|heating|cooling|furnace|refrigerat|mechanical|climate|comfort|"
                       r"aircon|airconditioning|conditioning|heatpump|\bheat\b", _re.I)
_AIR_RE = _re.compile(r"(?:^|[^a-z])air(?:[^a-z]|$)", _re.I)
_LAND_RE = _re.compile(r"lawn|landscap|landcare|yard|turf|grounds|mow|garden|"
                       r"outdoor|scape|irrigation|hardscape|planting|\bsod\b|greenery|nursery|"
                       r"groundskeep|propertyturf|propertyland|treecare|treeservice", _re.I)


def _niche(domain_or_email: str) -> str:
    """Classify a domain/inbox as 'hvac', 'landscaping', or 'generic' by its name.
    HVAC and landscaping must never replace each other; generic fills either."""
    d = (domain_or_email or "").lower()
    if "@" in d:
        d = d.split("@")[-1]
    hv = bool(_HVAC_RE.search(d) or _AIR_RE.search(d))
    la = bool(_LAND_RE.search(d))
    if hv and not la:
        return "hvac"
    if la and not hv:
        return "landscaping"
    return "generic"


def _client_niche(client_name: str) -> str:
    """The client's niche. The client NAME is the strongest signal (e.g. 'Quantum
    Heating & Air', 'Woody's Landcare'); fall back to the dominant niche of its
    inbox domains only when the name is generic."""
    by_name = _niche(client_name)
    if by_name != "generic":
        return by_name
    from collections import Counter
    ov, _ = store.cache_get("overview_v2")
    c = Counter()
    for cl in (ov or {}).get("clients", []):
        if cl.get("name") != client_name:
            continue
        for L in ("a", "b"):
            for ad in (cl.get(f"group_{L}") or {}).get("account_details", []):
                n = _niche(ad.get("email", ""))
                if n != "generic":
                    c[n] += 1
    return c.most_common(1)[0][0] if c else "generic"


def required_niche(job: dict) -> str:
    """The niche a replacement for this burned inbox must be (or generic).

    The CLIENT's niche wins when it's decisive — an HVAC client must get HVAC/
    generic even if the specific burned inbox happens to be a landscaping-branded
    domain (a pre-existing mismatch); replacing it with more landscaping would
    just deepen the cross-contamination. Fall back to the inbox's own domain niche
    only when the client is niche-ambiguous."""
    want = _client_niche(job.get("client") or "")
    if want == "generic":
        want = _niche(job.get("old_email", ""))
    return want


def _resolve_campaign_ids(names) -> dict:
    """Campaign name -> id for the given names (live campaigns list, any status)."""
    import time
    import requests
    key = _sl_key()
    if not key or not names:
        return {}
    for _ in range(4):
        r = requests.get("https://server.smartlead.ai/api/v1/campaigns",
                         params={"api_key": key}, timeout=60)
        if r.status_code == 200 and r.text.strip():
            by_name = {c.get("name"): c.get("id") for c in (r.json() or [])}
            return {n: by_name[n] for n in names if n in by_name}
        time.sleep(6)
    return {}


def swap_campaign_membership(old_email: str, reserve_account_id: int,
                             campaign_names, dry_run: bool = True) -> dict:
    """Move campaign senders: ADD the reserve inbox, REMOVE the burned inbox, on
    every campaign the burned inbox is in. Re-tagging alone does NOT do this —
    campaign membership is a separate SmartLead association. Add-before-remove so
    the campaign never dips below capacity."""
    import time
    import requests
    # campaign_names may arrive as a JSON string (the status table serializes it
    # and the reader only de-serializes reasons/subscores) — normalise to a list.
    if isinstance(campaign_names, str):
        import json
        try:
            campaign_names = json.loads(campaign_names)
        except Exception:
            campaign_names = [campaign_names] if campaign_names.strip() else []
    if not isinstance(campaign_names, list):
        campaign_names = []
    old_id = _overview_account_id(old_email)
    cids = _resolve_campaign_ids(campaign_names)
    plan = {"old_email": old_email, "old_account_id": old_id,
            "reserve_account_id": reserve_account_id,
            "campaigns": [{"name": n, "id": i} for n, i in cids.items()]}
    if dry_run:
        return {"dry_run": True, **plan}
    if not old_id:
        return {"error": f"could not resolve account id for {old_email}", **plan}
    if not cids:
        return {"note": "burned inbox not in any resolvable campaign — nothing to move",
                "added": 0, "removed": 0, **plan}
    key = _sl_key()
    added = removed = 0
    results = []
    for name, cid in cids.items():
        base = f"https://server.smartlead.ai/api/v1/campaigns/{cid}/email-accounts"

        def _call(method, ids):
            for _ in range(4):
                r = requests.request(method, base, params={"api_key": key},
                                     json={"email_account_ids": ids}, timeout=60)
                if r.status_code != 429:
                    return r.status_code
                time.sleep(20)
            return 429
        a = _call("POST", [reserve_account_id])       # add new first
        d = _call("DELETE", [old_id])                 # then remove burned
        if a == 200:
            added += 1
        if d == 200:
            removed += 1
        results.append({"campaign": name, "id": cid, "add_http": a, "remove_http": d})
    try:
        store.log_monitor_event("health_swap_campaign", {
            "old_email": old_email, "reserve_account_id": reserve_account_id,
            "added": added, "removed": removed, "campaigns": list(cids.values())})
    except Exception:
        pass
    return {"added": added, "removed": removed, "results": results, **plan}


def swap_forwarding(old_email: str, reserve_email: str, dry_run: bool = True) -> dict:
    """Point the reserve inbox's domain at the same site the burned inbox's domain
    forwards to (the client's website), so the swapped-in domain doesn't redirect
    prospects to nowhere. Best-effort; never blocks a swap."""
    old_dom = old_email.split("@")[-1] if "@" in old_email else ""
    new_dom = reserve_email.split("@")[-1] if "@" in reserve_email else ""
    if not old_dom or not new_dom:
        return {"ok": False, "note": "missing domain"}
    try:
        import health_offboard as ho
        target = ho.domain_forwarding({old_dom}).get(old_dom)
        if not target:
            return {"ok": False, "note": f"burned domain {old_dom} has no forwarding to copy",
                    "target": None, "new_domain": new_dom}
        if dry_run:
            return {"dry_run": True, "target": target, "new_domain": new_dom, "from_domain": old_dom}
        res = ho.set_domain_forwarding({new_dom}, target)
        return {"ok": res.get("ok"), "target": target, "new_domain": new_dom, "from_domain": old_dom}
    except Exception as e:
        return {"ok": False, "note": str(e)[:120]}


def _is_acquisition(job: dict) -> bool:
    """True if this is one of THT's own outreach inboxes (client == '(acquisition)').
    Acquisition inboxes have no reserve — they must not be swapped with client stock."""
    return "acquisition" in (job.get("client") or "").lower()


def _load() -> dict:
    return store.get_state(STATE_KEY) or {"jobs": []}


def _save(st: dict) -> None:
    store.set_state(STATE_KEY, st)


def _annotate(j: dict) -> dict:
    """Add computed warmup countdown / readiness to a job."""
    ws = j.get("warming_started_at")
    if ws:
        ready = datetime.fromisoformat(ws) + timedelta(days=WARMUP_DAYS)
        j["ready_at"] = ready.strftime("%Y-%m-%d")
        j["days_left"] = max(0, (ready - datetime.now()).days + (1 if ready > datetime.now() else 0))
        j["is_ready"] = datetime.now() >= ready
        if j["status"] == "warming" and j["is_ready"]:
            j["status"] = "ready"
    else:
        j["ready_at"], j["days_left"], j["is_ready"] = None, None, False
    return j


def list_jobs() -> list[dict]:
    return [_annotate(j) for j in _load().get("jobs", [])]


def reserve_summary() -> dict:
    """How many warmed reserve inboxes are ready to deploy right now, broken down
    by niche. Reads generic groups from the overview cache; 'ready' = warmed >=
    WARMUP_DAYS. 'available' subtracts inboxes already claimed by reserved jobs."""
    from collections import Counter
    ov, _ = store.cache_get("overview_v2")
    ready, groups = 0, []
    ready_by = Counter()
    for g in (ov or {}).get("generic_groups", []):
        wd = g.get("warmup_days")
        ads = g.get("account_details", [])
        if ads and wd is not None and wd >= WARMUP_DAYS:
            ready += len(ads)
            groups.append({"name": g.get("name"), "count": len(ads)})
            for ad in ads:
                ready_by[_niche(ad.get("email", ""))] += 1
    claimed = 0
    claimed_by = Counter()
    for j in _load().get("jobs", []):
        if j["status"] == "reserved":
            claimed += 1
            claimed_by[_niche(j.get("reserve_email", ""))] += 1
    avail_by = {k: max(0, ready_by.get(k, 0) - claimed_by.get(k, 0))
                for k in ("hvac", "landscaping", "generic")}
    return {"ready": ready, "claimed": claimed,
            "available": max(0, ready - claimed), "groups": groups,
            "ready_by_niche": {k: ready_by.get(k, 0) for k in ("hvac", "landscaping", "generic")},
            "available_by_niche": avail_by}


def pick_reserve_inbox(exclude=None, want_niche=None) -> dict | None:
    """Pick a warmed reserve inbox (email + account id) not already claimed.
    If want_niche is 'hvac'/'landscaping', only pick that niche or 'generic' —
    NEVER cross HVAC<->landscaping. Prefers an exact-niche match so the scarce
    generic pool is conserved for niches that have no exact reserve."""
    exclude = exclude or set()
    ov, _ = store.cache_get("overview_v2")
    cands = []
    for g in (ov or {}).get("generic_groups", []):
        wd = g.get("warmup_days")
        if wd is None or wd < WARMUP_DAYS:
            continue
        for ad in g.get("account_details", []):
            em = ad.get("email")
            if em and em not in exclude and ad.get("id"):
                cands.append({"email": em, "account_id": ad["id"],
                              "group": g.get("name"), "niche": _niche(em)})
    if not cands:
        return None
    gen = [c for c in cands if c["niche"] == "generic"]
    if not want_niche or want_niche == "generic":
        # niche-ambiguous client: prefer a truly generic domain over stamping a
        # landscaping/HVAC brand onto it; fall back to anything if no generic left.
        return gen[0] if gen else cands[0]
    exact = [c for c in cands if c["niche"] == want_niche]
    if exact:
        return exact[0]
    return gen[0] if gen else None   # no compatible reserve (never cross-niche)


def create_jobs(emails: list[str]) -> dict:
    """Flag burned inboxes for replacement (idempotent on active jobs)."""
    st = _load()
    active = {j["old_email"] for j in st["jobs"] if j["status"] in _ACTIVE}
    status_by = {r["email"]: r for r in store.get_health_status_all()}
    made = 0
    now = datetime.now().strftime("%Y-%m-%d")
    next_id = max([j.get("id", 0) for j in st["jobs"]], default=0)
    for email in emails:
        if email in active:
            continue
        r = status_by.get(email, {})
        next_id += 1
        st["jobs"].append({
            "id": next_id,
            "old_email": email,
            "old_domain": r.get("domain", email.split("@")[-1] if "@" in email else ""),
            "client": r.get("client"),
            "group_letter": r.get("group_letter"),
            "campaigns": r.get("campaigns") or [],
            "reason": "; ".join(r.get("reasons") or []) or f"score {r.get('score')}",
            "status": "flagged",
            "new_domain": None,
            "flagged_at": now,
            "warming_started_at": None,
            "swapped_at": None,
        })
        made += 1
    _save(st)
    return {"created": made, "skipped": len(emails) - made}


def advance(job_id: int, action: str, new_domain: str | None = None, confirm: bool = False) -> dict:
    """Move a job forward. action: warm | reserve | swap | cancel.
    For swap on a reserved job, dry_run (confirm=False) returns the SmartLead
    re-tag plan; confirm=True executes the re-tag and finalizes the swap."""
    st = _load()
    job = next((j for j in st["jobs"] if j.get("id") == job_id), None)
    if not job:
        return {"error": "job not found"}

    if action == "warm":
        job["status"] = "warming"
        job["warming_started_at"] = datetime.now().isoformat()
        if new_domain:
            job["new_domain"] = new_domain
    elif action == "reserve":
        # The generic/warming reserve is CLIENT stock — never swap it into an
        # acquisition (THT's own outreach) inbox. We keep no acquisition reserve,
        # so there's nothing to draw from; say so instead of grabbing a client inbox.
        if _is_acquisition(job):
            return {"error": "No spare acquisition inboxes available — THT keeps no "
                             "acquisition reserve. The generic/warming reserve is client "
                             "stock only. Warm or buy a new inbox for acquisition."}
        # NICHE GUARD: a landscaping inbox may only be replaced by landscaping or
        # generic; HVAC only by HVAC or generic. Never cross HVAC<->landscaping.
        want = required_niche(job)
        used = {j.get("reserve_email") for j in st["jobs"] if j.get("reserve_email")}
        pick = pick_reserve_inbox(used, want_niche=want)
        if not pick:
            avail = reserve_summary().get("available_by_niche", {})
            if want in ("hvac", "landscaping"):
                return {"error": f"No warmed reserve compatible with a {want} inbox "
                                 f"(need {want} or generic). Available now — "
                                 f"{want}: {avail.get(want, 0)}, generic: {avail.get('generic', 0)}. "
                                 f"Warm new {want} inboxes or free up a {want} client's reserve."}
            return {"error": "no ready reserve inboxes available - warm a new one instead"}
        job["status"] = "reserved"
        job["reserve_email"] = pick["email"]
        job["reserve_account_id"] = pick["account_id"]
        job["reserve_source"] = pick["group"]
        job["reserve_niche"] = pick["niche"]
        job["want_niche"] = want
        job["reserved_at"] = datetime.now().strftime("%Y-%m-%d")
    elif action == "swap":
        _annotate(job)
        if job["status"] != "reserved" and not job.get("is_ready"):
            return {"error": f"not warmed yet - {job.get('days_left')} day(s) left of the {WARMUP_DAYS}-day warmup"}
        retag = None
        if job.get("reserve_account_id") and job.get("client"):
            import health_smartlead as hsl
            retag = hsl.reassign(job["reserve_account_id"], job.get("reserve_email"),
                                 job["client"], job.get("group_letter") or "A",
                                 dry_run=not confirm)
            job["retag"] = retag
        # campaign membership: add the reserve inbox + remove the burned inbox on
        # every campaign the burned one is in. Re-tag alone doesn't do this.
        camp = swap_campaign_membership(job["old_email"], job.get("reserve_account_id"),
                                        job.get("campaigns") or [], dry_run=not confirm)
        job["campaign_swap"] = camp
        # forwarding: point the reserve domain at the same client site the burned
        # domain forwards to, so the new domain doesn't redirect prospects nowhere
        fwd = swap_forwarding(job["old_email"], job.get("reserve_email", ""), dry_run=not confirm)
        job["forwarding"] = fwd
        # dry-run: show the full plan (re-tag + campaign move + forwarding), don't finalize yet
        if not confirm and retag is not None and not retag.get("error"):
            _save(st)
            return {"ok": True, "dry_run": True, "job": _annotate(job),
                    "retag": retag, "campaign_swap": camp, "forwarding": fwd}
        job["status"] = "swapped"
        job["swapped_at"] = datetime.now().strftime("%Y-%m-%d")
        _save(st)
        return {"ok": True, "job": _annotate(job), "retag": retag,
                "campaign_swap": camp, "forwarding": fwd}
    elif action == "cancel":
        job["status"] = "cancelled"
    else:
        return {"error": f"unknown action {action}"}
    _save(st)
    return {"ok": True, "job": _annotate(job)}
