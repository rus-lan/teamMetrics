import _pathfix  # noqa: F401

import io
import json
import logging
import unittest
import unittest.mock

from team_metrics import gitlab_client as glc
from team_metrics import jira_client as jc
from team_metrics import logging_setup


class _ResetLoggerMixin:
    """Snapshots and restores the shared "team_metrics" logger's handlers/
    level/propagate around each test — logging_setup's module-level state is
    process-wide, so a test that calls setup_logging() must not leak a
    handler bound to this test's own stderr/stdout stubs into any test that
    runs after it (this file's own, or another test module's)."""

    def setUp(self):
        logger = logging.getLogger("team_metrics")
        self._orig_handlers = list(logger.handlers)
        self._orig_level = logger.level
        self._orig_propagate = logger.propagate
        self.addCleanup(self._restore)

    def _restore(self):
        logger = logging.getLogger("team_metrics")
        logger.handlers = list(self._orig_handlers)
        logger.setLevel(self._orig_level)
        logger.propagate = self._orig_propagate

    def _clear_handlers(self):
        logging.getLogger("team_metrics").handlers = []


class NullHandlerPresentTests(_ResetLoggerMixin, unittest.TestCase):
    def test_null_handler_present_before_setup_is_ever_called(self):
        # Attached at import time (module-level code in logging_setup.py) —
        # true regardless of whether setup_logging() has run yet in this
        # process, and regardless of test order.
        logger = logging.getLogger("team_metrics")
        self.assertTrue(any(isinstance(h, logging.NullHandler) for h in logger.handlers))

    def test_log_calls_are_silent_safe_without_setup(self):
        # Simulates a pristine, never-configured state explicitly (only a
        # NullHandler, NOTSET level, propagate False) rather than relying on
        # whatever this process's global logging state happens to be at this
        # point in the suite — setup_logging() may already have run in an
        # earlier test file by the time this one executes.
        logger = logging.getLogger("team_metrics")
        logger.handlers = [logging.NullHandler()]
        logger.setLevel(logging.NOTSET)
        logger.propagate = False
        stderr = io.StringIO()
        with unittest.mock.patch("sys.stderr", stderr):
            logging_setup.get_logger("probe").warning("should not raise or print anywhere")
        self.assertEqual(stderr.getvalue(), "")


class LevelSelectionTests(_ResetLoggerMixin, unittest.TestCase):
    def test_default_level_is_info(self):
        self._clear_handlers()
        logging_setup.setup_logging()
        self.assertEqual(logging.getLogger("team_metrics").level, logging.INFO)

    def test_verbose_sets_debug(self):
        self._clear_handlers()
        logging_setup.setup_logging(verbose=True)
        self.assertEqual(logging.getLogger("team_metrics").level, logging.DEBUG)

    def test_quiet_sets_error(self):
        self._clear_handlers()
        logging_setup.setup_logging(quiet=True)
        self.assertEqual(logging.getLogger("team_metrics").level, logging.ERROR)

    def test_quiet_wins_over_verbose(self):
        self._clear_handlers()
        logging_setup.setup_logging(verbose=True, quiet=True)
        self.assertEqual(logging.getLogger("team_metrics").level, logging.ERROR)


class FileHandlerDoesNotSuppressStderrTests(_ResetLoggerMixin, unittest.TestCase):
    """logging.FileHandler is a StreamHandler subclass -- a naive isinstance
    check would treat one pre-attached by an embedding process as "already
    have our stderr handler" and skip adding it, silently dropping all
    console output for that process."""

    def test_pre_attached_file_handler_does_not_block_the_stderr_handler(self):
        self._clear_handlers()
        import tempfile as _tempfile

        with _tempfile.TemporaryDirectory() as tmp:
            file_handler = logging.FileHandler(f"{tmp}/team-metrics.log")
            logging.getLogger("team_metrics").addHandler(file_handler)
            try:
                fake_stderr = io.StringIO()
                with unittest.mock.patch("sys.stderr", fake_stderr):
                    logging_setup.setup_logging()
                    logging_setup.get_logger("probe").info("must reach stderr too")
                self.assertIn("must reach stderr too", fake_stderr.getvalue())
            finally:
                file_handler.close()


class StreamTargetTests(_ResetLoggerMixin, unittest.TestCase):
    def test_handler_writes_to_stderr_not_stdout(self):
        self._clear_handlers()
        fake_stderr = io.StringIO()
        fake_stdout = io.StringIO()
        with unittest.mock.patch("sys.stderr", fake_stderr), unittest.mock.patch("sys.stdout", fake_stdout):
            logging_setup.setup_logging()
            logging_setup.get_logger("probe").info("hello from a test")
        self.assertIn("hello from a test", fake_stderr.getvalue())
        self.assertEqual(fake_stdout.getvalue(), "")

    def test_format_includes_level_and_message(self):
        self._clear_handlers()
        fake_stderr = io.StringIO()
        with unittest.mock.patch("sys.stderr", fake_stderr):
            logging_setup.setup_logging()
            logging_setup.get_logger("probe").info("distinctive message text")
        out = fake_stderr.getvalue()
        self.assertIn("[INFO]", out)
        self.assertIn("distinctive message text", out)


class IdempotencyTests(_ResetLoggerMixin, unittest.TestCase):
    def test_two_calls_add_only_one_handler(self):
        self._clear_handlers()
        fake_stderr = io.StringIO()
        with unittest.mock.patch("sys.stderr", fake_stderr):
            logging_setup.setup_logging()
            logging_setup.setup_logging()
        logger = logging.getLogger("team_metrics")
        stream_handlers = [h for h in logger.handlers if isinstance(h, logging.StreamHandler)]
        self.assertEqual(len(stream_handlers), 1)

    def test_second_call_still_updates_level(self):
        self._clear_handlers()
        fake_stderr = io.StringIO()
        with unittest.mock.patch("sys.stderr", fake_stderr):
            logging_setup.setup_logging(verbose=False, quiet=False)
            logging_setup.setup_logging(verbose=True, quiet=False)
        self.assertEqual(logging.getLogger("team_metrics").level, logging.DEBUG)


class GetLoggerTests(unittest.TestCase):
    def test_name_is_namespaced_under_team_metrics(self):
        logger = logging_setup.get_logger("jira_client")
        self.assertEqual(logger.name, "team_metrics.jira_client")


class PropagateTests(_ResetLoggerMixin, unittest.TestCase):
    def test_propagate_is_false_after_setup(self):
        self._clear_handlers()
        logging_setup.setup_logging()
        self.assertFalse(logging.getLogger("team_metrics").propagate)

    def test_propagate_is_false_even_without_setup(self):
        # Set at import time too, not only inside setup_logging() — a caller
        # that only ever uses get_logger() (never calls setup_logging) must
        # still never feed the real root logger.
        self.assertFalse(logging.getLogger("team_metrics").propagate)


class _FakeHttpResponse:
    def __init__(self, status=200, body=b"{}", headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    def read(self):
        return self._body


class _FakeOpener:
    """Minimal stand-in for urllib.request's opener, matching the `.open(req,
    timeout=...)` shape both JiraClient and GitLabClient call — captures the
    outgoing request headers (including the real auth header, so a test can
    prove the token really was sent) and always answers with a fixed
    response, never touching the network."""

    def __init__(self, response):
        self.response = response
        self.requests: list = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        return self.response


class ClientsNeverLogTokenTests(unittest.TestCase):
    """Task 5's hard requirement: whatever jira_client.py/gitlab_client.py
    log (DEBUG per request, INFO per JQL/page/project, ...), the token used
    to authenticate must never be one of the logged values."""

    FAKE_JIRA_TOKEN = "jira-super-secret-token-should-never-be-logged"
    FAKE_GITLAB_TOKEN = "glpat-super-secret-token-should-never-be-logged"

    def test_jira_client_never_logs_the_token(self):
        response = _FakeHttpResponse(status=200, body=json.dumps({"issues": [], "total": 0}).encode("utf-8"))
        opener = _FakeOpener(response)
        client = jc.JiraClient(
            "https://jira.example.com", self.FAKE_JIRA_TOKEN, opener=opener, sleep=lambda _seconds: None
        )

        with self.assertLogs("team_metrics.jira_client", level="DEBUG") as cm:
            client.search_issues("project = TEST", ["summary"])

        self.assertTrue(cm.output, "expected the search to actually produce log records")
        for line in cm.output:
            self.assertNotIn(self.FAKE_JIRA_TOKEN, line)

        # Sanity check: the token really was sent, on the Authorization
        # header specifically — proves the negative log assertion above
        # means something rather than the request never having a token to
        # leak in the first place.
        sent_auth_headers = [r.get_header("Authorization") for r in opener.requests]
        self.assertIn(f"Bearer {self.FAKE_JIRA_TOKEN}", sent_auth_headers)

    def test_gitlab_client_never_logs_the_token(self):
        response = _FakeHttpResponse(status=200, body=json.dumps([]).encode("utf-8"))
        opener = _FakeOpener(response)
        client = glc.GitLabClient(
            "https://gitlab.example.com", self.FAKE_GITLAB_TOKEN, opener=opener, sleep=lambda _seconds: None
        )

        with self.assertLogs("team_metrics.gitlab_client", level="DEBUG") as cm:
            client.merge_requests("group/proj", 1, ["alice"], errors=[])

        self.assertTrue(cm.output, "expected the fetch to actually produce log records")
        for line in cm.output:
            self.assertNotIn(self.FAKE_GITLAB_TOKEN, line)

        sent_auth_headers = [r.get_header("Private-token") for r in opener.requests]
        self.assertIn(self.FAKE_GITLAB_TOKEN, sent_auth_headers)


if __name__ == "__main__":
    unittest.main()
