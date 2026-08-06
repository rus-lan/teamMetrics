import _pathfix  # noqa: F401

import unittest

from jira_metrics import burndown, metrics, model

from helpers import dt, make_cfg, make_issue


class BurndownWeekendCarryForwardTests(unittest.TestCase):
    """Burndown runs over CALENDAR days; a status recorded only on the last
    working day (Friday) must be carried forward through Sat/Sun (and any
    trailing calendar day with no working-day record) rather than dropping
    the issue or re-counting it — SPEC §6."""

    def setUp(self):
        # Schedule Mon 2026-01-05 .. Fri 2026-01-09; sprint actually completes
        # the following Monday (2026-01-12) -> burndown's calendar window runs
        # past the last working day, through the weekend, into that Monday.
        self.start = dt(2026, 1, 5)
        self.schedule_end = dt(2026, 1, 9)
        self.complete_at = dt(2026, 1, 12)
        self.tl = model.SprintTimeline(start=self.start, schedule_end=self.schedule_end, classify_end=self.complete_at)
        self.cfg = make_cfg(jira_status_categories={"In Progress": "indeterminate", "Done": "done"})

        never_done = make_issue("PROJ-1", story_points=5.0, initial_status="In Progress")
        becomes_done_thursday = make_issue(
            "PROJ-2",
            story_points=3.0,
            initial_status="In Progress",
            status_history=[model.StatusChange(at=dt(2026, 1, 8, 9, 0), from_status="In Progress", to_status="Done")],
        )
        member_forever = model.Interval(from_=dt(2025, 12, 1), until=None)

        sprint_input = metrics.SprintInput(
            id=1, name="Sprint 1", board_id=1, board_name="Board", state="closed",
            timeline=self.tl, complete_at=self.complete_at,
        )
        issues = [
            metrics.SprintIssue(never_done, [member_forever]),
            metrics.SprintIssue(becomes_done_thursday, [member_forever]),
        ]
        self.payload, _ = metrics.build_payload(sprint_input, issues, self.cfg, velocity_history=[])
        self.points = burndown.burndown_from_payload(self.payload, now=self.complete_at)

    def test_calendar_window_spans_start_to_complete_at(self):
        dates = [p.date for p in self.points]
        self.assertEqual(dates, [
            "2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08",
            "2026-01-09", "2026-01-10", "2026-01-11", "2026-01-12",
        ])

    def test_both_issues_counted_before_done_transition(self):
        for p in self.points[:3]:  # Mon, Tue, Wed
            self.assertEqual(p.remaining_items, 2, p.date)
            self.assertEqual(p.remaining_sp, 8.0, p.date)

    def test_done_issue_excluded_from_thursday_onward(self):
        for p in self.points[3:5]:  # Thu, Fri
            self.assertEqual(p.remaining_items, 1, p.date)
            self.assertEqual(p.remaining_sp, 5.0, p.date)

    def test_weekend_carries_forward_fridays_status_not_reset(self):
        saturday, sunday = self.points[5], self.points[6]
        self.assertEqual(saturday.date, "2026-01-10")
        self.assertEqual(sunday.date, "2026-01-11")
        for p in (saturday, sunday):
            self.assertEqual(p.remaining_items, 1)  # still just PROJ-1 — carried forward, not re-added
            self.assertEqual(p.remaining_sp, 5.0)

    def test_trailing_day_past_working_days_also_carries_forward(self):
        monday = self.points[7]
        self.assertEqual(monday.date, "2026-01-12")
        self.assertEqual(monday.remaining_items, 1)
        self.assertEqual(monday.remaining_sp, 5.0)

    def test_ideal_line_is_linear_committed_sp_burn(self):
        committed_sp = self.payload.metrics.committed_sp
        self.assertEqual(committed_sp, 8.0)
        total_days = 7
        for k, p in enumerate(self.points):
            expected = committed_sp * (1 - k / total_days)
            self.assertAlmostEqual(p.ideal_sp, expected)
        self.assertAlmostEqual(self.points[0].ideal_sp, 8.0)
        self.assertAlmostEqual(self.points[-1].ideal_sp, 0.0)

    def test_one_day_sprint_gives_single_point_at_full_committed_sp(self):
        tl = model.SprintTimeline(start=dt(2026, 2, 2), schedule_end=dt(2026, 2, 2), classify_end=dt(2026, 2, 2))
        issue = make_issue("PROJ-9", story_points=13.0, initial_status="In Progress")
        cfg = make_cfg(jira_status_categories={"In Progress": "indeterminate"})
        sprint_input = metrics.SprintInput(
            id=2, name="Sprint 2", board_id=1, board_name="Board", state="closed", timeline=tl, complete_at=dt(2026, 2, 2)
        )
        issues = [metrics.SprintIssue(issue, [model.Interval(from_=dt(2026, 1, 1), until=None)])]
        payload, _ = metrics.build_payload(sprint_input, issues, cfg, velocity_history=[])
        points = burndown.burndown_from_payload(payload, now=dt(2026, 2, 2))
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].ideal_sp, 13.0)


if __name__ == "__main__":
    unittest.main()
