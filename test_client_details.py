"""One build for every client, and honest about what it could not read."""
import re

import client_details as cd


def norm(n):
    return re.sub(r"[^a-z0-9]+", " ", str(n or "").lower()).strip()


def is_free(c):
    return c.get("name") == "Free Co"


def board(*rows):
    return {"rows": [{
        "crm_name": r[0], "client": r[0], "infra_buckets": [r[0]],
        "mailboxes": r[1], "monthly_cost": r[1] * 3,
        "domains": {"dedicated": [f"d{i}.info" for i in range(r[2])], "shared": [], "pool": []},
        "domain_count": r[2], "effective_end": "2026-12-01", "end_basis": "contract",
        "decision_by": "2026-11-24", "days_to_decision": 66, "hard_stop": "2026-12-15",
        "cohorts": [], "status": "active",
    } for r in rows]}


MBX = {
    "a@d0.info": {"domain": "d0.info", "created_at": "2026-06-01T00:00:00Z"},
    "b@d1.info": {"domain": "d1.info", "created_at": "2026-06-02T00:00:00Z"},
    "c@x.info": {"domain": "x.info", "created_at": "2026-06-03T00:00:00Z"},
}
TAGS = {"a@d0.info": "Acme", "b@d1.info": "Acme", "c@x.info": "Other"}


def test_every_client_is_built_in_one_pass():
    out = cd.build(board(("Acme", 2, 2), ("Other", 1, 1)), MBX, TAGS, {}, [], norm, is_free)
    assert set(out) == {"acme", "other"}
    assert [i["email"] for i in out["acme"]["inboxes"]] == ["a@d0.info", "b@d1.info"]
    assert [i["email"] for i in out["other"]["inboxes"]] == ["c@x.info"]


def test_health_rides_along_when_available():
    out = cd.build(board(("Acme", 2, 2)), MBX, TAGS, {"a@d0.info": "burned"}, [], norm, is_free)
    assert out["acme"]["inboxes"][0]["health"] == "burned"
    assert out["acme"]["inboxes"][1]["health"] is None


def test_an_unreadable_smartlead_walk_gives_None_not_an_empty_list():
    """A client with 57 inboxes must never render as having none."""
    out = cd.build(board(("Acme", 57, 19)), MBX, None, {}, [], norm, is_free)
    assert out["acme"]["inboxes"] is None
    assert out["acme"]["inboxes_unreadable"] is True
    assert out["acme"]["inbox_count"] == 57      # the count still tells the truth


def test_an_unreadable_zapmail_walk_does_the_same():
    out = cd.build(board(("Acme", 57, 19)), None, TAGS, {}, [], norm, is_free)
    assert out["acme"]["inboxes"] is None


def test_a_free_account_gets_no_invented_dates():
    out = cd.build(board(("Free Co", 18, 6)), MBX, TAGS, {}, [{"name": "Free Co"}], norm, is_free)
    f = out["free co"]
    assert f["exempt"] is True
    assert f["term_ends"] is None and f["decide_by"] is None and f["hard_stop"] is None
    assert f["term_basis"] == "free account — no term"


def test_a_paying_client_keeps_its_dates():
    out = cd.build(board(("Acme", 42, 14)), MBX, TAGS, {}, [{"name": "Acme"}], norm, is_free)
    a = out["acme"]
    assert a["term_ends"] == "2026-12-01" and a["days_to_decision"] == 66
    assert a["yearly_cost"] == 42 * 3 * 12


def test_inboxes_are_grouped_by_domain_for_readability():
    mbx = {"z@b.info": {"domain": "b.info"}, "a@a.info": {"domain": "a.info"}}
    tags = {"z@b.info": "Acme", "a@a.info": "Acme"}
    out = cd.build(board(("Acme", 2, 2)), mbx, tags, {}, [], norm, is_free)
    assert [i["domain"] for i in out["acme"]["inboxes"]] == ["a.info", "b.info"]


def test_an_untagged_mailbox_belongs_to_nobody():
    out = cd.build(board(("Acme", 2, 2)), MBX, {"a@d0.info": "Acme"}, {}, [], norm, is_free)
    assert [i["email"] for i in out["acme"]["inboxes"]] == ["a@d0.info"]
