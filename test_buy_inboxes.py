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
