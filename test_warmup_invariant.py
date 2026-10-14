"""Every inbox is sending or warming. There is no third state.

Tim's rule verbatim: "Inbox group should either be in active campaigns sending,
or be in a replacement group where it is warming up, or be in a generic group
where it is also warming up."

This exists because 42 service inboxes sat six weeks with warm-up never
switched on. Provisioning stalls at `forwarding`; `warmup` is the step AFTER
it — so a missing forwarding target silently meant no warm-up, and nothing
said so. A sweep then found 71 in that state, 33 of them on LIVE client tags.
"""
import check_invariants as civ


def _acct(email, tag, warm=True, sent=0):
    return {"from_email": email,
            "tags": [{"tag_name": "Zapmail"}, {"tag_name": tag}],
            "warmup_details": {"status": "ACTIVE"} if warm else {"status": "INACTIVE"},
            "daily_sent_count": sent}


def run(accounts):
    return civ.rule_12_every_inbox_is_working_or_warming({"accounts": accounts})


def test_a_warming_reserve_inbox_passes():
    assert run([_acct("a@x.co", "Generic Landscaping 1", warm=True)]).status == civ.PASS


def test_a_sending_inbox_that_is_not_warming_passes():
    """A live sender is working. Demanding warm-up on top would flag the whole
    production fleet."""
    assert run([_acct("a@x.co", "Mighty Oak Landscaping", warm=False, sent=15)]).status == civ.PASS


def test_an_idle_cold_inbox_fails():
    """The exact 42-service-inbox case: exists, costs money, does nothing."""
    r = run([_acct("a@x.co", "Replacement Group - Service", warm=False, sent=0)])
    assert r.status == civ.FAIL and len(r.violations) == 1
    assert "a@x.co" in r.violations[0]


def test_a_cold_LIVE_client_inbox_fails():
    """Worse than idle reserve — it is sending reputation-blind."""
    assert run([_acct("a@x.co", "Landry's Landscape", warm=False, sent=0)]).status == civ.FAIL


def test_retirement_buckets_are_exempt():
    """337 inboxes are in Cleanup. Demanding warm-up there would cry wolf and
    train everyone to ignore this rule."""
    for tag in ("Cleanup 2026-09", "Retired - burned", "Burnt Acquisition"):
        assert run([_acct("a@x.co", tag, warm=False, sent=0)]).status == civ.PASS, tag


def test_the_violation_names_the_group_so_it_is_actionable():
    r = run([_acct("a@x.co", "Landy Rose Media", warm=False, sent=0)])
    assert "Landy Rose Media" in r.violations[0]


def test_no_accounts_is_a_SKIP_not_a_PASS():
    """An unread fleet must never report 'all good' — that is how 42 inboxes
    stayed cold for six weeks."""
    assert civ.rule_12_every_inbox_is_working_or_warming({}).status == civ.SKIP


def test_a_mixed_fleet_reports_only_the_cold_ones():
    r = run([_acct("ok@x.co", "Generic Landscaping 1", warm=True),
             _acct("send@x.co", "Mighty Oak Landscaping", warm=False, sent=15),
             _acct("cold@x.co", "Generic Landscaping 2", warm=False, sent=0),
             _acct("gone@x.co", "Cleanup 2026-09", warm=False, sent=0)])
    assert r.status == civ.FAIL
    assert len(r.violations) == 1 and "cold@x.co" in r.violations[0]
