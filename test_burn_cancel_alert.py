"""The Slack line that says which burned domains to cancel."""
from datetime import date

import burn_cancel_alert as bca


def _dom(name, burned, total=3, client="Acme", scheduled=False, collateral=0):
    return {"domain": name, "total": total, "clients": [client], "scheduled": scheduled,
            "counts": {"burned": burned, "collateral": collateral}}


VIEW = {"domains": [_dom("a.info", 3), _dom("b.info", 2, collateral=1),
                    _dom("c.info", 1), _dom("d.info", 3, scheduled=True)]}


class _Store:
    def __init__(self): self.state = {}
    def cache_get(self, k): return ({"pipeline": {"newest_data_day": "2026-10-27"}}, None)
    def get_state(self, k): return self.state.get(k)
    def set_state(self, k, v): self.state[k] = v


def test_two_or_three_burned_is_cancel_one_is_swap_scheduled_is_skipped(monkeypatch):
    monkeypatch.setattr(bca, "store", _Store())
    b = bca.build(VIEW)
    assert [r["domain"] for r in b["cancel"]] == ["a.info", "b.info"]
    assert [r["domain"] for r in b["swap"]] == ["c.info"]
    text = bca.format_alert(b)
    assert "Cancel these 2 domains" in text and "1 healthy still sending" in text
    assert "d.info" not in text


def test_unchanged_list_posts_once_a_week(monkeypatch):
    import zapmail_removals as zr
    st, sent = _Store(), []
    monkeypatch.setattr(bca, "store", st)
    monkeypatch.setattr(zr, "post_slack", lambda m: sent.append(m) or "webhook")
    b = bca.build(VIEW)
    assert bca.post(b, dry_run=False, today=date(2026, 10, 28))["posted"]
    assert not bca.post(b, dry_run=False, today=date(2026, 10, 30))["posted"]
    assert bca.post(b, dry_run=False, today=date(2026, 11, 4))["posted"]
    b["cancel"].pop()
    assert bca.post(b, dry_run=False, today=date(2026, 11, 5))["posted"]
    assert len(sent) == 3


def test_nothing_burned_says_nothing():
    assert bca.format_alert({"cancel": [], "swap": []}) is None
