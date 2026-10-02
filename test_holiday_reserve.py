"""Holiday clients must be able to draw from the landscaping reserve.

Two faults made the Inboxes tab report "reserve 0" for Mary & Bright while 42
warmed inboxes sat available, so nobody replaced their burned ones:

  - the reserve was read from overview_v2, which held ONE group of 3 with
    warmup_days None, so the warmed check skipped it entirely;
  - holiday clients were classified from their DOMAINS, landing "Wonderly
    Lights Of Birmingham" on hvac — which is barred from landscaping reserve.
"""
import health_replace as hr


# ── who counts as holiday ────────────────────────────────────────────────

def test_lights_clients_are_holiday_whatever_their_domains_look_like():
    for name in ("Merry & Bright Christmas Lights",
                 "Wonderly Lights Of Birmingham",
                 "Iceberg Lighting",
                 "Festive Glow Installs"):
        assert hr._client_niche(name) == "holiday", name


def test_trade_clients_are_unaffected():
    assert hr._client_niche("Mighty Oak Landscaping") == "landscaping"
    assert hr._client_niche("Denair Hvac, Inc.") == "hvac"
    assert hr._client_niche("Quantum Heating & Air Conditioning Inc.") == "hvac"


# ── which reserve a niche may draw ───────────────────────────────────────

def test_holiday_falls_back_to_landscaping_then_generic():
    assert hr.acceptable_niches("holiday") == ("holiday", "landscaping", "generic")


def test_hvac_and_landscaping_still_never_cross():
    """The rule that predates this and must survive it."""
    assert "landscaping" not in hr.acceptable_niches("hvac")
    assert "hvac" not in hr.acceptable_niches("landscaping")


def test_each_trade_may_still_take_generic():
    assert "generic" in hr.acceptable_niches("hvac")
    assert "generic" in hr.acceptable_niches("landscaping")


# ── the picker ───────────────────────────────────────────────────────────

def _pool(monkeypatch, rows):
    monkeypatch.setattr(hr, "live_reserve", lambda force=False: rows)


def _inbox(email, niche, ready=True, smtp=True):
    return {"email": email, "account_id": hash(email) % 10000, "group": "Reserve",
            "niche": niche, "age_days": 30 if ready else 2, "ready": ready,
            "smtp_ok": smtp}


def test_a_holiday_client_gets_a_landscaping_inbox(monkeypatch):
    _pool(monkeypatch, [_inbox("a@lawncrew.info", "landscaping")])
    got = hr.pick_reserve_inbox(want_niche="holiday")
    assert got and got["niche"] == "landscaping"


def test_an_hvac_client_is_still_refused_a_landscaping_inbox(monkeypatch):
    _pool(monkeypatch, [_inbox("a@lawncrew.info", "landscaping")])
    assert hr.pick_reserve_inbox(want_niche="hvac") is None


def test_an_unwarmed_inbox_is_never_handed_over(monkeypatch):
    _pool(monkeypatch, [_inbox("a@lawncrew.info", "landscaping", ready=False)])
    assert hr.pick_reserve_inbox(want_niche="holiday") is None


def test_a_dead_sender_is_never_handed_over(monkeypatch):
    """Replacing a burned inbox with one that cannot send fixes nothing."""
    _pool(monkeypatch, [_inbox("a@lawncrew.info", "landscaping", smtp=False)])
    assert hr.pick_reserve_inbox(want_niche="holiday") is None


def test_an_already_claimed_inbox_is_skipped(monkeypatch):
    _pool(monkeypatch, [_inbox("taken@lawncrew.info", "landscaping"),
                        _inbox("free@lawncrew.info", "landscaping")])
    got = hr.pick_reserve_inbox(exclude={"taken@lawncrew.info"}, want_niche="holiday")
    assert got["email"] == "free@lawncrew.info"
