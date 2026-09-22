"""Provisioning a buy-order: Zapmail mailboxes -> SmartLead -> tag -> warm-up.

WHY THIS IS A STEP MACHINE AND NOT A SCRIPT. The work takes 15-60 minutes and
sometimes much longer: DNS propagation is unbounded, Zapmail slot purchases
settle on their own clock, and mailboxes appear in SmartLead asynchronously
after an export. Vercel stops a function at 300 seconds. The existing
`group/setup-domain` route papers over that with `time.sleep(60)` inside the
request, which works for one domain on a good day and silently truncates
otherwise.

So no step here ever sleeps for propagation. Each call does whatever work is
possible RIGHT NOW, writes what it did to the order's journal, and says whether
it wants to be called again. Being called ten minutes later is a normal
outcome, not a retry — which is what lets an order take an hour without any
single request taking more than a few seconds.

Every step is idempotent against the journal AND against the live systems: it
re-reads what exists before creating anything, so a call that dies halfway
through cannot double-create on the next pass. That matters most at
`mailboxes`, where a duplicate is a real monthly charge.

TWO HARD RULES, enforced here rather than trusted to the caller:

  Google only. Outlook/Microsoft provisioning is banned outright (Tim,
  standing rule) — the Microsoft acquisition batch replied at about a quarter
  of the Google rate and was retired in September. A MICROSOFT order refuses
  rather than warns.

  Never more than three mailboxes on a domain. Zapmail permits five. Three is
  ours: one bounce pattern takes out every mailbox on the domain, and the
  earliest-created one is an admin that cannot be removed while its siblings
  remain, so a domain overfilled today cannot be trimmed later.

The state machine, in order. A step that cannot finish leaves the order on
itself and asks to be resumed:

  dns        every domain live in Zapmail with its nameservers resolved
  slots      enough Google mailbox slots bought to cover what we will create
  mailboxes  create up to three per domain
  forwarding point every domain at the site it is selling for
  export     hand the mailboxes to SmartLead
  find       locate the SmartLead accounts (the export lands asynchronously)
  tag        tag them into their group
  warmup     start warm-up
  done

All I/O goes through an injected `io` object, so the whole machine is testable
without touching Zapmail, Spaceship or SmartLead.
"""
from __future__ import annotations

import time

MAX_PER_DOMAIN = 3
ALLOWED_PROVIDER = "GOOGLE"

STEPS = ("dns", "slots", "mailboxes", "forwarding", "export", "find", "tag",
         "warmup", "done")

# A call gives up its remaining work at this point and asks to be resumed. Well
# under Vercel's 300s so the response itself always gets out.
DEFAULT_BUDGET_SECONDS = 200

# How many times `find` will come back empty before we stop calling it progress.
# The export is asynchronous but not infinite; past this something is wrong and
# saying so beats polling forever.
MAX_FIND_ATTEMPTS = 40


class ProvisionRefused(Exception):
    """A rule says this order must not be provisioned at all."""


def check_order(order: dict) -> None:
    """Refuse an order that breaks a hard rule. Raises, never warns."""
    provider = (order.get("provider") or "").upper()
    if provider and provider != ALLOWED_PROVIDER:
        raise ProvisionRefused(
            f"provider is {provider}; only {ALLOWED_PROVIDER} may be provisioned. "
            "Outlook inboxes are not bought — the Microsoft batch replied at about "
            "a quarter of the Google rate and was retired on 2026-09-05.")
    per = int(order.get("inboxes_per_domain") or 0)
    if per > MAX_PER_DOMAIN:
        raise ProvisionRefused(
            f"{per} mailboxes per domain; the cap is {MAX_PER_DOMAIN}. "
            "The admin mailbox cannot be removed later while its siblings remain, "
            "so an overfilled domain cannot be trimmed.")


def per_domain(order: dict) -> int:
    """How many mailboxes this order puts on one domain, capped."""
    return max(1, min(MAX_PER_DOMAIN, int(order.get("inboxes_per_domain") or MAX_PER_DOMAIN)))


def mailbox_specs(domain: str, count: int, persona: dict | None = None) -> list[dict]:
    """The usernames to create, in a fixed order.

    Fixed, not random: re-running a half-finished domain has to ask for exactly
    the same three names, or the idempotence check ("does s.reynolds already
    exist?") compares against something that was never going to be created.
    """
    p = persona or {"first": "Sean", "last": "Reynolds"}
    f, l = p["first"], p["last"]
    fl, ll = f.lower(), l.lower()
    names = [f"{fl[0]}.{ll}", f"{fl}.{ll[0]}", f"{fl}.{ll}"]
    return [{"firstName": f, "lastName": l, "mailboxUsername": u} for u in names[:count]]


def _journal(order: dict) -> dict:
    j = order.setdefault("journal", {})
    j.setdefault("step", "dns")
    j.setdefault("domains", {})
    j.setdefault("log", [])
    j.setdefault("find_attempts", 0)
    return j


def _note(j: dict, text: str) -> None:
    """One line per thing that actually happened. Capped so an order that gets
    resumed a hundred times does not grow without bound."""
    j["log"].append(text)
    if len(j["log"]) > 200:
        del j["log"][:-200]


def _dom(j: dict, domain: str) -> dict:
    return j["domains"].setdefault(domain, {})


# ── the steps ─────────────────────────────────────────────────────────────
# Each returns (done, wants_resume). `done` advances to the next step.

def _step_dns(order, j, io):
    # Retry any domain the purchase could not connect because its nameservers
    # had not propagated yet. That is a normal few-minute state after buying,
    # not a failure, and it is exactly what this step is waiting on anyway.
    pending = [d for d in (order.get("connect_pending") or [])]
    if pending and hasattr(io, "connect_domains"):
        now_ok = io.connect_domains(pending)
        if now_ok:
            order["connected"] = sorted(set(order.get("connected") or []) | set(now_ok))
            order["connect_pending"] = [d for d in pending if d not in set(now_ok)]
            _note(j, f"dns: connected {len(now_ok)} domain(s) whose nameservers caught up")

    state = io.zapmail_domain_state(order.get("domains") or [])
    ready = []
    for d in order.get("domains") or []:
        s = state.get(d.lower()) or {}
        rec = _dom(j, d)
        rec["zapmail_id"] = s.get("id")
        rec["dns_ready"] = bool(s.get("dns_ready"))
        rec["existing_mailboxes"] = s.get("mailboxes")
        if rec["dns_ready"]:
            ready.append(d)
    j["dns_ready_count"] = len(ready)
    if not ready:
        return False, True                      # nothing resolves yet; come back
    # Partial is fine: provision what resolves, and the domains that are still
    # propagating are picked up by a later pass through this same step.
    _note(j, f"dns: {len(ready)}/{len(order.get('domains') or [])} domains resolved")
    return True, False


def _step_slots(order, j, io):
    per = per_domain(order)
    # Count what EXISTS right now, not what the journal remembers. The journal's
    # `existing_mailboxes` is written by the DNS step, which runs once and is
    # then behind us — so on any re-entry it still reads zero for domains that
    # have since been filled, and this step would buy a second set of slots for
    # mailboxes that already exist. The mailbox step re-reads live for exactly
    # this reason; the step that SPENDS THE MONEY should not be the laxer one.
    want = 0
    for d in order.get("domains") or []:
        rec = _dom(j, d)
        if not rec.get("dns_ready"):
            continue
        live = io.zapmail_mailboxes_on(d, rec.get("zapmail_id"))
        if live is None:
            _note(j, f"slots: could not read {d}; not buying blind")
            return False, True
        rec["existing_mailboxes"] = len(live)
        want += per - min(per, len(live))
    j["slots_needed"] = want
    if want <= 0:
        return True, False
    free = io.zapmail_free_slots()
    if free is None:
        _note(j, "slots: could not read the workspace; not buying blind")
        return False, True
    if free >= want:
        _note(j, f"slots: {free} free, need {want}")
        return True, False
    res = io.zapmail_buy_slots(want - free)
    if not res.get("ok"):
        # A wallet that is topping up settles on its own; we come back rather
        # than sleeping inside the request.
        j["blocked_reason"] = res.get("error") or "could not buy mailbox slots"
        _note(j, f"slots: {j['blocked_reason']}")
        return False, True
    j.pop("blocked_reason", None)
    _note(j, f"slots: bought {want - free}")
    return True, False


def _step_mailboxes(order, j, io):
    per = per_domain(order)
    made_any = False
    for d in order.get("domains") or []:
        rec = _dom(j, d)
        if not rec.get("dns_ready") or rec.get("mailboxes_done"):
            continue
        # Re-read the live count every pass. The journal alone is not enough:
        # a call that created mailboxes and then died before saving would
        # otherwise create them a second time, and each one is a real charge.
        live = io.zapmail_mailboxes_on(d, rec.get("zapmail_id"))
        if live is None:
            return False, True                  # cannot see it; never create blind
        have = len(live)
        if have >= per:
            rec["mailboxes_done"] = True
            rec["mailboxes"] = live[:per]
            _note(j, f"{d}: {have} mailboxes already exist")
            continue
        specs = mailbox_specs(d, per)[have:]
        res = io.zapmail_create_mailboxes(rec.get("zapmail_id"), d, specs)
        if not res.get("ok"):
            rec["error"] = res.get("error")
            _note(j, f"{d}: create failed — {str(res.get('error'))[:80]}")
            # One domain failing must not abandon the other thirteen. Zapmail
            # rate-limits ("Too many requests") partway through a 14-domain
            # batch, and aborting the whole pass there meant each resume got
            # through only one or two more domains -- 15 of 42 mailboxes after
            # several passes. A refusal on THIS domain is per-domain; carry on
            # and let the next pass retry it.
            continue
        after = io.zapmail_mailboxes_on(d, rec.get("zapmail_id"))
        rec["mailboxes"] = (after or [])[:per]
        rec["mailboxes_done"] = len(rec["mailboxes"]) >= per
        made_any = True
        _note(j, f"{d}: created {len(specs)}, now {len(rec['mailboxes'])}")
    pending = [d for d in order.get("domains") or []
               if _dom(j, d).get("dns_ready") and not _dom(j, d).get("mailboxes_done")]
    if pending:
        return False, True
    if not any(_dom(j, d).get("mailboxes") for d in order.get("domains") or []):
        return False, True
    if made_any:
        _note(j, "mailboxes: complete for every resolved domain")
    return True, False


def _all_mailboxes(order, j):
    out = []
    for d in order.get("domains") or []:
        out.extend(_dom(j, d).get("mailboxes") or [])
    return out


def _step_forwarding(order, j, io):
    """Point every domain at the website it is selling for.

    Forwarding is per DOMAIN and does not follow anything else, so nothing
    upstream sets it: not the purchase, not the Zapmail connect, not the
    mailbox creation. A batch can therefore go fully live — mailboxes created,
    warmed, tagged, sending — with every domain forwarding nowhere. LightDMV
    launched exactly like that, 15 of 19 domains pointing at nothing, and it
    was found by hand weeks later.

    A missing target BLOCKS rather than skips. "We do not know where this should
    point" is a thing to answer, not a step to pass over quietly — and a domain
    prospecting on behalf of a client while forwarding nowhere is the failure
    this step exists to prevent.
    """
    target = (order.get("forward_to") or "").strip()
    if not target:
        j["blocked_reason"] = (
            "no forwarding target on this order — set the client's website (or "
            "ours, for acquisition) before the domains go live, or prospects who "
            "click through land nowhere")
        _note(j, "forwarding: no target set")
        return False, False                   # needs a person, not a retry

    doms = [d for d in (order.get("domains") or []) if _dom(j, d).get("dns_ready")]
    done = set(j.get("forwarded") or [])
    todo = [d for d in doms if d not in done]
    if not todo:
        return True, False

    res = io.set_forwarding(todo, target)
    if not res.get("ok"):
        j["blocked_reason"] = res.get("error") or "could not set forwarding"
        _note(j, f"forwarding: {j['blocked_reason']}")
        return False, True
    j.pop("blocked_reason", None)
    j["forwarded"] = sorted(done | set(todo))
    _note(j, f"forwarding: {len(todo)} domain(s) -> {target}")
    return True, False


def _step_export(order, j, io):
    mbs = _all_mailboxes(order, j)
    if not mbs:
        return False, True
    ids = [m["id"] for m in mbs if m.get("id")]
    res = io.zapmail_export_to_smartlead(ids)
    if not res.get("ok"):
        j["blocked_reason"] = res.get("error") or "export to SmartLead failed"
        _note(j, f"export: {j['blocked_reason']}")
        return False, True
    j.pop("blocked_reason", None)
    j["exported_ids"] = ids
    _note(j, f"export: handed {len(ids)} mailboxes to SmartLead")
    return True, False


def _step_find(order, j, io):
    """The export lands asynchronously, so this polls — across calls, not inside one."""
    want = {(m.get("email") or "").lower() for m in _all_mailboxes(order, j) if m.get("email")}
    if not want:
        return False, True
    found = io.smartlead_accounts_for(sorted(want))
    if found is None:
        # A roster read that came back short is not an empty roster. Treating it
        # as one would tag and warm up a fraction of the batch and call it done.
        _note(j, "find: SmartLead roster unreadable; not treating that as 'none yet'")
        return False, True
    j["smartlead"] = {e: found[e] for e in found if e in want}
    j["find_attempts"] = int(j.get("find_attempts") or 0) + 1
    if len(j["smartlead"]) >= len(want):
        _note(j, f"find: all {len(want)} accounts are in SmartLead")
        return True, False
    if j["find_attempts"] >= MAX_FIND_ATTEMPTS:
        j["blocked_reason"] = (f"only {len(j['smartlead'])} of {len(want)} mailboxes reached "
                               f"SmartLead after {j['find_attempts']} checks")
        _note(j, f"find: {j['blocked_reason']}")
        return False, False                     # stop asking; this needs a person
    return False, True


def _step_tag(order, j, io):
    ids = [v for v in (j.get("smartlead") or {}).values()]
    if not ids:
        return False, True
    tag = order.get("tag") or ("Acquisition" if order.get("owner") == "acquisition"
                               else order.get("client_name"))
    if not tag:
        j["blocked_reason"] = "no tag to file these under"
        return False, False
    res = io.smartlead_tag(ids, tag)
    if not res.get("ok"):
        j["blocked_reason"] = res.get("error") or "tagging failed"
        _note(j, f"tag: {j['blocked_reason']}")
        return False, True
    j.pop("blocked_reason", None)
    j["tagged"] = ids
    _note(j, f"tag: {len(ids)} accounts filed under {tag}")
    return True, False


def _step_warmup(order, j, io):
    ids = [v for v in (j.get("smartlead") or {}).values()]
    done = set(j.get("warmed") or [])
    failed = []
    for aid in ids:
        if aid in done:
            continue
        if io.smartlead_start_warmup(aid).get("ok"):
            done.add(aid)
        else:
            failed.append(aid)
    j["warmed"] = sorted(done)
    if failed:
        j["blocked_reason"] = f"warm-up did not start on {len(failed)} account(s)"
        _note(j, f"warmup: {j['blocked_reason']}")
        return False, True
    j.pop("blocked_reason", None)
    _note(j, f"warmup: started on {len(done)} accounts — 14 days from now they are ready")
    return True, False


_HANDLERS = {
    "dns": _step_dns, "slots": _step_slots, "mailboxes": _step_mailboxes,
    "forwarding": _step_forwarding, "export": _step_export, "find": _step_find,
    "tag": _step_tag, "warmup": _step_warmup,
}


def advance(order: dict, io, budget_seconds: int = DEFAULT_BUDGET_SECONDS,
            clock=time.monotonic) -> dict:
    """Push one order as far as it will go right now. Mutates `order` in place.

    Returns {step, done, resume, ...} — `resume` means call again later, which
    is the normal answer while DNS propagates or an export settles.
    """
    check_order(order)
    j = _journal(order)
    started = clock()
    resume = False

    while j["step"] != "done":
        if clock() - started > budget_seconds:
            resume = True
            break
        handler = _HANDLERS.get(j["step"])
        if handler is None:
            j["step"] = "done"
            break
        advanced, wants_resume = handler(order, j, io)
        if advanced:
            j["step"] = STEPS[STEPS.index(j["step"]) + 1]
            continue
        resume = wants_resume
        break

    order["status"] = "live" if j["step"] == "done" else "provisioning"
    return {
        "order_id": order.get("id"),
        "step": j["step"],
        "done": j["step"] == "done",
        "resume": resume and j["step"] != "done",
        "blocked_reason": j.get("blocked_reason"),
        "domains": {d: {k: v for k, v in rec.items() if k != "mailboxes"}
                    for d, rec in j["domains"].items()},
        "mailboxes": len(_all_mailboxes(order, j)),
        "in_smartlead": len(j.get("smartlead") or {}),
        "forwarded": len(j.get("forwarded") or []),
        "forward_to": order.get("forward_to"),
        "tagged": len(j.get("tagged") or []),
        "warmed": len(j.get("warmed") or []),
        "log": j["log"][-20:],
    }


# ── the real world ────────────────────────────────────────────────────────

class LiveIO:
    """The `io` the route uses: Zapmail + SmartLead through setup.py.

    Every read that can come back SHORT returns None rather than a partial
    answer, because the machine above treats None as "could not look" and a
    value as "this is everything". Getting that wrong is how a truncated roster
    turns into a half-provisioned batch that reports success.
    """

    def __init__(self, setup_module=None):
        import setup as S
        self.S = setup_module or S

    # -- Zapmail --
    def zapmail_domain_state(self, domains):
        want = {d.lower() for d in domains}
        out = {}
        for zd in self.S.zm_list_domains() or []:
            name = (zd.get("domain") or "").lower()
            if name in want:
                out[name] = {
                    "id": zd.get("id"),
                    "dns_ready": bool(zd.get("status") == "ACTIVE"
                                      and not zd.get("dnsAuthenticationInProgress")
                                      and not zd.get("dnsBoxInvalidNameServers")),
                    "mailboxes": len(zd.get("mailboxes") or []),
                }
        return out

    def zapmail_free_slots(self):
        try:
            ws = ((self.S.zm_list_workspaces() or {}).get("data") or {}).get("currentWorkspace") or {}
            bought = int(ws.get("totalMailboxesPurchasedGoogle") or 0)
            used = int(ws.get("assignedMailboxesCountGoogle") or 0)
        except (AttributeError, TypeError, ValueError):
            return None
        return bought - used

    def zapmail_buy_slots(self, n):
        try:
            r = self.S.zm_buy_addon_mailboxes(int(n))
        except Exception as e:                       # noqa: BLE001 — reported, not raised
            return {"ok": False, "error": str(e)[:160]}
        if isinstance(r, dict) and r.get("message") and "success" not in str(r.get("message")).lower():
            return {"ok": False, "error": str(r.get("message"))[:160]}
        return {"ok": True}

    def zapmail_mailboxes_on(self, domain, zapmail_id):
        """Live mailbox list for one domain, or None if we could not see it."""
        try:
            for zd in self.S.zm_list_domains() or []:
                if (zd.get("domain") or "").lower() == domain.lower():
                    return [{"id": m.get("id"),
                             "email": (m.get("email")
                                       or f"{m.get('mailboxUsername','')}@{domain}").lower()}
                            for m in (zd.get("mailboxes") or []) if isinstance(m, dict)]
        except Exception:                            # noqa: BLE001
            return None
        return None          # the domain was not in the list: not the same as "zero"

    def zapmail_create_mailboxes(self, zapmail_id, domain, specs):
        try:
            r = self.S.zm_create_mailboxes(zapmail_id, domain, specs)
        except Exception as e:                       # noqa: BLE001
            return {"ok": False, "error": str(e)[:160]}
        data = (r or {}).get("data")
        if isinstance(data, list) and data:
            return {"ok": True}
        return {"ok": False, "error": str((r or {}).get("message") or r)[:160]}

    def connect_domains(self, domains):
        """Retry the Zapmail connect for domains whose nameservers were not
        ready at purchase time. Returns the ones that connected."""
        ok = []
        for d in domains:
            try:
                r = self.S.zm_connect_domain_single(d)
            except Exception:                            # noqa: BLE001
                continue
            if not (isinstance(r, dict) and r.get("error")):
                ok.append(d)
        return ok

    def set_forwarding(self, domains, target):
        """Point these domains at `target`. Zapmail takes domain IDs, so the
        names are resolved first and a name it does not know is an error, not a
        silent omission."""
        try:
            idx = {(d.get("domain") or "").lower(): d.get("id")
                   for d in (self.S.zm_list_domains() or [])}
        except Exception as e:                           # noqa: BLE001
            return {"ok": False, "error": f"could not read Zapmail: {str(e)[:120]}"}
        ids, missing = [], []
        for d in domains:
            i = idx.get(d.lower())
            (ids.append(i) if i else missing.append(d))
        if missing:
            return {"ok": False,
                    "error": f"{len(missing)} domain(s) not found in Zapmail: "
                             f"{', '.join(sorted(missing)[:4])}"}
        try:
            self.S.zm_set_forwarding(ids, target)
        except Exception as e:                           # noqa: BLE001
            return {"ok": False, "error": str(e)[:140]}
        return {"ok": True}

    def zapmail_export_to_smartlead(self, mailbox_ids):
        try:
            self.S.zm_export_mailboxes(["SMARTLEAD"], mailbox_ids=list(mailbox_ids))
        except Exception as e:                       # noqa: BLE001
            return {"ok": False, "error": str(e)[:160]}
        return {"ok": True}

    # -- SmartLead --
    def smartlead_accounts_for(self, emails):
        """{email: account_id} for the ones that exist, or None on a short read.

        The walk is all-or-nothing on purpose: a page that fails part way through
        would otherwise look like "those mailboxes have not arrived yet", and the
        machine would tag and warm up only the fraction it happened to see.
        """
        want = {e.lower() for e in emails}
        found, offset = {}, 0
        while True:
            try:
                batch = self.S.sl_list_accounts(offset=offset, limit=100)
            except Exception:                        # noqa: BLE001
                return None
            if batch is None:
                return None
            if not isinstance(batch, list):
                return None
            for a in batch:
                em = (a.get("from_email") or "").lower()
                if em in want:
                    found[em] = a.get("id")
            if len(batch) < 100:
                return found
            offset += 100

    def smartlead_tag(self, account_ids, tag_name):
        try:
            tag = self.S.sl_find_or_create_tag(tag_name)
            tid = tag.get("id") if isinstance(tag, dict) else tag
            if not tid:
                return {"ok": False, "error": f"could not resolve tag {tag_name!r}"}
            self.S.sl_tag_accounts_bulk(list(account_ids), [tid])
        except Exception as e:                       # noqa: BLE001
            return {"ok": False, "error": str(e)[:160]}
        return {"ok": True}

    def smartlead_start_warmup(self, account_id):
        try:
            self.S.sl_set_warmup(account_id)
        except Exception:                            # noqa: BLE001
            return {"ok": False}
        return {"ok": True}
