"""Coverage for sprint_series.py — bucketing and the aligned per-sprint
arrays (SPEC §C.10, §I)."""

import _pathfix  # noqa: F401

import unittest

from team_metrics import sprint_series

from helpers import dt


def _axis(*specs):
    """`specs`: (id, name, state, start, end, target) tuples."""
    return [
        sprint_series.AxisSprint(id=i, name=n, state=s, start=st, end=en, target=tg)
        for (i, n, s, st, en, tg) in specs
    ]


AXIS3 = _axis(
    (1, "Sprint 1", "closed", dt(2026, 1, 1), dt(2026, 1, 5), False),
    (2, "Sprint 2", "closed", dt(2026, 1, 8), dt(2026, 1, 12), True),
    (3, "Sprint 3", "active", dt(2026, 1, 15), dt(2026, 1, 19), True),
)


class BucketIndexTests(unittest.TestCase):
    def test_none_datetime_returns_none(self):
        self.assertIsNone(sprint_series.bucket_index(None, AXIS3))

    def test_inside_first_sprint(self):
        self.assertEqual(sprint_series.bucket_index(dt(2026, 1, 3), AXIS3), 0)

    def test_inside_last_sprint(self):
        self.assertEqual(sprint_series.bucket_index(dt(2026, 1, 17), AXIS3), 2)

    def test_end_of_day_boundary_included(self):
        # 23:59:59 on the sprint's own end date still counts as inside it.
        self.assertEqual(sprint_series.bucket_index(dt(2026, 1, 5, 23, 59, 59), AXIS3), 0)

    def test_just_after_end_of_day_falls_outside(self):
        self.assertIsNone(sprint_series.bucket_index(dt(2026, 1, 6, 0, 0, 1), AXIS3))

    def test_before_first_sprint_is_outside(self):
        self.assertIsNone(sprint_series.bucket_index(dt(2025, 12, 25), AXIS3))

    def test_gap_between_sprints_is_outside(self):
        self.assertIsNone(sprint_series.bucket_index(dt(2026, 1, 6, 12), AXIS3))

    def test_after_last_sprint_is_outside(self):
        self.assertIsNone(sprint_series.bucket_index(dt(2026, 2, 1), AXIS3))

    def test_tie_resolves_to_earliest_end_then_earliest_start(self):
        overlapping = _axis(
            (10, "Wide", "closed", dt(2026, 1, 1), dt(2026, 1, 20), False),
            (11, "Narrow", "closed", dt(2026, 1, 5), dt(2026, 1, 10), False),
        )
        # 2026-01-07 falls inside BOTH sprints — the narrower one (earlier end) wins.
        self.assertEqual(sprint_series.bucket_index(dt(2026, 1, 7), overlapping), 1)

    def test_tie_with_equal_end_resolves_to_earliest_start(self):
        overlapping = _axis(
            (20, "Later start", "closed", dt(2026, 1, 5), dt(2026, 1, 10), False),
            (21, "Earlier start", "closed", dt(2026, 1, 1), dt(2026, 1, 10), False),
        )
        self.assertEqual(sprint_series.bucket_index(dt(2026, 1, 7), overlapping), 1)


class ZeroTimeSprintGuardTests(unittest.TestCase):
    """A sprint with a missing startDate decodes to model.ZERO_TIME
    (year 1) -- its [start, end] window would otherwise cover essentially
    every real timestamp and, being the earliest-ending candidate in a tie,
    swallow every older record into itself."""

    def test_record_far_outside_the_zero_time_sprints_real_span_stays_outside(self):
        axis = _axis((1, "No start date", "active", dt(1, 1, 1), dt(2026, 1, 10), True))
        self.assertIsNone(sprint_series.bucket_index(dt(2000, 1, 1), axis))

    def test_a_normal_sprint_still_wins_a_tie_against_the_zero_time_sprint(self):
        axis = _axis(
            (1, "No start date", "closed", dt(1, 1, 1), dt(2026, 1, 10), False),
            (2, "Sprint 2", "closed", dt(2026, 1, 1), dt(2026, 1, 5), False),
        )
        self.assertEqual(sprint_series.bucket_index(dt(2026, 1, 3), axis), 1)


class BuildAxisTests(unittest.TestCase):
    def test_sorted_ascending_by_start_regardless_of_input_order(self):
        class _Sprint:
            def __init__(self, id_, start, end):
                self.id, self.name, self.state = id_, f"S{id_}", "closed"
                self.start_at, self.end_at = start, end

        class _Resolved:
            def __init__(self, sprint, target):
                self.sprint, self.target = sprint, target

        resolved = [
            _Resolved(_Sprint(2, dt(2026, 1, 8), dt(2026, 1, 12)), True),
            _Resolved(_Sprint(1, dt(2026, 1, 1), dt(2026, 1, 5)), False),
        ]
        axis = sprint_series.build_axis(resolved)
        self.assertEqual([a.id for a in axis], [1, 2])


class BucketTimestampFallbackTests(unittest.TestCase):
    """Per §C.10: MR -> merged_at, fallback created_at; pipeline -> created_at,
    fallback updated_at; deployment -> finished_at, fallback created_at."""

    def test_mr_falls_back_to_created_at_when_merged_at_absent(self):
        mr = {"created_at": "2026-01-03T00:00:00Z", "merged_at": None, "state": "closed"}
        buckets, outside = sprint_series._bucket_records([mr], sprint_series._mr_bucket_dt, AXIS3)
        self.assertEqual(len(buckets[0]), 1)
        self.assertEqual(outside, 0)

    def test_pipeline_falls_back_to_updated_at_when_created_at_absent(self):
        p = {"created_at": None, "updated_at": "2026-01-17T00:00:00Z", "status": "success"}
        buckets, outside = sprint_series._bucket_records([p], sprint_series._pipeline_bucket_dt, AXIS3)
        self.assertEqual(len(buckets[2]), 1)
        self.assertEqual(outside, 0)

    def test_deployment_falls_back_to_created_at_when_finished_at_absent(self):
        d = {"created_at": "2026-01-09T00:00:00Z", "finished_at": None, "status": "success"}
        buckets, outside = sprint_series._bucket_records([d], sprint_series._deployment_bucket_dt, AXIS3)
        self.assertEqual(len(buckets[1]), 1)
        self.assertEqual(outside, 0)

    def test_record_outside_every_sprint_counted_not_bucketed(self):
        mr = {"created_at": "2026-01-06T12:00:00Z", "merged_at": None, "state": "closed"}
        buckets, outside = sprint_series._bucket_records([mr], sprint_series._mr_bucket_dt, AXIS3)
        self.assertEqual(sum(len(b) for b in buckets), 0)
        self.assertEqual(outside, 1)


class BuildTeamSeriesTests(unittest.TestCase):
    def test_exactly_11_series_in_fixed_order(self):
        series, _outside = sprint_series.build_team_series(AXIS3, [], [], [], [])
        keys = [s["key"] for s in series]
        self.assertEqual(
            keys,
            [
                "throughput", "task_cycle_time", "rework", "rework_rate", "mr", "pr_cycle_time",
                "pipelines_deployments", "ci_deploy_success", "story_points_sum", "avg_estimates", "mr_weight",
            ],
        )

    def test_every_series_values_array_matches_axis_length(self):
        flows = [{"login": "a", "done_at": dt(2026, 1, 3), "cycle_time_hours": 10.0, "rework_count": 1, "story_points": 2.0, "qa_estimation": 1.0}]
        mrs = [{"author": "a", "state": "merged", "merged_at": "2026-01-03T00:00:00Z", "created_at": "2026-01-02T00:00:00Z",
                "cycle_time_hours": 5.0, "changes_count": 3, "changes_count_available": True}]
        pipelines = [{"created_at": "2026-01-03T00:00:00Z", "status": "success"}]
        deployments = [{"finished_at": "2026-01-03T00:00:00Z", "status": "success"}]
        series, _outside = sprint_series.build_team_series(AXIS3, flows, mrs, pipelines, deployments)
        for s in series:
            for sub in s["series"]:
                self.assertEqual(len(sub["values"]), len(AXIS3), s["key"])

    def test_empty_bucket_counts_are_zero_not_null(self):
        series, _outside = sprint_series.build_team_series(AXIS3, [], [], [], [])
        throughput = next(s for s in series if s["key"] == "throughput")
        self.assertEqual(throughput["series"][0]["values"], [0, 0, 0])
        rework = next(s for s in series if s["key"] == "rework")
        self.assertEqual(rework["series"][0]["values"], [0, 0, 0])

    def test_empty_bucket_averages_are_null_not_zero(self):
        series, _outside = sprint_series.build_team_series(AXIS3, [], [], [], [])
        cycle = next(s for s in series if s["key"] == "task_cycle_time")
        self.assertEqual(cycle["series"][0]["values"], [None, None, None])
        self.assertEqual(cycle["series"][1]["values"], [None, None, None])
        rework_rate = next(s for s in series if s["key"] == "rework_rate")
        self.assertEqual(rework_rate["series"][0]["values"], [None, None, None])

    def test_success_rate_null_when_bucket_has_no_rows(self):
        series, _outside = sprint_series.build_team_series(AXIS3, [], [], [], [])
        ci = next(s for s in series if s["key"] == "ci_deploy_success")
        self.assertEqual(ci["series"][0]["values"], [None, None, None])
        self.assertEqual(ci["series"][1]["values"], [None, None, None])

    def test_outside_counts_track_each_record_kind(self):
        outside_flow = {"login": "a", "done_at": dt(2025, 1, 1), "cycle_time_hours": None, "rework_count": 0, "story_points": None, "qa_estimation": None}
        outside_mr = {"author": "a", "state": "merged", "merged_at": "2025-01-01T00:00:00Z", "created_at": "2025-01-01T00:00:00Z"}
        outside_pipeline = {"created_at": "2025-01-01T00:00:00Z", "status": "success"}
        outside_deployment = {"finished_at": "2025-01-01T00:00:00Z", "status": "success"}
        _series, outside = sprint_series.build_team_series(
            AXIS3, [outside_flow], [outside_mr], [outside_pipeline], [outside_deployment]
        )
        self.assertEqual(outside, {"issues": 1, "merge_requests": 1, "pipelines": 1, "deployments": 1})

    def test_deterministic_same_input_twice_equal_output(self):
        flows = [{"login": "a", "done_at": dt(2026, 1, 3), "cycle_time_hours": 10.0, "rework_count": 1, "story_points": 2.0, "qa_estimation": 1.0}]
        out1 = sprint_series.build_team_series(AXIS3, flows, [], [], [])
        out2 = sprint_series.build_team_series(AXIS3, flows, [], [], [])
        self.assertEqual(out1, out2)


class BuildPeopleBySprintTests(unittest.TestCase):
    def test_has_data_false_when_no_flow_or_mr_in_that_sprint(self):
        rows = sprint_series.build_people_by_sprint(AXIS3, [], [])
        self.assertEqual([r["has_data"] for r in rows], [False, False, False])
        for r in rows:
            self.assertIsNone(r["throughput"])
            self.assertIsNone(r["mr_count"])

    def test_has_data_true_from_mr_alone_gives_zero_throughput_not_null(self):
        mrs = [{"author": "a", "state": "merged", "merged_at": "2026-01-03T00:00:00Z", "created_at": "2026-01-02T00:00:00Z",
                "cycle_time_hours": 4.0, "changes_count": 2, "changes_count_available": True}]
        rows = sprint_series.build_people_by_sprint(AXIS3, [], mrs)
        self.assertTrue(rows[0]["has_data"])
        self.assertEqual(rows[0]["throughput"], 0)
        self.assertEqual(rows[0]["mr_count"], 1)

    def test_rows_length_matches_axis(self):
        rows = sprint_series.build_people_by_sprint(AXIS3, [], [])
        self.assertEqual(len(rows), len(AXIS3))


class BuildPeopleSeriesTests(unittest.TestCase):
    def test_exactly_8_series_in_fixed_order(self):
        people = [{"login": "a", "display_name": "Alice", "by_sprint": [{"throughput": 1, "avg_cycle_time_hours": None,
                   "story_points_total": 0, "qa_estimation_total": 0, "rework_total": 0, "mr_count": 0,
                   "avg_mr_cycle_hours": None, "avg_mr_changes_count": None} for _ in AXIS3]}]
        series = sprint_series.build_people_series(AXIS3, people)
        keys = [s["key"] for s in series]
        self.assertEqual(
            keys,
            [
                "throughput_by_person", "task_cycle_time_by_person", "rework_by_person", "story_points_by_person",
                "qa_estimation_by_person", "mr_count_by_person", "pr_cycle_time_by_person", "mr_weight_by_person",
            ],
        )

    def test_values_pulled_from_by_sprint_in_people_order(self):
        people = [
            {"login": "a", "display_name": "A", "by_sprint": [{"throughput": 5, "avg_cycle_time_hours": None,
             "story_points_total": 0, "qa_estimation_total": 0, "rework_total": 0, "mr_count": 0,
             "avg_mr_cycle_hours": None, "avg_mr_changes_count": None} for _ in AXIS3]},
        ]
        series = sprint_series.build_people_series(AXIS3, people)
        throughput_series = next(s for s in series if s["key"] == "throughput_by_person")
        self.assertEqual(throughput_series["series"][0]["login"], "a")
        self.assertEqual(throughput_series["series"][0]["values"], [5, 5, 5])


class BuildEngineeringBySprintTests(unittest.TestCase):
    def test_rows_length_matches_axis_and_success_rate_null_when_empty(self):
        rows = sprint_series.build_engineering_by_sprint(AXIS3, [], [], [])
        self.assertEqual(len(rows), len(AXIS3))
        for r in rows:
            self.assertIsNone(r["pipeline_success_rate_pct"])
            self.assertIsNone(r["deployment_success_rate_pct"])
            self.assertEqual(r["pipeline_count"], 0)


if __name__ == "__main__":
    unittest.main()
