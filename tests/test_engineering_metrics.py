import _pathfix  # noqa: F401

import unittest

from jira_metrics import engineering_metrics as em


def pipeline(project="group/a", status="success", created_at="2026-01-01T00:00:00Z"):
    return {"project": project, "status": status, "created_at": created_at}


def deployment(project="group/a", status="success", created_at="2026-01-01T00:00:00Z", finished_at=None):
    return {
        "project": project,
        "status": status,
        "created_at": created_at,
        "finished_at": finished_at or created_at,
    }


def coverage(project="group/a", value=80.0):
    return {"project": project, "coverage": value}


class PipelineMetricsTests(unittest.TestCase):
    def test_success_rate_counts_only_literal_failed_status(self):
        rows = [pipeline(status="success"), pipeline(status="failed"), pipeline(status="canceled")]
        result = em.team_pipeline_metrics(rows)
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["failed"], 1)
        # (3 - 1) / 3 * 100, "canceled" counts toward the numerator like the source spec
        self.assertAlmostEqual(result["success_rate_pct"], 66.7)

    def test_zero_pipelines_yields_zero_rate_plus_warning_not_crash(self):
        result = em.team_pipeline_metrics([])
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["success_rate_pct"], 0.0)
        self.assertIn(f"{em.WARN_DIVISION_BY_ZERO}:pipeline_success_rate_pct", result["warnings"])
        self.assertIsNone(result["per_week"])
        # Zero pipelines is "genuinely none", not "not computed" (n2) — no
        # per-week-unavailable warning should fire on top of the div/0 one.
        self.assertNotIn("WARN_PER_WEEK_UNAVAILABLE", result["warnings"])

    def test_per_week_rate(self):
        rows = [
            pipeline(created_at="2026-01-01T00:00:00Z"),
            pipeline(created_at="2026-01-15T00:00:00Z"),  # 14 days = 2 weeks apart
        ]
        result = em.team_pipeline_metrics(rows)
        self.assertEqual(result["per_week"], 1.0)  # 2 pipelines / 2 weeks

    def test_single_row_has_no_span_so_per_week_is_none(self):
        result = em.team_pipeline_metrics([pipeline()])
        self.assertIsNone(result["per_week"])
        # Data exists (one pipeline) but there is no span to rate it over —
        # the ambiguous "not computed" case (n2), so it must warn.
        self.assertIn("WARN_PER_WEEK_UNAVAILABLE", result["warnings"])

    def test_per_project_rows_are_grouped_and_sorted(self):
        rows = [
            pipeline(project="group/b", status="failed"),
            pipeline(project="group/a", status="success"),
            pipeline(project="group/a", status="success"),
        ]
        result = em.team_pipeline_metrics(rows)
        projects = [p["project"] for p in result["per_project"]]
        self.assertEqual(projects, ["group/a", "group/b"])
        a_row = result["per_project"][0]
        self.assertEqual(a_row["count"], 2)
        self.assertEqual(a_row["failed"], 0)
        self.assertEqual(a_row["success_rate_pct"], 100.0)
        b_row = result["per_project"][1]
        self.assertEqual(b_row["success_rate_pct"], 0.0)


class MinWeeksFloorTests(unittest.TestCase):
    def test_per_week_uses_the_min_weeks_floor_for_a_near_instant_span(self):
        # Two pipelines a minute apart: days>0 but far below the 1/168-week
        # (one day) floor, so the floor must clamp the denominator rather
        # than blow per_week up to an unrealistic rate.
        rows = [
            pipeline(created_at="2026-01-01T00:00:00Z"),
            pipeline(created_at="2026-01-01T00:01:00Z"),
        ]
        result = em.team_pipeline_metrics(rows)
        self.assertEqual(result["per_week"], round(2 / em._MIN_WEEKS, 2))

    def test_weeks_in_span_returns_none_for_a_single_timestamp_repeated(self):
        rows = [pipeline(created_at="2026-01-01T00:00:00Z"), pipeline(created_at="2026-01-01T00:00:00Z")]
        self.assertIsNone(em._weeks_in_span(rows, "created_at"))


class DeploymentMetricsTests(unittest.TestCase):
    def test_totals_are_not_multiplied_by_employee_count(self):
        """Bug fix regression guard: the source skill's report_team.csv
        multiplied team pipeline/deployment totals by len(employees) because
        every per-employee row carried the whole team's counters and got
        summed. This module never loops per employee, so the count must
        equal len(rows) regardless of how many people exist upstream."""
        rows = [deployment(), deployment(), deployment(status="failed")]
        result = em.team_deployment_metrics(rows)
        self.assertEqual(result["count"], 3)  # not 3 * some employee count
        self.assertEqual(result["failed"], 1)

    def test_zero_deployments_yields_zero_rate_plus_warning(self):
        result = em.team_deployment_metrics([])
        self.assertEqual(result["success_rate_pct"], 0.0)
        self.assertIn(f"{em.WARN_DIVISION_BY_ZERO}:deploy_success_rate_pct", result["warnings"])

    def test_per_week_is_bucketed_on_finished_at_not_created_at(self):
        # M5: created_at is identical (no span) but finished_at spans 2
        # weeks — per_week must be computed off finished_at.
        rows = [
            deployment(created_at="2026-01-01T00:00:00Z", finished_at="2026-01-01T00:00:00Z"),
            deployment(created_at="2026-01-01T00:00:00Z", finished_at="2026-01-15T00:00:00Z"),
        ]
        result = em.team_deployment_metrics(rows)
        self.assertEqual(result["per_week"], 1.0)  # 2 deployments / 2 weeks


class CoverageMetricsTests(unittest.TestCase):
    def test_average_coverage_stays_in_0_100_range(self):
        rows = [coverage(value=60.0), coverage(value=80.0)]
        result = em.team_coverage_metrics(rows)
        self.assertEqual(result["coverage_avg_pct"], 70.0)
        self.assertEqual(result["sample_count"], 2)

    def test_no_rows_yields_none_not_zero(self):
        result = em.team_coverage_metrics([])
        self.assertIsNone(result["coverage_avg_pct"])
        self.assertEqual(result["sample_count"], 0)

    def test_rows_present_but_no_usable_value_warns(self):
        result = em.team_coverage_metrics([{"project": "group/a", "coverage": None}])
        self.assertIsNone(result["coverage_avg_pct"])
        self.assertIn("WARN_COVERAGE_UNAVAILABLE", result["warnings"])

    def test_per_project_breakdown(self):
        rows = [coverage(project="group/a", value=90.0), coverage(project="group/b", value=50.0)]
        result = em.team_coverage_metrics(rows)
        by_project = {p["project"]: p["coverage_avg_pct"] for p in result["per_project"]}
        self.assertEqual(by_project, {"group/a": 90.0, "group/b": 50.0})


class BuildEngineeringMetricsTests(unittest.TestCase):
    def test_window_applied_flag_is_passed_through_unchanged(self):
        flag = {"pipelines": True, "deployments": True, "coverage": True, "merge_requests": True}
        result = em.build_engineering_metrics(pipelines=[], deployments=[], coverage=[], window_applied=flag)
        self.assertEqual(result["window_applied"], flag)

    def test_missing_window_applied_defaults_to_empty_dict_not_crash(self):
        result = em.build_engineering_metrics(pipelines=[], deployments=[], coverage=[])
        self.assertEqual(result["window_applied"], {})

    def test_combines_all_three_sections(self):
        result = em.build_engineering_metrics(
            pipelines=[pipeline()], deployments=[deployment()], coverage=[coverage()]
        )
        self.assertIn("pipelines", result)
        self.assertIn("deployments", result)
        self.assertIn("coverage", result)


if __name__ == "__main__":
    unittest.main()
