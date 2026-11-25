"""
Tests for the transformation timeline.

The specification forbids promising a result by a fixed date and requires a range whose
assumptions are visible, recalculated at reassessment from what actually happened. These
tests exist to keep it a range.
"""

import unittest

from apps.tenant_core.services_assessment_timeline import estimate_timeline


class RangeShapeTests(unittest.TestCase):

    def test_a_range_is_returned_never_a_single_date(self):
        out = estimate_timeline(goal='FAT_LOSS', current_weight_kg=80, target_weight_kg=74)
        self.assertIsNotNone(out['weeks_low'])
        self.assertIsNotNone(out['weeks_high'])
        self.assertLess(out['weeks_low'], out['weeks_high'])
        self.assertNotIn('date', out)

    def test_every_estimate_shows_its_assumptions(self):
        out = estimate_timeline(goal='FAT_LOSS', current_weight_kg=80, target_weight_kg=74)
        self.assertTrue(out['assumptions'])
        self.assertTrue(out['is_estimate'])
        self.assertIn('not a promise', out['disclosure'])

    def test_a_bigger_change_takes_longer(self):
        small = estimate_timeline(goal='FAT_LOSS', current_weight_kg=80, target_weight_kg=78)
        large = estimate_timeline(goal='FAT_LOSS', current_weight_kg=80, target_weight_kg=70)
        self.assertGreater(large['weeks_high'], small['weeks_high'])

    def test_training_more_often_shortens_the_range(self):
        rare = estimate_timeline(goal='FAT_LOSS', current_weight_kg=80, target_weight_kg=74,
                                 weekly_frequency_days=2)
        often = estimate_timeline(goal='FAT_LOSS', current_weight_kg=80, target_weight_kg=74,
                                  weekly_frequency_days=4)
        self.assertLessEqual(often['weeks_high'], rare['weeks_high'])

    def test_no_measurable_target_falls_back_to_capability_progression(self):
        out = estimate_timeline(goal='POSTURE_ALIGNMENT')
        self.assertIsNotNone(out['weeks_low'])
        self.assertIn('strength', out['driver'])

    def test_a_target_already_reached_does_not_produce_a_weight_range(self):
        out = estimate_timeline(goal='FAT_LOSS', current_weight_kg=70, target_weight_kg=74)
        self.assertIn('strength', out['driver'])

    def test_very_low_frequency_is_warned_about(self):
        out = estimate_timeline(goal='FAT_LOSS', current_weight_kg=80, target_weight_kg=74,
                                weekly_frequency_days=1)
        self.assertTrue(any('do not hold' in w for w in out['warnings']))


class RecalculationTests(unittest.TestCase):
    """At reassessment the member's own trend replaces the generic rate."""

    def test_observed_trend_overrides_the_assumed_rate(self):
        out = estimate_timeline(
            goal='FAT_LOSS', current_weight_kg=78, target_weight_kg=72,
            observed={'weeks_elapsed': 6, 'weight_change_kg': 2.0, 'remaining_kg': 6.0},
        )
        self.assertEqual(out['basis'], 'observed')
        self.assertAlmostEqual(out['observed_rate_per_week'], 0.333, places=2)

    def test_a_slower_member_gets_a_longer_range(self):
        fast = estimate_timeline(
            goal='FAT_LOSS', current_weight_kg=78, target_weight_kg=72,
            observed={'weeks_elapsed': 6, 'weight_change_kg': 3.0, 'remaining_kg': 6.0})
        slow = estimate_timeline(
            goal='FAT_LOSS', current_weight_kg=78, target_weight_kg=72,
            observed={'weeks_elapsed': 6, 'weight_change_kg': 1.0, 'remaining_kg': 6.0})
        self.assertGreater(slow['weeks_high'], fast['weeks_high'])

    def test_too_short_a_window_falls_back_and_says_so(self):
        out = estimate_timeline(
            goal='FAT_LOSS', current_weight_kg=78, target_weight_kg=72,
            observed={'weeks_elapsed': 1, 'weight_change_kg': 0.4, 'remaining_kg': 6.0})
        self.assertEqual(out['basis'], 'planned')
        self.assertTrue(any('Not enough observed change' in w for w in out['warnings']))

    def test_poor_attendance_is_surfaced(self):
        out = estimate_timeline(
            goal='FAT_LOSS', current_weight_kg=78, target_weight_kg=72,
            observed={'weeks_elapsed': 6, 'weight_change_kg': 2.0, 'remaining_kg': 6.0,
                      'sessions_attended': 8, 'sessions_planned': 18})
        self.assertEqual(out['attendance_pct'], 44)
        self.assertTrue(any('Attendance is 44%' in w for w in out['warnings']))

    def test_good_attendance_raises_no_warning(self):
        out = estimate_timeline(
            goal='FAT_LOSS', current_weight_kg=78, target_weight_kg=72,
            observed={'weeks_elapsed': 6, 'weight_change_kg': 2.0, 'remaining_kg': 6.0,
                      'sessions_attended': 17, 'sessions_planned': 18})
        self.assertFalse(any('Attendance' in w for w in out['warnings']))


if __name__ == '__main__':
    unittest.main()
