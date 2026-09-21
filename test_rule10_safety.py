"""The nightly reply scan, and the refusal that depends on it.

Every test here is about the same failure: reporting "safe to delete" from a
scan that did not actually look.
"""
from datetime import datetime, timedelta

import pytest

import rule10_safety as r10


class FakeStore:
    def __init__(self, rec=None):
        self.rec = rec
    def get_state(self, k): return self.rec
    def set_state(self, k, v): self.rec = v


@pytest.fixture
def store(monkeypatch):
    s = FakeStore()
    monkeypatch.setattr(r10, "store", s)
    return s


def rec(ts_hours_ago=1.0, **kw):
    base = {"ts": (datetime.now() - timedelta(hours=ts_hours_ago)).isoformat(),
            "candidates": ["a@x.co"], "active": [], "positives": [],
            "positive_threads": {}, "campaigns_scanned": 415}
    base.update(kw)
    return base


# ── verdict ──────────────────────────────────────────────────────────────

def test_no_scan_on_record_is_stale(store):
    assert r10.verdict(["a@x.co"])["stale"] is True


def test_a_fresh_clean_scan_clears(store):
    store.rec = rec()
    v = r10.verdict(["a@x.co"])
    assert v["stale"] is False and v["active"] == [] and v["positives"] == []


def test_an_old_scan_is_stale(store):
    store.rec = rec(ts_hours_ago=r10.MAX_AGE_H + 1)
    v = r10.verdict(["a@x.co"])
    assert v["stale"] is True and "ago" in v["reason"]


def test_a_scan_that_errored_is_stale(store):
    store.rec = {"error": "campaign list unreadable"}
    assert r10.verdict(["a@x.co"])["stale"] is True


def test_an_unreadable_timestamp_is_stale(store):
    store.rec = rec()
    store.rec["ts"] = "not a date"
    assert r10.verdict(["a@x.co"])["stale"] is True


def test_a_mailbox_the_scan_never_saw_invalidates_the_whole_answer(store):
    """The dangerous shape: 9 of 10 were scanned, so the 10th looks clean."""
    store.rec = rec(candidates=["a@x.co"])
    v = r10.verdict(["a@x.co", "never-scanned@x.co"])
    assert v["stale"] is True and v["missing"] == ["never-scanned@x.co"]


def test_verdict_narrows_to_the_mailboxes_asked_about(store):
    store.rec = rec(candidates=["a@x.co", "b@x.co"],
                    active=["b@x.co"], positives=["b@x.co"])
    v = r10.verdict(["a@x.co"])
    assert v["active"] == [] and v["positives"] == []


# ── scan ─────────────────────────────────────────────────────────────────

class Resp:
    def __init__(self, payload): self._p = payload
    def json(self): return self._p


def test_scan_reports_an_unreadable_campaign_list_rather_than_an_empty_one(monkeypatch):
    monkeypatch.setattr(r10, "_get", lambda *a, **k: None)
    assert "unreadable" in r10.scan(["a@x.co"])["error"]


def test_scan_refuses_when_any_campaign_could_not_be_read(monkeypatch):
    """One unreadable campaign could be the one holding the reply."""
    def fake(path, **kw):
        if path == "/campaigns":
            return Resp([{"id": 1, "status": "PAUSED"}, {"id": 2, "status": "ACTIVE"}])
        return None if path.endswith("/2/email-accounts") else Resp([])
    monkeypatch.setattr(r10, "_get", fake)
    monkeypatch.setattr(r10.time, "sleep", lambda *_: None)
    assert "could not be read" in r10.scan(["a@x.co"])["error"]


def test_scan_finds_a_reply_on_a_paused_campaign(monkeypatch):
    """The exact case the ACTIVE-only in-request scan is blind to."""
    def fake(path, **kw):
        if path == "/campaigns":
            return Resp([{"id": 7, "status": "PAUSED"}])
        return Resp([{"from_email": "A@X.co"}])
    monkeypatch.setattr(r10, "_get", fake)
    monkeypatch.setattr(r10.time, "sleep", lambda *_: None)
    import health_positive as hp
    monkeypatch.setattr(hp, "owned_positive_threads",
                        lambda em, cid: [{"lead_id": "9"}] if em == "a@x.co" else [])
    out = r10.scan(["a@x.co"])
    assert out["positives"] == ["a@x.co"]
    assert out["active"] == []                      # paused, so not "in use"
    assert out["positive_threads"]["a@x.co"][0]["campaign_id"] == 7


def test_scan_propagates_a_failed_reply_lookup(monkeypatch):
    def fake(path, **kw):
        if path == "/campaigns":
            return Resp([{"id": 7, "status": "ACTIVE"}])
        return Resp([{"from_email": "a@x.co"}])
    monkeypatch.setattr(r10, "_get", fake)
    monkeypatch.setattr(r10.time, "sleep", lambda *_: None)
    import health_positive as hp
    def boom(em, cid): raise RuntimeError("leads-export failed")
    monkeypatch.setattr(hp, "owned_positive_threads", boom)
    assert "positive-reply check failed" in r10.scan(["a@x.co"])["error"]


def test_refresh_does_not_cache_a_failed_scan(store, monkeypatch):
    monkeypatch.setattr(r10, "scan", lambda c: {"error": "nope"})
    r10.refresh(["a@x.co"])
    assert store.rec is None
