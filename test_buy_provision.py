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
        self.forwarding_ok = True
        self.forwarded = {}
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

    def set_forwarding(self, domains, target):
        if not self.forwarding_ok:
            return {"ok": False, "error": "zapmail refused"}
        for d in domains:
            self.forwarded[d] = target
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
         "inboxes_per_domain": 3, "domains": ["x.co"], "tag": "Acquisition T",
         "forward_to": "https://theheadlinetheory.com"}
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
    """Stops at `slots`, one step earlier than it used to: the slot step now
    reads the same live count, so an unreadable domain is refused before any
    money is spent rather than just before mailboxes are made."""
    io = FakeIO(["x.co"])
    io.zapmail_mailboxes_on = lambda d, z: None
    o = order()
    r = bp.advance(o, io)
    assert r["step"] == "slots"
    assert io.created_calls == 0
    assert io.bought == 0


def test_slots_are_not_bought_again_for_mailboxes_that_already_exist():
    """The double-spend this guards against: the DNS step records
    `existing_mailboxes` once and is then behind us, so on any re-entry it
    still reads zero for domains that have since been filled. Counting from
    the journal would buy a second full set of slots for mailboxes that are
    already there — and slots are billed per mailbox."""
    io = FakeIO(["x.co"])
    o = order()
    bp.advance(o, io)                              # first pass fills x.co
    j = o["journal"]
    j["step"] = "slots"                            # re-enter the spending step
    j["domains"]["x.co"]["existing_mailboxes"] = 0  # stale journal value
    io.bought = 0
    io.free_slots = 0                              # nothing spare: it WOULD buy
    bp.advance(o, io)
    assert io.bought == 0, "bought slots for mailboxes that already exist"


def test_slots_are_still_bought_when_mailboxes_are_genuinely_missing():
    """The guard must not become a reason never to buy slots at all."""
    io = FakeIO(["x.co"])
    io.free_slots = 0
    o = order()
    bp.advance(o, io)
    assert io.bought > 0, "refused to buy slots that were actually needed"


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


def test_dns_step_retries_domains_whose_nameservers_were_not_ready(monkeypatch):
    """A domain bought minutes ago is still on the registrar's nameservers, so
    Zapmail refuses it. That is a wait, not a failure — 14 domains were bought,
    all 14 refused, and all 14 recorded as connected."""
    io = FakeIO(["a.co", "b.co"])
    io.connected_calls = []

    def connect(domains):
        io.connected_calls.append(list(domains))
        return ["b.co"]                       # b caught up, a has not
    io.connect_domains = connect

    o = order(domains=["a.co", "b.co"])
    o["connect_pending"] = ["a.co", "b.co"]
    o["connected"] = []
    bp.advance(o, io)
    assert io.connected_calls == [["a.co", "b.co"]]
    assert o["connected"] == ["b.co"]
    assert o["connect_pending"] == ["a.co"]


def test_dns_step_is_fine_when_nothing_is_pending():
    io = FakeIO(["x.co"])
    o = order()
    r = bp.advance(o, io)
    assert r["done"] is True


# ── forwarding ───────────────────────────────────────────────────────────
# Forwarding is per DOMAIN and follows nothing else — not the purchase, not the
# Zapmail connect, not the mailboxes. LightDMV went live with 15 of 19 domains
# pointing nowhere and it was found by hand weeks later.

def test_every_domain_is_pointed_at_the_site_it_sells_for():
    io = FakeIO(["a.co", "b.co"])
    o = order(domains=["a.co", "b.co"], forward_to="https://client.com")
    r = bp.advance(o, io)
    assert r["done"] is True
    assert io.forwarded == {"a.co": "https://client.com", "b.co": "https://client.com"}
    assert r["forwarded"] == 2


def test_no_forwarding_target_blocks_instead_of_skipping():
    """"We do not know where this points" is a question, not a step to pass."""
    io = FakeIO(["x.co"])
    o = order(forward_to=None)
    r = bp.advance(o, io)
    assert r["step"] == "forwarding"
    assert r["done"] is False and r["resume"] is False      # needs a person
    assert "forwarding target" in r["blocked_reason"]
    assert io.forwarded == {}


def test_nothing_reaches_smartlead_before_forwarding_is_set():
    """Order matters: a domain that is already sending cannot be un-sent."""
    io = FakeIO(["x.co"])
    bp.advance(order(forward_to=None), io)
    assert io.exported == [] and io.tagged == [] and io.warmed == []


def test_a_forwarding_failure_is_retried_not_swallowed():
    io = FakeIO(["x.co"])
    io.forwarding_ok = False
    o = order()
    r = bp.advance(o, io)
    assert r["step"] == "forwarding" and r["resume"] is True
    io.forwarding_ok = True
    r = bp.advance(o, io)
    assert r["done"] and io.forwarded == {"x.co": "https://theheadlinetheory.com"}


def test_forwarding_is_not_redone_on_a_resume():
    io = FakeIO(["a.co", "b.co"])
    o = order(domains=["a.co", "b.co"])
    bp.advance(o, io)
    io.forwarded = {}
    bp.advance(o, io)
    assert io.forwarded == {}          # already recorded as done


def test_only_dns_ready_domains_are_forwarded():
    io = FakeIO(["fast.co", "slow.co"])
    io.state["slow.co"]["dns_ready"] = False
    o = order(domains=["fast.co", "slow.co"])
    bp.advance(o, io)
    assert list(io.forwarded) == ["fast.co"]
