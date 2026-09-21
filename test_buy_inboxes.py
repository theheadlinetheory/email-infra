"""The two rules that must hold before any money moves."""
import buy_inboxes as bi


def test_outlook_is_refused_at_the_plan_stage():
    for p in ("outlook", "Outlook", "microsoft", "MS"):
        r = bi.plan({"provider": p, "domains": ["x.co"]})
        assert r.get("error") and "Google only" in r["error"], p
        assert r["ready_to_buy"] is False


def test_provider_map_no_longer_offers_microsoft():
    assert "outlook" not in bi.PROVIDERS
    assert set(bi.PROVIDERS.values()) == {"GOOGLE"}


def test_more_than_three_per_domain_is_refused():
    r = bi.plan({"provider": "google", "inboxes_per_domain": 4, "domains": ["x.co"]})
    assert r.get("error") and "cap is 3" in r["error"]
    assert r["ready_to_buy"] is False


def test_buying_cannot_route_around_the_plan_refusal():
    """buy_domains calls plan first; a refusal has to stop it before Spaceship."""
    r = bi.buy_domains({"provider": "outlook", "domains": ["x.co"]}, confirm=True)
    assert r.get("error") and "Google only" in r["error"]


def test_acquisition_names_must_carry_the_whole_brand():
    """"headline", "theorytoday", "headlineconnect" are not The Headline Theory."""
    roots = bi._brand_roots("The Headline Theory")
    assert "headline" in roots and "theory" in roots      # the loose list, by design
    out = bi.suggest_client_domains("The Headline Theory", count=0, max_checks=0,
                                    whole_brand_only=True)
    for r in out["roots"]:
        assert "headline" in r and "theory" in r, r


def test_whole_brand_never_falls_back_to_half_brand_names():
    out = bi.suggest_client_domains("Quantum Heating", count=0, max_checks=0,
                                    whole_brand_only=True)
    assert out["roots"] == ["quantumheating"]


def test_client_suggestions_keep_the_loose_roots():
    """A client brand still wants short variants — that is why they exist."""
    out = bi.suggest_client_domains("The Headline Theory", count=0, max_checks=0)
    assert any("theory" in r and "headline" not in r for r in out["roots"])


def test_a_purchase_refuses_to_start_while_one_is_running(monkeypatch):
    """Two overlapping runs register the same shortlist twice, and domains are
    not refundable."""
    monkeypatch.setattr(bi, "buy_progress", lambda: {"running": True, "done": 3, "total": 14})
    r = bi.buy_domains({"provider": "google", "domains": ["x.info"]}, confirm=True)
    assert "already running" in r["error"]
    assert r["in_flight"]["done"] == 3


def test_a_stale_lock_does_not_block_buying_for_ever(monkeypatch):
    """A crashed run must not leave the button dead."""
    import datetime as dt
    old = (dt.datetime.now(dt.timezone.utc)
           - dt.timedelta(seconds=bi.BUY_LOCK_STALE_SECONDS + 60)).isoformat()
    monkeypatch.setattr(bi.store, "get_state",
                        lambda k: {"running": True, "at": old} if k == bi.BUY_PROGRESS_KEY else None)
    p = bi.buy_progress()
    assert p["running"] is False and p["stale"] is True


def test_a_fresh_lock_still_blocks(monkeypatch):
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    monkeypatch.setattr(bi.store, "get_state",
                        lambda k: {"running": True, "at": now} if k == bi.BUY_PROGRESS_KEY else None)
    assert bi.buy_progress()["running"] is True
