"""Per-mailbox scheduled removal.

The point of this path is that three cleanup mailboxes sit on domains that also
carry live client inboxes. Cancelling by domain there would schedule a client's
senders for deletion, so every test is about not over-reaching.
"""
import pytest

import zapmail_removals as zr


class FakeSetup:
    def __init__(self, domains):
        self._d = domains

    def zm_list_domains(self):
        return self._d


DOMAINS = [
    {"domain": "shared.info", "mailboxes": [
        {"id": "m1", "username": "ours"},
        {"id": "m2", "username": "client1"},
        {"id": "m3", "username": "client2"},
    ]},
    {"domain": "solo.info", "mailboxes": [{"id": "m4", "username": "a"}]},
]


@pytest.fixture(autouse=True)
def fake_zapmail(monkeypatch):
    monkeypatch.setattr(zr, "S", FakeSetup(DOMAINS))


def test_a_dry_run_resolves_without_calling_zapmail(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(zr.requests, "put", lambda *a, **k: called.__setitem__("n", 1))
    r = zr.cancel_mailboxes(["ours@shared.info"], dry_run=True)
    assert r["dry_run"] is True and r["count"] == 1
    assert r["resolved"] == {"ours@shared.info": "m1"}
    assert called["n"] == 0


def test_only_the_named_mailbox_is_scheduled_not_its_neighbours(monkeypatch):
    sent = {}

    class R:
        status_code = 200
        text = "ok"

    monkeypatch.setattr(zr.requests, "put",
                        lambda url, headers=None, json=None, timeout=None:
                        (sent.update(json), R())[1])
    monkeypatch.setattr(zr, "_registry", lambda: {})
    monkeypatch.setattr(zr, "_save_registry", lambda e: None)
    r = zr.cancel_mailboxes(["ours@shared.info"], dry_run=False)
    assert r["ok"] is True
    # the client's two mailboxes on the same domain are untouched
    assert sent["mailboxIds"] == ["m1"]
    assert "domainIds" not in sent


def test_an_unresolvable_address_refuses_the_whole_call(monkeypatch):
    """Scheduling "most of" a list and reporting success is how a cancellation
    silently half-happens."""
    called = {"n": 0}
    monkeypatch.setattr(zr.requests, "put", lambda *a, **k: called.__setitem__("n", 1))
    r = zr.cancel_mailboxes(["ours@shared.info", "ghost@nowhere.info"], dry_run=False)
    assert "refusing to schedule a partial list" in r["error"]
    assert r["missing"] == ["ghost@nowhere.info"]
    assert called["n"] == 0


def test_an_empty_list_is_rejected():
    assert zr.cancel_mailboxes([]) == {"error": "no mailboxes given"}


def test_addresses_are_matched_case_insensitively():
    r = zr.cancel_mailboxes(["Ours@Shared.INFO"], dry_run=True)
    assert r["count"] == 1
