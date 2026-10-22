"""The expiry board's job is to say which domains can safely lapse.

Every test here is about the destructive direction: a domain that still carries
live senders must never be reported as empty and therefore disposable.
"""
import pytest

import domain_expiry_alert as dea

FLEET = 1200          # a plausible full roster for these fakes


def roster(n, domain="live.info", start=0):
    """Distinct addresses per page — the walk keys a dict by email, so repeating
    the same 100 across every page collapses to 100 and the test measures
    nothing."""
    return [{"from_email": f"a{i}@{domain}", "daily_sent_count": 5}
            for i in range(start, start + n)]


def full_pages(pages=12, per=100):
    return [roster(per, start=i * per) for i in range(pages)] + [[]]


def make_getter(pages, campaigns=()):
    """pages: list of page results; None means that page failed."""
    state = {"i": 0}

    def getter(url, params):
        if "email-accounts" in url and "campaigns" not in url:
            i = state["i"]
            state["i"] += 1
            return pages[i] if i < len(pages) else []
        if url.endswith("/campaigns"):
            return list(campaigns)
        return []
    return getter


def test_a_complete_walk_counts_every_domain():
    out = dea.fetch_live_domains(getter=make_getter(full_pages()))
    assert out["live.info"]["inboxes"] == 1200


def test_a_failed_page_raises_instead_of_reporting_domains_as_empty():
    """`if not batch: break` treated a failed page as the end of the list. On
    2026-09-19 that produced "286 empty auto-renewing domains, $6,797/yr"; a
    complete walk on the same data says 0. Wrong in the destructive direction."""
    pages = [roster(100, start=0), None]
    with pytest.raises(RuntimeError, match="part way through"):
        dea.fetch_live_domains(getter=make_getter(pages))


def test_an_implausibly_small_roster_raises_even_when_every_page_returned_200():
    """The same failure wearing a different hat: short pages, no errors."""
    pages = [roster(100), []]
    with pytest.raises(RuntimeError, match="implausibly few"):
        dea.fetch_live_domains(getter=make_getter(pages))


def test_an_unreadable_campaign_list_raises():
    """Without campaigns every domain reads 'no active senders', which is the
    input to 'safe to let lapse'."""
    inner = make_getter(full_pages())          # ONE getter: it carries the page cursor,
                                        # and rebuilding it per call never ends
    def getter(url, params):
        if url.endswith("/campaigns"):
            return None
        return inner(url, params)

    with pytest.raises(RuntimeError, match="campaign list"):
        dea.fetch_live_domains(getter=getter)


def test_the_plausibility_floor_sits_between_one_page_and_the_real_fleet():
    assert 100 < dea.MIN_PLAUSIBLE_ACCOUNTS < 1700


# ── repeat suppression ────────────────────────────────────────────────────────

WASTING = {"at_risk": [], "wasting": [{"domain": "x.co"}],
           "summary": {"empty_renewing": 1, "empty_renewing_cost": 31.2}}


class FakeSlack:
    def __init__(self, status=200):
        self.sent, self.status = [], status

    def post(self, url, json=None, timeout=None):
        self.sent.append(json["text"])
        return type("R", (), {"status_code": self.status})()


@pytest.fixture
def slack(monkeypatch):
    import requests
    store = {}
    fake = FakeSlack()
    monkeypatch.setenv("SLACK_INFRA_DECISIONS_WEBHOOK", "https://hooks.example/x")
    monkeypatch.setattr(requests, "post", fake.post)
    monkeypatch.setattr(dea, "_last_post", lambda: dict(store))
    monkeypatch.setattr(dea, "_record_post", lambda p: store.update(p))
    fake.store = store
    return fake


def test_a_retried_snapshot_posts_the_same_report_once(slack):
    """zapmail-watch retries the snapshot three times; each used to post."""
    for _ in range(3):
        dea.post(WASTING, dry_run=False)
    assert len(slack.sent) == 1


def test_an_unchanged_empty_domain_report_resurfaces_after_a_week(slack):
    from datetime import date, timedelta
    dea.post(WASTING, dry_run=False)
    slack.store["posted_on"] = (date.today() - timedelta(days=7)).isoformat()
    dea.post(WASTING, dry_run=False)
    assert len(slack.sent) == 2


def test_a_changed_report_always_posts(slack):
    dea.post(WASTING, dry_run=False)
    more = {**WASTING, "summary": {"empty_renewing": 2, "empty_renewing_cost": 62.4}}
    dea.post(more, dry_run=False)
    assert len(slack.sent) == 2


def test_a_failed_send_does_not_buy_silence(slack):
    slack.status = 500
    dea.post(WASTING, dry_run=False)
    slack.status = 200
    dea.post(WASTING, dry_run=False)
    assert len(slack.sent) == 2 and slack.store.get("fingerprint")
