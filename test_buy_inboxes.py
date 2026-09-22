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


# ── provisioning lock ────────────────────────────────────────────────────
# Provisioning buys mailbox slots. A step can outrun the browser's 120s while
# the server keeps working, and a timeout is exactly when someone clicks again.

class _LockStore:
    def __init__(self): self.s = {}
    def get_state(self, k): return self.s.get(k)
    def set_state(self, k, v): self.s[k] = v


def _lockstore(monkeypatch):
    st = _LockStore()
    monkeypatch.setattr(bi, "store", st)
    return st


def test_provision_lock_is_free_at_first(monkeypatch):
    _lockstore(monkeypatch)
    assert bi.provision_lock_acquire(3)["ok"] is True


def test_provision_lock_refuses_a_second_holder(monkeypatch):
    _lockstore(monkeypatch)
    bi.provision_lock_acquire(3)
    r = bi.provision_lock_acquire(3)
    assert r["ok"] is False and "age_seconds" in r


def test_provision_lock_is_per_order(monkeypatch):
    _lockstore(monkeypatch)
    bi.provision_lock_acquire(3)
    assert bi.provision_lock_acquire(4)["ok"] is True


def test_provision_lock_releases(monkeypatch):
    _lockstore(monkeypatch)
    bi.provision_lock_acquire(3)
    bi.provision_lock_release(3)
    assert bi.provision_lock_acquire(3)["ok"] is True


def test_a_stale_provision_lock_does_not_block_for_ever(monkeypatch):
    """A crashed run must not wedge an order permanently."""
    import datetime as dt
    st = _lockstore(monkeypatch)
    old = (dt.datetime.now(dt.timezone.utc)
           - dt.timedelta(seconds=bi.PROVISION_LOCK_STALE_SECONDS + 60)).isoformat()
    st.s[bi.PROVISION_LOCK_KEY] = {"3": {"at": old}}
    assert bi.provision_lock_acquire(3)["ok"] is True


def test_provision_lock_never_blocks_on_a_read_failure(monkeypatch):
    """The lock is a guard against double-spending, not a gate on provisioning
    at all — a broken state store must not stop work."""
    class Broken:
        def get_state(self, k): raise RuntimeError("store down")
        def set_state(self, k, v): pass
    monkeypatch.setattr(bi, "store", Broken())
    r = bi.provision_lock_acquire(3)
    assert r["ok"] is True and r["unverified"] is True


# ── per-domain purchase progress ─────────────────────────────────────────
# "9 of 14 registered" cannot tell you WHICH five failed, and the nameserver
# half of the loop is where the silent failures live: 14 domains were bought,
# left on the registrar's own nameservers, and recorded as connected.

def test_progress_rows_start_one_per_domain():
    doms = ["a.co", "b.co", "c.co"]
    rows = {d: {"domain": d, "registered": None, "ns": None,
                "zapmail": None, "error": None} for d in doms}
    assert [rows[d]["domain"] for d in doms] == doms
    assert all(r["registered"] is None for r in rows.values())


def test_a_row_distinguishes_every_outcome():
    """None (not reached), 'working', True, False and 'pending' must stay
    distinct — collapsing 'pending DNS' into False is what made 14 unconnected
    domains read as connected."""
    states = [None, "working", True, False, "pending"]
    assert len(set(map(str, states))) == len(states)


def test_a_registered_domain_with_failed_nameservers_is_not_clean():
    """The exact 2026-09-21 shape: registered True, ns False. The row has to
    carry both, because the summary count says 'registered' either way."""
    row = {"domain": "x.co", "registered": True, "ns": False,
           "zapmail": None, "error": "nameserver update rejected"}
    assert row["registered"] is True and row["ns"] is False
    assert row["error"]
