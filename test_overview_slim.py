"""?slim=1 must strip account rows at EVERY depth, and change nothing else."""


def strip(obj, counter):
    """The same recursion /api/overview uses, lifted so it can be tested
    without a Flask request context."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "account_details":
                counter[0] += len(v or [])
                continue
            out[k] = strip(v, counter)
        return out
    if isinstance(obj, list):
        return [strip(x, counter) for x in obj]
    return obj


def payload():
    acct = [{"email": f"a{i}@x.co", "sent": 5} for i in range(3)]
    return {
        "clients": [{
            "name": "Acme", "accounts": 3, "account_details": list(acct),
            # the nested copy that made the first attempt useless
            "group_a": {"letter": "A", "account_details": list(acct)},
            "group_b": None,
            "campaigns": [{"id": 1, "name": "c", "status": "ACTIVE"}],
        }],
        "acquisition_groups": [{"label": "T", "account_details": list(acct)}],
        "total_accounts": 6,
    }


def test_nested_account_details_are_stripped_too():
    c = [0]
    out = strip(payload(), c)
    assert "account_details" not in out["clients"][0]
    assert "account_details" not in out["clients"][0]["group_a"]
    assert "account_details" not in out["acquisition_groups"][0]
    assert c[0] == 9


def test_everything_else_survives_untouched():
    out = strip(payload(), [0])
    cl = out["clients"][0]
    assert cl["name"] == "Acme" and cl["accounts"] == 3
    assert cl["group_a"]["letter"] == "A"
    assert cl["campaigns"] == [{"id": 1, "name": "c", "status": "ACTIVE"}]
    assert cl["group_b"] is None
    assert out["total_accounts"] == 6


def test_it_is_a_copy_so_the_cache_is_not_mutated():
    p = payload()
    strip(p, [0])
    assert len(p["clients"][0]["account_details"]) == 3
    assert len(p["clients"][0]["group_a"]["account_details"]) == 3


def test_stripping_something_with_no_account_details_is_a_no_op():
    d = {"a": 1, "b": [{"c": 2}]}
    assert strip(d, [0]) == d
