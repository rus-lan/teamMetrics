"""End-to-end coverage of report_data.build_report against a fake, duck-typed
Jira client — no urllib/network involvement anywhere in this file."""

import _pathfix  # noqa: F401

import dataclasses
import unittest

from team_metrics import jira_client as jc
from team_metrics import metrics as metrics_mod
from team_metrics import model, report_data

from helpers import dt, make_cfg


class FakeJiraClient:
    """Implements exactly the subset of JiraClient's interface report_data.py calls."""

    def __init__(self, sprints, board, closed_ids, active_ids, facts, statuses):
        self._sprints = sprints
        self._board = board
        self._closed_ids = closed_ids
        self._active_ids = active_ids
        self._facts = facts
        self._statuses = statuses

    def sprint(self, sprint_id):
        return self._sprints[sprint_id]

    def board(self, board_id):
        return self._board

    def closed_sprints(self, board_id):
        return [self._sprints[i] for i in self._closed_ids]

    def board_sprints(self, board_id, state=None):
        if state == "active":
            return [self._sprints[i] for i in self._active_ids]
        if state == "closed":
            return [self._sprints[i] for i in self._closed_ids]
        return list(self._sprints.values())

    def fetch_sprint_issues(self, sprint_ids, story_points_field_id=""):
        out = []
        for f in self._facts:
            filtered = {sid: f.membership_by_sprint.get(sid, []) for sid in sprint_ids}
            if not any(filtered.values()):
                continue
            out.append(dataclasses.replace(f, membership_by_sprint=filtered))
        return out

    def list_statuses(self):
        return self._statuses

    def suggest_sprints(self, query):
        return []


def _make_fact(key, sprint_id, interval, story_points, done_at=None):
    history = []
    if done_at is not None:
        history.append(jc.RawStatusChange(at=done_at, from_name="In Progress", to_name="Done", from_id="2", to_id="3"))
    return jc.IssueFacts(
        key=key, epic_key="", type="Story", role="", labels=[], assignee="",
        story_points=story_points, qa_estimation=0.0,
        created=dt(2025, 12, 1),
        initial_status="In Progress", initial_status_id="2",
        status_history=history, sp_events=[],
        current_status="Done" if done_at else "In Progress",
        current_status_category_key="done" if done_at else "indeterminate",
        membership_by_sprint={sprint_id: [interval]},
    )


def _base_sprint_facts(prefix, sprint_id, days):
    out = []
    for i, day in enumerate(days, start=1):
        interval = model.Interval(from_=dt(2025, 12, 1), until=None)
        out.append(_make_fact(f"{prefix}-{i}", sprint_id, interval, 1.0, done_at=day.replace(hour=15)))
    return out


STATUSES = [
    jc.Status(id="1", name="To Do", category_key="new"),
    jc.Status(id="2", name="In Progress", category_key="indeterminate"),
    jc.Status(id="3", name="Done", category_key="done"),
]


class BuildReportIntegrationTests(unittest.TestCase):
    def setUp(self):
        # end_at/complete_at carry an end-of-day time (18:00, like a real Jira
        # sprint), not midnight: classify_end is an EXCLUSIVE boundary, so a
        # midnight end would cut the last working day's own status changes
        # out of the classification window entirely.
        sprint98 = jc.Sprint(id=98, name="Sprint 98", state="closed", board_id=1,
                              start_at=dt(2026, 1, 5), end_at=dt(2026, 1, 9, 18), complete_at=dt(2026, 1, 9, 18))
        sprint99 = jc.Sprint(id=99, name="Sprint 99", state="closed", board_id=1,
                              start_at=dt(2026, 1, 12), end_at=dt(2026, 1, 16, 18), complete_at=dt(2026, 1, 16, 18))
        sprint100 = jc.Sprint(id=100, name="Sprint 100", state="closed", board_id=1,
                               start_at=dt(2026, 1, 19), end_at=dt(2026, 1, 23, 18), complete_at=dt(2026, 1, 23, 18))

        base98 = _base_sprint_facts("S98", 98, [dt(2026, 1, 5), dt(2026, 1, 6), dt(2026, 1, 7), dt(2026, 1, 8), dt(2026, 1, 9)])
        base99 = _base_sprint_facts("S99", 99, [dt(2026, 1, 12), dt(2026, 1, 13), dt(2026, 1, 14), dt(2026, 1, 15), dt(2026, 1, 16)])

        t1 = _make_fact("T1", 100, model.Interval(from_=dt(2025, 12, 1), until=None), 5.0, done_at=dt(2026, 1, 21, 10, 0))
        t2 = jc.IssueFacts(
            key="T2", epic_key="", type="Story", role="", labels=[], assignee="",
            story_points=3.0, qa_estimation=0.0, created=dt(2025, 12, 1),
            initial_status="In Progress", initial_status_id="2", status_history=[], sp_events=[],
            current_status="In Progress", current_status_category_key="indeterminate",
            membership_by_sprint={100: [model.Interval(from_=dt(2026, 1, 20, 9, 0), until=None)]},
        )
        t3 = jc.IssueFacts(
            key="T3", epic_key="", type="Story", role="", labels=[], assignee="",
            story_points=2.0, qa_estimation=0.0, created=dt(2025, 12, 1),
            initial_status="In Progress", initial_status_id="2", status_history=[], sp_events=[],
            current_status="In Progress", current_status_category_key="indeterminate",
            membership_by_sprint={100: [model.Interval(from_=dt(2025, 12, 1), until=dt(2026, 1, 22, 9, 0))]},
        )

        self.client = FakeJiraClient(
            sprints={98: sprint98, 99: sprint99, 100: sprint100},
            board=jc.Board(id=1, name="Team Board", type="scrum"),
            closed_ids=[98, 99, 100],
            active_ids=[],
            facts=base98 + base99 + [t1, t2, t3],
            statuses=STATUSES,
        )

        self.report = report_data.build_report(
            self.client, sprint_ids=[100], history_sprint_count=5, target_items=7, now=dt(2026, 1, 24)
        )

    def _target_entry(self):
        return next(s for s in self.report["sprints"] if s["payload"]["sprint"]["id"] == 100)

    def test_board_identity(self):
        self.assertEqual(self.report["board"], {"id": 1, "name": "Team Board"})

    def test_all_three_sprints_present_ascending_by_start_and_target_flagged(self):
        ids_and_targets = [(s["payload"]["sprint"]["id"], s["target"]) for s in self.report["sprints"]]
        self.assertEqual(ids_and_targets, [(98, False), (99, False), (100, True)])

    def test_target_sprint_metrics(self):
        m = self._target_entry()["payload"]["metrics"]
        self.assertEqual(m["committed_sp"], 7.0)
        self.assertEqual(m["committed_items"], 2)
        self.assertEqual(m["delivered_sp"], 5.0)
        self.assertEqual(m["delivered_items"], 1)
        self.assertEqual(m["scope_added_sp"], 3.0)
        self.assertEqual(m["scope_removed_sp"], 2.0)
        self.assertAlmostEqual(m["performance_pct"], 5 / 7 * 100)
        self.assertAlmostEqual(m["load_pct"], 140.0)
        self.assertAlmostEqual(m["scope_change_pct"], 5 / 7 * 100)
        self.assertAlmostEqual(m["closure_pct_items"], 50.0)
        self.assertAlmostEqual(m["closure_pct_sp"], 62.5)

    def test_board_kpi_over_base_sprints_only(self):
        kpi = self.report["kpi"]
        self.assertEqual(kpi["velocity_sma5_sp"], 5.0)
        self.assertEqual(kpi["avg_performance_pct"], 100.0)
        self.assertEqual(kpi["avg_scope_change_pct"], 0.0)
        self.assertEqual(kpi["avg_closure_pct_items"], 100.0)
        self.assertEqual(kpi["avg_closure_pct_sp"], 100.0)
        self.assertEqual(kpi["throughput_avg_items"], 5.0)
        self.assertTrue(kpi["forecast_available"])  # exactly 10 non-zero daily points

    def test_baseline_short_warning_present_no_spurious_division_warning(self):
        self.assertIn(model.WARN_BASELINE_SHORT, self.report["warnings"])
        self.assertNotIn(model.WARN_DIVISION_BY_ZERO, self.report["warnings"])
        self.assertNotIn(model.WARN_SPRINT_ACTIVE_PARTIAL, self.report["warnings"])

    def test_forecast_succeeds_with_enough_nonzero_points(self):
        # Forecast's own population (B1) is the board's closed sprints
        # ordered by end_at, capped at 5, target-inclusive — so it's sprints
        # 98+99+100 here (all three are closed), NOT just the 2 non-target
        # base sprints --history would show on the board table.
        self.assertIsNone(self.report["forecast_error"])
        forecast = self.report["forecast"]
        self.assertEqual(forecast["sample_sprints"], 3)
        self.assertEqual(forecast["sample_days"], 15)
        self.assertEqual(forecast["target_items"], 7)
        self.assertEqual(len(forecast["percentiles"]), 3)

    def test_heatmap_and_burndown_built_for_target_sprint_only(self):
        self.assertEqual(len(self.report["heatmaps"]), 1)
        self.assertEqual(len(self.report["burndowns"]), 1)

        hm = self.report["heatmaps"][0]
        self.assertEqual(hm["sprint_id"], 100)
        self.assertEqual(hm["days"], ["2026-01-19", "2026-01-20", "2026-01-21", "2026-01-22", "2026-01-23"])
        rows_by_key = {r["issue_key"]: r["cells"] for r in hm["rows"]}
        # T1: delivered Jan 21 10:00 -> "In Progress" through Jan 20, "Done" from Jan 21.
        self.assertEqual(
            [(c["date"], c["status"]) for c in rows_by_key["T1"]],
            [("2026-01-19", "In Progress"), ("2026-01-20", "In Progress"),
             ("2026-01-21", "Done"), ("2026-01-22", "Done"), ("2026-01-23", "Done")],
        )
        # T2: joins Jan 20 09:00 -> no Jan 19 cell at all (not yet a member).
        self.assertEqual([c["date"] for c in rows_by_key["T2"]], ["2026-01-20", "2026-01-21", "2026-01-22", "2026-01-23"])
        # T3: removed Jan 22 09:00 -> last cell is Jan 21, none after.
        self.assertEqual([c["date"] for c in rows_by_key["T3"]], ["2026-01-19", "2026-01-20", "2026-01-21"])

        bd = self.report["burndowns"][0]
        self.assertEqual(bd["sprint_id"], 100)
        points_by_date = {p["date"]: p for p in bd["points"]}
        # Jan 19: only T1 (5sp) + T3 (2sp) are members yet (T2 joins Jan 20).
        self.assertEqual(points_by_date["2026-01-19"]["remaining_items"], 2)
        self.assertEqual(points_by_date["2026-01-19"]["remaining_sp"], 7.0)
        self.assertEqual(points_by_date["2026-01-19"]["ideal_sp"], 7.0)
        # Jan 20: T2 has joined -> all three members, none delivered yet.
        self.assertEqual(points_by_date["2026-01-20"]["remaining_items"], 3)
        self.assertEqual(points_by_date["2026-01-20"]["remaining_sp"], 10.0)
        # Jan 21: T1 (5sp) becomes Done and drops out of "remaining".
        self.assertEqual(points_by_date["2026-01-21"]["remaining_items"], 2)
        self.assertEqual(points_by_date["2026-01-21"]["remaining_sp"], 5.0)
        # Jan 23 (last day): ideal line reaches exactly 0.
        self.assertEqual(points_by_date["2026-01-23"]["ideal_sp"], 0.0)

    def test_export_tables(self):
        export = self.report["export"]
        board = export["board_table"]
        self.assertEqual(
            board["header"],
            [
                "sprint", "state", "start", "end",
                "committed_sp", "delivered_sp", "added_sp", "removed_sp", "estimation_change_sp",
                "performance_pct", "load_pct", "scope_change_pct",
                "velocity_sp", "sma5_sp",
                "throughput_count", "closure_rate_count_pct", "closure_rate_sp_pct",
            ],
        )
        self.assertEqual(len(board["rows"]), 3)
        # Sprint 100 (the target) — every column value, not just row count.
        self.assertEqual(
            board["rows"][2],
            ["Sprint 100", "closed", "2026-01-19", "2026-01-23",
             "7", "5", "3", "2", "0", "71.42857142857143", "140", "71.42857142857143", "5", "5", "1", "50", "62.5"],
        )

        heatmap = export["heatmap_table"]
        self.assertEqual(
            heatmap["header"],
            [
                "Epic", "Issue", "Story Points", "QA Estimation", "Роль", "Labels",
                "Before", "100 / 2026-01-19 / пн", "100 / 2026-01-20 / вт", "100 / 2026-01-21 / ср",
                "100 / 2026-01-22 / чт", "100 / 2026-01-23 / пт", "End",
            ],
        )
        rows_by_key = {r[1]: r for r in heatmap["rows"]}
        self.assertEqual(
            rows_by_key["T1"],
            ["", "T1", "5", "0", "", "", "In Progress", "In Progress", "In Progress", "Done", "Done", "Done", "Done"],
        )
        self.assertEqual(
            rows_by_key["T3"],
            ["", "T3", "2", "0", "", "", "In Progress", "In Progress", "In Progress", "In Progress", "", "", "In Progress"],
        )

        # heatmap_csv: UTF-8 BOM, `;` separator, same column order as the table.
        self.assertTrue(export["heatmap_csv"].startswith("﻿"))
        csv_lines = export["heatmap_csv"].lstrip("﻿").rstrip("\n").split("\n")
        self.assertEqual(csv_lines[0], ";".join(heatmap["header"]))

    def test_params_echo(self):
        params = self.report["params"]
        self.assertEqual(params["board_id"], 1)
        self.assertEqual(params["history_sprint_count"], 5)
        self.assertEqual(params["target_items_resolved"], 7)
        self.assertEqual(params["sprint_ids"], [100])

    def test_board_id_mismatch_raises(self):
        with self.assertRaises(report_data.ReportError):
            report_data.build_report(self.client, sprint_ids=[100], board_id_override=999, now=dt(2026, 1, 24))

    def test_mutually_exclusive_target_selectors_required(self):
        with self.assertRaises(report_data.ReportError):
            report_data.build_report(self.client, sprint_ids=[], sprint_names=[], now=dt(2026, 1, 24))
        with self.assertRaises(report_data.ReportError):
            report_data.build_report(self.client, sprint_ids=[100], sprint_names=["Sprint 100"], now=dt(2026, 1, 24))


class ResolveTargetItemsTests(unittest.TestCase):
    """Default target_items = remaining items of the board's active sprint (SPEC §1.2)."""

    def _client_and_cfg(self, facts):
        active = jc.Sprint(id=101, name="Active", state="active", board_id=1,
                            start_at=dt(2026, 1, 26), end_at=dt(2026, 1, 30), complete_at=model.ZERO_TIME)
        client = FakeJiraClient(
            sprints={101: active}, board=jc.Board(id=1, name="B", type="scrum"),
            closed_ids=[], active_ids=[101], facts=facts, statuses=STATUSES,
        )
        cfg = make_cfg(jira_status_categories={"In Progress": "indeterminate", "Done": "done"})
        return client, cfg

    def test_remaining_items_formula(self):
        facts = [
            _make_fact("A1", 101, model.Interval(from_=dt(2025, 12, 1), until=None), 1.0),  # committed, not delivered
            _make_fact("A2", 101, model.Interval(from_=dt(2025, 12, 1), until=None), 1.0, done_at=dt(2026, 1, 27)),  # committed+delivered
            jc.IssueFacts(  # added after start, not delivered
                key="A3", epic_key="", type="Story", role="", labels=[], assignee="",
                story_points=1.0, qa_estimation=0.0, created=dt(2025, 12, 1),
                initial_status="In Progress", initial_status_id="2", status_history=[], sp_events=[],
                current_status="In Progress", current_status_category_key="indeterminate",
                membership_by_sprint={101: [model.Interval(from_=dt(2026, 1, 27, 9, 0), until=None)]},
            ),
            jc.IssueFacts(  # committed AND (later) removed — counts in both sets, SPEC §3.3
                key="A4", epic_key="", type="Story", role="", labels=[], assignee="",
                story_points=1.0, qa_estimation=0.0, created=dt(2025, 12, 1),
                initial_status="In Progress", initial_status_id="2", status_history=[], sp_events=[],
                current_status="In Progress", current_status_category_key="indeterminate",
                membership_by_sprint={101: [model.Interval(from_=dt(2025, 12, 1), until=dt(2026, 1, 27, 9, 0))]},
            ),
        ]
        client, cfg = self._client_and_cfg(facts)
        remaining = report_data._resolve_target_items(
            all_payloads=[], client=client, board_id=1, cfg=cfg, id_to_name={}, now=dt(2026, 1, 28)
        )
        # committed(3: A1,A2,A4) + added(1: A3) - removed(1: A4) - delivered(1: A2) = 2
        self.assertEqual(remaining, 2)

    def test_remaining_items_clamped_at_zero_when_formula_goes_negative(self):
        facts = [
            _make_fact("B1", 101, model.Interval(from_=dt(2025, 12, 1), until=None), 1.0, done_at=dt(2026, 1, 27)),
            _make_fact("B2", 101, model.Interval(from_=dt(2025, 12, 1), until=None), 1.0, done_at=dt(2026, 1, 27)),
        ]
        client, cfg = self._client_and_cfg(facts)
        remaining = report_data._resolve_target_items(
            all_payloads=[], client=client, board_id=1, cfg=cfg, id_to_name={}, now=dt(2026, 1, 28)
        )
        # committed(2) + added(0) - removed(0) - delivered(2) = 0 — never negative
        self.assertEqual(remaining, 0)

    def test_no_active_sprint_returns_none(self):
        client = FakeJiraClient(sprints={}, board=jc.Board(id=1, name="B", type="scrum"),
                                 closed_ids=[], active_ids=[], facts=[], statuses=STATUSES)
        cfg = make_cfg()
        remaining = report_data._resolve_target_items(
            all_payloads=[], client=client, board_id=1, cfg=cfg, id_to_name={}, now=dt(2026, 1, 28)
        )
        self.assertIsNone(remaining)


class CanonicalStatusNameTests(unittest.TestCase):
    def test_falls_back_to_historical_when_id_missing_from_catalog(self):
        self.assertEqual(report_data._canonical_status_name("", "Old Name", {}), "Old Name")
        self.assertEqual(report_data._canonical_status_name("9", "Old Name", {}), "Old Name")

    def test_renamed_status_resolves_via_stable_id(self):
        id_to_name = {"9": "Готово"}  # status renamed in Jira but ID kept
        self.assertEqual(report_data._canonical_status_name("9", "Done", id_to_name), "Готово")


def _sprint(id_, start, end=None, complete=None, state="closed"):
    end = end if end is not None else start
    complete = complete if complete is not None else end
    return jc.Sprint(id=id_, name=f"Sprint {id_}", state=state, board_id=1, start_at=start, end_at=end, complete_at=complete)


class PickBaseSprintsTests(unittest.TestCase):
    def test_excludes_targets_and_caps_at_limit_most_recent_by_start(self):
        closed = [_sprint(i, dt(2026, 1, i)) for i in range(1, 8)]  # ids 1..7, ascending start
        targets = [closed[5]]  # id=6
        picked = report_data._pick_base_sprints(closed, targets, limit=3)
        self.assertEqual([s.id for s in picked], [4, 5, 7])  # 3 most recent excluding id=6, ascending

    def test_limit_larger_than_available_returns_all_non_target_ascending(self):
        closed = [_sprint(i, dt(2026, 1, i)) for i in range(1, 4)]
        picked = report_data._pick_base_sprints(closed, targets=[], limit=10)
        self.assertEqual([s.id for s in picked], [1, 2, 3])


class PickForecastSprintsTests(unittest.TestCase):
    """B1: forecast's own population — closed sprints ordered by end_at,
    capped at MAX_BASE_SPRINTS, target-inclusive (unlike _pick_base_sprints)."""

    def test_all_included_when_within_limit_ordered_by_end_at_ascending(self):
        closed = [_sprint(1, dt(2026, 1, 1), end=dt(2026, 1, 9)),
                  _sprint(2, dt(2026, 1, 12), end=dt(2026, 1, 16)),
                  _sprint(3, dt(2026, 1, 19), end=dt(2026, 1, 23))]
        picked = report_data._pick_forecast_sprints(closed, limit=5)
        self.assertEqual([s.id for s in picked], [1, 2, 3])

    def test_caps_at_limit_taking_most_recent_by_end_at(self):
        closed = [_sprint(i, dt(2026, 1, i), end=dt(2026, 1, i)) for i in range(1, 8)]
        picked = report_data._pick_forecast_sprints(closed, limit=5)
        self.assertEqual([s.id for s in picked], [3, 4, 5, 6, 7])

    def test_default_limit_is_max_base_sprints(self):
        closed = [_sprint(i, dt(2026, 1, i), end=dt(2026, 1, i)) for i in range(1, 8)]
        picked = report_data._pick_forecast_sprints(closed)
        self.assertEqual(len(picked), report_data.MAX_BASE_SPRINTS)


class VelocityHistoryForTests(unittest.TestCase):
    def _velocity_payload(self, velocity_sp):
        m = metrics_mod.Metrics(velocity_sp=velocity_sp)
        sprint_meta = metrics_mod.SprintMeta(
            id=1, name="S", board_id=1, board_name="B", state="closed",
            start_at=dt(2026, 1, 1), end_at=dt(2026, 1, 5), complete_at=dt(2026, 1, 5), working_days=[],
        )
        return metrics_mod.Payload(schema_version=1, sprint=sprint_meta, issues=[], sets=model.Sets(), metrics=m, throughput_daily=[])

    def _rs(self, id_, target):
        return report_data._ResolvedSprint(sprint=_sprint(id_, dt(2026, 1, id_)), target=target)

    def test_only_strictly_preceding_non_target_sprints_included(self):
        all_sprints = [self._rs(1, False), self._rs(2, True), self._rs(3, False)]
        payloads = {1: self._velocity_payload(10.0), 3: self._velocity_payload(30.0)}
        out = report_data._velocity_history_for(all_sprints, 2, payloads)
        self.assertEqual(out, [10.0])

    def test_target_sprint_never_contributes_even_if_preceding(self):
        all_sprints = [self._rs(1, True), self._rs(2, False)]
        payloads = {1: self._velocity_payload(999.0), 2: self._velocity_payload(20.0)}
        out = report_data._velocity_history_for(all_sprints, 1, payloads)
        self.assertEqual(out, [])  # index 0 is a target -> excluded

    def test_caps_at_max_base_sprints_keeping_most_recent(self):
        all_sprints = [self._rs(i, False) for i in range(1, 8)]  # 7 base sprints
        payloads = {i: self._velocity_payload(float(i)) for i in range(1, 8)}
        out = report_data._velocity_history_for(all_sprints, len(all_sprints), payloads)
        self.assertEqual(out, [3.0, 4.0, 5.0, 6.0, 7.0])  # last 5 of the 7 preceding


class DayHeaderTests(unittest.TestCase):
    def test_known_weekday_abbreviations(self):
        self.assertEqual(report_data._day_header(100, "2026-01-19"), "100 / 2026-01-19 / пн")  # Monday
        self.assertEqual(report_data._day_header(100, "2026-01-24"), "100 / 2026-01-24 / сб")  # Saturday
        self.assertEqual(report_data._day_header(100, "2026-01-25"), "100 / 2026-01-25 / вс")  # Sunday

    def test_malformed_date_omits_weekday_but_keeps_prefix(self):
        self.assertEqual(report_data._day_header(100, "not-a-date"), "100 / not-a-date / ")


class FormatNumberTests(unittest.TestCase):
    def test_integral_float_renders_without_decimal_point(self):
        self.assertEqual(report_data._format_number(5.0), "5")
        self.assertEqual(report_data._format_number(-3.0), "-3")
        self.assertEqual(report_data._format_number(0.0), "0")

    def test_fractional_value_is_shortest_round_trip(self):
        self.assertEqual(report_data._format_number(5 / 7 * 100), "71.42857142857143")


class CsvGuardTests(unittest.TestCase):
    def test_csv_guard_prefixes_tab_for_each_formula_leading_char(self):
        for ch in "=+-@":
            self.assertEqual(report_data._csv_guard(f"{ch}cmd"), f"\t{ch}cmd")

    def test_csv_guard_leaves_normal_text_alone(self):
        self.assertEqual(report_data._csv_guard("Sprint 1"), "Sprint 1")

    def test_csv_guard_empty_string_unchanged(self):
        self.assertEqual(report_data._csv_guard(""), "")

    def test_sanitize_csv_replaces_separator_and_newlines(self):
        # \r and \n are each replaced with their own space independently.
        self.assertEqual(report_data._sanitize_csv("a;b\r\nc"), "a,b  c")

    def test_sanitize_csv_also_applies_the_formula_guard(self):
        self.assertEqual(report_data._sanitize_csv("=SUM(A1)"), "\t=SUM(A1)")

    def test_sanitize_csv_leaves_plain_text_alone(self):
        self.assertEqual(report_data._sanitize_csv("plain text"), "plain text")


class SafeCellTests(unittest.TestCase):
    """M2: board_table's formula-injection guard — mirrors Go's safeCell."""

    def test_prefixes_single_quote_for_each_dangerous_leading_char(self):
        for ch in "=+-@\t\r":
            self.assertEqual(report_data._safe_cell(f"{ch}x"), f"'{ch}x")

    def test_leaves_normal_text_alone(self):
        self.assertEqual(report_data._safe_cell("Sprint 1"), "Sprint 1")

    def test_empty_string_unchanged(self):
        self.assertEqual(report_data._safe_cell(""), "")

    def test_negative_number_string_also_gets_guarded(self):
        # Uniform application, same as Go: a negative numeric cell's leading
        # '-' is guarded too, not just text columns.
        self.assertEqual(report_data._safe_cell("-5"), "'-5")


class MissingSprintDatesNoTracebackTests(unittest.TestCase):
    """M1: a future/active sprint with no startDate/endDate must never crash
    build_report — the missing value decodes to model.ZERO_TIME, not None."""

    def test_target_sprint_with_no_dates_builds_without_crashing(self):
        sprint = jc.Sprint(
            id=200, name="Future Sprint", state="active", board_id=1,
            start_at=model.ZERO_TIME, end_at=model.ZERO_TIME, complete_at=model.ZERO_TIME,
        )
        client = FakeJiraClient(
            sprints={200: sprint}, board=jc.Board(id=1, name="Board", type="scrum"),
            closed_ids=[], active_ids=[200], facts=[], statuses=STATUSES,
        )
        report = report_data.build_report(client, sprint_ids=[200], now=dt(2026, 6, 1))

        entry = next(s for s in report["sprints"] if s["payload"]["sprint"]["id"] == 200)
        self.assertEqual(len(entry["payload"]["sprint"]["working_days"]), 1)
        board_row = report["export"]["board_table"]["rows"][0]
        self.assertEqual(board_row[2], "0001-01-01")  # start
        self.assertEqual(board_row[3], "0001-01-01")  # end


class ResolveTargetItemsAugmentedCfgTests(unittest.TestCase):
    """M3: the on-demand active-sprint fetch must thread story_points_field_id
    and augment cfg with the fetched issues' own current status category —
    same as the main pass (translate.go:31-38)."""

    def test_story_points_field_id_is_forwarded_to_the_on_demand_fetch(self):
        active = jc.Sprint(id=301, name="Active", state="active", board_id=1,
                            start_at=dt(2026, 1, 26), end_at=dt(2026, 1, 30), complete_at=model.ZERO_TIME)
        recorded = {}

        class RecordingClient(FakeJiraClient):
            def fetch_sprint_issues(self, sprint_ids, story_points_field_id=""):
                recorded["story_points_field_id"] = story_points_field_id
                return super().fetch_sprint_issues(sprint_ids, story_points_field_id=story_points_field_id)

        client = RecordingClient(
            sprints={301: active}, board=jc.Board(id=1, name="B", type="scrum"),
            closed_ids=[], active_ids=[301], facts=[], statuses=STATUSES,
        )
        report_data._resolve_target_items(
            all_payloads=[], client=client, board_id=1, cfg=make_cfg(), id_to_name={},
            now=dt(2026, 1, 28), story_points_field_id="customfield_999",
        )
        self.assertEqual(recorded["story_points_field_id"], "customfield_999")

    def test_status_only_known_via_the_fetched_issue_is_folded_into_cfg(self):
        active = jc.Sprint(id=302, name="Active", state="active", board_id=1,
                            start_at=dt(2026, 1, 26), end_at=dt(2026, 1, 30), complete_at=model.ZERO_TIME)
        fact = jc.IssueFacts(
            key="X-1", epic_key="", type="Story", role="", labels=[], assignee="",
            story_points=1.0, qa_estimation=0.0, created=dt(2026, 1, 20),
            initial_status="Released", initial_status_id="9", status_history=[], sp_events=[],
            current_status="Released", current_status_category_key="done",
            membership_by_sprint={302: [model.Interval(from_=dt(2026, 1, 20), until=None)]},
        )
        client = FakeJiraClient(
            sprints={302: active}, board=jc.Board(id=1, name="B", type="scrum"),
            closed_ids=[], active_ids=[302], facts=[fact], statuses=STATUSES,
        )
        # cfg carries no knowledge of "Released" at all — only the on-demand
        # fetch's own IssueFacts.current_status_category_key does.
        cfg = make_cfg()
        remaining = report_data._resolve_target_items(
            all_payloads=[], client=client, board_id=1, cfg=cfg, id_to_name={}, now=dt(2026, 1, 28),
        )
        # committed(1) - delivered(1, via the augmented "Released"->done) = 0.
        # Without the M3 fix this would come out 1 (spuriously not delivered).
        self.assertEqual(remaining, 0)


class FakeGitLabClient:
    """Duck-typed stub matching gitlab_client.GitLabClient's public surface —
    only what glc.fetch_team_data() calls. No network involvement."""

    def __init__(self, project_ids, mrs_by_project=None, pipelines_by_project=None, deployments_by_project=None, coverage_by_project=None):
        self._project_ids = project_ids
        self._mrs = mrs_by_project or {}
        self._pipelines = pipelines_by_project or {}
        self._deployments = deployments_by_project or {}
        self._coverage = coverage_by_project or {}
        self.mr_calls = []  # [(path, authors_tuple, window)]
        self.pipelines_calls = []  # [fetch_pipeline_user, ...]
        # fetch_team_data() reads client.request_count before and after its
        # own call and reports the delta — one "request" per stubbed method
        # call is a fine duck-typed stand-in since this fake never does real
        # HTTP.
        self.request_count = 0

    def project_id(self, path):
        self.request_count += 1
        return self._project_ids.get(path)

    def merge_requests(self, path, project_id, authors, *, states=(), window=None, errors=None, fetch_mr_details=True):
        self.mr_calls.append((path, tuple(authors), window, fetch_mr_details))
        self.request_count += 1
        if not authors:
            return []
        return list(self._mrs.get(path, []))

    def pipelines(self, path, project_id, *, window=None, fetch_pipeline_user=True):
        self.pipelines_calls.append(fetch_pipeline_user)
        self.request_count += 1
        return list(self._pipelines.get(path, []))

    def deployments(self, path, project_id, *, window=None):
        self.request_count += 1
        return list(self._deployments.get(path, []))

    def coverage(self, path, project_id, *, window=None):
        self.request_count += 1
        return list(self._coverage.get(path, []))


class BuildCombinedReportTests(unittest.TestCase):
    """Part 3 wiring: one Jira pass feeds sprint-metrics + personal + engineering."""

    def setUp(self):
        # One closed target sprint; one issue, delivered inside it.
        # story_points=8.0 is the CURRENT field value (as if re-estimated
        # after the sprint), while its sp_events replay to 5.0 as of the end
        # of its sprint membership — the two tabs must show DIFFERENT
        # numbers for the same issue (the "critical trap").
        self.target = jc.Sprint(id=500, name="Sprint 500", state="closed", board_id=9,
                                 start_at=dt(2026, 2, 2), end_at=dt(2026, 2, 6, 18), complete_at=dt(2026, 2, 6, 18))
        self.fact_alice = jc.IssueFacts(
            key="P-1", epic_key="", type="Story", role="", labels=[], assignee="alice",
            story_points=8.0, qa_estimation=0.0, created=dt(2026, 1, 1),
            initial_status="In Progress", initial_status_id="2",
            status_history=[jc.RawStatusChange(at=dt(2026, 2, 4, 10, 0), from_name="In Progress", to_name="Done", from_id="2", to_id="3")],
            sp_events=[jc.RawSPChange(at=dt(2026, 1, 20), from_value=3.0, to_value=5.0)],
            current_status="Done", current_status_category_key="done",
            resolutiondate=dt(2026, 2, 4, 10, 0),
            membership_by_sprint={500: [model.Interval(from_=dt(2026, 1, 15), until=None)]},
        )
        self.client = FakeJiraClient(
            sprints={500: self.target}, board=jc.Board(id=9, name="Team Board", type="scrum"),
            closed_ids=[500], active_ids=[], facts=[self.fact_alice], statuses=STATUSES,
        )
        self.gitlab = FakeGitLabClient(
            project_ids={"group/proj": 42},
            mrs_by_project={"group/proj": [
                {"author": "alice", "state": "merged", "created_at": "2026-02-03T00:00:00Z", "merged_at": "2026-02-04T00:00:00Z"},
            ]},
            pipelines_by_project={"group/proj": [
                {"user_username": "alice", "status": "success"},
                {"user_username": "alice", "status": "failed"},
            ]},
        )

    def test_gitlab_absent_degrades_to_unavailable_never_a_hard_failure(self):
        report = report_data.build_combined_report(self.client, sprint_ids=[500], now=dt(2026, 2, 7), gitlab_client_obj=None)
        self.assertFalse(report["personal"]["available"])
        self.assertIsNone(report["personal"]["data"])
        self.assertFalse(report["engineering"]["available"])
        self.assertIsNone(report["engineering"]["data"])
        self.assertEqual(len(report["semantics_notes"]), 4)
        self.assertIn("board", report)  # tab 1 still fully built

    def test_gitlab_present_builds_personal_and_engineering(self):
        report = report_data.build_combined_report(
            self.client, sprint_ids=[500], now=dt(2026, 2, 7),
            gitlab_client_obj=self.gitlab, gitlab_projects=["group/proj"], employees=["alice", "bob"],
        )
        self.assertTrue(report["personal"]["available"])
        self.assertTrue(report["engineering"]["available"])
        people = {p["user"]: p for p in report["personal"]["data"]["people"]}
        self.assertIn("alice", people)
        # New per-person fields (rework_tasks/rework_share/issue_count) flow
        # straight through from personal_metrics()'s own dict — no wiring
        # needed beyond calling it. pipeline_success_rate does need explicit
        # wiring (build_personal_report() has no pipelines= parameter), so
        # it is asserted precisely: 1 success of 2 pipelines for alice.
        self.assertEqual(people["alice"]["issue_count"], 1)
        self.assertEqual(people["alice"]["rework_tasks"], 0)
        self.assertIn("rework_share", people["alice"])
        self.assertEqual(people["alice"]["pipeline_success_rate"], 0.5)

    def test_pipeline_success_rate_is_none_without_pipeline_data(self):
        gitlab_no_pipelines = FakeGitLabClient(
            project_ids={"group/proj": 42},
            mrs_by_project={"group/proj": [
                {"author": "alice", "state": "merged", "created_at": "2026-02-03T00:00:00Z", "merged_at": "2026-02-04T00:00:00Z"},
            ]},
        )
        report = report_data.build_combined_report(
            self.client, sprint_ids=[500], now=dt(2026, 2, 7),
            gitlab_client_obj=gitlab_no_pipelines, gitlab_projects=["group/proj"], employees=["alice"],
        )
        people = {p["user"]: p for p in report["personal"]["data"]["people"]}
        self.assertIsNone(people["alice"]["pipeline_success_rate"])

    def test_personal_uses_current_story_points_sprint_tab_uses_end_of_membership(self):
        report = report_data.build_combined_report(
            self.client, sprint_ids=[500], now=dt(2026, 2, 7),
            gitlab_client_obj=self.gitlab, gitlab_projects=["group/proj"], employees=["alice"],
        )
        sprint_entry = next(s for s in report["sprints"] if s["payload"]["sprint"]["id"] == 500)
        p1 = next(i for i in sprint_entry["payload"]["issues"] if i["key"] == "P-1")
        people = {p["user"]: p for p in report["personal"]["data"]["people"]}
        self.assertEqual(p1["story_points"], 5.0)  # sprint tab: end-of-membership
        self.assertEqual(people["alice"]["story_points_total"], 8.0)  # personal: current value

    def test_no_personal_skips_the_mr_fetch_but_keeps_engineering(self):
        report = report_data.build_combined_report(
            self.client, sprint_ids=[500], now=dt(2026, 2, 7),
            gitlab_client_obj=self.gitlab, gitlab_projects=["group/proj"], employees=["alice", "bob"],
            include_personal=False,
        )
        self.assertFalse(report["personal"]["available"])
        self.assertEqual(report["personal"]["reason"], "--no-personal")
        self.assertTrue(report["engineering"]["available"])
        self.assertEqual(self.gitlab.mr_calls[0][1], ())  # no authors -> no MR fetch

    def test_window_derived_from_target_sprint_dates_not_end_of_day_adjusted(self):
        report_data.build_combined_report(
            self.client, sprint_ids=[500], now=dt(2026, 2, 7),
            gitlab_client_obj=self.gitlab, gitlab_projects=["group/proj"], employees=["alice"],
        )
        window = self.gitlab.mr_calls[0][2]
        self.assertEqual(window.start, dt(2026, 2, 2))
        self.assertEqual(window.end, dt(2026, 2, 6, 18))  # raw sprint end_at, no pre-applied end-of-day

    def test_gitlab_fetch_issues_surfaced_from_fetch_team_data(self):
        report = report_data.build_combined_report(
            self.client, sprint_ids=[500], now=dt(2026, 2, 7),
            gitlab_client_obj=self.gitlab, gitlab_projects=["group/proj"], employees=["alice"],
        )
        self.assertIn("skipped_projects", report["gitlab_fetch_issues"])
        self.assertIn("mr_fetch_errors", report["gitlab_fetch_issues"])

    def test_request_count_and_opt_out_flags_echoed_into_params_when_gitlab_used(self):
        report = report_data.build_combined_report(
            self.client, sprint_ids=[500], now=dt(2026, 2, 7),
            gitlab_client_obj=self.gitlab, gitlab_projects=["group/proj"], employees=["alice"],
        )
        # 1 project_id + 1 merge_requests + 1 pipelines + 1 deployments + 1
        # coverage call against the single stubbed project -> 5.
        self.assertEqual(report["params"]["gitlab_request_count"], 5)
        self.assertIs(report["params"]["gitlab_fetch_mr_details"], True)
        self.assertIs(report["params"]["gitlab_fetch_pipeline_user"], True)

    def test_opt_out_flags_are_threaded_through_to_the_gitlab_client_calls(self):
        report_data.build_combined_report(
            self.client, sprint_ids=[500], now=dt(2026, 2, 7),
            gitlab_client_obj=self.gitlab, gitlab_projects=["group/proj"], employees=["alice"],
            fetch_mr_details=False, fetch_pipeline_user=False,
        )
        self.assertEqual(self.gitlab.mr_calls[0][3], False)  # fetch_mr_details reached merge_requests()
        self.assertEqual(self.gitlab.pipelines_calls, [False])  # fetch_pipeline_user reached pipelines()

    def test_params_gitlab_fields_are_none_when_gitlab_not_configured(self):
        report = report_data.build_combined_report(
            self.client, sprint_ids=[500], now=dt(2026, 2, 7), gitlab_client_obj=None,
        )
        self.assertIsNone(report["params"]["gitlab_request_count"])
        self.assertIsNone(report["params"]["gitlab_fetch_mr_details"])
        self.assertIsNone(report["params"]["gitlab_fetch_pipeline_user"])

    def test_semantics_notes_are_nonempty_russian_prose(self):
        report = report_data.build_combined_report(
            self.client, sprint_ids=[500], now=dt(2026, 2, 7), gitlab_client_obj=None,
        )
        for note in report["semantics_notes"]:
            self.assertGreater(len(note), 20)
            self.assertTrue(any(ord(c) > 127 for c in note))  # contains Cyrillic


if __name__ == "__main__":
    unittest.main()
