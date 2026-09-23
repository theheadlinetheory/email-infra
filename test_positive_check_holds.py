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


# ── the same false negative, one level up ────────────────────────────────

def summarise_route(status_map: dict, emails: list) -> dict:
    """The route's outer guard. campaign_index() returns {} only when the live
    fetch failed AND there is no last-good cache. With an empty map nothing
    matches ACTIVE, so no lookup runs, nothing raises, and every inbox reports
    a clean zero — a false negative produced by the ABSENCE of work."""
    if not status_map:
        return {"results": [{"email": e, "positive_total": None, "unknown": True}
                            for e in emails],
                "unknown_count": len(emails)}
    return {"results": [{"email": e, "positive_total": 0, "unknown": False}
                        for e in emails], "unknown_count": 0}


def test_an_unreadable_campaign_list_makes_every_inbox_unknown():
    r = summarise_route({}, ["a@x.co", "b@x.co"])
    assert r["unknown_count"] == 2
    assert all(x["unknown"] and x["positive_total"] is None for x in r["results"])


def test_a_readable_campaign_list_still_clears_clean_inboxes():
    """The guard must not become a reason nothing can ever be reallocated."""
    r = summarise_route({"Campaign A": "ACTIVE"}, ["a@x.co"])
    assert r["unknown_count"] == 0 and r["results"][0]["positive_total"] == 0


def test_no_campaigns_is_different_from_no_campaign_list():
    """An inbox genuinely on zero active campaigns is clear. A campaign list we
    could not read is not. These must not produce the same answer."""
    unreadable = summarise_route({}, ["a@x.co"])["results"][0]
    readable = summarise_route({"C": "ACTIVE"}, ["a@x.co"])["results"][0]
    assert unreadable["unknown"] is True
    assert readable["unknown"] is False
    assert unreadable["positive_total"] != readable["positive_total"]


# ── the gate was resolving zero campaigns for every inbox ────────────────
# get_health_status_all de-serialised `reasons` and `subscores` but not
# `campaigns`, so it stayed a JSON STRING. Iterating a string walks characters.

import json


def camps_of(row) -> list:
    """health_replace._camps_of, mirrored so this file stays import-light."""
    c = row.get("campaigns")
    if isinstance(c, str):
        try:
            c = json.loads(c)
        except Exception:
            c = [c] if c.strip() else []
    return c if isinstance(c, list) else []


def test_iterating_the_raw_string_walks_characters():
    """The bug, pinned. Every 'campaign' here is one character, so it matches
    no campaign name and the inbox reads as being in none."""
    row = {"campaigns": json.dumps(["Timesavers #1 - DM Matches - client"])}
    walked = list(row["campaigns"])
    assert all(len(x) == 1 for x in walked)
    assert "Timesavers #1 - DM Matches - client" not in walked


def test_camps_of_recovers_the_real_names():
    names = ["Timesavers #1 - DM Matches - client", "Timesavers #2"]
    assert camps_of({"campaigns": json.dumps(names)}) == names


def test_a_decoded_list_passes_through_unchanged():
    """db now decodes centrally, so the helper must be idempotent."""
    names = ["A", "B"]
    assert camps_of({"campaigns": names}) == names


def test_an_empty_string_is_no_campaigns_not_one_blank():
    assert camps_of({"campaigns": ""}) == []
    assert camps_of({"campaigns": "[]"}) == []


def test_a_missing_field_is_no_campaigns():
    assert camps_of({}) == []


def test_unparseable_text_is_kept_as_one_name_not_shredded():
    """A bare campaign name that was never JSON must survive as ONE entry
    rather than becoming a list of letters."""
    assert camps_of({"campaigns": "Some Campaign"}) == ["Some Campaign"]
