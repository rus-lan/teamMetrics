"""Tests for render_html.py — the schema-v2, 9-tab HTML report renderer.

Built and tested exclusively against tests/fixtures/report_v2.json (a copy
of .research/v3-redesign/fixture-report-v2.json — the fixture IS the
contract between the data track and this one, per SPEC.md §F). No network,
no report_data/jira_client/gitlab_client involvement anywhere in this file.
"""

import _pathfix  # noqa: F401

import copy
import json
import os
import re
import unittest
from html.parser import HTMLParser

from team_metrics import render_html as rh

_FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "report_v2.json")


def load_fixture() -> dict:
    with open(_FIXTURE_PATH, encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------
# Structural HTML helpers (tag balance, chart/hint DOM adjacency)
# --------------------------------------------------------------------------

_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class _TagBalanceParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.errors = []

    def handle_starttag(self, tag, attrs):
        if tag not in _VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        if tag in _VOID_TAGS:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"</{tag}> does not match top of stack {self.stack[-3:]}")
            return
        self.stack.pop()


def assert_tags_balanced(testcase: unittest.TestCase, html_text: str):
    parser = _TagBalanceParser()
    parser.feed(html_text)
    parser.close()
    testcase.assertEqual(parser.errors, [], f"HTML tag mismatches: {parser.errors}")
    testcase.assertEqual(parser.stack, [], f"unclosed tags at end of document: {parser.stack}")


class _TreeBuilder(HTMLParser):
    """Builds a plain nested-dict tree (tag/class/children) so tests can walk
    parent/sibling relationships — html.parser gives only a linear event
    stream, and the chart/hint adjacency rule (§H.8) needs siblings."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.roots = []

    def _push(self, tag, attrs):
        node = {"tag": tag, "class": dict(attrs).get("class", ""), "children": []}
        (self.stack[-1]["children"] if self.stack else self.roots).append(node)
        return node

    def handle_starttag(self, tag, attrs):
        node = self._push(tag, attrs)
        if tag not in _VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self._push(tag, attrs)

    def handle_endtag(self, tag):
        if tag in _VOID_TAGS:
            return
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i]["tag"] == tag:
                del self.stack[i:]
                return


def build_tree(html_text: str):
    p = _TreeBuilder()
    p.feed(html_text)
    p.close()
    return p.roots


def has_class(node: dict, cls: str) -> bool:
    return cls in (node.get("class") or "").split()


def walk(nodes):
    for n in nodes:
        yield n
        yield from walk(n["children"])


# --------------------------------------------------------------------------
# Full-render tests against the fixture
# --------------------------------------------------------------------------


class RenderFixtureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = load_fixture()
        cls.html = rh.render_html(cls.report)

    def test_no_unresolved_tokens(self):
        self.assertNotIn("{{", self.html)
        self.assertNotIn("}}", self.html)

    def test_tags_balanced(self):
        assert_tags_balanced(self, self.html)

    def test_determinism_byte_identical(self):
        html2 = rh.render_html(load_fixture())
        self.assertEqual(self.html, html2)
        html3 = rh.render_html(self.report)
        self.assertEqual(self.html, html3)

    def test_nine_tab_labels_present(self):
        labels = [
            "01 Обзор", "02 Спринт", "03 Динамика команды", "04 Прогноз",
            "05 Люди — сравнение", "06 Люди — динамика", "07 Инженерия",
            "08 Данные", "09 Словарь и риски",
        ]
        for label in labels:
            self.assertIn(label, self.html, f"missing tab label {label!r}")

    def test_exactly_nine_tab_radio_inputs(self):
        matches = re.findall(r'<input class="tab-input" type="radio" name="report-tab"', self.html)
        self.assertEqual(len(matches), 9)

    def test_no_snake_case_leak_outside_code(self):
        no_code = re.sub(r"<code[^>]*>.*?</code>", "", self.html, flags=re.S)
        leaks = sorted(set(re.findall(r"\b[a-z][a-z0-9]+(?:_[a-z0-9]+)+\b", no_code)))
        self.assertEqual(leaks, [], f"snake_case machine keys leaked outside <code>: {leaks}")

    def test_known_forbidden_keys_absent_outside_code(self):
        no_code = re.sub(r"<code[^>]*>.*?</code>", "", self.html, flags=re.S)
        forbidden = [
            "mr_merge_rate_pct", "task_cycle_time_avg_hours", "success_rate_pct",
            "coverage_avg_pct", "sample_count", "window_applied", "calc_schema_version",
            "connection_id", "force_refresh", "job_id", "target_items", "per_project",
            "committed_sp",
        ]
        for key in forbidden:
            self.assertNotIn(key, no_code, f"{key!r} leaked outside <code>")

    def test_no_bare_warning_codes_outside_code(self):
        no_code = re.sub(r"<code[^>]*>.*?</code>", "", self.html, flags=re.S)
        codes = re.findall(r"WARN_[A-Z_]*|ERR_[A-Z_]*", no_code)
        self.assertEqual(codes, [], f"bare warning/error codes outside <code>: {codes}")

    def test_no_external_urls_or_script_src(self):
        self.assertNotIn("https://", self.html)
        self.assertNotIn("//cdn", self.html)
        self.assertNotIn("<script src", self.html.lower())
        self.assertNotIn("<link href", self.html.lower())
        # the SVG xmlns declaration is the only allowed http:// occurrence
        for m in re.finditer(r"http://[^\"\s]*", self.html):
            self.assertEqual(m.group(0), "http://www.w3.org/2000/svg")

    def test_js_disabled_structure(self):
        """Tab switching, theme, and the burndown unit toggle must all be
        CSS-radio driven — the inline <script> must contain nothing that
        gates content visibility."""
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", self.html, flags=re.S)
        self.assertEqual(len(scripts), 1)
        body = scripts[0]
        self.assertNotIn("innerHTML", body)
        self.assertIn("theme", body.lower())
        for i in range(1, 10):
            self.assertIn(f'id="tab-{i:02d}"', self.html)
            self.assertIn(f'id="panel-{i:02d}"', self.html)

    def test_people_shown_by_display_name_not_bare_login(self):
        self.assertIn("Александр Максименков", self.html)
        no_code = re.sub(r"<code[^>]*>.*?</code>", "", self.html, flags=re.S)
        no_title = re.sub(r'title="[^"]*"', "", no_code)
        self.assertNotIn("amaksimenkov", no_title)

    def test_coverage_risk_never_multiplied_by_100(self):
        """Regression for source bug (a): coverage 55.0 must print 55%, not 5500."""
        self.assertIn("55%", self.html)
        self.assertNotIn("5500", self.html)

    def test_every_chart_svg_has_hint_sibling(self):
        tree = build_tree(self.html)
        total = 0
        with_hint = 0
        for node in walk(tree):
            children = node["children"]
            for i, c in enumerate(children):
                if c["tag"] == "svg" and has_class(c, "chart"):
                    total += 1
                    if i + 1 < len(children) and children[i + 1]["tag"] == "p" and has_class(children[i + 1], "hint"):
                        with_hint += 1
        self.assertGreater(total, 0)
        self.assertEqual(total, with_hint, "every chart svg must have an immediately-following <p class=hint>")

    def test_every_table_has_hint_in_its_section(self):
        tree = build_tree(self.html)

        def section_has_table_and_hint(node):
            found_table = any(n["tag"] == "table" for n in walk([node]))
            found_hint = any(n["tag"] == "p" and has_class(n, "hint") for n in walk([node]))
            return found_table, found_hint

        sections = [n for n in walk(tree) if n["tag"] == "section" and has_class(n, "section")]
        checked = 0
        for sec in sections:
            has_table, has_hint = section_has_table_and_hint(sec)
            if has_table:
                checked += 1
                self.assertTrue(has_hint, f"section with a table has no .hint paragraph: {sec}")
        self.assertGreater(checked, 0)

    def test_glossary_and_metric_defs_from_json_not_hardcoded(self):
        for term in ("PR cycle time", "Velocity SMA5", "CV (коэффициент вариации)"):
            self.assertIn(term, self.html)
        self.assertIn("Число MR", self.html)
        self.assertIn("mr_count", self.html)  # allowed: inside <code> in tab 09

    def test_risks_rendered(self):
        self.assertIn("Высокий defect rate", self.html)
        self.assertIn("Низкое покрытие тестами", self.html)

    def test_jira_labels_shown_as_is_in_code_chip(self):
        self.assertIn("tech_bucket", self.html)
        # must be inside a <code> chip, not bare
        self.assertRegex(self.html, r"<code[^>]*>tech_bucket</code>")


# --------------------------------------------------------------------------
# Contract enforcement
# --------------------------------------------------------------------------


class ContractTest(unittest.TestCase):
    def test_missing_top_level_key_raises_template_error(self):
        report = load_fixture()
        del report["board_kpi"]
        with self.assertRaises(rh.TemplateError):
            rh.render_html(report)

    def test_missing_sprint_axis_raises(self):
        report = load_fixture()
        del report["sprint_axis"]
        with self.assertRaises(rh.TemplateError):
            rh.render_html(report)

    def test_no_target_sprint_raises(self):
        report = load_fixture()
        for s in report["sprint_axis"]:
            s["target"] = False
        with self.assertRaises(rh.TemplateError):
            rh.render_html(report)

    def test_people_unavailable_renders_reason_not_crash(self):
        report = load_fixture()
        report["people_available"] = False
        report["people_reason_ru"] = "GitLab не настроен для этого запуска"
        html = rh.render_html(report)
        self.assertIn("GitLab не настроен для этого запуска", html)
        assert_tags_balanced(self, html)

    def test_engineering_unavailable_renders_reason_not_crash(self):
        report = load_fixture()
        report["engineering"] = {"available": False, "reason_ru": "GitLab не настроен", "pipelines": {}, "deployments": {}, "coverage": {}, "window_applied": {}, "by_sprint": []}
        html = rh.render_html(report)
        self.assertIn("GitLab не настроен", html)
        assert_tags_balanced(self, html)

    def test_forecast_unavailable_renders_reason_not_crash(self):
        report = load_fixture()
        report["forecast"] = {"available": False, "error": {"code": "ERR_FORECAST_NOT_ENOUGH_DATA", "message_ru": "Недостаточно данных для прогноза.", "detail": None}}
        html = rh.render_html(report)
        self.assertIn("Недостаточно данных для прогноза.", html)
        assert_tags_balanced(self, html)


# --------------------------------------------------------------------------
# Formatting primitives
# --------------------------------------------------------------------------


class FormattingTest(unittest.TestCase):
    def test_fmt_num_strips_trailing_zeros(self):
        self.assertEqual(rh.fmt_num(21.0, 1), "21")
        self.assertEqual(rh.fmt_num(21.5, 1), "21.5")
        self.assertEqual(rh.fmt_num(None, 1), rh.EM_DASH)

    def test_fmt_pct(self):
        self.assertEqual(rh.fmt_pct(83.3), "83.3%")
        self.assertEqual(rh.fmt_pct(None), rh.EM_DASH)

    def test_bool_ru(self):
        self.assertEqual(rh.bool_ru(True), "да")
        self.assertEqual(rh.bool_ru(False), "нет")
        self.assertEqual(rh.bool_ru(None), rh.EM_DASH)

    def test_esc_escapes_hostile_input(self):
        hostile = "<script>alert(1)</script>&\"'"
        out = rh.esc(hostile)
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)

    def test_auto_code_wrap_wraps_snake_case_only(self):
        text = rh.esc("см. committed_sp и Обычный текст")
        out = rh.auto_code_wrap(text)
        self.assertIn("<code>committed_sp</code>", out)
        self.assertIn("Обычный текст", out)

    def test_id_safe_has_no_underscore(self):
        self.assertNotIn("_", rh.id_safe("avg_estimates"))


# --------------------------------------------------------------------------
# §G new SVG builders
# --------------------------------------------------------------------------


class DonutSvgTest(unittest.TestCase):
    def test_empty_when_total_zero(self):
        self.assertIsNone(rh.build_donut_svg("c1", "T", [("A", 0), ("B", 0)], "0"))
        self.assertIsNone(rh.build_donut_svg("c1", "T", [], "0"))

    def test_share_math_and_deterministic_id(self):
        svg = rh.build_donut_svg("chart-x", "T", [("A", 30), ("B", 70)], "100")
        self.assertIsNotNone(svg)
        self.assertIn('id="chart-x"', svg)
        self.assertIn("30 (30.0%)", svg)
        self.assertIn("70 (70.0%)", svg)
        # same input -> byte-identical output (no global counters / random ids)
        svg2 = rh.build_donut_svg("chart-x", "T", [("A", 30), ("B", 70)], "100")
        self.assertEqual(svg, svg2)


class HbarSvgTest(unittest.TestCase):
    def test_none_row_prints_no_data(self):
        svg = rh.build_hbar_svg("c1", "T", [("Alice", 5.0), ("Bob", None)])
        self.assertIn("нет данных", svg)

    def test_long_label_truncated_with_title(self):
        long_name = "Александр Оченьдлинноеимя Фамилиев"
        svg = rh.build_hbar_svg("c1", "T", [(long_name, 5.0)])
        self.assertIn(f"<title>{long_name}</title>", svg)
        self.assertIn("Александр Очень…", svg)  # visible text is the truncated form

    def test_empty_items_returns_none(self):
        self.assertIsNone(rh.build_hbar_svg("c1", "T", []))


class GroupedStackedBarTest(unittest.TestCase):
    def test_grouped_bar_empty(self):
        self.assertIsNone(rh.build_grouped_bar_svg("c1", "T", [], []))
        self.assertIsNone(rh.build_grouped_bar_svg("c1", "T", ["A"], []))

    def test_stacked_bar_reference_line_present(self):
        svg = rh.build_stacked_bar_svg("c1", "T", ["S1", "S2"], [("A", [10, 20]), ("B", [5, 5])], ref_line=100.0)
        self.assertIsNotNone(svg)
        self.assertIn("100", svg)
        self.assertIn('class="c-marker"', svg)

    def test_stacked_bar_segments_present_for_each_series(self):
        svg = rh.build_stacked_bar_svg("c1", "T", ["S1"], [("A", [10]), ("B", [20]), ("C", [5])])
        self.assertIn("A · S1: 10", svg)
        self.assertIn("B · S1: 20", svg)
        self.assertIn("C · S1: 5", svg)


class VbarSvgTest(unittest.TestCase):
    def test_single_ref_line(self):
        svg = rh.build_vbar_svg("c1", "T", ["S1", "S2"], [80.0, 60.0], ref_line=80.0)
        self.assertIn('class="c-marker"', svg)

    def test_multiple_ref_lines_for_forecast_histogram(self):
        svg = rh.build_vbar_svg("c1", "T", ["7", "9", "13"], [214, 2900, 1500], ref_lines=[(9, "P50"), (13, "P85"), (16, "P95")])
        self.assertEqual(svg.count('class="c-marker"'), 3)

    def test_empty_values_returns_none(self):
        self.assertIsNone(rh.build_vbar_svg("c1", "T", [], []))

    def test_none_value_skips_bar_but_keeps_label(self):
        svg = rh.build_vbar_svg("c1", "T", ["S1", "S2"], [None, 5.0])
        self.assertIsNotNone(svg)


class MultilineSvgTest(unittest.TestCase):
    def test_empty_when_all_none(self):
        self.assertIsNone(rh.build_multiline_svg("c1", "T", ["S1", "S2"], [("A", [None, None])]))

    def test_gaps_for_none_points(self):
        svg = rh.build_multiline_svg("c1", "T", ["S1", "S2", "S3"], [("A", [1.0, None, 3.0])], show_trend=False)
        self.assertIsNotNone(svg)
        # only 2 real points -> 2 circles, and since they are non-adjacent no
        # connecting path segment is drawn between them
        self.assertEqual(svg.count("<circle"), 2)

    def test_ols_trend_endpoints_known_series(self):
        # y = 2x + 1 exactly over x=0,1,2 -> k=2, b=1 (hand-computed)
        k, b = rh.ols_trend([(0, 1.0), (1, 3.0), (2, 5.0)])
        self.assertAlmostEqual(k, 2.0)
        self.assertAlmostEqual(b, 1.0)

    def test_ols_trend_single_point(self):
        k, b = rh.ols_trend([(0, 5.0)])
        self.assertEqual((k, b), (0.0, 5.0))

    def test_ols_trend_zero_variance_x(self):
        k, b = rh.ols_trend([(0, 1.0), (0, 3.0)])
        self.assertEqual(k, 0.0)
        self.assertAlmostEqual(b, 2.0)

    def test_show_trend_draws_dashed_line(self):
        svg = rh.build_multiline_svg("c1", "T", ["S1", "S2", "S3"], [("A", [1.0, 2.0, 3.0])], show_trend=True)
        self.assertIn('class="c-trend"', svg)

    def test_deterministic_ids(self):
        svg1 = rh.build_multiline_svg("chart-y", "T", ["S1", "S2"], [("A", [1.0, 2.0])])
        svg2 = rh.build_multiline_svg("chart-y", "T", ["S1", "S2"], [("A", [1.0, 2.0])])
        self.assertEqual(svg1, svg2)
        self.assertIn('id="chart-y"', svg1)


class ChartBlockTest(unittest.TestCase):
    def test_none_svg_renders_empty_box(self):
        block = rh.chart_block("c1", "Title", None, "hint text")
        self.assertIn("Недостаточно данных для построения графика.", block)
        self.assertIn("<p class=\"hint\">hint text</p>", block)

    def test_real_svg_wrapped_with_hint_sibling(self):
        svg = '<svg class="chart" id="c1"></svg>'
        block = rh.chart_block("c1", "Title", svg, "hint text")
        self.assertIn(svg, block)
        self.assertTrue(block.rstrip().endswith("</div>"))
        self.assertIn(f"{svg}<p class=\"hint\">hint text</p>", block)


class WarnCatalogTest(unittest.TestCase):
    def test_collects_every_code_message_pair_in_the_report(self):
        report = load_fixture()
        catalog = rh.warn_catalog(report)
        self.assertIn("WARN_BASELINE_SHORT", catalog)
        self.assertIn("WARN_OUTSIDE_SPRINTS", catalog)
        self.assertIn("NOT_FOUND", catalog)
        self.assertIn("FILTER_REJECTED_FALLBACK", catalog)
        self.assertIn("PAGINATION_LIMIT", catalog)
        self.assertTrue(all(isinstance(v, str) and v for v in catalog.values()))


# --------------------------------------------------------------------------
# Review findings regression tests
# --------------------------------------------------------------------------


class HtmlInjectionTest(unittest.TestCase):
    """Finding 1 (CRITICAL): tab 08's parameters table used to emit a value
    RAW whenever it happened to start with "<" — reachable via board.name
    and params.sprint_names, both attacker-influenced Jira free text. This
    is an end-to-end test through render_html(), not just esc() in
    isolation, because the hole was in how a value reached esc(), not in
    esc() itself."""

    def test_hostile_board_name_renders_as_inert_text(self):
        report = load_fixture()
        report["board"]["name"] = '<img src=x onerror=alert(document.domain)>Team Board'
        html = rh.render_html(report)
        assert_tags_balanced(self, html)
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img src=x onerror=alert(document.domain)&gt;Team Board", html)

    def test_hostile_sprint_name_renders_as_inert_text(self):
        report = load_fixture()
        report["params"]["sprint_ids"] = []
        report["params"]["sprint_names"] = ["<script>alert(1)</script>"]
        html = rh.render_html(report)
        assert_tags_balanced(self, html)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)

    def test_no_data_driven_raw_html_branch_left_in_renderer(self):
        import inspect

        source = inspect.getsource(rh)
        self.assertNotIn("startswith('<')", source)
        self.assertNotIn('startswith("<")', source)


class ParamsTableEscapingTest(unittest.TestCase):
    def test_tool_version_escaped_exactly_once(self):
        """Finding 7: values used to be pre-escaped via esc_or_dash() and
        then escaped again at render time, turning `&` into `&amp;amp;`."""
        report = load_fixture()
        report["params"]["tool_version"] = '3.0.0 "R&D"'
        html = rh.render_html(report)
        self.assertIn("3.0.0 &quot;R&amp;D&quot;", html)
        self.assertNotIn("&amp;quot;", html)
        self.assertNotIn("&amp;amp;", html)


class DonutFullCircleTest(unittest.TestCase):
    """Finding 2: a 100% (or 0%-after-filtering) share made the arc's start
    and end points coincide, and SVG 1.1 F.6.2 drops such an arc entirely,
    leaving an empty donut."""

    def test_hundred_percent_share_renders_visible_ring(self):
        svg = rh.build_donut_svg("c1", "T", [("Успешно", 100.0)], "100")
        self.assertIsNotNone(svg)
        self.assertIn('fill-rule="evenodd"', svg)
        self.assertEqual(svg.count(" A "), 4)

    def test_zero_percent_share_after_filtering_still_renders(self):
        svg = rh.build_donut_svg("c1", "T", [("Успешно", 0), ("Не успешно", 100)], "100")
        self.assertIsNotNone(svg)
        self.assertIn('fill-rule="evenodd"', svg)

    def test_single_segment_distribution_renders(self):
        svg = rh.build_donut_svg("c1", "T", [("Task", 20)], "20")
        self.assertIsNotNone(svg)
        self.assertIn('fill-rule="evenodd"', svg)

    def test_two_segment_donut_unaffected(self):
        svg = rh.build_donut_svg("c1", "T", [("A", 30), ("B", 70)], "100")
        self.assertNotIn("fill-rule", svg)


class BreakdownTableStatusCategoryTest(unittest.TestCase):
    def test_uses_shared_status_category_label_not_a_second_hardcoded_dict(self):
        """Finding 4: build_breakdown_table used to carry its own
        new/indeterminate/done/cancelled -> Russian dict instead of
        report["labels"]["status_categories"] — an override in the JSON
        would then disagree with the heatmap legend built from the shared
        lookup."""
        report = load_fixture()
        report["labels"]["status_categories"]["done"] = "Кастомное Готово"
        bd = {"rows": [{"key": "X-1", "final_status": "Done", "final_status_category": "done", "delivered": True}]}
        table = rh.build_breakdown_table(report, bd)
        self.assertIn("Кастомное Готово", table)
        self.assertNotIn(">Готово<", table)


class AxisLabelDecimalsTest(unittest.TestCase):
    """Finding 5: Y-axis gridline labels were always rounded to a fixed
    decimal count, independent of the actual step size, producing
    duplicate adjacent labels whenever the step was smaller than 1."""

    def test_whole_number_step_needs_no_decimals(self):
        self.assertEqual(rh.axis_label_decimals([0, 1, 2, 3]), 0)

    def test_fractional_step_gets_enough_decimals_to_stay_distinct(self):
        self.assertEqual(rh.axis_label_decimals([0, 0.5, 1, 1.5, 2, 2.5]), 1)

    def test_vbar_small_integer_series_has_no_duplicate_y_labels(self):
        svg = rh.build_vbar_svg("c", "Throughput (задач)", ["S1", "S2", "S3"], [1, 2, 1])
        labels = re.findall(r'<text class="c-unit-label"[^>]*>([^<]+)</text>', svg)
        self.assertGreater(len(labels), 0)
        self.assertEqual(len(labels), len(set(labels)), f"duplicate y-axis labels: {labels}")

    def test_multiline_collapsed_range_has_no_duplicate_y_labels(self):
        svg = rh.build_multiline_svg("c", "T", ["S1", "S2", "S3", "S4", "S5"], [("Rework", [2.9, 3.0, 3.05, 3.1, 3.12])], show_trend=False)
        labels = re.findall(r'<text class="c-unit-label"[^>]*>([^<]+)</text>', svg)
        self.assertGreater(len(labels), 0)
        self.assertEqual(len(labels), len(set(labels)), f"duplicate y-axis labels: {labels}")


class KpiTileStatusNoneTest(unittest.TestCase):
    """Finding 6: status "none" means "no threshold defined for this tile",
    not "no data" — the renderer printed the invented «нет базы» directly
    under a real value, which reads as "there is no data"."""

    def test_status_none_renders_no_invented_label(self):
        tile = {"key": "x", "label_ru": "X", "value": 10.0, "unit_ru": "шт", "target_ru": None, "status": "none", "series": [], "hint_ru": "h"}
        html = rh.build_kpi_tile_html(tile)
        self.assertNotIn("нет базы", html)
        self.assertIn('<div class="delta"></div>', html)

    def test_status_good_still_shows_its_label(self):
        tile = {"key": "x", "label_ru": "X", "value": 90.0, "unit_ru": "%", "target_ru": None, "status": "good", "series": [], "hint_ru": "h"}
        html = rh.build_kpi_tile_html(tile)
        self.assertIn("норма", html)

    def test_fixture_render_has_no_invented_label(self):
        report = load_fixture()
        html = rh.render_html(report)
        self.assertNotIn("нет базы", html)


class DisplayNameForLoginTest(unittest.TestCase):
    """Finding 8: GitLab MR-fetch-error rows showed a bare login even when
    people[] carried a matching display_name for it."""

    def test_mr_fetch_error_author_shown_by_display_name(self):
        report = load_fixture()
        html = rh.render_html(report)
        m = re.search(r'<section class="section" id="sec-07-completeness">.*?</section>', html, re.S)
        self.assertIsNotNone(m)
        section = m.group(0)
        self.assertIn("Ирина Петрова", section)
        no_title = re.sub(r'title="[^"]*"', "", section)
        self.assertNotIn("ipetrova", no_title)

    def test_unknown_login_falls_back_to_the_login_itself(self):
        report = load_fixture()
        self.assertEqual(rh.display_name_for_login(report, "someone-not-in-people"), "someone-not-in-people")
        self.assertEqual(rh.display_name_for_login(report, ""), "")


class StackedBarNegativeValueTest(unittest.TestCase):
    def test_negative_value_segments_stay_within_viewbox(self):
        """Finding 9: a negative running sum pushed a rect's y/height
        entirely outside the viewBox (fixed zero-at-bottom assumption)."""
        svg = rh.build_stacked_bar_svg("c1", "T", ["a"], [("x", [-5.0]), ("y", [3.0])])
        self.assertIsNotNone(svg)
        vb = re.search(r'viewBox="0 0 (\d+) (\d+)"', svg)
        vb_h = float(vb.group(2))
        rects = re.findall(r'<rect[^>]*\sy="(-?[\d.]+)"[^>]*\sheight="([\d.]+)"', svg)
        self.assertGreater(len(rects), 0)
        for y_str, h_str in rects:
            y, h = float(y_str), float(h_str)
            self.assertGreaterEqual(y, 0)
            self.assertLessEqual(y + h, vb_h)

    def test_all_non_negative_values_unaffected(self):
        svg1 = rh.build_stacked_bar_svg("c1", "T", ["S1"], [("A", [10]), ("B", [20])])
        svg2 = rh.build_stacked_bar_svg("c1", "T", ["S1"], [("A", [10]), ("B", [20])])
        self.assertEqual(svg1, svg2)
        self.assertIn("A · S1: 10", svg1)
        self.assertIn("B · S1: 20", svg1)


class Tab05EmptyPeopleTest(unittest.TestCase):
    """Finding 10: an empty people[] with people_available=True rendered a
    heading with no body (05.4) and a one-column table of metric names with
    no "нет данных" marker (05.3)."""

    def test_empty_people_list_renders_standard_empty_state(self):
        report = load_fixture()
        report["people_available"] = True
        report["people"] = []
        html = rh.render_html(report)
        assert_tags_balanced(self, html)
        m1 = re.search(r'<section class="section" id="sec-05-table">.*?</section>', html, re.S)
        self.assertIn('class="empty"', m1.group(0))
        m2 = re.search(r'<section class="section" id="sec-05-dist">.*?</section>', html, re.S)
        self.assertIn('class="empty"', m2.group(0))


class FmtNumInfinityTest(unittest.TestCase):
    """Finding 11: fmt_num/fmt_int called int() on the rounded value
    unconditionally — NaN/Inf (both valid to json.loads by default) raised
    a bare ValueError/OverflowError instead of the standard no-data dash."""

    def test_fmt_num_handles_nan_and_inf(self):
        self.assertEqual(rh.fmt_num(float("nan"), 1), rh.EM_DASH)
        self.assertEqual(rh.fmt_num(float("inf"), 1), rh.EM_DASH)
        self.assertEqual(rh.fmt_num(float("-inf"), 1), rh.EM_DASH)

    def test_fmt_int_handles_nan_and_inf(self):
        self.assertEqual(rh.fmt_int(float("nan")), rh.EM_DASH)
        self.assertEqual(rh.fmt_int(float("inf")), rh.EM_DASH)
        self.assertEqual(rh.fmt_int(float("-inf")), rh.EM_DASH)

    def test_render_survives_nan_value_in_report(self):
        report = load_fixture()
        report["overview"]["pr_cycle_time_avg_hours"] = float("nan")
        html = rh.render_html(report)
        assert_tags_balanced(self, html)


class GitlabWindowNullWordingTest(unittest.TestCase):
    def test_null_gitlab_window_has_readable_wording(self):
        """Finding 12: a null gitlab_window rendered em-dash/en-dash/em-dash
        mashed together as the period text."""
        report = load_fixture()
        report["params"]["gitlab_window"] = None
        html = rh.render_html(report)
        self.assertNotIn(f"{rh.EM_DASH}–{rh.EM_DASH}", html)
        self.assertIn("не определён", html)


class Tab09CodeTableSplitTest(unittest.TestCase):
    def test_report_codes_and_transport_codes_are_in_separate_tables(self):
        """Finding 13: WARN_*/ERR_* report codes and transport/diagnostic
        codes (HTTP_500, NOT_FOUND, ...) used to sit in one table."""
        report = load_fixture()
        html = rh.render_html(report)
        m = re.search(r'<section class="section" id="sec-09-warnings">.*?</section>', html, re.S)
        self.assertIsNotNone(m)
        section = m.group(0)
        idx_warn_heading = section.index("Коды предупреждений")
        idx_warn_code = section.index("WARN_BASELINE_SHORT")
        idx_transport_heading = section.index("Технические коды")
        idx_transport_code = section.index("NOT_FOUND")
        self.assertLess(idx_warn_heading, idx_warn_code)
        self.assertLess(idx_warn_code, idx_transport_heading)
        self.assertLess(idx_transport_heading, idx_transport_code)


class Tab08FileInventoryTest(unittest.TestCase):
    """Finding 14: the out/ file table was a static 21-row list rendered
    unconditionally, over-reporting under --no-gitlab (7 CSVs never
    written)."""

    _GITLAB_ONLY_FILES = [
        "gitlab_mrs.csv", "gitlab_pipelines.csv", "gitlab_deployments.csv",
        "gitlab_coverage.csv", "gitlab_users.csv", "gitlab_by_sprint.csv", "report_merged.csv",
    ]

    def test_gitlab_only_files_dropped_when_engineering_unavailable(self):
        report = load_fixture()
        report["engineering"] = {
            "available": False, "reason_ru": "GitLab не настроен",
            "pipelines": {}, "deployments": {}, "coverage": {}, "window_applied": {}, "by_sprint": [],
        }
        html = rh.render_html(report)
        for name in self._GITLAB_ONLY_FILES:
            self.assertNotIn(f"<code>{name}</code>", html, f"{name} should not be listed without GitLab")

    def test_gitlab_files_present_when_engineering_available(self):
        report = load_fixture()
        html = rh.render_html(report)
        for name in self._GITLAB_ONLY_FILES:
            self.assertIn(f"<code>{name}</code>", html)


if __name__ == "__main__":
    unittest.main()
