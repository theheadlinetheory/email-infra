"""The 2026-10-27 burn rule, and the guarantee that a Smartlead outage can no
longer freeze the burned list.

Rule (Tim): over an inbox's latest 200 sends, burned if replies are below 0.5%
in WHOLE replies (0 of 200, <=1 of 400) or bounces are >= 3%.
"""
import health_model as hm
import health_snapshot as hs


# The 10-27 EITHER rule, pinned here so its maths stays tested now that the
# default is back to the old BOTH rule (see the tests at the end).
OR_CFG = {"burn_combine": "or", "burn_window_days": None, "burn_min_sends": 200}


def _rows(*days):
    """days: (date, sent, replied, bounced) newest-or-oldest, any order."""
    return [{"date": d, "sent": s, "replied": r, "bounced": b, "positive_replied": 0}
            for d, s, r, b in days]


def _score(rows):
    win = hm.latest_sends_window(rows, 200)
    return hm.score_inbox({"win": win, "in_campaign": True, "in_warmup": False,
                           "smtp_ok": True}, OR_CFG), win


def _days(n, sent, replied=0, bounced=0, start=1):
    return _rows(*[(f"2026-10-{start + i:02d}", sent, replied if i == 0 else 0,
                    bounced if i == 0 else 0) for i in range(n)])


def test_zero_replies_in_200_sends_is_burned():
    res, win = _score(_days(14, 15))
    assert win["sends"] == 210 and res["status"] == hm.BURNED


def test_one_reply_in_210_sends_is_not_burned():
    """0.48% is below 0.5%, but one reply per 200 is the standard: counted in
    whole replies, 210 sends need exactly one."""
    res, _ = _score(_days(14, 15, replied=1, start=10))   # the one reply falls inside the window
    assert res["status"] != hm.BURNED


def test_the_walk_stops_once_it_has_200_sends():
    """Old replies outside the latest 200 sends do not rescue an inbox."""
    rows = _days(14, 15, start=10) + _rows(("2026-09-01", 15, 5, 0))
    res, win = _score(rows)
    assert win["from"] == "2026-10-10" and res["status"] == hm.BURNED


def test_bounce_at_3_percent_burns_even_with_replies():
    rows = _rows(*[(f"2026-10-{d:02d}", 20, 1 if d == 1 else 0, 6 if d == 1 else 0)
                   for d in range(1, 11)])
    res, win = _score(rows)
    assert win["sends"] == 200 and res["status"] == hm.BURNED
    assert any("bounce" in r for r in res["reasons"])


def test_under_200_sends_is_low_volume_and_says_how_many():
    res, _ = _score(_days(13, 15))
    assert res["status"] == hm.INSUFFICIENT
    assert "195 sends" in res["reasons"][0]


def test_a_low_sender_is_judged_once_it_reaches_200_however_long_it_takes():
    """738 of 1,200 inboxes sent under 10.5/day and could never reach the old
    105-in-10-days gate. Five a day reaches 200 in 40 days."""
    rows = _rows(*[(f"2026-{9 + (d > 30):02d}-{(d - 1) % 30 + 1:02d}", 5, 0, 0)
                   for d in range(1, 41)])
    res, win = _score(rows)
    assert win["sends"] == 200 and res["status"] == hm.BURNED


def test_a_day_without_counts_is_a_hole_not_a_zero():
    rows = _days(5, 15, start=20) + [{"date": "2026-10-19", "sent": 15,
                                      "replied": None, "bounced": None}]
    res, win = _score(rows)
    assert win["complete"] is False and res["status"] == hm.INSUFFICIENT
    assert "backfilled" in res["reasons"][0]


def test_replies_needed_is_whole_replies():
    assert [hm.replies_needed(n) for n in (199, 200, 399, 400, 600)] == [0, 1, 1, 2, 3]


# ── the outage guarantee ────────────────────────────────────────────────────

class _Store:
    _CACHE_WRITE_ENABLED = True

    def __init__(self, history, cfg=None):
        self.history, self.cache, self.status, self.state = history, {}, None, {}
        self.cfg = OR_CFG if cfg is None else cfg

    def get_health_config(self, k): return self.cfg
    def has_daily_counts(self): return True
    def get_health_daily_counts(self, since, before_date=None):
        return {e: [r for r in rs if r["date"] < before_date] for e, rs in self.history.items()}
    def get_health_status_all(self): return []
    def get_state(self, k): return self.state.get(k)
    def set_state(self, k, v): self.state[k] = v
    def upsert_health_status(self, rows): self.status = rows
    def cache_set(self, k, v): self.cache[k] = v


OVERVIEW = {"clients": [{"name": "Acme", "group_a": {"account_details": [
    {"email": "a@x.co", "domain": "x.co", "in_campaign": True, "smtp_ok": True},
    {"email": "b@x.co", "domain": "x.co", "in_campaign": True, "smtp_ok": True}]}}]}


def test_smartlead_down_still_rescores_from_stored_history(monkeypatch):
    """2026-10-24..26: a blank Smartlead session failed six runs in a row and,
    because scoring needed a fresh windowed call, the list froze for 45 hours.
    Now the fetch failing is reported, and the fleet is scored anyway."""
    import health_daily as hd
    hist = {"a@x.co": _days(14, 15, start=1),                       # 0 replies
            "b@x.co": _days(14, 15, replied=2, start=1)}
    st = _Store(hist)
    monkeypatch.setattr(hs, "store", st)

    def boom(*a, **k):
        raise RuntimeError("metrics fetch failed: BLANK SESSION")
    monkeypatch.setattr(hd, "refresh_history", boom)

    out = hs.snapshot_daily(overview=OVERVIEW, today="2026-10-27")
    assert out["scored"] is True and "BLANK SESSION" in out["error"]
    by = {r["email"]: r for r in st.status}
    assert by["a@x.co"]["status"] == hm.BURNED and by["a@x.co"]["win_sends"] == 210
    assert by["b@x.co"]["status"] != hm.BURNED
    fleet = st.cache["health_fleet"]
    assert fleet["pipeline"]["newest_data_day"] == "2026-10-14"
    assert "BLANK SESSION" in fleet["pipeline"]["fetch_error"]


def test_todays_partial_day_is_never_in_the_window(monkeypatch):
    """Today's replies have not arrived yet; counting its sends would push an
    inbox towards 'no replies' every morning."""
    import health_daily as hd
    hist = {"a@x.co": _days(13, 15, replied=1, start=1)
            + _rows(("2026-10-27", 15, 0, 0))}
    st = _Store(hist)
    monkeypatch.setattr(hs, "store", st)
    monkeypatch.setattr(hd, "refresh_history",
                        lambda attrs: {"sent_by_date": {}, "no_data_days": [], "rows": 0})
    hs.snapshot_daily(overview=OVERVIEW, today="2026-10-27")
    a = {r["email"]: r for r in st.status}["a@x.co"]
    assert a["win_sends"] == 195 and a["status"] == hm.INSUFFICIENT


# ── 2026-10-29: back to the old dashboard's rule, from stored counts ────────

def _old(rows):
    win = hm.latest_sends_window(rows, 105, 10)
    return hm.score_inbox({"win": win, "in_campaign": True, "in_warmup": False,
                           "smtp_ok": True}), win


def test_old_rule_needs_bounce_and_no_replies_together():
    both = _rows(*[(f"2026-10-{d:02d}", 15, 0, 1) for d in range(1, 11)])     # 0%, 6.7%
    assert _old(both)[0]["status"] == hm.BURNED


def test_old_rule_bounce_alone_is_at_risk_not_burned():
    """130 of the 197 the EITHER rule burned were bounce-only at 3-5%."""
    bounce_only = _rows(*[(f"2026-10-{d:02d}", 15, 1, 1) for d in range(1, 11)])
    assert _old(bounce_only)[0]["status"] == hm.AT_RISK


def test_old_rule_no_replies_alone_is_at_risk_not_burned():
    reply_only = _rows(*[(f"2026-10-{d:02d}", 15, 0, 0) for d in range(1, 11)])
    assert _old(reply_only)[0]["status"] == hm.AT_RISK


def test_old_rule_window_is_ten_sending_days():
    rows = _rows(*[(f"2026-10-{d:02d}", 15, 0, 1) for d in range(1, 15)])
    res, win = _old(rows)
    assert win["days"] == 10 and win["sends"] == 150 and win["from"] == "2026-10-05"


def test_under_150_sends_is_low_volume():
    rows = _rows(*[(f"2026-10-{d:02d}", 10, 0, 1) for d in range(1, 11)])
    assert _old(rows)[0]["status"] == hm.INSUFFICIENT


def test_old_rule_still_scores_when_smartlead_is_down(monkeypatch):
    import health_daily as hd
    hist = {"a@x.co": _rows(*[(f"2026-10-{d:02d}", 15, 0, 1) for d in range(1, 11)]),
            "b@x.co": _rows(*[(f"2026-10-{d:02d}", 15, 0, 0) for d in range(1, 11)])}
    st = _Store(hist, cfg={})
    monkeypatch.setattr(hs, "store", st)

    def boom(*a, **k):
        raise RuntimeError("metrics fetch failed: BLANK SESSION")
    monkeypatch.setattr(hd, "refresh_history", boom)
    out = hs.snapshot_daily(overview=OVERVIEW, today="2026-10-27")
    assert out["scored"] is True
    by = {r["email"]: r["status"] for r in st.status}
    assert by == {"a@x.co": hm.BURNED, "b@x.co": hm.AT_RISK}


def test_default_window_is_the_latest_150_sends_not_10_days():
    """A follow-up-only inbox sending 2 a day: 10 sending days is 20 emails."""
    rows = _rows(*[(f"2026-{9 + (d > 30):02d}-{(d - 1) % 30 + 1:02d}", 2, 0, 0)
                   for d in range(1, 41)])
    win = hm.latest_sends_window(rows, hm.DEFAULT_CONFIG["burn_min_sends"],
                                 hm.DEFAULT_CONFIG["burn_window_days"])
    assert win["sends"] == 80
    res = hm.score_inbox({"win": win, "in_campaign": True, "in_warmup": False, "smtp_ok": True})
    assert res["status"] == hm.INSUFFICIENT and "need 150" in res["reasons"][0]
