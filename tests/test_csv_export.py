"""Coverage for csv_export.py — the 18 out/ CSVs (SPEC §C.12, §I)."""

import _pathfix  # noqa: F401

import csv
import tempfile
import unittest
from pathlib import Path

from team_metrics import csv_export, jira_client as jc, metrics as metrics_mod, model, out_writer, report_data

from helpers import dt

STATUSES = [
    jc.Status(id="1", name="To Do", category_key="new"),
    jc.Status(id="2", name="In Progress", category_key="indeterminate"),
    jc.Status(id="3", name="Done", category_key="done"),
]


class _FakeJiraClient:
    def __init__(self, facts):
        self._sprint = jc.Sprint(
            id=100, name="Sprint 100", state="closed", board_id=1,
            start_at=dt(2026, 1, 5), end_at=dt(2026, 1, 9, 18), complete_at=dt(2026, 1, 9, 18),
        )
        self._board = jc.Board(id=1, name="Team Board", type="scrum")
        self._facts = facts

    def sprint(self, sprint_id):
        return self._sprint

    def board(self, board_id):
        return self._board

    def closed_sprints(self, board_id):
        return [self._sprint]

    def board_sprints(self, board_id, state=None):
        return [self._sprint] if state == "closed" else []

    def fetch_sprint_issues(self, sprint_ids, story_points_field_id=""):
        out = []
        for f in self._facts:
            filtered = {sid: f.membership_by_sprint.get(sid, []) for sid in sprint_ids}
            if any(filtered.values()):
                import dataclasses

                out.append(dataclasses.replace(f, membership_by_sprint=filtered))
        return out

    def list_statuses(self):
        return STATUSES

    def suggest_sprints(self, query):
        return []


def _fact(key, assignee, done_at, story_points=3.0, qa_estimation=1.0):
    return jc.IssueFacts(
        key=key, epic_key="", type="Story", role="", labels=[], assignee=assignee,
        story_points=story_points, qa_estimation=qa_estimation, created=dt(2026, 1, 5),
        initial_status="In Progress", initial_status_id="2",
        status_history=[jc.RawStatusChange(at=done_at, from_name="In Progress", to_name="Done", from_id="2", to_id="3")],
        sp_events=[], current_status="Done", current_status_category_key="done",
        assignee_display_name=assignee.capitalize(),
        membership_by_sprint={100: [model.Interval(from_=dt(2026, 1, 5), until=None)]},
    )


class _FakeGitLabClient:
    """3 employees (alice/bob/carol); 10 team pipelines split across
    employees + unattributed — the exact regression shape for source bug (b)
    (merge_report.py's per-employee rows carrying the WHOLE team's pipeline
    count, multiplying report_team.csv totals by employee count)."""

    def __init__(self):
        self.request_count = 0
        self.deployment_warnings = []

    def project_id(self, path):
        return 1

    def merge_requests(self, path, project_id, authors, *, states=(), window=None, errors=None, fetch_mr_details=True):
        rows = []
        for a in authors:
            if a == "alice":
                rows.append({"author": "alice", "author_name": "Alice", "state": "merged",
                             "created_at": "2026-01-06T00:00:00Z", "merged_at": "2026-01-07T00:00:00Z",
                             "cycle_time_hours": 24.0, "changes_count": 4, "changes_count_available": True,
                             "additions": 10, "deletions": 5, "commits_count": 2, "mr_id": 1, "project": path,
                             "jira_key": "T-1"})
            elif a == "carol":
                # carol appears in `people` (an MR author) but has NO pipeline/
                # deployment attribution at all — proves the per-employee
                # columns come out as an empty cell, never a fabricated 0.
                rows.append({"author": "carol", "author_name": "Carol", "state": "closed",
                             "created_at": "2026-01-06T00:00:00Z", "merged_at": None,
                             "cycle_time_hours": None, "changes_count": None, "changes_count_available": False,
                             "additions": None, "deletions": None, "commits_count": None, "mr_id": 2, "project": path,
                             "jira_key": ""})
        return rows

    def pipelines(self, path, project_id, *, window=None, fetch_pipeline_user=True):
        rows = []
        for i in range(4):
            rows.append({"project": path, "pipeline_id": i, "status": "success" if i < 3 else "failed",
                         "created_at": "2026-01-06T00:00:00Z", "updated_at": "2026-01-06T00:00:00Z",
                         "user_username": "alice", "user_name": "Alice"})
        for i in range(4, 7):
            rows.append({"project": path, "pipeline_id": i, "status": "success",
                         "created_at": "2026-01-06T00:00:00Z", "updated_at": "2026-01-06T00:00:00Z",
                         "user_username": "bob", "user_name": "Bob"})
        for i in range(7, 10):
            rows.append({"project": path, "pipeline_id": i, "status": "success",
                         "created_at": "2026-01-06T00:00:00Z", "updated_at": "2026-01-06T00:00:00Z",
                         "user_username": "", "user_name": ""})
        return rows

    def deployments(self, path, project_id, *, window=None):
        return [{"project": path, "deployment_id": 1, "status": "success",
                 "created_at": "2026-01-06T00:00:00Z", "finished_at": "2026-01-06T00:00:00Z",
                 "user_username": "alice", "user_name": "Alice"}]

    def coverage(self, path, project_id, *, window=None):
        return [{"project": path, "pipeline_id": 1, "coverage": 61.0, "created_at": "2026-01-06T00:00:00Z"}]


def _build_report_and_raw(gitlab=True):
    facts = [_fact("T-1", "alice", dt(2026, 1, 7, 10)), _fact("T-2", "bob", dt(2026, 1, 8, 10))]
    client = _FakeJiraClient(facts)
    kwargs = dict(sprint_ids=[100], history_sprint_count=1, now=dt(2026, 1, 10))
    if gitlab:
        kwargs.update(
            gitlab_client_obj=_FakeGitLabClient(), gitlab_projects=["group/app"],
            employees=["alice", "bob", "carol"],
        )
    return report_data.build_combined_report_with_raw(client, **kwargs)


class CsvHeaderTests(unittest.TestCase):
    """Exact §C.12 header tuples for the DictWriter-based CSVs."""

    def setUp(self):
        self.report, self.raw = _build_report_and_raw()
        self.tmp = tempfile.TemporaryDirectory()
        self.out_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _header(self, filename):
        text = (self.out_dir / filename).read_text(encoding="utf-8")
        return text.splitlines()[0].split(",")

    def test_gitlab_mrs_header(self):
        csv_export.write_all(self.out_dir, self.report, self.raw)
        self.assertEqual(
            self._header("gitlab_mrs.csv"),
            ["project", "project_id", "mr_id", "title", "author", "state", "web_url", "created_at",
             "merged_at", "cycle_time_seconds", "cycle_time_hours", "additions", "deletions", "commits_count",
             "changes_count", "jira_key"],
        )

    def test_gitlab_pipelines_header(self):
        csv_export.write_all(self.out_dir, self.report, self.raw)
        self.assertEqual(
            self._header("gitlab_pipelines.csv"),
            ["project", "project_id", "pipeline_id", "ref", "sha", "status", "created_at", "updated_at",
             "web_url", "user_username", "user_name"],
        )

    def test_gitlab_deployments_header(self):
        csv_export.write_all(self.out_dir, self.report, self.raw)
        self.assertEqual(
            self._header("gitlab_deployments.csv"),
            ["project", "project_id", "deployment_id", "status", "environment", "ref", "sha", "created_at",
             "finished_at", "web_url", "user_username", "user_name"],
        )

    def test_gitlab_coverage_header(self):
        csv_export.write_all(self.out_dir, self.report, self.raw)
        self.assertEqual(
            self._header("gitlab_coverage.csv"), ["project", "project_id", "pipeline_id", "ref", "coverage", "created_at"],
        )

    def test_gitlab_users_header(self):
        csv_export.write_all(self.out_dir, self.report, self.raw)
        self.assertEqual(self._header("gitlab_users.csv"), ["login", "display_name"])

    def test_jira_users_header(self):
        csv_export.write_all(self.out_dir, self.report, self.raw)
        self.assertEqual(self._header("jira_users.csv"), ["login", "display_name"])

    def test_jira_issues_header(self):
        csv_export.write_all(self.out_dir, self.report, self.raw)
        self.assertEqual(
            self._header("jira_issues.csv"),
            ["key", "summary", "status", "issuetype", "assignee", "reporter", "sprint", "created",
             "resolutiondate", "resolution", "story_points", "qa_estimation"],
        )

    def test_jira_cycle_time_header(self):
        csv_export.write_all(self.out_dir, self.report, self.raw)
        self.assertEqual(
            self._header("jira_cycle_time.csv"),
            ["key", "assignee", "issuetype", "first_in_progress", "done_at", "cycle_time_hours", "cycle_time_seconds"],
        )

    def test_jira_rework_header(self):
        csv_export.write_all(self.out_dir, self.report, self.raw)
        self.assertEqual(self._header("jira_rework.csv"), ["key", "assignee", "rework_count", "done_transitions"])

    def test_jira_throughput_header(self):
        csv_export.write_all(self.out_dir, self.report, self.raw)
        self.assertEqual(self._header("jira_throughput.csv"), ["key", "assignee", "issuetype", "status", "resolved"])

    def test_sprints_header(self):
        csv_export.write_all(self.out_dir, self.report, self.raw)
        self.assertEqual(self._header("sprints.csv"), ["name", "start_date", "end_date"])

    def test_jira_by_sprint_header(self):
        csv_export.write_all(self.out_dir, self.report, self.raw)
        self.assertEqual(
            self._header("jira_by_sprint.csv"),
            ["name", "start_date", "end_date", "throughput", "avg_cycle_time_hours", "median_cycle_time_hours",
             "rework_total", "rework_rate", "defect_rate", "story_points_total", "avg_story_points",
             "qa_estimation_total", "avg_qa_estimation"],
        )

    def test_gitlab_by_sprint_header(self):
        csv_export.write_all(self.out_dir, self.report, self.raw)
        self.assertEqual(
            self._header("gitlab_by_sprint.csv"),
            ["name", "start_date", "end_date", "bucket_type", "mr_count", "mr_merged_count",
             "avg_mr_cycle_time_hours", "avg_mr_changes_count", "total_mr_changes", "pipeline_count",
             "pipeline_success_rate", "deployment_count", "deployment_success_rate"],
        )

    def test_report_per_employee_header(self):
        csv_export.write_all(self.out_dir, self.report, self.raw)
        self.assertEqual(
            self._header("report_per_employee.csv"),
            ["employee", "gitlab_username", "jira_username", "email", "team", "mr_count", "mr_merged_count",
             "mr_closed_count", "mr_merge_rate", "avg_mr_cycle_time_hours", "avg_mr_diff_size",
             "avg_mr_changes_count", "total_mr_changes", "total_mr_additions", "total_mr_deletions",
             "total_mr_commits", "deployment_count", "deployment_failed", "deployment_fail_rate",
             "pipeline_count", "pipeline_failed", "pipeline_fail_rate", "issues_count", "tasks_done",
             "bug_count", "defect_rate", "avg_issue_cycle_time_hours", "rework_count", "story_points_total",
             "avg_story_points", "qa_estimation_total", "avg_qa_estimation"],
        )

    def test_report_team_header(self):
        csv_export.write_all(self.out_dir, self.report, self.raw)
        self.assertEqual(
            self._header("report_team.csv"),
            ["metric", "employees_count", "total_mr_count", "total_mr_merged", "total_mr_closed",
             "avg_mr_merge_rate", "avg_mr_cycle_time_hours", "avg_mr_diff_size", "avg_mr_changes_count",
             "total_mr_changes", "total_mr_additions", "total_mr_deletions", "total_mr_commits",
             "total_deployments", "total_deployment_failed", "avg_deployment_fail_rate", "total_pipelines",
             "total_pipeline_failed", "avg_pipeline_fail_rate", "total_issues", "total_tasks_done",
             "total_bugs", "avg_defect_rate", "avg_issue_cycle_time_hours", "total_rework",
             "total_story_points", "avg_story_points", "total_qa_estimation", "avg_qa_estimation"],
        )

    def test_report_merged_header(self):
        csv_export.write_all(self.out_dir, self.report, self.raw)
        self.assertEqual(
            self._header("report_merged.csv"),
            ["mr_id", "project", "mr_title", "mr_author", "mr_state", "mr_cycle_time_hours", "mr_additions",
             "mr_deletions", "mr_changes_count", "jira_key", "jira_summary", "jira_assignee", "jira_status",
             "jira_issuetype", "jira_story_points", "jira_qa_estimation"],
        )

    def test_board_header_is_machine_form(self):
        csv_export.write_all(self.out_dir, self.report, self.raw)
        self.assertEqual(
            self._header("board.csv"),
            ["sprint", "state", "start", "end", "committed_sp", "delivered_sp", "added_sp", "removed_sp",
             "estimation_change_sp", "performance_pct", "load_pct", "scope_change_pct", "velocity_sp",
             "sma5_sp", "throughput_count", "closure_rate_count_pct", "closure_rate_sp_pct"],
        )


class HeatmapCsvFormatTests(unittest.TestCase):
    def test_bom_semicolon_and_formula_guard(self):
        report, raw = _build_report_and_raw()
        with tempfile.TemporaryDirectory() as tmp:
            csv_export.write_all(tmp, report, raw)
            raw_bytes = (Path(tmp) / "heatmap.csv").read_bytes()
        self.assertTrue(raw_bytes.startswith("﻿".encode("utf-8")))
        text = raw_bytes.decode("utf-8")
        first_line = text.split("\n", 1)[0]
        self.assertIn(";", first_line)
        self.assertNotIn(",", first_line.replace(";", ""))  # no comma-CSV leaking in


class BoardCsvStillGuardedViaWriteCsvTests(unittest.TestCase):
    """report_data._build_board_table() no longer runs its own formula
    guard (retired as a redundant double-guard on top of out_writer's
    central one — see BoardTableGuardRetiredTests in test_report_data.py).
    board.csv itself must still come out guarded, because csv_export.write_all()
    feeds those same rows through out_writer.write_csv() before they touch
    disk."""

    def _board_csv_data_row(self, sprint_name: str, performance_pct: float) -> list[str]:
        sprint = metrics_mod.SprintMeta(
            id=1, name=sprint_name, board_id=1, board_name="Board", state="closed",
            start_at=dt(2026, 1, 5), end_at=dt(2026, 1, 9), complete_at=dt(2026, 1, 9),
            working_days=[],
        )
        metrics = metrics_mod.Metrics(committed_sp=5.0, delivered_sp=5.0, performance_pct=performance_pct)
        payload = metrics_mod.Payload(
            schema_version=2, sprint=sprint, issues=[], sets=model.Sets(), metrics=metrics, throughput_daily=[],
        )
        header, rows = report_data._build_board_table([{"payload": payload}])
        board_rows = [dict(zip(header, row)) for row in rows]
        with tempfile.TemporaryDirectory() as tmp:
            path = out_writer.write_csv(tmp, "board.csv", board_rows)
            with path.open(encoding="utf-8", newline="") as f:
                reader = csv.reader(f)
                next(reader)  # header
                return next(reader)

    def test_hostile_sprint_name_is_guarded_on_disk(self):
        data_row = self._board_csv_data_row('=HYPERLINK("http://evil/","click")', 0.0)
        self.assertEqual(data_row[0], '\'=HYPERLINK("http://evil/","click")')

    def test_negative_percentage_is_guarded_on_disk(self):
        data_row = self._board_csv_data_row("Sprint 1", -12.5)
        self.assertEqual(data_row[9], "'-12.5")  # performance_pct column


class FractionVsPercentTests(unittest.TestCase):
    """CSVs keep the source skill's 0..1 fraction convention (JSON is 0..100)."""

    def test_report_per_employee_rates_are_fractions(self):
        report, raw = _build_report_and_raw()
        alice_pct = next(p["metrics"]["mr_merge_rate_pct"] for p in report["people"] if p["login"] == "alice")
        with tempfile.TemporaryDirectory() as tmp:
            csv_export.write_all(tmp, report, raw)
            text = (Path(tmp) / "report_per_employee.csv").read_text(encoding="utf-8")
        header = text.splitlines()[0].split(",")
        alice_row = next(line for line in text.splitlines()[1:] if line.startswith("Alice,"))
        fields = dict(zip(header, alice_row.split(",")))
        self.assertAlmostEqual(float(fields["mr_merge_rate"]), alice_pct / 100.0, places=3)


class ReportTeamBugBRegressionTests(unittest.TestCase):
    """Source bug (b): merge_report.py's per-employee rows carried the WHOLE
    team's pipeline/deployment rows, so summing them multiplied
    total_pipelines by len(employees). 3 employees, 10 team pipelines here —
    total_pipelines must stay 10, never 30."""

    def test_total_pipelines_equals_flat_team_count_not_multiplied_by_employees(self):
        report, raw = _build_report_and_raw()
        self.assertEqual(len(report["people"]), 3)
        self.assertEqual(report["engineering"]["pipelines"]["count"], 10)
        with tempfile.TemporaryDirectory() as tmp:
            csv_export.write_all(tmp, report, raw)
            text = (Path(tmp) / "report_team.csv").read_text(encoding="utf-8")
        header = text.splitlines()[0].split(",")
        row = dict(zip(header, text.splitlines()[1].split(",")))
        self.assertEqual(row["total_pipelines"], "10")

    def test_per_employee_pipeline_columns_filtered_by_user_username(self):
        report, raw = _build_report_and_raw()
        with tempfile.TemporaryDirectory() as tmp:
            csv_export.write_all(tmp, report, raw)
            text = (Path(tmp) / "report_per_employee.csv").read_text(encoding="utf-8")
        header = text.splitlines()[0].split(",")
        rows = {line.split(",")[0]: dict(zip(header, line.split(","))) for line in text.splitlines()[1:]}
        self.assertEqual(rows["Alice"]["pipeline_count"], "4")
        self.assertEqual(rows["Bob"]["pipeline_count"], "3")
        # carol has no MR/pipeline attribution at all -> empty cell, not 0
        self.assertEqual(rows["Carol"]["pipeline_count"], "")


class EmptyCollectionSkipTests(unittest.TestCase):
    def test_no_crash_and_gitlab_csvs_skipped_when_gitlab_not_configured(self):
        report, raw = _build_report_and_raw(gitlab=False)
        self.assertFalse(raw["gitlab_configured"])
        with tempfile.TemporaryDirectory() as tmp:
            written = csv_export.write_all(tmp, report, raw)
            names = {p.name for p in written}
        # SPEC §H.12: exactly these 6 gitlab-named CSVs + report_merged.csv
        # are absent when GitLab was never configured -- not just the two
        # spot-checked here, or a whole-axis-worth of empty
        # gitlab_by_sprint.csv rows would slip through unnoticed (regression
        # covered once, fixed in csv_export._gitlab_by_sprint_rows).
        absent = {
            "gitlab_mrs.csv", "gitlab_pipelines.csv", "gitlab_deployments.csv",
            "gitlab_coverage.csv", "gitlab_users.csv", "gitlab_by_sprint.csv", "report_merged.csv",
        }
        self.assertEqual(names & absent, set())
        self.assertIn("jira_issues.csv", names)
        self.assertIn("board.csv", names)
        self.assertIn("sprints.csv", names)
        self.assertIn("jira_by_sprint.csv", names)
        self.assertIn("heatmap.csv", names)


def _typed_fact(key, assignee, done_at, issuetype="Story"):
    return jc.IssueFacts(
        key=key, epic_key="", type=issuetype, role="", labels=[], assignee=assignee,
        story_points=1.0, qa_estimation=0.0, created=dt(2026, 1, 5),
        initial_status="In Progress", initial_status_id="2",
        status_history=[jc.RawStatusChange(at=done_at, from_name="In Progress", to_name="Done", from_id="2", to_id="3")],
        sp_events=[], current_status="Done", current_status_category_key="done",
        assignee_display_name=assignee.capitalize(),
        membership_by_sprint={100: [model.Interval(from_=dt(2026, 1, 5), until=None)]},
    )


class _EmptyGitLabClient:
    """GitLab configured, but every fetch returns nothing -- flips
    people_available on without pulling in any MR/pipeline/deployment data."""

    def __init__(self):
        self.request_count = 0
        self.deployment_warnings = []

    def project_id(self, path):
        return 1

    def merge_requests(self, path, project_id, authors, **kw):
        return []

    def pipelines(self, path, project_id, **kw):
        return []

    def deployments(self, path, project_id, **kw):
        return []

    def coverage(self, path, project_id, **kw):
        return []


class MrAdditionsDeletionsTests(unittest.TestCase):
    """Finding #7: total_mr_additions/total_mr_deletions must come from the
    per-MR additions/deletions already present in raw["mrs"], not stay
    permanently blank."""

    def test_report_per_employee_and_team_totals_use_real_mr_diff_sums(self):
        report, raw = _build_report_and_raw()
        with tempfile.TemporaryDirectory() as tmp:
            csv_export.write_all(tmp, report, raw)
            per_employee = (Path(tmp) / "report_per_employee.csv").read_text(encoding="utf-8")
            team = (Path(tmp) / "report_team.csv").read_text(encoding="utf-8")

        header = per_employee.splitlines()[0].split(",")
        rows = {line.split(",")[0]: dict(zip(header, line.split(","))) for line in per_employee.splitlines()[1:]}
        # alice's one MR: additions=10, deletions=5 (see _FakeGitLabClient).
        self.assertEqual(rows["Alice"]["total_mr_additions"], "10")
        self.assertEqual(rows["Alice"]["total_mr_deletions"], "5")
        # carol's MR has additions/deletions=None -- stays blank, not 0.
        self.assertEqual(rows["Carol"]["total_mr_additions"], "")
        self.assertEqual(rows["Carol"]["total_mr_deletions"], "")

        team_header = team.splitlines()[0].split(",")
        team_row = dict(zip(team_header, team.splitlines()[1].split(",")))
        self.assertEqual(team_row["total_mr_additions"], "10")
        self.assertEqual(team_row["total_mr_deletions"], "5")


class JiraCycleTimeFirstInProgressTests(unittest.TestCase):
    """Finding #7: jira_cycle_time.csv.first_in_progress must come from
    IssueFlow.first_in_progress, not stay hardcoded blank."""

    def test_first_in_progress_populated_not_blank(self):
        fact = jc.IssueFacts(
            key="T-9", epic_key="", type="Story", role="", labels=[], assignee="alice",
            story_points=3.0, qa_estimation=1.0, created=dt(2026, 1, 5),
            initial_status="To Do", initial_status_id="1",
            status_history=[
                jc.RawStatusChange(at=dt(2026, 1, 6, 9), from_name="To Do", to_name="In Progress", from_id="1", to_id="2"),
                jc.RawStatusChange(at=dt(2026, 1, 7, 10), from_name="In Progress", to_name="Done", from_id="2", to_id="3"),
            ],
            sp_events=[], current_status="Done", current_status_category_key="done",
            assignee_display_name="Alice",
            membership_by_sprint={100: [model.Interval(from_=dt(2026, 1, 5), until=None)]},
        )
        client = _FakeJiraClient([fact])
        report, raw = report_data.build_combined_report_with_raw(
            client, sprint_ids=[100], history_sprint_count=1, now=dt(2026, 1, 10)
        )
        with tempfile.TemporaryDirectory() as tmp:
            csv_export.write_all(tmp, report, raw)
            text = (Path(tmp) / "jira_cycle_time.csv").read_text(encoding="utf-8")
        header = text.splitlines()[0].split(",")
        row = dict(zip(header, text.splitlines()[1].split(",")))
        self.assertEqual(row["first_in_progress"], "2026-01-06 09:00:00")


class AvgDefectRateUsesPerEmployeeAverageTests(unittest.TestCase):
    """Finding #8: report_team.csv.avg_defect_rate is the average of the
    PER-EMPLOYEE defect rates, not the flat team ratio (§C.12 item 15: only
    avg_*_fail_rate reads flat team numbers)."""

    def test_avg_defect_rate_is_not_the_flat_team_ratio(self):
        facts = [
            _typed_fact("B-1", "alice", dt(2026, 1, 7, 10), issuetype="Bug"),
            _typed_fact("B-2", "bob", dt(2026, 1, 7, 11)),
            _typed_fact("B-3", "bob", dt(2026, 1, 8, 11)),
            _typed_fact("B-4", "bob", dt(2026, 1, 9, 11)),
        ]
        client = _FakeJiraClient(facts)
        report, raw = report_data.build_combined_report_with_raw(
            client, sprint_ids=[100], history_sprint_count=1, now=dt(2026, 1, 10),
            gitlab_client_obj=_EmptyGitLabClient(), gitlab_projects=["group/app"], employees=["alice", "bob"],
        )
        # flat team ratio: 1 bug / 4 done * 100 = 25%.
        self.assertAlmostEqual(report["overview"]["defect_rate_pct"], 25.0)
        with tempfile.TemporaryDirectory() as tmp:
            csv_export.write_all(tmp, report, raw)
            text = (Path(tmp) / "report_team.csv").read_text(encoding="utf-8")
        header = text.splitlines()[0].split(",")
        row = dict(zip(header, text.splitlines()[1].split(",")))
        # average of per-employee rates: (100% + 0%) / 2 = 50% = 0.5 fraction.
        self.assertEqual(row["avg_defect_rate"], "0.5")


class DeploymentColumnConsistencyTests(unittest.TestCase):
    """Finding #10: deployment_count/deployment_failed/deployment_fail_rate
    must encode "no data" the same way within one row."""

    def test_person_with_pipelines_but_no_deployments_reads_as_measured_zero(self):
        report, raw = _build_report_and_raw()
        with tempfile.TemporaryDirectory() as tmp:
            csv_export.write_all(tmp, report, raw)
            text = (Path(tmp) / "report_per_employee.csv").read_text(encoding="utf-8")
        header = text.splitlines()[0].split(",")
        rows = {line.split(",")[0]: dict(zip(header, line.split(","))) for line in text.splitlines()[1:]}
        # bob: 3 pipelines attributed (proves attribution works for him),
        # zero deployments -- a real, measured zero, not "unknown".
        self.assertEqual(rows["Bob"]["deployment_count"], "0")
        self.assertEqual(rows["Bob"]["deployment_failed"], "0")
        self.assertEqual(rows["Bob"]["deployment_fail_rate"], "")  # 0/0 stays undefined
        # carol has neither pipelines nor deployments -- stays fully blank.
        self.assertEqual(rows["Carol"]["deployment_count"], "")
        self.assertEqual(rows["Carol"]["deployment_failed"], "")
        self.assertEqual(rows["Carol"]["deployment_fail_rate"], "")


if __name__ == "__main__":
    unittest.main()
