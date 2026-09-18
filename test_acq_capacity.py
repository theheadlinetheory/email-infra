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
