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


def test_an_empty_list_is_not_a_shape_change():
    """An empty list was briefly raised here, on the theory that the endpoint
    had become a catch-all. It had not -- stale credentials return an empty
    list instead of a 401, and the endpoint came straight back when the
    password was fixed. Raising on empty would fail the nightly EVERY Saturday,
    when the fleet genuinely sends nothing. Whether emptiness is a fault is the
    caller's question, and min_records already encodes it."""
    assert hd._rows_from({"ok": True, "data": []}, "2026-09-19", "2026-09-21") == []


def test_an_empty_window_still_fails_when_records_were_required(monkeypatch):
    """The guard that actually matters: a multi-day window passing MIN_RECORDS
    must still refuse an empty answer, and say why."""
    class R:
        status_code = 200
        def json(self): return {"ok": True, "data": []}
    monkeypatch.setattr(hd, "_headers", lambda: {})
    monkeypatch.setattr(hd.time, "sleep", lambda *_: None)
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: R())
    with pytest.raises(RuntimeError) as e:
        hd.fetch_window("2026-09-19", "2026-09-21", retries=1, min_records=50)
    assert "SMARTLEAD_LOGIN_PASSWORD" in str(e.value)


def test_a_single_empty_day_is_accepted(monkeypatch):
    """Saturday. min_records defaults to 0, so a true zero is recorded."""
    class R:
        status_code = 200
        def json(self): return {"ok": True, "data": []}
    monkeypatch.setattr(hd, "_headers", lambda: {})
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: R())
    assert hd.fetch_window("2026-09-19", "2026-09-19", retries=1) == {}


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


# ── the cache the list is actually built from ────────────────────────────
# The banner measured inbox_health_daily while the burned list was rendered
# from the health_fleet CACHE. Those disagree exactly when it matters: the
# table current, the cache days old, and a frozen list calling itself fresh.

from datetime import datetime, timedelta, timezone


def _hours_ago(h):
    return (datetime.now(timezone.utc) - timedelta(hours=h)).isoformat()


def test_a_stale_cache_is_stale_even_when_the_table_is_current(monkeypatch):
    monkeypatch.setattr(hd, "latest_health_date", lambda: "2026-09-22")
    f = hd.health_freshness(today="2026-09-22", built_at=_hours_ago(24 * 6))
    assert f["stale"] is True
    assert "built" in f["reason"] and "rebuilt" in f["reason"]


def test_a_fresh_cache_and_fresh_table_is_fresh(monkeypatch):
    monkeypatch.setattr(hd, "latest_health_date", lambda: "2026-09-22")
    f = hd.health_freshness(today="2026-09-22", built_at=_hours_ago(3))
    assert f["stale"] is False and f["built_hours_ago"] == 3.0


def test_one_missed_nightly_does_not_cry_wolf(monkeypatch):
    """36h covers a single skipped run; firing on every hiccup trains people
    to ignore the banner."""
    monkeypatch.setattr(hd, "latest_health_date", lambda: "2026-09-22")
    assert hd.health_freshness(today="2026-09-22", built_at=_hours_ago(30))["stale"] is False


def test_an_unreadable_build_time_is_stale(monkeypatch):
    """A timestamp we were given but cannot parse is unknown age, not fresh."""
    monkeypatch.setattr(hd, "latest_health_date", lambda: "2026-09-22")
    f = hd.health_freshness(today="2026-09-22", built_at="not-a-timestamp")
    assert f["stale"] is True and "age unknown" in f["reason"]


def test_no_build_time_falls_back_to_the_table(monkeypatch):
    """Callers that have no cache timestamp keep the old behaviour rather than
    being told everything is stale."""
    monkeypatch.setattr(hd, "latest_health_date", lambda: "2026-09-22")
    assert hd.health_freshness(today="2026-09-22")["stale"] is False


def test_a_stale_table_still_wins(monkeypatch):
    """A fresh rebuild of old data is still old data."""
    monkeypatch.setattr(hd, "latest_health_date", lambda: "2026-09-10")
    f = hd.health_freshness(today="2026-09-22", built_at=_hours_ago(1))
    assert f["stale"] is True and "12 days old" in f["reason"]


# ── one parser, not three ────────────────────────────────────────────────
# The same read was copy-pasted into sync.py and dashboard.py. Fixing
# health_daily left both broken, and sync's failure took the whole Overview
# tab down for five days: zero health records -> its own guard aborts ->
# overview_v2 never rewritten -> the landing page silently shows a smaller
# fleet. These pin the shapes every caller must survive.

def test_both_wire_shapes_parse_to_the_same_rows():
    rows = [{"from_email": "a@x.co", "bounce_rate": "4"}]
    as_object = {"data": {"email_health_metrics": rows}}
    as_list = {"data": rows}
    assert hd._rows_from(as_object, "s", "e") == hd._rows_from(as_list, "s", "e") == rows


def test_the_list_shape_no_longer_yields_zero_records():
    """The exact failure: the old `(data or {}).get("email_health_metrics", [])`
    returned [] for this payload, and sync aborted on `len(health) < 50`."""
    rows = [{"from_email": f"m{i}@x.co"} for i in range(60)]
    assert len(hd._rows_from({"data": rows}, "s", "e")) == 60


# ── sync must never untag the fleet ──────────────────────────────────────
# `a["tags"] = tag_map.get(a["id"], [])` overwrote unconditionally. When the
# GraphQL tag fetch failed, every account was untagged, no account matched a
# client, and the overview shipped 1,771 accounts attached to nobody.

def apply_tags(accounts: list, tag_map: dict) -> int:
    """sync's tag application, extracted."""
    kept = 0
    for a in accounts:
        mapped = tag_map.get(a["id"])
        if mapped:
            a["tags"] = mapped
        elif a.get("tags"):
            kept += 1
        else:
            a["tags"] = []
    return kept


def test_graphql_tags_win_when_present():
    """GraphQL carries the real client tag; REST does not always reflect it."""
    accs = [{"id": 1, "tags": [{"name": "stale"}]}]
    apply_tags(accs, {1: [{"name": "Landry's Landscape"}]})
    assert accs[0]["tags"] == [{"name": "Landry's Landscape"}]


def test_an_empty_tag_map_does_not_untag_the_fleet():
    """The regression: a failed GraphQL call wiped every tag."""
    accs = [{"id": i, "tags": [{"name": "Timesavers"}]} for i in range(3)]
    kept = apply_tags(accs, {})
    assert kept == 3
    assert all(a["tags"] for a in accs), "fleet was untagged by an empty map"


def test_an_account_with_no_tags_anywhere_stays_empty():
    accs = [{"id": 1, "tags": []}]
    apply_tags(accs, {})
    assert accs[0]["tags"] == []


def test_a_partial_map_only_replaces_what_it_covers():
    accs = [{"id": 1, "tags": [{"name": "A"}]}, {"id": 2, "tags": [{"name": "B"}]}]
    kept = apply_tags(accs, {1: [{"name": "A-new"}]})
    assert accs[0]["tags"] == [{"name": "A-new"}]
    assert accs[1]["tags"] == [{"name": "B"}] and kept == 1


def test_accounts_attributed_to_nobody_is_a_refusal_not_a_smaller_fleet():
    """1,771 accounts belonging to no client is unusable, not merely partial —
    health_snapshot, the burnt list and the Inboxes tab all read attribution
    from it."""
    overview = {"total_accounts": 1771, "clients": [{"accounts": 0} for _ in range(45)]}
    attributed = sum(c.get("accounts") or 0 for c in overview["clients"])
    assert overview["total_accounts"] and not attributed
