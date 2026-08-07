import _pathfix  # noqa: F401

import random
import unittest

from team_metrics import forecast


class PercentileTests(unittest.TestCase):
    """Nearest-rank (no interpolation): rank = ceil(p/100*len)-1, clamped."""

    def setUp(self):
        self.sorted10 = [float(x) for x in range(1, 11)]  # [1.0..10.0]

    def test_p50_of_ten_values(self):
        # rank = ceil(0.5*10)-1 = 4 -> sorted[4] = 5.0
        self.assertEqual(forecast.percentile(self.sorted10, 50), 5.0)

    def test_p85_of_ten_values(self):
        # rank = ceil(8.5)-1 = 8 -> sorted[8] = 9.0
        self.assertEqual(forecast.percentile(self.sorted10, 85), 9.0)

    def test_p95_of_ten_values(self):
        # rank = ceil(9.5)-1 = 9 -> sorted[9] = 10.0
        self.assertEqual(forecast.percentile(self.sorted10, 95), 10.0)

    def test_single_element(self):
        self.assertEqual(forecast.percentile([7.0], 50), 7.0)
        self.assertEqual(forecast.percentile([7.0], 95), 7.0)

    def test_empty_list_returns_zero(self):
        self.assertEqual(forecast.percentile([], 50), 0)

    def test_p1_clamps_to_first_element(self):
        # rank = ceil(0.01*10)-1 = 1-1 = 0 -> sorted[0] = 1.0
        self.assertEqual(forecast.percentile(self.sorted10, 1), 1.0)


class SpSeriesCvTests(unittest.TestCase):
    """Population-variance CV over the raw per-sprint SP series (not a
    weekly-binned daily series — the SP forecast operates at sprint
    granularity)."""

    def test_fewer_than_two_values_is_none(self):
        self.assertIsNone(forecast.sp_series_cv_pct([]))
        self.assertIsNone(forecast.sp_series_cv_pct([10.0]))

    def test_zero_mean_is_none(self):
        self.assertIsNone(forecast.sp_series_cv_pct([0.0, 0.0, 0.0]))

    def test_identical_values_give_zero_cv(self):
        self.assertEqual(forecast.sp_series_cv_pct([20.0, 20.0, 20.0]), 0.0)

    def test_known_cv_value(self):
        # values [10, 30]: mean=20, population stddev=10 -> cv = 10/20*100 = 50.0
        self.assertAlmostEqual(forecast.sp_series_cv_pct([10.0, 30.0]), 50.0)

    def test_cv_above_threshold(self):
        # values [5, 35]: mean=20, stddev=15 -> cv = 75.0 > 50
        cv = forecast.sp_series_cv_pct([5.0, 35.0])
        self.assertAlmostEqual(cv, 75.0)
        self.assertTrue(cv > forecast.CV_WARN_THRESHOLD_PCT)


class HistogramTests(unittest.TestCase):
    def test_buckets_by_distinct_sp_value_no_zero_fill(self):
        outcomes = [10.0, 10.0, 20.0, 20.0, 20.0, 50.0]
        buckets = forecast.build_histogram(outcomes)
        self.assertEqual(
            [(b.sp, b.count) for b in buckets],
            [(10.0, 2), (20.0, 3), (50.0, 1)],
        )

    def test_empty_outcomes(self):
        self.assertEqual(forecast.build_histogram([]), [])


class ForecastSpTests(unittest.TestCase):
    def _history(self):
        return [20.0, 24.0, 18.0, 30.0, 22.0]

    def test_not_enough_data_raises(self):
        with self.assertRaises(forecast.NotEnoughDataError):
            forecast.forecast_sp([10.0, 20.0], rng=random.Random(1))

    def test_minimum_sprints_boundary_succeeds(self):
        out = forecast.forecast_sp([10.0, 20.0, 30.0], rng=random.Random(1))
        self.assertEqual(out.sample_sprints, 3)

    def test_deterministic_given_same_seed(self):
        hist = self._history()
        out1 = forecast.forecast_sp(hist, rng=random.Random(42), iterations=200)
        out2 = forecast.forecast_sp(hist, rng=random.Random(42), iterations=200)
        self.assertEqual(out1.percentiles, out2.percentiles)
        self.assertEqual(out1.histogram, out2.histogram)
        self.assertEqual(out1.mean_sp, out2.mean_sp)

    def test_different_seeds_need_not_match(self):
        hist = self._history()
        out1 = forecast.forecast_sp(hist, rng=random.Random(1), iterations=500)
        out2 = forecast.forecast_sp(hist, rng=random.Random(2), iterations=500)
        for out in (out1, out2):
            self.assertEqual(len(out.percentiles), 3)
            self.assertEqual([p.p for p in out.percentiles], [50, 85, 95])
            self.assertEqual(out.sample_sprints, len(hist))
            self.assertEqual(sum(b.count for b in out.histogram), 500)

    def test_percentiles_only_ever_take_historical_values(self):
        hist = self._history()
        out = forecast.forecast_sp(hist, rng=random.Random(7), iterations=1000)
        for pc in out.percentiles:
            self.assertIn(pc.sp, hist)

    def test_percentile_labels_are_qualitative_not_mechanical(self):
        hist = self._history()
        out = forecast.forecast_sp(hist, rng=random.Random(7), iterations=50)
        labels = {pc.p: pc.label_ru for pc in out.percentiles}
        self.assertEqual(labels[50], "50% прогонов уложились")
        self.assertEqual(labels[85], "рабочее обещание")
        self.assertEqual(labels[95], "безопасный внешний срок")

    def test_mean_sp_is_the_historical_mean_not_the_resampled_one(self):
        hist = [10.0, 20.0, 30.0]
        out = forecast.forecast_sp(hist, rng=random.Random(3), iterations=50)
        self.assertEqual(out.mean_sp, 20.0)

    def test_cv_warning_set_when_series_unstable(self):
        hist = [5.0, 35.0, 5.0]  # highly variable
        out = forecast.forecast_sp(hist, rng=random.Random(3), iterations=50)
        self.assertIn(forecast.WARN_THROUGHPUT_UNSTABLE, out.warnings)

    def test_no_cv_warning_when_series_stable(self):
        hist = [20.0, 21.0, 19.0, 20.0]
        out = forecast.forecast_sp(hist, rng=random.Random(3), iterations=50)
        self.assertEqual(out.warnings, [])


class WeeklyCVTests(unittest.TestCase):
    """CV = population-stddev(week sums) / mean(week sums) * 100 over 7-day
    windows — the item-throughput stability signal kept for
    report_data.py's recommendations block, independent of the SP forecast."""

    def test_fewer_than_two_full_weeks_gives_zero(self):
        self.assertEqual(forecast.weekly_cv([1, 2, 3]), 0.0)
        self.assertEqual(forecast.weekly_cv([1] * 7), 0.0)  # exactly one week

    def test_zero_mean_gives_zero(self):
        self.assertEqual(forecast.weekly_cv([0] * 14), 0.0)

    def test_trailing_partial_week_is_discarded(self):
        hist = [1, 1, 1, 0, 0, 0, 0] + [1, 0, 0, 0, 0, 0, 0] + [9, 9, 9]
        self.assertAlmostEqual(forecast.weekly_cv(hist), 50.0)

    def test_cv_exactly_at_threshold_is_not_a_warning_boundary(self):
        hist = [1, 1, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0]
        cv = forecast.weekly_cv(hist)
        self.assertAlmostEqual(cv, 50.0)
        self.assertFalse(cv > forecast.CV_WARN_THRESHOLD_PCT)

    def test_cv_above_threshold(self):
        hist = [1, 1, 1, 1, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0]
        cv = forecast.weekly_cv(hist)
        self.assertAlmostEqual(cv, 60.0)
        self.assertTrue(cv > forecast.CV_WARN_THRESHOLD_PCT)


if __name__ == "__main__":
    unittest.main()
