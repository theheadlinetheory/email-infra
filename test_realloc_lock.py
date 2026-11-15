"""Overlapping confirmed reallocations must not both run (2026-11-15 double-pick)."""
import time
import health_replace as hr


class _Conflict(Exception):
    def __init__(self):
        self.response = type("R", (), {"status_code": 409})()


def _fake_store(monkeypatch, rows):
    def req(method, path, params=None, json_body=None, headers=None):
        if method == "POST":
            if json_body["key"] in rows:
                raise _Conflict()
            rows[json_body["key"]] = json_body["data"]
        elif method == "DELETE":
            rows.pop(params["key"][3:], None)
        return []
    import json
    monkeypatch.setattr(hr.store, "_request", req)
    monkeypatch.setattr(hr.store, "get_state",
                        lambda k: json.loads(rows[k]) if k in rows else None)


def test_second_run_refused_while_first_holds_lock(monkeypatch):
    rows = {}
    _fake_store(monkeypatch, rows)
    seen = []

    def inner(emails, confirm):
        seen.append(emails)
        nested = hr.reallocate_emails(["b@y.co"], confirm=True)   # overlapping click
        assert nested.get("busy") and "another reallocation" in nested["error"]
        return {"ok": True}
    monkeypatch.setattr(hr, "_reallocate_emails", inner)
    assert hr.reallocate_emails(["a@x.co"], confirm=True) == {"ok": True}
    assert seen == [["a@x.co"]]
    assert hr.REALLOC_LOCK_KEY not in rows            # released


def test_lock_released_on_exception(monkeypatch):
    rows = {}
    _fake_store(monkeypatch, rows)
    monkeypatch.setattr(hr, "_reallocate_emails",
                        lambda e, confirm: (_ for _ in ()).throw(RuntimeError("boom")))
    try:
        hr.reallocate_emails(["a@x.co"], confirm=True)
    except RuntimeError:
        pass
    assert hr.REALLOC_LOCK_KEY not in rows


def test_stale_lock_is_taken_over(monkeypatch):
    import json
    rows = {hr.REALLOC_LOCK_KEY: json.dumps({"ts": time.time() - 1000, "what": "old"})}
    _fake_store(monkeypatch, rows)
    monkeypatch.setattr(hr, "_reallocate_emails", lambda e, confirm: {"ok": True})
    assert hr.reallocate_emails(["a@x.co"], confirm=True) == {"ok": True}


def test_dry_run_skips_lock(monkeypatch):
    rows = {hr.REALLOC_LOCK_KEY: json_now()}
    _fake_store(monkeypatch, rows)
    monkeypatch.setattr(hr, "_reallocate_emails", lambda e, confirm: {"dry_run": True})
    assert hr.reallocate_emails(["a@x.co"]) == {"dry_run": True}


def json_now():
    import json
    return json.dumps({"ts": time.time(), "what": "x"})
