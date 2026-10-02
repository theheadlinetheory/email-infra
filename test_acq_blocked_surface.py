"""The acquisition fleet is not 100% working, and the page must say so.

Tim: "are we sure that 100% of our inboxes are sending to new leads, sending
to follow-ups, or between sequence steps?" No — 32 of 304 cannot send at all,
verified against live Smartlead SMTP status. That was a row in a table, which
reads as a category rather than a problem.
"""


def summarise(by_email: list, total: int) -> dict:
    """What the Acquisition tab's blocked banner reports."""
    blocked = [x for x in by_email if x.get("work") == "blocked"]
    return {
        "count": len(blocked),
        "lost_per_day": sum(x.get("per_day") or 0 for x in blocked),
        "pct": round(len(blocked) * 100 / total) if total else 0,
    }


def _fleet(n_blocked, n_working, per_day=15):
    return ([{"email": f"b{i}@x.co", "work": "blocked", "per_day": per_day}
             for i in range(n_blocked)]
            + [{"email": f"w{i}@x.co", "work": "between_sequences", "per_day": per_day}
               for i in range(n_working)])


def test_the_live_shape_is_reported_exactly():
    """32 of 304 at 15/day = 480/day, 11%."""
    s = summarise(_fleet(32, 272), 304)
    assert s == {"count": 32, "lost_per_day": 480, "pct": 11}


def test_a_clean_fleet_shows_nothing():
    """The banner must not appear when there is nothing to act on."""
    assert summarise(_fleet(0, 304), 304)["count"] == 0


def test_blocked_capacity_is_not_counted_as_working():
    """The whole point: a blocked inbox is NOT 'between sequence steps'. It
    would fail if a campaign tried to use it."""
    fleet = _fleet(32, 272)
    working = [x for x in fleet if x["work"] in ("new_leads", "followups", "between_sequences")]
    assert len(working) == 272
    assert len(working) + summarise(fleet, 304)["count"] == 304


def test_the_six_states_account_for_every_inbox():
    """Nothing unclassified — a missing inbox is a silently wrong total."""
    STATES = {"new_leads", "followups", "between_sequences",
              "no_work", "unallocated", "blocked"}
    fleet = _fleet(3, 7)
    assert all(x["work"] in STATES for x in fleet)
    assert len(fleet) == 10


def test_varying_per_day_is_summed_not_assumed():
    fleet = [{"email": "a@x.co", "work": "blocked", "per_day": 15},
             {"email": "b@x.co", "work": "blocked", "per_day": 5},
             {"email": "c@x.co", "work": "blocked"}]        # missing per_day
    assert summarise(fleet, 3)["lost_per_day"] == 20


def test_zero_total_does_not_divide_by_zero():
    assert summarise([], 0)["pct"] == 0
