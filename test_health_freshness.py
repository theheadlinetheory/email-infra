"""The burned list must never present a frozen answer as a current one.

Every test here is the same failure: the metrics feed stops, inbox_health_daily
stops gaining rows, and the burned list keeps rendering the last good day as
though it were today. A quiet week and a broken pipeline look identical.
"""
import pytest

import health_daily as hd


# ── shape changes must raise, never degrade to zero ──────────────────────

def test_the_old_object_shape_still_parses():
    payload = {"data": {"email_health_metrics": [{"from_email": "a@x.co"}]}}
    assert hd._rows_from(payload, "2026-09-01", "2026-09-02") == [{"from_email": "a@x.co"}]


def test_an_empty_object_shape_is_a_legitimate_zero():
    """A weekend genuinely has no rows. The OBJECT shape saying so is honest."""
    payload = {"data": {"email_health_metrics": []}}
    assert hd._rows_from(payload, "2026-09-01", "2026-09-02") == []


def test_the_new_empty_list_shape_raises():
    """This is the regression. SmartLead now answers {"ok":true,"data":[]} for
    every path, including ones that never existed, so an empty list is a
    catch-all -- not a report that no inbox bounced."""
    with pytest.raises(hd.MetricsShapeChanged) as e:
        hd._rows_from({"ok": True, "data": []}, "2026-09-19", "2026-09-21")
    assert "catch-all" in str(e.value)


def test_a_populated_list_shape_is_accepted():
    """If the feed comes back in the new shape WITH data, use it."""
    rows = [{"from_email": "a@x.co", "bounce_rate": "3"}]
    assert hd._rows_from({"data": rows}, "2026-09-01", "2026-09-02") == rows


def test_a_missing_metrics_key_raises_rather_than_defaulting():
    with pytest.raises(hd.MetricsShapeChanged):
        hd._rows_from({"data": {"something_else": []}}, "2026-09-01", "2026-09-02")


def test_a_non_object_payload_raises():
    with pytest.raises(hd.MetricsShapeChanged):
        hd._rows_from([], "2026-09-01", "2026-09-02")


def test_shape_change_is_a_runtime_error_so_existing_callers_still_catch_it():
    assert issubclass(hd.MetricsShapeChanged, RuntimeError)


# ── freshness ────────────────────────────────────────────────────────────

def test_fresh_data_is_not_stale(monkeypatch):
    monkeypatch.setattr(hd, "latest_health_date", lambda: "2026-09-22")
    f = hd.health_freshness(today="2026-09-22")
    assert f["stale"] is False and f["age_days"] == 0


def test_a_friday_to_monday_gap_is_not_stale(monkeypatch):
    """THT sends weekdays only. Friday's data read on Monday is 3 days old and
    is the NORMAL state every week -- crying wolf here would train us to
    ignore the banner."""
    monkeypatch.setattr(hd, "latest_health_date", lambda: "2026-09-18")   # Fri
    assert hd.health_freshness(today="2026-09-21")["stale"] is False      # Mon


def test_a_missed_weekday_is_stale(monkeypatch):
    """One day past the Friday-to-Monday allowance means a weekday was missed."""
    monkeypatch.setattr(hd, "latest_health_date", lambda: "2026-09-18")
    assert hd.health_freshness(today="2026-09-22")["stale"] is True


def test_old_data_is_stale(monkeypatch):
    monkeypatch.setattr(hd, "latest_health_date", lambda: "2026-09-17")
    f = hd.health_freshness(today="2026-09-22")
    assert f["stale"] is True and f["age_days"] == 5
    assert "5 days old" in f["reason"]


def test_an_unknown_age_is_treated_as_stale(monkeypatch):
    """The dangerous default. If we cannot establish how old the data is, the
    list must not claim to be current."""
    monkeypatch.setattr(hd, "latest_health_date", lambda: None)
    f = hd.health_freshness(today="2026-09-22")
    assert f["stale"] is True and f["age_days"] is None


def test_an_unreadable_date_is_stale(monkeypatch):
    monkeypatch.setattr(hd, "latest_health_date", lambda: "not-a-date")
    assert hd.health_freshness(today="2026-09-22")["stale"] is True


def test_latest_health_date_returns_none_rather_than_guessing(monkeypatch):
    """A failed read must be None (unknown), never today's date."""
    monkeypatch.setenv("SUPABASE_URL", "")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "")
    monkeypatch.setenv("SUPABASE_KEY", "")
    assert hd.latest_health_date() is None
