"""Tests for infra_lifecycle — run with `.venv/bin/python test_infra_lifecycle.py`.

Stdlib unittest on purpose: this repo has no test runner, and the maths here
decides when real mailboxes get deleted, so it has to be checkable without
installing anything.
"""

import unittest
from datetime import date

import infra_lifecycle as il


def mb(created, domain, email=None):
    """A minimal Zapmail mailbox record."""
    return {"created_at": created + "T12:00:00.000Z", "domain": domain,
            "email": email or f"a@{domain}", "bucket": "x"}


class Dates(unittest.TestCase):
    def test_anniversary_is_month_end_safe(self):
        # A mailbox made on the 31st bills on the 28th in February and springs
        # back to the 31st in March. Repeated +1-month addition would clamp to
        # the 28th and stay there, quietly moving every later billing date.
        jan31 = date(2026, 1, 31)
        self.assertEqual(il.anniversary_on_or_after(jan31, date(2026, 2, 1)), date(2026, 2, 28))
        self.assertEqual(il.anniversary_on_or_after(jan31, date(2026, 3, 1)), date(2026, 3, 31))
        self.assertEqual(il.anniversary_on_or_after(jan31, date(2026, 4, 1)), date(2026, 4, 30))

    def test_anniversary_includes_the_day_itself(self):
        a = date(2026, 7, 15)
        self.assertEqual(il.anniversary_on_or_after(a, date(2026, 7, 15)), date(2026, 7, 15))
        self.assertEqual(il.anniversary_on_or_after(a, date(2026, 7, 16)), date(2026, 8, 15))

    def test_anniversary_before_anchor_returns_anchor(self):
        a = date(2026, 7, 15)
        self.assertEqual(il.anniversary_on_or_after(a, date(2026, 1, 1)), a)

    def test_season_end_rolls_to_next_year(self):
        self.assertEqual(il.season_end_on_or_after(12, 25, date(2026, 9, 1)), date(2026, 12, 25))
        self.assertEqual(il.season_end_on_or_after(12, 25, date(2027, 1, 5)), date(2027, 12, 25))


class Naming(unittest.TestCase):
    def test_smartlead_and_crm_spellings_match(self):
        crm = ["Mary & Brite's Christmas Lights", "Galaxy Plumbing Inc.",
               "Lightning Lawn Care", "Denair Hvac, Inc."]
        self.assertEqual(il.match_client("Mary & Brite Christmas Lites (Medford) Group", crm),
                         "Mary & Brite's Christmas Lights")
        self.assertEqual(il.match_client("Galaxy Plumbing Inc. Group", crm), "Galaxy Plumbing Inc.")
        self.assertEqual(il.match_client("Denair Group", crm), "Denair Hvac, Inc.")

    def test_unknown_client_does_not_get_forced_onto_someone(self):
        crm = ["Galaxy Plumbing Inc.", "Lightning Lawn Care"]
        self.assertIsNone(il.match_client("Wonderly Lights of Birmingham Group", crm))

    def test_lightning_is_not_a_holiday_lighting_client(self):
        # "Lightning" contains "light"; the seasonal matcher must not bite.
        self.assertEqual(il.classify_vertical("Lightning Lawn Care")[0], None)
        self.assertEqual(il.classify_vertical("Lightning Group")[0], None)

    def test_a_landscaper_who_also_plows_snow_is_not_seasonal(self):
        # GM Landscaping is year-round and lists "Snow plowing" among 15 trades.
        # Reading the services array made it a seasonal snow client and put a
        # 31 March hard stop on 80 live inboxes.
        gm = ["Weekly lawn maintenance", "Snow plowing", "Salting/de-icing",
              "Sidewalk snow clearing", "Patio installation"]
        self.assertIsNone(il.classify_vertical("Gm Landscaping & Design", gm)[0])

    def test_holiday_lighting_is_detected(self):
        for name in ("Merry & Bright Christmas Lights", "Mary & Brite Christmas Lites (Medford)",
                     "Wonderly Lights of Birmingham"):
            self.assertEqual(il.classify_vertical(name)[0], "holiday_lighting", name)


class SeasonalScenario(unittest.TestCase):
    """Aidan's drawing: 2 weeks of warm-up, then a 2-month engagement."""

    def setUp(self):
        self.crm = {"name": "Twinkle Lights Co", "status": "active",
                    "billing_model": "retainer", "agreement_type": "prepaid",
                    "launch_date": "2026-02-01", "prepaid_months": 2, "services": []}
        # Inboxes bought and warmed from 2026-01-18, so they are sendable 02-01.
        self.mbs = [mb("2026-01-18", f"twinklelights{i}.info") for i in range(10)]
        self.dom = {m["domain"]: {"Twinkle Lights Co Group"} for m in self.mbs}

    def row(self, today=date(2026, 2, 1)):
        return il.build_client("Twinkle Lights Co Group", self.mbs, self.crm,
                               {}, {}, self.dom, today)

    def test_engagement_ends_two_months_after_launch(self):
        self.assertEqual(self.row()["effective_end"], "2026-04-01")

    def test_hard_stop_is_the_billing_anniversary_after_the_end(self):
        # Billing anchor is the 18th, so the last cycle runs 03-18 -> 04-18.
        r = self.row()
        self.assertEqual(r["hard_stop"], "2026-04-18")
        # 2 days of operational slack, not a billing cut-off — Zapmail confirmed
        # a cancellation filed 1 day out still optimises that cycle.
        self.assertEqual(r["schedule_by"], "2026-04-16")

    def test_the_third_cycle_is_the_one_aidan_is_paying_for(self):
        # 01-18, 02-18, 03-18 = three cycles bought to deliver two months.
        r = self.row()
        cohort = r["cohorts"][0]
        self.assertEqual(cohort["billing_day"], 18)
        self.assertEqual(cohort["sendable_from"], "2026-02-01")   # 14-day warm-up
        self.assertEqual(cohort["wasted_days"], 17)               # 04-01 -> 04-18

    def test_the_client_is_asked_at_the_end_then_given_silent_grace(self):
        # Aidan's model: ask ON the end date, then a week of grace the client is
        # never told about. The first build had this inverted (end - 7), which
        # chased clients a week early and gave no grace at all.
        r = self.row()
        self.assertEqual(r["ask_on"], "2026-04-01")               # the term end itself
        self.assertEqual(r["grace_until"], "2026-04-08")          # +7, undisclosed
        self.assertEqual(r["decision_by"], "2026-04-08")
        self.assertGreater(r["decision_by"], r["effective_end"])  # AFTER, not before

    def test_grace_cannot_outrun_the_billing_date(self):
        # If the first cohort re-bills before grace expires, the billing date
        # wins — there is no point granting grace we cannot act on.
        r = self.row()
        self.assertLessEqual(r["decision_by"], r["schedule_by"])

    def test_dedicated_domains_are_cancelled(self):
        self.assertEqual(self.row()["action"], "cancel")

    def test_a_lighting_name_the_matcher_misses_is_not_seasonal_by_itself(self):
        # "Twinkle Lights Co" carries no keyword, so the heuristic sees an
        # evergreen client. This is the gap the override registry exists to fill.
        self.assertFalse(self.row()["seasonal"])

    def test_an_override_declares_a_vertical_the_name_misses(self):
        r = il.build_client("Twinkle Lights Co Group", self.mbs, self.crm,
                            {"vertical": "holiday_lighting"}, {}, self.dom,
                            date(2026, 2, 1))
        self.assertTrue(r["seasonal"])
        self.assertEqual(r["vertical_label"], "Holiday lighting")

    def test_a_contract_that_outruns_the_season_is_cut_at_the_season(self):
        # An autumn lighting client sold six months would run to April on paper.
        # Nobody books Christmas lights in March, so the infra stops at the season.
        crm = dict(self.crm, name="Twinkle Christmas Lights",
                   launch_date="2026-10-01", prepaid_months=6)   # paper end 2027-04-01
        r = il.build_client("Twinkle Lights Co Group", self.mbs, crm, {}, {},
                            self.dom, date(2026, 10, 1))
        self.assertTrue(r["seasonal"])
        # Christmas, not 31 Dec — Aidan: "Christmas probably would be when they
        # would be done, because that would be all the last minute."
        self.assertEqual(r["effective_end"], "2026-12-25")
        self.assertEqual(r["end_basis"], "season close (before contract end)")

    def test_a_contract_ending_before_the_season_is_left_alone(self):
        crm = dict(self.crm, name="Twinkle Christmas Lights")     # 2-month, ends 04-01
        r = il.build_client("Twinkle Lights Co Group", self.mbs, crm, {}, {},
                            self.dom, date(2026, 2, 1))
        self.assertEqual(r["effective_end"], "2026-04-01")

    def test_an_override_can_suppress_a_false_positive(self):
        crm = dict(self.crm, name="Twinkle Christmas Lights",
                   launch_date="2026-10-01", prepaid_months=6)
        r = il.build_client("Twinkle Lights Co Group", self.mbs, crm,
                            {"seasonal": False}, {}, self.dom, date(2026, 10, 1))
        self.assertFalse(r["seasonal"])
        self.assertEqual(r["effective_end"], "2027-04-01")

    def test_unanswered_deadline_defaults_to_stop(self):
        r = self.row(today=date(2026, 4, 9))                      # one day past grace
        self.assertTrue(r["outcome"].startswith("stop (defaulted"))
        self.assertEqual(r["urgency"], "crit")

    def test_the_client_stays_committed_through_the_grace_week(self):
        # Flipping to "ended" on the term date silenced the countdown during the
        # exact week we are waiting for an answer.
        for d, want in ((date(2026, 4, 1), "committed"),    # term end
                        (date(2026, 4, 5), "committed"),    # mid-grace
                        (date(2026, 4, 8), "committed"),    # last grace day
                        (date(2026, 4, 9), "ended")):       # grace expired
            self.assertEqual(self.row(today=d)["phase"], want, d)

    def test_a_recorded_renewal_stops_the_default(self):
        r = il.build_client("Twinkle Lights Co Group", self.mbs, self.crm, {},
                            {"decision": "renew"}, self.dom, date(2026, 4, 9))
        self.assertEqual(r["outcome"], "renew")


class SharedDomainSafety(unittest.TestCase):
    def test_a_shared_domain_is_never_cancelled(self):
        mbs = [mb("2026-01-18", "sharedpool.info")]
        dom = {"sharedpool.info": {"Client A Group", "Client B Group"}}
        r = il.build_client("Client A Group", mbs, None, {}, {}, dom, date(2026, 2, 1))
        self.assertEqual(r["action"], "unpick")
        self.assertIn("sharedpool.info", r["domains"]["shared"])
        self.assertNotIn("sharedpool.info", r["domains"]["dedicated"])

    def test_mixed_estate_splits_rather_than_cancelling_everything(self):
        mbs = [mb("2026-01-18", "galaxymechanicalhq.info"),
               mb("2026-01-18", "sharedpool.info")]
        dom = {"galaxymechanicalhq.info": {"Galaxy Plumbing Inc. Group"},
               "sharedpool.info": {"Galaxy Plumbing Inc. Group", "Someone Else Group"}}
        crm = {"name": "Galaxy Plumbing Inc.", "status": "active",
               "billing_model": "retainer", "agreement_type": "month_to_month",
               "launch_date": "2026-01-01", "renewal_day": 1}
        r = il.build_client("Galaxy Plumbing Inc. Group", mbs, crm, {}, {}, dom,
                            date(2026, 2, 1))
        self.assertEqual(r["action"], "split")
        self.assertEqual(r["domains"]["dedicated"], ["galaxymechanicalhq.info"])
        self.assertEqual(r["domains"]["shared"], ["sharedpool.info"])

    def test_a_non_client_bucket_does_not_make_a_domain_shared(self):
        # Reserve/acquisition buckets sitting on a domain are ours, not a client's.
        mbs = [mb("2026-01-18", "generichq.info")]
        dom = {"generichq.info": {"Client A Group", "(generic reserve)"}}
        r = il.build_client("Client A Group", mbs, None, {}, {}, dom, date(2026, 2, 1))
        self.assertEqual(r["action"], "recycle")


class RecoverableCost(unittest.TestCase):
    """What actually stops when a client leaves — proven on 2026-09-14."""

    crm = {"name": "Acme Co", "status": "active", "billing_model": "retainer",
           "agreement_type": "prepaid", "launch_date": "2026-02-01", "prepaid_months": 3}

    def test_exclusive_domains_recover_slots_and_the_renewal(self):
        mbs = [mb("2026-01-18", "acmeco1.info", "a@acmeco1.info"),
               mb("2026-01-18", "acmeco1.info", "b@acmeco1.info"),
               mb("2026-01-18", "acmeco1.info", "c@acmeco1.info")]
        dm = {"acmeco1.info": mbs}
        r = il.build_client("Acme Co", mbs, self.crm, {}, {},
                            {"acmeco1.info": {"Acme Co"}}, date(2026, 2, 1), dm)
        self.assertEqual(r["exclusive_domains"], 1)
        self.assertEqual(r["slot_cost_yr"], 3 * 3 * 12)          # 3 mailboxes
        self.assertAlmostEqual(r["domain_cost_yr"], 22.14)        # .info renewal
        self.assertAlmostEqual(r["recoverable_yr"], 108 + 22.14)

    def test_a_shared_domain_recovers_no_renewal(self):
        mine = [mb("2026-01-18", "shared.info", "a@shared.info")]
        theirs = [mb("2026-01-18", "shared.info", "z@shared.info")]
        dm = {"shared.info": mine + theirs}
        r = il.build_client("Acme Co", mine, self.crm, {}, {},
                            {"shared.info": {"Acme Co", "Other"}}, date(2026, 2, 1), dm)
        self.assertEqual(r["exclusive_domains"], 0)
        self.assertEqual(r["domain_cost_yr"], 0)                  # domain keeps renewing
        self.assertEqual(r["recoverable_yr"], 1 * 3 * 12)          # slots only

    def test_an_admin_mailbox_on_a_shared_domain_is_stranded(self):
        # Zapmail refuses to remove the earliest-created mailbox while a sibling
        # remains. 35 mailboxes were stranded by this in a single pass.
        mine = [mb("2026-01-01", "shared.info", "admin@shared.info")]   # earliest = admin
        theirs = [mb("2026-02-01", "shared.info", "z@shared.info")]
        dm = {"shared.info": mine + theirs}
        r = il.build_client("Acme Co", mine, self.crm, {}, {},
                            {"shared.info": {"Acme Co", "Other"}}, date(2026, 2, 1), dm)
        self.assertEqual(r["admin_blocked"], 1)
        self.assertEqual(r["stranded_yr"], 36)
        self.assertIn("admin mailbox", " ".join(r["flags"]))

    def test_not_the_admin_when_a_sibling_is_older(self):
        mine = [mb("2026-03-01", "shared.info", "later@shared.info")]
        theirs = [mb("2026-01-01", "shared.info", "admin@shared.info")]
        dm = {"shared.info": theirs + mine}
        r = il.build_client("Acme Co", mine, self.crm, {}, {},
                            {"shared.info": {"Acme Co", "Other"}}, date(2026, 2, 1), dm)
        self.assertEqual(r["admin_blocked"], 0)


class Phases(unittest.TestCase):
    def test_a_rolling_client_is_not_reported_as_overdue(self):
        # Denair: launched April, still active in September. The three-month
        # mark passed and they simply kept paying — that is not a blown deadline.
        crm = {"name": "Denair Hvac, Inc.", "status": "active",
               "billing_model": "retainer", "agreement_type": "month_to_month",
               "launch_date": "2026-04-23", "renewal_day": 29}
        mbs = [mb("2026-03-28", "denairhq.info")]
        r = il.build_client("Denair Group", mbs, crm, {}, {},
                            {"denairhq.info": {"Denair Group"}}, date(2026, 9, 5))
        self.assertEqual(r["phase"], "rolling")
        self.assertEqual(r["effective_end"], "2026-09-29")
        self.assertEqual(r["outcome"], "ongoing")

    def test_rolling_clients_raise_no_countdown_notices(self):
        board = {"rows": [{"client": "X", "days_to_decision": 3, "decision": None,
                           "phase": "rolling"}]}
        self.assertEqual(il.notices(board), [])

    def test_per_lead_clients_have_no_deadline(self):
        crm = {"name": "Airlast", "status": "active", "billing_model": "per_lead",
               "agreement_type": "prepaid", "launch_date": None}
        r = il.build_client("Airlast Group", [mb("2026-01-18", "airlasthq.info")],
                            crm, {}, {}, {"airlasthq.info": {"Airlast Group"}},
                            date(2026, 9, 5))
        self.assertEqual(r["phase"], "per_lead")
        self.assertIsNone(r["decision_by"])
        self.assertEqual(r["urgency"], "")

    def test_override_sets_a_term_the_crm_cannot_store(self):
        crm = {"name": "Six Month Co", "status": "active", "billing_model": "retainer",
               "agreement_type": "month_to_month", "launch_date": "2026-01-01",
               "renewal_day": 1}
        r = il.build_client("Six Month Co Group", [mb("2026-01-01", "sixmonthco.info")],
                            crm, {"term_months": 6}, {},
                            {"sixmonthco.info": {"Six Month Co Group"}}, date(2026, 2, 1))
        self.assertEqual(r["effective_end"], "2026-07-01")
        self.assertEqual(r["end_basis"], "override term")


class LiveClientMissingFromCrm(unittest.TestCase):
    """Wonderly Lights is a paying client with no CRM row — it must not read as
    abandoned infrastructure, or the tool points at cancelling a live client."""

    mbs = [mb("2026-08-25", "deckthelights.info")]
    dom = {"deckthelights.info": {"Wonderly Lights of Birmingham Group"}}

    def test_without_the_override_it_looks_like_an_orphan(self):
        r = il.build_client("Wonderly Lights of Birmingham Group", self.mbs, None,
                            {}, {}, self.dom, date(2026, 9, 9))
        self.assertNotEqual(r["status"], "active")
        self.assertIn("nobody paying for it", " ".join(r["flags"]))

    def test_asserting_active_clears_the_orphan_reading(self):
        r = il.build_client("Wonderly Lights of Birmingham Group", self.mbs, None,
                            {"status": "active"}, {}, self.dom, date(2026, 9, 9))
        self.assertEqual(r["status"], "active")
        self.assertTrue(r["status_asserted"])
        self.assertNotIn("nobody paying for it", " ".join(r["flags"]))
        self.assertIn("no CRM row", " ".join(r["flags"]))


class MultipleCohorts(unittest.TestCase):
    def test_each_creation_date_gets_its_own_hard_stop(self):
        # Merry & Bright's real shape: inboxes added in waves, so one client
        # carries several billing anniversaries.
        mbs = ([mb("2026-07-28", "a.info")] * 8 + [mb("2026-08-22", "b.info")] * 39)
        crm = {"name": "Waves Co", "status": "active", "billing_model": "retainer",
               "agreement_type": "prepaid", "launch_date": "2026-09-04",
               "prepaid_months": 3}
        r = il.build_client("Waves Co Group", mbs, crm, {}, {},
                            {"a.info": {"Waves Co Group"}, "b.info": {"Waves Co Group"}},
                            date(2026, 9, 5))
        self.assertEqual(len(r["cohorts"]), 2)
        stops = {c["created"]: c["hard_stop"] for c in r["cohorts"]}
        self.assertEqual(stops["2026-07-28"], "2026-12-28")
        self.assertEqual(stops["2026-08-22"], "2026-12-22")
        # The client-level hard stop is the LAST one — nothing is gone before it.
        self.assertEqual(r["hard_stop"], "2026-12-28")
        # ...but the schedule deadline is driven by the FIRST one to bill.
        self.assertEqual(r["schedule_by"], "2026-12-20")


class FailLoud(unittest.TestCase):
    def test_an_empty_crm_read_raises_instead_of_reading_as_clean(self):
        import os, tempfile, json as _json
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            _json.dump([], fh)
            path = fh.name
        old = os.environ.get("INFRA_LIFECYCLE_CRM_FILE")
        os.environ["INFRA_LIFECYCLE_CRM_FILE"] = path
        try:
            # The file path short-circuits the HTTP call, so assert on the live
            # path's guard directly: an empty roster must never become a board.
            self.assertEqual(il.fetch_crm_clients(), [])
        finally:
            if old is None:
                del os.environ["INFRA_LIFECYCLE_CRM_FILE"]
            else:
                os.environ["INFRA_LIFECYCLE_CRM_FILE"] = old
            os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class UnownedInfraTests(unittest.TestCase):
    """The alarm for infrastructure with nobody accountable for it."""

    def board(self, **kw):
        b = {"orphans": [], "active_clients_without_infra": [], "pools": {}}
        b.update(kw)
        return b

    def test_client_with_no_crm_row_is_reported(self):
        # LightDMV, 2026-09-17: 57 inboxes provisioned, no CRM row, so no
        # launch date, so no term, so absent from `rows` rather than overdue.
        out = il.unowned_infra(self.board(
            orphans=[{"client": "LightDMV", "status": None, "mailboxes": 57}]))
        self.assertEqual(len(out), 1)
        self.assertIn("LightDMV", out[0])
        self.assertIn("no CRM row", out[0])

    def test_operational_buckets_are_not_clients(self):
        # These legitimately have no CRM row — they are where inboxes wait.
        # Reporting them daily would bury the one line that is a real client.
        for name in ("Cleanup 2026-09", "Replacement Group", "Generic Landscaping 2",
                     "(acquisition)", "Retired - burned", "Burnt Acquisition",
                     "Premium Inboxes", "(untagged)"):
            out = il.unowned_infra(self.board(
                orphans=[{"client": name, "status": None, "mailboxes": 431}]))
            self.assertEqual(out, [], f"{name} should not be reported as a client")

    def test_churned_client_still_holding_infra(self):
        out = il.unowned_infra(self.board(
            orphans=[{"client": "Urban Growth", "status": "inactive", "mailboxes": 3}]))
        self.assertEqual(len(out), 1)
        self.assertIn("churned", out[0])

    def test_active_client_with_no_infra(self):
        out = il.unowned_infra(self.board(active_clients_without_infra=["Landy Rose Media"]))
        self.assertEqual(len(out), 1)
        self.assertIn("no inboxes", out[0])

    def test_reserve_ceiling(self):
        under = self.board(pools={"(generic reserve)": {"mailboxes": il.RESERVE_CEILING}})
        self.assertEqual(il.unowned_infra(under), [])
        over = self.board(pools={"(generic reserve)": {"mailboxes": il.RESERVE_CEILING + 26}})
        out = il.unowned_infra(over)
        self.assertEqual(len(out), 1)
        self.assertIn("26 over", out[0])

    def test_acquisition_is_not_counted_as_reserve(self):
        # Our own prospecting is a deliberate spend, not idle stock.
        out = il.unowned_infra(self.board(pools={"(acquisition)": {"mailboxes": 277}}))
        self.assertEqual(out, [])

    def test_untagged_is_reported(self):
        out = il.unowned_infra(self.board(pools={"(untagged)": {"mailboxes": 22}}))
        self.assertEqual(len(out), 1)
        self.assertIn("no client tag", out[0])

    def test_silent_when_everything_is_owned(self):
        self.assertEqual(il.unowned_infra(self.board()), [])
        self.assertEqual(il.post_unowned(self.board(), dry_run=True)["count"], 0)

class ContractedTermTests(unittest.TestCase):
    """The CRM's own contracted term beats the guess."""

    def test_contract_term_in_months(self):
        self.assertEqual(
            il.contracted_term({}, {"initial_term_length": 2, "initial_term_unit": "months"}),
            (2, 0, "contract term (2 months)"))

    def test_contract_term_in_days(self):
        # The CRM offers a days unit; it must not be silently read as months.
        self.assertEqual(
            il.contracted_term({}, {"initial_term_length": 45, "initial_term_unit": "days"}),
            (0, 45, "contract term (45 days)"))

    def test_override_wins(self):
        self.assertEqual(
            il.contracted_term({"term_months": 6},
                               {"initial_term_length": 2, "initial_term_unit": "months"}),
            (6, 0, "override term"))

    def test_contract_term_beats_prepaid_months(self):
        # Landry's: 3 prepaid months AND a 3-month term. Same answer, but the
        # basis must name the agreement, not the payment.
        m, d, basis = il.contracted_term(
            {}, {"initial_term_length": 3, "initial_term_unit": "months", "prepaid_months": 3})
        self.assertEqual((m, d), (3, 0))
        self.assertIn("contract term", basis)

    def test_prepaid_months_when_no_contract_term(self):
        self.assertEqual(il.contracted_term({}, {"prepaid_months": 3}),
                         (3, 0, "prepaid_months"))

    def test_falls_back_to_the_guess_and_says_so(self):
        m, d, basis = il.contracted_term({}, {})
        self.assertEqual((m, d), (il.DEFAULT_TERM_MONTHS, 0))
        self.assertTrue(basis.startswith("assumed"))

    def test_blank_and_junk_values_fall_through(self):
        for bad in (None, "", "three", 0, "0"):
            m, d, basis = il.contracted_term({}, {"initial_term_length": bad})
            self.assertTrue(basis.startswith("assumed"), f"{bad!r} should not be a term")

    def test_missing_unit_defaults_to_months(self):
        self.assertEqual(il.contracted_term({}, {"initial_term_length": 2}),
                         (2, 0, "contract term (2 months)"))
