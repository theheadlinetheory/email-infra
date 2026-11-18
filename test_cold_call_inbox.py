"""The cold-calling contact@ inbox: one domain, one mailbox, its own tag, and a
Retry that never buys twice."""
import buy_provision as bp
import cold_call_inbox as cci
import crm_auth
from test_buy_provision import FakeIO


class FakeOrders:
    def __init__(self, monkeypatch):
        self.orders = []
        monkeypatch.setattr(cci.bi, "_orders", lambda: self.orders)
        monkeypatch.setattr(cci.bi, "_save_orders", lambda o: None)
        monkeypatch.setattr(cci.bi, "_get_order",
                            lambda oid: next((o for o in self.orders if o["id"] == oid), None))
        monkeypatch.setattr(cci.bi, "provision_lock_acquire", lambda oid: {"ok": True})
        monkeypatch.setattr(cci.bi, "provision_lock_release", lambda oid: None)
        self.buys = []

    def buy(self, spec, confirm=False):
        assert confirm
        self.buys.append(spec)
        order = {"id": len(self.orders) + 1, "status": "domains_registered",
                 "provider": "GOOGLE", **spec}
        self.orders.append(order)
        return {"ok": True, "order_id": order["id"], "registered": spec["domains"],
                "status": "domains_registered"}


BODY = {"crm_client_id": "c-1", "client_name": "Woody's Landcare LLC", "website": "woodyslandcare.com"}


class ColdIO(FakeIO):
    """FakeIO plus what a cold-calling order also touches."""

    def __init__(self, *a, logo_url="https://x/client_logos/woodyslandcare.png", **k):
        super().__init__(*a, **k)
        self.logo_url = logo_url
        self.zapmail_tags = {}
        self.photos = {}
        self.client_assigned = None

    def smartlead_tag(self, ids, tag, client_name=None):
        self.client_assigned = client_name
        return super().smartlead_tag(ids, tag)

    def zapmail_tag_domains(self, ids, name):
        for i in ids:
            self.zapmail_tags[i] = name
        return {"ok": True, "tag": name}

    def client_logo_url(self, logo):
        return {"url": self.logo_url} if self.logo_url else {"error": "no logo found"}

    def set_profile_picture(self, ids, url):
        for i in ids:
            self.photos[i] = url
        return {"ok": True}


def test_the_whole_brand_on_info_is_tried_first():
    assert cci.candidate_domains("https://www.lightdmv.com/")[0] == "lightdmv.info"
    assert cci.candidate_domains("Woody's Landcare LLC")[0] == "woodyslandcare.info"


def test_a_taken_clean_name_falls_through_to_the_next():
    taken = {"lightdmv.info"}
    d = cci.pick_domain("lightdmv.com", check=lambda d: d not in taken)
    assert d and d != "lightdmv.info" and d.endswith(".info")


def test_start_buys_one_domain_for_one_contact_mailbox_named_for_the_company(monkeypatch):
    f = FakeOrders(monkeypatch)
    res = cci.start(BODY, buy=f.buy, pick=lambda b: "woodyslandcare.info")
    assert res["ok"] and res["email"] == "contact@woodyslandcare.info"
    spec = f.buys[0]
    assert spec["inboxes_per_domain"] == 1
    # As the hand-built contact@woodyslandcare.info: company name, never Sean.
    assert spec["mailbox_specs"] == [{"firstName": "Woody's", "lastName": "Landcare",
                                      "mailboxUsername": "contact"}]
    assert spec["forward_to"] == "https://woodyslandcare.com"


def test_tags_copy_the_existing_contact_inboxes(monkeypatch):
    f = FakeOrders(monkeypatch)
    cci.start(BODY, buy=f.buy, pick=lambda b: "woodyslandcare.info")
    spec = f.buys[0]
    assert spec["tags"] == ["Woody's Landcare LLC", "Zapmail", cci.date_tag()]
    assert not any("group" in t.lower() for t in spec["tags"])   # never joins a campaign
    assert spec["sl_client_name"] == "Woody's Landcare LLC"
    assert spec["zapmail_tag"] == "Woody's Landcare"
    assert spec["logo"]["slug"] == "woodyslandcare"


def test_company_names_split_like_the_hand_built_inboxes():
    assert cci.sender_name("Mighty Oak Landscaping") == {"firstName": "Mighty Oak", "lastName": "Landscaping"}
    assert cci.sender_name("Gm Landscaping & Design") == {"firstName": "GM", "lastName": "Landscaping & Design"}
    assert cci.sender_name("Clear Heating & Air, Inc.") == {"firstName": "Clear", "lastName": "Heating & Air"}
    assert cci.sender_name("Kinsley Landscape Ltd.") == {"firstName": "Kinsley", "lastName": "Landscape"}
    assert cci.sender_name("Timesavers", "timesaverslandscaping.com") == {"firstName": "Timesavers", "lastName": "Landscaping"}


def test_the_date_tag_is_setup_pys_format():
    import datetime
    assert cci.date_tag(datetime.date(2026, 10, 27)) == "10/27/26"
    assert cci.date_tag(datetime.date(2026, 11, 3)) == "11/3/26"


def test_a_retry_never_buys_a_second_domain(monkeypatch):
    f = FakeOrders(monkeypatch)
    cci.start(BODY, buy=f.buy, pick=lambda b: "woodyslandcare.info")
    again = cci.start(BODY, buy=f.buy, pick=lambda b: "other.info")
    assert len(f.buys) == 1
    assert again["existed"] and again["email"] == "contact@woodyslandcare.info"


def test_no_website_is_refused_before_any_spend(monkeypatch):
    f = FakeOrders(monkeypatch)
    res = cci.start({**BODY, "website": ""}, buy=f.buy, pick=lambda b: "x.info")
    assert "website" in res["error"] and not f.buys


def test_the_order_provisions_exactly_one_contact_mailbox_end_to_end(monkeypatch):
    f = FakeOrders(monkeypatch)
    monkeypatch.setattr(cci.S.Spaceship, "set_nameservers", lambda d: {"success": True},
                        raising=False)
    cci.start(BODY, buy=f.buy, pick=lambda b: "woodyslandcare.info")
    io = ColdIO(["woodyslandcare.info"])
    out = cci.advance_all(io=io)
    assert out["advanced"][0]["status"] == "live"
    assert io.mailboxes["woodyslandcare.info"] == [{"id": "mb-contact@woodyslandcare.info",
                                                    "email": "contact@woodyslandcare.info"}]
    assert io.forwarded and io.tagged
    assert io.warmed == []                         # contact@ never warms
    assert io.tag_name == ["Woody's Landcare LLC", "Zapmail", cci.date_tag()]
    assert io.client_assigned == "Woody's Landcare LLC"
    assert list(io.zapmail_tags.values()) == ["Woody's Landcare"]
    assert io.photos == {"mb-contact@woodyslandcare.info": "https://x/client_logos/woodyslandcare.png"}
    # Finished orders are left alone.
    assert cci.advance_all(io=io)["pending"] == 0


def test_a_missing_logo_never_holds_the_inbox_back_from_smartlead(monkeypatch):
    f = FakeOrders(monkeypatch)
    cci.start(BODY, buy=f.buy, pick=lambda b: "woodyslandcare.info")
    io = ColdIO(["woodyslandcare.info"], logo_url=None)
    out = cci.advance_all(io=io)["advanced"][0]
    assert io.tagged and io.warmed == []           # in SmartLead, never warming
    assert out["step"] == "profile" and "logo" in out["blocked_reason"]
    io.logo_url = "https://x/client_logos/woodyslandcare.png"   # dropped in by hand
    assert cci.advance_all(io=io)["advanced"][0]["status"] == "live"


def test_a_nameserver_update_that_failed_at_purchase_is_retried(monkeypatch):
    f = FakeOrders(monkeypatch)
    cci.start(BODY, buy=f.buy, pick=lambda b: "woodyslandcare.info")
    f.orders[0]["nameserver_failed"] = [{"domain": "woodyslandcare.info", "error": "not in account"}]
    calls = []
    monkeypatch.setattr(cci.S.Spaceship, "set_nameservers",
                        lambda d: calls.append(d) or {"success": True}, raising=False)
    cci.advance_all(io=ColdIO(["woodyslandcare.info"], dns_ready=False))
    assert calls == ["woodyslandcare.info"]
    assert f.orders[0]["nameserver_failed"] == []
    assert "woodyslandcare.info" in f.orders[0]["connect_pending"]


def test_client_names_match_across_smartlead_and_zapmail_spellings():
    rows = [{"id": 1, "name": "Clear Heating & Air, "}, {"id": 2, "name": "Clearwater Pools"},
            {"id": 3, "name": "Woody's Landcare LLC"}]
    assert bp.match_client("Clear Heating & Air, Inc.", rows) == 1
    assert bp.match_client("Woody's Land Care", rows) == 3
    assert bp.match_client("Brand New Client", rows) is None    # never guessed, never created


def test_logo_candidates_prefer_the_clients_own_over_customer_logos():
    import client_logo as cl
    html = """<header><img src="/a/google-logo.png" alt="" srcset="https://acme.com/x.png 2x"></header>
    <img src="/clients/logo-aw.jpg" alt="A&W">
    <img src="/up/AcmeHeating-Air41.webp" alt="">
    <link rel="apple-touch-icon" href="/acme-icon.png">"""
    got = cl.logo_candidates(html, "https://acme.com/", ["acmeheating", "acme"])
    assert got[0] == "https://acme.com/up/AcmeHeating-Air41.webp"
    assert got.index("https://acme.com/clients/logo-aw.jpg") == len(got) - 1


def test_ordinary_orders_keep_the_persona_usernames():
    assert [s["mailboxUsername"] for s in bp.specs_for({}, "x.co", 3)] == \
        ["s.reynolds", "sean.r", "sean.reynolds"]


class _Resp:
    def __init__(self, code, body):
        self.status_code, self._b = code, body

    def json(self):
        return self._b


def test_only_internal_crm_sessions_are_accepted():
    ok = crm_auth.verify_crm_user("Bearer t", get=lambda *a, **k: _Resp(200, {"email": "Aidan@TheHeadlineTheory.com"}))
    assert ok == {"ok": True, "email": "aidan@theheadlinetheory.com"}
    assert crm_auth.verify_crm_user("Bearer t", get=lambda *a, **k: _Resp(200, {"email": "x@gmail.com"}))["status"] == 403
    assert crm_auth.verify_crm_user("Bearer t", get=lambda *a, **k: _Resp(401, {}))["status"] == 401
    assert crm_auth.verify_crm_user(None)["status"] == 401

    def boom(*a, **k):
        raise OSError("down")
    assert crm_auth.verify_crm_user("Bearer t", get=boom)["status"] == 503


# ── warm-up stays off on contact@ (Tim, 2026-10-31) ───────────────────────

def _accts(*rows):
    return [{"id": i, "from_email": e, "warmup_details": {"status": st}}
            for i, (e, st) in enumerate(rows, 1)]


def test_enforcer_turns_off_only_warming_contact_inboxes():
    saved, off = {}, []
    out = cci.enforce_warmup_off(
        list_accounts=lambda: _accts(("contact@a.info", "ACTIVE"), ("contact@b.info", "INACTIVE"),
                                     ("s.reynolds@a.info", "ACTIVE")),
        disable=lambda aid: off.append(aid) or True,
        state=(lambda k: None, lambda k, v: saved.update({k: v})), now=10_000)
    assert out["turned_off"] == ["contact@a.info"] and off == [1]
    assert saved[cci.WARMUP_OFF_KEY]["ran_at"] == 10_000


def test_enforcer_runs_at_most_hourly():
    state = ({cci.WARMUP_OFF_KEY: {"ran_at": 10_000}}.get, lambda k, v: None)
    called = []
    out = cci.enforce_warmup_off(list_accounts=lambda: called.append(1) or [],
                                 state=state, now=10_000 + 600)
    assert out["ran"] is False and not called


def test_enforcer_unreadable_roster_is_not_all_clear():
    saved = {}
    out = cci.enforce_warmup_off(list_accounts=lambda: None,
                                 state=(lambda k: None, lambda k, v: saved.update({k: v})), now=10_000)
    assert out["ran"] is False and "unreadable" in out["error"] and not saved
