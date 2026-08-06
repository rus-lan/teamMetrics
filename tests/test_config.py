import _pathfix  # noqa: F401

import json
import os
import tempfile
import unittest

from team_metrics import config


class LoadEnvTests(unittest.TestCase):
    def test_missing_base_url_raises(self):
        with self.assertRaises(config.ConfigError):
            config.load_env({"JIRA_TOKEN": "tok"})

    def test_missing_token_raises(self):
        with self.assertRaises(config.ConfigError):
            config.load_env({"JIRA_BASE_URL": "https://jira.example.com"})

    def test_both_present(self):
        env = config.load_env({"JIRA_BASE_URL": "https://jira.example.com", "JIRA_TOKEN": "tok"})
        self.assertEqual(env.base_url, "https://jira.example.com")
        self.assertEqual(env.token, "tok")


class FileConfigTests(unittest.TestCase):
    def _write(self, obj) -> str:
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        self.addCleanup(os.remove, path)
        return path

    def test_missing_optional_config_file_falls_back_to_defaults(self):
        # Run from an empty temp dir so a stray ./.team-metrics.json elsewhere
        # (e.g. the repo root) can never leak into this test.
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            self.addCleanup(os.chdir, cwd)
            cfg = config.load_file_config(None)
        self.assertEqual(cfg.cancelled_statuses, config.DEFAULT_CANCELLED_STATUSES)
        self.assertEqual(cfg.status_map, {})

    def test_explicit_missing_path_raises(self):
        with self.assertRaises(config.ConfigError):
            config.load_file_config("/no/such/file.json")

    def test_rejects_token_like_keys(self):
        path = self._write({"token": "super-secret"})
        with self.assertRaises(config.ConfigError):
            config.load_file_config(path)

    def test_rejects_pat_key_too(self):
        path = self._write({"pat": "secret"})
        with self.assertRaises(config.ConfigError):
            config.load_file_config(path)

    def test_rejects_private_token_key(self):
        path = self._write({"private_token": "glpat-xxx"})
        with self.assertRaises(config.ConfigError):
            config.load_file_config(path)

    def test_rejects_access_token_key(self):
        path = self._write({"access_token": "xxx"})
        with self.assertRaises(config.ConfigError):
            config.load_file_config(path)

    def test_rejects_api_key_key(self):
        path = self._write({"api_key": "xxx"})
        with self.assertRaises(config.ConfigError):
            config.load_file_config(path)

    def test_rejects_api_key_nested_under_gitlab(self):
        path = self._write({"gitlab": {"projects": ["a/b"], "api_key": "xxx"}})
        with self.assertRaises(config.ConfigError):
            config.load_file_config(path)

    def test_rejects_private_token_nested_under_gitlab(self):
        path = self._write({"gitlab": {"private_token": "xxx"}})
        with self.assertRaises(config.ConfigError):
            config.load_file_config(path)

    def test_key_variant_casing_and_separators_all_rejected(self):
        for bad_key in ("PRIVATE_TOKEN", "Access-Token", "API KEY", "apiKey", "ACCESSTOKEN"):
            with self.subTest(bad_key=bad_key):
                path = self._write({bad_key: "xxx"})
                with self.assertRaises(config.ConfigError):
                    config.load_file_config(path)

    def test_legitimate_schema_keys_are_never_rejected(self):
        path = self._write({
            "status_map": {"In Review": "indeterminate"},
            "cancelled_statuses": ["Won't Do"],
            "story_points_field_id": "customfield_10099",
            "history_sprint_count": 3,
            "gitlab": {"projects": ["group/proj"]},
            "employees": ["alice"],
            "jira": {"final_statuses": ["Done"]},
        })
        config.load_file_config(path)  # must not raise

    def test_reads_status_map_and_cancelled_statuses(self):
        path = self._write({
            "status_map": {"In Review": "indeterminate"},
            "cancelled_statuses": ["Won't Do"],
            "story_points_field_id": "customfield_10099",
            "history_sprint_count": 3,
        })
        cfg = config.load_file_config(path)
        self.assertEqual(cfg.status_map, {"In Review": "indeterminate"})
        self.assertEqual(cfg.cancelled_statuses, ["Won't Do"])
        self.assertEqual(cfg.story_points_field_id, "customfield_10099")
        self.assertEqual(cfg.history_sprint_count, 3)

    def test_empty_cancelled_statuses_falls_back_to_default(self):
        path = self._write({"cancelled_statuses": []})
        cfg = config.load_file_config(path)
        self.assertEqual(cfg.cancelled_statuses, config.DEFAULT_CANCELLED_STATUSES)


class CrlfTokenValidationTests(unittest.TestCase):
    """m1: a token with embedded CR/LF must be rejected before it can ever
    reach an HTTP header, with a message that never echoes the value."""

    def test_jira_token_with_crlf_rejected(self):
        with self.assertRaises(config.ConfigError):
            config.load_env({"JIRA_BASE_URL": "https://jira.example.com", "JIRA_TOKEN": "tok\r\nInjected: x"})

    def test_gitlab_token_with_crlf_rejected(self):
        with self.assertRaises(config.ConfigError):
            config.load_gitlab_env({"GITLAB_URL": "https://gitlab.example.com", "GITLAB_TOKEN": "tok\nInjected: x"})

    def test_error_message_never_echoes_token_value(self):
        secret = "tok\r\nInjected: x"
        try:
            config.load_env({"JIRA_BASE_URL": "https://jira.example.com", "JIRA_TOKEN": secret})
            self.fail("expected ConfigError")
        except config.ConfigError as e:
            self.assertNotIn(secret, str(e))
            self.assertNotIn("Injected", str(e))


class GitLabEnvTests(unittest.TestCase):
    def test_both_absent_returns_none_not_an_error(self):
        self.assertIsNone(config.load_gitlab_env({}))

    def test_both_present(self):
        env = config.load_gitlab_env({"GITLAB_URL": "https://gitlab.example.com", "GITLAB_TOKEN": "tok"})
        self.assertEqual(env.base_url, "https://gitlab.example.com")
        self.assertEqual(env.token, "tok")

    def test_url_without_token_raises(self):
        with self.assertRaises(config.ConfigError):
            config.load_gitlab_env({"GITLAB_URL": "https://gitlab.example.com"})

    def test_token_without_url_raises(self):
        with self.assertRaises(config.ConfigError):
            config.load_gitlab_env({"GITLAB_TOKEN": "tok"})


class TokenKeyRejectionTests(unittest.TestCase):
    def _write(self, obj) -> str:
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        self.addCleanup(os.remove, path)
        return path

    def test_rejects_gitlab_token_key_nested_under_gitlab(self):
        path = self._write({"gitlab": {"projects": ["a/b"], "token": "super-secret"}})
        with self.assertRaises(config.ConfigError):
            config.load_file_config(path)

    def test_rejects_secret_key_nested_under_gitlab(self):
        path = self._write({"gitlab": {"secret": "x"}})
        with self.assertRaises(config.ConfigError):
            config.load_file_config(path)

    def test_gitlab_object_without_token_keys_is_fine(self):
        path = self._write({"gitlab": {"projects": ["group/proj"]}})
        cfg = config.load_file_config(path)
        self.assertEqual(cfg.gitlab_projects, ["group/proj"])


class GitLabFileConfigTests(unittest.TestCase):
    def _write(self, obj) -> str:
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        self.addCleanup(os.remove, path)
        return path

    def test_gitlab_projects_and_employees_and_final_statuses(self):
        path = self._write({
            "gitlab": {"projects": ["group/proj-a", "group/proj-b"]},
            "employees": ["alice", "bob"],
            "jira": {"final_statuses": ["Done", "Closed"]},
        })
        cfg = config.load_file_config(path)
        self.assertEqual(cfg.gitlab_projects, ["group/proj-a", "group/proj-b"])
        self.assertEqual(cfg.employees, ["alice", "bob"])
        self.assertEqual(cfg.final_statuses, ["Done", "Closed"])

    def test_missing_gitlab_and_jira_keys_fall_back_to_defaults(self):
        path = self._write({})
        cfg = config.load_file_config(path)
        self.assertEqual(cfg.gitlab_projects, [])
        self.assertEqual(cfg.employees, [])
        self.assertEqual(cfg.final_statuses, list(config.DEFAULT_FINAL_STATUSES))

    def test_gitlab_key_must_be_an_object(self):
        path = self._write({"gitlab": ["not", "an", "object"]})
        with self.assertRaises(config.ConfigError):
            config.load_file_config(path)

    def test_jira_key_must_be_an_object(self):
        path = self._write({"jira": "not an object"})
        with self.assertRaises(config.ConfigError):
            config.load_file_config(path)


class NoGitlabNoPersonalFlagTests(unittest.TestCase):
    def _env(self):
        return {"JIRA_BASE_URL": "https://jira.example.com", "JIRA_TOKEN": "tok"}

    def test_flags_default_to_false(self):
        run_cfg = config.parse_args(["--sprint-ids", "1"], environ=self._env())
        self.assertFalse(run_cfg.no_gitlab)
        self.assertFalse(run_cfg.no_personal)

    def test_no_gitlab_flag_parsed(self):
        run_cfg = config.parse_args(["--sprint-ids", "1", "--no-gitlab"], environ=self._env())
        self.assertTrue(run_cfg.no_gitlab)

    def test_no_personal_flag_parsed(self):
        run_cfg = config.parse_args(["--sprint-ids", "1", "--no-personal"], environ=self._env())
        self.assertTrue(run_cfg.no_personal)

    def test_gitlab_env_is_none_when_not_set(self):
        run_cfg = config.parse_args(["--sprint-ids", "1"], environ=self._env())
        self.assertIsNone(run_cfg.gitlab_env)

    def test_gitlab_env_populated_when_set(self):
        env = dict(self._env())
        env["GITLAB_URL"] = "https://gitlab.example.com"
        env["GITLAB_TOKEN"] = "gtok"
        run_cfg = config.parse_args(["--sprint-ids", "1"], environ=env)
        self.assertEqual(run_cfg.gitlab_env.base_url, "https://gitlab.example.com")

    def test_fetch_flags_default_to_true(self):
        run_cfg = config.parse_args(["--sprint-ids", "1"], environ=self._env())
        self.assertTrue(run_cfg.fetch_mr_details)
        self.assertTrue(run_cfg.fetch_pipeline_user)

    def test_no_mr_details_flag_disables_fetch_mr_details(self):
        run_cfg = config.parse_args(["--sprint-ids", "1", "--no-mr-details"], environ=self._env())
        self.assertFalse(run_cfg.fetch_mr_details)
        self.assertTrue(run_cfg.fetch_pipeline_user)  # unaffected

    def test_no_pipeline_users_flag_disables_fetch_pipeline_user(self):
        # Flag is plural ("users"), matching cli.py's own --no-pipeline-users
        # exactly — RunConfig.fetch_pipeline_user (singular) is unrelated to
        # the flag spelling, it just names the gitlab_client.py parameter.
        run_cfg = config.parse_args(["--sprint-ids", "1", "--no-pipeline-users"], environ=self._env())
        self.assertTrue(run_cfg.fetch_mr_details)  # unaffected
        self.assertFalse(run_cfg.fetch_pipeline_user)


class HistorySprintCountTests(unittest.TestCase):
    def test_zero_or_negative_defaults_to_five(self):
        self.assertEqual(config.resolve_history_sprint_count(0), 5)
        self.assertEqual(config.resolve_history_sprint_count(-3), 5)

    def test_clamped_to_twenty(self):
        self.assertEqual(config.resolve_history_sprint_count(100), 20)

    def test_passthrough_within_range(self):
        self.assertEqual(config.resolve_history_sprint_count(8), 8)


class ParseArgsTests(unittest.TestCase):
    def _env(self):
        return {"JIRA_BASE_URL": "https://jira.example.com", "JIRA_TOKEN": "tok"}

    def test_sprint_ids_parsed_as_ints(self):
        run_cfg = config.parse_args(["--sprint-ids", "101, 102"], environ=self._env())
        self.assertEqual(run_cfg.sprint_ids, [101, 102])
        self.assertEqual(run_cfg.sprint_names, [])

    def test_sprint_names_parsed_and_trimmed(self):
        run_cfg = config.parse_args(["--sprint-names", " Sprint 1 , Sprint 2 "], environ=self._env())
        self.assertEqual(run_cfg.sprint_names, ["Sprint 1", "Sprint 2"])

    def test_default_seed_is_fixed(self):
        run_cfg = config.parse_args(["--sprint-ids", "1"], environ=self._env())
        self.assertEqual(run_cfg.seed, config.DEFAULT_SEED)

    def test_mutually_exclusive_ids_and_names_rejected_by_argparse(self):
        with self.assertRaises(SystemExit):
            config.parse_args(["--sprint-ids", "1", "--sprint-names", "Sprint 1"], environ=self._env())

    def test_history_default_zero_becomes_five(self):
        run_cfg = config.parse_args(["--sprint-ids", "1"], environ=self._env())
        self.assertEqual(run_cfg.history_sprint_count, 5)


if __name__ == "__main__":
    unittest.main()
