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


# createdAt matters: Zapmail's ADMIN mailbox is the first one created on a
# domain and cannot be removed while siblings remain. Here `client1` is the
# admin, so `ours` is an ordinary sibling and freely schedulable.
DOMAINS = [
    {"domain": "shared.info", "mailboxes": [
        {"id": "m2", "username": "client1", "createdAt": "2026-01-01T00:00:00Z"},
        {"id": "m1", "username": "ours", "createdAt": "2026-01-02T00:00:00Z"},
        {"id": "m3", "username": "client2", "createdAt": "2026-01-03T00:00:00Z"},
    ]},
    {"domain": "solo.info", "mailboxes": [
        {"id": "m4", "username": "admin", "createdAt": "2026-01-01T00:00:00Z"},
        {"id": "m5", "username": "second", "createdAt": "2026-01-02T00:00:00Z"},
    ]},
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


def test_an_admin_mailbox_is_reported_not_sent(monkeypatch):
    """Zapmail 400s on an admin mailbox while siblings remain, and that refusal
    kills the whole call. Catching it here keeps one blocked address from
    taking a batch of good ones down with it."""
    called = {"n": 0}
    monkeypatch.setattr(zr.requests, "put", lambda *a, **k: called.__setitem__("n", 1))
    r = zr.cancel_mailboxes(["admin@solo.info"], dry_run=False)
    assert r["blocked_admin"] == ["admin@solo.info"]
    assert "siblings remain" in r["error"]
    assert called["n"] == 0


def test_a_batch_schedules_what_it_can_and_reports_the_blocked_admin(monkeypatch):
    """One blocked address must not take the good ones down with it — that is
    exactly what Zapmail's 400 did to a 13-mailbox batch."""
    sent = {}

    class R:
        status_code = 200
        text = "ok"

    monkeypatch.setattr(zr.requests, "put",
                        lambda url, headers=None, json=None, timeout=None:
                        (sent.update(json), R())[1])
    monkeypatch.setattr(zr, "_registry", lambda: {})
    monkeypatch.setattr(zr, "_save_registry", lambda e: None)
    # admin@solo.info is blocked (its sibling stays); ours@shared.info is fine.
    r = zr.cancel_mailboxes(["admin@solo.info", "ours@shared.info"], dry_run=False)
    assert r["ok"] is True
    assert sent["mailboxIds"] == ["m1"]           # the schedulable one only
    assert r["blocked_admin"] == ["admin@solo.info"]


def test_taking_every_mailbox_on_a_domain_includes_the_admin(monkeypatch):
    """Naming all of them is how an admin legitimately goes: nothing is left
    behind it."""
    r = zr.cancel_mailboxes(["client1@shared.info", "ours@shared.info",
                             "client2@shared.info"], dry_run=True)
    assert r["blocked_admin"] == []
    assert r["count"] == 3


def test_a_snapshot_avoids_re_walking_zapmail(monkeypatch):
    """Without it, each call re-walks ~900 domains to resolve one address —
    a 13-mailbox run took ten minutes."""
    monkeypatch.setattr(zr, "S", FakeSetup([]))     # walking would find nothing
    r = zr.cancel_mailboxes(["ours@shared.info"], dry_run=True, snapshot=DOMAINS)
    assert r["count"] == 1


def test_the_success_path_still_names_what_it_skipped(monkeypatch):
    """ok:true with no mention of the skipped admin is silent partial success."""
    class R:
        status_code = 200
        text = "ok"

    monkeypatch.setattr(zr.requests, "put", lambda *a, **k: R())
    monkeypatch.setattr(zr, "_registry", lambda: {})
    monkeypatch.setattr(zr, "_save_registry", lambda e: None)
    r = zr.cancel_mailboxes(["admin@solo.info", "ours@shared.info"], dry_run=False)
    assert r["ok"] is True
    assert r["scheduled"] == ["ours@shared.info"]
    assert r["blocked_admin"] == ["admin@solo.info"]


def test_the_admin_is_found_when_every_createdAt_is_identical():
    """A domain's mailboxes are provisioned in one batch and stamped with the
    same createdAt to the millisecond. min() then breaks the tie by list order,
    which is arbitrary — it picked `sean.reynolds` on lawnworkspecialists.info
    when the admin is `s.reynolds`, and the address sailed past the guard into
    a 400 that killed the whole batch."""
    same = "2026-04-16T01:35:33.585Z"
    mbs = [{"username": "sean.reynolds", "createdAt": same},
           {"username": "sean.r", "createdAt": same},
           {"username": "s.reynolds", "createdAt": same}]
    assert zr._admin_username(mbs) == "s.reynolds"


def test_a_genuinely_earlier_mailbox_still_wins():
    """Creation time is still the rule; the provisioning order only breaks ties."""
    mbs = [{"username": "s.reynolds", "createdAt": "2026-05-01T00:00:00Z"},
           {"username": "sean.r", "createdAt": "2026-04-01T00:00:00Z"}]
    assert zr._admin_username(mbs) == "sean.r"


def test_an_unknown_username_sorts_after_the_known_ones_on_a_tie():
    same = "2026-04-16T01:35:33.585Z"
    mbs = [{"username": "someoneelse", "createdAt": same},
           {"username": "s.reynolds", "createdAt": same}]
    assert zr._admin_username(mbs) == "s.reynolds"
