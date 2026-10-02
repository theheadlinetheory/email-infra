"""The burnt-inbox count must be right every day, or say loudly that it isn't.

This is the dashboard's whole purpose. It ran silently wrong for a week:
the daily snapshot failed, GitHub went red, and nobody watches GitHub. A red
tick in a tab nobody opens is not an alert.
"""
import health_snapshot as hs


class Store:
    def __init__(self, state=None): self.s = dict(state or {})
    def get_state(self, k): return self.s.get(k)
    def set_state(self, k, v): self.s[k] = v


def _wire(monkeypatch, state=None):
    st = Store(state)
    monkeypatch.setattr(hs, "store", st)
    sent = []
    monkeypatch.setattr(hs, "alert_snapshot_broken",
                        lambda detail, fails: (sent.append((detail, fails)),
                                               {"alerted": True, "via": "test"})[1])
    return st, sent


def test_the_first_failure_alerts(monkeypatch):
    """Not the third. Waiting for a streak is how a week went by."""
    st, sent = _wire(monkeypatch)
    hs.record_run(False, "metrics returned nothing")
    assert len(sent) == 1
    assert sent[0][1] == 1
    assert st.s[hs.SNAPSHOT_RUN_KEY]["consecutive_failures"] == 1


def test_a_continuing_outage_does_not_re_alert_every_run(monkeypatch):
    """Four runs a day must not mean four pings an hour."""
    st, sent = _wire(monkeypatch)
    for _ in range(4):
        hs.record_run(False, "still broken")
    assert len(sent) == 1, "re-alerted during one continuous outage"
    assert st.s[hs.SNAPSHOT_RUN_KEY]["consecutive_failures"] == 4


def test_recovery_clears_the_latch_so_the_next_outage_alerts(monkeypatch):
    """Without this the pipeline alerts once, ever."""
    st, sent = _wire(monkeypatch)
    hs.record_run(False, "broken")
    hs.record_run(True)
    assert st.s[hs.SNAPSHOT_RUN_KEY]["consecutive_failures"] == 0
    assert not st.s[hs.SNAPSHOT_RUN_KEY].get("alerted_at")
    hs.record_run(False, "broken again")
    assert len(sent) == 2


def test_success_records_a_last_success_timestamp(monkeypatch):
    st, _ = _wire(monkeypatch)
    hs.record_run(True)
    rec = st.s[hs.SNAPSHOT_RUN_KEY]
    assert rec["ok"] is True and rec["last_success_at"]


def test_a_failure_keeps_the_previous_success_timestamp(monkeypatch):
    """So "how long has this been broken" is answerable."""
    st, _ = _wire(monkeypatch)
    hs.record_run(True)
    was = st.s[hs.SNAPSHOT_RUN_KEY]["last_success_at"]
    hs.record_run(False, "broken")
    assert st.s[hs.SNAPSHOT_RUN_KEY]["last_success_at"] == was


def test_telemetry_never_breaks_the_run(monkeypatch):
    """Recording the outcome must not become a new way to fail the snapshot."""
    class Broken:
        def get_state(self, k): raise RuntimeError("store down")
        def set_state(self, k, v): raise RuntimeError("store down")
    monkeypatch.setattr(hs, "store", Broken())
    hs.record_run(False, "x")          # must not raise
    hs.record_run(True)


def test_the_alert_names_the_consequence_not_just_the_fault(monkeypatch):
    """"Snapshot failed" means nothing to a reader. "The list will
    under-report burns" is the sentence that gets it fixed."""
    posted = {}
    monkeypatch.setattr(hs.requests if hasattr(hs, "requests") else hs, "__name__", "hs")
    import os
    monkeypatch.setenv("SLACK_ZAPMAIL_WEBHOOK", "https://hooks.example/x")

    class R:
        status_code = 200
    import requests
    monkeypatch.setattr(requests, "post",
                        lambda url, json=None, timeout=None: (posted.update(json or {}), R())[1])
    hs.alert_snapshot_broken("endpoint returned nothing", 2)
    text = posted.get("text", "")
    assert "UNDER-REPORT" in text
    assert "2 time(s)" in text
