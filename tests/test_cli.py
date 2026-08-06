"""Coverage for the team-metrics command dispatcher (init/check/run/report).

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
import unittest.mock
from pathlib import Path

from team_metrics import cli
from team_metrics import config as config_mod
from team_metrics import gitlab_client as glc
from team_metrics import jira_client as jc
from team_metrics import metrics as metrics_mod
from team_metrics import model, report_data
from team_metrics import render_html as render_html_mod

from helpers import dt

# Russian check-item names/status labels cli.py prints — kept as constants so
# a wording change in cli.py breaks exactly one place per test file.
ITEM_JIRA_ENV = "переменные окружения Jira"
ITEM_GITLAB_ENV = "переменные окружения GitLab"
ITEM_CONFIG_FILE = "файл настроек"
ITEM_JIRA_CONN = "подключение к Jira"
ITEM_JIRA_VERSION = "версия Jira"
ITEM_STORY_POINT = "поле Story Points"
ITEM_SPRINT_BOARD = "поиск спринта/доски"
ITEM_GITLAB_CONN = "подключение к GitLab"
ITEM_GITLAB_PROJECTS = "проекты GitLab"

PASS = "[УСПЕШНО]"
FAIL = "[ОШИБКА]"
SKIP = "[ПРОПУЩЕНО]"
WARN = "[ПРЕДУПРЕЖДЕНИЕ]"


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
        code = cli.main(argv, environ=environ, invocation="team-metrics")
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
            self.assertIn("файл .team-metrics.json создан", out)
            self.assertIn("team-metrics check", out)

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
            self.assertIn("создан", out)
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
            self.assertIn("уже заданы", out)
            self.assertNotIn("нужно ещё задать", out)

    def test_half_configured_gitlab_env_warns(self):
        with _tempdir():
            environ = {**_jira_env(), "GITLAB_URL": "https://gitlab.example.com"}
            code, out, _err = _run_main(["init"], environ=environ)
            self.assertEqual(code, 0)
            self.assertIn("только один из GITLAB_URL/GITLAB_TOKEN", out)

    def test_old_config_filename_present_prints_migration_hint(self):
        with _tempdir() as tmp:
            (tmp / config_mod.OLD_CONFIG_FILENAME).write_text("{}", encoding="utf-8")
            code, out, _err = _run_main(["init"], environ={})
            self.assertEqual(code, 0)
            self.assertIn(config_mod.OLD_CONFIG_FILENAME, out)
            self.assertIn("team-metrics", out)
            # init still writes the new file from the bundled example --
            # the old file is never read, only pointed at.
            self.assertTrue((tmp / config_mod.DEFAULT_CONFIG_FILENAME).exists())
            self.assertEqual((tmp / config_mod.OLD_CONFIG_FILENAME).read_text(encoding="utf-8"), "{}")

    def test_old_config_filename_ignored_once_new_one_exists(self):
        with _tempdir() as tmp:
            (tmp / config_mod.OLD_CONFIG_FILENAME).write_text("{}", encoding="utf-8")
            (tmp / config_mod.DEFAULT_CONFIG_FILENAME).write_text('{"marker": "already-migrated"}', encoding="utf-8")
            code, out, _err = _run_main(["init"], environ={})
            self.assertEqual(code, 1)  # refused without --force, same as any pre-existing dest
            self.assertNotIn(config_mod.OLD_CONFIG_FILENAME, out)


# --------------------------------------------------------------------------
# check
# --------------------------------------------------------------------------


_DEFAULT_SERVER_INFO = jc.ServerInfo(
    version="9.12.28", version_numbers=[9, 12, 28], deployment_type="Server", build_number=912000, server_title="Jira"
)


class _OKJiraClient:
    def __init__(self, field_ids=None, sprints=None, board=None, suggestions=None, server_info=None):
        self._field_ids = field_ids if field_ids is not None else {"Story Points": "customfield_10016"}
        self._sprints = sprints or {}
        self._board = board
        self._suggestions = suggestions or {}
        self._server_info = server_info if server_info is not None else _DEFAULT_SERVER_INFO

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

    def server_info(self):
        return self._server_info


class _FailingJiraClient:
    def field_ids(self):
        raise jc.JiraError("FieldIDs", "connection refused", code=jc.CODE_JIRA_UNREACHABLE)

    def server_info(self):
        raise jc.JiraError("ServerInfo", "connection refused", code=jc.CODE_JIRA_UNREACHABLE)


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
        self.assertIn(f"{PASS} {ITEM_JIRA_ENV}", out)
        self.assertIn(f"{PASS} {ITEM_JIRA_CONN}", out)
        self.assertIn(f"{PASS} {ITEM_STORY_POINT}", out)
        self.assertIn(f"{PASS} {ITEM_GITLAB_CONN}", out)

    def test_missing_jira_env_vars_fails(self):
        code, out, _err = self._check([], {})
        self.assertEqual(code, 1)
        self.assertIn(f"{FAIL} {ITEM_JIRA_ENV}", out)

    def test_no_gitlab_flag_skips_gitlab_checks(self):
        code, out, _err = self._check(["--no-gitlab"], _jira_env())
        self.assertEqual(code, 0)
        self.assertIn(f"{SKIP} {ITEM_GITLAB_ENV}", out)
        self.assertIn(f"{SKIP} {ITEM_GITLAB_CONN}", out)

    def test_gitlab_not_configured_is_skip_not_fail(self):
        code, out, _err = self._check([], _jira_env())
        self.assertEqual(code, 0)
        self.assertIn(f"{SKIP} {ITEM_GITLAB_ENV}", out)
        self.assertIn(f"{SKIP} {ITEM_GITLAB_CONN}", out)

    def test_half_configured_gitlab_env_fails(self):
        environ = {**_jira_env(), "GITLAB_URL": "https://gitlab.example.com"}
        code, out, _err = self._check([], environ)
        self.assertEqual(code, 1)
        self.assertIn(f"{FAIL} {ITEM_GITLAB_ENV}", out)

    def test_jira_unreachable_fails(self):
        code, out, _err = self._check([], _jira_env(), jira_cls=_FailingJiraClient)
        self.assertEqual(code, 1)
        self.assertIn(f"{FAIL} {ITEM_JIRA_CONN}", out)
        self.assertIn(f"{SKIP} {ITEM_STORY_POINT}", out)

    # -- Jira server version item ------------------------------------------

    def test_well_formed_server_version_passes(self):
        code, out, _err = self._check([], _jira_env())
        self.assertEqual(code, 0)
        self.assertIn(f"{PASS} {ITEM_JIRA_VERSION}", out)
        self.assertIn("9.12.28", out)
        self.assertIn("Server", out)

    def test_cloud_deployment_warns(self):
        info = jc.ServerInfo(version="1001.0.0", version_numbers=[1001, 0, 0], deployment_type="Cloud")
        code, out, _err = self._check([], _jira_env(), jira_cls=lambda: _OKJiraClient(server_info=info))
        self.assertEqual(code, 0, "a Cloud instance is a WARN, never a FAIL")
        self.assertIn(f"{WARN} {ITEM_JIRA_VERSION}", out)
        self.assertIn("Cloud", out)
        self.assertIn("/search/jql", out)

    def test_pre_8_14_version_warns(self):
        info = jc.ServerInfo(version="8.5.0", version_numbers=[8, 5, 0], deployment_type="Server")
        code, out, _err = self._check([], _jira_env(), jira_cls=lambda: _OKJiraClient(server_info=info))
        self.assertEqual(code, 0, "an old version is a WARN, never a FAIL")
        self.assertIn(f"{WARN} {ITEM_JIRA_VERSION}", out)
        self.assertIn("8.14", out)
        self.assertIn("Bearer", out)

    def test_version_exactly_at_the_8_14_threshold_passes(self):
        info = jc.ServerInfo(version="8.14.0", version_numbers=[8, 14, 0], deployment_type="Server")
        code, out, _err = self._check([], _jira_env(), jira_cls=lambda: _OKJiraClient(server_info=info))
        self.assertEqual(code, 0)
        self.assertIn(f"{PASS} {ITEM_JIRA_VERSION}", out)

    def test_server_info_endpoint_unreachable_warns_not_fails(self):
        info_error = jc.JiraError("ServerInfo", "restricted", code="", status_code=403)

        class _RestrictedJiraClient(_OKJiraClient):
            def server_info(self):
                raise info_error

        code, out, _err = self._check([], _jira_env(), jira_cls=_RestrictedJiraClient)
        self.assertEqual(code, 0, "being unable to read the version must not block an otherwise-working run")
        self.assertIn(f"{WARN} {ITEM_JIRA_VERSION}", out)

    def test_unparseable_version_warns_rather_than_guessing(self):
        info = jc.ServerInfo(version="", version_numbers=[], deployment_type="Server")
        code, out, _err = self._check([], _jira_env(), jira_cls=lambda: _OKJiraClient(server_info=info))
        self.assertEqual(code, 0)
        self.assertIn(f"{WARN} {ITEM_JIRA_VERSION}", out)

    def test_version_numbers_list_too_short_falls_back_to_the_version_string(self):
        info = jc.ServerInfo(version="9.12.28", version_numbers=[9], deployment_type="Server")
        code, out, _err = self._check([], _jira_env(), jira_cls=lambda: _OKJiraClient(server_info=info))
        self.assertEqual(code, 0)
        self.assertIn(f"{PASS} {ITEM_JIRA_VERSION}", out)

    def test_never_prints_a_token_alongside_the_version_item(self):
        environ = {**_jira_env(), **_gitlab_env()}
        code, out, err = self._check([], environ)
        self.assertIn(f"{PASS} {ITEM_JIRA_VERSION}", out)
        self.assertNotIn(environ["JIRA_TOKEN"], out)
        self.assertNotIn(environ["JIRA_TOKEN"], err)

    def test_gitlab_auth_failed_fails(self):
        code, out, _err = self._check([], {**_jira_env(), **_gitlab_env()}, gitlab_cls=_FailingGitLabClient)
        self.assertEqual(code, 1)
        self.assertIn(f"{FAIL} {ITEM_GITLAB_CONN}", out)

    def test_story_point_field_not_found_fails(self):
        code, out, _err = self._check(
            [], _jira_env(), jira_cls=lambda: _OKJiraClient(field_ids={"Summary": "summary"})
        )
        self.assertEqual(code, 1)
        self.assertIn(f"{FAIL} {ITEM_STORY_POINT}", out)

    def test_configured_story_point_override_not_found_fails(self):
        with _tempdir() as tmp:
            (tmp / config_mod.DEFAULT_CONFIG_FILENAME).write_text(
                json.dumps({"story_points_field_id": "customfield_99999"}), encoding="utf-8"
            )
            code, out, _err = self._check([], _jira_env(), jira_cls=lambda: _OKJiraClient(field_ids={"Summary": "summary"}))
        self.assertEqual(code, 1)
        self.assertIn(f"{FAIL} {ITEM_STORY_POINT}", out)

    def test_sprint_id_resolves(self):
        sprint = jc.Sprint(id=100, name="Sprint 100", state="closed", board_id=1,
                            start_at=dt(2026, 1, 1), end_at=dt(2026, 1, 5), complete_at=dt(2026, 1, 5))
        code, out, _err = self._check(
            ["--sprint-ids", "100"], _jira_env(), jira_cls=lambda: _OKJiraClient(sprints={100: sprint})
        )
        self.assertEqual(code, 0)
        self.assertIn(f"{PASS} {ITEM_SPRINT_BOARD}", out)

    def test_sprint_id_not_found_fails(self):
        code, out, _err = self._check(["--sprint-ids", "999"], _jira_env(), jira_cls=lambda: _OKJiraClient())
        self.assertEqual(code, 1)
        self.assertIn(f"{FAIL} {ITEM_SPRINT_BOARD}", out)

    def test_board_id_mismatch_fails(self):
        sprint = jc.Sprint(id=100, name="Sprint 100", state="closed", board_id=1,
                            start_at=dt(2026, 1, 1), end_at=dt(2026, 1, 5), complete_at=dt(2026, 1, 5))
        code, out, _err = self._check(
            ["--sprint-ids", "100", "--board-id", "999"], _jira_env(), jira_cls=lambda: _OKJiraClient(sprints={100: sprint})
        )
        self.assertEqual(code, 1)
        self.assertIn(f"{FAIL} {ITEM_SPRINT_BOARD}", out)

    def test_no_sprint_ref_given_is_skip(self):
        code, out, _err = self._check([], _jira_env())
        self.assertEqual(code, 0)
        self.assertIn(f"{SKIP} {ITEM_SPRINT_BOARD}", out)

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
        self.assertIn(f"{SKIP} {ITEM_GITLAB_PROJECTS}", out)

    def test_no_proxy_flag_passes_trust_env_proxy_false_to_jira_client(self):
        captured = {}

        def factory(*_a, **kw):
            captured.update(kw)
            return _OKJiraClient()

        args = cli.build_parser().parse_args(["check", "--no-proxy"])
        with contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_check(args, _jira_env(), jira_client_cls=factory, gitlab_client_cls=lambda *_a, **_kw: _OKGitLabClient())
        self.assertIs(captured.get("trust_env_proxy"), False)

    def test_default_passes_trust_env_proxy_true_to_jira_client(self):
        captured = {}

        def factory(*_a, **kw):
            captured.update(kw)
            return _OKJiraClient()

        args = cli.build_parser().parse_args(["check"])
        with contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_check(args, _jira_env(), jira_client_cls=factory, gitlab_client_cls=lambda *_a, **_kw: _OKGitLabClient())
        self.assertIs(captured.get("trust_env_proxy"), True)

    def test_no_proxy_flag_passes_trust_env_proxy_false_to_gitlab_client(self):
        captured = {}

        def factory(*_a, **kw):
            captured.update(kw)
            return _OKGitLabClient()

        args = cli.build_parser().parse_args(["check", "--no-proxy"])
        with contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_check(
                args, {**_jira_env(), **_gitlab_env()},
                jira_client_cls=lambda *_a, **_kw: _OKJiraClient(), gitlab_client_cls=factory,
            )
        self.assertIs(captured.get("trust_env_proxy"), False)

    def test_default_passes_trust_env_proxy_true_to_gitlab_client(self):
        captured = {}

        def factory(*_a, **kw):
            captured.update(kw)
            return _OKGitLabClient()

        args = cli.build_parser().parse_args(["check"])
        with contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_check(
                args, {**_jira_env(), **_gitlab_env()},
                jira_client_cls=lambda *_a, **_kw: _OKJiraClient(), gitlab_client_cls=factory,
            )
        self.assertIs(captured.get("trust_env_proxy"), True)

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
        self.assertIn(f"{PASS} {ITEM_GITLAB_PROJECTS}", out)
        self.assertIn("2/2", out)

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
        self.assertIn(f"{FAIL} {ITEM_GITLAB_PROJECTS}", out)
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
        self.assertIn(f"{FAIL} {ITEM_GITLAB_CONN}", out)
        self.assertIn(f"{SKIP} {ITEM_GITLAB_PROJECTS}", out)


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
            self.assertIn("ошибка настройки", err.getvalue())

    def test_no_proxy_flag_passes_trust_env_proxy_false_to_jira_client(self):
        captured = {}

        def factory(*_a, **kw):
            captured.update(kw)
            return _FakeJiraClient()

        with _tempdir():
            args = cli.build_parser().parse_args(["run", "--sprint-ids", "100", "--no-gitlab", "--no-proxy"])
            with contextlib.redirect_stdout(io.StringIO()):
                cli.cmd_run(args, _jira_env(), jira_client_cls=factory)
        self.assertIs(captured.get("trust_env_proxy"), False)

    def test_default_passes_trust_env_proxy_true_to_jira_client(self):
        captured = {}

        def factory(*_a, **kw):
            captured.update(kw)
            return _FakeJiraClient()

        with _tempdir():
            args = cli.build_parser().parse_args(["run", "--sprint-ids", "100", "--no-gitlab"])
            with contextlib.redirect_stdout(io.StringIO()):
                cli.cmd_run(args, _jira_env(), jira_client_cls=factory)
        self.assertIs(captured.get("trust_env_proxy"), True)

    def test_no_proxy_flag_passes_trust_env_proxy_false_to_gitlab_client(self):
        """GitLabClient is only constructed when GitLab is actually
        configured (not --no-gitlab) — build_combined_report() itself is
        spied out so the fake GitLab client (which only implements
        current_user()/project_id(), not the full fetch_team_data()
        interface) never has to handle a real fetch."""
        captured = {}

        def factory(*_a, **kw):
            captured.update(kw)
            return _OKGitLabClient()

        fixture = _build_fixture_report_dict()
        with _tempdir():
            args = cli.build_parser().parse_args(["run", "--sprint-ids", "100", "--no-proxy"])
            with unittest.mock.patch.object(cli.report_data, "build_combined_report", return_value=fixture):
                with contextlib.redirect_stdout(io.StringIO()):
                    cli.cmd_run(
                        args, {**_jira_env(), **_gitlab_env()},
                        jira_client_cls=lambda *_a, **_kw: _FakeJiraClient(), gitlab_client_cls=factory,
                    )
        self.assertIs(captured.get("trust_env_proxy"), False)

    def test_default_passes_trust_env_proxy_true_to_gitlab_client(self):
        captured = {}

        def factory(*_a, **kw):
            captured.update(kw)
            return _OKGitLabClient()

        fixture = _build_fixture_report_dict()
        with _tempdir():
            args = cli.build_parser().parse_args(["run", "--sprint-ids", "100"])
            with unittest.mock.patch.object(cli.report_data, "build_combined_report", return_value=fixture):
                with contextlib.redirect_stdout(io.StringIO()):
                    cli.cmd_run(
                        args, {**_jira_env(), **_gitlab_env()},
                        jira_client_cls=lambda *_a, **_kw: _FakeJiraClient(), gitlab_client_cls=factory,
                    )
        self.assertIs(captured.get("trust_env_proxy"), True)

    # -- --no-mr-details / --no-pipeline-users / --light -----------------

    def _run_with_spy(self, extra_argv, spy_return):
        """Runs cmd_run with report_data.build_combined_report() replaced by
        a spy that records its kwargs and returns `spy_return` — isolates
        cli.py's own flag-wiring logic from report_data.py's actual behavior
        (out of scope, another agent's file) while still letting
        render_html.render_html() run for real against a fully valid report
        dict (spy_return should come from _build_fixture_report_dict())."""
        captured = {}

        def spy(*_a, **kw):
            captured.update(kw)
            return spy_return

        with _tempdir():
            args = cli.build_parser().parse_args(["run", "--sprint-ids", "100", "--no-gitlab"] + extra_argv)
            with unittest.mock.patch.object(cli.report_data, "build_combined_report", side_effect=spy):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = cli.cmd_run(args, _jira_env(), jira_client_cls=lambda *_a, **_kw: _FakeJiraClient())
        return code, out.getvalue(), captured

    def test_default_passes_both_fanout_flags_true(self):
        code, _out, captured = self._run_with_spy([], _build_fixture_report_dict())
        self.assertEqual(code, 0)
        self.assertIs(captured.get("fetch_mr_details"), True)
        self.assertIs(captured.get("fetch_pipeline_user"), True)

    def test_no_mr_details_flag_disables_only_mr_details(self):
        code, _out, captured = self._run_with_spy(["--no-mr-details"], _build_fixture_report_dict())
        self.assertEqual(code, 0)
        self.assertIs(captured.get("fetch_mr_details"), False)
        self.assertIs(captured.get("fetch_pipeline_user"), True)

    def test_no_pipeline_users_flag_disables_only_pipeline_user(self):
        code, _out, captured = self._run_with_spy(["--no-pipeline-users"], _build_fixture_report_dict())
        self.assertEqual(code, 0)
        self.assertIs(captured.get("fetch_mr_details"), True)
        self.assertIs(captured.get("fetch_pipeline_user"), False)

    def test_light_flag_disables_both_fanouts(self):
        code, _out, captured = self._run_with_spy(["--light"], _build_fixture_report_dict())
        self.assertEqual(code, 0)
        self.assertIs(captured.get("fetch_mr_details"), False)
        self.assertIs(captured.get("fetch_pipeline_user"), False)

    def test_light_combined_with_an_individual_flag_still_disables_both(self):
        code, _out, captured = self._run_with_spy(["--light", "--no-mr-details"], _build_fixture_report_dict())
        self.assertEqual(code, 0)
        self.assertIs(captured.get("fetch_mr_details"), False)
        self.assertIs(captured.get("fetch_pipeline_user"), False)

    # -- request-count print ----------------------------------------------

    def test_prints_gitlab_request_count_when_available(self):
        fixture = dict(_build_fixture_report_dict())
        fixture["params"] = {**fixture.get("params", {}), "gitlab_request_count": 1234}
        code, out, _captured = self._run_with_spy([], fixture)
        self.assertEqual(code, 0)
        self.assertIn("1234", out)
        self.assertIn("GitLab", out)
        self.assertIn("без учёта Jira", out, "must label the count as GitLab-only, not imply it is the run's total")

    def test_omits_request_count_line_when_gitlab_was_not_used(self):
        fixture = dict(_build_fixture_report_dict())
        fixture["params"] = {**fixture.get("params", {}), "gitlab_request_count": None}
        code, out, _captured = self._run_with_spy([], fixture)
        self.assertEqual(code, 0)
        self.assertNotIn("HTTP-запросов к GitLab", out)


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


# --------------------------------------------------------------------------
# --help wording is Russian
# --------------------------------------------------------------------------


class HelpTextIsRussianTests(unittest.TestCase):
    """argparse's `--help` prints and calls sys.exit(0) — captured the same
    way real terminal usage would see it. Asserts on the actual Russian
    wording cli.py sets, not a "contains Cyrillic somewhere" regex."""

    def _help_text(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as ctx:
                cli.build_parser().parse_args(argv)
        self.assertEqual(ctx.exception.code, 0)
        # argparse's HelpFormatter wraps long help= text across lines at the
        # terminal width it detects — collapse whitespace so a substring
        # check doesn't depend on exactly where that wrap lands.
        return " ".join(out.getvalue().split())

    def test_top_level_help_is_russian(self):
        text = self._help_text(["--help"])
        self.assertIn("Отчёты по метрикам команды из Jira и GitLab", text)
        self.assertIn("Создать .team-metrics.json в текущей папке", text)
        self.assertIn("Проверить настройку Jira/GitLab без построения отчёта", text)
        self.assertIn("Собрать данные из Jira/GitLab, посчитать метрики, записать JSON и HTML", text)
        self.assertIn("Отрисовать HTML из уже полученного JSON-файла — без обращений к сети", text)

    def test_init_help_is_russian(self):
        text = self._help_text(["init", "--help"])
        self.assertIn("Перезаписать существующий .team-metrics.json", text)

    def test_check_help_is_russian(self):
        text = self._help_text(["check", "--help"])
        self.assertIn("Список id спринтов через запятую", text)
        self.assertIn("Пропустить проверки GitLab", text)
        self.assertIn(cli.NO_PROXY_HELP, text)

    def test_run_help_is_russian(self):
        """Covers _translate_pipeline_help(): flags run shares with
        report_data.py via config.add_pipeline_args() must still show
        Russian help text on `run --help` even though report_data.py's own
        --help (untouched, out of scope) keeps its English text."""
        text = self._help_text(["run", "--help"])
        self.assertIn("Список id спринтов Jira через запятую (целевые спринты)", text)
        self.assertIn("Пропустить обе вкладки GitLab", text)
        self.assertIn("Путь для HTML-отчёта", text)
        self.assertIn("Путь для JSON-файла с данными", text)
        self.assertIn(cli.NO_PROXY_HELP, text)

    def test_run_help_states_the_fanout_tradeoff_plainly(self):
        """Item 3 of the task: a user must see this in --help, not discover
        it only by reading the report afterwards."""
        text = self._help_text(["run", "--help"])
        self.assertIn(cli.NO_MR_DETAILS_HELP, text)
        self.assertIn(cli.NO_PIPELINE_USERS_HELP, text)
        # Not a full-string check for LIGHT_HELP: argparse's wrapper can
        # break a line right at the hyphen inside "--no-mr-details" (turning
        # it into "--no-mr- details" even after whitespace-collapsing), which
        # LIGHT_HELP itself quotes verbatim — check distinctive fragments
        # either side of that risk spot instead.
        self.assertIn("Отключить оба тяжёлых обхода GitLab разом", text)
        self.assertIn("экономит около 1200 запросов из ~1660", text)
        self.assertIn("связанные метрики станут «нет данных»", text)
        for help_text in (cli.NO_MR_DETAILS_HELP, cli.NO_PIPELINE_USERS_HELP, cli.LIGHT_HELP):
            self.assertIn("нет данных", help_text)
        self.assertIn("800", cli.NO_MR_DETAILS_HELP)
        self.assertIn("750", cli.NO_PIPELINE_USERS_HELP)
        self.assertIn("1660", cli.LIGHT_HELP)

    def test_report_help_is_russian(self):
        text = self._help_text(["report", "--help"])
        self.assertIn("Путь к JSON-файлу report_data", text)
        self.assertIn("Свой путь к шаблону", text)

    def test_report_data_py_own_help_is_unaffected_and_stays_english(self):
        """_translate_pipeline_help() must only mutate the `run` subparser's
        own actions, never config.add_pipeline_args()'s shared definitions —
        report_data.py's own CLI must keep reading in English, unchanged."""
        text_out = io.StringIO()
        with contextlib.redirect_stdout(text_out):
            with self.assertRaises(SystemExit):
                config_mod.build_arg_parser().parse_args(["--help"])
        text = " ".join(text_out.getvalue().split())
        self.assertIn("Comma-separated Jira sprint ids (target sprints)", text)


# --------------------------------------------------------------------------
# invocation-name resolution: never an absolute path in a printed hint
# --------------------------------------------------------------------------


class InvocationNameTests(unittest.TestCase):
    def test_relative_candidate_used_as_is(self):
        self.assertEqual(cli._resolve_invocation_name("scripts/team-metrics", {}), "scripts/team-metrics")

    def test_dot_slash_relative_candidate_used_as_is(self):
        self.assertEqual(cli._resolve_invocation_name("./scripts/team-metrics", {}), "./scripts/team-metrics")

    def test_python_dash_m_friendly_string_used_as_is(self):
        self.assertEqual(cli._resolve_invocation_name("python3 -m team_metrics", {}), "python3 -m team_metrics")

    def test_absolute_candidate_collapses_to_basename(self):
        name = cli._resolve_invocation_name("/home/alice/.claude/skills/team-metrics/scripts/team-metrics", {})
        self.assertEqual(name, "team-metrics")

    def test_env_override_wins_over_a_relative_candidate(self):
        name = cli._resolve_invocation_name("scripts/team-metrics", {cli.INVOCATION_NAME_ENV_VAR: "team-metrics"})
        self.assertEqual(name, "team-metrics")

    def test_env_override_wins_over_an_absolute_candidate(self):
        name = cli._resolve_invocation_name(
            "/home/alice/.claude/skills/team-metrics/scripts/team-metrics",
            {cli.INVOCATION_NAME_ENV_VAR: "team-metrics"},
        )
        self.assertEqual(name, "team-metrics")

    def test_empty_or_missing_candidate_falls_back_to_short_name(self):
        self.assertEqual(cli._resolve_invocation_name(None, {}), "team-metrics")
        self.assertEqual(cli._resolve_invocation_name("", {}), "team-metrics")

    def test_result_is_never_an_absolute_path(self):
        candidates = (
            None, "", "scripts/team-metrics", "./scripts/team-metrics",
            "/home/alice/.claude/skills/team-metrics/scripts/team-metrics", "/",
        )
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                name = cli._resolve_invocation_name(candidate, {})
                self.assertFalse(os.path.isabs(name), f"{name!r} must not be an absolute path")

    def test_init_hint_never_contains_an_absolute_path_after_simulated_global_install(self):
        with _tempdir():
            absolute_argv0 = "/home/alice/.claude/skills/team-metrics/scripts/team-metrics"
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(["init"], environ={}, invocation=absolute_argv0)
            self.assertEqual(code, 0)
            self.assertNotIn("/home/alice", out.getvalue())
            self.assertIn("team-metrics check", out.getvalue())

    def test_init_hint_honors_launcher_env_override(self):
        with _tempdir():
            absolute_argv0 = "/home/alice/.claude/skills/team-metrics/scripts/team-metrics"
            out = io.StringIO()
            environ = {cli.INVOCATION_NAME_ENV_VAR: "team-metrics"}
            with contextlib.redirect_stdout(out):
                code = cli.main(["init"], environ=environ, invocation=absolute_argv0)
            self.assertEqual(code, 0)
            self.assertIn("дальше: team-metrics check", out.getvalue())


# --------------------------------------------------------------------------
# --version / -v
# --------------------------------------------------------------------------


class VersionCommandTests(unittest.TestCase):
    def _run_version(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as ctx:
                cli.main(argv, environ={})
        return ctx.exception.code, out.getvalue()

    def test_reads_version_from_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            version_file = Path(tmp) / "VERSION"
            version_file.write_text("9.9.9\n", encoding="utf-8")
            with unittest.mock.patch.object(cli, "VERSION_PATH", version_file):
                code, out = self._run_version(["--version"])
        self.assertEqual(code, 0)
        self.assertIn("team-metrics 9.9.9", out)

    def test_short_flag_also_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            version_file = Path(tmp) / "VERSION"
            version_file.write_text("2.0.0", encoding="utf-8")
            with unittest.mock.patch.object(cli, "VERSION_PATH", version_file):
                code, out = self._run_version(["-v"])
        self.assertEqual(code, 0)
        self.assertIn("team-metrics 2.0.0", out)

    def test_survives_missing_version_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "no-such-subdir" / "VERSION"
            with unittest.mock.patch.object(cli, "VERSION_PATH", missing):
                code, out = self._run_version(["--version"])
        self.assertEqual(code, 0, "a missing VERSION file must degrade gracefully, not crash")
        self.assertIn("team-metrics", out)
        self.assertIn("не найден файл VERSION", out)

    def test_survives_empty_version_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            version_file = Path(tmp) / "VERSION"
            version_file.write_text("   \n", encoding="utf-8")
            with unittest.mock.patch.object(cli, "VERSION_PATH", version_file):
                code, out = self._run_version(["--version"])
        self.assertEqual(code, 0)
        self.assertIn("team-metrics", out)


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------


class DoctorCommandTests(unittest.TestCase):
    def _doctor(
        self,
        environ,
        *,
        template_exists=True,
        global_dir_exists=True,
        launcher_found=True,
        write_config=True,
        write_old_config=False,
    ):
        with contextlib.ExitStack() as stack:
            tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))

            version_file = tmp / "VERSION_for_test"
            version_file.write_text("1.2.3", encoding="utf-8")
            stack.enter_context(unittest.mock.patch.object(cli, "VERSION_PATH", version_file))

            if not template_exists:
                stack.enter_context(
                    unittest.mock.patch.object(render_html_mod, "TEMPLATE_PATH", tmp / "missing-dir" / "report.html")
                )

            global_dir = tmp / "global_skill_dir"
            if global_dir_exists:
                global_dir.mkdir()
            stack.enter_context(unittest.mock.patch.object(cli, "GLOBAL_SKILL_DIR", global_dir))

            stack.enter_context(
                unittest.mock.patch.object(
                    cli.shutil, "which", lambda name: f"/usr/local/bin/{name}" if launcher_found else None
                )
            )

            workdir = tmp / "cwd"
            workdir.mkdir()
            if write_config:
                (workdir / config_mod.DEFAULT_CONFIG_FILENAME).write_text("{}", encoding="utf-8")
            if write_old_config:
                (workdir / config_mod.OLD_CONFIG_FILENAME).write_text("{}", encoding="utf-8")
            old_cwd = os.getcwd()
            os.chdir(workdir)
            stack.callback(os.chdir, old_cwd)

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(["doctor"], environ=environ, invocation="team-metrics")
            return code, out.getvalue()

    def test_healthy_checkout_all_pass(self):
        code, out = self._doctor({**_jira_env(), **_gitlab_env()})
        self.assertEqual(code, 0)
        self.assertIn(f"{PASS} версия Python", out)
        self.assertIn(f"{PASS} файлы skill", out)
        self.assertIn(f"{PASS} установка skill", out)
        self.assertIn(f"{PASS} команда team-metrics в PATH", out)
        self.assertIn(f"{PASS} файл настроек в текущей папке", out)
        self.assertIn(f"{PASS} переменные окружения", out)
        self.assertIn(f"{PASS} самопроверка шаблона отчёта", out)
        self.assertIn(f"{PASS} прокси", out)

    def test_run_from_checkout_warns_not_fails(self):
        code, out = self._doctor({}, global_dir_exists=False)
        self.assertEqual(code, 0)
        self.assertIn(f"{WARN} установка skill", out)
        self.assertIn("локальная копия", out)

    def test_no_config_file_warns_not_fails(self):
        code, out = self._doctor({}, write_config=False)
        self.assertEqual(code, 0)
        self.assertIn(f"{WARN} файл настроек в текущей папке", out)
        self.assertIn("team-metrics init", out)

    def test_old_config_file_warns_with_migration_hint_instead_of_init_hint(self):
        code, out = self._doctor({}, write_config=False, write_old_config=True)
        self.assertEqual(code, 0)
        self.assertIn(f"{WARN} файл настроек в текущей папке", out)
        self.assertIn(config_mod.OLD_CONFIG_FILENAME, out)
        self.assertIn(config_mod.DEFAULT_CONFIG_FILENAME, out)
        self.assertNotIn("team-metrics init", out)

    def test_new_config_file_takes_priority_over_old_one(self):
        code, out = self._doctor({}, write_config=True, write_old_config=True)
        self.assertEqual(code, 0)
        self.assertIn(f"{PASS} файл настроек в текущей папке", out)
        self.assertNotIn(config_mod.OLD_CONFIG_FILENAME, out)

    def test_launcher_not_on_path_warns_not_fails(self):
        code, out = self._doctor({}, launcher_found=False)
        self.assertEqual(code, 0)
        self.assertIn(f"{WARN} команда team-metrics в PATH", out)
        self.assertIn("PATH", out)

    def test_missing_template_fails(self):
        code, out = self._doctor({}, template_exists=False)
        self.assertNotEqual(code, 0, "a broken/truncated install is a real failure, doctor must exit non-zero")
        self.assertIn(f"{FAIL} файлы skill", out)
        self.assertIn(f"{FAIL} самопроверка шаблона отчёта", out)

    def test_missing_jira_env_vars_warns_not_fails(self):
        code, out = self._doctor({})
        self.assertEqual(code, 0, "doctor must work with no tokens set at all — that is the point of the command")
        self.assertIn(f"{WARN} переменные окружения", out)
        self.assertIn("JIRA_BASE_URL: не задан", out)
        self.assertIn("JIRA_TOKEN: не задан", out)

    def test_half_configured_gitlab_env_warns(self):
        environ = {**_jira_env(), "GITLAB_URL": "https://gitlab.example.com"}
        code, out = self._doctor(environ)
        self.assertEqual(code, 0)
        self.assertIn(f"{WARN} переменные окружения", out)

    def test_never_prints_a_token_value(self):
        environ = {
            "JIRA_BASE_URL": "https://jira.example.com",
            "JIRA_TOKEN": "super-secret-jira-token-xyz",
            "GITLAB_URL": "https://gitlab.example.com",
            "GITLAB_TOKEN": "super-secret-gitlab-token-abc",
        }
        code, out = self._doctor(environ)
        self.assertEqual(code, 0)
        self.assertNotIn("super-secret-jira-token-xyz", out)
        self.assertNotIn("super-secret-gitlab-token-abc", out)
        self.assertIn("JIRA_TOKEN: задан", out)
        self.assertIn("GITLAB_TOKEN: задан", out)

    def test_python_too_old_fails(self):
        """This suite only ever actually runs under >= 3.9 — faking
        sys.version_info (its real type isn't user-constructible, so a
        namedtuple with the same field names stands in) exercises the < 3.9
        branch without needing an old interpreter."""
        import collections

        fake_version_info = collections.namedtuple("version_info", ["major", "minor", "micro", "releaselevel", "serial"])
        with unittest.mock.patch.object(cli.sys, "version_info", fake_version_info(3, 8, 5, "final", 0)):
            code, out = self._doctor({})
        self.assertNotEqual(code, 0)
        self.assertIn(f"{FAIL} версия Python", out)
        self.assertIn("3.8.5", out)

    # -- proxy item -----------------------------------------------------

    def test_no_proxy_configured_passes(self):
        code, out = self._doctor({})
        self.assertEqual(code, 0)
        self.assertIn(f"{PASS} прокси", out)
        self.assertIn("не настроен", out)

    def test_https_proxy_configured_warns(self):
        code, out = self._doctor({"HTTPS_PROXY": "http://proxy.corp.example.com:8080"})
        self.assertEqual(code, 0, "proxy exposure is a WARN, never a FAIL")
        self.assertIn(f"{WARN} прокси", out)
        self.assertIn("proxy.corp.example.com", out)
        self.assertIn("Bearer", out)

    def test_lowercase_https_proxy_env_var_also_detected(self):
        code, out = self._doctor({"https_proxy": "http://proxy.corp.example.com:8080"})
        self.assertIn(f"{WARN} прокси", out)
        self.assertIn("proxy.corp.example.com", out)

    def test_http_proxy_configured_warns(self):
        code, out = self._doctor({"HTTP_PROXY": "http://proxy2.example.com:3128"})
        self.assertIn(f"{WARN} прокси", out)
        self.assertIn("proxy2.example.com", out)

    def test_proxy_url_credentials_never_printed(self):
        environ = {"HTTPS_PROXY": "http://svc-account:sup3r-secret-proxy-pw@proxy.corp.example.com:8080"}
        code, out = self._doctor(environ)
        self.assertIn(f"{WARN} прокси", out)
        self.assertIn("proxy.corp.example.com", out)
        self.assertNotIn("svc-account", out)
        self.assertNotIn("sup3r-secret-proxy-pw", out)
        self.assertNotIn("@proxy.corp.example.com", out, "the host-only extraction must not leave the userinfo separator behind")

    def test_no_proxy_covering_jira_host_downgrades_to_pass(self):
        environ = {
            "JIRA_BASE_URL": "https://jira.example.com",
            "HTTPS_PROXY": "http://proxy.corp.example.com:8080",
            "no_proxy": "jira.example.com",
        }
        code, out = self._doctor(environ)
        self.assertEqual(code, 0)
        self.assertIn(f"{PASS} прокси", out)
        self.assertIn("no_proxy", out)
        self.assertNotIn(f"{WARN} прокси", out)

    def test_no_proxy_domain_suffix_match_covers_subdomain(self):
        environ = {
            "JIRA_BASE_URL": "https://jira.corp.example.com",
            "HTTPS_PROXY": "http://proxy.corp.example.com:8080",
            "NO_PROXY": "example.com",
        }
        code, out = self._doctor(environ)
        self.assertIn(f"{PASS} прокси", out)

    def test_no_proxy_not_covering_jira_host_still_warns(self):
        environ = {
            "JIRA_BASE_URL": "https://jira.example.com",
            "HTTPS_PROXY": "http://proxy.corp.example.com:8080",
            "no_proxy": "other.example.com",
        }
        code, out = self._doctor(environ)
        self.assertIn(f"{WARN} прокси", out)
        self.assertIn("jira.example.com", out)

    def test_proxy_set_without_jira_base_url_warns_and_notes_it_cannot_check_no_proxy(self):
        code, out = self._doctor({"HTTPS_PROXY": "http://proxy.corp.example.com:8080"})
        self.assertIn(f"{WARN} прокси", out)
        self.assertIn("JIRA_BASE_URL", out)

    def test_proxy_warning_names_the_no_proxy_flag_as_the_remedy(self):
        code, out = self._doctor({"HTTPS_PROXY": "http://proxy.corp.example.com:8080"})
        self.assertIn(f"{WARN} прокси", out)
        self.assertIn("--no-proxy", out)

    def test_proxy_warning_states_the_flag_covers_both_jira_and_gitlab(self):
        """GitLabClient now accepts trust_env_proxy too (same as JiraClient) —
        the remedy text must say --no-proxy covers both, not just Jira, and
        must NOT carry the old "GitLab ещё не поддерживает" caveat (that
        would now be a lie)."""
        code, out = self._doctor({"HTTPS_PROXY": "http://proxy.corp.example.com:8080"})
        self.assertIn("GitLab", out)
        self.assertIn("--no-proxy", out)
        self.assertNotIn("не поддерживает", out)


if __name__ == "__main__":
    unittest.main()
