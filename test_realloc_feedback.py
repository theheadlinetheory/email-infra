"""After a reallocation the operator must be told what happened, to every inbox.

Three faults, all in the UI:
  - the result was written into the domain card, which the forced refresh
    destroys — so the one action that moves live senders flashed and vanished;
  - only the swapped count was shown, so "2 of 3" left the third unexplained;
  - the campaigns needing the manual "Reallocate mailboxes" click were returned
    by the API, with URLs, and thrown away.
"""


def account_for(asked: int, swapped: int, held: int, failed: int) -> dict:
    """Every inbox asked about lands in exactly one bucket. The leftover is
    'already had a job' — the case that made 2-of-3 look broken."""
    return {"swapped": swapped, "held": held, "failed": failed,
            "already_had_job": max(0, asked - swapped - held - failed)}


def test_every_inbox_is_accounted_for():
    r = account_for(asked=3, swapped=2, held=0, failed=0)
    assert r["already_had_job"] == 1
    assert sum(r.values()) == 3


def test_a_held_inbox_is_not_a_failure():
    """Held = owns a positive reply and was deliberately kept. Reporting it as
    a failure would push someone to retry and detach a live conversation."""
    r = account_for(asked=3, swapped=2, held=1, failed=0)
    assert r["held"] == 1 and r["failed"] == 0 and r["already_had_job"] == 0


def test_a_clean_full_swap_leaves_nothing_unexplained():
    r = account_for(asked=3, swapped=3, held=0, failed=0)
    assert r["already_had_job"] == 0


def test_failures_are_counted_separately_from_holds():
    r = account_for(asked=4, swapped=1, held=1, failed=2)
    assert r == {"swapped": 1, "held": 1, "failed": 2, "already_had_job": 0}


def test_the_leftover_never_goes_negative():
    """Defensive: a backend that reports more swapped than asked must not
    produce a nonsense negative bucket."""
    assert account_for(asked=2, swapped=3, held=0, failed=0)["already_had_job"] == 0


# ── the manual SmartLead click ───────────────────────────────────────────

def campaign_links(payload: dict) -> list:
    return [c for c in (payload.get("campaigns_to_reallocate") or []) if c.get("url")]


def test_campaign_links_are_surfaced_when_present():
    p = {"campaigns_to_reallocate": [
        {"name": "Mary & Bright #1", "id": 42,
         "url": "https://app.smartlead.ai/app/email-campaign/42/analytics"}]}
    assert len(campaign_links(p)) == 1


def test_a_campaign_without_an_id_is_not_offered_as_a_link():
    p = {"campaigns_to_reallocate": [{"name": "Unknown", "id": None, "url": None}]}
    assert campaign_links(p) == []


def test_no_active_campaign_means_no_click_is_needed():
    assert campaign_links({"campaigns_to_reallocate": []}) == []
    assert campaign_links({}) == []
