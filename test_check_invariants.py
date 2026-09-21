"""Every rule, against fixtures built from the September 2026 incidents."""
import unittest
import check_invariants as ci


def acct(email, tag=None, extra_tags=()):
    tags = [{"tag_name": "Zapmail"}, {"tag_name": "9/17/26"}]
    if tag:
        tags.append({"tag_name": tag})
    tags += [{"tag_name": t} for t in extra_tags]
    return {"from_email": email, "tags": tags}


def client(name, **kw):
    row = {"name": name, "status": "active", "billing_model": "retainer",
           "launch_date": "2026-07-01", "initial_term_length": 3,
           "monthly_retainer": 2000, "monthly_update_enabled": True}
    row.update(kw)
    return row


def fleet(tag, n, target=42, dom_prefix="d"):
    """n inboxes for `tag`, 3 per domain, the way domains are actually bought."""
    return [acct(f"u{i}@{dom_prefix}{i // 3}.info", tag) for i in range(n)]


class RuleOne(unittest.TestCase):
    def test_a_client_at_target_passes(self):
        s = {"accounts": fleet("Acme", 42), "crm": [client("Acme")]}
        self.assertEqual(ci.rule_1_client_counts(s).status, ci.PASS)

    def test_northstar_at_44_fails(self):
        # Two strays re-tagged in on 2026-09-18.
        s = {"accounts": fleet("Acme", 44), "crm": [client("Acme")]}
        r = ci.rule_1_client_counts(s)
        self.assertEqual(r.status, ci.FAIL)
        self.assertIn("44", r.violations[0])

    def test_seasonal_client_wants_57(self):
        s = {"accounts": fleet("Merry & Bright Christmas Lights", 57),
             "crm": [client("Merry & Bright Christmas Lights")]}
        self.assertEqual(ci.rule_1_client_counts(s).status, ci.PASS)
        s54 = {"accounts": fleet("Merry & Bright Christmas Lights", 54),
               "crm": [client("Merry & Bright Christmas Lights")]}
        self.assertEqual(ci.rule_1_client_counts(s54).status, ci.FAIL)

    def test_free_account_is_exempt(self):
        # Landy Rose: 18 inboxes, run at no charge, by design.
        s = {"accounts": fleet("Landy Rose Media", 18),
             "crm": [client("Landy Rose Media", monthly_retainer=None,
                            monthly_update_enabled=False)]}
        self.assertEqual(ci.rule_1_client_counts(s).status, ci.PASS)

    def test_pool_buckets_are_not_clients(self):
        for tag in ("Cleanup 2026-09", "Generic Landscaping 1", "Replacement Group",
                    "(acquisition)", "Premium Inboxes"):
            s = {"accounts": fleet(tag, 431), "crm": [client("Acme")]}
            self.assertEqual(ci.rule_1_client_counts(s).status, ci.PASS, tag)

    def test_skips_without_input(self):
        self.assertEqual(ci.rule_1_client_counts({}).status, ci.SKIP)


class RuleTwo(unittest.TestCase):
    def test_untagged_fails(self):
        s = {"accounts": [acct("a@d.info", "Acme"), acct("b@d.info")]}
        r = ci.rule_2_one_tag(s)
        self.assertEqual(r.status, ci.FAIL)
        self.assertIn("b@d.info", r.violations[0])

    def test_two_client_tags_fails(self):
        s = {"accounts": [acct("a@d.info", "Acme", extra_tags=("Other Co",))]}
        self.assertEqual(ci.rule_2_one_tag(s).status, ci.FAIL)

    def test_zapmail_and_date_tags_do_not_count(self):
        self.assertEqual(ci.rule_2_one_tag({"accounts": [acct("a@d.info", "Acme")]}).status,
                         ci.PASS)


class RuleThree(unittest.TestCase):
    def test_shared_domain_fails(self):
        s = {"accounts": [acct("a@shared.info", "Acme"), acct("b@shared.info", "Other")]}
        self.assertEqual(ci.rule_3_one_client_per_domain(s).status, ci.FAIL)

    def test_exclusive_domains_pass(self):
        s = {"accounts": [acct("a@x.info", "Acme"), acct("b@x.info", "Acme")]}
        self.assertEqual(ci.rule_3_one_client_per_domain(s).status, ci.PASS)


class RuleFour(unittest.TestCase):
    def _s(self, fwd_bad):
        # Three domains for one client; the third points somewhere else —
        # Clear Heating's prospects landing on Quantum's site.
        accounts = [acct(f"u{i}@d{i}.info", "Acme") for i in range(3)]
        zm = {f"d{i}.info": {"forwardTo": "https://acme.com/", "mailbox_count": 1}
              for i in range(3)}
        if fwd_bad:
            zm["d2.info"]["forwardTo"] = "https://competitor.com/"
        return {"accounts": accounts, "zm_domains": zm}

    def test_all_matching_passes(self):
        self.assertEqual(ci.rule_4_forwarding(self._s(False)).status, ci.PASS)

    def test_one_pointing_elsewhere_fails(self):
        r = ci.rule_4_forwarding(self._s(True))
        self.assertEqual(r.status, ci.FAIL)
        self.assertIn("competitor.com", r.violations[0])

    def test_pool_domains_are_not_checked(self):
        s = {"accounts": [acct("a@p.info", "Replacement Group")],
             "zm_domains": {"p.info": {"forwardTo": None, "mailbox_count": 1}}}
        self.assertEqual(ci.rule_4_forwarding(s).status, ci.PASS)


class RuleFive(unittest.TestCase):
    def test_inboxes_with_no_crm_row_fails(self):
        # LightDMV: 57 inboxes live, no CRM row, so no term and no deadline.
        s = {"accounts": fleet("LightDMV", 57), "crm": [client("Acme")]}
        r = ci.rule_5_crm_rows(s)
        self.assertEqual(r.status, ci.FAIL)
        self.assertTrue(any("no CRM row" in v for v in r.violations))

    def test_missing_term_fails(self):
        s = {"accounts": fleet("Acme", 42),
             "crm": [client("Acme", initial_term_length=None, prepaid_months=None)]}
        r = ci.rule_5_crm_rows(s)
        self.assertTrue(any("no contract term" in v for v in r.violations))

    def test_active_client_with_no_inboxes_fails(self):
        s = {"accounts": fleet("Acme", 42), "crm": [client("Acme"), client("Ghost Co")]}
        r = ci.rule_5_crm_rows(s)
        self.assertTrue(any("no inboxes" in v for v in r.violations))

    def test_free_account_without_inboxes_is_exempt(self):
        s = {"accounts": fleet("Acme", 42),
             "crm": [client("Acme"),
                     client("Landy Rose Media", monthly_retainer=None,
                            monthly_update_enabled=False)]}
        self.assertEqual(ci.rule_5_crm_rows(s).status, ci.PASS)


class RuleSix(unittest.TestCase):
    def test_skips_when_no_campaign_is_live(self):
        # 2026-09-17: every campaign paused, out of credits. A pass here would
        # mean "no domain is at risk" when the truth is "we cannot tell".
        s = {"registrar": {"d.info": {"expires": "2026-09-20", "auto_renew": False}}}
        r = ci.rule_6_expiry(s)
        self.assertEqual(r.status, ci.SKIP)
        self.assertIn("paused", r.detail)

    def test_live_domain_lapsing_soon_fails(self):
        s = {"registrar": {"d.info": {"expires": "2026-09-20", "auto_renew": False}},
             "live_domains": {"d.info"}}
        self.assertEqual(ci.rule_6_expiry(s).status, ci.FAIL)

    def test_auto_renew_on_is_safe(self):
        s = {"registrar": {"d.info": {"expires": "2026-09-20", "auto_renew": True}},
             "live_domains": {"d.info"}}
        self.assertEqual(ci.rule_6_expiry(s).status, ci.PASS)


class RuleSeven(unittest.TestCase):
    # Occupancy comes from `_per_domain`, derived from the mailbox map. The
    # Zapmail domain record carries NO mailbox list, so an earlier version that
    # counted off it read every one of 916 domains as empty.
    def test_empty_and_renewing_fails(self):
        s = {"zm_domains": {"e.info": {}}, "_per_domain": {},
             "registrar": {"e.info": {"auto_renew": True, "expires": "2027-02-18"}}}
        self.assertEqual(ci.rule_7_empty_autorenew(s).status, ci.FAIL)

    def test_empty_and_lapsing_is_fine(self):
        s = {"zm_domains": {"e.info": {}}, "_per_domain": {},
             "registrar": {"e.info": {"auto_renew": False}}}
        self.assertEqual(ci.rule_7_empty_autorenew(s).status, ci.PASS)

    def test_occupied_and_renewing_is_fine(self):
        s = {"zm_domains": {"e.info": {}}, "_per_domain": {"e.info": 3},
             "registrar": {"e.info": {"auto_renew": True}}}
        self.assertEqual(ci.rule_7_empty_autorenew(s).status, ci.PASS)


class RegressionsFromTheFirstLiveRun(unittest.TestCase):
    """Four bugs the first run against the real fleet exposed, one a false PASS."""

    def test_rule_4_reads_forward_to_not_forwardTo(self):
        # Zapmail's inventory helper renames the field. Reading only the
        # camelCase spelling compared None to None and passed while 38 domains
        # pointed at the wrong company.
        s = {"accounts": [acct(f"u{i}@d{i}.info", "Acme") for i in range(3)],
             "zm_domains": {"d0.info": {"forward_to": "https://acme.com/"},
                            "d1.info": {"forward_to": "https://acme.com/"},
                            "d2.info": {"forward_to": "https://competitor.com/"}}}
        r = ci.rule_4_forwarding(s)
        self.assertEqual(r.status, ci.FAIL)
        self.assertIn("competitor.com", r.violations[0])

    def test_lightning_lawn_care_is_not_holiday_lighting(self):
        # "light" is a substring of "Lightning". They are on 42, not 57.
        self.assertEqual(ci.target_for("Lightning Lawn Care", None), 42)
        self.assertEqual(ci.target_for("Merry & Bright Christmas Lights", None), 57)

    def test_both_spellings_of_a_client_resolve_to_57(self):
        # The Smartlead tag and the CRM name differ; whichever reaches
        # target_for has to match or the client reads 15 inboxes over target.
        for n in ("Mary & Brite Christmas Lites (Medford)",
                  "Mary & Brite's Christmas Lights Installation"):
            self.assertEqual(ci.target_for(n, None), 57, n)

    def test_snow_clients_carry_no_seasonal_word(self):
        # Nothing in the name or the CRM services says snow, so they can only
        # come from the explicit list.
        for name in ("Kinsley Landscape Ltd.", "Peak Services Colorado, Inc.",
                     "Gm Landscaping & Design"):
            self.assertEqual(ci.target_for(name, None), 57, name)

    def test_a_free_account_needs_all_three_conditions(self):
        free = {"billing_model": "retainer", "monthly_retainer": None,
                "monthly_update_enabled": False}
        self.assertTrue(ci.is_free_account(free))
        # A per-lead client has no monthly amount by definition and is not free.
        self.assertFalse(ci.is_free_account({**free, "billing_model": "per_lead"}))
        self.assertFalse(ci.is_free_account({**free, "monthly_retainer": 2000}))
        self.assertFalse(ci.is_free_account({**free, "monthly_update_enabled": True}))

    def test_an_unselected_column_must_not_read_as_free(self):
        # monthly_update_enabled comes back None unless fetch_crm_clients asks
        # for it. With billing_model required, a per-lead client whose column
        # was never fetched still cannot be mistaken for a free one.
        self.assertFalse(ci.is_free_account(
            {"billing_model": "per_lead", "monthly_retainer": None}))

    def test_month_to_month_clients_need_no_contract_term(self):
        # Denair: a rolling monthly retainer with no fixed end. Demanding a
        # term would invent a deadline nobody agreed to.
        s = {"accounts": fleet("Denair Hvac, Inc.", 42),
             "crm": [client("Denair Hvac, Inc.", agreement_type="month_to_month",
                            initial_term_length=None, prepaid_months=None)]}
        self.assertEqual(ci.rule_5_crm_rows(s).status, ci.PASS)

    def test_a_fixed_term_client_still_needs_one(self):
        s = {"accounts": fleet("Acme", 42),
             "crm": [client("Acme", agreement_type="prepaid",
                            initial_term_length=None, prepaid_months=None)]}
        self.assertEqual(ci.rule_5_crm_rows(s).status, ci.FAIL)

    def test_per_lead_clients_need_no_contract_term(self):
        s = {"accounts": fleet("Airlast", 42),
             "crm": [client("Airlast", billing_model="per_lead",
                            initial_term_length=None, prepaid_months=None)]}
        self.assertEqual(ci.rule_5_crm_rows(s).status, ci.PASS)

    def test_acquisition_may_share_domains_and_carry_two_tags(self):
        shared = {"accounts": [acct("a@h.info", "Acquisition N"),
                               acct("b@h.info", "Acquisition T")]}
        self.assertEqual(ci.rule_3_one_client_per_domain(shared).status, ci.PASS)
        two = {"accounts": [acct("a@h.info", "Premium Inboxes",
                                 extra_tags=("Acquisition N",))]}
        self.assertEqual(ci.rule_2_one_tag(two).status, ci.PASS)
        # but a CLIENT inbox with two tags is still wrong
        bad = {"accounts": [acct("a@d.info", "Acme", extra_tags=("Other Co",))]}
        self.assertEqual(ci.rule_2_one_tag(bad).status, ci.FAIL)


class RuleEight(unittest.TestCase):
    def test_gap_fails_with_the_annual_cost(self):
        s = {"billing": [{"provider": "GOOGLE", "billed": 1792, "actual": 1720, "gap": 72}]}
        r = ci.rule_8_billing(s)
        self.assertEqual(r.status, ci.FAIL)
        self.assertIn("2,592", r.violations[0])

    def test_matched_passes(self):
        s = {"billing": [{"provider": "GOOGLE", "billed": 1720, "actual": 1720, "gap": 0}]}
        self.assertEqual(ci.rule_8_billing(s).status, ci.PASS)


class RuleNine(unittest.TestCase):
    def test_reserve_at_ceiling_passes(self):
        s = {"accounts": fleet("Generic Landscaping 1", 42, dom_prefix="a")
                         + fleet("Generic Landscaping 2", 42, dom_prefix="b")}
        self.assertEqual(ci.rule_9_pool_ceilings(s).status, ci.PASS)

    def test_reserve_over_ceiling_fails(self):
        s = {"accounts": fleet("Generic Landscaping 1", 50, dom_prefix="a")
                         + fleet("Generic Landscaping 2", 42, dom_prefix="b")}
        r = ci.rule_9_pool_ceilings(s)
        self.assertEqual(r.status, ci.FAIL)
        self.assertIn("8 over", r.violations[0])

    def test_acquisition_is_not_counted(self):
        s = {"accounts": fleet("(acquisition)", 277)}
        self.assertEqual(ci.rule_9_pool_ceilings(s).status, ci.PASS)


class RuleTen(unittest.TestCase):
    def test_smartlead_account_without_a_mailbox_fails(self):
        s = {"accounts": [acct("gone@d.info", "Cleanup 2026-09")],
             "zm_mailboxes": {"live@d.info": {}}}
        self.assertEqual(ci.rule_10_expired_purged(s).status, ci.FAIL)

    def test_externally_hosted_domains_are_exempt(self):
        s = {"accounts": [acct("a@headlinetheoryhq.com", "Premium Inboxes")],
             "zm_mailboxes": {"x@d.info": {}}}
        self.assertEqual(ci.rule_10_expired_purged(s).status, ci.PASS)


class Harness(unittest.TestCase):
    def test_every_rule_skips_on_an_empty_snapshot(self):
        for r in ci.check({}):
            self.assertEqual(r.status, ci.SKIP, f"rule {r.n} should skip")

    def test_a_broken_rule_does_not_mask_the_others(self):
        results = ci.check({"accounts": "not-a-list", "crm": [client("Acme")]})
        self.assertEqual(len(results), 11)

    def test_exit_code_is_nonzero_only_on_failure(self):
        ok = [ci.Result(1, "x", ci.PASS), ci.Result(2, "y", ci.SKIP)]
        bad = ok + [ci.Result(3, "z", ci.FAIL)]
        self.assertFalse(any(r.status == ci.FAIL for r in ok))
        self.assertTrue(any(r.status == ci.FAIL for r in bad))


if __name__ == "__main__":
    unittest.main()


# ── Rule 11: did we actually see the fleet? ──────────────────────────────

def _snap(**kw):
    base = {"accounts": [{"from_email": f"a{i}@x.co"} for i in range(1800)],
            "zm_mailboxes": {f"a{i}@x.co": {} for i in range(1750)},
            "zm_domains": {f"d{i}.co": {} for i in range(600)},
            "crm": [{"name": "Acme"}]}
    base.update(kw)
    return base


def test_rule_11_passes_on_a_full_snapshot():
    r = ci.rule_11_reads_were_complete(_snap())
    assert r.status == "PASS"


def test_rule_11_catches_a_truncated_smartlead_walk():
    """214 acquisition inboxes when there were 307; 286 empty domains when the
    answer was 0. Both were short reads believed in full."""
    r = ci.rule_11_reads_were_complete(_snap(accounts=[{"from_email": "a@x.co"}] * 200))
    assert r.status == "FAIL"
    assert any("truncated walk" in v for v in r.violations)


def test_rule_11_catches_the_postgrest_thousand_row_cap():
    """An unbounded select returns exactly 1000 rows as an ordinary 200."""
    r = ci.rule_11_reads_were_complete(
        _snap(zm_mailboxes={f"a{i}@x.co": {} for i in range(1000)}))
    assert r.status == "FAIL"
    assert any("1000 rows" in v for v in r.violations)


def test_rule_11_catches_an_rls_denied_crm_read():
    """A 200 with zero rows is what a denied read looks like."""
    r = ci.rule_11_reads_were_complete(_snap(crm=[]))
    assert r.status == "FAIL"
    assert any("denied read" in v for v in r.violations)


def test_rule_11_names_a_source_that_was_never_read():
    r = ci.rule_11_reads_were_complete(_snap(accounts=None))
    assert r.status == "FAIL"
    assert any("not read at all" in v for v in r.violations)


def test_rule_11_is_registered_and_last():
    """It has to run, and it reads best beneath the rules it qualifies."""
    assert ci.rule_11_reads_were_complete in ci.RULES
    assert ci.RULES[-1] is ci.rule_11_reads_were_complete
