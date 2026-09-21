"""A day Smartlead returns nothing for must never be written as a day of zeros.

On 2026-09-17 that wrote 1,902 rows with sent=0 over a Thursday the fleet had
actually sent 9,955 mails on, and every reader downstream — burn rate, capacity,
health scores — believed it for five days.
"""
import health_daily as hd

ATTRS = {f"a{i}@x.co": {"client": "Acme", "domain": "x.co"} for i in range(3)}


def test_an_empty_response_writes_nothing_for_that_day(monkeypatch):
    monkeypatch.setattr(hd, "fetch_window", lambda a, b: {})
    rows, skipped = hd.daily_rows(["2026-09-17"], ATTRS)
    assert rows == []
    assert skipped == ["2026-09-17"]


def test_a_day_with_data_still_zero_fills_the_inboxes_that_were_quiet(monkeypatch):
    """Within a day that DID return records, an inbox absent from them really
    did send zero, and the history must say so."""
    one = {"a0@x.co": {"sent": 40, "bounced": 0, "replied": 2, "opened": 5,
                       "positive_replied": 1, "unique_lead_count": 40,
                       "reply_rate": 5.0, "bounce_rate": 0.0}}
    monkeypatch.setattr(hd, "fetch_window", lambda a, b: one)
    rows, skipped = hd.daily_rows(["2026-09-18"], ATTRS)
    assert skipped == []
    assert len(rows) == 3
    by = {r["email"]: r for r in rows}
    assert by["a0@x.co"]["sent"] == 40
    assert by["a1@x.co"]["sent"] == 0


def test_a_mix_of_good_and_empty_days_keeps_only_the_good(monkeypatch):
    def fake(a, b):
        return {} if a == "2026-09-19" else {"a0@x.co": {
            "sent": 10, "bounced": 0, "replied": 0, "opened": 0,
            "positive_replied": 0, "unique_lead_count": 10,
            "reply_rate": 0.0, "bounce_rate": 0.0}}
    monkeypatch.setattr(hd, "fetch_window", fake)
    rows, skipped = hd.daily_rows(["2026-09-18", "2026-09-19", "2026-09-21"], ATTRS)
    assert skipped == ["2026-09-19"]
    assert {r["date"] for r in rows} == {"2026-09-18", "2026-09-21"}


def test_credential_state_names_what_is_missing(monkeypatch):
    """"401 unauthorized" cost five days of silence because it did not say WHICH
    credential was absent."""
    monkeypatch.delenv("SMARTLEAD_LOGIN_EMAIL", raising=False)
    monkeypatch.delenv("SMARTLEAD_LOGIN_PASSWORD", raising=False)
    monkeypatch.setenv("SMARTLEAD_JWT", "stale.token.here")
    st = hd.credential_state()
    assert st["login_pair"] is False and st["static_jwt"] is True
    assert "SMARTLEAD_LOGIN_EMAIL" in st["fix"]


def test_credential_state_with_nothing_set_says_so(monkeypatch):
    for k in ("SMARTLEAD_LOGIN_EMAIL", "SMARTLEAD_LOGIN_PASSWORD", "SMARTLEAD_JWT"):
        monkeypatch.delenv(k, raising=False)
    st = hd.credential_state()
    assert st["login_pair"] is False and st["static_jwt"] is False
    assert "set SMARTLEAD_LOGIN_EMAIL" in st["fix"]


def test_credential_state_never_leaks_a_value(monkeypatch):
    monkeypatch.setenv("SMARTLEAD_JWT", "super-secret-token")
    monkeypatch.delenv("SMARTLEAD_LOGIN_EMAIL", raising=False)
    assert "super-secret-token" not in str(hd.credential_state())
