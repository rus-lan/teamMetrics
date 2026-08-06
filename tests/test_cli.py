"""Coverage for the jira-metrics command dispatcher (init/check/run/report).

No real network calls anywhere in this file: `check`/`run` use dependency-
injected fake Jira/GitLab client classes (cli.cmd_check/cmd_run accept
jira_client_cls/gitlab_client_cls overrides for exactly this reason), and the
ZeroNetworkTests class for `report` additionally blocks socket.socket itself.
"""

import _pathfix  # noqa: F401

import contextlib
import dataclasses
import io
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path

from jira_metrics import cli
from jira_metrics import config as config_mod
from jira_metrics import gitlab_client as glc
from jira_metrics import jira_client as jc
from jira_metrics import metrics as metrics_mod
from jira_metrics import model, report_data

from helpers import dt


def _jira_env():
    return {"JIRA_BASE_URL": "https://jira.example.com", "JIRA_TOKEN": "jira-secret-token"}


def _gitlab_env():
    return {"GITLAB_URL": "https://gitlab.example.com", "GITLAB_TOKEN": "gitlab-secret-token"}


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
    """Runs cli.main() capturing stdout/stderr; returns (exit_code, out, err)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv, environ=environ, invocation="jira-metrics")
    return code, out.getvalue(), err.getvalue()


# --------------------------------------------------------------------------
# init
# --------------------------------------------------------------------------


class InitCommandTests(unittest.TestCase):
    def test_fresh_directory_writes_config_and_next_step(self):
        with _tempdir() as tmp:
            code, out, _err = _run_main(["init"], environ={})
            self.assertEqual(code, 0)
            dest = tmp / config_mod.DEFAULT_CONFIG_FILENAME
            self.assertTrue(dest.exists())
            self.assertIn("wrote .jira-metrics.json", out)
            self.assertIn("jira-metrics check", out)

    def test_written_config_matches_bundled_example(self):
        with _tempdir() as tmp:
            _run_main(["init"], environ={})
            written = json.loads((tmp / config_mod.DEFAULT_CONFIG_FILENAME).read_text(encoding="utf-8"))
            example = json.loads(cli.EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"))
            self.assertEqual(written, example)

    def test_existing_file_without_force_is_refused(self):
        with _tempdir() as tmp:
            dest = tmp / config_mod.DEFAULT_CONFIG_FILENAME
            dest.write_text('{"marker": "do-not-touch"}', encoding="utf-8")
            code, _out, err = _run_main(["init"], environ={})
            self.assertEqual(code, 1)
            self.assertIn("--force", err)
            self.assertEqual(json.loads(dest.read_text(encoding="utf-8")), {"marker": "do-not-touch"})

    def test_existing_file_with_force_is_overwritten(self):
        with _tempdir() as tmp:
            dest = tmp / config_mod.DEFAULT_CONFIG_FILENAME
            dest.write_text('{"marker": "do-not-touch"}', encoding="utf-8")
            code, out, _err = _run_main(["init", "--force"], environ={})
            self.assertEqual(code, 0)
            self.assertIn("wrote", out)
            written = json.loads(dest.read_text(encoding="utf-8"))
            self.assertNotEqual(written, {"marker": "do-not-touch"})

    def test_never_writes_a_token(self):
        with _tempdir() as tmp:
            environ = {**_jira_env(), **_gitlab_env()}
            _run_main(["init"], environ=environ)
            written_text = (tmp / config_mod.DEFAULT_CONFIG_FILENAME).read_text(encoding="utf-8")
            self.assertNotIn("jira-secret-token", written_text)
            self.assertNotIn("gitlab-secret-token", written_text)
            written = json.loads(written_text)
            self.assertNotIn("token", str(written).lower().replace("story_points_field_id", ""))

    def test_reports_missing_required_env_vars(self):
        with _tempdir():
            code, out, _err = _run_main(["init"], environ={})
            self.assertEqual(code, 0)
            self.assertIn("JIRA_BASE_URL", out)
            self.assertIn("JIRA_TOKEN", out)

    def test_all_env_vars_already_set_says_so(self):
        with _tempdir():
            environ = {**_jira_env(), **_gitlab_env()}
            code, out, _err = _run_main(["init"], environ=environ)
            self.assertEqual(code, 0)
            self.assertIn("already set", out)
            self.assertNotIn("still export", out)

    def test_half_configured_gitlab_env_warns(self):
        with _tempdir():
            environ = {**_jira_env(), "GITLAB_URL": "https://gitlab.example.com"}
            code, out, _err = _run_main(["init"], environ=environ)
            self.assertEqual(code, 0)
            self.assertIn("only one of GITLAB_URL/GITLAB_TOKEN", out)


# --------------------------------------------------------------------------
# check
# --------------------------------------------------------------------------


class _OKJiraClient:
    def __init__(self, field_ids=None, sprints=None, board=None, suggestions=None):
        self._field_ids = field_ids if field_ids is not None else {"Story Points": "customfield_10016"}
        self._sprints = sprints or {}
        self._board = board
        self._suggestions = suggestions or {}

    def field_ids(self):
        return dict(self._field_ids)

    def sprint(self, sprint_id):
        if sprint_id not in self._sprints:
            raise jc.JiraError("Sprint", "not found", code="", status_code=404)
        return self._sprints[sprint_id]

    def board(self, board_id):
        return self._board

    def suggest_sprints(self, query):
        return self._suggestions.get(query, [])


class _FailingJiraClient:
    def field_ids(self):
        raise jc.JiraError("FieldIDs", "connection refused", code=jc.CODE_JIRA_UNREACHABLE)


class _OKGitLabClient:
    def __init__(self, username="alice", project_ids=None):
        self._username = username
        self._project_ids = project_ids or {}

    def current_user(self):
        return {"username": self._username}

    def project_id(self, path):
        return self._project_ids.get(path)


class _FailingGitLabClient:
    def current_user(self):
        raise glc.GitLabError("CurrentUser", "unauthorized", code="AUTH_FAILED", status_code=401)


class _ProjectFailingGitLabClient(_OKGitLabClient):
    """current_user() succeeds; project_id() raises for any path not in
    `project_ids` (NOT_FOUND-style) so FAIL formatting can be exercised."""

    def project_id(self, path):
        if path in self._project_ids:
            return self._project_ids[path]
        raise glc.GitLabError("ProjectID", "404 Project Not Found", code="NOT_FOUND", status_code=404)


class CheckCommandTests(unittest.TestCase):
    def _check(self, extra_argv, environ, jira_cls=_OKJiraClient, gitlab_cls=_OKGitLabClient):
        out, err = io.StringIO(), io.StringIO()
        args = cli.build_parser().parse_args(["check"] + extra_argv)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.cmd_check(
                args, environ, jira_client_cls=lambda *_a, **_kw: jira_cls(), gitlab_client_cls=lambda *_a, **_kw: gitlab_cls()
            )
        return code, out.getvalue(), err.getvalue()

    def test_all_pass_exits_zero(self):
        code, out, _err = self._check([], {**_jira_env(), **_gitlab_env()})
        self.assertEqual(code, 0)
        self.assertIn("[PASS] jira env vars", out)
        self.assertIn("[PASS] jira connectivity", out)
        self.assertIn("[PASS] story point field", out)
        self.assertIn("[PASS] gitlab connectivity", out)

    def test_missing_jira_env_vars_fails(self):
        code, out, _err = self._check([], {})
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] jira env vars", out)

    def test_no_gitlab_flag_skips_gitlab_checks(self):
        code, out, _err = self._check(["--no-gitlab"], _jira_env())
        self.assertEqual(code, 0)
        self.assertIn("[SKIP] gitlab env vars", out)
        self.assertIn("[SKIP] gitlab connectivity", out)

    def test_gitlab_not_configured_is_skip_not_fail(self):
        code, out, _err = self._check([], _jira_env())
        self.assertEqual(code, 0)
        self.assertIn("[SKIP] gitlab env vars", out)
        self.assertIn("[SKIP] gitlab connectivity", out)

    def test_half_configured_gitlab_env_fails(self):
        environ = {**_jira_env(), "GITLAB_URL": "https://gitlab.example.com"}
        code, out, _err = self._check([], environ)
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] gitlab env vars", out)

    def test_jira_unreachable_fails(self):
        code, out, _err = self._check([], _jira_env(), jira_cls=_FailingJiraClient)
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] jira connectivity", out)
        self.assertIn("[SKIP] story point field", out)

    def test_gitlab_auth_failed_fails(self):
        code, out, _err = self._check([], {**_jira_env(), **_gitlab_env()}, gitlab_cls=_FailingGitLabClient)
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] gitlab connectivity", out)

    def test_story_point_field_not_found_fails(self):
        code, out, _err = self._check(
            [], _jira_env(), jira_cls=lambda: _OKJiraClient(field_ids={"Summary": "summary"})
        )
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] story point field", out)

    def test_configured_story_point_override_not_found_fails(self):
        with _tempdir() as tmp:
            (tmp / config_mod.DEFAULT_CONFIG_FILENAME).write_text(
                json.dumps({"story_points_field_id": "customfield_99999"}), encoding="utf-8"
            )
            code, out, _err = self._check([], _jira_env(), jira_cls=lambda: _OKJiraClient(field_ids={"Summary": "summary"}))
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] story point field", out)

    def test_sprint_id_resolves(self):
        sprint = jc.Sprint(id=100, name="Sprint 100", state="closed", board_id=1,
                            start_at=dt(2026, 1, 1), end_at=dt(2026, 1, 5), complete_at=dt(2026, 1, 5))
        code, out, _err = self._check(
            ["--sprint-ids", "100"], _jira_env(), jira_cls=lambda: _OKJiraClient(sprints={100: sprint})
        )
        self.assertEqual(code, 0)
        self.assertIn("[PASS] sprint/board resolution", out)

    def test_sprint_id_not_found_fails(self):
        code, out, _err = self._check(["--sprint-ids", "999"], _jira_env(), jira_cls=lambda: _OKJiraClient())
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] sprint/board resolution", out)

    def test_board_id_mismatch_fails(self):
        sprint = jc.Sprint(id=100, name="Sprint 100", state="closed", board_id=1,
                            start_at=dt(2026, 1, 1), end_at=dt(2026, 1, 5), complete_at=dt(2026, 1, 5))
        code, out, _err = self._check(
            ["--sprint-ids", "100", "--board-id", "999"], _jira_env(), jira_cls=lambda: _OKJiraClient(sprints={100: sprint})
        )
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] sprint/board resolution", out)

    def test_no_sprint_ref_given_is_skip(self):
        code, out, _err = self._check([], _jira_env())
        self.assertEqual(code, 0)
        self.assertIn("[SKIP] sprint/board resolution", out)

    def test_output_never_contains_token(self):
        environ = {**_jira_env(), **_gitlab_env()}
        code, out, err = self._check([], environ)
        self.assertNotIn(environ["JIRA_TOKEN"], out)
        self.assertNotIn(environ["JIRA_TOKEN"], err)
        self.assertNotIn(environ["GITLAB_TOKEN"], out)
        self.assertNotIn(environ["GITLAB_TOKEN"], err)

    def test_no_gitlab_projects_configured_is_skip(self):
        code, out, _err = self._check([], {**_jira_env(), **_gitlab_env()})
        self.assertEqual(code, 0)
        self.assertIn("[SKIP] gitlab projects", out)

    def test_configured_gitlab_projects_all_resolve(self):
        with _tempdir() as tmp:
            (tmp / config_mod.DEFAULT_CONFIG_FILENAME).write_text(
                json.dumps({"gitlab": {"projects": ["team/a", "team/b"]}}), encoding="utf-8"
            )
            code, out, _err = self._check(
                [], {**_jira_env(), **_gitlab_env()},
                gitlab_cls=lambda: _OKGitLabClient(project_ids={"team/a": 1, "team/b": 2}),
            )
        self.assertEqual(code, 0)
        self.assertIn("[PASS] gitlab projects", out)
        self.assertIn("2/2 resolve", out)

    def test_unresolvable_gitlab_project_fails_with_readable_detail_not_a_stringified_dict(self):
        with _tempdir() as tmp:
            (tmp / config_mod.DEFAULT_CONFIG_FILENAME).write_text(
                json.dumps({"gitlab": {"projects": ["team/a", "team/renamed"]}}), encoding="utf-8"
            )
            code, out, _err = self._check(
                [], {**_jira_env(), **_gitlab_env()},
                gitlab_cls=lambda: _ProjectFailingGitLabClient(project_ids={"team/a": 1}),
            )
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] gitlab projects", out)
        # readable fields, not Python's dict repr ("{'project': ...}")
        self.assertIn("team/renamed", out)
        self.assertIn("NOT_FOUND", out)
        self.assertNotIn("{'project'", out)

    def test_gitlab_projects_skipped_when_connectivity_check_failed(self):
        with _tempdir() as tmp:
            (tmp / config_mod.DEFAULT_CONFIG_FILENAME).write_text(
                json.dumps({"gitlab": {"projects": ["team/a"]}}), encoding="utf-8"
            )
            code, out, _err = self._check([], {**_jira_env(), **_gitlab_env()}, gitlab_cls=_FailingGitLabClient)
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] gitlab connectivity", out)
        self.assertIn("[SKIP] gitlab projects", out)


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------


class _FakeJiraClient:
    """Minimal single-sprint fixture, enough for build_combined_report()."""

    def __init__(self):
        self._sprint = jc.Sprint(
            id=100, name="Sprint 100", state="closed", board_id=1,
            start_at=dt(2026, 1, 19), end_at=dt(2026, 1, 23, 18), complete_at=dt(2026, 1, 23, 18),
        )
        self._board = jc.Board(id=1, name="Team Board", type="scrum")
        self._facts = [
            jc.IssueFacts(
                key="T1", epic_key="", type="Story", role="", labels=[], assignee="",
                story_points=5.0, qa_estimation=0.0, created=dt(2025, 12, 1),
                initial_status="In Progress", initial_status_id="2",
                status_history=[jc.RawStatusChange(at=dt(2026, 1, 21, 10), from_name="In Progress", to_name="Done", from_id="2", to_id="3")],
                sp_events=[], current_status="Done", current_status_category_key="done",
                membership_by_sprint={100: [model.Interval(from_=dt(2025, 12, 1), until=None)]},
            )
        ]
        self._statuses = [
            jc.Status(id="1", name="To Do", category_key="new"),
            jc.Status(id="2", name="In Progress", category_key="indeterminate"),
            jc.Status(id="3", name="Done", category_key="done"),
        ]

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
                out.append(dataclasses.replace(f, membership_by_sprint=filtered))
        return out

    def list_statuses(self):
        return self._statuses

    def suggest_sprints(self, query):
        return []


class RunCommandTests(unittest.TestCase):
    def test_writes_both_json_and_html(self):
        with _tempdir() as tmp:
            args = cli.build_parser().parse_args(["run", "--sprint-ids", "100", "--no-gitlab"])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.cmd_run(args, _jira_env(), jira_client_cls=lambda *_a, **_kw: _FakeJiraClient())
            self.assertEqual(code, 0)

            json_path = tmp / cli.DEFAULT_RUN_JSON_OUT
            html_path = tmp / cli.DEFAULT_RUN_HTML_OUT
            self.assertTrue(json_path.exists())
            self.assertTrue(html_path.exists())

            data = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(data["schema_version"], metrics_mod.SCHEMA_VERSION)
            self.assertIn("<html", html_path.read_text(encoding="utf-8").lower())
            self.assertIn(cli.DEFAULT_RUN_JSON_OUT, out.getvalue())
            self.assertIn(cli.DEFAULT_RUN_HTML_OUT, out.getvalue())

    def test_custom_out_paths_honored(self):
        with _tempdir() as tmp:
            args = cli.build_parser().parse_args(
                ["run", "--sprint-ids", "100", "--no-gitlab", "--out", "custom.html", "--json-out", "custom.json"]
            )
            with contextlib.redirect_stdout(io.StringIO()):
                code = cli.cmd_run(args, _jira_env(), jira_client_cls=lambda *_a, **_kw: _FakeJiraClient())
            self.assertEqual(code, 0)
            self.assertTrue((tmp / "custom.html").exists())
            self.assertTrue((tmp / "custom.json").exists())

    def test_missing_env_vars_reports_config_error(self):
        with _tempdir():
            args = cli.build_parser().parse_args(["run", "--sprint-ids", "100", "--no-gitlab"])
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = cli.cmd_run(args, {}, jira_client_cls=lambda *_a, **_kw: _FakeJiraClient())
            self.assertEqual(code, 2)
            self.assertIn("config error", err.getvalue())


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------


def _build_fixture_report_dict():
    client = _FakeJiraClient()
    return report_data.build_report(client, sprint_ids=[100], history_sprint_count=5, target_items=7, now=dt(2026, 1, 24))


class ReportCommandTests(unittest.TestCase):
    def test_renders_html_from_json_file(self):
        with _tempdir() as tmp:
            report = _build_fixture_report_dict()
            src = tmp / "data.json"
            src.write_text(json.dumps(report), encoding="utf-8")

            args = cli.build_parser().parse_args(["report", str(src), "-o", "out.html"])
            with contextlib.redirect_stdout(io.StringIO()):
                code = cli.cmd_report(args)
            self.assertEqual(code, 0)
            html = (tmp / "out.html").read_text(encoding="utf-8")
            self.assertIn("<html", html.lower())

    def test_rejects_missing_schema_version(self):
        with _tempdir() as tmp:
            report = _build_fixture_report_dict()
            del report["schema_version"]
            src = tmp / "data.json"
            src.write_text(json.dumps(report), encoding="utf-8")

            args = cli.build_parser().parse_args(["report", str(src), "-o", "out.html"])
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = cli.cmd_report(args)
            self.assertEqual(code, 1)
            self.assertIn("schema_version", err.getvalue())
            self.assertFalse((tmp / "out.html").exists())

    def test_rejects_incompatible_schema_version(self):
        with _tempdir() as tmp:
            report = _build_fixture_report_dict()
            report["schema_version"] = 999
            src = tmp / "data.json"
            src.write_text(json.dumps(report), encoding="utf-8")

            args = cli.build_parser().parse_args(["report", str(src)])
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                code = cli.cmd_report(args)
            self.assertEqual(code, 1)
            self.assertIn("999", err.getvalue())


class ZeroNetworkTests(unittest.TestCase):
    """`report` must never open a socket — proves it independently of
    ReportCommandTests's DI-free call path by blocking socket.socket itself."""

    def test_report_never_touches_the_network(self):
        report = _build_fixture_report_dict()
        with _tempdir() as tmp:
            src = tmp / "data.json"
            src.write_text(json.dumps(report), encoding="utf-8")

            args = cli.build_parser().parse_args(["report", str(src), "-o", "out.html"])

            original_socket = socket.socket

            def _blocked(*_a, **_kw):
                raise AssertionError("report command attempted to open a network socket")

            socket.socket = _blocked
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    code = cli.cmd_report(args)
            finally:
                socket.socket = original_socket

            self.assertEqual(code, 0)
            self.assertTrue((tmp / "out.html").exists())

    def test_report_via_main_with_no_env_vars_set(self):
        """Also exercised through main() end-to-end, with an empty environ —
        no JIRA_*/GITLAB_* var is read on this path at all."""
        report = _build_fixture_report_dict()
        with _tempdir() as tmp:
            src = tmp / "data.json"
            src.write_text(json.dumps(report), encoding="utf-8")
            code, out, _err = _run_main(["report", str(src), "-o", "out.html"], environ={})
            self.assertEqual(code, 0)
            self.assertTrue((tmp / "out.html").exists())


if __name__ == "__main__":
    unittest.main()
