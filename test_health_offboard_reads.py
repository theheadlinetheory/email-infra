"""The offboard domain walks must never return a short map.

Both maps feed an offboard decision. A domain missing because of one 429 reads
as "not this client's", which quietly widens what gets cancelled — the failure
looks safe and is not.
"""
import pytest

import health_offboard as ho


class Resp:
    def __init__(self, code, payload=None):
        self.status_code = code
        self._p = payload or {}

    def json(self):
        return self._p


def page(domains, total_pages):
    return {"data": {"domains": domains, "totalPages": total_pages}}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(ho.time, "sleep", lambda *_: None)
    monkeypatch.setenv("ZAPMAIL_API_KEY", "x")


def test_a_complete_walk_returns_every_page(monkeypatch):
    pages = {
        1: Resp(200, page([{"domain": "a.info", "id": "1"}], 2)),
        2: Resp(200, page([{"domain": "b.info", "id": "2"}], 2)),
    }
    monkeypatch.setattr(ho.requests, "get",
                        lambda url, **k: pages[int(url.split("page=")[1].split("&")[0])])
    out = ho.zapmail_domain_ids({"a.info", "b.info"})
    assert out == {"a.info": "1", "b.info": "2"}


def test_a_failed_second_page_raises_rather_than_returning_page_one(monkeypatch):
    pages = {1: Resp(200, page([{"domain": "a.info", "id": "1"}], 2)), 2: Resp(500)}
    monkeypatch.setattr(ho.requests, "get",
                        lambda url, **k: pages[int(url.split("page=")[1].split("&")[0])])
    with pytest.raises(RuntimeError, match="part way through"):
        ho.zapmail_domain_ids({"a.info", "b.info"})


def test_a_rate_limited_page_is_retried_before_giving_up(monkeypatch):
    calls = {"n": 0}

    def get(url, **k):
        p = int(url.split("page=")[1].split("&")[0])
        if p == 1:
            return Resp(200, page([{"domain": "a.info", "id": "1"}], 2))
        calls["n"] += 1
        if calls["n"] < 3:
            return Resp(429)
        return Resp(200, page([{"domain": "b.info", "id": "2"}], 2))

    monkeypatch.setattr(ho.requests, "get", get)
    assert ho.zapmail_domain_ids({"a.info", "b.info"}) == {"a.info": "1", "b.info": "2"}


def test_the_forwarding_walk_has_the_same_guard(monkeypatch):
    pages = {1: Resp(200, page([{"domain": "a.info", "forwardTo": "x"}], 2)), 2: Resp(500)}
    monkeypatch.setattr(ho.requests, "get",
                        lambda url, **k: pages[int(url.split("page=")[1].split("&")[0])])
    with pytest.raises(RuntimeError, match="partial read"):
        ho.domain_forwarding({"a.info"})


def test_no_key_still_returns_empty_without_calling_out(monkeypatch):
    """Not configured is a different thing from a failed read."""
    monkeypatch.delenv("ZAPMAIL_API_KEY", raising=False)
    assert ho.zapmail_domain_ids({"a.info"}) == {}
