"""Each subscription and mailbox is counted once, under its own provider.

On 2026-10-13 Zapmail stopped partitioning by `x-service-provider` and returned
the whole account under both headers. Bucketing by header billed the fleet
twice (1,429 on each plan), the inventory pass relabelled every mailbox
MICROSOFT, and the alert asked Zapmail to release 1,448 slots against a real 19.
"""
import sys
import types

import billing_followup as bf
import infra_lifecycle as il

GOOGLE_SUB = {"id": "g1", "subscriptionStatus": "ACTIVE",
              "uniquePlanKey": "zap_google_pro_monthly", "totalMailboxQuantity": 10}
MS_SUB = {"id": "m1", "subscriptionStatus": "ACTIVE",
          "uniquePlanKey": "zap_microsoft_pro_monthly", "totalMailboxQuantity": 3}
DEAD_SUB = {"id": "x1", "subscriptionStatus": "CANCELLED",
            "uniquePlanKey": "zap_google_pro_monthly", "totalMailboxQuantity": 99}


def _domain(name, provider, n):
    return {"domain": name, "id": name, "serviceProvider": provider,
            "mailboxes": [{"id": f"{name}{i}", "username": f"u{i}"} for i in range(n)]}


INVENTORY = [_domain("g.info", "GOOGLE", 8), _domain("m.info", "MICROSOFT", 3)]


class _Resp:
    def __init__(self, body, status=200):
        self.status_code, self._body, self.text = status, body, "x"

    def json(self):
        return self._body


def _fake_requests(monkeypatch, subs_by_header, domains_by_header):
    mod = types.ModuleType("requests")
    mod.RequestException = Exception

    def get(url, headers=None, timeout=None):
        h = headers["x-service-provider"]
        if "subscriptions" in url:
            return _Resp({"data": subs_by_header[h]})
        return _Resp({"data": {"domains": domains_by_header[h], "totalPages": 1}})

    mod.get = get
    monkeypatch.setitem(sys.modules, "requests", mod)
    monkeypatch.setenv("ZAPMAIL_API_KEY", "k")
    monkeypatch.delenv("INFRA_LIFECYCLE_ZM_FILE", raising=False)
    monkeypatch.setattr(bf, "slots_committed_to_unfinished_orders", lambda: 0)
    monkeypatch.setattr(bf, "_registry", lambda: {})


def _per(board):
    return {p["provider"]: (p["billed"], p["actual"], p["gap"])
            for p in board["per_provider"]}


def test_header_ignored_counts_everything_once(monkeypatch):
    """Both headers return the whole account: no double bill, no relabelling."""
    everything = [GOOGLE_SUB, MS_SUB, DEAD_SUB]
    _fake_requests(monkeypatch, {"GOOGLE": everything, "MICROSOFT": everything},
                   {"GOOGLE": INVENTORY, "MICROSOFT": INVENTORY})
    board = bf.reconcile()
    assert _per(board) == {"GOOGLE": (10, 8, 2), "MICROSOFT": (3, 3, 0)}
    assert board["gap"] == 2


def test_header_partitioned_still_works(monkeypatch):
    """The old behaviour: each header sees only its own half."""
    _fake_requests(monkeypatch,
                   {"GOOGLE": [GOOGLE_SUB, DEAD_SUB], "MICROSOFT": [MS_SUB]},
                   {"GOOGLE": INVENTORY[:1], "MICROSOFT": INVENTORY[1:]})
    assert _per(bf.reconcile()) == {"GOOGLE": (10, 8, 2), "MICROSOFT": (3, 3, 0)}


def test_inventory_provider_comes_from_the_domain(monkeypatch):
    _fake_requests(monkeypatch, {"GOOGLE": [], "MICROSOFT": []},
                   {"GOOGLE": INVENTORY, "MICROSOFT": INVENTORY})
    inv = il.fetch_zapmail_inventory()
    provs = {m["domain"]: m["provider"] for m in inv["mailboxes"].values()}
    assert provs == {"g.info": "GOOGLE", "m.info": "MICROSOFT"}


def test_unreadable_subscriptions_raise_rather_than_report(monkeypatch):
    _fake_requests(monkeypatch, {"GOOGLE": [], "MICROSOFT": []},
                   {"GOOGLE": INVENTORY, "MICROSOFT": INVENTORY})
    mod = sys.modules["requests"]
    real = mod.get
    mod.get = lambda url, **kw: _Resp({}, 500) if "subscriptions" in url else real(url, **kw)
    try:
        bf.reconcile()
    except RuntimeError:
        return
    raise AssertionError("a failed subscription read must not produce a board")
