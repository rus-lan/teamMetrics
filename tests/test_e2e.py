"""Full end-to-end coverage of the `team-metrics` CLI: init -> check -> run ->
report, with Jira and GitLab mocked strictly at the HTTP contract level.

## Seam mocked, and why

Both `JiraClient` and `GitLabClient` build their own `urllib.request` opener
internally. `JiraClient.__init__` always does
`urllib.request.build_opener(_NoRedirect)` with no constructor parameter to
override it. `GitLabClient.__init__` *does* take an `opener=` kwarg, but
`cli.py`'s real `main()` never threads one through — `cmd_check`/`cmd_run`
only accept `jira_client_cls`/`gitlab_client_cls` overrides (used by
tests/test_cli.py's DI-based tests), and `main()` itself calls both with
neither override. So through a real `cli.main(argv, environ)` invocation —
which is what this file drives, exactly like a user typing
`scripts/team-metrics ...` — there is no reachable seam on either client
object. The one seam both share is `urllib.request.build_opener` itself:
every test here patches that name to return a `fixtures.http_fake.FakeOpener`
serving real API-shaped fixture responses (tests/fixtures/wire.py), so the
whole init/check/run/report cycle runs through the actual CLI entry point
with a fake transport underneath, never a fake client class.

## Fixture inventory (tests/fixtures/)

- `http_fake.py`: generic path-routed fake `urllib.request` opener
  (`FakeOpener`/`FakeResponse`), auth-header helpers, and `RouteNotFound`.
- `wire.py`: one coherent scenario's worth of real Jira/GitLab wire payloads
  — one board, two closed history sprints, one active target sprint (name
  carries a literal quote), three Jira issues (one delivered in history, one
  added-but-unfinished in the target sprint, one delivered in the target
  sprint via a truncated-then-refetched changelog), one GitLab project whose
  path carries markup, and merge requests/pipelines/deployments/coverage for
  it. `build_opener(...)` wires all of that into one `FakeOpener` that both
  clients share.

## Why the hostile "issue summary" lives in the fixture but isn't asserted as escaped

The team-lead brief asked for hostile fixture data including "an issue
summary containing `<script>alert(1)</script>`" with its escaping asserted
in the output. `wire.PROJ3_DTO["fields"]["summary"]` carries exactly that
string, for wire-shape realism (Jira really does return `summary`, and
`jira_client.py`'s `COMMON_SEARCH_FIELD_NAMES` really does request it) — but
tracing the pipeline shows `summary` is fetched and then never stored
anywhere (`jira_client.RawIssue` has no summary field, and
`report_data.py`/`model.py` never reference `dto["fields"]["summary"]`
either): grep confirms `"summary"` appears in this codebase exactly once
outside this comment, as a search field name. So a hostile summary can never
reach render_html.py — asserting it "comes out escaped" would be asserting
nothing. The assignee login (`wire.HOSTILE_USER`, rendered via
`render_html.PERSON_USER = esc(person["user"])`) carries that assertion
instead: it is the same shape of untrusted string, genuinely rendered, in a
field JIRA's own wire format would let contain far stranger text than a
plain login (see the module-level docstring in wire.py).

## Determinism and `generated_at`

`build_combined_report()`'s `now` parameter defaults to `datetime.now(UTC)`,
and neither `report_data.py`'s CLI nor `cli.py`'s `run` subcommand exposes a
way to override it (no `--now` flag anywhere in `config.add_pipeline_args`).
Two real `run` invocations at different wall-clock instants therefore always
produce different `params.generated_at` — by design, a report legitimately
stamps when it was built. To honor the "same seed -> byte-identical output"
requirement (which is really about the seeded Monte-Carlo forecast, not the
timestamp) without changing product behavior, `_frozen_now()` below patches
`team_metrics.report_data.datetime` with a `datetime` subclass whose `.now()`
returns a fixed instant — a standard freezegun-style technique, applied only
inside this test module.
"""

import _pathfix  # noqa: F401

import contextlib
import html as html_lib
import io
import json
import logging
import os
import re
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

from team_metrics import cli
from team_metrics import config as config_mod
from team_metrics import metrics as metrics_mod

from fixtures import wire
from fixtures.http_fake import FakeOpener

UTC = timezone.utc

# --------------------------------------------------------------------------
# schema v2 / out/ inventory constants (SPEC .research/v3-redesign/SPEC.md
# §B, §C.12, §H) — kept local to this file the same way the check-item name
# constants above are: this module and test_cli.py are each a standalone
# `unittest discover` module and deliberately don't import each other.
# --------------------------------------------------------------------------

# SPEC §H.2 — the exact top-level key set of a schema-v2 report.json, no
# extras, nothing missing.
TOP_LEVEL_SCHEMA_V2_KEYS = frozenset(
    {
        "schema_version", "board", "params", "warnings", "sprint_axis", "sprints", "board_kpi",
        "overview", "burndown", "heatmap", "issue_breakdown", "forecast", "people_available",
        "people_reason_ru", "people", "people_individual_jira", "engineering", "team_series",
        "people_series", "export_tables", "glossary", "metric_defs", "risks",
        "recommendations", "recommendations_empty_ru", "recommendations_intro_ru", "labels",
        "semantics_notes", "gitlab_fetch_issues",
    }
)

# SPEC §C.12 — all 18 out/ CSV filenames `run` writes.
ALL_CSV_NAMES = frozenset(
    {
        "gitlab_mrs.csv", "gitlab_pipelines.csv", "gitlab_deployments.csv", "gitlab_coverage.csv",
        "gitlab_users.csv", "jira_users.csv", "jira_issues.csv", "jira_cycle_time.csv",
        "jira_rework.csv", "jira_throughput.csv", "sprints.csv", "jira_by_sprint.csv",
        "gitlab_by_sprint.csv", "report_per_employee.csv", "report_team.csv", "report_merged.csv",
        "heatmap.csv", "board.csv",
    }
)
# SPEC §H.12 — the CSVs that are GitLab-derived and therefore absent under
# `--no-gitlab` (the 6 "gitlab_"-prefixed CSVs, plus report_merged.csv which
# joins MRs to Jira issues).
GITLAB_ONLY_CSV_NAMES = frozenset(
    {
        "gitlab_mrs.csv", "gitlab_pipelines.csv", "gitlab_deployments.csv", "gitlab_coverage.csv",
        "gitlab_users.csv", "gitlab_by_sprint.csv", "report_merged.csv",
    }
)
JIRA_ONLY_CSV_NAMES = ALL_CSV_NAMES - GITLAB_ONLY_CSV_NAMES

# SPEC §C.12 — the 7 out/raw/*.json files a GitLab-configured run writes.
ALL_RAW_JSON_NAMES = frozenset(
    {
        "jira_issue_facts.json", "jira_sprints.json", "gitlab_merge_requests.json",
        "gitlab_pipelines.json", "gitlab_deployments.json", "gitlab_coverage.json",
        "gitlab_fetch_issues.json",
    }
)
JIRA_ONLY_RAW_JSON_NAMES = frozenset({"jira_issue_facts.json", "jira_sprints.json"})

# Russian check-item names/status labels cli.py prints — mirrors the same
# constants in test_cli.py; kept local here too since these two test modules
# don't import each other (each is a standalone `unittest discover` module).
ITEM_JIRA_ENV = "переменные окружения Jira"
ITEM_GITLAB_ENV = "переменные окружения GitLab"
ITEM_CONFIG_FILE = "файл настроек"
ITEM_JIRA_CONN = "подключение к Jira"
ITEM_JIRA_VERSION = "версия Jira"
ITEM_STORY_POINT = "поле Story Points"
ITEM_SPRINT_BOARD = "поиск спринта/доски"
ITEM_GITLAB_CONN = "подключение к GitLab"
ITEM_GITLAB_PROJECTS = "проекты GitLab"
ITEM_GITLAB_DEPLOYMENTS = "запрос деплоев GitLab"

PASS = "[УСПЕШНО]"
FAIL = "[ОШИБКА]"
SKIP = "[ПРОПУЩЕНО]"

# --------------------------------------------------------------------------
# Environment / config / process helpers
# --------------------------------------------------------------------------


@contextlib.contextmanager
def _tempdir():
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            yield Path(tmp)
        finally:
            os.chdir(cwd)


def _current_schema_fixture_path(tmp: Path) -> Path:
    """A copy of tests/fixtures/report_v2.json (owned by the render track,
    never edited here) with `schema_version` patched to the tool's current
    accepted value — the fixture predates the 3.1.0 fix-release schema bump,
    and this file only needs a report `cli.cmd_report` accepts, not that
    exact fixture's on-disk content."""
    fixture_path = Path(__file__).resolve().parent / "fixtures" / "report_v2.json"
    data = json.loads(fixture_path.read_text(encoding="utf-8"))
    data["schema_version"] = metrics_mod.SCHEMA_VERSION
    out_path = tmp / "current_schema_fixture.json"
    out_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return out_path


def _run_main(argv, environ):
    """Runs the real cli.main() capturing stdout/stderr; returns (code, out, err)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv, environ=environ, invocation="team-metrics")
    return code, out.getvalue(), err.getvalue()


def _good_environ(**overrides):
    env = {
        "JIRA_BASE_URL": "https://jira.example.com",
        "JIRA_TOKEN": wire.JIRA_VALID_TOKEN,
        "GITLAB_URL": "https://gitlab.example.com",
        "GITLAB_TOKEN": wire.GITLAB_VALID_TOKEN,
    }
    env.update(overrides)
    return env


def _write_config(tmp: Path, *, gitlab_projects=None, employees=None, extra=None):
    cfg = {
        "story_points_field_id": wire.STORY_POINTS_FIELD_ID,
        "history_sprint_count": 2,
        "gitlab": {"projects": gitlab_projects if gitlab_projects is not None else [wire.GITLAB_PROJECT_PATH]},
        "employees": employees if employees is not None else ["alice", wire.HOSTILE_USER],
        "cancelled_statuses": ["Cancelled", "Отменено", "Rejected"],
    }
    if extra:
        cfg.update(extra)
    (tmp / config_mod.DEFAULT_CONFIG_FILENAME).write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")


@contextlib.contextmanager
def _patched_opener(opener: FakeOpener):
    with unittest.mock.patch("urllib.request.build_opener", return_value=opener):
        yield opener


@contextlib.contextmanager
def _forbidden_opener():
    """Any code path that calls `urllib.request.build_opener` at all (the
    seam both JiraClient and GitLabClient use to get their transport) fails
    the test immediately — used to prove `report` truly never constructs
    either client, without relying on socket-level blocking."""

    def _raise(*_args, **_kwargs):
        raise AssertionError("network touched: urllib.request.build_opener() was called")

    with unittest.mock.patch("urllib.request.build_opener", side_effect=_raise):
        yield


class _FrozenDateTimeMeta(type):
    """Makes `isinstance(x, _FrozenDateTime)` delegate to `isinstance(x, datetime)`.

    report_data._to_jsonable does `isinstance(obj, datetime)` against
    report_data.py's own module-global `datetime` name, which is exactly the
    name this test patches — so after patching, that check would otherwise
    run as `isinstance(obj, _FrozenDateTime)`. Almost every datetime value in
    a report (sprint dates, issue timestamps, ...) is a genuine
    `datetime.datetime` built by jira_client.py, not a `_FrozenDateTime`
    instance (only the frozen `now` value itself would be) — a plain
    subclass, without this metaclass override, would make that isinstance
    check fail for all of them and break JSON serialization. Same technique
    `freezegun` uses.
    """

    def __instancecheck__(cls, instance):
        return isinstance(instance, datetime)


class _FrozenDateTime(datetime, metaclass=_FrozenDateTimeMeta):
    _frozen = datetime(2026, 1, 10, 12, 0, 0, tzinfo=UTC)

    @classmethod
    def now(cls, tz=None):
        return cls._frozen if tz is None else cls._frozen.astimezone(tz)


@contextlib.contextmanager
def _frozen_now():
    with unittest.mock.patch("team_metrics.report_data.datetime", _FrozenDateTime):
        yield


@contextlib.contextmanager
def _isolated_logging():
    """`logging_setup.setup_logging()` is deliberately idempotent (SPEC): a
    second call only updates the log level, it never attaches a second
    `StreamHandler` — right for a real process, which calls it exactly once.
    This test module calls `cli.main()` many times in ONE Python process,
    so the very first call's handler stays permanently bound to whatever
    `sys.stderr` object was live at that moment; a later
    `contextlib.redirect_stderr(...)` swap does not move an already-attached
    handler's stream. Stripping any existing `StreamHandler` off the
    `"team_metrics"` logger before a logging-focused test forces the next
    `setup_logging()` call (inside `cli.main()`) to attach a fresh handler
    bound to the CURRENT (redirected) stderr, so the test can actually
    observe the output. The `NullHandler` logging_setup.py installs at
    import time is left alone."""
    logger = logging.getLogger("team_metrics")
    saved_handlers = list(logger.handlers)
    saved_level = logger.level
    logger.handlers = [h for h in saved_handlers if isinstance(h, logging.NullHandler)]
    try:
        yield
    finally:
        logger.handlers = saved_handlers
        logger.setLevel(saved_level)


def _assert_schema_v2_shape(testcase: unittest.TestCase, data: dict):
    """SPEC §H.2/§H.3/§H.4: exact top-level key set, every aligned `values`
    array matches `len(sprint_axis)`, and every `"warnings"` list anywhere in
    the document holds only `{code, message_ru, detail}` objects — never a
    bare string."""
    testcase.assertEqual(data["schema_version"], metrics_mod.SCHEMA_VERSION)
    testcase.assertEqual(set(data.keys()), TOP_LEVEL_SCHEMA_V2_KEYS, "schema v2 top-level key set drifted")

    axis_len = len(data["sprint_axis"])
    testcase.assertGreater(axis_len, 0)

    for tile in data["board_kpi"]["tiles"]:
        testcase.assertEqual(len(tile["series"]), axis_len, f"board_kpi tile {tile.get('key')!r} series misaligned")
    for ts in data["team_series"]:
        for series in ts["series"]:
            testcase.assertEqual(len(series["values"]), axis_len, f"team_series {ts.get('key')!r}/{series.get('key')!r} misaligned")
    for ps in data["people_series"]:
        for series in ps["series"]:
            testcase.assertEqual(len(series["values"]), axis_len, f"people_series {ps.get('key')!r} misaligned")
    for person in data["people"]:
        testcase.assertEqual(len(person["by_sprint"]), axis_len, f"people[{person.get('login')!r}].by_sprint misaligned")
    if data["engineering"]["available"]:
        testcase.assertEqual(len(data["engineering"]["by_sprint"]), axis_len, "engineering.by_sprint misaligned")

    def _walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "warnings" and isinstance(value, list):
                    for item in value:
                        testcase.assertIsInstance(item, dict, f"bare warning entry (not an object): {item!r}")
                        testcase.assertEqual(
                            set(item.keys()), {"code", "message_ru", "detail"}, f"warning object has unexpected shape: {item!r}"
                        )
                        testcase.assertTrue(item["message_ru"], "warning object has empty message_ru")
                else:
                    _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(data)


_RUN_ARGV = ["run", "--sprint-ids", str(wire.SPRINT_TARGET_ID), "--seed", "42"]


def _do_run(tmp: Path, opener: FakeOpener, argv=None, environ=None):
    with _patched_opener(opener), _frozen_now():
        _write_config(tmp)
        return _run_main(argv if argv is not None else list(_RUN_ARGV), environ if environ is not None else _good_environ())


# --------------------------------------------------------------------------
# Rendered-HTML sanity: structural properties + hostile-data escaping
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
        pass  # self-closed (`<tag ... />`) — never pushed, nothing to pop

    def handle_endtag(self, tag):
        if tag in _VOID_TAGS:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"</{tag}> does not match top of stack {self.stack[-3:]}")
            return
        self.stack.pop()


def _assert_tags_balanced(testcase: unittest.TestCase, html_text: str):
    parser = _TagBalanceParser()
    parser.feed(html_text)
    parser.close()
    testcase.assertEqual(parser.errors, [], f"HTML tag mismatches: {parser.errors}")
    testcase.assertEqual(parser.stack, [], f"unclosed tags at end of document: {parser.stack}")


def _assert_html_well_formed(testcase: unittest.TestCase, html_text: str):
    testcase.assertGreater(len(html_text), 3000, "rendered report looks too small to be a real report")
    testcase.assertNotIn("{{", html_text, "unresolved template token left in output")
    testcase.assertNotIn("}}", html_text, "unresolved template token left in output")

    lower = html_text.lower()
    testcase.assertNotIn("<script src", lower, "external script reference")
    for m in re.finditer(r'<link\b[^>]*\bhref="([^"]*)"', html_text, re.IGNORECASE):
        testcase.assertFalse(m.group(1).lower().startswith(("http:", "https:", "//")), f"external stylesheet: {m.group(1)!r}")
    testcase.assertNotIn("@import", html_text, "CSS @import (external stylesheet)")
    for m in re.finditer(r'<img\b[^>]*\bsrc="([^"]*)"', html_text, re.IGNORECASE):
        testcase.assertFalse(m.group(1).lower().startswith(("http:", "https:", "//")), f"remote image: {m.group(1)!r}")
    testcase.assertNotIn('fill="var(', html_text, "unresolved CSS var() in an SVG fill attribute")
    testcase.assertNotIn('stroke="var(', html_text, "unresolved CSS var() in an SVG stroke attribute")

    _assert_tags_balanced(testcase, html_text)


def _assert_hostile_fixture_data_escaped(testcase: unittest.TestCase, html_text: str):
    for raw in (wire.HOSTILE_USER, wire.GITLAB_PROJECT_PATH, wire.TARGET_SPRINT_NAME):
        escaped = html_lib.escape(raw, quote=True)
        testcase.assertIn(escaped, html_text, f"expected the escaped form of {raw!r} somewhere in the report")
        testcase.assertNotIn(raw, html_text, f"raw untrusted string {raw!r} leaked into the rendered HTML unescaped")


# --------------------------------------------------------------------------
# 1. init
# --------------------------------------------------------------------------


class InitE2ETests(unittest.TestCase):
    def test_init_refuses_then_force_overwrites_never_writes_a_token(self):
        with _tempdir() as tmp, _forbidden_opener():
            code, out, _err = _run_main(["init"], environ=_good_environ())
            self.assertEqual(code, 0, out)
            cfg_path = tmp / config_mod.DEFAULT_CONFIG_FILENAME
            self.assertTrue(cfg_path.exists())
            written_text = cfg_path.read_text(encoding="utf-8")
            self.assertNotIn(wire.JIRA_VALID_TOKEN, written_text)
            self.assertNotIn(wire.GITLAB_VALID_TOKEN, written_text)
            written = json.loads(written_text)
            self.assertNotIn("token", str(written).lower().replace("story_points_field_id", ""))

            code2, _out2, err2 = _run_main(["init"], environ={})
            self.assertNotEqual(code2, 0)
            self.assertIn("--force", err2)
            self.assertEqual(cfg_path.read_text(encoding="utf-8"), written_text, "refused init must not touch the file")

            cfg_path.write_text('{"marker": "do-not-touch"}', encoding="utf-8")
            code3, out3, _err3 = _run_main(["init", "--force"], environ={})
            self.assertEqual(code3, 0, out3)
            self.assertNotEqual(json.loads(cfg_path.read_text(encoding="utf-8")), {"marker": "do-not-touch"})


# --------------------------------------------------------------------------
# 2. check
# --------------------------------------------------------------------------


class CheckE2ETests(unittest.TestCase):
    def test_all_items_pass(self):
        opener = wire.build_opener()
        with _tempdir() as tmp, _patched_opener(opener):
            _write_config(tmp)
            code, out, err = _run_main(
                ["check", "--sprint-ids", str(wire.SPRINT_TARGET_ID), "--board-id", str(wire.BOARD_ID)],
                environ=_good_environ(),
            )
            self.assertEqual(code, 0, out + err)
            for name in (
                ITEM_JIRA_ENV, ITEM_GITLAB_ENV, ITEM_CONFIG_FILE, ITEM_JIRA_CONN, ITEM_JIRA_VERSION,
                ITEM_STORY_POINT, ITEM_SPRINT_BOARD, ITEM_GITLAB_CONN, ITEM_GITLAB_PROJECTS, ITEM_GITLAB_DEPLOYMENTS,
            ):
                self.assertIn(f"{PASS} {name}", out, out)
            self.assertIn("9.12.28", out)
            self.assertIn("Server", out)
            self.assertNotIn(wire.JIRA_VALID_TOKEN, out + err)
            self.assertNotIn(wire.GITLAB_VALID_TOKEN, out + err)

    def test_bad_jira_token_fails_and_never_leaks_it(self):
        opener = wire.build_opener()
        with _tempdir(), _patched_opener(opener):
            environ = _good_environ(JIRA_TOKEN="totally-wrong-jira-token")
            code, out, err = _run_main(["check"], environ=environ)
            self.assertNotEqual(code, 0)
            self.assertIn(f"{FAIL} {ITEM_JIRA_CONN}", out)
            self.assertNotIn("totally-wrong-jira-token", out + err)
            self.assertNotIn(wire.JIRA_VALID_TOKEN, out + err)

    def test_bad_gitlab_token_fails_and_never_leaks_it(self):
        opener = wire.build_opener()
        with _tempdir(), _patched_opener(opener):
            environ = _good_environ(GITLAB_TOKEN="totally-wrong-gitlab-token")
            code, out, err = _run_main(["check"], environ=environ)
            self.assertNotEqual(code, 0)
            self.assertIn(f"{FAIL} {ITEM_GITLAB_CONN}", out)
            self.assertNotIn("totally-wrong-gitlab-token", out + err)
            self.assertNotIn(wire.GITLAB_VALID_TOKEN, out + err)

    def test_unresolvable_gitlab_project_fails(self):
        opener = wire.build_opener()
        with _tempdir() as tmp, _patched_opener(opener):
            _write_config(tmp, gitlab_projects=["team/this-project-does-not-exist"])
            code, out, err = _run_main(["check"], environ=_good_environ())
            self.assertNotEqual(code, 0)
            self.assertIn(f"{FAIL} {ITEM_GITLAB_PROJECTS}", out)
            self.assertIn("team/this-project-does-not-exist", out)
            self.assertIn("NOT_FOUND", out)
            self.assertNotIn(wire.JIRA_VALID_TOKEN, out + err)
            self.assertNotIn(wire.GITLAB_VALID_TOKEN, out + err)

    def test_missing_env_var_fails_without_touching_the_network(self):
        with _tempdir(), _forbidden_opener():
            code, out, err = _run_main(["check"], environ={})
            self.assertNotEqual(code, 0)
            self.assertIn(f"{FAIL} {ITEM_JIRA_ENV}", out)
            self.assertNotIn(wire.JIRA_VALID_TOKEN, out + err)
            self.assertNotIn(wire.GITLAB_VALID_TOKEN, out + err)


# --------------------------------------------------------------------------
# 3-7. run / report
# --------------------------------------------------------------------------


def _default_json_path(tmp: Path) -> Path:
    return tmp / config_mod.DEFAULT_OUT_DIR / cli.DEFAULT_RUN_JSON_OUT


def _default_html_path(tmp: Path) -> Path:
    return tmp / config_mod.DEFAULT_OUT_DIR / cli.DEFAULT_RUN_HTML_OUT


class RunAndReportE2ETests(unittest.TestCase):
    # -- 3. run writes both files under ./out/, sane schema-v2 JSON shape,
    #    sane + escaped HTML, all nine tabs present -----------------------

    def test_run_writes_json_and_html_with_expected_shape(self):
        opener = wire.build_opener()
        with _tempdir() as tmp:
            code, out, err = _do_run(tmp, opener)
            self.assertEqual(code, 0, out + err)

            json_path = _default_json_path(tmp)
            html_path = _default_html_path(tmp)
            self.assertTrue(json_path.exists(), "run must default report.json under ./out/")
            self.assertTrue(html_path.exists(), "run must default report.html under ./out/")
            # cli.py prints the full resolved path (e.g. "out/report.json"),
            # of which DEFAULT_RUN_JSON_OUT/_HTML_OUT is always a substring.
            self.assertIn(cli.DEFAULT_RUN_JSON_OUT, out)
            self.assertIn(cli.DEFAULT_RUN_HTML_OUT, out)

            data = json.loads(json_path.read_text(encoding="utf-8"))
            _assert_schema_v2_shape(self, data)
            self.assertTrue(data["people_available"])
            self.assertIsNone(data["people_reason_ru"])
            self.assertTrue(data["engineering"]["available"])
            self.assertIsNone(data["engineering"]["reason_ru"])

            # Proves the truncated-changelog re-fetch path actually ran, not
            # just that the search response happened to already carry the
            # right data (SPEC §2.5).
            refetch_calls = [
                (path, q) for path, q in opener.calls
                if path == f"/rest/api/2/issue/{wire.PROJ3_KEY}" and q.get("expand") == "changelog"
            ]
            self.assertEqual(len(refetch_calls), 1, opener.calls)
            # Proves pagination was actually exercised, not just declared.
            search_pages = {q.get("startAt") for path, q in opener.calls if path == "/rest/api/2/search"}
            self.assertEqual(search_pages, {"0", "2"})
            sprint_pages = {q.get("startAt") for path, q in opener.calls if path.endswith("/sprint")}
            self.assertEqual(sprint_pages, {"0", "1"})

            html_text = html_path.read_text(encoding="utf-8")
            _assert_html_well_formed(self, html_text)
            _assert_hostile_fixture_data_escaped(self, html_text)

            # SPEC §H.7 — all nine tabs, exact Russian labels, one radio each.
            for label in (
                "01 Обзор", "02 Спринт", "03 Динамика команды", "04 Прогноз",
                "05 Люди — сравнение", "06 Люди — динамика", "07 Инженерия",
                "08 Данные", "09 Словарь и риски",
            ):
                self.assertIn(label, html_text, f"missing tab label: {label!r}")
            # `name="report-tab"` scopes this to the 9 TAB radios specifically
            # -- the burndown Задачи/SP unit toggle (SPEC §D tab 02) is a
            # separate, per-target-sprint radio group and legitimately adds
            # more `type="radio"` inputs to the document.
            self.assertEqual(html_text.count('name="report-tab"'), 9, "expected exactly 9 tab radio inputs")

    # -- 4. report reproduces identical HTML, zero network, schema guard ----

    def test_report_reproduces_identical_html_with_zero_network_calls(self):
        opener = wire.build_opener()
        with _tempdir() as tmp:
            code, out, err = _do_run(tmp, opener)
            self.assertEqual(code, 0, out + err)
            original_html = _default_html_path(tmp).read_text(encoding="utf-8")

            with _forbidden_opener():
                code2, out2, err2 = _run_main(
                    ["report", str(_default_json_path(tmp)), "-o", str(tmp / "again.html")],
                    environ={},
                )
            self.assertEqual(code2, 0, out2 + err2)
            self.assertEqual((tmp / "again.html").read_text(encoding="utf-8"), original_html)

    def test_report_rejects_missing_schema_version(self):
        opener = wire.build_opener()
        with _tempdir() as tmp:
            code, out, err = _do_run(tmp, opener)
            self.assertEqual(code, 0, out + err)
            data = json.loads(_default_json_path(tmp).read_text(encoding="utf-8"))
            del data["schema_version"]
            bad_path = tmp / "bad_missing.json"
            bad_path.write_text(json.dumps(data), encoding="utf-8")
            out_path = tmp / "bad_missing.html"

            with _forbidden_opener():
                code2, _out2, err2 = _run_main(["report", str(bad_path), "-o", str(out_path)], environ={})
            self.assertNotEqual(code2, 0)
            self.assertIn("schema_version", err2)
            self.assertFalse(out_path.exists())

    def test_report_rejects_bumped_schema_version(self):
        opener = wire.build_opener()
        with _tempdir() as tmp:
            code, out, err = _do_run(tmp, opener)
            self.assertEqual(code, 0, out + err)
            data = json.loads(_default_json_path(tmp).read_text(encoding="utf-8"))
            data["schema_version"] = data["schema_version"] + 999
            bad_path = tmp / "bad_bumped.json"
            bad_path.write_text(json.dumps(data), encoding="utf-8")
            out_path = tmp / "bad_bumped.html"

            with _forbidden_opener():
                code2, _out2, err2 = _run_main(["report", str(bad_path), "-o", str(out_path)], environ={})
            self.assertNotEqual(code2, 0)
            self.assertIn("schema_version", err2)
            self.assertFalse(out_path.exists())

    def test_report_rejects_schema_v1_json_with_russian_message(self):
        """SPEC §H.14 / §C.13, run against an actual pre-3.0.0-shaped v1
        document (not just a bumped-integer variant of a v2 one): exit 1,
        stderr names both required Russian phrases, no traceback, nothing
        written."""
        v1_doc = {
            "schema_version": 1, "board": {"id": 1, "name": "Team Board"},
            "sprints": [], "heatmaps": [], "burndowns": [], "kpi": {}, "forecast": None,
            "forecast_error": None, "warnings": [], "export": {}, "params": {},
            "personal": {"available": False, "reason": ""}, "engineering": {"available": False, "reason": ""},
            "semantics_notes": [], "gitlab_fetch_issues": {},
        }
        with _tempdir() as tmp:
            v1_path = tmp / "old_v1.json"
            v1_path.write_text(json.dumps(v1_doc), encoding="utf-8")
            out_path = tmp / "old_v1.html"
            with _forbidden_opener():
                code, _out, err = _run_main(["report", str(v1_path), "-o", str(out_path)], environ={})
            self.assertEqual(code, 1)
            self.assertIn("не поддерживается", err)
            self.assertIn("пересоздайте его командой", err)
            self.assertNotIn("Traceback", err)
            self.assertFalse(out_path.exists())

    # -- 5. run --no-gitlab -------------------------------------------------

    def test_run_no_gitlab_marks_personal_and_engineering_unavailable(self):
        opener = wire.build_opener()
        argv = list(_RUN_ARGV) + ["--no-gitlab"]
        with _tempdir() as tmp:
            code, out, err = _do_run(tmp, opener, argv=argv)
            self.assertEqual(code, 0, out + err)

            data = json.loads(_default_json_path(tmp).read_text(encoding="utf-8"))
            _assert_schema_v2_shape(self, data)
            self.assertFalse(data["people_available"])
            self.assertTrue(data["people_reason_ru"])
            self.assertFalse(data["engineering"]["available"])
            self.assertTrue(data["engineering"]["reason_ru"])
            self.assertFalse(any(p.startswith("/api/v4") for p in opener.paths_called()), "GitLab endpoint hit despite --no-gitlab")

    # -- 6. GitLab AUTH_FAILED during run fails loudly -----------------------

    def test_gitlab_auth_failed_during_run_fails_loudly_and_writes_nothing(self):
        opener = wire.build_opener()
        with _tempdir() as tmp:
            environ = _good_environ(GITLAB_TOKEN="revoked-gitlab-token")
            code, out, err = _do_run(tmp, opener, environ=environ)
            self.assertNotEqual(code, 0, "a revoked GitLab token must fail the whole run, not degrade silently")
            self.assertIn("ошибка", (out + err).lower())
            self.assertFalse((tmp / config_mod.DEFAULT_OUT_DIR).exists(), "must not write ANY out/ artifact on a genuine GitLab fault")
            self.assertNotIn("revoked-gitlab-token", out + err)
            self.assertNotIn(wire.GITLAB_VALID_TOKEN, out + err)

    # -- 7. determinism -------------------------------------------------------

    def test_same_seed_produces_byte_identical_json_and_html(self):
        with _tempdir() as tmp1:
            code1, out1, err1 = _do_run(tmp1, wire.build_opener())
            self.assertEqual(code1, 0, out1 + err1)
            json1 = _default_json_path(tmp1).read_text(encoding="utf-8")
            html1 = _default_html_path(tmp1).read_text(encoding="utf-8")

        with _tempdir() as tmp2:
            code2, out2, err2 = _do_run(tmp2, wire.build_opener())
            self.assertEqual(code2, 0, out2 + err2)
            json2 = _default_json_path(tmp2).read_text(encoding="utf-8")
            html2 = _default_html_path(tmp2).read_text(encoding="utf-8")

        self.assertEqual(json1, json2)
        self.assertEqual(html1, html2)

    def test_rendering_the_same_json_twice_is_byte_identical(self):
        """SPEC §H.13, second half: `report` itself is a pure function of its
        input JSON — two independent renders of the fixture must match
        byte-for-byte, zero network either time."""
        with _tempdir() as tmp, _forbidden_opener():
            fixture_path = _current_schema_fixture_path(tmp)
            out1 = tmp / "r1.html"
            out2 = tmp / "r2.html"
            code1, _o1, e1 = _run_main(["report", str(fixture_path), "-o", str(out1)], environ={})
            code2, _o2, e2 = _run_main(["report", str(fixture_path), "-o", str(out2)], environ={})
            self.assertEqual(code1, 0, e1)
            self.assertEqual(code2, 0, e2)
            self.assertEqual(out1.read_text(encoding="utf-8"), out2.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# 8. out/ inventory: full CSV+raw set, --out-dir relocation, no token leaks
# --------------------------------------------------------------------------


class OutInventoryE2ETests(unittest.TestCase):
    def test_full_inventory_with_gitlab(self):
        """SPEC §H.12: with GitLab configured and no fetch flags disabled,
        `out/` holds exactly report.json + report.html + the 18 CSVs, and
        `out/raw/` holds exactly the 7 raw JSON dumps — nothing else."""
        opener = wire.build_opener()
        with _tempdir() as tmp:
            code, out, err = _do_run(tmp, opener)
            self.assertEqual(code, 0, out + err)

            out_dir = tmp / config_mod.DEFAULT_OUT_DIR
            top_level = {p.name for p in out_dir.iterdir() if p.is_file()}
            self.assertEqual(top_level, ALL_CSV_NAMES | {"report.json", "report.html"})

            raw_dir = out_dir / "raw"
            self.assertTrue(raw_dir.is_dir())
            raw_files = {p.name for p in raw_dir.iterdir() if p.is_file()}
            self.assertEqual(raw_files, ALL_RAW_JSON_NAMES)

    def test_no_gitlab_drops_gitlab_only_files_but_still_exits_zero(self):
        opener = wire.build_opener()
        argv = list(_RUN_ARGV) + ["--no-gitlab"]
        with _tempdir() as tmp:
            code, out, err = _do_run(tmp, opener, argv=argv)
            self.assertEqual(code, 0, out + err)

            out_dir = tmp / config_mod.DEFAULT_OUT_DIR
            top_level = {p.name for p in out_dir.iterdir() if p.is_file()}
            self.assertEqual(top_level & GITLAB_ONLY_CSV_NAMES, set(), "GitLab-derived CSVs must be absent under --no-gitlab")
            # report_per_employee.csv has no rows either (no `people` at all
            # without GitLab) -- out_writer's own "empty rows never write a
            # headerless file" rule skips it too, same reason, not a GitLab-
            # named exception to carve out.
            self.assertNotIn("report_per_employee.csv", top_level)
            for name in JIRA_ONLY_CSV_NAMES - {"report_per_employee.csv"}:
                self.assertIn(name, top_level, f"expected {name} to still be written under --no-gitlab")

            raw_dir = out_dir / "raw"
            raw_files = {p.name for p in raw_dir.iterdir() if p.is_file()}
            self.assertEqual(raw_files, JIRA_ONLY_RAW_JSON_NAMES, "no GitLab raw dump under --no-gitlab")

    def test_out_dir_flag_relocates_every_artifact(self):
        opener = wire.build_opener()
        argv = list(_RUN_ARGV) + ["--out-dir", "custom_artifacts"]
        with _tempdir() as tmp:
            code, out, err = _do_run(tmp, opener, argv=argv)
            self.assertEqual(code, 0, out + err)

            self.assertFalse((tmp / config_mod.DEFAULT_OUT_DIR).exists(), "default ./out/ must not be touched when --out-dir is given")
            custom_dir = tmp / "custom_artifacts"
            self.assertTrue(custom_dir.is_dir())
            top_level = {p.name for p in custom_dir.iterdir() if p.is_file()}
            self.assertEqual(top_level, ALL_CSV_NAMES | {"report.json", "report.html"})
            raw_files = {p.name for p in (custom_dir / "raw").iterdir() if p.is_file()}
            self.assertEqual(raw_files, ALL_RAW_JSON_NAMES)

            data = json.loads((custom_dir / cli.DEFAULT_RUN_JSON_OUT).read_text(encoding="utf-8"))
            self.assertEqual(data["params"]["out_dir"], "custom_artifacts")

    def test_no_token_value_anywhere_under_out(self):
        """A real leak risk now that `run` dumps raw upstream API responses
        (out/raw/*.json) alongside 18 derived CSVs — walks EVERY file
        actually written under out/ and greps its bytes for both fake
        tokens, not just the JSON/HTML report the older assertions covered."""
        opener = wire.build_opener()
        with _tempdir() as tmp:
            code, out, err = _do_run(tmp, opener)
            self.assertEqual(code, 0, out + err)

            out_dir = tmp / config_mod.DEFAULT_OUT_DIR
            checked = 0
            for path in out_dir.rglob("*"):
                if not path.is_file():
                    continue
                checked += 1
                content = path.read_bytes()
                self.assertNotIn(
                    wire.JIRA_VALID_TOKEN.encode("utf-8"), content, f"Jira token leaked into {path.relative_to(tmp)}"
                )
                self.assertNotIn(
                    wire.GITLAB_VALID_TOKEN.encode("utf-8"), content, f"GitLab token leaked into {path.relative_to(tmp)}"
                )
            self.assertGreaterEqual(checked, 20, "sanity: expected at least 20 files under out/ (18 CSVs + json + html)")


# --------------------------------------------------------------------------
# 9. logging: --verbose adds DEBUG, --quiet silences INFO, stdout stays clean
# --------------------------------------------------------------------------


class LoggingE2ETests(unittest.TestCase):
    _LOG_LINE_RE = re.compile(r"^\d{2}:\d{2}:\d{2} \[(?P<level>[A-Z]+)\] ", re.MULTILINE)

    def _log_levels_seen(self, text: str) -> set:
        return {m.group("level") for m in self._LOG_LINE_RE.finditer(text)}

    def test_verbose_adds_debug_lines_in_expected_format(self):
        opener = wire.build_opener()
        argv = list(_RUN_ARGV) + ["--verbose"]
        with _tempdir() as tmp, _isolated_logging():
            code, out, err = _do_run(tmp, opener, argv=argv)
            self.assertEqual(code, 0, out + err)
            self.assertIn("DEBUG", self._log_levels_seen(err), "expected at least one [DEBUG] line under --verbose")
            self.assertIn("INFO", self._log_levels_seen(err), "--verbose must not drop INFO lines")
            self.assertEqual(self._log_levels_seen(out), set(), "stdout must carry no log lines at all")

    def test_quiet_suppresses_info_but_errors_still_surface(self):
        opener = wire.build_opener()
        argv = list(_RUN_ARGV) + ["--quiet"]
        with _tempdir() as tmp, _isolated_logging():
            code, out, err = _do_run(tmp, opener, argv=argv)
            self.assertEqual(code, 0, out + err)
            self.assertNotIn("INFO", self._log_levels_seen(err), "--quiet must suppress INFO lines")
            self.assertNotIn("DEBUG", self._log_levels_seen(err))
            self.assertEqual(self._log_levels_seen(out), set(), "stdout must carry no log lines at all")

        # A genuine failure (revoked GitLab token) must still be reported
        # loudly even under --quiet: `cmd_run`'s error path is a plain
        # print() to stderr, not gated by the logging level at all.
        with _tempdir() as tmp, _isolated_logging():
            environ = _good_environ(GITLAB_TOKEN="revoked-gitlab-token")
            argv2 = list(_RUN_ARGV) + ["--quiet"]
            code2, out2, err2 = _do_run(tmp, wire.build_opener(), argv=argv2, environ=environ)
            self.assertNotEqual(code2, 0)
            self.assertIn("ошибка", (out2 + err2).lower())

    def test_default_verbosity_is_info_no_debug(self):
        opener = wire.build_opener()
        with _tempdir() as tmp, _isolated_logging():
            code, out, err = _do_run(tmp, opener)
            self.assertEqual(code, 0, out + err)
            self.assertIn("INFO", self._log_levels_seen(err))
            self.assertNotIn("DEBUG", self._log_levels_seen(err), "DEBUG lines must not appear without --verbose")

    def test_report_writes_html_to_stdout_with_no_log_pollution(self):
        """SPEC task item 6: `report` with no `-o` writes the rendered HTML
        straight to stdout; logging (on stderr, if any were emitted) must
        never leak into that stream."""
        with _tempdir() as tmp, _isolated_logging(), _forbidden_opener():
            fixture_path = _current_schema_fixture_path(tmp)
            code, out, err = _run_main(["report", str(fixture_path)], environ={})
        self.assertEqual(code, 0, err)
        self.assertTrue(out.lstrip().startswith("<!DOCTYPE html>"), out[:200])
        self.assertEqual(self._log_levels_seen(out), set(), "stdout must contain no log lines")
        _assert_html_well_formed(self, out)


if __name__ == "__main__":
    unittest.main()
