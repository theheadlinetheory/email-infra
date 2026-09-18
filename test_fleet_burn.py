"""Burn rate rules. The ones that matter are about not inventing a number."""
import fleet_burn as fb

TODAY = "2026-09-19"


def st(burned=(), at_risk=0, healthy=0):
    rows = [{"status": "burned", "email": e} for e in burned]
    rows += [{"status": "at_risk", "email": f"r{i}@x.co"} for i in range(at_risk)]
    rows += [{"status": "healthy", "email": f"h{i}@x.co"} for i in range(healthy)]
    return rows


def snap(date_, burned):
    return {"date": date_, "counts": {"burned": len(burned)}, "burned": sorted(burned)}


def test_day_one_says_not_yet_rather_than_zero():
    """A rate of 0 on the first day would read as 'nothing is burning'."""
    r = fb.build([], st(burned=["a@x.co"], at_risk=626), 45, TODAY)
    assert r["measured"] is False and r["recording"] is True
    assert r["summary"]["per_week"] is None
    assert "not yet known" in r["reason"]
    # the trustworthy counts are still reported
    assert r["summary"]["burned_now"] == 1 and r["summary"]["at_risk"] == 626


def test_a_rate_appears_once_there_is_something_to_compare():
    hist = [snap("2026-09-12", ["a@x.co"])]
    r = fb.build(hist, st(burned=["a@x.co", "b@x.co"]), 45, TODAY)
    assert r["measured"] is True
    assert r["summary"]["new_burns_window"] == 1
    assert r["summary"]["days_observed"] == 7
    assert r["summary"]["per_week"] == 1.0


def test_new_burns_not_net_change():
    """A cancelled inbox leaves the burned set. Net change would cancel out a
    genuinely new burn and can even go negative while the fleet is burning."""
    hist = [snap("2026-09-12", ["old1@x.co", "old2@x.co"])]
    r = fb.build(hist, st(burned=["new@x.co"]), 45, TODAY)
    assert r["summary"]["new_burns_window"] == 1     # not -1
    assert r["summary"]["burned_now"] == 1


def test_an_inbox_already_burned_is_not_counted_again():
    hist = [snap("2026-09-12", ["a@x.co"])]
    r = fb.build(hist, st(burned=["a@x.co"]), 45, TODAY)
    assert r["summary"]["new_burns_window"] == 0


def test_runway_is_the_pool_over_the_rate():
    hist = [snap("2026-09-12", [])]
    r = fb.build(hist, st(burned=[f"b{i}@x.co" for i in range(2)]), 45, TODAY)
    assert r["summary"]["per_week"] == 2.0
    assert r["summary"]["runway_weeks"] == 22.5


def test_nothing_burning_gives_no_runway_rather_than_infinite_cover():
    hist = [snap("2026-09-12", [])]
    r = fb.build(hist, st(), 45, TODAY)
    assert r["summary"]["per_week"] == 0.0
    assert r["summary"]["runway_weeks"] is None


def test_an_unreadable_pool_gives_no_runway_not_a_zero():
    hist = [snap("2026-09-12", [])]
    r = fb.build(hist, st(burned=["a@x.co"]), None, TODAY)
    assert r["summary"]["replacement_pool"] is None
    assert r["summary"]["runway_weeks"] is None


def test_unreadable_status_withholds_everything():
    r = fb.build([snap("2026-09-12", [])], None, 45, TODAY)
    assert r["measured"] is False and r["recording"] is False
    assert r["summary"]["burned_now"] is None


def test_todays_snapshot_is_never_compared_against_itself():
    """An existing record for today would otherwise make the rate read zero."""
    hist = [snap("2026-09-12", []), snap(TODAY, ["a@x.co"])]
    r = fb.build(hist, st(burned=["a@x.co"]), 45, TODAY)
    assert r["summary"]["new_burns_window"] == 1


def test_record_replaces_the_same_day_rather_than_duplicating_it():
    h = fb.record([], st(burned=["a@x.co"]), TODAY)
    h = fb.record(h, st(burned=["a@x.co", "b@x.co"]), TODAY)
    assert len(h) == 1 and h[0]["counts"]["burned"] == 2


def test_record_keeps_days_in_order_and_bounded():
    h = []
    for d in range(1, 10):
        h = fb.record(h, st(burned=[f"b{d}@x.co"]), f"2026-09-{d:02d}")
    assert [x["date"] for x in h] == sorted(x["date"] for x in h)
    assert len(h) == 9
    assert len(fb.record(h, st(), "2026-12-31")) <= fb.MAX_SNAPSHOTS


def test_the_snapshot_stores_who_is_burned_not_only_how_many():
    s = fb.snapshot(st(burned=["a@x.co", "B@X.co"]), TODAY)
    assert s["burned"] == ["a@x.co", "b@x.co"]
