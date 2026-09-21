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


from datetime import date

TODAY = date(2026, 9, 21)


def test_warm_state_is_per_inbox_not_per_group():
    """McFarlane holds 27 inboxes at 17 days and 15 at 13. The old dashboard
    derived ONE warmup_days for the whole group from its earliest date tag, so
    a single verdict covered both batches and hid the half that disagreed."""
    mbx = {f"a{i}@d.info": {"domain": "d.info", "created_at": "2026-09-04T00:00:00Z"} for i in range(27)}
    mbx.update({f"b{i}@d.info": {"domain": "d.info", "created_at": "2026-09-08T00:00:00Z"} for i in range(15)})
    tags = {e: "Acme" for e in mbx}
    out = cd.build(board(("Acme", 42, 1)), mbx, tags, {}, [], norm, is_free, today=TODAY)
    w = out["acme"]["warmup"]
    assert w["ready"] == 27 and w["warming"] == 15
    assert w["all_ready_on"] == "2026-09-22"


def test_day_fourteen_has_finished_warming():
    """Strictly less than: an inbox created 14 days ago can send."""
    assert cd.warm_state("2026-09-07", TODAY)["ready"] is True       # 14 days
    assert cd.warm_state("2026-09-08", TODAY)["ready"] is False      # 13 days


def test_a_warming_inbox_says_when_it_is_ready():
    w = cd.warm_state("2026-09-08", TODAY)
    assert w["age_days"] == 13 and w["ready_on"] == "2026-09-22"


def test_no_creation_date_is_unknown_not_ready():
    """Absence of a date is not evidence that it finished warming."""
    w = cd.warm_state(None, TODAY)
    assert w["ready"] is None and w["age_days"] is None


def test_an_unknown_date_is_counted_separately_from_ready():
    mbx = {"a@d.info": {"domain": "d.info", "created_at": None}}
    out = cd.build(board(("Acme", 1, 1)), mbx, {"a@d.info": "Acme"}, {}, [], norm, is_free, today=TODAY)
    w = out["acme"]["warmup"]
    assert w["ready"] == 0 and w["warming"] == 0 and w["unknown"] == 1


def test_warmup_is_None_when_the_inbox_list_could_not_be_read():
    out = cd.build(board(("Acme", 42, 14)), None, None, {}, [], norm, is_free, today=TODAY)
    assert out["acme"]["warmup"] is None
