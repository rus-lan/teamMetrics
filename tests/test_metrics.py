import _pathfix  # noqa: F401

import unittest

from team_metrics import metrics, model

from helpers import dt, make_cfg, make_issue, make_timeline


class RatioPctTests(unittest.TestCase):
    def test_zero_denominator_yields_zero_and_warning(self):
        value, warn = metrics.ratio_pct(10.0, 0.0)
        self.assertEqual(value, 0.0)
        self.assertEqual(warn, model.WARN_DIVISION_BY_ZERO)

    def test_negative_denominator_also_yields_zero_and_warning(self):
        value, warn = metrics.ratio_pct(10.0, -3.0)
        self.assertEqual(value, 0.0)
        self.assertEqual(warn, model.WARN_DIVISION_BY_ZERO)

    def test_normal_division(self):
        value, warn = metrics.ratio_pct(5.0, 20.0)
        self.assertEqual(value, 25.0)
        self.assertEqual(warn, "")

    def test_never_raises_or_produces_nan(self):
        for num, den in [(0.0, 0.0), (-5.0, 0.0), (1e300, 0.0)]:
            value, warn = metrics.ratio_pct(num, den)
            self.assertEqual(value, 0.0)
            self.assertFalse(value != value)  # not NaN


class SMA5Tests(unittest.TestCase):
    def test_empty_history_yields_zero_and_division_warning(self):
        avg, warn = metrics.sma5([])
        self.assertEqual(avg, 0.0)
        self.assertEqual(warn, model.WARN_DIVISION_BY_ZERO)

    def test_fewer_than_5_sprints_yields_average_plus_baseline_short(self):
        avg, warn = metrics.sma5([10.0, 20.0, 30.0])
        self.assertEqual(avg, 20.0)
        self.assertEqual(warn, model.WARN_BASELINE_SHORT)

    def test_exactly_5_sprints_no_warning(self):
        avg, warn = metrics.sma5([10.0, 20.0, 30.0, 40.0, 50.0])
        self.assertEqual(avg, 30.0)
        self.assertEqual(warn, "")


class ComputeMetricsDivisionByZeroTests(unittest.TestCase):
    def test_no_committed_issues_zeroes_percentage_metrics_with_warnings(self):
        issues = [
            model.Issue(
                key="A", epic_key="", story_points=3.0, qa_estimation=0, role="", labels=[], assignee="",
                membership_intervals=[], day_statuses=[], status_initial="", status_before_end="", status_end="",
                committed=False, added=True, removed=False, delivered=False,
            )
        ]
        m, warns = metrics.compute_metrics(issues, 0.0, [], [], is_active=False)
        self.assertEqual(m.committed_sp, 0.0)
        self.assertEqual(m.performance_pct, 0.0)
        self.assertEqual(m.load_pct, 0.0)
        self.assertEqual(m.scope_change_pct, 0.0)
        self.assertIn(model.WARN_DIVISION_BY_ZERO, warns)
        # SMA5([]) also contributes WARN_DIVISION_BY_ZERO -> still deduped to one entry
        self.assertEqual(warns.count(model.WARN_DIVISION_BY_ZERO), 1)


class BuildPayloadClassificationTests(unittest.TestCase):
    """End-to-end: classify + assemble a tiny sprint payload and check sets/metrics."""

    def setUp(self):
        self.tl = make_timeline(dt(2026, 1, 5), dt(2026, 1, 9))  # one working week, Mon-Fri
        self.cfg = make_cfg(jira_status_categories={"To Do": "new", "In Progress": "indeterminate", "Done": "done"})

    def _sprint_input(self, state="closed"):
        return metrics.SprintInput(
            id=1, name="Sprint 1", board_id=42, board_name="Board", state=state, timeline=self.tl, complete_at=dt(2026, 1, 9)
        )

    def test_committed_added_removed_delivered_sets(self):
        committed_delivered = make_issue(
            "PROJ-1",
            story_points=5.0,
            status_history=[model.StatusChange(at=dt(2026, 1, 6), from_status="To Do", to_status="Done")],
        )
        added_issue = make_issue("PROJ-2", story_points=2.0)
        removed_issue = make_issue("PROJ-3", story_points=3.0)

        issues = [
            metrics.SprintIssue(committed_delivered, [model.Interval(from_=dt(2026, 1, 1), until=None)]),
            metrics.SprintIssue(added_issue, [model.Interval(from_=dt(2026, 1, 7), until=None)]),
            metrics.SprintIssue(removed_issue, [model.Interval(from_=dt(2026, 1, 1), until=dt(2026, 1, 7))]),
        ]

        payload, warnings = metrics.build_payload(self._sprint_input(), issues, self.cfg, velocity_history=[10, 10, 10, 10, 10])

        # Sets preserve INPUT order (model.build_sets iterates `issues` as
        # given, Go's sets.go:101-114 does the same) — assert the exact list,
        # not a set, so an accidental reorder would be caught.
        self.assertEqual(payload.sets.committed_keys, ["PROJ-1", "PROJ-3"])
        self.assertEqual(payload.sets.added_keys, ["PROJ-2"])
        self.assertEqual(payload.sets.removed_keys, ["PROJ-3"])
        self.assertEqual(payload.sets.delivered_keys, ["PROJ-1"])
        self.assertEqual(payload.metrics.committed_sp, 8.0)  # PROJ-1 + PROJ-3
        self.assertEqual(payload.metrics.delivered_sp, 5.0)
        self.assertEqual(payload.metrics.scope_added_sp, 2.0)
        self.assertEqual(payload.metrics.scope_removed_sp, 3.0)
        self.assertNotIn(model.WARN_DIVISION_BY_ZERO, warnings)


class ThroughputDayAttributionTests(unittest.TestCase):
    """_throughput_day_for/build_throughput_daily — SPEC §4.2."""

    def setUp(self):
        self.cfg = make_cfg(jira_status_categories={"In Progress": "indeterminate", "Done": "done"})

    def _issue(self, *, status_initial, day_statuses, delivered=True):
        return model.Issue(
            key="PROJ-1", epic_key="", story_points=1.0, qa_estimation=0.0, role="", labels=[], assignee="",
            membership_intervals=[], day_statuses=day_statuses,
            status_initial=status_initial, status_before_end="", status_end="",
            committed=True, added=False, removed=False, delivered=delivered,
        )

    def test_already_done_on_entry_contributes_nothing(self):
        # status_initial is already "Done" -> prev_done starts True, so no
        # day ever crosses not-done -> done inside the sprint.
        issue = self._issue(
            status_initial="Done",
            day_statuses=[
                model.DayStatus(date="2026-01-05", status="Done", status_category="done"),
                model.DayStatus(date="2026-01-06", status="Done", status_category="done"),
            ],
        )
        date, ok = metrics._throughput_day_for(issue, self.cfg)
        self.assertFalse(ok)
        self.assertEqual(date, "")

    def test_last_transition_into_done_wins_over_first(self):
        # Enters done on day 2, reopens on day 3, re-enters done on day 4 —
        # throughput must attribute to day 4, the LAST entry, not day 2.
        issue = self._issue(
            status_initial="In Progress",
            day_statuses=[
                model.DayStatus(date="2026-01-05", status="In Progress", status_category="indeterminate"),
                model.DayStatus(date="2026-01-06", status="Done", status_category="done"),
                model.DayStatus(date="2026-01-07", status="In Progress", status_category="indeterminate"),
                model.DayStatus(date="2026-01-08", status="Done", status_category="done"),
            ],
        )
        date, ok = metrics._throughput_day_for(issue, self.cfg)
        self.assertTrue(ok)
        self.assertEqual(date, "2026-01-08")

    def test_not_delivered_never_attributed(self):
        issue = self._issue(
            status_initial="In Progress",
            day_statuses=[model.DayStatus(date="2026-01-05", status="Done", status_category="done")],
            delivered=False,
        )
        date, ok = metrics._throughput_day_for(issue, self.cfg)
        self.assertFalse(ok)

    def test_build_throughput_daily_groups_by_date_ascending_and_counts(self):
        same_day_a = self._issue(
            status_initial="In Progress",
            day_statuses=[model.DayStatus(date="2026-01-06", status="Done", status_category="done")],
        )
        same_day_b = self._issue(
            status_initial="In Progress",
            day_statuses=[model.DayStatus(date="2026-01-06", status="Done", status_category="done")],
        )
        earlier_day = self._issue(
            status_initial="In Progress",
            day_statuses=[model.DayStatus(date="2026-01-05", status="Done", status_category="done")],
        )
        not_delivered = self._issue(
            status_initial="In Progress",
            day_statuses=[model.DayStatus(date="2026-01-07", status="Done", status_category="done")],
            delivered=False,
        )
        out = metrics.build_throughput_daily([same_day_a, same_day_b, earlier_day, not_delivered], self.cfg)
        self.assertEqual([(d.date, d.count) for d in out], [("2026-01-05", 1), ("2026-01-06", 2)])


class BuildKpiTests(unittest.TestCase):
    """build_kpi — board KPI over base (closed, non-target) sprints only (SPEC §7)."""

    def _payload(self, *, performance_pct, scope_change_pct, closure_items, closure_sp, throughput_items, daily_counts):
        m = metrics.Metrics(
            performance_pct=performance_pct, scope_change_pct=scope_change_pct,
            closure_pct_items=closure_items, closure_pct_sp=closure_sp, throughput_items=throughput_items,
        )
        sprint_meta = metrics.SprintMeta(
            id=1, name="S", board_id=1, board_name="B", state="closed",
            start_at=dt(2026, 1, 1), end_at=dt(2026, 1, 5), complete_at=dt(2026, 1, 5), working_days=[],
        )
        throughput_daily = [metrics.ThroughputDay(date=d, count=c) for d, c in daily_counts]
        return metrics.Payload(
            schema_version=1, sprint=sprint_meta, issues=[], sets=model.Sets(), metrics=m, throughput_daily=throughput_daily
        )

    def test_averages_and_sma5_over_base_sprints(self):
        payloads = [
            self._payload(
                performance_pct=80.0, scope_change_pct=10.0, closure_items=50.0, closure_sp=60.0,
                throughput_items=3, daily_counts=[("2026-01-05", 2), ("2026-01-06", 1)],
            ),
            self._payload(
                performance_pct=100.0, scope_change_pct=20.0, closure_items=100.0, closure_sp=90.0,
                throughput_items=5, daily_counts=[("2026-01-12", 5)],
            ),
        ]
        velocities = [10.0, 20.0, 30.0, 40.0, 50.0]  # exactly 5 -> no WARN_BASELINE_SHORT
        kpi, warnings = metrics.build_kpi(payloads, velocities, min_non_zero_points=2)

        self.assertEqual(kpi.velocity_sma5_sp, 30.0)
        self.assertEqual(kpi.avg_performance_pct, 90.0)
        self.assertEqual(kpi.avg_scope_change_pct, 15.0)
        self.assertEqual(kpi.avg_closure_pct_items, 75.0)
        self.assertEqual(kpi.avg_closure_pct_sp, 75.0)
        self.assertEqual(kpi.throughput_avg_items, 4.0)
        self.assertEqual(warnings, [])
        self.assertTrue(kpi.forecast_available)  # 3 non-zero daily points >= 2

    def test_empty_base_sprints_zeroes_out_with_division_warning(self):
        kpi, warnings = metrics.build_kpi([], [], min_non_zero_points=10)
        self.assertEqual(kpi.velocity_sma5_sp, 0.0)
        self.assertEqual(kpi.avg_performance_pct, 0.0)
        self.assertEqual(kpi.throughput_avg_items, 0.0)
        self.assertFalse(kpi.forecast_available)
        self.assertIn(model.WARN_DIVISION_BY_ZERO, warnings)

    def test_forecast_available_is_a_strict_threshold(self):
        below = self._payload(
            performance_pct=0.0, scope_change_pct=0.0, closure_items=0.0, closure_sp=0.0,
            throughput_items=1, daily_counts=[("2026-01-05", 1)],
        )
        kpi, _ = metrics.build_kpi([below], [10.0], min_non_zero_points=2)
        self.assertFalse(kpi.forecast_available)  # only 1 non-zero point < 2

        at_threshold = self._payload(
            performance_pct=0.0, scope_change_pct=0.0, closure_items=0.0, closure_sp=0.0,
            throughput_items=2, daily_counts=[("2026-01-05", 1), ("2026-01-06", 1)],
        )
        kpi, _ = metrics.build_kpi([at_threshold], [10.0], min_non_zero_points=2)
        self.assertTrue(kpi.forecast_available)  # exactly 2 non-zero points >= 2


if __name__ == "__main__":
    unittest.main()
