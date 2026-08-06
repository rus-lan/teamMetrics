"""Full end-to-end coverage of the `jira-metrics` CLI: init -> check -> run ->
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
`scripts/jira-metrics ...` — there is no reachable seam on either client
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
`jira_metrics.report_data.datetime` with a `datetime` subclass whose `.now()`
returns a fixed instant — a standard freezegun-style technique, applied only
inside this test module.
"""

import _pathfix  # noqa: F401

import contextlib
import html as html_lib
import io
import json
import os
import re
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

from jira_metrics import cli
from jira_metrics import config as config_mod
from jira_metrics import metrics as metrics_mod

from fixtures import wire
from fixtures.http_fake import FakeOpener

UTC = timezone.utc

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


def _run_main(argv, environ):
    """Runs the real cli.main() capturing stdout/stderr; returns (code, out, err)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv, environ=environ, invocation="jira-metrics")
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
    with unittest.mock.patch("jira_metrics.report_data.datetime", _FrozenDateTime):
        yield


_RUN_ARGV = ["run", "--sprint-ids", str(wire.SPRINT_TARGET_ID), "--target-items", "5", "--seed", "42"]


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
                "jira env vars", "gitlab env vars", "config file", "jira connectivity",
                "story point field", "sprint/board resolution", "gitlab connectivity", "gitlab projects",
            ):
                self.assertIn(f"[PASS] {name}", out, out)
            self.assertNotIn(wire.JIRA_VALID_TOKEN, out + err)
            self.assertNotIn(wire.GITLAB_VALID_TOKEN, out + err)

    def test_bad_jira_token_fails_and_never_leaks_it(self):
        opener = wire.build_opener()
        with _tempdir(), _patched_opener(opener):
            environ = _good_environ(JIRA_TOKEN="totally-wrong-jira-token")
            code, out, err = _run_main(["check"], environ=environ)
            self.assertNotEqual(code, 0)
            self.assertIn("[FAIL] jira connectivity", out)
            self.assertNotIn("totally-wrong-jira-token", out + err)
            self.assertNotIn(wire.JIRA_VALID_TOKEN, out + err)

    def test_bad_gitlab_token_fails_and_never_leaks_it(self):
        opener = wire.build_opener()
        with _tempdir(), _patched_opener(opener):
            environ = _good_environ(GITLAB_TOKEN="totally-wrong-gitlab-token")
            code, out, err = _run_main(["check"], environ=environ)
            self.assertNotEqual(code, 0)
            self.assertIn("[FAIL] gitlab connectivity", out)
            self.assertNotIn("totally-wrong-gitlab-token", out + err)
            self.assertNotIn(wire.GITLAB_VALID_TOKEN, out + err)

    def test_unresolvable_gitlab_project_fails(self):
        opener = wire.build_opener()
        with _tempdir() as tmp, _patched_opener(opener):
            _write_config(tmp, gitlab_projects=["team/this-project-does-not-exist"])
            code, out, err = _run_main(["check"], environ=_good_environ())
            self.assertNotEqual(code, 0)
            self.assertIn("[FAIL] gitlab projects", out)
            self.assertIn("team/this-project-does-not-exist", out)
            self.assertIn("NOT_FOUND", out)
            self.assertNotIn(wire.JIRA_VALID_TOKEN, out + err)
            self.assertNotIn(wire.GITLAB_VALID_TOKEN, out + err)

    def test_missing_env_var_fails_without_touching_the_network(self):
        with _tempdir(), _forbidden_opener():
            code, out, err = _run_main(["check"], environ={})
            self.assertNotEqual(code, 0)
            self.assertIn("[FAIL] jira env vars", out)
            self.assertNotIn(wire.JIRA_VALID_TOKEN, out + err)
            self.assertNotIn(wire.GITLAB_VALID_TOKEN, out + err)


# --------------------------------------------------------------------------
# 3-7. run / report
# --------------------------------------------------------------------------


class RunAndReportE2ETests(unittest.TestCase):
    # -- 3. run writes both files, sane JSON shape, sane + escaped HTML ----

    def test_run_writes_json_and_html_with_expected_shape(self):
        opener = wire.build_opener()
        with _tempdir() as tmp:
            code, out, err = _do_run(tmp, opener)
            self.assertEqual(code, 0, out + err)

            json_path = tmp / cli.DEFAULT_RUN_JSON_OUT
            html_path = tmp / cli.DEFAULT_RUN_HTML_OUT
            self.assertTrue(json_path.exists())
            self.assertTrue(html_path.exists())
            self.assertIn(cli.DEFAULT_RUN_JSON_OUT, out)
            self.assertIn(cli.DEFAULT_RUN_HTML_OUT, out)

            data = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(data["schema_version"], metrics_mod.SCHEMA_VERSION)
            for key in (
                "schema_version", "board", "sprints", "heatmaps", "burndowns", "kpi",
                "forecast", "forecast_error", "warnings", "export", "params",
                "personal", "engineering", "semantics_notes", "gitlab_fetch_issues",
            ):
                self.assertIn(key, data, f"missing top-level report key: {key}")
            self.assertTrue(data["personal"]["available"])
            self.assertTrue(data["engineering"]["available"])

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

    # -- 4. report reproduces identical HTML, zero network, schema guard ----

    def test_report_reproduces_identical_html_with_zero_network_calls(self):
        opener = wire.build_opener()
        with _tempdir() as tmp:
            code, out, err = _do_run(tmp, opener)
            self.assertEqual(code, 0, out + err)
            original_html = (tmp / cli.DEFAULT_RUN_HTML_OUT).read_text(encoding="utf-8")

            with _forbidden_opener():
                code2, out2, err2 = _run_main(
                    ["report", str(tmp / cli.DEFAULT_RUN_JSON_OUT), "-o", str(tmp / "again.html")],
                    environ={},
                )
            self.assertEqual(code2, 0, out2 + err2)
            self.assertEqual((tmp / "again.html").read_text(encoding="utf-8"), original_html)

    def test_report_rejects_missing_schema_version(self):
        opener = wire.build_opener()
        with _tempdir() as tmp:
            code, out, err = _do_run(tmp, opener)
            self.assertEqual(code, 0, out + err)
            data = json.loads((tmp / cli.DEFAULT_RUN_JSON_OUT).read_text(encoding="utf-8"))
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
            data = json.loads((tmp / cli.DEFAULT_RUN_JSON_OUT).read_text(encoding="utf-8"))
            data["schema_version"] = data["schema_version"] + 999
            bad_path = tmp / "bad_bumped.json"
            bad_path.write_text(json.dumps(data), encoding="utf-8")
            out_path = tmp / "bad_bumped.html"

            with _forbidden_opener():
                code2, _out2, err2 = _run_main(["report", str(bad_path), "-o", str(out_path)], environ={})
            self.assertNotEqual(code2, 0)
            self.assertIn("schema_version", err2)
            self.assertFalse(out_path.exists())

    # -- 5. run --no-gitlab -------------------------------------------------

    def test_run_no_gitlab_marks_personal_and_engineering_unavailable(self):
        opener = wire.build_opener()
        argv = list(_RUN_ARGV) + ["--no-gitlab"]
        with _tempdir() as tmp:
            code, out, err = _do_run(tmp, opener, argv=argv)
            self.assertEqual(code, 0, out + err)

            data = json.loads((tmp / cli.DEFAULT_RUN_JSON_OUT).read_text(encoding="utf-8"))
            self.assertFalse(data["personal"]["available"])
            self.assertTrue(data["personal"]["reason"])
            self.assertFalse(data["engineering"]["available"])
            self.assertTrue(data["engineering"]["reason"])
            self.assertFalse(any(p.startswith("/api/v4") for p in opener.paths_called()), "GitLab endpoint hit despite --no-gitlab")

    # -- 6. GitLab AUTH_FAILED during run fails loudly -----------------------

    def test_gitlab_auth_failed_during_run_fails_loudly_and_writes_nothing(self):
        opener = wire.build_opener()
        with _tempdir() as tmp:
            environ = _good_environ(GITLAB_TOKEN="revoked-gitlab-token")
            code, out, err = _do_run(tmp, opener, environ=environ)
            self.assertNotEqual(code, 0, "a revoked GitLab token must fail the whole run, not degrade silently")
            self.assertIn("error", (out + err).lower())
            self.assertFalse((tmp / cli.DEFAULT_RUN_JSON_OUT).exists(), "must not write a report on a genuine GitLab fault")
            self.assertFalse((tmp / cli.DEFAULT_RUN_HTML_OUT).exists())
            self.assertNotIn("revoked-gitlab-token", out + err)
            self.assertNotIn(wire.GITLAB_VALID_TOKEN, out + err)

    # -- 7. determinism -------------------------------------------------------

    def test_same_seed_produces_byte_identical_json_and_html(self):
        with _tempdir() as tmp1:
            code1, out1, err1 = _do_run(tmp1, wire.build_opener())
            self.assertEqual(code1, 0, out1 + err1)
            json1 = (tmp1 / cli.DEFAULT_RUN_JSON_OUT).read_text(encoding="utf-8")
            html1 = (tmp1 / cli.DEFAULT_RUN_HTML_OUT).read_text(encoding="utf-8")

        with _tempdir() as tmp2:
            code2, out2, err2 = _do_run(tmp2, wire.build_opener())
            self.assertEqual(code2, 0, out2 + err2)
            json2 = (tmp2 / cli.DEFAULT_RUN_JSON_OUT).read_text(encoding="utf-8")
            html2 = (tmp2 / cli.DEFAULT_RUN_HTML_OUT).read_text(encoding="utf-8")

        self.assertEqual(json1, json2)
        self.assertEqual(html1, html2)


if __name__ == "__main__":
    unittest.main()
