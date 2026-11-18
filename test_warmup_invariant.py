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
    # Deliberately NOT Landy Rose: that tag is exempt, so it would never
    # produce a violation to name. See the exemption tests below.
    r = run([_acct("a@x.co", "Mighty Oak Landscaping", warm=False, sent=0)])
    assert "Mighty Oak Landscaping" in r.violations[0]


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


# ---------------------------------------------------------------------------
# Deliberate exemptions.
#
# A fleet-wide sweep warmed Landy Rose Media's 18 mailboxes. Tim, 2026-10-15:
# "don't start warm-up for Landy Rose, please do not." So the rule must not ask
# for warm-up there — and, just as important, the sweep must not override the
# rule. An exemption honoured by the check but ignored by the writer is not an
# exemption, it is a race.
# ---------------------------------------------------------------------------

def test_an_exempt_client_is_not_demanded_to_warm():
    r = run([_acct("a@x.co", "Landy Rose Media", warm=False, sent=0)])
    assert r.status == civ.PASS, r.detail


def test_the_exemption_is_stated_not_silent():
    """A quietly-dropped inbox is how things hide. The count has to show."""
    r = run([_acct("a@x.co", "Landy Rose Media", warm=False, sent=0)])
    assert "exempt" in r.detail.lower() and "landy rose" in r.detail.lower()


def test_the_exemption_is_name_scoped_not_fleet_wide():
    r = run([_acct("a@x.co", "Landy Rose Media", warm=False),
             _acct("b@y.co", "Acquisition P", warm=False, sent=0)])
    assert r.status == civ.FAIL
    assert len(r.violations) == 1 and "b@y.co" in r.violations[0]


def test_is_warmup_exempt_is_case_and_suffix_tolerant():
    assert civ.is_warmup_exempt("Landy Rose Media")
    assert civ.is_warmup_exempt("  landy rose media  ")
    assert not civ.is_warmup_exempt("Landry's Landscape")   # a different client
    assert not civ.is_warmup_exempt("")
    assert not civ.is_warmup_exempt(None)


def test_the_sweep_honours_the_same_exemption_as_the_rule():
    """The writer and the check must agree, or the nightly alert passes while
    the sweep keeps switching warm-up back on."""
    import scripts.warmup_sweep as sweep
    cold, skipped = sweep.classify([
        {"id": 1, "from_email": "a@x.co",
         "tags": [{"tag_name": "Landy Rose Media"}],
         "warmup_details": {"status": "INACTIVE"}, "daily_sent_count": 0},
        {"id": 2, "from_email": "b@y.co",
         "tags": [{"tag_name": "Acquisition P"}],
         "warmup_details": {"status": "INACTIVE"}, "daily_sent_count": 0},
    ])
    assert skipped["exempt"] == 1
    assert [c[1] for c in cold] == ["b@y.co"]


# ---------------------------------------------------------------------------
# The per-group breakdown the dashboard renders.
# ---------------------------------------------------------------------------

def test_breakdown_splits_every_state():
    b = civ.warmup_breakdown({"accounts": [
        _acct("a@x.co", "Generic Landscaping 1", warm=True),
        _acct("b@x.co", "Mighty Oak Landscaping", warm=False, sent=15),
        _acct("c@x.co", "Replacement Group - Service", warm=False, sent=0),
        _acct("d@x.co", "Landy Rose Media", warm=False, sent=0),
        _acct("e@x.co", "Cleanup Oct", warm=False, sent=0),
    ]})
    assert b["available"]
    assert b["totals"] == {"warming": 1, "sending": 1, "cold": 1,
                           "exempt": 1, "retired": 1}


def test_breakdown_puts_the_cold_groups_first():
    b = civ.warmup_breakdown({"accounts": [
        _acct("a@x.co", "Aaa Pool", warm=True),
        _acct("b@x.co", "Zzz Pool", warm=False, sent=0),
    ]})
    assert b["groups"][0]["group"] == "Zzz Pool"


def test_breakdown_with_no_accounts_says_unavailable_not_zero():
    """Zero cold and "could not read the fleet" must never render the same."""
    b = civ.warmup_breakdown({})
    assert b["available"] is False and b["totals"] == {}


# ── contact@ cold-calling inboxes never warm (Tim, 2026-10-31) ─────────────

def test_a_cold_contact_inbox_is_exempt_not_a_violation():
    """contact@ carries the client's own tag, so only the mailbox name can exempt it."""
    r = run([_acct("contact@woodyslandcare.info", "Woody's Landcare", warm=False, sent=0)])
    assert r.status == civ.PASS
    assert "deliberately exempt" in r.detail and "contact@" in r.detail


def test_the_mailbox_exemption_is_exact_not_a_substring():
    assert civ.is_warmup_exempt_mailbox("Contact@X.info")
    assert civ.is_warmup_exempt("Woody's Landcare", "contact@woodyslandcare.info")
    assert not civ.is_warmup_exempt_mailbox("contacts@x.info")
    assert not civ.is_warmup_exempt_mailbox("s.reynolds@contact.info")
    assert not civ.is_warmup_exempt_mailbox("contact")
    assert not civ.is_warmup_exempt_mailbox(None)


def test_the_sweep_never_warms_a_contact_inbox():
    import importlib.util, os
    spec = importlib.util.spec_from_file_location(
        "warmup_sweep", os.path.join(os.path.dirname(__file__), "scripts", "warmup_sweep.py"))
    sweep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sweep)
    cold, skipped = sweep.classify([dict(_acct("contact@x.info", "Woody's Landcare", warm=False), id=1),
                                    dict(_acct("s.r@x.co", "Woody's Landcare", warm=False), id=2)])
    assert [c[1] for c in cold] == ["s.r@x.co"] and skipped["exempt"] == 1


def test_breakdown_counts_contact_as_exempt():
    b = civ.warmup_breakdown({"accounts": [_acct("contact@x.info", "Woody's Landcare", warm=False)]})
    assert b["totals"]["exempt"] == 1 and b["totals"]["cold"] == 0
