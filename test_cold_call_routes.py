"""The cold-call routes as the CRM calls them: a Bearer session, cross-origin."""
import importlib
import sys

import pytest

pytest.importorskip("flask")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw-test")
    sys.modules.pop("api.index", None)
    api = importlib.import_module("api.index")
    return api.app.test_client()


def test_a_bearer_token_is_checked_not_crashed_on(client, monkeypatch):
    # server_auth.py does not exist; that once turned every Bearer call into a 500.
    import crm_auth
    monkeypatch.setattr(crm_auth, "verify_crm_user",
                        lambda h: {"ok": False, "status": 401, "error": "Session invalid"})
    r = client.post("/api/cold-call-inbox", json={}, headers={"Authorization": "Bearer junk"})
    assert r.status_code == 401
    assert r.get_json()["error"] == "Session invalid"


def test_a_staff_session_reaches_the_purchase(client, monkeypatch):
    import crm_auth
    import cold_call_inbox as cci
    monkeypatch.setattr(crm_auth, "verify_crm_user", lambda h: {"ok": True, "email": "a@theheadlinetheory.com"})
    monkeypatch.setattr(cci, "start", lambda body: {"ok": True, "email": "contact@x.info", "seen": body})
    r = client.post("/api/cold-call-inbox", json={"crm_client_id": "c1"},
                    headers={"Authorization": "Bearer good"})
    assert r.status_code == 200 and r.get_json()["seen"] == {"crm_client_id": "c1"}


def test_the_advance_route_needs_the_dashboard_password(client, monkeypatch):
    import cold_call_inbox as cci
    monkeypatch.setattr(cci, "advance_all", lambda: {"ok": True, "pending": 0})
    assert client.post("/api/cold-call-inbox/advance").status_code == 401
    assert client.post("/api/cold-call-inbox/advance?pw=pw-test").get_json()["pending"] == 0
