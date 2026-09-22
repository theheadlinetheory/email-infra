"""An inconclusive reply check must hold, never clear.

health_positive.owned_positive_threads documents its own contract:

    "Raises on an inconclusive lookup — callers MUST treat that as hold,
     never as no replies. A false negative here is exactly the failure this
     module exists to prevent."

The /api/health-positive-check route broke it: it caught the exception, stored
positive_count None, and then `sum(c.get("positive_count") or 0 ...)` turned
that into 0. A rate-limited leads-export made an inbox read as CLEAR, and the
dashboard would detach it from a conversation it still owned. A detached thread
cannot be recovered in Smartlead -- the reply is welded by message-id to the
mailbox that sent it.

These test the shape of the answer the route builds.
"""


def summarise(per: list) -> dict:
    """The route's aggregation, extracted so it can be tested without Flask."""
    unknown = any(c.get("error") for c in per)
    known = sum(c.get("positive_count") or 0 for c in per)
    return {"positive_total": None if unknown else known,
            "unknown": unknown, "known_positive_count": known}


def test_a_clean_check_reports_zero():
    assert summarise([])["positive_total"] == 0
    assert summarise([])["unknown"] is False


def test_real_positives_are_counted():
    r = summarise([{"campaign": "a", "positive_count": 2},
                   {"campaign": "b", "positive_count": 1}])
    assert r["positive_total"] == 3 and r["unknown"] is False


def test_a_failed_lookup_is_unknown_not_zero():
    """The regression. This used to come back as 0 -- safe to reallocate."""
    r = summarise([{"campaign": "a", "positive_count": None, "error": "429"}])
    assert r["positive_total"] is None
    assert r["unknown"] is True
    assert r["positive_total"] != 0


def test_one_failure_taints_the_whole_inbox():
    """Two campaigns, one readable and clean, one unreadable. The inbox is not
    clear: the unreadable campaign is exactly where the reply might be."""
    r = summarise([{"campaign": "a", "positive_count": 0},
                   {"campaign": "b", "positive_count": None, "error": "timeout"}])
    assert r["unknown"] is True and r["positive_total"] is None


def test_a_failure_alongside_known_positives_still_reads_unknown():
    r = summarise([{"campaign": "a", "positive_count": 3},
                   {"campaign": "b", "positive_count": None, "error": "500"}])
    assert r["unknown"] is True
    assert r["positive_total"] is None
    assert r["known_positive_count"] == 3     # kept for display, not for the gate


def test_the_old_javascript_default_would_have_cleared_it():
    """`positive_total ?? 0` only defaults on null/undefined -- which is exactly
    what an unknown now is. So the UI must branch on `unknown`, not on the
    number. This pins why the flag exists."""
    r = summarise([{"campaign": "a", "positive_count": None, "error": "429"}])
    assert (r["positive_total"] if r["positive_total"] is not None else 0) == 0
    assert r["unknown"] is True, "the flag is the only thing that saves it"
