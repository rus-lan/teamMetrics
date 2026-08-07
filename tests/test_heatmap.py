import _pathfix  # noqa: F401

import unittest

from team_metrics import heatmap, metrics, model

from helpers import dt, make_cfg, make_issue


class BuildHeatmapTests(unittest.TestCase):
    def test_row_shorter_than_days_when_issue_joins_late(self):
        tl = model.SprintTimeline(start=dt(2026, 1, 5), schedule_end=dt(2026, 1, 9), classify_end=dt(2026, 1, 9))
        cfg = make_cfg(jira_status_categories={"In Progress": "indeterminate"})
        sprint_input = metrics.SprintInput(
            id=1, name="S1", board_id=1, board_name="B", state="closed", timeline=tl, complete_at=dt(2026, 1, 9)
        )
        early_issue = make_issue("PROJ-1", initial_status="In Progress")
        late_issue = make_issue("PROJ-2", initial_status="In Progress")
        issues = [
            metrics.SprintIssue(early_issue, [model.Interval(from_=dt(2025, 12, 1), until=None)]),
            metrics.SprintIssue(late_issue, [model.Interval(from_=dt(2026, 1, 8), until=None)]),  # joins Thursday
        ]
        payload, _ = metrics.build_payload(sprint_input, issues, cfg, velocity_history=[])
        hm = heatmap.heatmap_from_payload(payload)

        self.assertEqual(hm.days, ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"])
        rows_by_key = {r.issue_key: r for r in hm.rows}
        self.assertEqual(len(rows_by_key["PROJ-1"].cells), 5)
        self.assertEqual(len(rows_by_key["PROJ-2"].cells), 2)  # only Thu + Fri
        self.assertEqual([c.date for c in rows_by_key["PROJ-2"].cells], ["2026-01-08", "2026-01-09"])

    def test_cell_carries_raw_status_name_and_category(self):
        hm = heatmap.build_heatmap(
            1,
            ["2026-01-05"],
            [
                model.Issue(
                    key="PROJ-1", epic_key="", story_points=1, qa_estimation=0, role="", labels=[], assignee="",
                    membership_intervals=[],
                    day_statuses=[model.DayStatus(date="2026-01-05", status="В работе", status_category="indeterminate")],
                    status_initial="", status_before_end="", status_end="",
                    committed=True, added=False, removed=False, delivered=False,
                )
            ],
        )
        cell = hm.rows[0].cells[0]
        self.assertEqual(cell.status, "В работе")
        self.assertEqual(cell.status_category, "indeterminate")


class EnrichedRowFieldsTests(unittest.TestCase):
    """v2 heatmap rows (SPEC §B.8) carry epic_key/assignee/story_points/
    qa_estimation/role/role_ru/labels/status_initial/status_end — no other
    lookup should be needed to render a row."""

    def _issue(self, **overrides):
        base = dict(
            key="PROJ-1", epic_key="PROJ-90", story_points=5.0, qa_estimation=2.0, role="BE",
            labels=["ai-review"], assignee="amaksimenkov", membership_intervals=[], day_statuses=[],
            status_initial="To Do", status_before_end="Done", status_end="Done",
            committed=True, added=False, removed=False, delivered=True,
        )
        base.update(overrides)
        return model.Issue(**base)

    def test_row_carries_every_enrichment_field(self):
        hm = heatmap.build_heatmap(1, [], [self._issue()], display_names={"amaksimenkov": "Александр Максименков"})
        row = hm.rows[0]
        self.assertEqual(row.epic_key, "PROJ-90")
        self.assertEqual(row.assignee_login, "amaksimenkov")
        self.assertEqual(row.assignee_display_name, "Александр Максименков")
        self.assertEqual(row.story_points, 5.0)
        self.assertEqual(row.qa_estimation, 2.0)
        self.assertEqual(row.role, "BE")
        self.assertEqual(row.role_ru, "Backend-разработчик")
        self.assertEqual(row.labels, ["ai-review"])
        self.assertEqual(row.status_initial, "To Do")
        self.assertEqual(row.status_end, "Done")

    def test_no_display_name_map_falls_back_to_login(self):
        hm = heatmap.build_heatmap(1, [], [self._issue()])
        self.assertEqual(hm.rows[0].assignee_display_name, "amaksimenkov")

    def test_empty_role_gives_empty_role_ru_not_a_placeholder(self):
        hm = heatmap.build_heatmap(1, [], [self._issue(role="", assignee="")])
        self.assertEqual(hm.rows[0].role_ru, "")

    def test_unknown_role_code_shown_as_is(self):
        hm = heatmap.build_heatmap(1, [], [self._issue(role="ZZ")])
        self.assertEqual(hm.rows[0].role_ru, "ZZ")


if __name__ == "__main__":
    unittest.main()
