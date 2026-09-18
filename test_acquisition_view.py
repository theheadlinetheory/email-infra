"""The Acquisition tab's rules, as tests.

The ones that matter are about what is NOT called free.
"""
import acquisition_view as av

TODAY = "2026-09-18"


def _acq(inboxes, measured=True, **summary):
    s = {"inboxes": len(inboxes), "total_capacity": sum(i.get("per_day", 15) for i in inboxes),
         "by_state": {}, "measured": {"measured": measured, "window_from": "2026-09-11",
                                      "window_to": "2026-09-16", "window_sending_days": 4}}
    s.update(summary)
    return {"generated_at": "now", "summary": s, "inboxes": inboxes}


def _ib(email, state, sent=0, domain=None, per_day=15, **kw):
    d = domain or email.split("@", 1)[1]
    row = {"email": email, "domain": d, "state": state, "per_day": per_day,
           "sent_measured": sent, "group": "Acquisition A"}
    row.update(kw)
    return row


def test_an_idle_inbox_that_sent_nothing_is_free():
    r = av.build(_acq([_ib("a@x.co", "unassigned", sent=0)]), {}, TODAY)
    assert r["summary"]["free_inboxes"] == 1
    assert r["summary"]["free_capacity"] == 15


def test_an_idle_inbox_that_is_still_sending_is_not_free():
    """The 2-day gap between sequence steps looks exactly like this: campaign
    state reads quiet, the mailbox is demonstrably working."""
    r = av.build(_acq([_ib("a@x.co", "parked", sent=40)]), {}, TODAY)
    assert r["summary"]["free_inboxes"] == 0
    assert r["free"] == []


def test_a_sending_inbox_is_never_free_even_with_no_measured_sends():
    r = av.build(_acq([_ib("a@x.co", "sending", sent=0)]), {}, TODAY)
    assert r["summary"]["free_inboxes"] == 0


def test_a_blocked_inbox_is_not_free():
    """Blocked means it could not send if we did reallocate it."""
    r = av.build(_acq([_ib("a@x.co", "blocked", sent=0)]), {}, TODAY)
    assert r["summary"]["free_inboxes"] == 0


def test_unmeasured_withholds_the_number_rather_than_printing_zero():
    r = av.build(_acq([_ib("a@x.co", "unassigned", sent=0)], measured=False), {}, TODAY)
    assert r["summary"]["free_inboxes"] is None
    assert r["summary"]["free_capacity"] is None
    assert any("could not be measured" in n for n in r["notes"])


def test_domains_roll_up_and_flag_the_three_per_domain_cap():
    rows = [_ib(f"u{i}@shared.co", "sending") for i in range(4)]
    rows.append(_ib("solo@other.co", "sending"))
    r = av.build(_acq(rows), {}, TODAY)
    assert r["summary"]["domains"] == 2
    assert r["summary"]["over_cap_domains"] == 1
    assert r["over_cap"][0]["domain"] == "shared.co"
    assert r["over_cap"][0]["inboxes"] == 4
    # exactly three is the cap, not over it
    r3 = av.build(_acq(rows[:3]), {}, TODAY)
    assert r3["summary"]["over_cap_domains"] == 0


def test_a_domain_missing_from_the_registrar_reads_unknown_not_healthy():
    r = av.build(_acq([_ib("a@x.co", "sending")]), {}, TODAY)
    d = r["domains"][0]
    assert d["auto_renew"] is None and d["expires"] is None
    assert d["known_to_registrar"] is False
    assert r["summary"]["lapsing_domains"] == 0       # unknown is not an alarm
    assert any("expiry is unknown" in n for n in r["notes"])


def test_an_unreadable_registrar_is_not_an_empty_one():
    r = av.build(_acq([_ib("a@x.co", "sending")]), None, TODAY)
    assert r["summary"]["registrar_read"] is False


def test_a_lapsing_domain_with_live_senders_is_raised():
    reg = {"x.co": {"registrar": "spaceship", "auto_renew": False, "expires": "2026-10-10"}}
    r = av.build(_acq([_ib("a@x.co", "sending")]), reg, TODAY)
    assert r["summary"]["lapsing_domains"] == 1
    assert r["lapsing"][0]["days_to_expiry"] == 22


def test_a_lapsing_domain_with_nothing_on_it_is_not_raised_here():
    """That is the domain-expiry board's job; this tab is about live sending."""
    reg = {"x.co": {"registrar": "spaceship", "auto_renew": False, "expires": "2026-10-10"}}
    r = av.build(_acq([_ib("a@x.co", "unassigned")]), reg, TODAY)
    assert r["summary"]["lapsing_domains"] == 0


def test_auto_renewing_domain_is_not_lapsing():
    reg = {"x.co": {"registrar": "spaceship", "auto_renew": True, "expires": "2026-09-20"}}
    r = av.build(_acq([_ib("a@x.co", "sending")]), reg, TODAY)
    assert r["summary"]["lapsing_domains"] == 0


def test_followup_only_senders_are_called_out_as_in_use():
    r = av.build(_acq([_ib("a@x.co", "sending")], followup_only_inboxes=195), {}, TODAY)
    assert any("working through follow-ups" in n for n in r["notes"])


def test_phantom_idle_is_reported_not_silently_dropped():
    acq = _acq([_ib("a@x.co", "parked", sent=30)])
    acq["summary"]["measured"]["phantom_inboxes"] = 1
    r = av.build(acq, {}, TODAY)
    assert any("still sending" in n for n in r["notes"])


def test_an_upstream_error_passes_straight_through():
    r = av.build({"error": "overview_v2 cache empty"}, {}, TODAY)
    assert r == {"error": "overview_v2 cache empty"}


def test_a_bad_expiry_date_is_unknown_not_a_crash():
    reg = {"x.co": {"registrar": "porkbun", "auto_renew": False, "expires": "not-a-date"}}
    r = av.build(_acq([_ib("a@x.co", "sending")]), reg, TODAY)
    assert r["domains"][0]["days_to_expiry"] is None
    assert r["summary"]["lapsing_domains"] == 0


def test_burned_acquisition_inboxes_are_surfaced():
    rows = [_ib("ok@x.co", "sending"),
            _ib("bad@x.co", "blocked", health="burned", bounce_3d=9, why=["burned — replace"])]
    r = av.build(_acq(rows), {}, TODAY, replacement_pool=45)
    assert r["summary"]["burned"] == 1
    assert r["burned"][0]["email"] == "bad@x.co"
    assert r["summary"]["replacement_pool"] == 45


def test_burned_with_an_empty_replacement_pool_is_called_out():
    rows = [_ib("bad@x.co", "blocked", health="burned")]
    r = av.build(_acq(rows), {}, TODAY, replacement_pool=0)
    assert any("nothing to swap" in n for n in r["notes"])


def test_an_unreadable_replacement_pool_is_not_an_empty_one():
    rows = [_ib("bad@x.co", "blocked", health="burned")]
    r = av.build(_acq(rows), {}, TODAY, replacement_pool=None)
    assert r["summary"]["replacement_pool"] is None
    assert not any("nothing to swap" in n for n in r["notes"])


def test_in_use_counts_every_working_state_not_send_volume():
    """Tim's rule: sending to new leads, sending follow-ups, or between
    sequences all count as in use. The old ratio (sends/day over capacity/day)
    called a follow-up sequence 'capacity we do not use'."""
    rows = [_ib(f"s{n}@x.co", "sending") for n in range(97)]
    rows += [_ib("free@x.co", "unassigned", sent=0)]
    rows += [_ib("dead@x.co", "blocked")]
    r = av.build(_acq(rows, followup_only_inboxes=90), {}, TODAY)
    assert r["summary"]["in_use"] == 97
    assert r["summary"]["usable"] == 98        # blocked is out of both sides
    assert r["summary"]["in_use_pct"] == 99


def test_a_blocked_inbox_is_not_counted_against_utilisation():
    rows = [_ib("a@x.co", "sending"), _ib("b@x.co", "blocked", health="burned")]
    r = av.build(_acq(rows), {}, TODAY)
    assert r["summary"]["in_use_pct"] == 100


def test_followup_only_is_described_as_working_not_as_waste():
    r = av.build(_acq([_ib("a@x.co", "sending")], followup_only_inboxes=195), {}, TODAY)
    note = " ".join(r["notes"])
    assert "doing its job" in note and "need more leads" in note
    assert "waste" not in note.replace("not waste", "")
