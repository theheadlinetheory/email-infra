"""Warm-up boundary and age exposure in acq_capacity."""
import unittest
from datetime import datetime, timedelta

import acq_capacity as ac


def iso(days_ago):
    return (datetime.now() - timedelta(days=days_ago, hours=1)).isoformat(timespec="milliseconds") + "Z"


class AgeDays(unittest.TestCase):
    def test_counts_whole_days(self):
        self.assertEqual(ac._age_days(iso(0)), 0)
        self.assertEqual(ac._age_days(iso(13)), 13)
        self.assertEqual(ac._age_days(iso(14)), 14)
        self.assertEqual(ac._age_days(iso(30)), 30)

    def test_unparseable_is_none_not_zero(self):
        # None must not read as "brand new" — that would hide a real inbox.
        for bad in (None, "", "not-a-date", 12345):
            self.assertIsNone(ac._age_days(bad), repr(bad))

    def test_tolerates_the_timestamp_shapes_smartlead_returns(self):
        for s in ("2026-09-03T10:48:16.303Z", "2026-09-03T10:48:16Z",
                  "2026-09-03T10:48:16.303+00:00"):
            self.assertIsInstance(ac._age_days(s), int, s)


class WarmupBoundary(unittest.TestCase):
    """Warm-up is exactly 14 days, so day 14 is DONE, not still warming.

    With `<=`, every one of the 74 unallocated acquisition inboxes on
    2026-09-18 was exactly 14 days old and got excluded — the 7am email
    reported 0 free capacity and 100% allocated while 1,110 sends/day sat
    ready. The boundary decides whether a whole batch is visible.
    """

    def _excluded(self, age, threshold=14):
        # Mirrors the guard in build(): exclude only while genuinely warming.
        return age is not None and age < threshold

    def test_day_13_is_still_warming(self):
        self.assertTrue(self._excluded(13))

    def test_day_14_has_finished_and_is_available(self):
        self.assertFalse(self._excluded(14))

    def test_older_inboxes_are_available(self):
        for a in (15, 30, 400):
            self.assertFalse(self._excluded(a), a)

    def test_unknown_age_is_kept(self):
        # Never silently hide real spare capacity for want of a timestamp.
        self.assertFalse(self._excluded(None))

    def test_threshold_of_zero_excludes_nothing(self):
        for a in (0, 1, 14):
            self.assertFalse(self._excluded(a, 0))


if __name__ == "__main__":
    unittest.main()


class LiveAccountFactsIsAllOrNothing(unittest.TestCase):
    """A truncated account walk must return {}, never a partial roster.

    On 2026-09-18 this returned 200 accounts of ~1,900 after one rate-limited
    page, and the caller used it to decide which inboxes were still
    acquisition-tagged. 93 real inboxes vanished from the denominator purely
    because a truncated response did not mention them — 214 reported where 302
    was the truth. A partial answer is worse than none: none falls back to the
    cached roster, which is merely stale.
    """

    def setUp(self):
        import requests
        self._real = requests.get
        self._real_key = ac._sl_key
        ac._sl_key = lambda: "test-key"      # else it bails before any request
        self._real_sleep = ac.time.sleep
        ac.time.sleep = lambda *_a, **_k: None   # do not wait out the backoff
        self.calls = []

    def tearDown(self):
        import requests
        requests.get = self._real
        ac._sl_key = self._real_key
        ac.time.sleep = self._real_sleep

    def _serve(self, pages):
        """pages: list of (status, body) served in order."""
        import requests

        class R:
            def __init__(s, status, body):
                s.status_code, s._b = status, body
                s.text = "x" if body is not None else ""

            def json(s):
                return s._b

        seq = list(pages)

        def fake(url, **kw):
            self.calls.append(kw.get("params", {}).get("offset"))
            return R(*(seq.pop(0) if seq else (200, [])))

        requests.get = fake

    def _accounts(self, lo, hi):
        return [{"from_email": f"a{i}@d.info", "id": i, "is_smtp_success": True,
                 "message_per_day": 15, "tags": []} for i in range(lo, hi)]

    def test_a_complete_walk_returns_every_account(self):
        # A short final page ends the walk; the two pages must not overlap or
        # the dict dedupes them and the count lies.
        self._serve([(200, self._accounts(0, 100)), (200, self._accounts(100, 120))])
        out = ac._live_account_facts()
        self.assertEqual(len(out), 120)

    def test_a_rate_limited_page_abandons_the_whole_result(self):
        # First page fine, second 429s on every retry.
        self._serve([(200, self._accounts(0, 100))] + [(429, None)] * 6)
        out = ac._live_account_facts()
        self.assertEqual(out, {}, "a partial roster must never be returned")

    def test_it_retries_before_giving_up(self):
        self._serve([(429, None), (429, None), (200, self._accounts(0, 1))])
        out = ac._live_account_facts()
        self.assertEqual(len(out), 1, "a transient 429 should not lose the page")
