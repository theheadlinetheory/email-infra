"""The provisioning machine, driven against a fake Zapmail/SmartLead.

The tests that matter are the ones about NOT doing something twice and NOT
declaring success on a partial read.
"""
import pytest

import buy_provision as bp


class FakeIO:
    """A Zapmail + SmartLead that can be told to misbehave."""

    def __init__(self, domains, dns_ready=True, free_slots=99):
        self.state = {d: {"id": f"zm-{d}", "dns_ready": dns_ready, "mailboxes": 0}
                      for d in domains}
        self.mailboxes = {d: [] for d in domains}
        self.free_slots = free_slots
        self.bought = 0
        self.created_calls = 0
        self.exported = []
        self.smartlead = {}          # email -> account id
        self.tagged = []
        self.warmed = []
        self.roster_readable = True
        self.export_lands = True
        self.create_ok = True
        self.warmup_ok = True
        self.next_account_id = 1000

    # -- Zapmail --
    def zapmail_domain_state(self, domains):
        return {d.lower(): dict(self.state[d], mailboxes=len(self.mailboxes[d]))
                for d in domains if d in self.state}

    def zapmail_free_slots(self):
        return self.free_slots

    def zapmail_buy_slots(self, n):
        self.bought += n
        self.free_slots += n
        return {"ok": True}

    def zapmail_mailboxes_on(self, domain, zid):
        return list(self.mailboxes.get(domain, []))

    def zapmail_create_mailboxes(self, zid, domain, specs):
        self.created_calls += 1
        if not self.create_ok:
            return {"ok": False, "error": "not enough mailboxes"}
        for s in specs:
            email = f"{s['mailboxUsername']}@{domain}"
            self.mailboxes[domain].append({"id": f"mb-{email}", "email": email})
        return {"ok": True}

    def zapmail_export_to_smartlead(self, ids):
        self.exported = list(ids)
        if self.export_lands:
            for d, mbs in self.mailboxes.items():
                for m in mbs:
                    if m["id"] in ids:
                        self.smartlead[m["email"]] = self.next_account_id
                        self.next_account_id += 1
        return {"ok": True}

    # -- SmartLead --
    def smartlead_accounts_for(self, emails):
        if not self.roster_readable:
            return None
        return {e: self.smartlead[e] for e in emails if e in self.smartlead}

    def smartlead_tag(self, ids, tag):
        self.tagged = list(ids)
        self.tag_name = tag
        return {"ok": True}

    def smartlead_start_warmup(self, aid):
        if not self.warmup_ok:
            return {"ok": False}
        self.warmed.append(aid)
        return {"ok": True}


def order(**kw):
    o = {"id": 1, "owner": "acquisition", "provider": "GOOGLE",
         "inboxes_per_domain": 3, "domains": ["x.co"], "tag": "Acquisition T"}
    o.update(kw)
    return o


# ── the hard rules ────────────────────────────────────────────────────────

def test_outlook_is_refused_outright_not_warned_about():
    with pytest.raises(bp.ProvisionRefused) as e:
        bp.advance(order(provider="MICROSOFT"), FakeIO(["x.co"]))
    assert "GOOGLE" in str(e.value)


def test_more_than_three_per_domain_is_refused():
    with pytest.raises(bp.ProvisionRefused) as e:
        bp.advance(order(inboxes_per_domain=5), FakeIO(["x.co"]))
    assert "cap is 3" in str(e.value)


def test_three_is_allowed():
    io = FakeIO(["x.co"])
    r = bp.advance(order(inboxes_per_domain=3), io)
    assert r["done"] and len(io.mailboxes["x.co"]) == 3


# ── the happy path ────────────────────────────────────────────────────────

def test_a_clean_order_runs_all_the_way_through():
    io = FakeIO(["x.co", "y.co"])
    o = order(domains=["x.co", "y.co"])
    r = bp.advance(o, io)
    assert r["done"] and r["step"] == "done" and r["resume"] is False
    assert o["status"] == "live"
    assert r["mailboxes"] == 6 and r["in_smartlead"] == 6
    assert r["tagged"] == 6 and r["warmed"] == 6
    assert io.tag_name == "Acquisition T"


# ── resuming ──────────────────────────────────────────────────────────────

def test_dns_that_has_not_resolved_waits_instead_of_provisioning():
    io = FakeIO(["x.co"], dns_ready=False)
    o = order()
    r = bp.advance(o, io)
    assert r["step"] == "dns" and r["resume"] is True
    assert io.created_calls == 0
    assert o["status"] == "provisioning"


def test_it_picks_up_where_it_stopped_when_dns_arrives():
    io = FakeIO(["x.co"], dns_ready=False)
    o = order()
    bp.advance(o, io)
    io.state["x.co"]["dns_ready"] = True
    r = bp.advance(o, io)
    assert r["done"] and len(io.mailboxes["x.co"]) == 3


def test_a_second_pass_never_creates_a_second_set_of_mailboxes():
    """The expensive mistake. Each duplicate is a real monthly charge."""
    io = FakeIO(["x.co"])
    o = order()
    bp.advance(o, io)
    calls = io.created_calls
    bp.advance(o, io)
    bp.advance(o, io)
    assert io.created_calls == calls
    assert len(io.mailboxes["x.co"]) == 3


def test_a_journal_lost_mid_flight_still_does_not_double_create():
    """Simulates a call that created mailboxes and died before saving: the
    journal knows nothing, but the live read does."""
    io = FakeIO(["x.co"])
    bp.advance(order(), io)            # creates 3, journal thrown away below
    fresh = order()                    # brand new order dict, no journal
    bp.advance(fresh, io)
    assert io.created_calls == 1
    assert len(io.mailboxes["x.co"]) == 3


def test_an_export_that_has_not_landed_yet_polls_across_calls():
    io = FakeIO(["x.co"])
    io.export_lands = False
    o = order()
    r = bp.advance(o, io)
    assert r["step"] == "find" and r["resume"] is True
    assert r["tagged"] == 0 and r["warmed"] == 0
    io.export_lands = True
    io.zapmail_export_to_smartlead([m["id"] for m in io.mailboxes["x.co"]])
    r = bp.advance(o, io)
    assert r["done"] and r["warmed"] == 3


def test_it_stops_asking_once_the_export_is_clearly_never_landing():
    io = FakeIO(["x.co"])
    io.export_lands = False
    o = order()
    for _ in range(bp.MAX_FIND_ATTEMPTS + 2):
        r = bp.advance(o, io)
    assert r["resume"] is False and not r["done"]
    assert "SmartLead" in r["blocked_reason"]


# ── partial reads must never read as completion ───────────────────────────

def test_an_unreadable_smartlead_roster_is_not_an_empty_one():
    io = FakeIO(["x.co"])
    io.roster_readable = False
    o = order()
    r = bp.advance(o, io)
    assert r["step"] == "find" and r["resume"] is True
    assert io.tagged == [] and io.warmed == []
    # and it does not burn the attempt budget on a read it never got
    assert o["journal"]["find_attempts"] == 0


def test_a_domain_whose_mailboxes_cannot_be_read_is_never_created_into():
    io = FakeIO(["x.co"])
    io.zapmail_mailboxes_on = lambda d, z: None
    o = order()
    r = bp.advance(o, io)
    assert r["step"] == "mailboxes" and io.created_calls == 0


def test_one_slow_domain_does_not_hold_up_the_one_that_resolved():
    io = FakeIO(["fast.co", "slow.co"])
    io.state["slow.co"]["dns_ready"] = False
    o = order(domains=["fast.co", "slow.co"])
    r = bp.advance(o, io)
    assert len(io.mailboxes["fast.co"]) == 3
    assert io.mailboxes["slow.co"] == []
    assert r["done"] is True          # what resolved is finished end to end


# ── failures that need a person ───────────────────────────────────────────

def test_a_low_wallet_stops_cleanly_and_says_why():
    io = FakeIO(["x.co"], free_slots=0)
    io.zapmail_buy_slots = lambda n: {"ok": False, "error": "Insufficient wallet balance"}
    o = order()
    r = bp.advance(o, io)
    assert r["step"] == "slots" and r["resume"] is True
    assert "wallet" in r["blocked_reason"]
    assert io.created_calls == 0


def test_slots_are_bought_only_for_what_is_missing():
    io = FakeIO(["x.co"], free_slots=1)
    bp.advance(order(), io)
    assert io.bought == 2


def test_warmup_failure_is_reported_and_retried_not_swallowed():
    io = FakeIO(["x.co"])
    io.warmup_ok = False
    o = order()
    r = bp.advance(o, io)
    assert r["step"] == "warmup" and r["resume"] is True
    assert "warm-up" in r["blocked_reason"]
    io.warmup_ok = True
    r = bp.advance(o, io)
    assert r["done"] and r["warmed"] == 3


def test_the_time_budget_returns_rather_than_running_past_the_limit():
    io = FakeIO(["x.co"])
    ticks = iter([0, 500, 1000, 1500])
    r = bp.advance(order(), io, budget_seconds=200, clock=lambda: next(ticks))
    assert r["resume"] is True and not r["done"]


def test_usernames_are_fixed_so_a_resume_asks_for_the_same_three():
    a = bp.mailbox_specs("x.co", 3)
    b = bp.mailbox_specs("x.co", 3)
    assert a == b
    assert [s["mailboxUsername"] for s in a] == ["s.reynolds", "sean.r", "sean.reynolds"]
