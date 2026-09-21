"""Rule fixes are scripts, so they are tested like scripts.

Every test is about refusing to act, because that is the failure that costs
money: disabling auto-renew on a domain with live senders, or deleting a
Smartlead account that still owns a conversation.
"""
import pytest

import rule10_safety
import rule_fixes as rf


@pytest.fixture(autouse=True)
def fresh_reply_scan(monkeypatch):
    """Default: last night's full-campaign reply scan ran and found nothing.
    Tests that care override `verdict` themselves."""
    monkeypatch.setattr(rule10_safety, "verdict",
                        lambda emails: {"stale": False, "active": [], "positives": [],
                                        "campaigns_scanned": 415, "age_hours": 3.0})


class IO:
    def __init__(self, **kw):
        self.reg = kw.get("reg", {})
        self.counts = kw.get("counts", {})
        self.live = kw.get("live", set())
        self.accts = kw.get("accts", [])
        self.ext = kw.get("ext", set())
        self.senders = kw.get("senders", {})
        self.guard = kw.get("guard", {"active": [], "positives": []})
        self.disabled, self.deleted = [], []

    def registrar_domains(self): return self.reg
    def mailbox_counts_by_domain(self): return self.counts
    def sender_counts_by_domain(self): return self.senders
    def renewal_price(self, d): return 11.0
    def zapmail_mailboxes(self): return self.live
    def smartlead_accounts(self): return self.accts
    def external_domains(self): return self.ext
    def safety_check(self, emails): return self.guard

    def disable_auto_renew(self, doms):
        self.disabled = list(doms); return {"ok": True, "disabled": len(doms)}

    def delete_smartlead(self, rows):
        self.deleted = [r["email"] for r in rows]; return {"ok": True, "deleted": len(rows)}


# ── rule 7 ───────────────────────────────────────────────────────────────

def test_rule_7_targets_only_empty_auto_renewing_domains():
    io = IO(reg={"empty.co": {"auto_renew": True}, "full.co": {"auto_renew": True},
                 "off.co": {"auto_renew": False}},
            counts={"full.co": 3})
    r = rf.run(7, io, confirm=True)
    assert r["targets"] == ["empty.co"]
    assert io.disabled == ["empty.co"]


def test_rule_7_re_derives_rather_than_trusting_the_stored_violation():
    """A domain empty when the rule ran may have been provisioned since, and
    letting a domain with live senders lapse cannot be undone."""
    io = IO(reg={"was-empty.co": {"auto_renew": True}}, counts={"was-empty.co": 3})
    r = rf.run(7, io, confirm=True)
    assert r["targets"] == [] and io.disabled == []


def test_rule_7_dry_run_changes_nothing():
    io = IO(reg={"empty.co": {"auto_renew": True}}, counts={})
    r = rf.run(7, io)
    assert r["dry_run"] is True and io.disabled == []
    assert r["saving_yr"] == 11.0


def test_rule_7_refuses_on_an_unreadable_source():
    io = IO(reg=None, counts={})
    assert "refusing" in rf.run(7, io, confirm=True)["error"]
    io2 = IO(reg={"a.co": {"auto_renew": True}}, counts=None)
    assert "refusing" in rf.run(7, io2, confirm=True)["error"]


# ── rule 10 ──────────────────────────────────────────────────────────────

def acct(e): return {"email": e, "id": 1}


def test_rule_10_deletes_only_accounts_with_no_zapmail_mailbox():
    io = IO(live={"kept@x.co"}, accts=[acct("kept@x.co"), acct("gone@x.co")])
    r = rf.run(10, io, confirm=True)
    assert r["targets"] == ["gone@x.co"] and io.deleted == ["gone@x.co"]


def test_rule_10_never_touches_an_external_domain():
    """The headlinetheory*.com mailboxes are not in Zapmail and never were —
    36 of them are actively sending."""
    io = IO(live=set(), accts=[acct("a@headlinetheory.com")], ext={"headlinetheory.com"})
    r = rf.run(10, io, confirm=True)
    assert r["count"] == 0 and io.deleted == []


def test_rule_10_holds_anything_on_an_active_campaign():
    io = IO(live=set(), accts=[acct("busy@x.co"), acct("dead@x.co")],
            guard={"active": ["busy@x.co"], "positives": []})
    r = rf.run(10, io, confirm=True)
    assert io.deleted == ["dead@x.co"]
    assert r["held"] == ["busy@x.co"]


def test_rule_10_holds_anything_owning_a_positive_reply(monkeypatch):
    """The reply may sit on a PAUSED campaign, which the in-request ACTIVE
    scan cannot see — so this verdict comes from the nightly full walk."""
    monkeypatch.setattr(rule10_safety, "verdict",
                        lambda e: {"stale": False, "active": [],
                                   "positives": ["talks@x.co"],
                                   "campaigns_scanned": 415, "age_hours": 3.0})
    io = IO(live=set(), accts=[acct("talks@x.co")], guard={"active": [], "positives": []})
    r = rf.run(10, io, confirm=True)
    assert io.deleted == [] and r["held"] == ["talks@x.co"]


def test_rule_10_deletes_nothing_when_the_reply_scan_is_stale(monkeypatch):
    """Rule 11 applied to the button: a check that did not run is not a check
    that passed. Without a fresh full-campaign walk, a mailbox holding a live
    thread on a paused campaign is indistinguishable from a dead one."""
    monkeypatch.setattr(rule10_safety, "verdict",
                        lambda e: {"stale": True, "reason": "last full scan was 71h ago"})
    io = IO(live=set(), accts=[acct("gone@x.co")])
    r = rf.run(10, io, confirm=True)
    assert io.deleted == []
    assert r["count"] == 0 and r["targets"] == []
    assert r["blocked_by"] == "reply scan"
    assert "71h" in r["note"]


def test_rule_10_still_holds_active_campaigns_when_the_reply_scan_is_stale(monkeypatch):
    monkeypatch.setattr(rule10_safety, "verdict",
                        lambda e: {"stale": True, "reason": "no scan on record"})
    io = IO(live=set(), accts=[acct("busy@x.co")], guard={"active": ["busy@x.co"], "positives": []})
    r = rf.run(10, io, confirm=True)
    assert io.deleted == [] and r["held"] == ["busy@x.co"]


def test_rule_10_aborts_entirely_if_the_safety_check_failed():
    """A scan that could not complete is not a clean scan."""
    io = IO(live=set(), accts=[acct("a@x.co")], guard={"error": "scan incomplete"})
    r = rf.run(10, io, confirm=True)
    assert "nothing deleted" in r["error"] and io.deleted == []


def test_rule_10_refuses_a_partial_roster():
    io = IO(live=None, accts=[acct("a@x.co")])
    assert "partial roster" in rf.run(10, io, confirm=True)["error"]


def test_rule_10_dry_run_changes_nothing():
    io = IO(live=set(), accts=[acct("gone@x.co")])
    r = rf.run(10, io)
    assert r["dry_run"] is True and io.deleted == []


# ── the rest ─────────────────────────────────────────────────────────────

def test_a_rule_with_no_automatic_fix_says_so():
    for n in (1, 2, 3, 4, 5, 6, 8, 9, 11):
        assert "no automatic fix" in rf.run(n, IO(), confirm=True)["error"]


def test_targets_are_parsed_from_the_violation_shape_not_guessed():
    assert rf.targets_for(7, ["empty.co: 0 mailboxes, auto-renew ON"]) == ["empty.co"]
    assert rf.targets_for(10, ["a@x.co (Acquisition P)"]) == ["a@x.co"]
    assert rf.targets_for(7, ["something unexpected"]) == []


def test_rule_7_never_touches_a_domain_with_live_senders_outside_zapmail():
    """The headlinetheory*.com domains were never in Zapmail and carry 33
    actively sending Smartlead accounts. Checking only Zapmail put all eleven
    on the disable list — letting them lapse takes the senders with them."""
    io = IO(reg={"external.com": {"auto_renew": True}, "dead.info": {"auto_renew": True}},
            counts={},                                   # neither is in Zapmail
            senders={"external.com": 3})                 # but one is sending
    r = rf.run(7, io, confirm=True)
    assert r["targets"] == ["dead.info"]
    assert io.disabled == ["dead.info"]
    assert r["kept_external"] == [{"domain": "external.com", "smartlead_senders": 3}]


def test_rule_7_refuses_when_the_smartlead_side_is_unreadable():
    """Without it, 'empty' cannot be established — and the error is one-way."""
    io = IO(reg={"a.co": {"auto_renew": True}}, counts={}, senders=None)
    assert "refusing" in rf.run(7, io, confirm=True)["error"]
