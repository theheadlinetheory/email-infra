"""Free up an off-boarded client's inboxes.

Takes every inbox tagged to a client we've stopped working with, re-tags it into
a fresh `Generic <letter>` group and turns warm-up back on, so the inboxes go
back into the reserve pool and can be assigned to the next client.

SAFETY — this is the whole point of the module:
  An inbox tagged for client X can still be sitting in client Y's ACTIVE
  campaign (it happens when inboxes get reallocated across clients). Freeing
  those would silently kill another client's sending. plan() detects that and
  execute() REFUSES unless the caller explicitly overrides.

Flow: plan() is read-only -> show it -> execute(confirm=True) does the work:
  1. optionally pause the client's own active campaigns
  2. re-tag every inbox to the target Generic group (Zapmail + date tag kept)
  3. re-enable warm-up on each inbox

Mirrors the manual off-boarding run of 2026-07-18 (Borja/Kay's/Tropical/
Coastal/High Southern -> Generic C/D/E/F/H), turned into a repeatable action.
"""

from __future__ import annotations

import os
import re
import string
import time

import requests

import db as store
import health_smartlead as hsl

ZAPMAIL_TAG_ID = 262254
SL_INTERNAL = "https://server.smartlead.ai/api"
PUBLIC_API = "https://server.smartlead.ai/api/v1"
_DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$")
_GROUP_RE = re.compile(r"^(.*)\s+Group\s+([AB])$", re.I)


def _key(name: str) -> str:
    """Normalise a client/tag name for comparison ('Borja Group' -> 'borja')."""
    s = (name or "").lower().strip()
    s = re.sub(r"\s+group\s*$", "", s)
    s = re.sub(r"[,.]+$", "", s).strip()
    return re.sub(r"\s+", " ", s)


def _campaign_key(client_name: str) -> str:
    """First two significant words — used to tell this client's campaigns apart
    from another client's (campaign names rarely repeat the full legal name)."""
    words = _key(client_name).split()
    return " ".join(words[:2]) if words else ""


def _api_key() -> str:
    return (os.environ.get("SMARTLEAD_API_KEY", "") or "").strip()


def _tag_members(tag_id: int) -> list[int]:
    q = ("query($t:Int!){ email_account_tag_mappings(where:{tag_id:{_eq:$t}})"
         "{ email_account_id } }")
    rows = hsl._gql(q, {"t": tag_id}).get("data", {}).get("email_account_tag_mappings", [])
    return [r["email_account_id"] for r in rows]


def _all_tags() -> list[dict]:
    return hsl._gql("{ tags { id name } }").get("data", {}).get("tags", [])


def find_client_tags(client_name: str, tags: list[dict] | None = None) -> list[dict]:
    """Every '<client> Group A/B' tag for this client, with its members."""
    tags = tags if tags is not None else _all_tags()
    key = _key(client_name)
    out = []
    for t in tags:
        m = _GROUP_RE.match(t["name"])
        if not m:
            continue
        base = _key(m.group(1))
        if base == key or base.startswith(key):
            out.append({"tag_id": t["id"], "name": t["name"],
                        "letter": m.group(2).upper(), "account_ids": _tag_members(t["id"])})
    return out


def next_generic_letter(tags: list[dict] | None = None) -> str | None:
    """First unused letter for a new `Generic <letter>` group."""
    tags = tags if tags is not None else _all_tags()
    used = set()
    for t in tags:
        p = t["name"].split()
        if len(p) == 2 and p[0].lower() == "generic" and len(p[1]) == 1:
            used.add(p[1].upper())
    return next((c for c in string.ascii_uppercase if c not in used), None)


def domains_for_accounts(account_ids) -> set:
    """Sending domains behind these SmartLead account ids (from the overview cache)."""
    ov, _ = store.cache_get("overview_v2")
    id2email = {}
    for c in (ov or {}).get("clients", []):
        for L in ("a", "b"):
            for ad in (c.get(f"group_{L}") or {}).get("account_details", []):
                if ad.get("id"):
                    id2email[ad["id"]] = ad.get("email", "")
    for sec in ("generic_groups", "acquisition_groups"):
        for g in (ov or {}).get(sec, []):
            for ad in g.get("account_details", []):
                if ad.get("id"):
                    id2email[ad["id"]] = ad.get("email", "")
    out = set()
    for aid in account_ids:
        em = id2email.get(aid, "")
        if "@" in em:
            out.add(em.split("@")[-1].lower())
    return out


def zapmail_domain_ids(domains: set) -> dict:
    """domain name -> Zapmail domain id (paginated over the whole account)."""
    key = (os.environ.get("ZAPMAIL_API_KEY", "") or "").strip()
    if not key or not domains:
        return {}
    headers = {"Content-Type": "application/json",
               "x-auth-zapmail": key, "x-service-provider": "GOOGLE"}
    want = {d.lower() for d in domains}
    out, page = {}, 1
    while True:
        try:
            r = requests.get(f"https://api.zapmail.ai/api/v2/domains?page={page}&limit=100",
                             headers=headers, timeout=30)
        except requests.RequestException:
            break
        if r.status_code != 200:
            break
        data = r.json().get("data", {})
        for dom in data.get("domains", []):
            nm = (dom.get("domain") or "").lower()
            if nm in want and dom.get("id"):
                out[nm] = dom["id"]
        if page >= data.get("totalPages", 1):
            break
        page += 1
    return out


def set_domain_forwarding(domains, forward_to: str | None) -> dict:
    """Point these sending domains at `forward_to`. Pass '' / None to CLEAR it,
    so a freed domain never keeps redirecting to an ex-client's website."""
    ids = zapmail_domain_ids(set(domains))
    if not ids:
        return {"domains": 0, "ok": False, "note": "no matching Zapmail domains"}
    import setup
    res = setup.zm_set_forwarding(list(ids.values()), forward_to or "")
    ok = str((res or {}).get("status", "")) in ("200", "201") or (res or {}).get("status") in (200, 201)
    return {"domains": len(ids), "forward_to": forward_to or None, "ok": ok, "response": res}


def _campaign_status_map() -> dict:
    try:
        r = requests.get(f"{PUBLIC_API}/campaigns", params={"api_key": _api_key()}, timeout=60)
        if r.status_code != 200:
            return {}
        return {c.get("name", ""): {"id": c.get("id"), "status": c.get("status")}
                for c in (r.json() or [])}
    except requests.RequestException:
        return {}


def _client_campaigns(client_name: str):
    """Split the campaigns this client's inboxes sit in into own vs foreign.
    Foreign + ACTIVE == another client is still sending on these inboxes."""
    ov, _ = store.cache_get("overview_v2")
    ckey = _campaign_key(client_name)
    names: set[str] = set()
    for c in (ov or {}).get("clients", []):
        if _key(c.get("name", "")) != _key(client_name):
            continue
        for L in ("a", "b"):
            for ad in (c.get(f"group_{L}") or {}).get("account_details", []):
                for cn in (ad.get("campaign_names") or []):
                    names.add(cn)
    statuses = _campaign_status_map()
    own, foreign = [], []
    for n in sorted(names):
        info = statuses.get(n, {})
        rec = {"name": n, "status": info.get("status"), "id": info.get("id")}
        (own if ckey and ckey in _key(n) else foreign).append(rec)
    return own, foreign


def plan(client_name: str) -> dict:
    """Read-only preview + safety verdict. Changes nothing."""
    tags = _all_tags()
    ctags = find_client_tags(client_name, tags)
    inboxes = sorted({a for t in ctags for a in t["account_ids"]})
    own, foreign = _client_campaigns(client_name)
    blocking = [c for c in foreign if (c.get("status") or "").upper() == "ACTIVE"]
    own_active = [c for c in own if (c.get("status") or "").upper() == "ACTIVE"]
    return {
        "client": client_name,
        "tags": [{"name": t["name"], "count": len(t["account_ids"])} for t in ctags],
        "inboxes": len(inboxes),
        "target_generic": f"Generic {next_generic_letter(tags)}",
        "own_campaigns": own,
        "own_active": own_active,
        "foreign_campaigns": foreign,
        "blocking": blocking,
        "safe": not blocking and len(inboxes) > 0,
        "reason": ("still sending for another client: "
                   + ", ".join(c["name"][:60] for c in blocking)) if blocking
                  else ("no inboxes tagged to this client" if not inboxes else ""),
    }


def execute(client_name: str, confirm: bool = False, pause_campaigns: bool = True,
            force: bool = False) -> dict:
    """Re-tag the client's inboxes into a fresh Generic group + re-enable warm-up."""
    p = plan(client_name)
    if not confirm:
        return {"dry_run": True, **p}
    if not p["safe"] and not force:
        return {"error": p["reason"] or "not safe to free up", **p}
    if not p["inboxes"]:
        return {"error": "no inboxes tagged to this client", **p}

    tags = _all_tags()
    letter = next_generic_letter(tags)
    gname = f"Generic {letter}"
    gid = next((t["id"] for t in tags if t["name"] == gname), None)
    if gid is None:
        res = hsl._gql("mutation($o: tags_insert_input!){ insert_tags_one(object:$o){ id } }",
                       {"o": {"name": gname, "color": "#D0FCB1"}})
        gid = ((res.get("data") or {}).get("insert_tags_one") or {}).get("id")
    if not gid:
        return {"error": f"could not create {gname}", **p}

    # 1) pause this client's own active campaigns
    paused = []
    if pause_campaigns:
        for c in p["own_active"]:
            if not c.get("id"):
                continue
            try:
                r = requests.post(f"{PUBLIC_API}/campaigns/{c['id']}/status",
                                  params={"api_key": _api_key()},
                                  json={"status": "PAUSED"}, timeout=30)
                if r.status_code == 200:
                    paused.append(c["name"])
            except requests.RequestException:
                pass

    # 2) date tag per account (one bulk pass, not one call per inbox)
    acct_date, off = {}, 0
    while True:
        rows = hsl._gql("{ email_account_tag_mappings(limit:1000, offset:%d)"
                        "{ email_account_id tag{id name} } }" % off) \
                  .get("data", {}).get("email_account_tag_mappings", [])
        for r in rows:
            t = r.get("tag") or {}
            if _DATE_RE.match(t.get("name", "")):
                acct_date[r["email_account_id"]] = t["id"]
        if len(rows) < 1000:
            break
        off += 1000

    # 3) re-tag + re-enable warm-up
    ctags = find_client_tags(client_name, tags)
    ids = sorted({a for t in ctags for a in t["account_ids"]})
    # capture the sending domains BEFORE re-tagging (overview still lists them
    # under this client) so we can strip their forwarding afterwards
    freed_domains = domains_for_accounts(ids)
    retagged = warmed = failed = 0
    import setup
    for aid in ids:
        dt = acct_date.get(aid)
        new_tags = [ZAPMAIL_TAG_ID, gid] + ([dt] if dt else [])
        try:
            r = requests.post(f"{SL_INTERNAL}/email-account/save-management-details",
                              headers=hsl._headers(), json={"id": int(aid), "tags": new_tags},
                              timeout=30)
            if r.status_code in (401, 403):
                hsl.get_jwt(force=True)
                r = requests.post(f"{SL_INTERNAL}/email-account/save-management-details",
                                  headers=hsl._headers(), json={"id": int(aid), "tags": new_tags},
                                  timeout=30)
            if r.status_code == 200:
                retagged += 1
            else:
                failed += 1
        except requests.RequestException:
            failed += 1
        try:
            w = requests.post(setup.sl_url(f"/email-accounts/{aid}/warmup"),
                              json=setup.GOOGLE_WARMUP, timeout=30)
            if w.status_code == 200:
                warmed += 1
        except requests.RequestException:
            pass
        time.sleep(0.3)

    # 4) strip domain forwarding — a freed domain must not keep redirecting
    #    prospects to the ex-client's website (Zapmail forwardTo does NOT follow
    #    a re-tag, so without this it silently points at the old client forever).
    try:
        fwd = set_domain_forwarding(freed_domains, "")
    except Exception as e:
        fwd = {"domains": 0, "ok": False, "note": str(e)[:120]}

    try:
        store.log_monitor_event("client_offboard", {
            "client": client_name, "generic": gname, "retagged": retagged,
            "warmed": warmed, "failed": failed, "paused": paused,
            "forwarding_cleared": fwd.get("domains", 0)})
    except Exception:
        pass
    return {"ok": True, "client": client_name, "generic_group": gname,
            "inboxes": len(ids), "retagged": retagged, "warmed": warmed,
            "failed": failed, "paused_campaigns": paused,
            "forwarding_cleared": fwd.get("domains", 0), "forwarding": fwd,
            "note": (f"Inboxes are back in the reserve as {gname} with warm-up on; "
                     f"forwarding stripped from {fwd.get('domains', 0)} domain(s).")}
