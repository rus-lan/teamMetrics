import _pathfix  # noqa: F401

import unittest
from datetime import datetime, timedelta, timezone

from team_metrics import personal_metrics as pm

UTC = timezone.utc


def dt(y, m, d, h=0, mi=0, s=0):
    return datetime(y, m, d, h, mi, s, tzinfo=UTC)


def issue(
    key,
    assignee="alice",
    issuetype="Story",
    story_points=None,
    qa_estimation=None,
    events=None,
    resolutiondate=None,
):
    return pm.JiraIssueInput(
        key=key,
        issuetype=issuetype,
        assignee=assignee,
        story_points=story_points,
        qa_estimation=qa_estimation,
        status_events=events or [],
        resolutiondate=resolutiondate,
    )


def ev(at, to_status):
    return pm.StatusEvent(at=at, to_status=to_status)


def mr(author="alice", state="merged", **overrides):
    base = {
        "author": author,
        "state": state,
        "created_at": "2026-01-01T00:00:00Z",
        "merged_at": "2026-01-02T00:00:00Z",
        "cycle_time_hours": 24.0,
        "additions": 10,
        "deletions": 2,
        "diff_stats_available": True,
        "commits_count": 3,
        "commits_count_available": True,
        "changes_count": 4,
        "changes_count_available": True,
        "jira_key": "",
    }
    base.update(overrides)
    return base


class AvgMedianTests(unittest.TestCase):
    def test_average_odd_sample(self):
        self.assertEqual(pm._avg([1, 2, 3]), 2.0)

    def test_average_even_sample(self):
        self.assertEqual(pm._avg([1, 2, 3, 4]), 2.5)

    def test_median_odd_sample(self):
        self.assertEqual(pm._median([5, 1, 3]), 3.0)

    def test_median_even_sample(self):
        self.assertEqual(pm._median([1, 2, 3, 4]), 2.5)

    def test_none_values_are_ignored(self):
        self.assertEqual(pm._avg([1, None, 3]), 2.0)
        self.assertEqual(pm._median([1, None, 3]), 2.0)

    def test_all_none_or_empty_yields_none(self):
        self.assertIsNone(pm._avg([]))
        self.assertIsNone(pm._avg([None, None]))
        self.assertIsNone(pm._median([]))


class CycleTimeTests(unittest.TestCase):
    """First-vs-repeat transition rule: cycle time uses the FIRST entry into
    a start-work status and the FIRST entry into a final status — later
    repeat entries must never move either boundary."""

    def test_basic_cycle_time(self):
        i = issue(
            "PROJ-1",
            events=[
                ev(dt(2026, 1, 1, 9), "In Progress"),
                ev(dt(2026, 1, 3, 9), "To Test"),
            ],
        )
        flow = pm.compute_issue_flow(i)
        self.assertEqual(flow.first_in_progress, dt(2026, 1, 1, 9))
        self.assertEqual(flow.done_at, dt(2026, 1, 3, 9))
        self.assertEqual(flow.cycle_time_hours, 48.0)
        self.assertTrue(flow.is_done)

    def test_repeat_in_progress_does_not_move_the_start_boundary(self):
        i = issue(
            "PROJ-1",
            events=[
                ev(dt(2026, 1, 1, 0), "In Progress"),
                ev(dt(2026, 1, 1, 6), "Code Review"),
                ev(dt(2026, 1, 2, 0), "In Progress"),  # repeat entry
                ev(dt(2026, 1, 3, 0), "Done"),
            ],
        )
        flow = pm.compute_issue_flow(i)
        self.assertEqual(flow.first_in_progress, dt(2026, 1, 1, 0))  # not the repeat at Jan 2
        self.assertEqual(flow.cycle_time_hours, 48.0)  # Jan 1 00:00 -> Jan 3 00:00

    def test_unfinished_issue_has_no_done_at_and_is_excluded(self):
        i = issue("PROJ-1", events=[ev(dt(2026, 1, 1), "In Progress")])
        flow = pm.compute_issue_flow(i)
        self.assertIsNone(flow.done_at)
        self.assertIsNone(flow.cycle_time_hours)
        self.assertFalse(flow.is_done)

    def test_events_out_of_order_are_sorted_before_use(self):
        i = issue(
            "PROJ-1",
            events=[
                ev(dt(2026, 1, 3), "Done"),
                ev(dt(2026, 1, 1), "In Progress"),
            ],
        )
        flow = pm.compute_issue_flow(i)
        self.assertEqual(flow.cycle_time_hours, 48.0)

    def test_localized_start_work_status_names(self):
        i = issue("PROJ-1", events=[ev(dt(2026, 1, 1), "В работе"), ev(dt(2026, 1, 2), "Closed")])
        flow = pm.compute_issue_flow(i)
        self.assertEqual(flow.cycle_time_hours, 24.0)


class ResolutiondateFallbackTests(unittest.TestCase):
    """M1: an issue with no changelog-tracked transition into a final status
    still counts as done when it carries resolutiondate."""

    def test_resolutiondate_used_when_no_final_status_transition(self):
        i = issue("PROJ-1", events=[ev(dt(2026, 1, 1), "In Progress")], resolutiondate=dt(2026, 1, 5))
        flow = pm.compute_issue_flow(i)
        self.assertEqual(flow.done_at, dt(2026, 1, 5))
        self.assertTrue(flow.is_done)
        # Cycle time stays None: there is no transition into a final status,
        # even though resolutiondate makes the issue count as done.
        self.assertIsNone(flow.cycle_time_hours)

    def test_transition_done_at_wins_over_resolutiondate_when_both_exist(self):
        i = issue(
            "PROJ-1",
            events=[ev(dt(2026, 1, 1), "In Progress"), ev(dt(2026, 1, 3), "Done")],
            resolutiondate=dt(2026, 1, 10),  # later than the transition; must not override it
        )
        flow = pm.compute_issue_flow(i)
        self.assertEqual(flow.done_at, dt(2026, 1, 3))
        self.assertEqual(flow.cycle_time_hours, 48.0)

    def test_no_resolutiondate_and_no_transition_stays_unfinished(self):
        i = issue("PROJ-1", events=[ev(dt(2026, 1, 1), "In Progress")])
        flow = pm.compute_issue_flow(i)
        self.assertIsNone(flow.done_at)
        self.assertFalse(flow.is_done)

    def test_resolutiondate_counted_task_shows_up_in_tasks_done(self):
        issues = [issue("A", story_points=5, events=[ev(dt(2026, 1, 1), "In Progress")], resolutiondate=dt(2026, 1, 5))]
        result = pm.personal_metrics("alice", mrs=[], issues=issues)
        self.assertEqual(result["tasks_done"], 1)
        self.assertEqual(result["story_points_total"], 5.0)


class ReworkCountTests(unittest.TestCase):
    def test_no_rework_on_a_clean_run(self):
        i = issue("PROJ-1", events=[ev(dt(2026, 1, 1), "In Progress"), ev(dt(2026, 1, 2), "Done")])
        self.assertEqual(pm.compute_issue_flow(i).rework_count, 0)

    def test_repeat_entry_into_start_work_counts_once(self):
        i = issue(
            "PROJ-1",
            events=[
                ev(dt(2026, 1, 1), "In Progress"),
                ev(dt(2026, 1, 2), "Code Review"),
                ev(dt(2026, 1, 3), "In Progress"),  # 2nd entry -> +1
                ev(dt(2026, 1, 4), "Done"),
            ],
        )
        self.assertEqual(pm.compute_issue_flow(i).rework_count, 1)

    def test_every_reopened_entry_counts(self):
        i = issue(
            "PROJ-1",
            events=[
                ev(dt(2026, 1, 1), "In Progress"),
                ev(dt(2026, 1, 2), "Done"),
                ev(dt(2026, 1, 3), "Reopened"),
                ev(dt(2026, 1, 4), "In Progress"),  # repeat -> +1
                ev(dt(2026, 1, 5), "Done"),
                ev(dt(2026, 1, 6), "Reopened"),  # +1
            ],
        )
        self.assertEqual(pm.compute_issue_flow(i).rework_count, 3)  # 1 repeat + 2 reopens

    def test_third_and_later_repeats_each_count(self):
        i = issue(
            "PROJ-1",
            events=[
                ev(dt(2026, 1, 1), "In Progress"),
                ev(dt(2026, 1, 2), "In Progress"),
                ev(dt(2026, 1, 3), "In Progress"),
            ],
        )
        self.assertEqual(pm.compute_issue_flow(i).rework_count, 2)


class DefectRateTests(unittest.TestCase):
    def test_share_of_bug_among_done_tasks(self):
        issues = [
            issue("A", issuetype="Bug", events=[ev(dt(2026, 1, 1), "In Progress"), ev(dt(2026, 1, 2), "Done")]),
            issue("B", issuetype="Story", events=[ev(dt(2026, 1, 1), "In Progress"), ev(dt(2026, 1, 2), "Done")]),
            issue("C", issuetype="Story", events=[ev(dt(2026, 1, 1), "In Progress"), ev(dt(2026, 1, 2), "Done")]),
            issue("D", issuetype="Story", events=[ev(dt(2026, 1, 1), "In Progress")]),  # unfinished, excluded
        ]
        result = pm.personal_metrics("alice", mrs=[], issues=issues)
        self.assertEqual(result["tasks_done"], 3)
        self.assertEqual(result["bug_count"], 1)
        self.assertAlmostEqual(result["defect_rate_pct"], 33.3)

    def test_zero_done_tasks_yields_zero_and_warning_not_crash(self):
        issues = [issue("A", events=[ev(dt(2026, 1, 1), "In Progress")])]  # never finished
        result = pm.personal_metrics("alice", mrs=[], issues=issues)
        self.assertEqual(result["tasks_done"], 0)
        self.assertEqual(result["defect_rate_pct"], 0.0)
        self.assertIn(f"{pm.WARN_DIVISION_BY_ZERO}:defect_rate_pct", result["warnings"])


class NonMatchingAssigneeTests(unittest.TestCase):
    def test_issues_for_other_assignees_are_excluded(self):
        issues = [issue("A", assignee="bob", events=[ev(dt(2026, 1, 1), "In Progress"), ev(dt(2026, 1, 2), "Done")])]
        result = pm.personal_metrics("alice", mrs=[], issues=issues)
        self.assertEqual(result["tasks_done"], 0)
        self.assertEqual(result["issue_count"], 0)


class MergeRateZeroMRsTests(unittest.TestCase):
    def test_zero_mrs_yields_zero_rate_plus_warning(self):
        result = pm.personal_metrics("alice", mrs=[], issues=[])
        self.assertEqual(result["mr_count"], 0)
        self.assertEqual(result["mr_merge_rate_pct"], 0.0)
        self.assertIn(f"{pm.WARN_DIVISION_BY_ZERO}:mr_merge_rate_pct", result["warnings"])
        self.assertEqual(result["mr_merge_rate_pct"], result["mr_merge_rate_pct"])  # not NaN

    def test_normal_merge_rate(self):
        mrs = [mr(state="merged"), mr(state="merged"), mr(state="closed")]
        result = pm.personal_metrics("alice", mrs=mrs, issues=[])
        self.assertEqual(result["mr_count"], 3)
        self.assertEqual(result["mr_merged_count"], 2)
        self.assertAlmostEqual(result["mr_merge_rate_pct"], 66.7)
        self.assertNotIn(f"{pm.WARN_DIVISION_BY_ZERO}:mr_merge_rate_pct", result["warnings"])

    def test_mrs_belonging_to_other_authors_are_excluded(self):
        mrs = [mr(author="bob", state="merged")]
        result = pm.personal_metrics("alice", mrs=mrs, issues=[])
        self.assertEqual(result["mr_count"], 0)


class CycleTimeValueAssertionTests(unittest.TestCase):
    """No prior test asserted the actual VALUES of the cycle-time fields —
    swapping the underlying source list would have kept every old test
    green."""

    def test_mr_cycle_time_avg_and_median_values(self):
        mrs = [
            mr(cycle_time_hours=24.0),
            mr(cycle_time_hours=72.0),
            mr(cycle_time_hours=120.0),
        ]
        result = pm.personal_metrics("alice", mrs=mrs, issues=[])
        self.assertEqual(result["mr_cycle_time_avg_hours"], 72.0)
        self.assertEqual(result["mr_cycle_time_median_hours"], 72.0)

    def test_mr_cycle_time_none_values_are_excluded_not_zeroed(self):
        mrs = [mr(cycle_time_hours=24.0), mr(cycle_time_hours=None)]
        result = pm.personal_metrics("alice", mrs=mrs, issues=[])
        self.assertEqual(result["mr_cycle_time_avg_hours"], 24.0)

    def test_mr_cycle_time_unavailable_for_every_mr_warns(self):
        mrs = [mr(cycle_time_hours=None), mr(cycle_time_hours=None)]
        result = pm.personal_metrics("alice", mrs=mrs, issues=[])
        self.assertIsNone(result["mr_cycle_time_avg_hours"])
        self.assertIn(pm.WARN_MR_CYCLE_TIME_UNAVAILABLE, result["warnings"])

    def test_task_cycle_time_avg_and_median_values(self):
        issues = [
            issue("A", events=[ev(dt(2026, 1, 1), "In Progress"), ev(dt(2026, 1, 2), "Done")]),  # 24h
            issue("B", events=[ev(dt(2026, 1, 1), "In Progress"), ev(dt(2026, 1, 4), "Done")]),  # 72h
            issue("C", events=[ev(dt(2026, 1, 1), "In Progress"), ev(dt(2026, 1, 6), "Done")]),  # 120h
        ]
        result = pm.personal_metrics("alice", mrs=[], issues=issues)
        self.assertEqual(result["task_cycle_time_avg_hours"], 72.0)
        self.assertEqual(result["task_cycle_time_median_hours"], 72.0)

    def test_task_cycle_time_unavailable_for_every_done_task_warns(self):
        # Both done only via resolutiondate fallback (M1) -> no transition
        # -> cycle_time_hours is None for both, even though both count done.
        issues = [
            issue("A", events=[], resolutiondate=dt(2026, 1, 5)),
            issue("B", events=[], resolutiondate=dt(2026, 1, 6)),
        ]
        result = pm.personal_metrics("alice", mrs=[], issues=issues)
        self.assertEqual(result["tasks_done"], 2)
        self.assertIsNone(result["task_cycle_time_avg_hours"])
        self.assertIn(pm.WARN_TASK_CYCLE_TIME_UNAVAILABLE, result["warnings"])


class MrCommitsAndChangesValueTests(unittest.TestCase):
    def test_mr_commits_and_changes_values(self):
        mrs = [mr(commits_count=3, changes_count=4), mr(commits_count=5, changes_count=6)]
        result = pm.personal_metrics("alice", mrs=mrs, issues=[])
        self.assertEqual(result["mr_commits_avg"], 4.0)
        self.assertEqual(result["mr_commits_sum"], 8)
        self.assertEqual(result["mr_changes_count_avg"], 5.0)
        self.assertEqual(result["mr_changes_count_sum"], 10)


class DiffStatsAvailabilityTests(unittest.TestCase):
    def test_unavailable_diff_stats_are_excluded_from_average_not_zeroed(self):
        mrs = [
            mr(additions=None, deletions=None, diff_stats_available=False),
            mr(additions=None, deletions=None, diff_stats_available=False),
        ]
        result = pm.personal_metrics("alice", mrs=mrs, issues=[])
        self.assertIsNone(result["mr_diff_size_avg"])  # not 0.0
        self.assertEqual(result["mr_diff_size_available_count"], 0)
        self.assertIn(pm.WARN_DIFF_STATS_UNAVAILABLE, result["warnings"])

    def test_available_diff_stats_are_averaged_correctly(self):
        mrs = [mr(additions=10, deletions=0), mr(additions=4, deletions=6)]
        result = pm.personal_metrics("alice", mrs=mrs, issues=[])
        self.assertEqual(result["mr_diff_size_avg"], 10.0)  # (10+10)/2
        self.assertEqual(result["mr_diff_size_available_count"], 2)
        self.assertNotIn(pm.WARN_DIFF_STATS_UNAVAILABLE, result["warnings"])

    def test_mix_of_available_and_unavailable_only_averages_available_ones(self):
        mrs = [mr(additions=10, deletions=0), mr(additions=None, deletions=None, diff_stats_available=False)]
        result = pm.personal_metrics("alice", mrs=mrs, issues=[])
        self.assertEqual(result["mr_diff_size_avg"], 10.0)
        self.assertEqual(result["mr_diff_size_available_count"], 1)


class LinkedTasksTests(unittest.TestCase):
    def test_linked_tasks_and_mr_per_task(self):
        mrs = [
            mr(jira_key="PROJ-1"),
            mr(jira_key="PROJ-1"),
            mr(jira_key="PROJ-2"),
            mr(jira_key=""),
        ]
        result = pm.personal_metrics("alice", mrs=mrs, issues=[])
        self.assertEqual(result["linked_tasks"], 2)
        self.assertEqual(result["mr_with_jira_key"], 3)
        self.assertAlmostEqual(result["mr_per_task"], 1.5)

    def test_no_linked_tasks_yields_none_not_zero(self):
        result = pm.personal_metrics("alice", mrs=[mr(jira_key="")], issues=[])
        self.assertEqual(result["linked_tasks"], 0)
        self.assertIsNone(result["mr_per_task"])


class StoryPointsQaEstimationTests(unittest.TestCase):
    def test_sums_and_averages_only_over_done_tasks(self):
        issues = [
            issue("A", story_points=5, qa_estimation=2, events=[ev(dt(2026, 1, 1), "In Progress"), ev(dt(2026, 1, 2), "Done")]),
            issue("B", story_points=3, qa_estimation=1, events=[ev(dt(2026, 1, 1), "In Progress"), ev(dt(2026, 1, 2), "Done")]),
            issue("C", story_points=8, qa_estimation=4, events=[ev(dt(2026, 1, 1), "In Progress")]),  # unfinished
        ]
        result = pm.personal_metrics("alice", mrs=[], issues=issues)
        self.assertEqual(result["story_points_total"], 8.0)
        self.assertEqual(result["story_points_avg"], 4.0)
        self.assertEqual(result["qa_estimation_total"], 3.0)
        self.assertEqual(result["qa_estimation_avg"], 1.5)


class ReworkShareTests(unittest.TestCase):
    """M3: rework_tasks / rework_share / issue_count were missing entirely."""

    def test_rework_tasks_and_share(self):
        issues = [
            issue(
                "A",
                events=[
                    ev(dt(2026, 1, 1), "In Progress"),
                    ev(dt(2026, 1, 2), "Code Review"),
                    ev(dt(2026, 1, 3), "In Progress"),  # repeat -> rework_count=1
                    ev(dt(2026, 1, 4), "Done"),
                ],
            ),
            issue("B", events=[ev(dt(2026, 1, 1), "In Progress"), ev(dt(2026, 1, 2), "Done")]),  # rework_count=0
            issue("C", events=[ev(dt(2026, 1, 1), "In Progress")]),  # unfinished, excluded from denominator
        ]
        result = pm.personal_metrics("alice", mrs=[], issues=issues)
        self.assertEqual(result["tasks_done"], 2)
        self.assertEqual(result["issue_count"], 2)
        self.assertEqual(result["rework_tasks"], 1)
        self.assertEqual(result["rework_total"], 1)
        self.assertAlmostEqual(result["rework_share"], 0.5)

    def test_rework_share_is_none_when_no_done_tasks(self):
        result = pm.personal_metrics("alice", mrs=[], issues=[])
        self.assertIsNone(result["rework_share"])
        self.assertEqual(result["rework_tasks"], 0)

    def test_rework_only_counted_over_done_flows(self):
        # An unfinished issue's rework must never leak into rework_total —
        # rework is summed only over DONE flows.
        issues = [
            issue(
                "A",
                events=[
                    ev(dt(2026, 1, 1), "In Progress"),
                    ev(dt(2026, 1, 2), "Code Review"),
                    ev(dt(2026, 1, 3), "In Progress"),  # rework_count=1, but never finished
                ],
            ),
        ]
        result = pm.personal_metrics("alice", mrs=[], issues=issues)
        self.assertEqual(result["tasks_done"], 0)
        self.assertEqual(result["rework_total"], 0)
        self.assertEqual(result["rework_tasks"], 0)


class PipelineSuccessByUserTests(unittest.TestCase):
    """m6: gitlab_client.py emits user_username on pipelines with no
    consumer — wired into personal_metrics() as pipeline_success_rate."""

    def test_success_rate_for_user(self):
        pipelines = [
            {"user_username": "alice", "status": "success"},
            {"user_username": "alice", "status": "failed"},
            {"user_username": "bob", "status": "success"},
        ]
        self.assertEqual(pm.personal_pipeline_success("alice", pipelines), 0.5)

    def test_none_when_no_pipelines_at_all(self):
        self.assertIsNone(pm.personal_pipeline_success("alice", []))

    def test_none_when_user_has_no_pipelines(self):
        pipelines = [{"user_username": "bob", "status": "success"}]
        self.assertIsNone(pm.personal_pipeline_success("alice", pipelines))

    def test_wired_into_personal_metrics(self):
        pipelines = [{"user_username": "alice", "status": "success"}, {"user_username": "alice", "status": "success"}]
        result = pm.personal_metrics("alice", mrs=[], issues=[], pipelines=pipelines)
        self.assertEqual(result["pipeline_success_rate"], 1.0)

    def test_personal_metrics_without_pipelines_arg_is_none(self):
        result = pm.personal_metrics("alice", mrs=[], issues=[])
        self.assertIsNone(result["pipeline_success_rate"])


class PipelineUserAttributionMissingTests(unittest.TestCase):
    """team-lead follow-up: fetch_pipeline_user=False makes every record's
    user_username == "", so pipeline_success_rate silently becomes None for
    EVERY person — indistinguishable from "genuinely zero pipelines" unless
    this module raises its own warning off gitlab_client.py's
    user_lookup_available flag."""

    def test_opted_out_run_warns_and_stays_none_not_a_fabricated_rate(self):
        # Mirrors gitlab_client.py's pipelines(fetch_pipeline_user=False)
        # output shape: every record carries user_lookup_available=False.
        pipelines = [
            {"user_username": "", "status": "success", "user_lookup_available": False},
            {"user_username": "", "status": "failed", "user_lookup_available": False},
        ]
        result = pm.personal_metrics("alice", mrs=[], issues=[], pipelines=pipelines)
        self.assertIsNone(result["pipeline_success_rate"])
        self.assertIn(pm.WARN_PIPELINE_SUCCESS_UNAVAILABLE, result["warnings"])

    def test_genuinely_zero_pipelines_for_this_person_does_not_warn(self):
        # Attribution WAS collected (user_lookup_available=True); this
        # person simply triggered none — a real "0", not "we didn't ask".
        pipelines = [
            {"user_username": "bob", "status": "success", "user_lookup_available": True},
        ]
        result = pm.personal_metrics("alice", mrs=[], issues=[], pipelines=pipelines)
        self.assertIsNone(result["pipeline_success_rate"])
        self.assertNotIn(pm.WARN_PIPELINE_SUCCESS_UNAVAILABLE, result["warnings"])

    def test_no_pipeline_data_at_all_does_not_warn(self):
        # A third distinct state: nothing was fetched at all (e.g. GitLab
        # not configured) — not the same as "we asked and got nothing back".
        result = pm.personal_metrics("alice", mrs=[], issues=[], pipelines=[])
        self.assertIsNone(result["pipeline_success_rate"])
        self.assertNotIn(pm.WARN_PIPELINE_SUCCESS_UNAVAILABLE, result["warnings"])

    def test_successful_lookup_with_a_real_rate_does_not_warn(self):
        pipelines = [
            {"user_username": "alice", "status": "success", "user_lookup_available": True},
            {"user_username": "alice", "status": "failed", "user_lookup_available": True},
        ]
        result = pm.personal_metrics("alice", mrs=[], issues=[], pipelines=pipelines)
        self.assertEqual(result["pipeline_success_rate"], 0.5)
        self.assertNotIn(pm.WARN_PIPELINE_SUCCESS_UNAVAILABLE, result["warnings"])

    def test_missing_flag_defaults_to_available_for_backward_compatibility(self):
        # Hand-built fixtures / pre-opt-out data that never set the flag at
        # all must behave exactly as before this fix — no spurious warning.
        pipelines = [{"user_username": "alice", "status": "success"}]
        result = pm.personal_metrics("alice", mrs=[], issues=[], pipelines=pipelines)
        self.assertNotIn(pm.WARN_PIPELINE_SUCCESS_UNAVAILABLE, result["warnings"])

    def test_helper_function_directly(self):
        self.assertTrue(pm._pipeline_user_attribution_missing([{"user_lookup_available": False}]))
        self.assertFalse(pm._pipeline_user_attribution_missing([{"user_lookup_available": True}]))
        self.assertFalse(pm._pipeline_user_attribution_missing([]))
        self.assertFalse(pm._pipeline_user_attribution_missing([{}]))  # missing key -> available


class TzAwareValidationTests(unittest.TestCase):
    """m7: naive/aware datetime mixing must raise a clear, field-named
    error instead of a bare TypeError from a later comparison."""

    def test_naive_status_event_at_raises_clear_error(self):
        with self.assertRaises(ValueError) as ctx:
            pm.StatusEvent(at=datetime(2026, 1, 1), to_status="Done")  # naive, no tzinfo
        self.assertIn("StatusEvent.at", str(ctx.exception))

    def test_naive_sprint_start_raises_clear_error(self):
        with self.assertRaises(ValueError) as ctx:
            pm.Sprint(name="S", start=datetime(2026, 1, 1), end=dt(2026, 1, 14))
        self.assertIn("Sprint.start", str(ctx.exception))

    def test_naive_sprint_end_raises_clear_error(self):
        with self.assertRaises(ValueError) as ctx:
            pm.Sprint(name="S", start=dt(2026, 1, 1), end=datetime(2026, 1, 14))
        self.assertIn("Sprint.end", str(ctx.exception))

    def test_non_utc_but_aware_offset_is_accepted(self):
        # jira_client.py's parse_jira_time keeps the Jira server's ORIGINAL
        # offset (e.g. +03:00) rather than normalizing to Z — only naive
        # datetimes must be rejected, not every non-zero-offset one.
        plus3 = timezone(timedelta(hours=3))
        s = pm.Sprint(name="S", start=datetime(2026, 1, 1, tzinfo=plus3), end=dt(2026, 1, 14))
        self.assertEqual(s.start, datetime(2026, 1, 1, tzinfo=plus3))

    def test_naive_resolutiondate_raises_clear_error(self):
        with self.assertRaises(ValueError) as ctx:
            pm.JiraIssueInput(key="A", issuetype="Story", assignee="alice", resolutiondate=datetime(2026, 1, 1))
        self.assertIn("resolutiondate", str(ctx.exception))

    def test_aware_utc_values_are_accepted(self):
        pm.StatusEvent(at=dt(2026, 1, 1), to_status="Done")
        pm.Sprint(name="S", start=dt(2026, 1, 1), end=dt(2026, 1, 14))
        pm.JiraIssueInput(key="A", issuetype="Story", assignee="alice", resolutiondate=dt(2026, 1, 1))


class SprintBreakdownTests(unittest.TestCase):
    def setUp(self):
        # Raw sprint ends — no pre-baked 23:59:59 (B2's contract: the module
        # applies end-of-day internally via Sprint.end_of_day).
        self.sprint1 = pm.Sprint(name="Sprint 1", start=dt(2026, 1, 1), end=dt(2026, 1, 14))
        self.sprint2 = pm.Sprint(name="Sprint 2", start=dt(2026, 1, 15), end=dt(2026, 1, 28))

    def test_raises_on_empty_sprint_scope(self):
        with self.assertRaises(ValueError):
            pm.personal_sprint_breakdown("alice", mrs=[], issues=[], sprints=[])
        with self.assertRaises(ValueError):
            pm.build_personal_report(mrs=[], issues=[], sprints=[])

    def test_sprint_with_no_completed_task_is_skipped(self):
        issues = [issue("A", story_points=5, events=[ev(dt(2026, 1, 5), "In Progress")])]  # never finished
        rows = pm.personal_sprint_breakdown("alice", mrs=[], issues=issues, sprints=[self.sprint1, self.sprint2])
        self.assertEqual(rows, [])

    def test_task_bucketed_into_the_sprint_containing_done_at(self):
        issues = [
            issue(
                "A",
                story_points=5,
                qa_estimation=2,
                events=[ev(dt(2026, 1, 3), "In Progress"), ev(dt(2026, 1, 5), "Done")],
            ),
            issue(
                "B",
                story_points=3,
                events=[ev(dt(2026, 1, 16), "In Progress"), ev(dt(2026, 1, 20), "Done")],
            ),
        ]
        rows = pm.personal_sprint_breakdown("alice", mrs=[], issues=issues, sprints=[self.sprint1, self.sprint2])
        self.assertEqual([r["sprint"] for r in rows], ["Sprint 1", "Sprint 2"])
        self.assertEqual(rows[0]["throughput"], 1)
        self.assertEqual(rows[0]["story_points_total"], 5.0)
        self.assertEqual(rows[1]["throughput"], 1)
        self.assertEqual(rows[1]["story_points_total"], 3.0)

    def test_mr_bucketed_by_merged_at_falling_back_to_created_at(self):
        issues = [issue("A", events=[ev(dt(2026, 1, 3), "In Progress"), ev(dt(2026, 1, 5), "Done")])]
        mrs = [
            mr(created_at="2026-01-04T00:00:00Z", merged_at="2026-01-06T00:00:00Z"),  # merged_at in sprint 1
            mr(created_at="2026-01-30T00:00:00Z", merged_at=None, state="closed"),  # outside both sprints
        ]
        rows = pm.personal_sprint_breakdown("alice", mrs=mrs, issues=issues, sprints=[self.sprint1, self.sprint2])
        self.assertEqual(rows[0]["mr_count"], 1)

    def test_done_at_exactly_on_sprint_start_boundary_is_included(self):
        issues = [issue("A", events=[ev(dt(2026, 1, 1, 0, 0, 0), "In Progress"), ev(dt(2026, 1, 1, 0, 0, 0), "Done")])]
        rows = pm.personal_sprint_breakdown("alice", mrs=[], issues=issues, sprints=[self.sprint1, self.sprint2])
        self.assertEqual(rows[0]["sprint"], "Sprint 1")
        self.assertEqual(rows[0]["throughput"], 1)

    def test_done_at_exactly_on_sprint_end_boundary_is_included(self):
        # sprint1's raw end is Jan 14; end_of_day extends it to 23:59:59
        # (B2) — a completion at exactly that moment must still count.
        issues = [issue("A", events=[ev(dt(2026, 1, 1), "In Progress"), ev(dt(2026, 1, 14, 23, 59, 59), "Done")])]
        rows = pm.personal_sprint_breakdown("alice", mrs=[], issues=issues, sprints=[self.sprint1, self.sprint2])
        self.assertEqual(rows[0]["sprint"], "Sprint 1")

    def test_overlapping_sprints_tie_break_by_earliest_end(self):
        overlap_a = pm.Sprint(name="Overlap A", start=dt(2026, 1, 1), end=dt(2026, 1, 20))
        overlap_b = pm.Sprint(name="Overlap B", start=dt(2026, 1, 10), end=dt(2026, 1, 15))
        issues = [issue("A", events=[ev(dt(2026, 1, 1), "In Progress"), ev(dt(2026, 1, 12), "Done")])]
        rows = pm.personal_sprint_breakdown("alice", mrs=[], issues=issues, sprints=[overlap_a, overlap_b])
        # done_at (Jan 12) falls inside BOTH sprints; earliest-end tie-break
        # picks Overlap B (ends Jan 15) over Overlap A (ends Jan 20) — the
        # task must appear in exactly one sprint's row, never both (M2).
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sprint"], "Overlap B")
        self.assertEqual(rows[0]["throughput"], 1)


class TeamRosterTests(unittest.TestCase):
    def test_build_personal_report_covers_everyone_from_both_sources(self):
        mrs = [mr(author="alice"), mr(author="bob")]
        issues = [issue("A", assignee="carol", events=[ev(dt(2026, 1, 3), "In Progress"), ev(dt(2026, 1, 5), "Done")])]
        sprints = [pm.Sprint(name="Sprint 1", start=dt(2026, 1, 1), end=dt(2026, 1, 14))]
        report = pm.build_personal_report(mrs=mrs, issues=issues, sprints=sprints)
        users = {p["user"] for p in report["people"]}
        self.assertEqual(users, {"alice", "bob", "carol"})


class DateWindowScopeTests(unittest.TestCase):
    """m8: an explicit date window is a legitimate alternative to sprint
    scope — only a truly unscoped run (neither) must be refused."""

    def test_date_window_allows_a_sprintless_report(self):
        mrs = [mr(author="alice")]
        issues = [issue("A", events=[ev(dt(2026, 1, 3), "In Progress"), ev(dt(2026, 1, 5), "Done")])]
        report = pm.build_personal_report(
            mrs=mrs, issues=issues, sprints=[], date_window=(dt(2026, 1, 1), dt(2026, 1, 31))
        )
        self.assertEqual(len(report["people"]), 1)
        self.assertEqual(report["people"][0]["sprints"], [])
        self.assertEqual(report["people"][0]["tasks_done"], 1)

    def test_unscoped_run_still_refused(self):
        with self.assertRaises(ValueError):
            pm.build_personal_report(mrs=[], issues=[], sprints=[], date_window=None)

    def test_naive_date_window_raises_clear_error(self):
        with self.assertRaises(ValueError):
            pm.build_personal_report(mrs=[], issues=[], sprints=[], date_window=(datetime(2026, 1, 1), dt(2026, 1, 31)))

    def test_sprints_and_date_window_both_given_still_builds_breakdown(self):
        issues = [issue("A", events=[ev(dt(2026, 1, 3), "In Progress"), ev(dt(2026, 1, 5), "Done")])]
        sprints = [pm.Sprint(name="Sprint 1", start=dt(2026, 1, 1), end=dt(2026, 1, 14))]
        report = pm.build_personal_report(
            mrs=[], issues=issues, sprints=sprints, date_window=(dt(2026, 1, 1), dt(2026, 1, 31))
        )
        alice = next(p for p in report["people"] if p["user"] == "alice")
        self.assertEqual(len(alice["sprints"]), 1)


if __name__ == "__main__":
    unittest.main()
