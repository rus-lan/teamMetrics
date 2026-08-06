import _pathfix  # noqa: F401

import random
import unittest

from jira_metrics import forecast


class PercentileTests(unittest.TestCase):
    """Nearest-rank (no interpolation): rank = ceil(p/100*len)-1, clamped — SPEC §8.4."""

    def setUp(self):
        self.sorted10 = list(range(1, 11))  # [1..10]

    def test_p50_of_ten_values(self):
        # rank = ceil(0.5*10)-1 = 4 -> sorted[4] = 5
        self.assertEqual(forecast.percentile(self.sorted10, 50), 5)

    def test_p85_of_ten_values(self):
        # rank = ceil(8.5)-1 = 8 -> sorted[8] = 9
        self.assertEqual(forecast.percentile(self.sorted10, 85), 9)

    def test_p95_of_ten_values(self):
        # rank = ceil(9.5)-1 = 9 -> sorted[9] = 10
        self.assertEqual(forecast.percentile(self.sorted10, 95), 10)

    def test_single_element(self):
        self.assertEqual(forecast.percentile([7], 50), 7)
        self.assertEqual(forecast.percentile([7], 95), 7)

    def test_empty_list_returns_zero(self):
        self.assertEqual(forecast.percentile([], 50), 0)

    def test_p1_clamps_to_first_element(self):
        # rank = ceil(0.01*10)-1 = 1-1 = 0 -> sorted[0] = 1
        self.assertEqual(forecast.percentile(self.sorted10, 1), 1)


class WeeklyCVTests(unittest.TestCase):
    """CV = population-stddev(week sums) / mean(week sums) * 100 over 7-day windows — SPEC §8.6."""

    def test_fewer_than_two_full_weeks_gives_zero(self):
        self.assertEqual(forecast.weekly_cv([1, 2, 3]), 0.0)
        self.assertEqual(forecast.weekly_cv([1] * 7), 0.0)  # exactly one week

    def test_zero_mean_gives_zero(self):
        self.assertEqual(forecast.weekly_cv([0] * 14), 0.0)

    def test_trailing_partial_week_is_discarded(self):
        # two full weeks (sum 3, sum 1) + 3 extra days that must not shift the windows
        hist = [1, 1, 1, 0, 0, 0, 0] + [1, 0, 0, 0, 0, 0, 0] + [9, 9, 9]
        self.assertAlmostEqual(forecast.weekly_cv(hist), 50.0)

    def test_cv_exactly_at_threshold_is_not_a_warning_boundary(self):
        # weeks [3, 1]: mean=2, |diff|/(sum)=2/4=0.5 -> cv == 50.0 exactly (boundary, not > 50)
        hist = [1, 1, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0]
        cv = forecast.weekly_cv(hist)
        self.assertAlmostEqual(cv, 50.0)
        self.assertFalse(cv > forecast.CV_WARN_THRESHOLD_PCT)

    def test_cv_above_threshold(self):
        # weeks [4, 1]: mean=2.5, |diff|/(sum)=3/5=0.6 -> cv == 60.0 > 50
        hist = [1, 1, 1, 1, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0]
        cv = forecast.weekly_cv(hist)
        self.assertAlmostEqual(cv, 60.0)
        self.assertTrue(cv > forecast.CV_WARN_THRESHOLD_PCT)


class NonZeroPointsTests(unittest.TestCase):
    def test_counts_only_positive_days(self):
        self.assertEqual(forecast.non_zero_points([0, 1, 0, 2, 0, 3]), 3)

    def test_empty_history(self):
        self.assertEqual(forecast.non_zero_points([]), 0)


class HistogramTests(unittest.TestCase):
    def test_buckets_by_distinct_day_value_no_zero_fill(self):
        outcomes = [1, 1, 2, 2, 2, 5]
        buckets = forecast.build_histogram(outcomes)
        self.assertEqual(
            [(b.days, b.count) for b in buckets],
            [(1, 2), (2, 3), (5, 1)],
        )  # note: no bucket for days=3 or 4 — missing durations get no zero-count row

    def test_empty_outcomes(self):
        self.assertEqual(forecast.build_histogram([]), [])


class ForecastEndToEndTests(unittest.TestCase):
    def _daily_hist_with_ten_nonzero_points(self):
        # 14 calendar days, 10 of them non-zero -> clears MIN_NON_ZERO_POINTS
        return [1, 2, 0, 3, 1, 2, 1, 0, 1, 2, 1, 1, 0, 0]

    def test_not_enough_data_raises(self):
        with self.assertRaises(forecast.NotEnoughDataError):
            forecast.forecast([1, 0, 0, 0, 0], sample_sprints=1, target_items=5, rng=random.Random(1))

    def test_deterministic_given_same_seed(self):
        hist = self._daily_hist_with_ten_nonzero_points()
        out1 = forecast.forecast(hist, sample_sprints=1, target_items=10, rng=random.Random(42), iterations=200)
        out2 = forecast.forecast(hist, sample_sprints=1, target_items=10, rng=random.Random(42), iterations=200)
        self.assertEqual(out1.outcomes, out2.outcomes)
        self.assertEqual(out1.percentiles, out2.percentiles)
        self.assertEqual(out1.histogram, out2.histogram)

    def test_different_seeds_need_not_match(self):
        hist = self._daily_hist_with_ten_nonzero_points()
        out1 = forecast.forecast(hist, sample_sprints=1, target_items=10, rng=random.Random(1), iterations=500)
        out2 = forecast.forecast(hist, sample_sprints=1, target_items=10, rng=random.Random(2), iterations=500)
        # not asserting inequality (could coincide), just that both run and produce valid, well-formed output
        for out in (out1, out2):
            self.assertEqual(len(out.percentiles), 3)
            self.assertEqual({p.percentile for p in out.percentiles}, {50, 85, 95})
            self.assertEqual(out.sample_days, len(hist))
            self.assertEqual(sum(b.count for b in out.histogram), 500)

    def test_target_items_zero_gives_all_zero_day_outcomes(self):
        hist = self._daily_hist_with_ten_nonzero_points()
        out = forecast.forecast(hist, sample_sprints=1, target_items=0, rng=random.Random(7), iterations=50)
        self.assertTrue(all(o == 0 for o in out.outcomes))
        self.assertEqual([p.days for p in out.percentiles], [0, 0, 0])


if __name__ == "__main__":
    unittest.main()
