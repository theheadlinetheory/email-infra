"""Smartlead returns tags under two different keys depending on the source.

    REST /email-accounts/   {"tag_id", "tag_name", "tag_color"}
    GraphQL tag mappings    {"id", "name"}

Reading only "name" makes every REST-sourced tag invisible — which is how sync
published 1,813 accounts attributed to no client: the GraphQL fetch failed, the
REST tags were correctly kept as a fallback, and the extractor could not see
them.
"""
from tag_utils import get_group_tag_from_account


def test_graphql_shape():
    acct = {"tags": [{"id": 1, "name": "Zapmail"}, {"id": 2, "name": "Timesavers A"}]}
    assert get_group_tag_from_account(acct) == "Timesavers A"


def test_rest_shape():
    """The regression. This returned None before."""
    acct = {"tags": [{"tag_id": 1, "tag_name": "Zapmail"},
                     {"tag_id": 2, "tag_name": "Timesavers A"}]}
    assert get_group_tag_from_account(acct) == "Timesavers A"


def test_mixed_shapes_in_one_account():
    acct = {"tags": [{"tag_id": 1, "tag_name": "Zapmail"},
                     {"id": 2, "name": "Landry's Landscape"}]}
    assert get_group_tag_from_account(acct) == "Landry's Landscape"


def test_dates_are_skipped_in_either_shape():
    for key in ("name", "tag_name"):
        acct = {"tags": [{key: "Zapmail"}, {key: "8/10/26"}, {key: "Landry's Landscape"}]}
        assert get_group_tag_from_account(acct) == "Landry's Landscape", key


def test_only_infrastructure_tags_is_no_group():
    acct = {"tags": [{"tag_name": "Zapmail"}, {"tag_name": "8/10/26"}]}
    assert get_group_tag_from_account(acct) is None


def test_no_tags_is_no_group():
    assert get_group_tag_from_account({"tags": []}) is None
    assert get_group_tag_from_account({}) is None


def test_a_tag_with_neither_key_does_not_crash():
    acct = {"tags": [{"colour": "red"}, {"tag_name": "Timesavers A"}]}
    assert get_group_tag_from_account(acct) == "Timesavers A"
