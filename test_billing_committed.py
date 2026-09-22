"""Slots bought for an order still creating its mailboxes are not waste.

They look identical to phantom slots -- billed, no mailbox behind them -- and
they are the opposite: paid-for capacity the next provisioning pass fills. The
alert asked Zapmail to release 12 slots that were the 12 mailboxes an in-flight
order was about to create.
"""
import billing_followup as bf


class FakeBI:
    def __init__(self, orders): self._o = orders
    def _orders(self): return self._o


def _orders(monkeypatch, orders):
    import sys, types
    mod = types.ModuleType("buy_inboxes")
    mod._orders = lambda: orders
    monkeypatch.setitem(sys.modules, "buy_inboxes", mod)


def order(step, domains, made, per=3, ready=True):
    return {"inboxes_per_domain": per, "domains": domains,
            "journal": {"step": step,
                        "domains": {d: {"dns_ready": ready,
                                        "mailboxes": ["m"] * made.get(d, 0)}
                                    for d in domains}}}


def test_an_unfinished_order_holds_its_unfilled_slots(monkeypatch):
    _orders(monkeypatch, [order("mailboxes", ["a.co", "b.co"], {"a.co": 3})])
    assert bf.slots_committed_to_unfinished_orders() == 3     # b.co owes 3


def test_a_finished_order_holds_nothing(monkeypatch):
    _orders(monkeypatch, [order("done", ["a.co", "b.co"], {"a.co": 3})])
    assert bf.slots_committed_to_unfinished_orders() == 0


def test_a_domain_that_is_not_resolving_holds_no_slot(monkeypatch):
    """No slot is bought for a domain the DNS step has not cleared."""
    _orders(monkeypatch, [order("mailboxes", ["a.co"], {}, ready=False)])
    assert bf.slots_committed_to_unfinished_orders() == 0


def test_a_fully_created_order_awaiting_later_steps_holds_nothing(monkeypatch):
    """Sitting at `forwarding` with every mailbox made owes no slots."""
    _orders(monkeypatch, [order("forwarding", ["a.co"], {"a.co": 3})])
    assert bf.slots_committed_to_unfinished_orders() == 0


def test_a_read_failure_never_inflates_the_gap(monkeypatch):
    """Failing to read orders must report 0 held, not crash and not guess --
    the gap stays as it was rather than being wrongly suppressed or enlarged."""
    import sys, types
    mod = types.ModuleType("buy_inboxes")
    def boom(): raise RuntimeError("store down")
    mod._orders = boom
    monkeypatch.setitem(sys.modules, "buy_inboxes", mod)
    assert bf.slots_committed_to_unfinished_orders() == 0


def test_several_orders_accumulate(monkeypatch):
    _orders(monkeypatch, [order("mailboxes", ["a.co"], {}),
                          order("mailboxes", ["b.co"], {"b.co": 1})])
    assert bf.slots_committed_to_unfinished_orders() == 3 + 2
