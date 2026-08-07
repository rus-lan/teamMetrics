"""Tests for render_html.py — the schema-v3, 9-tab HTML report renderer.

Built and tested exclusively against tests/fixtures/report_v3.json — a
fixture built for the team-metrics 3.1.0 render-layer fix release,
extending the retired report_v2.json fixture to a 9-sprint axis and 20+
people (one with no GitLab data) and adding every new §CONTRACT key
(recommendations, the restructured SP-based forecast, params.allowlist,
the aggregated gitlab_fetch_issues.deployment_warnings shape). No network,
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

_FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "report_v3.json")


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
        attrs_d = dict(attrs)
        node = {"tag": tag, "class": attrs_d.get("class", ""), "style": attrs_d.get("style", ""), "children": []}
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
        """A login is allowed inside an attribute value — `title=`
        tooltips, `id=`/`data-login=`/`data-forecast-scope=`/`<option
        value=`, all JS/CSS hooks §control A-C wire onto person-bound
        elements — never as text node content a reader would actually
        read. Strips every attribute value generically rather than
        chasing each new attribute name individually."""
        self.assertIn("Александр Максименков", self.html)
        no_code = re.sub(r"<code[^>]*>.*?</code>", "", self.html, flags=re.S)
        no_attrs = re.sub(r'([a-zA-Z-]+)="[^"]*"', "", no_code)
        self.assertNotIn("amaksimenkov", no_attrs)

    def test_coverage_risk_never_multiplied_by_100(self):
        """Regression for source bug (a): coverage 55.0 must print 55%, not 5500."""
        self.assertIn("55%", self.html)
        self.assertNotIn("5500", self.html)

    def test_every_chart_svg_has_hint_sibling(self):
        """A chart's SVG sits inside a `.scroll-x` wrapper (§ tab01/tab05
        donut-row hscroll fix) — the wrapper's own next sibling is the
        chart's explanation, optionally with a `.filter-empty` note
        and/or one `.chart-legend` div in between (§P1-6: the external
        HTML legend used by charts with too many series for an in-SVG
        one)."""
        tree = build_tree(self.html)
        total = 0
        with_hint = 0
        for node in walk(tree):
            children = node["children"]
            for i, c in enumerate(children):
                if c["tag"] == "div" and has_class(c, "scroll-x") and any(gc["tag"] == "svg" and has_class(gc, "chart") for gc in c["children"]):
                    total += 1
                    j = i + 1
                    while j < len(children) and (has_class(children[j], "filter-empty") or has_class(children[j], "chart-legend")):
                        j += 1
                    nxt = children[j] if j < len(children) else None
                    if nxt is not None and nxt["tag"] == "p" and has_class(nxt, "hint"):
                        with_hint += 1
        self.assertGreater(total, 0)
        self.assertEqual(total, with_hint, "every chart svg's .scroll-x wrapper must have an immediately-following <p class=hint> (optionally after .filter-empty/.chart-legend)")

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
        self.assertIn("Александр Оченьдлинно…", svg)  # visible text is the truncated form

    def test_bars_share_one_colour_not_a_per_row_palette(self):
        """§P1-7: a person is already named by the row label — a 21-colour
        palette on top of that carried no information."""
        items = [("Alice", 5.0), ("Bob", 3.0), ("Carol", 1.0)]
        svg = rh.build_hbar_svg("c1", "T", items)
        self.assertEqual(svg.count('class="c-bar"'), 3)
        for i in range(3):
            self.assertNotIn(f'class="s{i}"', svg)

    def test_logins_wrap_each_row_in_a_data_login_group(self):
        svg = rh.build_hbar_svg("c1", "T", [("Alice", 5.0), ("Bob", None)], logins=["alice", "bob"])
        self.assertIn('<g data-login="alice">', svg)
        self.assertIn('<g data-login="bob">', svg)

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
        self.assertIn(f'<div class="scroll-x">{svg}</div><p class="hint">hint text</p>', block)

    def test_real_svg_scroll_wrapper_prevents_page_overflow(self):
        """A chart's own inline min-width (e.g. a donut's 380px) must
        never leak past its card into the page — the SVG needs its own
        `.scroll-x` boundary, the same pattern already used by
        `chart_block_wide` (§ tab01/tab05 donut-row hscroll at 768px)."""
        svg = '<svg class="chart" id="c1" style="min-width:380px"></svg>'
        block = rh.chart_block("c1", "Title", svg, "hint text")
        self.assertIn(f'<div class="scroll-x">{svg}</div>', block)


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


class DonutSingleSegmentSameQuantityTest(unittest.TestCase):
    """Release 3.1.0 review, finding 1: a single-segment success/fail donut
    reused `center_label` (a rate pinned to the FIRST item) verbatim even
    when the surviving segment was the SECOND one — an all-failed pipeline
    donut showed "Упало: 0%" (the success rate) instead of "Упало: 100%".
    `is_rate_pair=True` makes the stat tile compute the surviving
    segment's own share of the pair instead of trusting `center_label`."""

    def test_all_failed_shows_the_failed_segments_own_rate(self):
        items = [("Успешно", 0), ("Упало", 5)]
        block = rh.donut_block("c1", "T", items, rh.fmt_pct(0), "hint", is_rate_pair=True)
        self.assertIn('<div class="stat-label">Упало</div>', block)
        self.assertIn('<div class="stat-value">100%</div>', block)
        self.assertNotIn('<div class="stat-value">0%</div>', block)

    def test_all_succeeded_shows_the_succeeded_segments_own_rate(self):
        items = [("Успешно", 5), ("Упало", 0)]
        block = rh.donut_block("c1", "T", items, rh.fmt_pct(100), "hint", is_rate_pair=True)
        self.assertIn('<div class="stat-label">Успешно</div>', block)
        self.assertIn('<div class="stat-value">100%</div>', block)

    def test_non_rate_pair_single_segment_keeps_using_center_label(self):
        """issue_type_dist-style donuts pass a total, not a rate pinned to
        one item — that total already equals the surviving segment's own
        count by construction, so `is_rate_pair` defaults to False and the
        tile is unaffected by this fix."""
        items = [("Задача", 5), ("Баг", 0)]
        block = rh.donut_block("c1", "T", items, rh.fmt_int(5), "hint")
        self.assertIn('<div class="stat-label">Задача</div>', block)
        self.assertIn('<div class="stat-value">5</div>', block)

    def test_all_failed_pipeline_donut_renders_correctly_end_to_end(self):
        report = load_fixture()
        report["engineering"]["pipelines"] = {
            "count": 5, "failed": 5, "success_rate_pct": 0.0, "per_week": 1.0, "per_project": [], "warnings": [],
        }
        report["overview"]["pipeline_success_rate_pct"] = 0.0
        html = rh.render_html(report)
        m = re.search(r'id="cb-chart-pipeline-success"[^>]*>.*?</div>\s*</div>', html, re.S)
        self.assertIsNotNone(m)
        self.assertIn("Упало", m.group(0))
        self.assertIn("100%", m.group(0))
        self.assertNotIn(">0%<", m.group(0))


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

    def test_out_files_hint_no_longer_claims_unqualified_column_compatibility(self):
        """Release 3.1.0 review, finding 6: this is the same "compatible by
        names AND columns" claim README.md/SKILL.md made — but rendered
        straight into every report on tab 08, so every reader sees it, not
        just someone who opens the docs."""
        report = load_fixture()
        html = rh.render_html(report)
        self.assertNotIn("совместимыми по именам и колонкам", html)
        self.assertIn("deployment_count", html)


# --------------------------------------------------------------------------
# 3.1.0 fix-release regression tests
# --------------------------------------------------------------------------


class LineFillNoneRegressionTest(unittest.TestCase):
    """P0-1: every line-chart path used to pick up an opaque fill from the
    later `.s0`..`.s9` palette rule (same specificity, later in the
    stylesheet -> wins per cascade order), turning an open polyline into
    a filled polygon spanning first point to last. `.c-line.sN` must
    carry `fill: none` at higher specificity so it wins regardless of
    declaration order."""

    def test_every_line_series_class_pins_fill_none(self):
        css = rh.TEMPLATE_PATH.read_text(encoding="utf-8")
        m = re.search(r"\.c-line\.s0[^{]*\{([^}]*)\}", css)
        self.assertIsNotNone(m, "no .c-line.sN combinator rule found in the template")
        self.assertIn("fill: none", m.group(1))
        for i in range(10):
            self.assertIn(f".c-line.s{i}", css)

    def test_burndown_has_no_area_fill_or_gradient(self):
        """The user's instruction: charts carry no start/end colour fill
        at all — the burndown's decorative gradient must be gone
        entirely, not just fixed."""
        css = rh.TEMPLATE_PATH.read_text(encoding="utf-8")
        self.assertNotIn(".c-area", css)
        self.assertNotIn("--chart-fill", css)
        self.assertNotIn("linearGradient", css)
        html = rh.render_html(load_fixture())
        self.assertNotIn("linearGradient", html)
        self.assertNotIn('class="c-area"', html)


class ChartFontFloorTest(unittest.TestCase):
    """P0-2: the viewBox width of a chart rendered into the responsive
    `.chart-grid` must not exceed the narrowest real column width (the
    CSS `minmax()` floor minus the chartbox's own padding) — otherwise
    the SVG is downscaled and 10-11px CSS text renders below the 11px
    floor. Checked structurally (CSS grid minimum, chartbox padding,
    each builder's fixed viewBox width) rather than in a browser."""

    _CHART_GRID_MIN = 520  # templates/report.html .chart-grid minmax()
    _CHARTBOX_PADDING = 32  # 16px left + 16px right
    _FONT_FLOOR = 11

    def test_css_chart_grid_matches_the_assumed_floor(self):
        css = rh.TEMPLATE_PATH.read_text(encoding="utf-8")
        self.assertIn(f"minmax({self._CHART_GRID_MIN}px, 1fr)", css)

    def test_categorical_builders_use_a_viewbox_no_wider_than_the_worst_case_column(self):
        rendered_width = self._CHART_GRID_MIN - self._CHARTBOX_PADDING
        cases = [
            rh.build_vbar_svg("c", "T", ["A", "B", "C"], [1, 2, 3]),
            rh.build_stacked_bar_svg("c", "T", ["A", "B", "C"], [("s", [1, 2, 3])]),
            rh.build_multiline_svg("c", "T", ["A", "B", "C"], [("s", [1.0, 2.0, 3.0])]),
        ]
        for svg in cases:
            m = re.search(r'viewBox="0 0 (\d+(?:\.\d+)?) ', svg)
            self.assertIsNotNone(m)
            self.assertLessEqual(float(m.group(1)), rendered_width, svg[:80])

    def test_axis_and_unit_label_font_sizes_meet_the_floor(self):
        css = rh.TEMPLATE_PATH.read_text(encoding="utf-8")
        for cls in (".c-tick-label", ".c-unit-label", ".c-marker-v-label"):
            m = re.search(re.escape(cls) + r"\s*\{[^}]*font-size:\s*([\d.]+)px", css)
            self.assertIsNotNone(m, f"{cls} rule not found")
            self.assertGreaterEqual(float(m.group(1)), self._FONT_FLOOR, cls)

    def test_wide_person_keyed_charts_pin_their_own_width_instead_of_shrinking(self):
        """A person count is unbounded (unlike the sprint axis), so
        build_grouped_bar_svg renders through the `.scroll-x` wide path
        (§P0-2) instead of squeezing into one grid column — its inline
        min-width must equal its own viewBox width, i.e. it always
        renders at scale 1, never downscaled."""
        svg = rh.build_grouped_bar_svg("c", "T", [f"P{i}" for i in range(15)], [("s", list(range(15)))])
        vb = re.search(r'viewBox="0 0 (\d+) ', svg)
        style = re.search(r'style="min-width:(\d+)px"', svg)
        self.assertIsNotNone(vb)
        self.assertIsNotNone(style)
        self.assertEqual(int(vb.group(1)), int(style.group(1)))


class AxisLabelHoistingTest(unittest.TestCase):
    """P0-3: a shared label prefix is hoisted into one axis caption
    instead of repeating on every tick (colliding neighbours); when no
    prefix applies and there are enough categories to collide, ticks
    rotate -45° instead."""

    def test_common_prefix_hoisted_and_suffixes_returned(self):
        caption, short = rh.shorten_axis_labels(["T100-T102 26.1", "T100-T102 26.2", "T100-T102 26.3"])
        self.assertEqual(caption, "T100-T102 26.")
        self.assertEqual(short, ["1", "2", "3"])

    def test_no_common_prefix_returns_labels_unchanged(self):
        caption, short = rh.shorten_axis_labels(["Sprint 41", "Alpha", "Beta"])
        self.assertIsNone(caption)
        self.assertEqual(short, ["Sprint 41", "Alpha", "Beta"])

    def test_prepare_category_axis_rotates_when_many_categories_share_no_prefix(self):
        _caption, _short, rotate = rh.prepare_category_axis(["Данилов Данил", "Кузнецова Мария", "Морозов Егор", "Соколова Ольга", "Волков Николай", "Никитина Светлана", "Захаров Павел"])
        self.assertTrue(rotate)

    def test_chart_caption_rendered_in_dom(self):
        svg = rh.build_vbar_svg("c", "T", ["1", "2"], [1.0, 2.0])
        block = rh.chart_block("c", "T", svg, "hint", caption="Sprint ")
        self.assertIn('<p class="axis-caption">Sprint …</p>', block)


class ForecastVerticalMarkerTest(unittest.TestCase):
    """P1-4: P50/P85/P95 are values on the X axis (story points), not a Y
    threshold — the marker is a vertical line (x1==x2, y1 != y2) spanning
    the plot height, not a horizontal one collapsed near the bottom."""

    def test_markers_are_vertical_not_horizontal(self):
        bins = [(5.0, 100), (10.0, 400), (15.0, 250)]
        percentiles = [(6.0, "P50"), (11.0, "P85"), (14.0, "P95")]
        svg = rh.build_forecast_histogram_svg("c", "T", bins, percentiles, "SP")
        markers = re.findall(r'<line class="c-marker-v" x1="([\d.]+)" y1="([\d.]+)" x2="([\d.]+)" y2="([\d.]+)"/>', svg)
        self.assertEqual(len(markers), 3)
        for x1, y1, x2, y2 in markers:
            self.assertAlmostEqual(float(x1), float(x2), places=1)
            self.assertNotAlmostEqual(float(y1), float(y2), places=1)

    def test_close_markers_stagger_onto_two_label_rows(self):
        bins = [(5.0, 100), (6.0, 400)]
        percentiles = [(5.0, "P50"), (5.05, "P85")]  # <40px apart on this chart's X axis
        svg = rh.build_forecast_histogram_svg("c", "T", bins, percentiles, "SP")
        labels_y = re.findall(r'<text class="c-marker-v-label" x="[\d.]+" y="([\d.]+)"', svg)
        self.assertEqual(len(labels_y), 2)
        self.assertNotEqual(labels_y[0], labels_y[1])

    def test_empty_bins_returns_none(self):
        self.assertIsNone(rh.build_forecast_histogram_svg("c", "T", [], [], "SP"))


class StatGridIsAGridTest(unittest.TestCase):
    """P1-5: `.stat-grid` declared `grid-template-columns` with no
    `display: grid` — computed display stayed `block`, so every `.stat`
    tile stretched full-width instead of tiling."""

    def test_stat_grid_declares_display_grid(self):
        css = rh.TEMPLATE_PATH.read_text(encoding="utf-8")
        m = re.search(r"\.stat-grid\s*\{([^}]*)\}", css)
        self.assertIsNotNone(m)
        self.assertIn("display: grid", m.group(1))


class NoChartBoxWithoutDataTest(unittest.TestCase):
    """P1-7: a person with no GitLab pipeline data used to get its own
    empty chartbox placeholder (13 repeats in production); a person with
    no data must produce zero placeholder boxes and one consolidated
    line naming them instead. A single-segment donut (a full ring
    conveys no comparison) becomes a plain numeric tile."""

    def test_no_gitlab_person_gets_no_pipeline_chartbox(self):
        report = load_fixture()
        html = rh.render_html(report)
        no_gitlab_idx = [i for i, p in enumerate(report["people"]) if p["metrics"].get("pipeline_success_rate_pct") is None]
        self.assertGreater(len(no_gitlab_idx), 0)
        for i in no_gitlab_idx:
            self.assertNotIn(f'id="cb-chart-p-pipe-{i}"', html)
        self.assertIn("Нет данных о пайплайнах GitLab", html)

    def test_single_segment_donut_becomes_a_stat_tile_not_a_ring(self):
        block = rh.donut_block("c1", "T", [("Успешно", 100.0)], "100%", "hint")
        self.assertIn("stat-tile-box", block)
        self.assertNotIn("<svg", block)

    def test_donut_block_empty_falls_back_to_standard_chart_block(self):
        block = rh.donut_block("c1", "T", [], "0", "hint")
        self.assertIn("Недостаточно данных для построения графика.", block)

    def test_two_segment_donut_still_a_real_ring(self):
        block = rh.donut_block("c1", "T", [("A", 30), ("B", 70)], "100", "hint")
        self.assertIn("<svg", block)
        self.assertNotIn("stat-tile-box", block)

    def test_deployments_table_shows_no_data_state_not_empty_skeleton(self):
        """§P1-9: an empty deployments table used to render a four-column
        <thead> over an empty <tbody>."""
        html = rh.build_tab07({
            "engineering": {
                "available": True, "reason_ru": None,
                "pipelines": {"count": 5, "failed": 1, "success_rate_pct": 80.0, "per_week": 1.0, "per_project": []},
                "deployments": {"count": 0, "failed": 0, "success_rate_pct": None, "per_week": None, "per_project": []},
                "coverage": {"coverage_avg_pct": None, "sample_count": 0, "per_project": []},
                "window_applied": {"merge_requests": True, "pipelines": True, "deployments": True, "coverage": True},
                "by_sprint": [],
            },
            "sprint_axis": [], "gitlab_fetch_issues": {},
        })
        m = re.search(r'<section class="section" id="sec-07-deployments">.*?</section>', html, re.S)
        self.assertIsNotNone(m)
        self.assertNotIn("<thead>", m.group(0))
        self.assertIn("Нет данных о деплоях.", m.group(0))


class DataLoginCoverageTest(unittest.TestCase):
    """§control C: every person-bound element carries `data-login` so the
    people multi-select (tabs 05/06) can toggle it, and a legend entry
    never exists without its matching drawn series (§P1-6)."""

    @classmethod
    def setUpClass(cls):
        cls.report = load_fixture()
        cls.html = rh.render_html(cls.report)
        cls.logins = [p["login"] for p in cls.report["people"]]

    def _section(self, section_id: str) -> str:
        m = re.search(rf'<section class="section" id="{section_id}">.*?</section>', self.html, re.S)
        self.assertIsNotNone(m, section_id)
        return m.group(0)

    def test_tab05_table_columns_carry_data_login(self):
        section = self._section("sec-05-table")
        for login in self.logins:
            self.assertIn(f'data-login="{login}"', section)

    def test_tab05_person_donut_pairs_carry_data_login(self):
        section = self._section("sec-05-dist")
        for login in self.logins:
            self.assertIn(f'data-login="{login}"', section)

    def test_tab05_hbar_rows_carry_data_login(self):
        section = self._section("sec-05-compare")
        for login in self.logins:
            self.assertIn(f'<g data-login="{login}">', section)

    def test_tab06_person_cards_carry_data_login(self):
        section = self._section("sec-06-cards")
        for login in self.logins:
            self.assertIn(f'data-login="{login}"', section)

    def test_tab06_multiline_series_wrapped_in_data_login_groups(self):
        section = self._section("sec-06-jira")
        found = set(re.findall(r'<g data-login="([^"]+)">', section))
        for login in self.logins:
            self.assertIn(login, found)

    def test_tab04_forecast_person_scopes_carry_data_login(self):
        section = self._section("sec-04-forecast")
        for p in self.report["forecast"]["people"]:
            self.assertIn(f'data-login="{p["login"]}"', section)

    def test_every_legend_entry_has_a_matching_drawn_series(self):
        section = self._section("sec-06-jira")
        legend_logins = set(re.findall(r'<span class="lg-item" data-login="([^"]+)">', section))
        series_logins = set(re.findall(r'<g data-login="([^"]+)">', section))
        self.assertTrue(legend_logins)
        self.assertTrue(legend_logins.issubset(series_logins))

    def test_series_with_no_values_dropped_from_legend(self):
        html = rh.build_multiline_svg(
            "c", "T", ["S1", "S2"],
            [("Has data", [1.0, 2.0]), ("No data", [None, None])],
            external_legend=True, series_logins=["has", "none"],
        )
        legend = rh.chart_legend_html([("Has data", "s0", "has"), ("No data", "s1", "none")])
        # simulate the same filter build_tab06 applies before calling chart_legend_html
        self.assertIn('data-login="has"', html)
        self.assertIn('data-login="none"', html)  # the (invisible) <g> wrapper still exists
        self.assertIn("Has data", legend)  # legend text itself is caller-filtered, not builder-filtered


class PeopleFilterPrototypePollutionTest(unittest.TestCase):
    """Release 3.1.0 review, finding 4: the people multi-select (tabs
    05/06) built its `hidden` lookup as a plain `{}` object literal — for
    the login `constructor` (a legal GitLab username), `hidden["constructor"]`
    resolved to the inherited `Function`, which is truthy, so that person's
    rows/series stayed hidden no matter their checkbox state. Same class of
    bug for `toString`/`valueOf`/`hasOwnProperty`. Fixed with
    `Object.create(null)`, which carries no prototype to collide with."""

    def test_hidden_lookup_is_not_a_plain_object_literal(self):
        html = rh.render_html(load_fixture())
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, flags=re.S)
        self.assertEqual(len(scripts), 1)
        self.assertIn("Object.create(null)", scripts[0])
        self.assertNotIn("var hidden = {}", scripts[0])

    def test_login_named_constructor_renders_normal_data_login_attributes(self):
        report = load_fixture()
        person = copy.deepcopy(report["people"][0])
        person["login"] = "constructor"
        report["people"].append(person)
        html = rh.render_html(report)
        self.assertIn('data-login-check="constructor"', html)
        self.assertIn('data-login="constructor"', html)


class NoJsGracefulDegradationTest(unittest.TestCase):
    """§Graceful degradation: with JavaScript disabled, every sprint
    block, both forecast scopes, and all people must stay visible — the
    static markup never starts anything in a hidden state; JS only
    narrows it, never reveals it."""

    @classmethod
    def setUpClass(cls):
        cls.report = load_fixture()
        cls.html = rh.render_html(cls.report)

    def test_no_element_starts_with_the_hidden_class(self):
        """`.tm-hidden` legitimately appears in <style>/<script> (the CSS
        rule and the classList.toggle target) — this checks no rendered
        element's `class="..."` attribute actually carries it."""
        classes = re.findall(r'class="([^"]*)"', self.html)
        for cls in classes:
            self.assertNotIn("tm-hidden", cls.split())

    def test_every_axis_sprint_block_present(self):
        for s in self.report["sprint_axis"]:
            self.assertIn(f'data-sprint-id="{s["id"]}"', self.html)

    def test_both_forecast_scopes_present(self):
        self.assertIn('data-forecast-scope="team"', self.html)
        for p in self.report["forecast"]["people"]:
            self.assertIn(f'data-forecast-scope="{rh._forecast_person_scope(p["login"])}"', self.html)

    def test_control_elements_hidden_only_via_css_not_markup(self):
        """The <select>/checkbox controls themselves are the JS-only
        pieces (hidden by html.js CSS, same pattern as the theme switch)
        — the content they control is never gated in the markup."""
        css = rh.TEMPLATE_PATH.read_text(encoding="utf-8")
        self.assertIn("html.js .control-bar", css)
        self.assertIn("html.js .people-filter", css)

    def test_script_body_only_narrows_via_classlist_toggle(self):
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", self.html, flags=re.S)
        self.assertEqual(len(scripts), 1)
        self.assertIn("classList.toggle", scripts[0])


class RecommendationsTest(unittest.TestCase):
    """§CONTRACT: recommendations restores the standalone section the
    previous version had, rendered under the KPI tiles on tab 01."""

    def test_recommendations_rendered_with_severity_and_action(self):
        report = load_fixture()
        html = rh.render_html(report)
        m = re.search(r'<section class="section" id="sec-01-recommendations">.*?</section>', html, re.S)
        self.assertIsNotNone(m)
        section = m.group(0)
        for r in report["recommendations"]:
            self.assertIn(r["metric_ru"], section)
            self.assertIn(r["action_ru"], section)
        self.assertIn("Действие:", section)
        self.assertIn('data-severity="bad"', section)
        self.assertIn(report["recommendations_intro_ru"], section)

    def test_empty_recommendations_shows_empty_text(self):
        report = load_fixture()
        report["recommendations"] = []
        html = rh.render_html(report)
        self.assertIn(report["recommendations_empty_ru"], html)


class AllowlistTest(unittest.TestCase):
    def test_allowlist_section_rendered(self):
        report = load_fixture()
        html = rh.render_html(report)
        self.assertIn("Allowlist сотрудников", html)
        self.assertIn(str(report["params"]["allowlist"]["configured_count"]), html)

    def test_allowlist_absent_when_not_in_params(self):
        report = load_fixture()
        del report["params"]["allowlist"]
        html = rh.render_html(report)
        self.assertNotIn("Allowlist сотрудников", html)
        assert_tags_balanced(self, html)

    def test_allowlist_hint_names_the_real_config_key(self):
        """Release 3.1.0 review, finding 2: the config file's real key is
        `employees` (config.py load_file_config, .team-metrics.example.json)
        — the report used to teach readers a key that does not exist,
        `allowlist.logins`, which silently does nothing if typed in."""
        report = load_fixture()
        html = rh.render_html(report)
        self.assertIn("employees в .team-metrics.json", html)
        self.assertNotIn("allowlist.logins", html)


class DeploymentWarningsAggregationTest(unittest.TestCase):
    """P1-8: the aggregated §CONTRACT shape (one entry per code with
    projects_count/projects) replaces one row per project — never print a
    raw HTTP response body."""

    def test_aggregated_form_renders_project_count_and_collapsed_list(self):
        report = load_fixture()
        html = rh.render_html(report)
        m = re.search(r'<section class="section" id="sec-07-completeness">.*?</section>', html, re.S)
        self.assertIsNotNone(m)
        section = m.group(0)
        self.assertIn("затронуто 5 проектов", section)
        self.assertIn("<details>", section)
        self.assertIn("group/mobile", section)


class ForecastContractTest(unittest.TestCase):
    """§CONTRACT forecast: SP-delivered-per-sprint, team + per-person
    scopes (§control B)."""

    def test_team_scope_renders_percentile_labels(self):
        report = load_fixture()
        html = rh.render_html(report)
        for p in report["forecast"]["team"]["percentiles"]:
            self.assertIn(p["label_ru"], html)

    def test_unavailable_person_shows_reason(self):
        report = load_fixture()
        html = rh.render_html(report)
        unavailable = [p for p in report["forecast"]["people"] if not p["available"]]
        self.assertGreater(len(unavailable), 0)
        for p in unavailable:
            self.assertIn(p["unavailable_reason_ru"], html)

    def test_forecast_select_has_team_plus_one_option_per_person(self):
        report = load_fixture()
        html = rh.render_html(report)
        m = re.search(r'<select id="forecast-scope-select"[^>]*>.*?</select>', html, re.S)
        self.assertIsNotNone(m)
        options = re.findall(r'<option value="[^"]*"', m.group(0))
        self.assertEqual(len(options), 1 + len(report["forecast"]["people"]))

    def test_team_unavailable_still_shows_available_person_forecasts(self):
        """report_data.py's `forecast.available` reflects the TEAM scope
        only — `people` entries are built independently and can be
        `available: true` even when the team-wide bootstrap has too few
        closed sprints. A person with real data must never be hidden just
        because the team scope failed."""
        report = load_fixture()
        report["forecast"]["team"] = None
        report["forecast"]["available"] = False
        report["forecast"]["error"] = {"code": "ERR_FORECAST_NOT_ENOUGH_DATA", "message_ru": "Недостаточно данных для командного прогноза.", "detail": None}
        html = rh.render_html(report)
        assert_tags_balanced(self, html)
        available_person = next(p for p in report["forecast"]["people"] if p["available"])
        self.assertIn(f'data-forecast-scope="{rh._forecast_person_scope(available_person["login"])}"', html)
        for perc in available_person["percentiles"]:
            self.assertIn(perc["label_ru"], html)
        self.assertIn("Недостаточно данных для командного прогноза.", html)
        m = re.search(r'<select id="forecast-scope-select"[^>]*>.*?</select>', html, re.S)
        self.assertIsNotNone(m)

    def test_team_and_all_people_unavailable_shows_single_message(self):
        report = load_fixture()
        report["forecast"] = {
            "available": False, "unit_ru": "SP",
            "error": {"code": "ERR_FORECAST_NOT_ENOUGH_DATA", "message_ru": "Недостаточно данных для прогноза.", "detail": None},
            "team": None, "people": [],
        }
        html = rh.render_html(report)
        assert_tags_balanced(self, html)
        self.assertIn("Недостаточно данных для прогноза.", html)
        self.assertNotIn('id="forecast-scope-select"', html)


class ForecastScopeLoginCollisionTest(unittest.TestCase):
    """Release 3.1.0 review, finding 5: the team scope used the sentinel
    value "team" while a person's scope used their raw login — a person
    whose GitLab/Jira login is literally `team` produced a duplicate
    `<option value="team">` and their block shared `data-forecast-scope`
    with the whole-team block. `_forecast_person_scope` namespaces every
    person's scope so it can never equal the sentinel."""

    def test_login_named_team_does_not_collide_with_the_team_sentinel(self):
        report = load_fixture()
        person = copy.deepcopy(report["forecast"]["people"][0])
        person["login"] = "team"
        person["display_name"] = "Person Named Team"
        report["forecast"]["people"].append(person)
        html = rh.render_html(report)

        self.assertEqual(html.count('data-forecast-scope="team"'), 1)
        self.assertIn(f'data-forecast-scope="{rh._forecast_person_scope("team")}"', html)
        self.assertEqual(len(re.findall(r'<option value="team"', html)), 1)
        self.assertIn(f'<option value="{rh._forecast_person_scope("team")}">', html)
        # the collision-prone `data-login` attribute (unrelated to the
        # scope sentinel) is untouched and still carries the raw login.
        self.assertIn('data-forecast-scope="person-team" data-login="team"', html)


class SprintSelectorTest(unittest.TestCase):
    """§control A: every axis sprint gets an option; burndown/heatmap/
    breakdown span the whole axis, not just target sprints."""

    def test_select_lists_every_axis_sprint(self):
        report = load_fixture()
        html = rh.render_html(report)
        m = re.search(r'<select id="sprint-select"[^>]*>.*?</select>', html, re.S)
        self.assertIsNotNone(m)
        options = re.findall(r'<option value="(\d+)"', m.group(0))
        self.assertEqual({int(o) for o in options}, {s["id"] for s in report["sprint_axis"]})

    def test_target_sprint_preselected(self):
        report = load_fixture()
        html = rh.render_html(report)
        primary = rh.primary_target_axis(report)
        self.assertIn(f'<option value="{primary["id"]}" selected>', html)

    def test_empty_sprint_shows_explicit_no_data_state(self):
        report = load_fixture()
        html = rh.render_html(report)
        self.assertIn("В этом спринте нет задач", html)


# ==========================================================================
# §v31 re-audit fixes (before the 3.1.0 release)
# ==========================================================================


class LegendSwatchBackgroundTest(unittest.TestCase):
    """Finding 1 (HIGH): `.s0`-`.s9` only ever set SVG `fill`/`stroke` —
    an HTML `<i class="lg-swatch sN">` (tab06's external chart legend)
    needs its own `background-color` rule to actually show its series
    colour. Every swatch that carries a palette class must have one."""

    def test_every_palette_class_has_a_swatch_background_rule(self):
        css = rh.TEMPLATE_PATH.read_text(encoding="utf-8")
        for i in range(10):
            pattern = r"\.chart-legend \.lg-swatch\.s%d\s*\{[^}]*background:\s*var\(--s%d\)" % (i, i)
            self.assertIsNotNone(re.search(pattern, css), f"no background rule for .lg-swatch.s{i}")

    def test_rendered_swatch_carries_its_series_palette_class(self):
        html = rh.chart_legend_html([("Alice", "s0", "alice"), ("Bob", "s3", "bob")])
        self.assertIn('<i class="lg-swatch s0">', html)
        self.assertIn('<i class="lg-swatch s3">', html)

    def test_no_other_html_element_reuses_a_bare_palette_class(self):
        """The sweep the finding asked for: `chart_legend_html` is the
        only place render_html.py puts an s0-s9 class on anything other
        than an SVG shape (rect/path/circle paint fill/stroke fine
        without a background rule)."""
        html = rh.render_html(load_fixture())
        for tag_match in re.finditer(r"<(\w+)([^>]*\bclass=\"[^\"]*\bs[0-9]\b[^\"]*\")", html):
            tag = tag_match.group(1)
            self.assertIn(tag, ("rect", "path", "circle", "i"), f"unexpected tag carrying a palette class: <{tag}>")


class RotatedLabelReachTest(unittest.TestCase):
    """Finding 2 (HIGH): tab05's «Cycle time по сотрудникам» chart rotates
    long names -45° — the reserved canvas (height below the axis, and
    left margin for the first tick) must scale with the actual label
    length instead of a fixed guess, or long names overflow the
    viewBox's bottom/left edge."""

    def test_short_labels_keep_the_historical_fixed_reserve(self):
        svg = rh.build_grouped_bar_svg("c", "T", ["A", "B", "C"], [("s", [1, 2, 3])], rotate_labels=True)
        vb = re.search(r'viewBox="0 0 (\d+) (\d+)"', svg)
        self.assertIsNotNone(vb)
        self.assertEqual(int(vb.group(2)), 200 + 18 + 52)  # bottom(rotated) + the old fixed 52px reserve

    def test_long_labels_grow_the_reserved_height_and_left_margin(self):
        # 20 categories, matching tab05's real person count — with fewer
        # categories the bars are wide enough that the first tick already
        # sits far enough right for a long name not to clip, which would
        # leave the left margin unchanged and defeat this assertion.
        long_names = [f"Александр Константинович Дальний {i}" for i in range(20)]
        short_names = [f"P{i}" for i in range(20)]
        svg_long = rh.build_grouped_bar_svg("c", "T", long_names, [("s", list(range(20)))], rotate_labels=True)
        svg_short = rh.build_grouped_bar_svg("c", "T", short_names, [("s", list(range(20)))], rotate_labels=True)
        w_long, h_long = (int(x) for x in re.search(r'viewBox="0 0 (\d+) (\d+)"', svg_long).groups())
        w_short, h_short = (int(x) for x in re.search(r'viewBox="0 0 (\d+) (\d+)"', svg_short).groups())
        self.assertGreater(h_long, h_short)
        self.assertGreater(w_long, w_short)

    def test_reserved_height_covers_the_estimated_rotated_extent(self):
        names = ["Иванова Мария Александровна", "A"]
        svg = rh.build_grouped_bar_svg("c", "T", names, [("s", [1, 2]), ("s2", [1, 2])], rotate_labels=True)
        short_labels = [rh.truncate(n, 16) for n in names]
        reach = rh._rotated_label_reach(short_labels)
        _w, h = (int(x) for x in re.search(r'viewBox="0 0 (\d+) (\d+)"', svg).groups())
        bottom = 200 + 18
        tick_baseline_y = bottom + 14
        self.assertGreaterEqual(h - tick_baseline_y, reach)

    def test_first_tick_never_clips_past_the_left_edge(self):
        names = ["Иванова Мария Александровна двадцать", "Б"]
        svg = rh.build_grouped_bar_svg("c", "T", names, [("s", [1, 2])], rotate_labels=True)
        short_labels = [rh.truncate(n, 16) for n in names]
        reach = rh._rotated_label_reach(short_labels)
        first_tick = re.search(r'<text class="c-tick-label" x="([\d.]+)"', svg)
        self.assertIsNotNone(first_tick)
        self.assertGreaterEqual(float(first_tick.group(1)), reach)

    def test_non_rotated_charts_are_untouched(self):
        """rotate_labels=False must stay byte-identical to before this
        fix — the reach reserve only ever applies to the rotated path."""
        svg = rh.build_grouped_bar_svg("c", "T", ["Александр Константинович Дальний"] * 3, [("s", [1, 2, 3])], rotate_labels=False)
        vb = re.search(r'viewBox="0 0 (\d+) (\d+)"', svg)
        self.assertEqual(int(vb.group(2)), 200 + 52)


class NoUnscrolledMinWidthTest(unittest.TestCase):
    """Finding 3 (MEDIUM): wide content must scroll inside its own
    container — a chart svg or table with a min-width (inline style or
    a min-width-bearing CSS class) must always sit inside a
    `.scroll-x`/`.table-wrap`/`.mini-wrap` ancestor, so the page itself
    never scrolls sideways (§768px hscroll audit)."""

    _MIN_WIDTH_CSS_CLASSES = {"chart-svg", "heatmap", "sprint-mini"}
    _SCROLL_CLASSES = {"scroll-x", "table-wrap", "mini-wrap"}

    @classmethod
    def setUpClass(cls):
        cls.html = rh.render_html(load_fixture())

    def test_sweep_for_unwrapped_min_width_elements(self):
        tree = build_tree(self.html)
        offenders = []

        def visit(node, inside_scroll):
            classes = set((node.get("class") or "").split())
            now_scroll = inside_scroll or bool(classes & self._SCROLL_CLASSES)
            style = node.get("style") or ""
            has_inline_min_width = "min-width" in style
            has_class_min_width = bool(classes & self._MIN_WIDTH_CSS_CLASSES) or (
                node["tag"] == "table" and bool(classes & {"xwide", "wide", "mid"})
            )
            if (has_inline_min_width or has_class_min_width) and not now_scroll:
                offenders.append((node["tag"], sorted(classes), style))
            for child in node["children"]:
                visit(child, now_scroll)

        for root in tree:
            visit(root, False)
        self.assertEqual(offenders, [])

    def test_burndown_panels_wrap_their_svg_in_scroll_x(self):
        html = rh.build_unit_toggle("c", '<svg style="min-width:760px"></svg>', '<svg style="min-width:760px"></svg>')
        self.assertEqual(html.count('<div class="scroll-x">'), 2)

    def test_donut_row_svgs_no_longer_overflow_their_flex_row(self):
        """§ tab01/tab05 donut-row/donut-pair regression: a donut's own
        380px inline min-width used to have no scroll boundary at all
        between it and the page (chart_block didn't wrap its svg)."""
        svg = '<svg class="chart" id="c1" style="min-width:380px"></svg>'
        block = rh.chart_block("c1", "T", svg, "hint")
        self.assertIn(f'<div class="scroll-x">{svg}</div>', block)


class PeopleFilterEmptyStateTest(unittest.TestCase):
    """Finding 4 (MEDIUM): filtering out every person must not leave a
    hollow chart/table with axes/headers and no explanation — every
    people-filterable chart/table carries a `data-people-scope` marker
    and a `.filter-empty` note, and the filter script (JS-time, not
    render-time) flags the scope empty once every one of its
    `[data-login]` children is hidden."""

    @classmethod
    def setUpClass(cls):
        cls.html = rh.render_html(load_fixture())

    def _section(self, section_id: str) -> str:
        m = re.search(rf'<section class="section" id="{section_id}">.*?</section>', self.html, re.S)
        self.assertIsNotNone(m, section_id)
        return m.group(0)

    def test_every_people_filterable_section_has_a_scoped_note(self):
        for section_id in (
            "sec-05-compare", "sec-05-cycle", "sec-05-table", "sec-05-dist",
            "sec-05-individual", "sec-06-jira", "sec-06-gitlab", "sec-06-cards",
        ):
            section = self._section(section_id)
            self.assertIn("data-people-scope", section, section_id)
            self.assertIn('class="filter-empty"', section, section_id)
            self.assertIn(rh._PEOPLE_FILTER_EMPTY_TEXT, section, section_id)

    def test_filter_empty_note_hidden_until_js_flags_its_scope(self):
        css = rh.TEMPLATE_PATH.read_text(encoding="utf-8")
        self.assertRegex(css, r"\.filter-empty\s*\{[^}]*display:\s*none")
        self.assertIn("[data-people-scope].people-scope-empty > .filter-empty", css)

    def test_js_toggles_people_scope_empty_from_data_login_state(self):
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", self.html, flags=re.S)
        self.assertEqual(len(scripts), 1)
        self.assertIn('querySelectorAll("[data-people-scope]")', scripts[0])
        self.assertIn("people-scope-empty", scripts[0])

    def test_chart_block_adds_scope_and_note_only_when_opted_in(self):
        svg = '<svg class="chart" id="c1"></svg>'
        scoped = rh.chart_block("c1", "T", svg, "hint", people_scope=True)
        self.assertIn("data-people-scope", scoped)
        self.assertIn(rh._PEOPLE_FILTER_EMPTY_TEXT, scoped)
        unscoped = rh.chart_block("c1", "T", svg, "hint")
        self.assertNotIn("data-people-scope", unscoped)
        self.assertNotIn("filter-empty", unscoped)

    def test_chart_block_wide_adds_scope_and_note_only_when_opted_in(self):
        svg = '<svg class="chart-svg" id="c1"></svg>'
        scoped = rh.chart_block_wide("c1", "T", svg, "hint", people_scope=True)
        self.assertIn("data-people-scope", scoped)
        self.assertIn(rh._PEOPLE_FILTER_EMPTY_TEXT, scoped)
        unscoped = rh.chart_block_wide("c1", "T", svg, "hint")
        self.assertNotIn("data-people-scope", unscoped)

    def test_scope_never_applies_to_a_structurally_empty_chart(self):
        """A chart with no data at all (svg_or_none is None) already
        explains itself at render time — it must not also grow an
        (always-empty) people-filter scope."""
        block = rh.chart_block("c1", "T", None, "hint", people_scope=True)
        self.assertNotIn("data-people-scope", block)


class TableScrollNoteCoverageTest(unittest.TestCase):
    """Finding 5 (MEDIUM): a height-capped `.table-wrap` (420px, no
    `no-cap`/`scroll-x`) must carry its own scroll note — with its own
    real row count — whenever it actually has more than fits, not just
    on the one table that happened to remember to pass `total_rows`."""

    @classmethod
    def setUpClass(cls):
        cls.report = load_fixture()
        cls.html = rh.render_html(cls.report)

    def test_scroll_note_helper_thresholds_at_ten_rows(self):
        self.assertEqual(rh._table_scroll_note(10), "")
        self.assertIn("11", rh._table_scroll_note(11))
        self.assertEqual(rh._table_scroll_note(None), "")

    def test_tab05_metrics_table_gets_a_note_with_its_real_row_count(self):
        """§ audit: 28 rows / 1702px scrollHeight inside a 420px cap
        rendered no note at all before this fix."""
        section = re.search(r'<section class="section" id="sec-05-table">.*?</section>', self.html, re.S).group(0)
        note = re.search(r"всего (\d+) строк", section)
        self.assertIsNotNone(note, "tab05's 05.3 table has no scroll-note even though it has >10 rows")
        claimed = int(note.group(1))
        tbody = re.search(r"<tbody>(.*?)</tbody>", section, re.S).group(1)
        self.assertEqual(claimed, tbody.count("<tr>"))
        self.assertGreater(claimed, 10)

    def test_tab09_reference_tables_have_no_cap_and_no_spurious_note(self):
        section = re.search(r'<section class="section" id="sec-09-metric-defs">.*?</section>', self.html, re.S)
        self.assertIsNotNone(section)
        self.assertIn("table-wrap no-cap", section.group(0))
        self.assertNotIn("scroll-note", section.group(0))

    def test_table_hint_still_appends_exactly_one_hint_paragraph(self):
        block = rh.table_hint("<table></table>", "hint text", total_rows=50)
        self.assertTrue(block.endswith('<p class="hint">hint text</p>'))
        self.assertEqual(block.count('class="hint"'), 1)


class DonutWallCollapseTest(unittest.TestCase):
    """Finding 6 (LOW): tab05's per-person donut wall (38 SVGs across 20
    people) is the single largest block in the report — it collapses
    into a native `<details>` (the mechanism §01.5's «Как читать цифры»
    already uses), reachable and expandable without any JS."""

    @classmethod
    def setUpClass(cls):
        cls.report = load_fixture()
        cls.html = rh.render_html(cls.report)

    def _dist_section(self) -> str:
        m = re.search(r'<section class="section" id="sec-05-dist">.*?</section>', self.html, re.S)
        self.assertIsNotNone(m)
        return m.group(0)

    def test_donut_wall_is_a_details_element_collapsed_by_default(self):
        section = self._dist_section()
        m = re.search(r'<details class="donut-wall"[^>]*>', section)
        self.assertIsNotNone(m)
        self.assertNotIn(" open", m.group(0))

    def test_donut_wall_carries_a_summary_with_the_people_count(self):
        section = self._dist_section()
        people_count = len(self.report.get("people") or [])
        self.assertIn(f"— {people_count} ", section)
        self.assertIn("<summary>", section)

    def test_every_person_donut_pair_still_present_inside_the_collapsed_wall(self):
        """Collapsed by default must not mean data-dropped — every
        person's donut-pair card stays in the markup, just inside
        <details>, reachable with a plain click and no JS."""
        section = self._dist_section()
        for p in self.report.get("people") or []:
            self.assertIn(f'data-login="{p["login"]}"', section)

    def test_tab05_panel_height_proxy_shrinks(self):
        """A rough proxy for the audited 17738px panel height: the
        donut-wall body (38 svgs) must sit after the </details>-closing
        boundary in document order relative to section 05.5, i.e. behind
        a single collapsed disclosure rather than 38 inline svgs."""
        section = self._dist_section()
        self.assertLess(section.count("<svg"), len(re.findall(r'data-login="', section)) * 3)


if __name__ == "__main__":
    unittest.main()
