"""Tests for render_html.py — the self-contained HTML report renderer.

No network involvement anywhere: every fixture here is a duck-typed fake
Jira/GitLab client, same pattern as test_report_data.py.
"""

import _pathfix  # noqa: F401

import dataclasses
import html
import re
import unittest
from html.parser import HTMLParser

from jira_metrics import jira_client as jc
from jira_metrics import model, report_data
from jira_metrics import render_html as rh

from helpers import dt

TOKEN_RE = re.compile(r"\{\{[A-Z0-9_]+\}\}")

STATUSES = [
    jc.Status(id="1", name="To Do", category_key="new"),
    jc.Status(id="2", name="In Progress", category_key="indeterminate"),
    jc.Status(id="3", name="Done", category_key="done"),
]


# --------------------------------------------------------------------------
# Fake clients (self-contained — do not depend on test_report_data.py)
# --------------------------------------------------------------------------


class FakeJiraClient:
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


class FakeGitLabClient:
    def __init__(self, project_ids, mrs_by_project=None, pipelines_by_project=None, deployments_by_project=None, coverage_by_project=None):
        self._project_ids = project_ids
        self._mrs = mrs_by_project or {}
        self._pipelines = pipelines_by_project or {}
        self._deployments = deployments_by_project or {}
        self._coverage = coverage_by_project or {}
        self.mr_calls = []

    def project_id(self, path):
        return self._project_ids.get(path)

    def merge_requests(self, path, project_id, authors, *, states=(), window=None, errors=None):
        self.mr_calls.append((path, tuple(authors), window))
        if not authors:
            return []
        return list(self._mrs.get(path, []))

    def pipelines(self, path, project_id, *, window=None):
        return list(self._pipelines.get(path, []))

    def deployments(self, path, project_id, *, window=None):
        return list(self._deployments.get(path, []))

    def coverage(self, path, project_id, *, window=None):
        return list(self._coverage.get(path, []))


def _make_fact(key, sprint_id, interval, story_points, done_at=None, assignee="", labels=None, issue_type="Story"):
    history = []
    if done_at is not None:
        history.append(jc.RawStatusChange(at=done_at, from_name="In Progress", to_name="Done", from_id="2", to_id="3"))
    return jc.IssueFacts(
        key=key, epic_key="", type=issue_type, role="", labels=labels or [], assignee=assignee,
        story_points=story_points, qa_estimation=0.0,
        created=dt(2025, 12, 1),
        initial_status="In Progress", initial_status_id="2",
        status_history=history, sp_events=[],
        current_status="Done" if done_at else "In Progress",
        current_status_category_key="done" if done_at else "indeterminate",
        membership_by_sprint={sprint_id: [interval]},
    )


def _base_sprint_facts(prefix, sprint_id, days, story_points=6.0):
    out = []
    for i, day in enumerate(days, start=1):
        interval = model.Interval(from_=dt(2025, 12, 1), until=None)
        out.append(_make_fact(f"{prefix}-{i}", sprint_id, interval, story_points, done_at=day.replace(hour=15)))
    return out


def build_multi_sprint_report(*, with_gitlab=True, target_items=None):
    """4 closed base sprints + 1 closed target sprint, enough throughput
    history for a real forecast, and (optionally) a small GitLab-derived
    personal/engineering half — a report shaped enough to exercise the KPI
    good/warn/bad thresholds and every chart."""
    base_days = {
        90: [dt(2025, 12, 1), dt(2025, 12, 2), dt(2025, 12, 3), dt(2025, 12, 4), dt(2025, 12, 5)],
        91: [dt(2025, 12, 8), dt(2025, 12, 9), dt(2025, 12, 10), dt(2025, 12, 11), dt(2025, 12, 12)],
        92: [dt(2025, 12, 15), dt(2025, 12, 16), dt(2025, 12, 17), dt(2025, 12, 18), dt(2025, 12, 19)],
        93: [dt(2025, 12, 22), dt(2025, 12, 23), dt(2025, 12, 24), dt(2025, 12, 25), dt(2025, 12, 26)],
    }
    sprints = {}
    for sid, days in base_days.items():
        start = days[0]
        end = days[-1].replace(hour=18)
        sprints[sid] = jc.Sprint(id=sid, name=f"Sprint {sid}", state="closed", board_id=1,
                                  start_at=start, end_at=end, complete_at=end)
    target = jc.Sprint(id=100, name="Sprint 100", state="closed", board_id=1,
                        start_at=dt(2025, 12, 29), end_at=dt(2026, 1, 2, 18), complete_at=dt(2026, 1, 2, 18))
    sprints[100] = target

    facts = []
    for sid, days in base_days.items():
        facts.extend(_base_sprint_facts(f"S{sid}", sid, days, story_points=6.0))

    t1 = _make_fact("T1", 100, model.Interval(from_=dt(2025, 12, 1), until=None), 5.0,
                     done_at=dt(2025, 12, 30, 10, 0), assignee="alice")
    t2 = _make_fact("T2", 100, model.Interval(from_=dt(2025, 12, 1), until=None), 3.0,
                     done_at=dt(2025, 12, 31, 10, 0), assignee="bob")
    t3 = jc.IssueFacts(
        key="T3", epic_key="", type="Story", role="", labels=[], assignee="alice",
        story_points=2.0, qa_estimation=0.0, created=dt(2025, 12, 1),
        initial_status="In Progress", initial_status_id="2", status_history=[], sp_events=[],
        current_status="In Progress", current_status_category_key="indeterminate",
        membership_by_sprint={100: [model.Interval(from_=dt(2025, 12, 30, 9, 0), until=None)]},
    )
    facts.extend([t1, t2, t3])

    client = FakeJiraClient(
        sprints=sprints, board=jc.Board(id=1, name="Team Board", type="scrum"),
        closed_ids=list(base_days) + [100], active_ids=[], facts=facts, statuses=STATUSES,
    )

    kwargs = dict(
        sprint_ids=[100], history_sprint_count=5, now=dt(2026, 1, 3),
        target_items=target_items if target_items is not None else 15,
    )

    if not with_gitlab:
        return report_data.build_combined_report(client, gitlab_client_obj=None, **kwargs)

    gitlab = FakeGitLabClient(
        project_ids={"group/proj": 42},
        mrs_by_project={"group/proj": [
            {"author": "alice", "state": "merged", "created_at": "2025-12-29T00:00:00Z", "merged_at": "2025-12-30T00:00:00Z",
             "cycle_time_hours": 12.0, "diff_stats_available": True, "additions": 10, "deletions": 5,
             "commits_count_available": True, "commits_count": 3, "changes_count_available": True, "changes_count": 2,
             "jira_key": "T1"},
            {"author": "bob", "state": "closed", "created_at": "2025-12-30T00:00:00Z", "merged_at": None,
             "cycle_time_hours": None, "diff_stats_available": False, "commits_count_available": False,
             "changes_count_available": False, "jira_key": None},
        ]},
        pipelines_by_project={"group/proj": [
            {"project": "group/proj", "status": "success", "created_at": "2025-12-29T00:00:00Z", "user_username": "alice"},
            {"project": "group/proj", "status": "failed", "created_at": "2025-12-30T00:00:00Z", "user_username": "bob"},
        ]},
        deployments_by_project={"group/proj": [
            {"project": "group/proj", "status": "success", "finished_at": "2025-12-29T00:00:00Z"},
        ]},
        coverage_by_project={"group/proj": [
            {"project": "group/proj", "coverage": 82.5},
        ]},
    )
    return report_data.build_combined_report(
        client, gitlab_client_obj=gitlab, gitlab_projects=["group/proj"], employees=["alice", "bob"], **kwargs
    )


def assert_tag_balance(testcase, html_text):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    class _P(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack = []
            self.mismatches = []

        def handle_starttag(self, tag, attrs):
            if tag not in VOID:
                self.stack.append(tag)

        def handle_endtag(self, tag):
            if tag in VOID:
                return
            if not self.stack or self.stack[-1] != tag:
                self.mismatches.append(tag)
                if tag in self.stack:
                    while self.stack and self.stack[-1] != tag:
                        self.stack.pop()
                    if self.stack:
                        self.stack.pop()
            else:
                self.stack.pop()

    p = _P()
    p.feed(html_text)
    testcase.assertEqual(p.mismatches, [], f"mismatched close tags: {p.mismatches}")
    testcase.assertEqual(p.stack, [], f"unclosed tags at EOF: {p.stack}")


# --------------------------------------------------------------------------
# Template completeness — the drift-catcher
# --------------------------------------------------------------------------


class TemplateCompletenessTests(unittest.TestCase):
    def test_full_gitlab_report_leaves_no_token_unsubstituted(self):
        report = build_multi_sprint_report(with_gitlab=True)
        out = rh.render_html(report)
        leftover = TOKEN_RE.findall(out)
        self.assertEqual(leftover, [], f"unsubstituted tokens: {leftover}")

    def test_no_gitlab_report_leaves_no_token_unsubstituted(self):
        report = build_multi_sprint_report(with_gitlab=False)
        out = rh.render_html(report)
        leftover = TOKEN_RE.findall(out)
        self.assertEqual(leftover, [], f"unsubstituted tokens: {leftover}")

    def test_forecast_error_report_leaves_no_token_unsubstituted(self):
        # A single closed sprint with almost no throughput history triggers
        # ERR_FORECAST_NOT_ENOUGH_DATA (< 10 non-zero daily points).
        sprint = jc.Sprint(id=1, name="Solo Sprint", state="closed", board_id=1,
                            start_at=dt(2026, 1, 5), end_at=dt(2026, 1, 9, 18), complete_at=dt(2026, 1, 9, 18))
        fact = _make_fact("A-1", 1, model.Interval(from_=dt(2026, 1, 5), until=None), 3.0, done_at=dt(2026, 1, 7, 10))
        client = FakeJiraClient(
            sprints={1: sprint}, board=jc.Board(id=1, name="B", type="scrum"),
            closed_ids=[1], active_ids=[], facts=[fact], statuses=STATUSES,
        )
        report = report_data.build_combined_report(
            client, sprint_ids=[1], now=dt(2026, 1, 10), target_items=5, gitlab_client_obj=None,
        )
        self.assertIsNotNone(report["forecast_error"])
        out = rh.render_html(report)
        leftover = TOKEN_RE.findall(out)
        self.assertEqual(leftover, [], f"unsubstituted tokens: {leftover}")


class MarkerCommentDeduplicationTests(unittest.TestCase):
    def test_scalar_values_are_not_duplicated_inside_their_own_marker_comment(self):
        # m9 — finalize()/sub_tokens() used to substitute {{TOKEN}} both
        # inside its own documentation-only `<!-- {{TOKEN}} -->` marker and
        # at the real content position, doubling every value's payload.
        report = build_multi_sprint_report(with_gitlab=True)
        out = rh.render_html(report)
        self.assertEqual(out.count("jira-team-metrics"), 1)


# --------------------------------------------------------------------------
# Table structure — column consistency (rowspan/colspan-aware)
# --------------------------------------------------------------------------


class _TableCellParser(HTMLParser):
    """Collects, per <table> in document order, one list of rows where each
    row is a list of (colspan, rowspan) for the cells it explicitly emits."""

    def __init__(self):
        super().__init__()
        self.tables: list = []
        self._tables_stack: list = []
        self._rows_stack: list = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table":
            self._tables_stack.append([])
            self._rows_stack.append(None)
        elif tag == "tr" and self._tables_stack:
            self._rows_stack[-1] = []
        elif tag in ("td", "th") and self._tables_stack and self._rows_stack[-1] is not None:
            colspan = int(a.get("colspan") or 1)
            rowspan = int(a.get("rowspan") or 1)
            self._rows_stack[-1].append((colspan, rowspan))

    def handle_endtag(self, tag):
        if tag == "tr" and self._tables_stack and self._rows_stack[-1] is not None:
            self._tables_stack[-1].append(self._rows_stack[-1])
            self._rows_stack[-1] = None
        elif tag == "table" and self._tables_stack:
            self.tables.append(self._tables_stack.pop())
            self._rows_stack.pop()


def _row_widths(rows):
    """Total occupied column width per row, carrying rowspan'd cells from
    earlier rows forward — the same check §1 of DESIGN-NOTES describes as
    "column-consistent under a rowspan-aware check", and the one that would
    have caught M3 (Table A's footer colspan short by 4)."""
    active: list = []  # [rows_left, colspan] for cells still covering the row being processed
    widths = []
    for cells in rows:
        carried = sum(colspan for rows_left, colspan in active if rows_left > 0)
        own = sum(colspan for colspan, _rowspan in cells)
        widths.append(carried + own)
        active = [[rows_left - 1, colspan] for rows_left, colspan in active if rows_left - 1 > 0]
        for colspan, rowspan in cells:
            if rowspan > 1:
                active.append([rowspan - 1, colspan])
    return widths


class TableColumnConsistencyTests(unittest.TestCase):
    def test_every_table_has_the_same_row_width_across_thead_tbody_tfoot(self):
        report = build_multi_sprint_report(with_gitlab=True)
        out = rh.render_html(report)
        parser = _TableCellParser()
        parser.feed(out)
        tables = [rows for rows in parser.tables if rows]
        self.assertGreater(len(tables), 0, "no <table> found in the rendered report")
        for i, rows in enumerate(tables):
            widths = _row_widths(rows)
            self.assertEqual(
                len(set(widths)), 1,
                f"table #{i} is column-inconsistent — row widths: {widths}",
            )


# --------------------------------------------------------------------------
# Escaping — every user-controlled string must be neutralized
# --------------------------------------------------------------------------


class EscapingTests(unittest.TestCase):
    XSS = '<script>alert(1)</script>"\'&javascript:alert(2)'
    ESCAPED_XSS = html.escape(XSS, quote=True)

    def _render_with_xss_names(self):
        sprint = jc.Sprint(id=500, name=self.XSS, state="closed", board_id=9,
                            start_at=dt(2026, 2, 2), end_at=dt(2026, 2, 6, 18), complete_at=dt(2026, 2, 6, 18))
        fact = jc.IssueFacts(
            key="P-1", epic_key="", type="Story", role="", labels=[self.XSS], assignee=self.XSS,
            story_points=8.0, qa_estimation=0.0, created=dt(2026, 1, 1),
            initial_status="In Progress", initial_status_id="2",
            status_history=[jc.RawStatusChange(at=dt(2026, 2, 4, 10, 0), from_name="In Progress", to_name="Done", from_id="2", to_id="3")],
            sp_events=[],
            current_status="Done", current_status_category_key="done",
            resolutiondate=dt(2026, 2, 4, 10, 0),
            membership_by_sprint={500: [model.Interval(from_=dt(2026, 1, 15), until=None)]},
        )
        client = FakeJiraClient(
            sprints={500: sprint}, board=jc.Board(id=9, name=self.XSS, type="scrum"),
            closed_ids=[500], active_ids=[], facts=[fact], statuses=STATUSES,
        )
        gitlab = FakeGitLabClient(
            project_ids={"group/proj": 42},
            mrs_by_project={"group/proj": [
                {"author": self.XSS, "state": "merged", "created_at": "2026-02-03T00:00:00Z", "merged_at": "2026-02-04T00:00:00Z", "jira_key": None},
            ]},
            pipelines_by_project={"group/proj": [{"project": self.XSS, "status": "success", "created_at": "2026-02-03T00:00:00Z", "user_username": self.XSS}]},
        )
        report = report_data.build_combined_report(
            client, sprint_ids=[500], now=dt(2026, 2, 7),
            gitlab_client_obj=gitlab, gitlab_projects=["group/proj"], employees=[self.XSS],
        )
        return rh.render_html(report)

    def test_script_tag_never_appears_unescaped(self):
        out = self._render_with_xss_names()
        self.assertNotIn("<script>alert(1)</script>", out)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", out)

    def test_quotes_and_ampersand_escaped(self):
        # The FULL payload must appear escaped as one contiguous block —
        # not just each entity type turning up somewhere unrelated in a
        # 150KB document, which any unrelated quote would also satisfy.
        out = self._render_with_xss_names()
        self.assertIn(self.ESCAPED_XSS, out)

    def test_javascript_url_text_never_lands_in_a_live_href(self):
        out = self._render_with_xss_names()
        self.assertNotRegex(out, r'href="javascript:')

    def test_only_script_tag_in_document_is_the_static_theme_switch(self):
        out = self._render_with_xss_names()
        script_tags = re.findall(r"<script[^>]*>", out)
        # Exactly the one static <script> block copied verbatim from the
        # prototype (theme switch + section collapse) — none injected.
        self.assertEqual(script_tags, ["<script>"])

    def test_document_title_tag_carries_the_escaped_payload_not_raw(self):
        out = self._render_with_xss_names()
        m = re.search(r"<title>(.*?)</title>", out, re.S)
        self.assertIsNotNone(m)
        self.assertIn(self.ESCAPED_XSS, m.group(1))
        self.assertNotIn("<script>", m.group(1))

    def test_no_raw_angle_bracket_inside_any_title_or_aria_label_attribute(self):
        # A real per-context escaping check: every title="..." and
        # aria-label="..." attribute in the whole document, wherever it
        # comes from, must never contain an unescaped '<' — the invariant
        # a leaking name/label/author string would actually break.
        out = self._render_with_xss_names()
        found_any = False
        for m in re.finditer(r'\b(?:title|aria-label)="([^"]*)"', out):
            found_any = True
            self.assertNotIn("<", m.group(1), f"unescaped '<' inside {m.group(0)[:120]}")
        self.assertTrue(found_any, "no title=/aria-label= attributes found — test would pass vacuously")


class NoExternalResourceTests(unittest.TestCase):
    def test_full_report_makes_zero_external_resource_references(self):
        report = build_multi_sprint_report(with_gitlab=True)
        out = rh.render_html(report)
        self.assertNotIn("<script src", out)
        self.assertNotRegex(out, r'<link[^>]+href="https?://')
        self.assertNotIn("@import", out)
        self.assertNotIn("http://", out)
        self.assertNotIn("https://", out)
        self.assertNotRegex(out, r"fetch\(|XMLHttpRequest")

    def test_no_var_inside_svg_presentation_attributes(self):
        report = build_multi_sprint_report(with_gitlab=True)
        out = rh.render_html(report)
        for attr in ("fill", "stroke", "stop-color"):
            self.assertNotRegex(out, rf'{attr}="var\(')


# --------------------------------------------------------------------------
# Chart geometry
# --------------------------------------------------------------------------


class RuPluralTests(unittest.TestCase):
    def test_one_few_many_selection(self):
        self.assertEqual(rh.ru_plural(1, "участник", "участника", "участников"), "участник")
        self.assertEqual(rh.ru_plural(2, "участник", "участника", "участников"), "участника")
        self.assertEqual(rh.ru_plural(4, "участник", "участника", "участников"), "участника")
        self.assertEqual(rh.ru_plural(5, "участник", "участника", "участников"), "участников")
        self.assertEqual(rh.ru_plural(0, "участник", "участника", "участников"), "участников")

    def test_11_to_14_exception_uses_many_not_one_or_few(self):
        # browser-verified bug: "21 дней" (should be "21 день"). The 11-14
        # exception is the one most implementations forget.
        for n in (11, 12, 13, 14):
            with self.subTest(n=n):
                self.assertEqual(rh.ru_plural(n, "день", "дня", "дней"), "дней")
        self.assertEqual(rh.ru_plural(21, "день", "дня", "дней"), "день")
        self.assertEqual(rh.ru_plural(22, "день", "дня", "дней"), "дня")
        self.assertEqual(rh.ru_plural(25, "день", "дня", "дней"), "дней")
        self.assertEqual(rh.ru_plural(111, "день", "дня", "дней"), "дней")  # 111 % 100 == 11


class NiceStepTests(unittest.TestCase):
    def test_every_tick_is_round_not_just_the_axis_max(self):
        # Browser-verified bug: nice_max(12)/6 ticks gave 0, 3, 7, 10, 13,
        # 17, 20 (gaps of 3,4,3,3,4,3) — the max was round, the ticks
        # weren't. nice_step picks the STEP first so every tick is round.
        step = rh.nice_step(12.0, 6)
        self.assertEqual(step, 2.0)
        top = step * 6
        ticks = [round(step * i) for i in range(7)]
        self.assertEqual(ticks, [0, 2, 4, 6, 8, 10, 12])
        self.assertGreaterEqual(top, 12.0)

    def test_step_covers_the_value_with_at_most_one_step_of_headroom(self):
        step = rh.nice_step(58.0, 6)
        top = step * 6
        self.assertGreaterEqual(top, 58.0)
        self.assertLess(top - 58.0, step)

    def test_zero_or_negative_value_falls_back_to_one(self):
        self.assertEqual(rh.nice_step(0.0, 6), 1.0)
        self.assertEqual(rh.nice_step(-5.0, 6), 1.0)


class SparkSvgTests(unittest.TestCase):
    def test_known_series_produces_expected_points(self):
        svg = rh.build_spark_svg([0.0, 10.0, 20.0], "good")
        # min=0 -> y=27 (bottom), max=20 -> y=5 (top), mid=10 -> y=16
        self.assertIn('points="2.00,27.00 50.00,16.00 98.00,5.00"', svg)
        self.assertIn('cx="98.00" cy="5.00"', svg)
        self.assertIn("sp-line sp-good", svg)

    def test_empty_series_renders_bare_svg_no_polyline(self):
        svg = rh.build_spark_svg([], "mute")
        self.assertNotIn("<polyline", svg)
        self.assertNotIn("<circle", svg)
        self.assertIn("<svg", svg)

    def test_single_point_renders_a_dot_not_a_line(self):
        svg = rh.build_spark_svg([42.0], "warn")
        self.assertNotIn("<polyline", svg)
        self.assertIn('cx="98"', svg)
        self.assertIn("sp-dot-warn", svg)

    def test_all_equal_series_does_not_divide_by_zero(self):
        svg = rh.build_spark_svg([5.0, 5.0, 5.0], "good")
        self.assertIn('points="2.00,16.00 50.00,16.00 98.00,16.00"', svg)


class BurndownSvgTests(unittest.TestCase):
    def test_known_points_produce_expected_path_coordinates(self):
        points = [
            {"date": "2026-01-05", "remaining_items": 2, "remaining_sp": 10.0, "ideal_sp": 10.0},
            {"date": "2026-01-06", "remaining_items": 1, "remaining_sp": 5.0, "ideal_sp": 5.0},
            {"date": "2026-01-07", "remaining_items": 0, "remaining_sp": 0.0, "ideal_sp": 0.0},
        ]
        aria, svg, _weekend = rh.build_burndown_svg(points)
        # nice_step(10, 6) = 2 -> top = 12 (a round step through every tick,
        # not just nice_max(10)=10 divided into 6 uneven-looking fractions);
        # y_of(v) = 288 - v/12*264. v=10 -> y=68; v=5 -> y=178; v=0 -> y=288.
        self.assertIn("56.00,68.00", svg)
        self.assertIn("393.00,178.00", svg)
        self.assertIn("730.00,288.00", svg)
        self.assertIn("остаток: 10, 5, 0 SP", aria)
        self.assertIn("осталось 0 SP", aria)

    def test_empty_points_render_no_data_message_not_a_broken_chart(self):
        aria, svg, _weekend = rh.build_burndown_svg([])
        self.assertIn("недоступен", aria)
        self.assertIn("нет данных", svg)
        self.assertNotIn("<polyline", svg)

    def test_single_point_does_not_crash_and_places_one_dot(self):
        points = [{"date": "2026-01-05", "remaining_items": 3, "remaining_sp": 12.0, "ideal_sp": 12.0}]
        aria, svg, _weekend = rh.build_burndown_svg(points)
        self.assertIn("<circle", svg)
        self.assertIn("12 SP", aria)

    def test_single_point_weekend_band_does_not_cover_the_whole_plot(self):
        # nit — day_width used to fall back to the full plot width (x1-x0)
        # for a single-day chart, shading the entire plot area.
        points = [{"date": "2026-01-10", "remaining_items": 1, "remaining_sp": 4.0, "ideal_sp": 4.0}]  # Saturday
        aria, svg, _weekend = rh.build_burndown_svg(points)
        m = re.search(r'<rect class="c-weekend"[^/]*width="([\d.]+)"', svg)
        self.assertIsNotNone(m)
        self.assertLess(float(m.group(1)), 674.0)  # x1 - x0

    def test_weekend_band_is_centered_on_the_day_tick_not_drifted(self):
        # m3 — day_width used /n instead of /(n-1) and bands anchored at
        # x0 + i*day_width instead of centring on xs[i], drifting a full
        # column by the last day.
        points = [
            {"date": "2026-01-05", "remaining_items": 3, "remaining_sp": 12.0, "ideal_sp": 12.0},
            {"date": "2026-01-06", "remaining_items": 2, "remaining_sp": 8.0, "ideal_sp": 8.0},
            {"date": "2026-01-10", "remaining_items": 1, "remaining_sp": 4.0, "ideal_sp": 4.0},  # Saturday
            {"date": "2026-01-11", "remaining_items": 0, "remaining_sp": 0.0, "ideal_sp": 0.0},  # Sunday, last day
        ]
        aria, svg, _weekend = rh.build_burndown_svg(points)
        # Saturday (index 2 of 4): xs[2] = 505.33, day_width = 674/3 = 224.67
        # -> centred band from 393.0 to 617.7.
        self.assertIn('x="393.0"', svg)
        self.assertIn('width="224.7"', svg)
        # Sunday is the last day: the band is clamped at the right edge
        # (x1=730) instead of overflowing past the plot.
        self.assertIn('x="617.7"', svg)
        self.assertIn('width="112.3"', svg)

    def test_all_zero_series_does_not_divide_by_zero(self):
        points = [
            {"date": "2026-01-05", "remaining_items": 0, "remaining_sp": 0.0, "ideal_sp": 0.0},
            {"date": "2026-01-06", "remaining_items": 0, "remaining_sp": 0.0, "ideal_sp": 0.0},
        ]
        aria, svg, _weekend = rh.build_burndown_svg(points)
        self.assertIn("<svg", svg)
        self.assertNotIn("nan", svg.lower())

    def test_has_weekend_flag_is_false_for_a_pure_weekday_sprint(self):
        points = [
            {"date": "2026-01-05", "remaining_items": 2, "remaining_sp": 10.0, "ideal_sp": 10.0},  # Mon
            {"date": "2026-01-09", "remaining_items": 0, "remaining_sp": 0.0, "ideal_sp": 0.0},  # Fri
        ]
        _aria, _svg, has_weekend = rh.build_burndown_svg(points)
        self.assertFalse(has_weekend)

    def test_has_weekend_flag_is_true_when_a_point_lands_on_a_weekend(self):
        points = [
            {"date": "2026-01-05", "remaining_items": 2, "remaining_sp": 10.0, "ideal_sp": 10.0},  # Mon
            {"date": "2026-01-10", "remaining_items": 0, "remaining_sp": 0.0, "ideal_sp": 0.0},  # Sat
        ]
        _aria, _svg, has_weekend = rh.build_burndown_svg(points)
        self.assertTrue(has_weekend)

    def test_full_render_omits_weekend_legend_for_a_mon_fri_sprint(self):
        # Browser-verified — the legend advertised "выходные" even when
        # the demo sprint (Mon-Fri) had zero weekend days.
        report = build_multi_sprint_report(with_gitlab=True)
        points = report["burndowns"][-1]["points"]
        any_weekend = any(rh._weekday(p["date"]) >= 5 for p in points)
        out = rh.render_html(report)
        # scope to the legend itself — the section-desc paragraph mentions
        # "выходные" generically (it explains the axis convention) even
        # when this particular chart has no weekend band.
        legend = out[out.index('<div class="legend">'):out.index('id="sec-heatmap"')]
        if any_weekend:
            self.assertIn("выходные", legend)
        else:
            self.assertNotIn("выходные", legend)


class ForecastSvgTests(unittest.TestCase):
    def test_known_histogram_bar_heights_scale_to_nice_max(self):
        forecast = {
            "target_items": 10,
            "sample_sprints": 3,
            "sample_days": 15,
            "throughput_cv_pct": 12.0,
            "percentiles": [{"percentile": 50, "days": 5}, {"percentile": 85, "days": 6}, {"percentile": 95, "days": 7}],
            "histogram": [{"days": 5, "count": 50}, {"days": 6, "count": 100}, {"days": 7, "count": 25}],
            "warnings": [],
        }
        aria, svg = rh.build_forecast_svg(forecast)
        # top = nice_max(100) = 100; y_of(c) = 232 - c/100*212
        self.assertIn(f'y="{232 - 50/100*212:.2f}"', svg)
        self.assertIn(f'y="{232 - 100/100*212:.2f}"', svg)
        self.assertIn("c-bar-strong", svg)  # p50/mode bar highlighted
        self.assertIn("p50 на 5 днях", aria)

    def test_empty_histogram_renders_no_data_message(self):
        forecast = {"target_items": 1, "sample_sprints": 0, "sample_days": 0, "throughput_cv_pct": 0.0,
                     "percentiles": [], "histogram": [], "warnings": []}
        aria, svg = rh.build_forecast_svg(forecast)
        self.assertIn("недоступна", aria)
        self.assertIn("нет данных", svg)


class ChartViewBoxTests(unittest.TestCase):
    """BUG 2 (browser-verified) — the forecast chart reused the engineering
    chart's 760x260 viewBox, clipping its own p50/p85/p95 labels and hiding
    the x-axis title entirely. Every <text> baseline must land inside its
    own <svg viewBox>."""

    SVG_RE = re.compile(r'<svg\b[^>]*\bviewBox="0 0 ([\d.]+) ([\d.]+)"[^>]*>(.*?)</svg>', re.S)
    TEXT_Y_RE = re.compile(r'<text\b[^>]*\by="(-?[\d.]+)"')

    def _assert_all_text_within_viewbox(self, out):
        checked = 0
        for m in self.SVG_RE.finditer(out):
            width, height, body = float(m.group(1)), float(m.group(2)), m.group(3)
            for tm in self.TEXT_Y_RE.finditer(body):
                checked += 1
                y = float(tm.group(1))
                self.assertTrue(
                    -2.0 <= y <= height + 2.0,
                    f"<text y=\"{y}\"> falls outside viewBox height {height}",
                )
        self.assertGreater(checked, 0, "no <text> elements found — test would pass vacuously")

    def test_forecast_chart_labels_stay_inside_its_viewbox(self):
        forecast = {
            "target_items": 10, "sample_sprints": 5, "sample_days": 60, "throughput_cv_pct": 12.0,
            "percentiles": [{"percentile": 50, "days": 17}, {"percentile": 85, "days": 21}, {"percentile": 95, "days": 24}],
            "histogram": [{"days": d, "count": c} for d, c in zip(range(11, 26), [25, 78, 180, 330, 520, 660, 710, 630, 520, 400, 300, 215, 150, 100, 70])],
            "warnings": [],
        }
        aria, svg = rh.build_forecast_svg(forecast)
        self._assert_all_text_within_viewbox(f"<div>{svg}</div>")

    def test_every_chart_in_a_full_render_stays_inside_its_own_viewbox(self):
        report = build_multi_sprint_report(with_gitlab=True)
        out = rh.render_html(report)
        self._assert_all_text_within_viewbox(out)


# --------------------------------------------------------------------------
# Four empty-value states (DESIGN-NOTES §5)
# --------------------------------------------------------------------------


class EmptyValueStateTests(unittest.TestCase):
    def test_div0_state_renders_zero_plus_badge(self):
        text, cls = rh._cell_state(0.0, ["WARN_DIVISION_BY_ZERO:mr_merge_rate_pct"], None, "WARN_DIVISION_BY_ZERO:mr_merge_rate_pct")
        self.assertEqual(cls, " cell-nodata")
        self.assertIn("DIV0", text)
        self.assertIn("warn-badge", text)

    def test_unavail_state_renders_dash_plus_na_badge(self):
        text, cls = rh._cell_state(None, ["WARN_DIFF_STATS_UNAVAILABLE"], "WARN_DIFF_STATS_UNAVAILABLE", None)
        self.assertEqual(cls, " cell-unavail")
        self.assertIn("нет данных", text)
        self.assertIn("badge-na", text)

    def test_nosample_state_renders_bare_dash_with_title(self):
        text, cls = rh._cell_state(None, [], "WARN_DIFF_STATS_UNAVAILABLE", None)
        self.assertEqual(cls, " cell-nosample")
        self.assertIn("title=", text)
        self.assertNotIn("badge-na", text)
        self.assertNotIn("warn-badge", text)

    def test_nosample_title_is_column_specific_not_one_generic_string(self):
        # browser-verified — the same "нет исходных значений" wording was
        # reused on 26 cells while the specific "ни одного MR" wording the
        # prototype uses appeared exactly once. mr_* and task_* columns
        # describe different empty populations and must say so.
        person = {
            "mr_count": 0, "mr_merged_count": 0, "mr_closed_count": 0,
            "mr_merge_rate_pct": 0.0, "mr_cycle_time_avg_hours": None, "mr_cycle_time_median_hours": None,
            "mr_diff_size_avg": None, "mr_diff_size_available_count": 0,
            "mr_commits_avg": None, "mr_commits_sum": None,
            "mr_changes_count_avg": None, "mr_changes_count_sum": None,
            "tasks_done": 0, "bug_count": 0, "defect_rate_pct": 0.0,
            "task_cycle_time_avg_hours": None, "task_cycle_time_median_hours": None,
            "rework_total": 0, "story_points_total": None, "story_points_avg": None,
            "qa_estimation_total": None, "qa_estimation_avg": None,
            "linked_tasks": 0, "mr_with_jira_key": 0, "mr_per_task": None,
            "warnings": [],
        }
        m = rh._person_cell_map(person)
        self.assertIn("ни одного MR", m["PERSON_MR_CYCLE_TIME_AVG_HOURS"])
        self.assertIn("ни одной закрытой задачи", m["PERSON_TASK_CYCLE_TIME_AVG_HOURS"])
        self.assertNotIn("нет исходных значений", m["PERSON_MR_CYCLE_TIME_AVG_HOURS"])
        self.assertNotIn("нет исходных значений", m["PERSON_TASK_CYCLE_TIME_AVG_HOURS"])

    def test_all_four_states_appear_with_correct_titles_in_a_full_render(self):
        report = build_multi_sprint_report(with_gitlab=True)
        # bob's MR has no cycle_time/diff/commits/changes stats and never merged
        out = rh.render_html(report)
        self.assertIn("cell-nodata", out)
        self.assertIn("cell-unavail", out)
        self.assertIn('title="null по контракту: linked_tasks = 0"', out)
        # the state legend explains all four
        self.assertIn("null по контракту", out)
        self.assertIn("знаменатель равен нулю", out)
        self.assertIn("источник не вернул статистику", out)
        self.assertIn("пустая выборка", out)

    def test_person_totals_diff_size_denom_is_real_markup_not_double_escaped(self):
        # M1 — esc() used to wrap the whole "<span class=denom>...</span>"
        # string, so the totals cell printed the literal tag text instead
        # of rendering the span.
        report = build_multi_sprint_report(with_gitlab=True)
        out = rh.render_html(report)
        self.assertIn('<span class="denom">/', out)
        self.assertNotIn("&lt;span class=", out)


class BoardTableDiv0Tests(unittest.TestCase):
    """m1 — every DIV0'd board_table ratio (not just load_pct/velocity_sma5_sp)
    must carry both the badge and the cell-nodata class."""

    @staticmethod
    def _all_div0_row_html():
        text = rh.strip_scalar_marker_comments(rh._load_template())
        inner = rh.extract_inner(text, "LOOP_BOARD_ROWS_START", "LOOP_BOARD_ROWS_END")
        metrics = {
            "committed_sp": 0.0, "committed_items": 0, "scope_added_items": 0, "scope_removed_items": 0,
            "scope_added_sp": 0.0, "scope_removed_sp": 0.0, "scope_change_pct": 0.0,
            "performance_pct": 0.0, "load_pct": 0.0, "velocity_sp": 0.0, "velocity_sma5_sp": 0.0,
            "throughput_items": 0, "closure_pct_items": 0.0, "closure_pct_sp": 0.0,
            "scope_estimation_change_sp": 0.0, "delivered_sp": 0.0, "delivered_items": 0,
        }
        sprint = {
            "target": True,
            "payload": {
                "sprint": {"name": "S1", "state": "closed", "start_at": "2026-01-05", "end_at": "2026-01-09"},
                "metrics": metrics,
            },
        }
        return rh.build_board_row_html(inner, (0, sprint), [sprint])

    def test_all_six_div0_ratios_get_the_badge_and_the_nodata_class(self):
        row_html = self._all_div0_row_html()
        self.assertEqual(row_html.count("DIV0"), 6)
        self.assertEqual(row_html.count("cell-nodata"), 6)


class MetricGroupDiv0StatusClassTests(unittest.TestCase):
    def test_performance_and_scope_change_class_is_none_not_a_fake_band_on_div0(self):
        # m2 — the CSS status class used to be computed from the div0'd 0.0
        # value itself, so a real div0 read as a red "critical" band.
        metrics = {
            "committed_sp": 0.0, "committed_items": 0, "scope_added_items": 0, "scope_removed_items": 0,
            "scope_added_sp": 0.0, "scope_removed_sp": 0.0, "scope_change_pct": 0.0,
            "performance_pct": 0.0, "load_pct": 0.0, "velocity_sp": 0.0, "velocity_sma5_sp": 0.0,
            "throughput_items": 0, "closure_pct_items": 0.0, "closure_pct_sp": 0.0,
            "scope_estimation_change_sp": 0.0, "delivered_sp": 0.0, "delivered_items": 0,
        }
        report = {"sprints": [{"target": True, "payload": {"metrics": metrics}}], "kpi": {}}
        ctx: dict = {}
        rh.build_metric_groups(report, ctx)
        self.assertEqual(ctx["METRIC_PERFORMANCE_PCT_CLASS"], "is-none")
        self.assertEqual(ctx["METRIC_SCOPE_CHANGE_PCT_CLASS"], "is-none")
        self.assertEqual(ctx["METRIC_CLOSURE_PCT_ITEMS_CLASS"], "is-none")
        self.assertEqual(ctx["METRIC_CLOSURE_PCT_SP_CLASS"], "is-none")


class PersonCardExtraStatsTests(unittest.TestCase):
    """m8 — rework_share and pipeline_success_rate have no home on the
    person table (its column budget is print-width constrained) but the
    card has room; render them there."""

    @staticmethod
    def _card_html(person_extra):
        text = rh.strip_scalar_marker_comments(rh._load_template())
        inner = rh.extract_inner(text, "PERSON_CARD_START", "PERSON_CARD_END")
        person = {
            "user": "alice", "mr_count": 3, "mr_merge_rate_pct": 66.7,
            "mr_cycle_time_median_hours": 10.0, "tasks_done": 2,
            "mr_with_jira_key": 1, "warnings": [], "sprints": [],
        }
        person.update(person_extra)
        return rh.build_person_card(inner, person)

    def test_rework_share_and_pipeline_success_render_as_percentages(self):
        html = self._card_html({"rework_share": 0.25, "pipeline_success_rate": 0.875})
        self.assertIn("доля переработок", html)
        self.assertIn("25", html)
        self.assertIn("успешность пайплайнов", html)
        self.assertIn("87.5", html)

    def test_both_show_none_state_when_null(self):
        html = self._card_html({"rework_share": None, "pipeline_success_rate": None})
        self.assertIn('data-status="none"><div class="stat-label">доля переработок', html)
        self.assertIn('data-status="none"><div class="stat-label">успешность пайплайнов', html)


# --------------------------------------------------------------------------
# KPI delta direction (DESIGN-NOTES §4)
# --------------------------------------------------------------------------


class KpiDeltaDirectionTests(unittest.TestCase):
    def test_scope_change_rising_is_flagged_bad_not_good(self):
        d, arrow = rh._dir_and_arrow(5.0, higher_is_good=False)
        self.assertEqual(d, "up-bad")
        d, arrow = rh._dir_and_arrow(-5.0, higher_is_good=False)
        self.assertEqual(d, "down-good")

    def test_velocity_rising_is_flagged_good(self):
        d, _ = rh._dir_and_arrow(5.0, higher_is_good=True)
        self.assertEqual(d, "up-good")
        d, _ = rh._dir_and_arrow(-5.0, higher_is_good=True)
        self.assertEqual(d, "down-bad")

    def test_zero_delta_is_flat(self):
        d, arrow = rh._dir_and_arrow(0.0, higher_is_good=True)
        self.assertEqual(d, "flat")

    def test_none_delta_is_none_direction(self):
        d, arrow = rh._dir_and_arrow(None, higher_is_good=True)
        self.assertEqual(d, "none")

    def test_load_status_band_is_good_only_in_the_middle(self):
        self.assertEqual(rh._load_status(100.0), "good")
        self.assertEqual(rh._load_status(120.0), "warn")
        self.assertEqual(rh._load_status(130.0), "bad")
        self.assertEqual(rh._load_status(75.0), "warn")
        self.assertEqual(rh._load_status(50.0), "bad")

    @staticmethod
    def _kpi_report(committed_sp, scope_change_pct, avg_scope_change_pct):
        base_metrics = {
            "committed_sp": committed_sp, "committed_items": 10, "scope_added_items": 0, "scope_removed_items": 0,
            "scope_added_sp": 0.0, "scope_removed_sp": 0.0, "scope_change_pct": 5.0,
            "performance_pct": 90.0, "load_pct": 100.0, "velocity_sp": 40.0, "velocity_sma5_sp": 40.0,
            "throughput_items": 8, "closure_pct_items": 90.0, "closure_pct_sp": 90.0,
            "scope_estimation_change_sp": 0.0, "delivered_sp": 36.0, "delivered_items": 9,
        }
        target_metrics = dict(base_metrics, scope_change_pct=scope_change_pct)
        return {
            "sprints": [
                {"target": False, "payload": {"metrics": base_metrics}},
                {"target": True, "payload": {"metrics": target_metrics}},
            ],
            "kpi": {"avg_scope_change_pct": avg_scope_change_pct},
        }

    def test_scope_change_tile_direction_is_bad_when_it_rises(self):
        # scope_change_pct rising vs. the board average is BAD, unlike
        # velocity/performance where rising is good — a test that also
        # accepts up-good would pass with the mapping inverted.
        report = self._kpi_report(committed_sp=40.0, scope_change_pct=20.0, avg_scope_change_pct=10.0)
        ctx = {}
        rh.build_kpi_tiles(report, ctx)
        self.assertEqual(ctx["KPI_SCOPE_CHANGE_DELTA_DIR"], "up-bad")

    def test_scope_change_tile_direction_is_good_when_it_falls(self):
        report = self._kpi_report(committed_sp=40.0, scope_change_pct=5.0, avg_scope_change_pct=10.0)
        ctx = {}
        rh.build_kpi_tiles(report, ctx)
        self.assertEqual(ctx["KPI_SCOPE_CHANGE_DELTA_DIR"], "down-good")

    def test_first_sprint_tiles_show_real_values_only_the_band_is_suppressed(self):
        # M4 — on a board's first sprint (n_base == 0) velocity_sp,
        # throughput_items and closure_pct_* are real numbers with nothing
        # to divide by zero; status="none" must drop only the band/delta.
        sprint = jc.Sprint(id=1, name="Sprint 1", state="closed", board_id=1,
                            start_at=dt(2026, 1, 5), end_at=dt(2026, 1, 9, 18), complete_at=dt(2026, 1, 9, 18))
        fact = _make_fact("A-1", 1, model.Interval(from_=dt(2026, 1, 5), until=None), 5.0, done_at=dt(2026, 1, 7, 10))
        client = FakeJiraClient(
            sprints={1: sprint}, board=jc.Board(id=1, name="B", type="scrum"),
            closed_ids=[1], active_ids=[], facts=[fact], statuses=STATUSES,
        )
        report = report_data.build_combined_report(
            client, sprint_ids=[1], now=dt(2026, 1, 10), target_items=5, gitlab_client_obj=None,
        )
        self.assertEqual(len(rh.base_entries(report)), 0)  # confirms this really is a first sprint
        ctx: dict = {}
        rh.build_kpi_tiles(report, ctx)
        for tid in ("VELOCITY", "THROUGHPUT", "CLOSURE_ITEMS", "CLOSURE_SP"):
            with self.subTest(tid=tid):
                self.assertNotEqual(ctx[f"KPI_{tid}_VALUE"], rh.EM_DASH)
        # velocity/throughput band relative to a baseline that doesn't
        # exist yet, so their band is legitimately absent...
        self.assertEqual(ctx["KPI_VELOCITY_STATUS"], "none")
        self.assertEqual(ctx["KPI_THROUGHPUT_STATUS"], "none")
        # ...but closure_pct_* bands on fixed thresholds and needs no
        # baseline at all, so it keeps a real band on the same sprint.
        self.assertIn(ctx["KPI_CLOSURE_ITEMS_STATUS"], ("good", "warn", "bad"))
        self.assertIn(ctx["KPI_CLOSURE_SP_STATUS"], ("good", "warn", "bad"))
        # SMA5 itself genuinely has no value with zero history.
        self.assertEqual(ctx["KPI_SMA5_VALUE"], rh.EM_DASH)


class EngTileBandTests(unittest.TestCase):
    def test_pipelines_count_and_deployments_per_week_carry_no_band(self):
        # m6 — pipelines.count borrowed the success-rate band (a run count
        # read as "критично"); deployments.per_week was unconditionally
        # "норма". §5 defines no band for either.
        report = build_multi_sprint_report(with_gitlab=True)
        ctx: dict = {}
        state = rh.build_eng_tab(report, ctx)
        self.assertEqual(state, "available")
        self.assertEqual(ctx["ENG_PIPELINES_COUNT_STATUS"], "none")
        self.assertEqual(ctx["ENG_DEPLOYMENTS_PER_WEEK_STATUS"], "none")


# --------------------------------------------------------------------------
# window_applied caveats
# --------------------------------------------------------------------------


class WindowCaveatTests(unittest.TestCase):
    @staticmethod
    def _pipelines_tile(out):
        section = out[out.index('id="sec-eng-kpi"'):out.index('id="sec-eng-chart"')]
        return section.split('kpi-label">Пайплайны<')[1].split("</article>")[0]

    def test_caveat_marker_absent_when_window_applied_is_true(self):
        report = build_multi_sprint_report(with_gitlab=True)
        report["engineering"]["data"]["window_applied"]["pipelines"] = True
        out = rh.render_html(report)
        self.assertNotIn("вне окна отчёта", self._pipelines_tile(out))

    def test_caveat_marker_present_when_window_applied_is_false(self):
        # the contract that matters: the marker must actually be emitted,
        # not merely absent-by-default.
        report = build_multi_sprint_report(with_gitlab=True)
        report["engineering"]["data"]["window_applied"]["pipelines"] = False
        out = rh.render_html(report)
        self.assertIn("вне окна отчёта", self._pipelines_tile(out))


# --------------------------------------------------------------------------
# Full-render smoke tests
# --------------------------------------------------------------------------


class FullRenderTests(unittest.TestCase):
    def test_full_gitlab_report_is_valid_balanced_html(self):
        report = build_multi_sprint_report(with_gitlab=True)
        out = rh.render_html(report)
        assert_tag_balance(self, out)
        self.assertTrue(out.strip().startswith("<!DOCTYPE html>"))
        self.assertTrue(out.strip().endswith("</html>"))

    def test_no_gitlab_report_is_valid_balanced_html_and_marks_sections_unavailable(self):
        report = build_multi_sprint_report(with_gitlab=False)
        out = rh.render_html(report)
        assert_tag_balance(self, out)
        self.assertIn("Раздел недоступен для этого запуска", out)

    def test_skipped_projects_render_as_visible_disclaimer_not_zero_activity(self):
        # gitlab_client.py:691 — skipped_projects entries are dicts, never
        # bare strings.
        report = build_multi_sprint_report(with_gitlab=True)
        report["gitlab_fetch_issues"]["skipped_projects"] = [
            {"project": "group/broken-project", "code": "NOT_FOUND", "message": "project id not returned"}
        ]
        out = rh.render_html(report)
        self.assertIn("group/broken-project", out)
        self.assertIn("NOT_FOUND", out)
        self.assertIn("Пропущены проекты", out)
        self.assertNotIn("{'project'", out)

    def test_skipped_project_also_surfaces_on_the_engineering_tab(self):
        # browser-verified — the footer said a project was skipped, but the
        # Инженерия tab's per_project caption gave no hint one was missing.
        report = build_multi_sprint_report(with_gitlab=True)
        report["gitlab_fetch_issues"]["skipped_projects"] = [
            {"project": "group/broken-project", "code": "NOT_FOUND", "message": "project id not returned"}
        ]
        out = rh.render_html(report)
        eng_table_section = out[out.index('id="sec-eng-table"'):out.index("</table>", out.index('id="sec-eng-table"'))]
        self.assertIn("group/broken-project", eng_table_section)
        self.assertIn("NOT_FOUND", eng_table_section)

    def test_mr_fetch_errors_render_as_detail_and_mark_the_affected_person(self):
        # m4 — a failed MR fetch must show project/author/state/code, and
        # must mark the affected person's own row, not only the footer.
        report = build_multi_sprint_report(with_gitlab=True)
        report["gitlab_fetch_issues"]["mr_fetch_errors"] = [
            {"project": "group/proj", "author": "alice", "state": "merged", "code": "RATE_LIMITED", "message": "429"}
        ]
        out = rh.render_html(report)
        self.assertIn("RATE_LIMITED", out)
        self.assertIn("alice", out)
        self.assertIn("MR_FETCH_ERROR", out)
        # the badge must sit near alice's own row, not only in the footer
        alice_at = out.index(">alice<")
        self.assertIn("MR_FETCH_ERROR", out[alice_at:alice_at + 4000])

    def test_semantics_notes_render_in_personal_banner(self):
        report = build_multi_sprint_report(with_gitlab=True)
        out = rh.render_html(report)
        for note in report["semantics_notes"]:
            self.assertIn(note[:30], out)


# --------------------------------------------------------------------------
# Every chart/table section explains itself: what it shows, what is good
# --------------------------------------------------------------------------

# Ordered section ids as they appear in the document — consecutive pairs
# bound the slice checked for each section; the last one is bounded by the
# footer, which always follows the last tab.
_SECTION_ORDER = [
    "sec-warnings", "sec-kpi", "sec-metrics", "sec-burndown", "sec-heatmap",
    "sec-forecast", "sec-tables",
    "sec-person-note", "sec-person-table", "sec-person-cards",
    "sec-eng-note", "sec-eng-kpi", "sec-eng-chart", "sec-eng-table",
]

# Sections that carry a chart, diagram, or table and must explain both what
# it shows and what counts as good — the ones this task is actually about.
# sec-warnings/sec-metrics/sec-person-note/sec-eng-note are plain text/scalar
# blocks, not visual elements, and are excluded on purpose.
_EXPLAINED_SECTIONS = [
    "sec-kpi", "sec-burndown", "sec-heatmap", "sec-forecast", "sec-tables",
    "sec-person-table", "sec-person-cards",
    "sec-eng-kpi", "sec-eng-chart", "sec-eng-table",
]


def _section_slice(out: str, section_id: str) -> str:
    start = out.index(f'id="{section_id}"')
    idx = _SECTION_ORDER.index(section_id)
    if idx + 1 < len(_SECTION_ORDER):
        end = out.index(f'id="{_SECTION_ORDER[idx + 1]}"', start)
    else:
        end = out.index("<footer", start)
    return out[start:end]


class ChartExplanationTests(unittest.TestCase):
    """Every chart, diagram and table must carry a non-empty description of
    what it shows and a non-empty note on what counts as good — a future
    section that ships without either must fail this test."""

    @classmethod
    def setUpClass(cls):
        cls.out = rh.render_html(build_multi_sprint_report(with_gitlab=True))

    def test_every_visual_section_has_a_description(self):
        for section_id in _EXPLAINED_SECTIONS:
            with self.subTest(section=section_id):
                section = _section_slice(self.out, section_id)
                m = re.search(r'<p class="section-desc">(.*?)</p>', section, re.S)
                self.assertIsNotNone(m, f"{section_id}: no <p class=\"section-desc\">")
                text = re.sub(r"<[^>]+>", "", m.group(1)).strip()
                self.assertGreater(len(text), 20, f"{section_id}: description too short/empty")

    def test_every_visual_section_states_what_is_good(self):
        # KPI sections carry the good/warn/bad bands in a collapsible
        # thresholds block instead of an inline "Хорошо" sentence — both
        # forms satisfy the requirement, so accept either.
        for section_id in _EXPLAINED_SECTIONS:
            with self.subTest(section=section_id):
                section = _section_slice(self.out, section_id)
                has_good_sentence = "Хорошо" in section
                has_thresholds_block = bool(re.search(r'<details class="thresholds">.*?</details>', section, re.S))
                self.assertTrue(
                    has_good_sentence or has_thresholds_block,
                    f"{section_id}: no 'what is good' note (neither a Хорошо sentence nor a thresholds block)",
                )

    def test_thresholds_are_marked_as_our_heuristic_not_the_module_standard(self):
        for section_id in ("sec-kpi", "sec-eng-kpi"):
            with self.subTest(section=section_id):
                section = _section_slice(self.out, section_id)
                m = re.search(r'<details class="thresholds">(.*?)</details>', section, re.S)
                self.assertIsNotNone(m, f"{section_id}: no thresholds block")
                block = m.group(1)
                self.assertIn("эвристика", block)
                self.assertIn("нет классификации «норма / под наблюдением / критично»", block)

    def test_forecast_marks_its_two_real_module_constants(self):
        # The one section where actual source-module constants exist
        # (forecast_available >= 10 points; CV warn threshold 50%) — they
        # must read as real, not lumped in with the invented KPI bands.
        section = _section_slice(self.out, "sec-forecast")
        self.assertIn("из исходного модуля", section)
        self.assertIn("10", section)
        self.assertIn("50", section)

    def test_kpi_thresholds_are_absent_from_the_no_gitlab_report_without_crashing(self):
        report = build_multi_sprint_report(with_gitlab=False)
        out = rh.render_html(report)
        self.assertIn("thresholds", out)


class RussianOnlyProseTests(unittest.TestCase):
    """The report must read in Russian: every label/status word/badge we
    author is Russian. Column headers and warning/error CODES keep their
    contract field names on purpose (documented, grep-able against the
    source) — this test only guards the prose we write ourselves."""

    def test_no_bare_english_good_warn_bad_in_our_own_prose(self):
        report = build_multi_sprint_report(with_gitlab=True)
        out = rh.render_html(report)
        for section_id in ("sec-kpi", "sec-eng-kpi", "sec-metrics", "sec-person-table", "sec-person-cards"):
            with self.subTest(section=section_id):
                section = _section_slice(out, section_id)
                # status WORDS, not the "warn-badge"/"cell-nodata" CSS classes
                self.assertNotRegex(section, r"[^-\"]\bgood\b")
                self.assertNotRegex(section, r"[^-\"]\bwarn\b(?!-)")
                self.assertNotRegex(section, r"[^-\"]\bbad\b")

    def test_kpi_tile_labels_are_russian(self):
        report = build_multi_sprint_report(with_gitlab=True)
        out = rh.render_html(report)
        for label in ("Скорость", "Скользящее среднее", "Выполнение обязательств", "Загрузка",
                      "Изменение объёма", "Пропускная способность", "Закрытие, задачи", "Закрытие, SP"):
            with self.subTest(label=label):
                self.assertIn(f'kpi-label">{label}<', out)
        for stale in ("Velocity", "Performance</span>", ">Load<", "Scope change", "Throughput</span>"):
            with self.subTest(stale=stale):
                self.assertNotIn(f'kpi-label">{stale}', out)

    def test_person_card_stat_labels_are_russian(self):
        report = build_multi_sprint_report(with_gitlab=True)
        out = rh.render_html(report)
        for label in ("доля смерженных", "медиана цикла MR", "задач закрыто", "доля переработок", "успешность пайплайнов"):
            with self.subTest(label=label):
                self.assertIn(label, out)


# --------------------------------------------------------------------------
# Closing recommendations — every item derived from a measured value
# --------------------------------------------------------------------------


class RecommendationsBlockTests(unittest.TestCase):
    @staticmethod
    def _healthy_report():
        metrics = {
            "committed_sp": 40.0, "committed_items": 10, "scope_added_items": 1, "scope_removed_items": 0,
            "scope_added_sp": 2.0, "scope_removed_sp": 1.0, "scope_change_pct": 7.5,
            "performance_pct": 95.0, "load_pct": 100.0, "velocity_sp": 38.0, "velocity_sma5_sp": 40.0,
            "throughput_items": 8, "closure_pct_items": 90.0, "closure_pct_sp": 90.0,
            "scope_estimation_change_sp": 0.0, "delivered_sp": 38.0, "delivered_items": 9,
        }
        return {
            "sprints": [
                {"target": False, "payload": {"metrics": dict(metrics)}},
                {"target": True, "payload": {"metrics": dict(metrics)}},
            ],
            "kpi": {},
            "forecast": {
                "warnings": [], "throughput_cv_pct": 10.0, "percentiles": [],
                "target_items": 5, "sample_sprints": 5, "sample_days": 50, "histogram": [],
            },
            "forecast_error": None,
            "engineering": {"available": False},
            "personal": {"available": False},
        }

    def test_healthy_fixture_says_no_obvious_problems(self):
        ctx: dict = {}
        rh.build_recommendations(self._healthy_report(), ctx)
        block = ctx["RECOMMENDATIONS_BLOCK"]
        self.assertIn("явных проблем не видно", block)
        self.assertIn('class="reco-none"', block)
        self.assertNotIn('class="reco-list"', block)

    def test_unhealthy_fixture_names_the_triggering_metric_and_value(self):
        # build_multi_sprint_report(with_gitlab=True) genuinely has
        # load_pct=26.67% (under-committed vs SMA5) and a 50% pipeline
        # success rate — verified triggers, not invented.
        report = build_multi_sprint_report(with_gitlab=True)
        ctx: dict = {}
        rh.build_recommendations(report, ctx)
        block = ctx["RECOMMENDATIONS_BLOCK"]
        self.assertIn('class="reco-list"', block)
        self.assertIn("Загрузка (load_pct) = 26.67%", block)
        self.assertIn("Успешность пайплайнов (pipelines.success_rate_pct) = 50", block)
        self.assertIn("<b>Действие:</b>", block)
        # ranked by severity: load_pct (75) must appear before pipelines (65)
        self.assertLess(block.index("Загрузка (load_pct)"), block.index("Успешность пайплайнов"))

    def test_forecast_unavailable_is_the_highest_severity_trigger(self):
        report = self._healthy_report()
        report["forecast"] = None
        report["forecast_error"] = "ERR_FORECAST_NOT_ENOUGH_DATA"
        ctx: dict = {}
        rh.build_recommendations(report, ctx)
        block = ctx["RECOMMENDATIONS_BLOCK"]
        self.assertIn("Прогноз не построен", block)
        self.assertIn("ERR_FORECAST_NOT_ENOUGH_DATA", block)
        # highest severity (90) — must be the first item when others also fire
        first_item = block.index('<li class="reco-item')
        self.assertEqual(block.index("Прогноз не построен"), block.index("reco-metric", first_item) + len('reco-metric">'))

    def test_rework_share_trigger_names_no_individual(self):
        report = self._healthy_report()
        report["personal"] = {
            "available": True,
            "data": {"people": [
                {"user": "alice", "rework_share": 0.5},
                {"user": "bob", "rework_share": 0.1},
                {"user": "carol", "rework_share": None},
            ]},
        }
        ctx: dict = {}
        rh.build_recommendations(report, ctx)
        block = ctx["RECOMMENDATIONS_BLOCK"]
        self.assertIn("Доля переработок", block)
        self.assertIn("1 из 3 участников", block)
        self.assertNotIn("alice", block)
        self.assertNotIn("bob", block)
        self.assertNotIn("carol", block)

    def test_recommendations_are_capped_at_seven(self):
        # Fire every trigger at once and confirm the list never exceeds the
        # documented cap, even though 8 conditions are true here.
        report = self._healthy_report()
        report["forecast"] = None
        report["forecast_error"] = "ERR_FORECAST_NOT_ENOUGH_DATA"
        m = report["sprints"][1]["payload"]["metrics"]
        m.update(performance_pct=50.0, load_pct=200.0, scope_change_pct=40.0,
                  committed_items=10, scope_removed_items=5)
        report["engineering"] = {
            "available": True,
            "data": {
                "pipelines": {"count": 10, "failed": 8, "success_rate_pct": 20.0, "per_project": []},
                "deployments": {"count": 0, "failed": 0, "success_rate_pct": None, "per_project": []},
                "coverage": {"coverage_avg_pct": None, "sample_count": 0, "per_project": []},
            },
        }
        report["personal"] = {
            "available": True,
            "data": {"people": [{"user": "a", "rework_share": 0.9}, {"user": "b", "rework_share": 0.1}]},
        }
        ctx: dict = {}
        rh.build_recommendations(report, ctx)
        block = ctx["RECOMMENDATIONS_BLOCK"]
        self.assertEqual(block.count('<li class="reco-item'), rh._RECO_MAX)

    def test_full_render_includes_a_reachable_printing_conclusions_section(self):
        report = build_multi_sprint_report(with_gitlab=True)
        out = rh.render_html(report)
        self.assertIn('id="sec-conclusions"', out)
        self.assertIn("Что можно улучшить", out)
        self.assertIn("не вердикт по людям", out)
        # outside every <div class="tabpanel" ...> gate, so it is visible
        # regardless of which tab is selected — not a tab-body screen-reader
        # trick, an unconditional section between </main> and <footer>.
        self.assertLess(out.index("</main>"), out.index('id="sec-conclusions"'))
        self.assertLess(out.index('id="sec-conclusions"'), out.index("<footer"))


if __name__ == "__main__":
    unittest.main()
