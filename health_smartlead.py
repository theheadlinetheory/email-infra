"""SmartLead re-tag — move an inbox from one group to a client's group.

This is what makes "assign reserve" / "swap" actually change SmartLead: it
re-tags an inbox via the internal GraphQL + save-management-details path (the
same mechanism the repo's assign-generic-to-client uses at group level).

HARD LIMITS — SmartLead exposes no public API for this:
  * Auth is the browser SMARTLEAD_JWT, which EXPIRES. Unattended runs need it
    refreshed (grab from app.smartlead.ai devtools). If it's stale, re-tag fails
    loudly rather than silently.
  * After re-tagging you must still hit "Reallocate" on the campaign in the
    SmartLead UI — there is no API for reallocation. We return a reminder.

reassign(dry_run=True) resolves the plan and changes nothing (default).
"""

from __future__ import annotations

import os
import re

import db as store

GQL = (os.environ.get("SMARTLEAD_GQL", "") or "https://fe-gql.smartlead.ai/v1/graphql")
SL_INTERNAL = "https://server.smartlead.ai/api"
ZAPMAIL_TAG_ID = 262254
_DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$")


_LOGIN_URL = "https://server.smartlead.ai/api/auth/login"
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_token_cache = {"jwt": "", "ts": 0}


def _mint_jwt() -> str:
    """Log into SmartLead and return a fresh JWT (auto-refresh).
    Needs SMARTLEAD_LOGIN_EMAIL / SMARTLEAD_LOGIN_PASSWORD env vars."""
    email = os.environ.get("SMARTLEAD_LOGIN_EMAIL", "").strip()
    pw = os.environ.get("SMARTLEAD_LOGIN_PASSWORD", "").strip()
    if not email or not pw:
        return ""
    import requests
    try:
        r = requests.post(_LOGIN_URL, json={"email": email, "password": pw}, timeout=30)
    except Exception:
        return ""
    if r.status_code != 200:
        return ""
    # token may sit under any field name / in a header — grab it by JWT shape
    hay = r.text + " " + " ".join(str(v) for v in r.headers.values())
    m = _JWT_RE.search(hay)
    return m.group(0) if m else ""


def get_jwt(force: bool = False) -> str:
    """Current JWT. Prefers a freshly-minted token (login creds) so it never
    goes stale; falls back to the static SMARTLEAD_JWT env var."""
    import time
    if os.environ.get("SMARTLEAD_LOGIN_EMAIL") and os.environ.get("SMARTLEAD_LOGIN_PASSWORD"):
        if force or not _token_cache["jwt"] or (time.time() - _token_cache["ts"] > 3600):
            fresh = _mint_jwt()
            if fresh:
                _token_cache["jwt"] = fresh
                _token_cache["ts"] = time.time()
        if _token_cache["jwt"]:
            return _token_cache["jwt"]
    return os.environ.get("SMARTLEAD_JWT", "").strip()


def login_diag() -> dict:
    """Safe diagnostic for the status endpoint — no token or password exposed."""
    email = os.environ.get("SMARTLEAD_LOGIN_EMAIL", "").strip()
    pw = os.environ.get("SMARTLEAD_LOGIN_PASSWORD", "").strip()
    if not (email and pw):
        return {"has_login_creds": False}
    import requests
    try:
        r = requests.post(_LOGIN_URL, json={"email": email, "password": pw}, timeout=30)
    except Exception as e:
        return {"has_login_creds": True, "error": str(e)[:120]}
    hay = r.text + " " + " ".join(str(v) for v in r.headers.values())
    return {"has_login_creds": True, "login_http": r.status_code,
            "token_found": bool(_JWT_RE.search(hay))}


def _headers():
    return {"Authorization": f"Bearer {get_jwt()}", "Content-Type": "application/json"}


def _gql(query, variables=None):
    import requests
    r = requests.post(GQL, headers=_headers(),
                      json={"query": query, "variables": variables or {}}, timeout=30)
    return r.json()


def jwt_ok() -> bool:
    """Cheap check that the JWT is still valid for the tag API."""
    try:
        return "tags" in (_gql("{ tags(limit:1){ id } }").get("data") or {})
    except Exception:
        return False


def _norm(n: str) -> str:
    s = (n or "").lower().strip()
    prev = ""
    while prev != s:
        prev = s
        s = re.sub(r"\s+(group|llc|inc\.?|construction|landscaping|lawn\s*care|hvac|"
                   r"land\s*care|scapes|landscape|heating\s*&?\s*air.*|"
                   r"lawn\s*solutions|land\s*solutions|&\s*design|conditioning)\s*$",
                   "", s, flags=re.I)
        s = re.sub(r"[,.\s&]+$", "", s).strip()
    return re.sub(r"\s+", " ", s)


def _find_client_tag(client: str, ab: str):
    tags = _gql("{ tags { id name color } }").get("data", {}).get("tags", [])
    suffix = f" {ab.upper()}"
    cn = _norm(client)
    for t in tags:
        nm = t["name"]
        if nm.upper().endswith(suffix) and _norm(nm[:-len(suffix)].strip()) == cn:
            return t["id"], nm, tags
    return None, f"{client} Group {ab.upper()}", tags


def _account_tags(account_id):
    q = ("{ email_account_tag_mappings(where:{email_account_id:{_eq: %d}}){ tag{id name} } }"
         % int(account_id))
    return [m["tag"] for m in (_gql(q).get("data", {}).get("email_account_tag_mappings") or [])]


def _create_tag(name, existing):
    used = {t.get("color") for t in existing}
    palette = ["#FF6B6B", "#FFA94D", "#FFD43B", "#51CF66", "#20C997",
               "#339AF0", "#5C7CFA", "#BE4BDB", "#E64980"]
    color = next((c for c in palette if c not in used), "#D0FCB1")
    res = _gql("mutation($o: tags_insert_input!){ insert_tags_one(object:$o){ id name } }",
               {"o": {"name": name, "color": color}})
    return (res.get("data", {}).get("insert_tags_one") or {}).get("id")


def reassign(account_id, email, client, ab="A", dry_run=True) -> dict:
    """Re-tag one inbox to <client> Group <ab>, preserving Zapmail + date tags."""
    if not get_jwt():
        return {"error": "no SmartLead token — set SMARTLEAD_JWT, or SMARTLEAD_LOGIN_EMAIL/PASSWORD for auto-refresh"}
    tag_id, tag_name, all_tags = _find_client_tag(client, ab)
    cur = _account_tags(account_id)
    date_tag = next((t["id"] for t in cur if _DATE_RE.match(t.get("name", ""))), None)
    plan = {
        "account_id": account_id, "email": email,
        "target": tag_name, "target_exists": bool(tag_id),
        "current_tags": [t.get("name") for t in cur],
        "reallocate_reminder": "Re-tagged in SmartLead — now hit Reallocate on the campaign (no API for that step).",
    }
    if dry_run:
        return {"dry_run": True, **plan}

    if not tag_id:
        tag_id = _create_tag(tag_name, all_tags)
        if not tag_id:
            return {"error": f"could not create tag '{tag_name}'", **plan}
    tag_ids = [ZAPMAIL_TAG_ID, tag_id] + ([date_tag] if date_tag else [])

    import requests
    def _post():
        return requests.post(f"{SL_INTERNAL}/email-account/save-management-details",
                             headers=_headers(), json={"id": int(account_id), "tags": tag_ids}, timeout=30)
    r = _post()
    if r.status_code in (401, 403):
        get_jwt(force=True)   # token went stale — mint a fresh one and retry once
        r = _post()
    ok = r.status_code == 200
    try:
        store.log_monitor_event("smartlead_retag", {
            "email": email, "account_id": account_id, "to": tag_name, "http": r.status_code})
    except Exception:
        pass
    return {"dry_run": False, "ok": ok, "http": r.status_code, "set_tags": tag_ids, **plan}
