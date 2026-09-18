"""The CRM -> infra handoff rules.

The important ones are about what must NOT read as onboarded.
"""
import re

import onboarding_view as ov

TODAY = "2026-09-18"
def TARGET(name, crm_row=None):
    """Stands in for check_invariants.target_for."""
    return 57 if "dmv" in name.lower() else 42


def MATCH(infra_name, crm_names):
    """Stands in for infra_lifecycle.match_client — the tag and the CRM name are
    spelled differently, which is the whole reason it is injected."""
    a = re.sub(r"[^a-z0-9]", "", infra_name.lower())
    for c in crm_names:
        if re.sub(r"[^a-z0-9]", "", c.lower()) == a:
            return c
    return None


def crm(name="Acme Co", **kw):
    row = {"name": name, "status": "active", "website": "acme.com",
           "launch_date": "2026-08-01", "billing_model": "retainer"}
    row.update(kw)
    return row


def board(*rows):
    return {"rows": [{"crm_name": r[0], "client": r[0], "mailboxes": r[1]} for r in rows]}


def doms(name, n, forward_to="acme.com"):
    return {name: [{"domain": f"d{i}.info", "forward_to": forward_to} for i in range(n)]}


def test_a_client_with_everything_is_not_in_the_queue():
    r = ov.build([crm()], board(("Acme Co", 42)), doms("Acme Co", 14), TARGET, TODAY, MATCH)
    assert r["summary"]["needs_work"] == 0
    assert r["complete"] == ["Acme Co"]


def test_a_won_client_with_no_infrastructure_is_the_top_of_the_queue():
    """The whole point: the CRM says active, infra has nothing, and until now
    no screen anywhere said so."""
    r = ov.build([crm()], board(), {}, TARGET, TODAY, MATCH)
    assert r["summary"]["no_infra"] == 1
    e = r["queue"][0]
    assert e["blocking"] == "infra" and e["short"] == 42
    assert "infrastructure has none" in " ".join(e["notes"])


def test_seasonal_clients_are_owed_57_not_42():
    r = ov.build([crm("Light Dmv")], board(), {}, TARGET, TODAY, MATCH)
    assert r["queue"][0]["target"] == 57
    assert r["summary"]["inboxes_to_buy"] == 57


def test_below_target_reports_the_exact_shortfall():
    r = ov.build([crm()], board(("Acme Co", 38)), doms("Acme Co", 13), TARGET, TODAY, MATCH)
    e = r["queue"][0]
    assert e["blocking"] == "target" and e["short"] == 4
    assert "38 of 42" in " ".join(e["notes"])


def test_domains_forwarding_nowhere_are_caught():
    """LightDMV shipped with 15 of 19 domains forwarding nowhere and every
    individual system read healthy."""
    d = {"Acme Co": [{"domain": "a.info", "forward_to": None},
                     {"domain": "b.info", "forward_to": "acme.com"}]}
    r = ov.build([crm()], board(("Acme Co", 42)), d, TARGET, TODAY, MATCH)
    e = r["queue"][0]
    assert e["blocking"] == "forwarding"
    assert "1 of 2 domains wrong (1 with none set)" in " ".join(e["notes"])


def test_domains_forwarding_to_the_wrong_company_are_caught():
    r = ov.build([crm()], board(("Acme Co", 42)),
                 doms("Acme Co", 3, forward_to="https://someoneelse.com"), TARGET, TODAY, MATCH)
    assert r["queue"][0]["blocking"] == "forwarding"


def test_scheme_and_www_do_not_count_as_a_mismatch():
    r = ov.build([crm(website="https://www.acme.com/")], board(("Acme Co", 42)),
                 doms("Acme Co", 3, forward_to="https://acme.com"), TARGET, TODAY, MATCH)
    assert r["summary"]["needs_work"] == 0


def test_unreadable_zapmail_makes_forwarding_unknown_not_correct():
    """A client whose forwarding we could not check must never read as done."""
    r = ov.build([crm()], board(("Acme Co", 42)), None, TARGET, TODAY, MATCH)
    e = r["queue"][0]
    assert e["forwarding_unknown"] is True
    assert r["summary"]["forwarding_checked"] is False
    assert r["complete"] == []


def test_a_missing_website_is_reported_rather_than_silently_skipped():
    r = ov.build([crm(website=None)], board(("Acme Co", 42)), doms("Acme Co", 3),
                 TARGET, TODAY, MATCH)
    assert "forwarding" in r["queue"][0]["steps"]
    assert "website first" in " ".join(r["queue"][0]["notes"])


def test_forwarding_is_not_raised_before_there_are_any_inboxes():
    """Telling someone their forwarding is wrong when they own no inboxes is noise."""
    r = ov.build([crm()], board(), {}, TARGET, TODAY, MATCH)
    assert r["queue"][0]["steps"] == ["infra"]


def test_a_missing_launch_date_is_its_own_step():
    r = ov.build([crm(launch_date=None)], board(("Acme Co", 42)),
                 doms("Acme Co", 14), TARGET, TODAY, MATCH)
    e = r["queue"][0]
    assert e["steps"] == ["launch"]
    assert "billing clock" in " ".join(e["notes"])


def test_inactive_clients_are_not_onboarding_work():
    r = ov.build([crm(status="inactive"), crm("Live Co")], board(("Live Co", 42)),
                 doms("Live Co", 14), TARGET, TODAY, MATCH)
    assert r["summary"]["active_clients"] == 1


def test_the_queue_leads_with_the_earliest_blocking_step():
    rows = [crm("No Infra"), crm("Short"), crm("Bad Fwd"), crm("No Date", launch_date=None)]
    b = board(("Short", 30), ("Bad Fwd", 42), ("No Date", 42))
    d = {"Short": [{"domain": "s.info", "forward_to": "acme.com"}],
         "Bad Fwd": [{"domain": "b.info", "forward_to": None}],
         "No Date": [{"domain": "n.info", "forward_to": "acme.com"}]}
    r = ov.build(rows, b, d, TARGET, TODAY, MATCH)
    assert [e["blocking"] for e in r["queue"]] == ["infra", "target", "forwarding", "launch"]


def test_total_inboxes_to_buy_adds_up_across_the_queue():
    rows = [crm("A"), crm("B")]
    r = ov.build(rows, board(("B", 40)), {"B": [{"domain": "b.info", "forward_to": "acme.com"}]},
                 TARGET, TODAY, MATCH)
    assert r["summary"]["inboxes_to_buy"] == 42 + 2


def test_a_tag_spelled_differently_from_the_crm_name_still_matches():
    """LightDMV: the board labels the row with the SmartLead tag, the CRM row
    says "Light Dmv". A plain compare reported a fully-built 57-inbox client as
    having no infrastructure at all."""
    r = ov.build([crm("Light Dmv", website="lightdmv.com")],
                 board(("LightDMV", 57)),
                 {"Light Dmv": [{"domain": "d.info", "forward_to": "lightdmv.com"}]},
                 TARGET, TODAY, MATCH)
    assert r["summary"]["no_infra"] == 0
    assert r["complete"] == ["Light Dmv"]


def test_without_a_matcher_it_falls_back_to_the_label():
    r = ov.build([crm()], board(("Acme Co", 42)), doms("Acme Co", 3), TARGET, TODAY)
    assert r["summary"]["needs_work"] == 0
