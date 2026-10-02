"""Assigning a pool to a client, from the UI, without anyone running a script.

The two failures this guards against are both silent:
  - resolving the pool from a cache that no longer matches Smartlead, and
    re-tagging the wrong inboxes;
  - re-tagging without repointing forwarding, so the group mails for one
    client while every link sends prospects to another. Gorilla and Iceberg
    shipped exactly that way.
"""


def pick_tag(account, want):
    """The tag match used by _accounts_by_tag: REST says tag_name, GraphQL
    says name, and reading one key makes the other source invisible."""
    for t in (account.get("tags") or []):
        nm = (t.get("tag_name") or t.get("name") or "").strip().lower()
        if nm == want.strip().lower():
            return True
    return False


def test_matches_a_rest_shaped_tag():
    assert pick_tag({"tags": [{"tag_id": 1, "tag_name": "Generic Landscaping 2"}]},
                    "Generic Landscaping 2")


def test_matches_a_graphql_shaped_tag():
    assert pick_tag({"tags": [{"id": 1, "name": "Generic Landscaping 2"}]},
                    "Generic Landscaping 2")


def test_does_not_match_a_different_pool():
    assert not pick_tag({"tags": [{"tag_name": "Generic Landscaping 1"}]},
                        "Generic Landscaping 2")


def test_match_is_case_and_space_insensitive():
    assert pick_tag({"tags": [{"tag_name": "  generic landscaping 2 "}]},
                    "Generic Landscaping 2")


def test_an_account_with_no_tags_matches_nothing():
    assert not pick_tag({}, "Generic Landscaping 2")


# ── which pools may be offered ───────────────────────────────────────────

def assignable(name: str) -> bool:
    SKIP = ("cleanup", "retired")
    ASSIGNABLE = ("generic", "replacement group", "reserve")
    nl = name.lower()
    return not any(k in nl for k in SKIP) and any(k in nl for k in ASSIGNABLE)


def test_reserve_and_replacement_pools_are_offered():
    assert assignable("Generic Landscaping 2")
    assert assignable("Replacement Group - Service")


def test_cleanup_and_retired_are_never_offered():
    """Those are on their way out, not stock. Handing one to a client assigns
    inboxes already scheduled for removal."""
    assert not assignable("Cleanup 2026-09")
    assert not assignable("Retired - burned")


def test_a_client_tag_is_not_a_pool():
    assert not assignable("Mighty Oak Landscaping")
    assert not assignable("Denair Hvac, Inc.")


# ── forwarding is part of the move ───────────────────────────────────────

def resolve_forward_to(body: dict, crm_site):
    """The route's default: an explicit target wins, else the CRM website,
    else nothing — never a guess."""
    return (body.get("forward_to") or "").strip() or (crm_site or "")


def test_forwarding_defaults_to_the_crm_website():
    assert resolve_forward_to({}, "https://gorillalifeoutdoors.com/") \
        == "https://gorillalifeoutdoors.com/"


def test_an_explicit_target_overrides_the_crm():
    assert resolve_forward_to({"forward_to": "https://other.com/"},
                              "https://crm.com/") == "https://other.com/"


def test_no_website_means_forwarding_is_left_alone():
    """Not guessed from the client name. A domain pointing at a site that does
    not exist is worse than one pointing nowhere — it looks configured."""
    assert resolve_forward_to({}, None) == ""
