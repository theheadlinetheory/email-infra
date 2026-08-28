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


def _camps_of(r) -> list:
    """Return a health-status row's `campaigns` as a real list. get_health_status_all
    leaves `campaigns` as a JSON STRING (it only de-serialises reasons/subscores), so
    iterating it raw walks CHARACTERS — an inbox then looks like it's in no active
    campaign, silently turning acquisition reallocation into a no-op (Tim 2026-08-28:
    "no active campaign to reallocate" for every burnt acquisition domain)."""
    c = r.get("campaigns")
    if isinstance(c, str):
        import json
        try:
            c = json.loads(c)
        except Exception:
            c = [c] if c.strip() else []
    return c if isinstance(c, list) else []


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
    camp_names = set()
    for cl in (ov or {}).get("clients", []):
        if cl.get("name") != client_name:
            continue
        for L in ("a", "b"):
            for ad in (cl.get(f"group_{L}") or {}).get("account_details", []):
                n = _niche(ad.get("email", ""))
                if n != "generic":
                    c[n] += 1
                for cn in (ad.get("campaign_names") or []):
                    camp_names.add(cn)
    # The campaign's BUSINESS-NAME prefix (before '#') is the strongest identity
    # signal — 'Denair HVAC Inc. #1 - ...' -> hvac even though Denair's domains are
    # landscaping. Only the prefix is read, so a landscaping client whose campaign
    # TARGETS hvac leads ('... - HVAC Contractors') isn't misclassified.
    for cn in camp_names:
        n = _niche(cn.split("#")[0])
        if n != "generic":
            return n
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
    """Campaign name -> id for the given names. Reuses the resilient, memoised
    campaign_index() (last-good fallback) so a rate-limited fetch can't silently
    resolve nothing and make a swap a no-op."""
    if not names:
        return {}
    idx = campaign_index()
    return {n: idx[n]["id"] for n in names if n in idx and idx[n].get("id")}


REALLOC_KEY = "reallocate_pending_campaigns"


SL_CAMPAIGN_URL = "https://app.smartlead.ai/app/email-campaign/{id}/analytics"


_CI_CACHE = {"data": None, "ts": 0.0}      # in-process memo + last-good fallback
_CI_STATE_KEY = "campaign_index_cache"


def campaign_index() -> dict:
    """Live SmartLead campaigns as {name: {"id", "status"}}.

    RESILIENCE MATTERS HERE: this decides whether an inbox's campaign counts as
    ACTIVE, so a transient failure that returned {} makes EVERY campaign look
    inactive — which silently turns reallocation into a no-op ("no active campaign
    to reallocate", 2026-08-28 with rate-limit storms). So never blank out all
    statuses on one failed fetch:
      (a) memo the result for 30s so the several calls in one reallocate flow do
          ONE fetch (also easier on the 800/min cap), and
      (b) on failure fall back to the last good result (in-process, then persisted
          in `state`) instead of an empty map."""
    import time
    import requests
    now = time.time()
    if _CI_CACHE["data"] and (now - _CI_CACHE["ts"] < 30):
        return _CI_CACHE["data"]
    key = _sl_key()
    if not key:
        return _CI_CACHE["data"] or {}
    backoff = 8
    for _ in range(5):
        try:
            r = requests.get("https://server.smartlead.ai/api/v1/campaigns",
                             params={"api_key": key}, timeout=60)
            if r.status_code == 200 and r.text.strip():
                idx = {c.get("name"): {"id": c.get("id"),
                                       "status": (c.get("status") or "").upper()}
                       for c in (r.json() or []) if c.get("name")}
                if idx:
                    _CI_CACHE["data"], _CI_CACHE["ts"] = idx, time.time()
                    try:
                        store.set_state(_CI_STATE_KEY, idx)
                    except Exception:
                        pass
                    return idx
            if r.status_code == 429:                 # rate limited — back off harder
                time.sleep(backoff); backoff = min(backoff * 2, 40); continue
        except requests.RequestException:
            pass
        time.sleep(5)
    # transient failure — reuse the last good statuses, NEVER an empty map
    if _CI_CACHE["data"]:
        return _CI_CACHE["data"]
    try:
        cached = store.get_state(_CI_STATE_KEY)
        if cached:
            _CI_CACHE["data"], _CI_CACHE["ts"] = cached, time.time()
            return cached
    except Exception:
        pass
    return {}


def campaign_status_map() -> dict:
    """Live SmartLead campaign full-name -> STATUS (uppercase). Used to tell
    actively-sending (ACTIVE) campaigns from paused/completed ones."""
    return {n: v["status"] for n, v in campaign_index().items()}


def _add_pending_reallocate(names) -> None:
    """Remember campaigns that had a sender swap so the UI can list which still
    need SmartLead's manual 'Reallocate mailboxes' click (no API for that step)."""
    names = [n for n in (names or []) if n]
    if not names:
        return
    st = store.get_state(REALLOC_KEY) or {"campaigns": []}
    cur = set(st.get("campaigns") or [])
    cur.update(names)
    store.set_state(REALLOC_KEY, {"campaigns": sorted(cur)})


def reallocation_campaigns() -> dict:
    """Campaigns awaiting a manual SmartLead 'Reallocate mailboxes' after swaps.

    ONLY ACTIVE campaigns are returned: 'Reallocate mailboxes' redistributes a
    campaign's LIVE lead queue onto its senders — a paused or completed campaign
    isn't sending, so a reallocate there is a no-op. Including them just piled up
    stale noise (paused/completed clients that never clear) and buried the few
    live campaigns that actually need the click. Also self-prunes the stored
    state to live ACTIVE campaigns so it can't grow unbounded."""
    names = (store.get_state(REALLOC_KEY) or {}).get("campaigns", [])
    if not names:
        return {"campaigns": [], "count": 0}
    idx = campaign_index()
    rows, keep = [], []
    for n in names:
        st = idx.get(n, {}).get("status")
        if st != "ACTIVE":
            continue  # gone from SmartLead, paused, or completed → no reallocate needed
        keep.append(n)
        cid = idx[n]["id"]
        rows.append({"name": n, "status": "ACTIVE", "id": cid,
                     "url": SL_CAMPAIGN_URL.format(id=cid) if cid else None})
    if len(keep) != len(names):                      # prune stale entries once
        try:
            store.set_state(REALLOC_KEY, {"campaigns": sorted(keep)})
        except Exception:
            pass                                     # display filter still applies
    rows.sort(key=lambda r: r["name"])
    return {"campaigns": rows, "count": len(rows)}


def clear_reallocation_campaign(name) -> dict:
    """Tim ticks a campaign off once he's hit Reallocate on it in SmartLead."""
    st = store.get_state(REALLOC_KEY) or {"campaigns": []}
    cur = [n for n in (st.get("campaigns") or []) if n != name]
    store.set_state(REALLOC_KEY, {"campaigns": cur})
    return {"ok": True, "remaining": len(cur)}


def acq_reserve_candidates(status_map: dict | None = None) -> list[dict]:
    """Idle acquisition inboxes usable as acquisition reserve: THT acquisition
    inboxes that are healthy/watch and sit in NO active campaign (a paused/completed
    campaign, or none) — real, unused capacity we can swap into an active campaign.
    Returns [{email, account_id}]. This is why acquisition isn't 'cancel only'."""
    status_map = status_map if status_map is not None else campaign_status_map()
    ov, _ = store.cache_get("overview_v2")
    id_by = {}
    for g in (ov or {}).get("acquisition_groups", []):
        for ad in g.get("account_details", []):
            if ad.get("email") and ad.get("id"):
                id_by[ad["email"]] = ad["id"]
    out = []
    for r in store.get_health_status_all():
        if "acquisition" not in (r.get("client") or "").lower():
            continue
        if r.get("status") not in ("healthy", "watch"):
            continue
        camps = _camps_of(r)
        if any(status_map.get(c) == "ACTIVE" for c in camps):
            continue
        aid = id_by.get(r["email"])
        if aid:
            # keep its current (paused/completed) campaigns so we can detach it from
            # them when it's swapped in — never leave a reserve in two campaigns
            out.append({"email": r["email"], "account_id": aid, "campaigns": camps})
    return out


def remove_account_from_campaigns(account_id, campaign_names, dry_run: bool = False) -> dict:
    """DELETE a single email account from each named campaign. Used to detach an
    idle reserve inbox from its old paused/completed campaign(s) when it's reused, so
    resuming that campaign never leaves the inbox sending from two campaigns at once.
    Does NOT flag these for SmartLead reallocation (they're not actively sending)."""
    import time
    import requests
    if isinstance(campaign_names, str):
        import json
        try:
            campaign_names = json.loads(campaign_names)
        except Exception:
            campaign_names = [campaign_names] if campaign_names.strip() else []
    if not isinstance(campaign_names, list) or not campaign_names:
        return {"removed": 0, "results": []}
    cids = _resolve_campaign_ids(campaign_names)
    if dry_run:
        return {"dry_run": True, "campaigns": [{"name": n, "id": i} for n, i in cids.items()]}
    key = _sl_key()
    removed, results = 0, []
    for name, cid in cids.items():
        base = f"https://server.smartlead.ai/api/v1/campaigns/{cid}/email-accounts"
        code = None
        for _ in range(4):
            r = requests.delete(base, params={"api_key": key},
                                json={"email_account_ids": [account_id]}, timeout=60)
            code = r.status_code
            if code != 429:
                break
            time.sleep(20)
        if code == 200:
            removed += 1
        results.append({"campaign": name, "id": cid, "http": code})
    return {"removed": removed, "results": results}


def swap_campaign_membership(old_email: str, reserve_account_id: int,
                             campaign_names, dry_run: bool = True,
                             force_remove: bool = False) -> dict:
    """Move campaign senders: ADD the reserve inbox, REMOVE the burned inbox, on
    every campaign the burned inbox is in. Re-tagging alone does NOT do this —
    campaign membership is a separate SmartLead association. Add-before-remove so
    the campaign never dips below capacity.

    POSITIVE-REPLY CUSTODY GUARD: the burned inbox is not removed from a campaign
    where it owns a positive reply. A reply lives in the sending mailbox's IMAP
    and is welded to it by message-id, so detaching freezes the thread ("the
    mailbox is disconnected") and no amount of SmartLead reallocation can move it
    — see health_positive. The ADD still happens, so the campaign gets its
    capacity back either way; only the REMOVE is deferred, the operator is
    notified, and releasing the hold performs it. force_remove=True overrides
    (and is never the default path)."""
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
    import health_positive as hp
    added = removed = 0
    results = []
    new_holds = []
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
        if a == 200:
            added += 1

        # --- positive-reply custody guard (before the remove, never after) ---
        threads, inconclusive = [], False
        if not force_remove:
            try:
                threads = hp.owned_positive_threads(old_email, cid)
            except Exception as exc:                  # fail CLOSED — hold, don't detach
                inconclusive = True
                threads = [{"lead_id": None, "lead_email": None, "company": None,
                            "category": f"lookup failed: {exc}"}]
        if threads:
            hold = hp.add_hold(old_email, old_id, cid, name, threads,
                               reserve_account_id=reserve_account_id,
                               reason="positive_reply_lookup_failed" if inconclusive
                                      else "positive_reply")
            new_holds.append(hold)
            results.append({"campaign": name, "id": cid, "add_http": a,
                            "remove_http": None, "held": True,
                            "positive_count": 0 if inconclusive else len(threads),
                            "hold_key": hold["key"],
                            "hold_reason": hold["reason"]})
            continue

        d = _call("DELETE", [old_id])                 # then remove burned
        if d == 200:
            removed += 1
        results.append({"campaign": name, "id": cid, "add_http": a, "remove_http": d,
                        "held": False})

    if new_holds:
        try:
            hp.notify(new_holds)
        except Exception:
            pass                                      # the hold itself is what matters
    try:
        store.log_monitor_event("health_swap_campaign", {
            "old_email": old_email, "reserve_account_id": reserve_account_id,
            "added": added, "removed": removed, "held": len(new_holds),
            "campaigns": list(cids.values())})
    except Exception:
        pass
    # record campaigns touched so the UI can list which need SmartLead reallocation
    _add_pending_reallocate([r["campaign"] for r in results
                             if r.get("add_http") == 200 or r.get("remove_http") == 200])
    return {"added": added, "removed": removed, "results": results,
            "held": len(new_holds), "holds": new_holds,
            "positive_threads_protected": sum(h.get("positive_count", 0) for h in new_holds),
            **plan}


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
    # A reserve inbox is spent once a job has claimed it — whether the job is still
    # 'reserved' or already 'swapped'. Until the next sync re-tags it out of its
    # generic group, it still appears in the cache, so exclude it BY EMAIL. This is
    # self-correcting: post-sync it leaves the generic group and the exclusion is a
    # no-op (no double count).
    claimed_emails = {j.get("reserve_email") for j in _load().get("jobs", [])
                      if j.get("reserve_email") and j.get("status") in ("reserved", "swapped")}
    ready, available, groups = 0, 0, []
    ready_by, avail_by = Counter(), Counter()
    for g in (ov or {}).get("generic_groups", []):
        wd = g.get("warmup_days")
        ads = g.get("account_details", [])
        if not (ads and wd is not None and wd >= WARMUP_DAYS):
            continue
        groups.append({"name": g.get("name"), "count": len(ads)})
        for ad in ads:
            em = ad.get("email", "")
            nic = _niche(em)
            ready += 1
            ready_by[nic] += 1
            if em not in claimed_emails:
                available += 1
                avail_by[nic] += 1
    return {"ready": ready, "claimed": len(claimed_emails), "available": available,
            "groups": groups,
            "ready_by_niche": {k: ready_by.get(k, 0) for k in ("hvac", "landscaping", "generic")},
            "available_by_niche": {k: avail_by.get(k, 0) for k in ("hvac", "landscaping", "generic")}}


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
        # retire the burned inbox: detach it from every campaign (not just the
        # ones snapshotted on the job) and strip its client tags, so a campaign
        # built later from this client's group can't recruit it again. The
        # SmartLead account itself is deleted later, once Zapmail has actually
        # removed the mailbox (purge_removed_accounts).
        try:
            job["retire"] = retire_inbox(job["old_email"], campaigns=job.get("campaigns"),
                                         delete=False, dry_run=False)
        except Exception as e:                       # never fail a good swap on cleanup
            job["retire"] = {"error": f"{type(e).__name__}: {e}"}
        _save(st)
        return {"ok": True, "job": _annotate(job), "retag": retag,
                "campaign_swap": camp, "forwarding": fwd, "retire": job["retire"]}
    elif action == "cancel":
        job["status"] = "cancelled"
    else:
        return {"error": f"unknown action {action}"}
    _save(st)
    return {"ok": True, "job": _annotate(job)}


def _burned_for_client(client: str) -> list[str]:
    """Emails of a client's currently-burned inboxes that DON'T already have a
    replacement job. A swapped inbox still shows 'burned' until the next snapshot
    re-scores it (it's out of the campaign but its status is stale) — without this
    guard a second run would replace it AGAIN, wasting reserve and over-provisioning
    the campaign. Cancelled jobs don't count (that inbox is fair game again)."""
    handled = {j["old_email"] for j in _load().get("jobs", [])
               if j.get("status") != "cancelled"}
    return [r["email"] for r in store.get_health_status_all()
            if r.get("status") == "burned" and (r.get("client") or "") == client
            and r["email"] not in handled]


def _run_swaps(emails: list[str]) -> dict:
    """Core reallocation loop: flag -> assign niche-matched reserve -> swap
    (re-tag + campaign add/remove + forwarding) for each email. Assumes reserve
    sufficiency was already checked by the caller."""
    create_jobs(emails)
    want_set = set(emails)
    swapped, failed, old_to_cancel, reserve_used, held = 0, [], [], [], []
    for j in [x for x in list_jobs() if x.get("old_email") in want_set
              and x["status"] in ("flagged", "reserved")]:
        r1 = advance(j["id"], "reserve")
        if r1.get("error"):
            failed.append({"email": j["old_email"], "stage": "reserve", "error": r1["error"]})
            continue
        r2 = advance(j["id"], "swap", confirm=True)
        if r2.get("error"):
            failed.append({"email": j["old_email"], "stage": "swap", "error": r2["error"]})
            continue
        swapped += 1
        holds = (r2.get("campaign_swap") or {}).get("holds") or []
        held += holds
        # An inbox still holding live conversations must NOT be queued for cancel —
        # cancelling deletes the Zapmail mailbox and destroys the very threads the
        # hold just protected. It becomes cancel-eligible once the hold is released.
        if not holds:
            old_to_cancel.append(j["old_email"])
        reserve_used.append((r1.get("job", {}) or {}).get("reserve_email"))
    return {"swapped": swapped, "failed": failed, "held": held,
            "old_to_cancel": old_to_cancel, "reserve_used": reserve_used}


def _run_acq_swaps(emails, cands, status_by, status_map) -> dict:
    """Reallocate acquisition inboxes using idle acquisition reserve — add the idle
    inbox + remove the burned one on each ACTIVE campaign. No re-tag/forwarding: an
    idle acquisition inbox is already THT-branded and acquisition-tagged."""
    swapped, failed, old_to_cancel, reserve_used, held = 0, [], [], [], []
    pool = list(cands)
    for e in emails:
        r = status_by.get(e, {})
        active = [c for c in _camps_of(r) if status_map.get(c) == "ACTIVE"]
        if not active:
            # nothing actively sending — no reallocation needed; it's cancel-ready
            failed.append({"email": e, "stage": "noop",
                           "error": "not in an active campaign — cancel directly, no reallocation"})
            continue
        if not pool:
            failed.append({"email": e, "stage": "reserve", "error": "no idle acquisition reserve left"})
            continue
        pick = pool.pop(0)
        camp = swap_campaign_membership(e, pick["account_id"], active, dry_run=False)
        if camp.get("error"):
            failed.append({"email": e, "stage": "swap", "error": camp["error"]})
            pool.insert(0, pick)
            continue
        # detach the reused idle inbox from its OWN old paused/completed campaign(s)
        # so it's never a member of two campaigns if the paused one is resumed later.
        detached = 0
        if pick.get("campaigns"):
            det = remove_account_from_campaigns(pick["account_id"], pick["campaigns"])
            detached = det.get("removed", 0)
        swapped += 1
        holds = camp.get("holds") or []
        held += holds
        # held == still the custodian of a live conversation; not cancel-eligible
        # until released, or the Zapmail delete destroys the thread.
        if not holds:
            old_to_cancel.append(e)
        reserve_used.append({"email": pick["email"], "detached_from": detached})
    return {"swapped": swapped, "failed": failed, "held": held,
            "old_to_cancel": old_to_cancel, "reserve_used": reserve_used}


def reallocate_emails(emails: list[str], confirm: bool = False) -> dict:
    """Reallocate an explicit set of burned inboxes (any client/domain/acquisition)
    — remove each from its ACTIVE campaign(s) and swap in compatible reserve. Drives
    the domain-priority view's 'Reallocate' button (frontend, no Claude session).

    Client inboxes draw niche-matched client reserve. Acquisition inboxes draw
    'idle acquisition reserve' — THT inboxes sitting in paused/completed campaigns
    (real unused capacity), so acquisition is reserve-driven, never 'cancel only'.
    Dry-run reports reserve sufficiency for both."""
    handled = {j["old_email"] for j in _load().get("jobs", [])
               if j.get("status") != "cancelled"}
    emails = [e for e in emails if e and e not in handled]
    if not emails:
        return {"dry_run": not confirm, "error": "nothing to reallocate (all already have jobs)",
                "reallocatable": 0}

    status_by = {r["email"]: r for r in store.get_health_status_all()}
    status_map = campaign_status_map()

    def _is_acq(e):
        return "acquisition" in (status_by.get(e, {}).get("client") or "").lower()

    acq_emails = [e for e in emails if _is_acq(e)]
    client_emails = [e for e in emails if not _is_acq(e)]

    # --- client reserve, per niche (exact + generic) ---
    rs = reserve_summary().get("available_by_niche", {})
    per_niche: dict[str, list] = {}
    for e in client_emails:
        n = required_niche({"old_email": e, "client": status_by.get(e, {}).get("client")})
        per_niche.setdefault(n, []).append(e)
    gen_avail = rs.get("generic", 0) + rs.get("landscaping", 0) + rs.get("hvac", 0)
    need_report, client_ok = {}, True
    for n, es in per_niche.items():
        pool = gen_avail if n == "generic" else rs.get(n, 0) + rs.get("generic", 0)
        need_report[n] = {"need": len(es), "reserve": pool, "enough": pool >= len(es)}
        if pool < len(es):
            client_ok = False

    # --- acquisition reserve (idle acquisition capacity) ---
    acq_cands = acq_reserve_candidates(status_map)
    acq_report = {"need": len(acq_emails), "reserve": len(acq_cands),
                  "enough": len(acq_cands) >= len(acq_emails)}
    acq_ok = acq_report["enough"] or not acq_emails

    plan = {"reallocatable": len(client_emails) + len(acq_emails),
            "by_niche": need_report, "acquisition": acq_report,
            "enough": client_ok and acq_ok,
            "emails": client_emails + acq_emails}
    if not confirm:
        return {"dry_run": True, **plan}
    if not plan["emails"]:
        return {"error": "nothing to reallocate", **plan}
    errs = []
    if client_emails and not client_ok:
        errs.append("not enough client reserve")
    if acq_emails and not acq_ok:
        errs.append("not enough idle acquisition reserve (warm/free more acquisition inboxes)")
    if errs:
        return {"error": "; ".join(errs), **plan}

    _empty = {"swapped": 0, "failed": [], "held": [], "old_to_cancel": [], "reserve_used": []}
    res_c = _run_swaps(client_emails) if client_emails else dict(_empty)
    res_a = (_run_acq_swaps(acq_emails, acq_cands, status_by, status_map) if acq_emails
             else dict(_empty))
    swapped = res_c["swapped"] + res_a["swapped"]
    failed = res_c["failed"] + res_a["failed"]
    held = (res_c.get("held") or []) + (res_a.get("held") or [])
    protected = sum(h.get("positive_count", 0) for h in held)
    try:
        store.log_monitor_event("health_reallocate", {"swapped": swapped,
                                                      "failed": len(failed),
                                                      "held": len(held)})
    except Exception:
        pass
    note = (f"Reallocated {swapped} inbox(es). Now hit 'Reallocate mailboxes' once per "
            f"campaign listed below in SmartLead, then the burned domains are safe to cancel.")
    if held:
        note += (f" ⚠ {len(held)} inbox(es) were KEPT in their campaign because they own "
                 f"{protected} positive repl(y/ies) — they are excluded from the cancel list. "
                 f"Work the replies, then release them in 'Held: positive replies'.")
    # Scope the "go reallocate these" list to THIS run's swaps only — the inboxes we
    # actually moved and the ACTIVE campaigns they were in. Returning the GLOBAL
    # reallocation_campaigns() here surfaced unrelated CLIENT campaigns from past swaps
    # under an acquisition domain that reallocated nothing (Tim spotted 2026-08-25).
    idx = campaign_index()
    swapped_emails = set(res_c["old_to_cancel"]) | set(res_a["old_to_cancel"])
    touched = set()
    for e in swapped_emails:
        for c in _camps_of(status_by.get(e, {})):
            if idx.get(c, {}).get("status") == "ACTIVE":
                touched.add(c)
    this_run = sorted(
        [{"name": n, "status": "ACTIVE", "id": idx[n]["id"],
          "url": SL_CAMPAIGN_URL.format(id=idx[n]["id"]) if idx[n].get("id") else None}
         for n in touched if n in idx],
        key=lambda r: r["name"])
    return {"ok": True, "swapped": swapped, "failed": failed,
            "held": held, "held_count": len(held), "positive_threads_protected": protected,
            "old_to_cancel": res_c["old_to_cancel"] + res_a["old_to_cancel"],
            "reserve_used": res_c["reserve_used"] + res_a["reserve_used"],
            "campaigns_to_reallocate": this_run,
            "note": note}


def replace_all_burned(client: str, confirm: bool = False) -> dict:
    """One-shot: for every burned inbox of a client, flag -> assign niche-matched
    reserve -> swap (re-tag + campaign add/remove + forwarding). Dry-run reports
    the plan + whether there's enough compatible reserve."""
    emails = _burned_for_client(client)
    want = required_niche({"old_email": emails[0], "client": client}) if emails else "generic"
    rs = reserve_summary().get("available_by_niche", {})
    pool = rs.get(want, 0) + (rs.get("generic", 0) if want != "generic" else 0)
    if want == "generic":
        pool = rs.get("generic", 0) + rs.get("landscaping", 0) + rs.get("hvac", 0)
    plan = {"client": client, "burned": len(emails), "niche": want,
            "reserve_compatible": pool, "enough": pool >= len(emails), "emails": emails}
    if not confirm:
        return {"dry_run": True, **plan}
    if not emails:
        return {"error": "no burned inboxes for this client", **plan}
    if pool < len(emails):
        return {"error": f"only {pool} compatible reserve inboxes for {len(emails)} burned "
                         f"({want} or generic) — warm more or free up a {want} client", **plan}

    res = _run_swaps(emails)
    swapped, failed = res["swapped"], res["failed"]
    held = res.get("held") or []
    protected = sum(h.get("positive_count", 0) for h in held)
    try:
        store.log_monitor_event("health_replace_all", {
            "client": client, "swapped": swapped, "failed": len(failed),
            "held": len(held)})
    except Exception:
        pass
    note = (f"Swapped {swapped} inbox(es). Now hit 'Reallocate mailboxes' once on "
            f"{client}'s campaign in SmartLead, then cancel the old inboxes.")
    if held:
        note += (f" ⚠ {len(held)} kept in campaign — they own {protected} positive "
                 f"repl(y/ies) and are NOT in the cancel list.")
    return {"ok": True, "client": client, "swapped": swapped, "failed": failed,
            "held": held, "held_count": len(held), "positive_threads_protected": protected,
            "reserve_used": res["reserve_used"], "old_to_cancel": res["old_to_cancel"],
            "niche": want, "note": note}


# ---------------------------------------------------------------------------
# Retiring a burned inbox
#
# A replacement used to only push the RESERVE inbox in; the burned one was left
# behind wearing the client's group tag and still attached to whatever campaigns
# it happened to be in. Two failure modes followed, both seen live on
# 2026-08-18:
#   1. New campaigns built from the client's group re-recruited the dead inbox —
#      7 of 9 affected campaigns were created AFTER the swap.
#   2. Zapmail cancels are per-DOMAIN, so cancelling a domain killed mailboxes
#      that were still senders on a live campaign, and SmartLead kept retrying
#      their dead OAuth token forever ("needs attention", 75 accounts).
#
# retire_inbox() closes both: strip the client tags + detach from every campaign
# the moment it's replaced, then delete the SmartLead account once the mailbox
# is actually gone at Zapmail (purge_removed_accounts, called by the removal
# watcher). Deleting only at that point keeps replies to already-sent mail
# reachable for as long as the mailbox still exists.
# ---------------------------------------------------------------------------

def _fleet_campaigns(email: str) -> list:
    """Campaign names the last snapshot saw this inbox in (the fleet row's list
    can arrive JSON-serialized — normalise it)."""
    data, _ = store.cache_get("health_fleet")
    for r in (data or {}).get("inboxes", []):
        if (r.get("email") or "").lower() == (email or "").lower():
            c = r.get("campaigns") or []
            if isinstance(c, str):
                import json
                try:
                    c = json.loads(c)
                except Exception:
                    c = [c] if c.strip() else []
            return c if isinstance(c, list) else []
    return []


def _split_by_positive_custody(email: str, account_id, names: list) -> tuple:
    """Partition campaign names into (safe_to_detach, holds).

    Same rule as swap_campaign_membership: an inbox that owns a positive reply in
    a campaign stays attached there, because the thread lives in its mailbox and
    nothing can move it. Fails CLOSED — an inconclusive lookup holds."""
    import health_positive as hp
    cids = _resolve_campaign_ids(names)
    safe, holds = [], []
    for name, cid in cids.items():
        try:
            threads = hp.owned_positive_threads(email, cid)
            reason = "positive_reply"
        except Exception as exc:
            threads = [{"lead_id": None, "lead_email": None, "company": None,
                        "category": f"lookup failed: {exc}"}]
            reason = "positive_reply_lookup_failed"
        if threads:
            holds.append(hp.add_hold(email, account_id, cid, name, threads, reason=reason))
        else:
            safe.append(name)
    # a name we couldn't resolve to a live campaign can't be checked OR detached
    safe += [n for n in names if n not in cids]
    return safe, holds


def retire_inbox(email: str, campaigns=None, delete: bool = False,
                 account_id=None, dry_run: bool = True,
                 force_remove: bool = False) -> dict:
    """Take a burned inbox fully out of service.

      1. detach it from every campaign we know it's in (job list + last snapshot)
      2. strip its client / group tags so no future campaign can recruit it
      3. delete the SmartLead account, if `delete` (only once the mailbox is gone)

    Safe to re-run: each step no-ops when there's nothing left to do.

    Step 1 honours the POSITIVE-REPLY CUSTODY GUARD — advance(swap) calls this
    right after swap_campaign_membership, so without the same check here a held
    inbox would simply be detached a moment later and the guard would be a no-op.
    Step 2 still runs on a held inbox (stripping tags stops re-recruitment and
    costs nothing), but step 3 is refused outright: deleting the account destroys
    every conversation it holds, permanently."""
    import health_smartlead as hsl
    acct_id = account_id or _overview_account_id(email)
    names = list(dict.fromkeys(list(campaigns or []) + _fleet_campaigns(email)))
    out = {"email": email, "account_id": acct_id, "campaigns": names, "delete": delete}
    if not acct_id:
        return {"error": f"could not resolve a SmartLead account id for {email}", **out}
    if dry_run:
        return {"dry_run": True, **out}

    detach, holds = (names, [])
    if not force_remove and names:
        detach, holds = _split_by_positive_custody(email, acct_id, names)
    if holds:
        out["held"] = holds
        out["held_campaigns"] = [h["campaign"] for h in holds]
        out["positive_threads_protected"] = sum(h.get("positive_count", 0) for h in holds)

    out["detached"] = remove_account_from_campaigns(acct_id, detach, dry_run=False)
    out["untag"] = hsl.untag_client(acct_id, email, dry_run=False)
    if delete and holds and not force_remove:
        out["deleted"] = {"ok": False,
                          "error": "refused — inbox still holds positive replies; "
                                   "deleting it destroys those threads permanently"}
    elif delete:
        out["deleted"] = hsl.delete_account(acct_id, email, dry_run=False)
    out["ok"] = not out["untag"].get("error") and (not delete or out["deleted"].get("ok"))
    return out


MAX_AUTO_PURGE = 25


def purge_removed_accounts(emails, dry_run: bool = True, force: bool = False) -> dict:
    """Delete the SmartLead accounts of mailboxes that no longer exist at Zapmail.

    Called by the removal watcher when it sees a mailbox actually disappear. Until
    this ran, a cancelled mailbox sat in SmartLead failing its OAuth refresh
    forever — that is exactly the "needs attention" backlog (75 accounts, every
    single one already cancelled).

    Deletion is irreversible and the trigger is a snapshot diff, so an unusually
    large batch is treated as a broken inventory scan rather than a real mass
    cancellation: past MAX_AUTO_PURGE it refuses and asks for `force`. (The
    inventory only covers Zapmail's GOOGLE provider — an OUTLOOK-side change
    could otherwise read as hundreds of removals.)"""
    import health_smartlead as hsl
    emails = list(emails or [])
    if len(emails) > MAX_AUTO_PURGE and not force and not dry_run:
        return {"deleted": 0, "skipped_too_many": len(emails), "limit": MAX_AUTO_PURGE,
                "error": f"{len(emails)} accounts is more than MAX_AUTO_PURGE "
                         f"({MAX_AUTO_PURGE}) — refusing to bulk-delete automatically. "
                         f"Verify the Zapmail inventory scan, then re-run with force."}
    done, missing, failed = [], [], []
    for e in emails or []:
        acct_id = _overview_account_id(e)
        if not acct_id:
            missing.append(e)           # already gone from SmartLead, or not synced yet
            continue
        if dry_run:
            done.append({"email": e, "account_id": acct_id, "dry_run": True})
            continue
        # strip tags first so a failed delete still can't be re-recruited
        hsl.untag_client(acct_id, e, dry_run=False)
        res = hsl.delete_account(acct_id, e, dry_run=False)
        (done if res.get("ok") else failed).append({"email": e, "account_id": acct_id, **res})
    return {"deleted": len(done), "not_in_smartlead": len(missing),
            "failed": len(failed), "details": done, "errors": failed,
            **({"dry_run": True} if dry_run else {})}
