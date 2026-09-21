"""What the acquisition fleet is doing today.

The state that matters is `between_sequences`: an inbox mid-sequence with
nothing due this morning. It looks idle by every cheap measure and is not.
"""
import acq_capacity as ac

DAY = "2026-09-19"


def ib(email, camps, per_day=15, state="sending"):
    return {"email": email, "campaigns": camps, "per_day": per_day, "state": state}


def facts(**kw):
    """name -> {status, remaining, in_progress}"""
    return {k: {"status": "ACTIVE", **v} for k, v in kw.items()}


def cap(out, state):
    return next(s for s in out["states"] if s["state"] == state)["capacity"]


def test_new_leads_beats_followups_when_both_are_queued():
    f = facts(c1={"remaining": 500, "in_progress": 200})
    out = ac.work_breakdown([ib("a@x.co", ["c1"])], f, {"a@x.co": 15}, DAY)
    assert cap(out, "new_leads") == 15
    assert cap(out, "followups") == 0


def test_followups_when_the_new_lead_queue_is_empty_and_it_sent_today():
    f = facts(c1={"remaining": 0, "in_progress": 200})
    out = ac.work_breakdown([ib("a@x.co", ["c1"])], f, {"a@x.co": 12}, DAY)
    assert cap(out, "followups") == 15


def test_between_sequences_when_mid_sequence_but_nothing_sent_today():
    """The gap. Working, but with nothing due this morning — and moving it cuts
    a live sequence."""
    f = facts(c1={"remaining": 0, "in_progress": 200})
    out = ac.work_breakdown([ib("a@x.co", ["c1"])], f, {"other@x.co": 5}, DAY)
    assert cap(out, "between_sequences") == 15
    assert cap(out, "followups") == 0
    # and it still counts as working capacity, never as spare
    assert out["working_capacity"] == 15


def test_an_empty_campaign_is_not_working():
    f = facts(c1={"remaining": 0, "in_progress": 0})
    out = ac.work_breakdown([ib("a@x.co", ["c1"])], f, {}, DAY)
    assert cap(out, "no_work") == 15
    assert out["working_capacity"] == 0


def test_no_active_campaign_is_unallocated():
    f = {"c1": {"status": "PAUSED", "remaining": 900, "in_progress": 5}}
    out = ac.work_breakdown([ib("a@x.co", ["c1"])], f, {}, DAY)
    assert cap(out, "unallocated") == 15


def test_a_blocked_inbox_is_never_counted_as_working():
    f = facts(c1={"remaining": 500, "in_progress": 0})
    out = ac.work_breakdown([ib("a@x.co", ["c1"], state=ac.BLOCKED)], f, {"a@x.co": 9}, DAY)
    assert cap(out, "blocked") == 15
    assert out["working_capacity"] == 0


def test_an_unknown_queue_counts_as_still_having_work():
    """Freeing a live sender is the expensive mistake; leaving one be is not."""
    f = facts(c1={"remaining": None, "in_progress": None})
    out = ac.work_breakdown([ib("a@x.co", ["c1"])], f, {}, DAY)
    assert cap(out, "new_leads") == 15


def test_without_send_data_the_gap_cannot_be_separated_and_says_so():
    f = facts(c1={"remaining": 0, "in_progress": 200})
    out = ac.work_breakdown([ib("a@x.co", ["c1"])], f, None, None)
    assert out["measured"] is False
    assert cap(out, "followups") == 15          # not split, not invented
    assert cap(out, "between_sequences") == 0


def test_capacity_adds_up_to_the_fleet():
    f = facts(c1={"remaining": 5, "in_progress": 0}, c2={"remaining": 0, "in_progress": 9})
    rows = [ib("a@x.co", ["c1"]), ib("b@x.co", ["c2"]), ib("c@x.co", []),
            ib("d@x.co", ["c1"], state=ac.BLOCKED)]
    out = ac.work_breakdown(rows, f, {"b@x.co": 3}, DAY)
    assert out["total_capacity"] == 60
    assert sum(s["capacity"] for s in out["states"]) == 60
    assert sum(s["inboxes"] for s in out["states"]) == 4
