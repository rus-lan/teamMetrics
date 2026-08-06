import _pathfix  # noqa: F401

import http.client
import json
import socket
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request
from datetime import datetime, timezone

from team_metrics import gitlab_client as gc

UTC = timezone.utc


def _client(**kw):
    kw.setdefault("sleep", lambda _d: None)
    return gc.GitLabClient("https://gitlab.example.com", "tok", **kw)


class RetryLoopTests(unittest.TestCase):
    """Exercises Client._do's retry/backoff state machine with a fake
    transport (Client._attempt monkeypatched) — no network involved."""

    def test_success_on_first_attempt(self):
        client = _client()
        client._attempt = lambda url: (b'{"ok":true}', 200, {}, None)
        data, _headers = client._do("Op", "/x")
        self.assertEqual(data, b'{"ok":true}')

    def test_retries_5xx_then_succeeds(self):
        client = _client()
        calls = {"n": 0}

        def fake_attempt(url):
            calls["n"] += 1
            if calls["n"] < 3:
                return b"server error", 503, {}, None
            return b"ok", 200, {}, None

        client._attempt = fake_attempt
        data, _headers = client._do("Op", "/x")
        self.assertEqual(data, b"ok")
        self.assertEqual(calls["n"], 3)

    def test_retries_429_too(self):
        client = _client()
        calls = {"n": 0}

        def fake_attempt(url):
            calls["n"] += 1
            if calls["n"] < 2:
                return b"slow down", 429, {}, None
            return b"ok", 200, {}, None

        client._attempt = fake_attempt
        data, _headers = client._do("Op", "/x")
        self.assertEqual(data, b"ok")

    def test_gives_up_after_max_retries_and_keeps_status_code(self):
        client = _client(max_retries=2)
        calls = {"n": 0}

        def fake_attempt(url):
            calls["n"] += 1
            return b"still down", 503, {}, None

        client._attempt = fake_attempt
        with self.assertRaises(gc.GitLabError) as ctx:
            client._do("Op", "/x")
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(calls["n"], 3)  # first attempt + 2 retries

    def test_401_is_not_retried_and_classified_auth_failed(self):
        client = _client()
        calls = {"n": 0}

        def fake_attempt(url):
            calls["n"] += 1
            return b"unauthorized", 401, {}, None

        client._attempt = fake_attempt
        with self.assertRaises(gc.GitLabError) as ctx:
            client._do("Op", "/x")
        self.assertEqual(calls["n"], 1)  # 4xx (except 429) never retried
        self.assertEqual(ctx.exception.code, "AUTH_FAILED")

    def test_404_is_not_retried_and_classified_not_found(self):
        client = _client()
        client._attempt = lambda url: (b"nope", 404, {}, None)
        with self.assertRaises(gc.GitLabError) as ctx:
            client._do("Op", "/x")
        self.assertEqual(ctx.exception.code, "NOT_FOUND")

    def test_network_error_retries_then_raises_unreachable(self):
        client = _client()
        client._attempt = lambda url: (None, 0, {}, ConnectionError("boom"))
        with self.assertRaises(gc.GitLabError) as ctx:
            client._do("Op", "/x")
        self.assertEqual(ctx.exception.code, "UNREACHABLE")

    def test_backoff_delay_is_bounded_exponential(self):
        client = _client(base_backoff=0.5, max_backoff=8.0)
        self.assertEqual(client._backoff_delay(0), 0.5)
        self.assertEqual(client._backoff_delay(1), 1.0)
        self.assertEqual(client._backoff_delay(2), 2.0)
        self.assertEqual(client._backoff_delay(4), 8.0)  # 0.5*2^4 == cap exactly
        self.assertEqual(client._backoff_delay(10), 8.0)  # far above cap -> clamped


class RetryAfterTests(unittest.TestCase):
    """Audit finding 1: a 429 used to sleep our own backoff regardless of
    what the server asked for. Mirrors jira_client.py's Retry-After rule:
    only ever widens the delay, capped at max_retry_after."""

    def test_retry_after_widens_the_delay_beyond_our_backoff(self):
        client = _client(base_backoff=0.5, max_backoff=8.0)
        sleeps = []
        client._sleep = lambda d: sleeps.append(d)
        calls = {"n": 0}

        def fake_attempt(url):
            calls["n"] += 1
            if calls["n"] == 1:
                return b"slow down", 429, {"retry-after": "5"}, None
            return b"ok", 200, {}, None

        client._attempt = fake_attempt
        client._do("Op", "/x")
        # Our own backoff for attempt 0 is 0.5s; Retry-After: 5 must win.
        self.assertEqual(sleeps, [5.0])

    def test_our_backoff_wins_when_retry_after_is_shorter(self):
        client = _client(base_backoff=5.0, max_backoff=30.0)
        sleeps = []
        client._sleep = lambda d: sleeps.append(d)
        calls = {"n": 0}

        def fake_attempt(url):
            calls["n"] += 1
            if calls["n"] == 1:
                return b"slow down", 429, {"retry-after": "1"}, None
            return b"ok", 200, {}, None

        client._attempt = fake_attempt
        client._do("Op", "/x")
        self.assertEqual(sleeps, [5.0])  # our backoff (5.0) > Retry-After (1.0)

    def test_retry_after_is_capped_at_max_retry_after(self):
        client = _client(base_backoff=0.5, max_backoff=8.0, max_retry_after=60.0)
        sleeps = []
        client._sleep = lambda d: sleeps.append(d)
        calls = {"n": 0}

        def fake_attempt(url):
            calls["n"] += 1
            if calls["n"] == 1:
                return b"slow down", 429, {"retry-after": "600"}, None  # server asks for 10 minutes
            return b"ok", 200, {}, None

        client._attempt = fake_attempt
        client._do("Op", "/x")
        self.assertEqual(sleeps, [60.0])  # clamped to max_retry_after, not 600

    def test_missing_retry_after_header_falls_back_to_backoff(self):
        client = _client(base_backoff=0.5, max_backoff=8.0)
        sleeps = []
        client._sleep = lambda d: sleeps.append(d)
        calls = {"n": 0}

        def fake_attempt(url):
            calls["n"] += 1
            if calls["n"] == 1:
                return b"slow down", 429, {}, None  # no Retry-After header at all
            return b"ok", 200, {}, None

        client._attempt = fake_attempt
        client._do("Op", "/x")
        self.assertEqual(sleeps, [0.5])

    def test_non_429_retryable_status_ignores_retry_after(self):
        # A 503 carrying a Retry-After (some proxies add it to any 5xx)
        # must not honour it — the rule is scoped to 429 specifically.
        client = _client(base_backoff=0.5, max_backoff=8.0)
        sleeps = []
        client._sleep = lambda d: sleeps.append(d)
        calls = {"n": 0}

        def fake_attempt(url):
            calls["n"] += 1
            if calls["n"] == 1:
                return b"down", 503, {"retry-after": "30"}, None
            return b"ok", 200, {}, None

        client._attempt = fake_attempt
        client._do("Op", "/x")
        self.assertEqual(sleeps, [0.5])

    def test_parse_retry_after_seconds_rejects_garbage(self):
        self.assertIsNone(gc._parse_retry_after_seconds(""))
        self.assertIsNone(gc._parse_retry_after_seconds("not-a-number"))
        self.assertIsNone(gc._parse_retry_after_seconds("-5"))
        self.assertEqual(gc._parse_retry_after_seconds("12"), 12.0)


class AttemptExceptionHandlingTests(unittest.TestCase):
    """Exercises `_attempt`'s own exception handling with a fake opener —
    not the monkeypatched `_do` retry loop above (M6). Before the fix, only
    urllib.error.HTTPError/URLError/TimeoutError were caught; a mid-body
    failure (ConnectionResetError, IncompleteRead) or a bare socket.timeout
    (Python 3.9, before it was aliased to TimeoutError) escaped uncaught."""

    def test_connection_reset_during_body_read_is_caught_and_returned(self):
        class FakeResp:
            status = 200
            headers = {}

            def read(self):
                raise ConnectionResetError("connection reset by peer")

        class FakeOpener:
            def open(self, req, timeout=None):
                return FakeResp()

        client = gc.GitLabClient("https://gitlab.example.com", "tok", opener=FakeOpener(), sleep=lambda _d: None)
        data, status, headers, err = client._attempt("https://gitlab.example.com/x")
        self.assertIsNone(data)
        self.assertEqual(status, 0)
        self.assertIsInstance(err, ConnectionResetError)

    def test_incomplete_read_during_body_read_is_caught_and_returned(self):
        class FakeResp:
            status = 200
            headers = {}

            def read(self):
                raise http.client.IncompleteRead(b"partial")

        class FakeOpener:
            def open(self, req, timeout=None):
                return FakeResp()

        client = gc.GitLabClient("https://gitlab.example.com", "tok", opener=FakeOpener(), sleep=lambda _d: None)
        data, status, headers, err = client._attempt("https://gitlab.example.com/x")
        self.assertIsNone(data)
        self.assertIsInstance(err, http.client.IncompleteRead)

    def test_socket_timeout_is_caught(self):
        """On 3.10+, socket.timeout IS TimeoutError; on 3.9 it is a distinct
        OSError subclass the old `except TimeoutError` missed."""

        class FakeOpener:
            def open(self, req, timeout=None):
                raise socket.timeout("timed out")

        client = gc.GitLabClient("https://gitlab.example.com", "tok", opener=FakeOpener(), sleep=lambda _d: None)
        data, status, headers, err = client._attempt("https://gitlab.example.com/x")
        self.assertIsNone(data)
        self.assertIsInstance(err, OSError)

    def test_http_error_body_read_failure_is_caught_not_raised(self):
        class FakeHTTPError(urllib.error.HTTPError):
            def __init__(self):
                super().__init__("http://x", 500, "err", {}, None)

            def read(self):
                raise ConnectionResetError("reset while reading error body")

        class FakeOpener:
            def open(self, req, timeout=None):
                raise FakeHTTPError()

        client = gc.GitLabClient("https://gitlab.example.com", "tok", opener=FakeOpener(), sleep=lambda _d: None)
        data, status, headers, err = client._attempt("https://gitlab.example.com/x")
        self.assertIsNone(data)
        self.assertIsInstance(err, ConnectionResetError)

    def test_connection_reset_retries_end_to_end_through_a_real_attempt(self):
        """Not a monkeypatched _do: a real _attempt failure must still drive
        _do's retry/backoff loop and end in GitLabError(code=UNREACHABLE)."""

        class FlakyOpener:
            def __init__(self):
                self.calls = 0

            def open(self, req, timeout=None):
                self.calls += 1
                raise ConnectionResetError("boom")

        opener = FlakyOpener()
        client = gc.GitLabClient(
            "https://gitlab.example.com", "tok", opener=opener, sleep=lambda _d: None, max_retries=2
        )
        with self.assertRaises(gc.GitLabError) as ctx:
            client._do("Op", "/x")
        self.assertEqual(ctx.exception.code, "UNREACHABLE")
        self.assertEqual(opener.calls, 3)  # first attempt + 2 retries

    def test_headers_are_lowercased_once(self):
        class FakeResp:
            status = 200
            headers = {"X-Next-Page": "2", "Content-Type": "application/json"}

            def read(self):
                return b"[]"

        class FakeOpener:
            def open(self, req, timeout=None):
                return FakeResp()

        client = gc.GitLabClient("https://gitlab.example.com", "tok", opener=FakeOpener(), sleep=lambda _d: None)
        _data, _status, headers, _err = client._attempt("https://gitlab.example.com/x")
        self.assertEqual(headers, {"x-next-page": "2", "content-type": "application/json"})


class PaginationTests(unittest.TestCase):
    def test_x_next_page_header_drives_pagination(self):
        client = _client()
        pages = {
            1: (json.dumps([{"id": 1}, {"id": 2}]).encode(), {"x-next-page": "2"}),
            2: (json.dumps([{"id": 3}]).encode(), {"x-next-page": ""}),
        }
        calls = []

        def fake_do(op, path, params=None):
            calls.append(params["page"])
            return pages[params["page"]]

        client._do = fake_do
        items = client._get_paginated("Op", "/x", per_page=2)
        self.assertEqual([i["id"] for i in items], [1, 2, 3])
        self.assertEqual(calls, [1, 2])

    def test_missing_header_falls_back_to_page_counter_and_short_page_stop(self):
        client = _client()
        pages = {
            1: (json.dumps([{"id": 1}, {"id": 2}]).encode(), {}),
            2: (json.dumps([{"id": 3}]).encode(), {}),  # short page (1 < per_page=2) -> stop
        }
        client._do = lambda op, path, params=None: pages[params["page"]]
        items = client._get_paginated("Op", "/x", per_page=2)
        self.assertEqual([i["id"] for i in items], [1, 2, 3])

    def test_empty_first_page_stops_immediately(self):
        client = _client()
        client._do = lambda op, path, params=None: (b"[]", {})
        items = client._get_paginated("Op", "/x", per_page=100)
        self.assertEqual(items, [])

    def test_non_list_response_raises(self):
        client = _client()
        client._do = lambda op, path, params=None: (b'{"not": "a list"}', {})
        with self.assertRaises(gc.GitLabError):
            client._get_paginated("Op", "/x", per_page=100)

    def test_page_cap_stops_a_server_that_ignores_page(self):
        """n4: a server that always returns a full page with no
        X-Next-Page header is indistinguishable from "ignores `page`"
        without a hard cap."""
        client = _client()
        client._do = lambda op, path, params=None: (json.dumps([{"id": 1}, {"id": 2}]).encode(), {})
        with self.assertRaises(gc.GitLabError):
            client._get_paginated("Op", "/x", per_page=2)

    def test_deadline_triggers_before_the_first_page_when_already_expired(self):
        """Audit finding 3: the page cap bounds pages, not wall-clock — a
        slow-but-responding server could hold a run open indefinitely.
        Mirrors jira_client.py's test pattern: expire the deadline before
        the first check by monkeypatching the module constant."""
        original = gc._MAX_PAGINATION_SECONDS
        gc._MAX_PAGINATION_SECONDS = -1.0
        try:
            client = _client()
            calls = {"n": 0}

            def fake_do(op, path, params=None):
                calls["n"] += 1
                return b"[]", {}

            client._do = fake_do
            with self.assertRaises(gc.GitLabError) as ctx:
                client._get_paginated("Op", "/x", per_page=100)
            self.assertEqual(ctx.exception.code, "UNREACHABLE")
            self.assertEqual(calls["n"], 0)  # never even got to make a request
        finally:
            gc._MAX_PAGINATION_SECONDS = original

    def test_deadline_triggers_mid_pagination_on_a_slow_server(self):
        original = gc._MAX_PAGINATION_SECONDS
        gc._MAX_PAGINATION_SECONDS = 0.01
        try:
            client = _client()

            def fake_do(op, path, params=None):
                time.sleep(0.02)  # slower than the deadline
                return json.dumps([{"id": 1}, {"id": 2}]).encode(), {}  # full page -> keeps paginating

            client._do = fake_do
            with self.assertRaises(gc.GitLabError) as ctx:
                client._get_paginated("Op", "/x", per_page=2)
            self.assertEqual(ctx.exception.code, "UNREACHABLE")
        finally:
            gc._MAX_PAGINATION_SECONDS = original


class FormatWindowBoundTests(unittest.TestCase):
    """Regression guard: strftime("%Y-...") delegates year formatting to the
    platform C library, which does not zero-pad years below 1000 on Python
    3.9 (produces "1-01-01" instead of "0001-01-01"), while 3.10+ does.
    `_format_window_bound` must not depend on that platform behavior."""

    def test_year_below_1000_is_zero_padded(self):
        self.assertEqual(gc._format_window_bound(datetime(1, 1, 1, tzinfo=UTC)), "0001-01-01T00:00:00Z")

    def test_normal_year_still_formats_correctly(self):
        self.assertEqual(gc._format_window_bound(datetime(2026, 3, 5, 9, 8, 7, tzinfo=UTC)), "2026-03-05T09:08:07Z")

    def test_window_params_zero_date_bound_is_zero_padded(self):
        window = gc.Window(start=datetime(1, 1, 1, tzinfo=UTC), end=datetime(1, 1, 1, tzinfo=UTC))
        params = window.params("a", "b")
        self.assertEqual(params["a"], "0001-01-01T00:00:00Z")
        self.assertEqual(params["b"], "0001-01-01T23:59:59Z")


class WindowFilterTests(unittest.TestCase):
    """Bug fixes: deployments/coverage were never window-filtered upstream
    even though the endpoints support it (SPEC.md GAPS §11.4), and the
    window's END bound must apply end-of-day internally (B2 CONTRACT: raw
    sprint end in, end-of-day applied internally) rather than truncating the
    final day."""

    def test_deployments_apply_window_as_finished_after_before(self):
        client = _client()
        captured = {}

        def fake_do(op, path, params=None):
            captured["params"] = params
            return b"[]", {}

        client._do = fake_do
        window = gc.Window(start=datetime(2026, 1, 1, tzinfo=UTC), end=datetime(2026, 1, 31, tzinfo=UTC))
        client.deployments("group/project", 42, window=window)
        self.assertEqual(captured["params"]["finished_after"], "2026-01-01T00:00:00Z")
        self.assertEqual(captured["params"]["finished_before"], "2026-01-31T23:59:59Z")
        self.assertNotIn("updated_after", captured["params"])
        self.assertNotIn("updated_before", captured["params"])
        # Regression (patch release): GitLab requires order_by=finished_at
        # alongside the finished_* filter — see DeploymentsFinishedAtSortRequirementTests.
        self.assertEqual(captured["params"]["order_by"], "finished_at")
        self.assertIn("sort", captured["params"])

    def test_deployments_without_window_sends_no_date_params(self):
        client = _client()
        captured = {}

        def fake_do(op, path, params=None):
            captured["params"] = params
            return b"[]", {}

        client._do = fake_do
        client.deployments("group/project", 42, window=None)
        self.assertNotIn("finished_after", captured["params"])
        self.assertNotIn("finished_before", captured["params"])
        # order_by/sort are meaningless (and per GitLab's docs, not
        # required) without the finished_* filter that demands them.
        self.assertNotIn("order_by", captured["params"])
        self.assertNotIn("sort", captured["params"])

    def test_coverage_applies_window(self):
        client = _client()
        captured = {}

        def fake_get_json(op, path, params=None):
            captured["params"] = params
            return []

        client._get_json = fake_get_json
        window = gc.Window(start=datetime(2026, 2, 1, tzinfo=UTC), end=datetime(2026, 2, 14, tzinfo=UTC))
        client.coverage("group/project", 42, window=window)
        self.assertEqual(captured["params"]["updated_after"], "2026-02-01T00:00:00Z")
        self.assertEqual(captured["params"]["updated_before"], "2026-02-14T23:59:59Z")

    def test_mr_list_applies_window_as_created_after_before(self):
        client = _client()
        captured = {}

        def fake_do(op, path, params=None):
            captured["params"] = params
            return b"[]", {}

        client._do = fake_do
        window = gc.Window(start=datetime(2026, 3, 1, tzinfo=UTC), end=datetime(2026, 3, 15, tzinfo=UTC))
        client._mr_list(1, "alice", "merged", window)
        self.assertEqual(captured["params"]["created_after"], "2026-03-01T00:00:00Z")
        self.assertEqual(captured["params"]["created_before"], "2026-03-15T23:59:59Z")

    def test_pipeline_list_applies_window_as_updated_after_before(self):
        client = _client()
        captured = {}

        def fake_do(op, path, params=None):
            captured["params"] = params
            return b"[]", {}

        client._do = fake_do
        window = gc.Window(start=datetime(2026, 4, 1, tzinfo=UTC), end=datetime(2026, 4, 10, tzinfo=UTC))
        client._pipeline_list(1, window)
        self.assertEqual(captured["params"]["updated_after"], "2026-04-01T00:00:00Z")
        self.assertEqual(captured["params"]["updated_before"], "2026-04-10T23:59:59Z")

    def test_window_end_already_at_end_of_day_is_unaffected(self):
        window = gc.Window(start=datetime(2026, 1, 1, tzinfo=UTC), end=datetime(2026, 1, 31, 23, 59, 59, tzinfo=UTC))
        params = window.params("a", "b")
        self.assertEqual(params["b"], "2026-01-31T23:59:59Z")

    def test_fetch_team_data_exposes_window_applied_flag(self):
        client = _client()
        client.project_id = lambda path: 1
        client.merge_requests = lambda *a, **kw: []
        client.pipelines = lambda *a, **kw: []
        client.deployments = lambda *a, **kw: []
        client.coverage = lambda *a, **kw: []

        result_no_window = gc.fetch_team_data(client, projects=["g/p"], employees=["alice"], window=None)
        self.assertFalse(result_no_window["window_applied"]["deployments"])
        self.assertFalse(result_no_window["window_applied"]["coverage"])

        window = gc.Window(start=datetime(2026, 1, 1, tzinfo=UTC), end=datetime(2026, 1, 31, tzinfo=UTC))
        result_windowed = gc.fetch_team_data(client, projects=["g/p"], employees=["alice"], window=window)
        self.assertTrue(result_windowed["window_applied"]["deployments"])
        self.assertTrue(result_windowed["window_applied"]["coverage"])


class DeploymentsFinishedAtSortRequirementTests(unittest.TestCase):
    """Regression, patch release: a live GitLab 19.0 server rejected our
    deployments request with

        {"message":"400 Bad request - `finished_at` filter requires
        `finished_at` sort."}

    GitLab's own docs (https://docs.gitlab.com/api/deployments/, "List
    project deployments") state: "When using finished_before or
    finished_after, you should specify the order_by to be finished_at and
    status should be success" — worded as "should", but the live server
    enforces the ordering half as a hard requirement. `status=success` is
    NOT added (see deployments()'s docstring: it would silently drop every
    failed deployment, breaking deploy_success_rate_pct)."""

    def _fake_attempt_enforcing_gitlab_rule(self, url):
        """Reproduces the exact rejection a real GitLab 19.0 server
        returned: 400 if a finished_* filter is present without
        order_by=finished_at."""
        if ("finished_after=" in url or "finished_before=" in url) and "order_by=finished_at" not in url:
            body = b'{"message":"400 Bad request - `finished_at` filter requires `finished_at` sort."}'
            return body, 400, {}, None
        return b"[]", 200, {}, None

    def test_current_client_request_does_not_trip_the_live_400_rule(self):
        client = _client()
        client._attempt = self._fake_attempt_enforcing_gitlab_rule
        window = gc.Window(start=datetime(2026, 1, 1, tzinfo=UTC), end=datetime(2026, 1, 31, tzinfo=UTC))
        # Must not raise: deployments() always sends order_by=finished_at
        # alongside finished_after/finished_before now.
        result = client.deployments("group/project", 42, window=window)
        self.assertEqual(result, [])

    def test_fake_transport_is_not_a_tautology_it_really_rejects_the_old_shape(self):
        """Sanity check on the fake itself: a request carrying the
        finished_* filter WITHOUT order_by=finished_at (the pre-fix shape)
        must still be rejected by the same fake, proving the previous test
        passed because of the fix, not because the fake always accepts."""
        client = _client()
        client._attempt = self._fake_attempt_enforcing_gitlab_rule
        with self.assertRaises(gc.GitLabError) as ctx:
            client._get_paginated(
                "ListDeployments",
                "/api/v4/projects/42/deployments",
                {"finished_after": "2026-01-01T00:00:00Z", "finished_before": "2026-01-31T23:59:59Z"},
                per_page=gc.DEPLOYMENT_PAGE_SIZE,
            )
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("finished_at", str(ctx.exception))


class MissingDiffStatsTests(unittest.TestCase):
    """Bug fix: additions/deletions/commits used to be read straight off the
    LIST response and silently reported as 0 when GitLab omitted them
    (SPEC.md GAPS §11.2). This client falls back to the detail endpoint /
    a dedicated commits fetch, and only ever reports an explicit
    unavailable flag — never a fabricated 0."""

    def _mr(self, **overrides):
        base = {
            "iid": 7,
            "created_at": "2026-01-01T00:00:00Z",
            "merged_at": "2026-01-02T00:00:00Z",
            "title": "no jira key here",
            "description": "",
            "author": {"username": "alice"},
            "state": "merged",
            "web_url": "https://x",
        }
        base.update(overrides)
        return base

    def test_missing_everywhere_marks_unavailable_not_zero(self):
        client = _client()
        client._mr_detail = lambda pid, iid: {}
        client._mr_commit_count = lambda pid, iid: None
        record = client._build_mr_record("group/project", 42, self._mr())

        self.assertIsNone(record["additions"])
        self.assertIsNone(record["deletions"])
        self.assertFalse(record["diff_stats_available"])
        self.assertIsNone(record["changes_count"])
        self.assertFalse(record["changes_count_available"])
        self.assertIsNone(record["commits_count"])
        self.assertFalse(record["commits_count_available"])

    def test_detail_endpoint_fills_in_missing_stats(self):
        client = _client()
        client._mr_detail = lambda pid, iid: {"additions": 10, "deletions": 4, "changes_count": "3", "commits": 2}
        client._mr_commit_count = lambda pid, iid: 999  # must not be needed: detail already had commits
        record = client._build_mr_record("group/project", 42, self._mr())

        self.assertEqual(record["additions"], 10)
        self.assertEqual(record["deletions"], 4)
        self.assertTrue(record["diff_stats_available"])
        self.assertEqual(record["changes_count"], 3)
        self.assertTrue(record["changes_count_available"])
        self.assertEqual(record["commits_count"], 2)
        self.assertTrue(record["commits_count_available"])

    def test_dedicated_commits_endpoint_used_when_detail_lacks_it_too(self):
        client = _client()
        client._mr_detail = lambda pid, iid: {"additions": 1, "deletions": 1, "changes_count": 1}
        client._mr_commit_count = lambda pid, iid: 5
        record = client._build_mr_record("group/project", 42, self._mr())
        self.assertEqual(record["commits_count"], 5)
        self.assertTrue(record["commits_count_available"])

    def test_list_row_values_used_without_any_extra_fetch(self):
        client = _client()

        def boom(*a, **kw):
            raise AssertionError("should not fetch detail/commits when the list row already has everything")

        client._mr_detail = boom
        client._mr_commit_count = boom
        record = client._build_mr_record(
            "group/project", 42, self._mr(additions=5, deletions=1, changes_count=2, commits=4)
        )
        self.assertEqual(record["additions"], 5)
        self.assertEqual(record["deletions"], 1)
        self.assertTrue(record["diff_stats_available"])
        self.assertEqual(record["changes_count"], 2)
        self.assertEqual(record["commits_count"], 4)
        self.assertTrue(record["commits_count_available"])

    def test_jira_key_extracted_from_title_and_description(self):
        client = _client()
        client._mr_detail = lambda pid, iid: {}
        client._mr_commit_count = lambda pid, iid: None
        record = client._build_mr_record(
            "group/project", 42, self._mr(title="fix bug", description="closes PROJ-42 for good")
        )
        self.assertEqual(record["jira_key"], "PROJ-42")

    def test_no_jira_key_is_empty_string_not_none(self):
        client = _client()
        client._mr_detail = lambda pid, iid: {}
        client._mr_commit_count = lambda pid, iid: None
        record = client._build_mr_record("group/project", 42, self._mr())
        self.assertEqual(record["jira_key"], "")


class FetchMrDetailsOptOutTests(unittest.TestCase):
    """Audit finding 2a: fetch_details=False must skip both per-MR fan-outs
    (detail GET, commits GET) entirely, and the affected fields must degrade
    to their existing UNAVAILABLE representation — never a fabricated 0."""

    def _mr(self, **overrides):
        base = {
            "iid": 7,
            "created_at": "2026-01-01T00:00:00Z",
            "merged_at": "2026-01-02T00:00:00Z",
            "title": "",
            "description": "",
            "author": {"username": "alice"},
            "state": "merged",
            "web_url": "https://x",
        }
        base.update(overrides)
        return base

    def test_opt_out_skips_detail_and_commit_fetches_entirely(self):
        client = _client()

        def boom(*a, **kw):
            raise AssertionError("fetch_details=False must not call _mr_detail/_mr_commit_count")

        client._mr_detail = boom
        client._mr_commit_count = boom
        record = client._build_mr_record("g/p", 1, self._mr(), fetch_details=False)
        self.assertIsNone(record["additions"])
        self.assertIsNone(record["deletions"])
        self.assertFalse(record["diff_stats_available"])
        self.assertIsNone(record["commits_count"])
        self.assertFalse(record["commits_count_available"])
        self.assertIsNone(record["changes_count"])
        self.assertFalse(record["changes_count_available"])

    def test_opt_out_still_uses_list_row_values_when_present(self):
        client = _client()

        def boom(*a, **kw):
            raise AssertionError("list row already had everything; must not fetch")

        client._mr_detail = boom
        client._mr_commit_count = boom
        record = client._build_mr_record(
            "g/p", 1, self._mr(additions=5, deletions=1, changes_count=2, commits=4), fetch_details=False
        )
        self.assertEqual(record["additions"], 5)
        self.assertTrue(record["diff_stats_available"])
        self.assertEqual(record["commits_count"], 4)
        self.assertTrue(record["commits_count_available"])

    def test_default_still_fetches_details(self):
        client = _client()
        client._mr_detail = lambda pid, iid: {"additions": 1, "deletions": 1, "changes_count": 1, "commits": 1}
        client._mr_commit_count = lambda pid, iid: 1
        record = client._build_mr_record("g/p", 1, self._mr())  # fetch_details defaults to True
        self.assertTrue(record["diff_stats_available"])
        self.assertTrue(record["commits_count_available"])

    def test_merge_requests_threads_the_flag_through(self):
        client = _client()
        client._mr_list = lambda project_id, author, state, window: [self._mr()]

        captured = {}

        def fake_build(project_path, project_id, mr, *, fetch_details=True):
            captured["fetch_details"] = fetch_details
            return {"author": "alice"}

        client._build_mr_record = fake_build
        client.merge_requests("g/p", 1, ["alice"], states=("merged",), fetch_mr_details=False)
        self.assertFalse(captured["fetch_details"])


class MrCycleTimeBuildTests(unittest.TestCase):
    """`_build_mr_record`'s cycle time (negative-delta -> None,
    round(sec/3600, 2)) had zero direct coverage."""

    def _mr(self, **overrides):
        base = {
            "iid": 1,
            "created_at": "2026-01-01T00:00:00Z",
            "merged_at": "2026-01-01T01:30:00Z",
            "title": "",
            "description": "",
            "author": {"username": "alice"},
            "state": "merged",
            "web_url": "https://x",
        }
        base.update(overrides)
        return base

    def test_positive_delta_rounds_to_two_decimals(self):
        client = _client()
        client._mr_detail = lambda pid, iid: {}
        client._mr_commit_count = lambda pid, iid: None
        record = client._build_mr_record("g/p", 1, self._mr())
        self.assertEqual(record["cycle_time_seconds"], 5400)
        self.assertEqual(record["cycle_time_hours"], 1.5)

    def test_negative_delta_yields_none(self):
        client = _client()
        client._mr_detail = lambda pid, iid: {}
        client._mr_commit_count = lambda pid, iid: None
        record = client._build_mr_record(
            "g/p", 1, self._mr(created_at="2026-01-02T00:00:00Z", merged_at="2026-01-01T00:00:00Z")
        )
        self.assertIsNone(record["cycle_time_seconds"])
        self.assertIsNone(record["cycle_time_hours"])

    def test_missing_merged_at_falls_back_to_closed_at(self):
        client = _client()
        client._mr_detail = lambda pid, iid: {}
        client._mr_commit_count = lambda pid, iid: None
        record = client._build_mr_record(
            "g/p", 1, self._mr(merged_at=None, closed_at="2026-01-01T12:00:00Z", state="closed")
        )
        self.assertEqual(record["cycle_time_hours"], 12.0)


class ChangesCountCapTests(unittest.TestCase):
    """m1: GitLab returns the literal string "1000+" when changes_count is
    capped — that's a KNOWN >=1000, not an unknown value."""

    def _mr(self, **overrides):
        base = {
            "iid": 1,
            "created_at": "2026-01-01T00:00:00Z",
            "merged_at": "2026-01-02T00:00:00Z",
            "title": "",
            "description": "",
            "author": {"username": "alice"},
            "state": "merged",
            "web_url": "https://x",
        }
        base.update(overrides)
        return base

    def test_1000_plus_parses_as_1000_and_sets_capped_flag(self):
        client = _client()
        client._mr_detail = lambda pid, iid: {}
        client._mr_commit_count = lambda pid, iid: None
        record = client._build_mr_record("g/p", 1, self._mr(changes_count="1000+"))
        self.assertEqual(record["changes_count"], 1000)
        self.assertTrue(record["changes_count_available"])
        self.assertTrue(record["changes_count_capped"])

    def test_normal_changes_count_is_not_capped(self):
        client = _client()
        client._mr_detail = lambda pid, iid: {}
        client._mr_commit_count = lambda pid, iid: None
        record = client._build_mr_record("g/p", 1, self._mr(changes_count=12))
        self.assertEqual(record["changes_count"], 12)
        self.assertFalse(record["changes_count_capped"])

    def test_capped_value_found_via_detail_fallback(self):
        client = _client()
        client._mr_detail = lambda pid, iid: {"changes_count": "1000+"}
        client._mr_commit_count = lambda pid, iid: None
        record = client._build_mr_record("g/p", 1, self._mr(changes_count=None))
        self.assertEqual(record["changes_count"], 1000)
        self.assertTrue(record["changes_count_capped"])


class JiraKeyCandidateTests(unittest.TestCase):
    """m5: the plain regex also matches non-Jira "WORD-NUMBER" tokens
    ("UTF-8", "CVE-2024"), and never searched source_branch."""

    def _mr(self, **overrides):
        base = {
            "iid": 7,
            "created_at": "2026-01-01T00:00:00Z",
            "merged_at": "2026-01-02T00:00:00Z",
            "title": "",
            "description": "",
            "author": {"username": "alice"},
            "state": "merged",
            "web_url": "https://x",
        }
        base.update(overrides)
        return base

    def test_false_positive_prefixes_excluded_from_best_guess(self):
        client = _client()
        client._mr_detail = lambda pid, iid: {}
        client._mr_commit_count = lambda pid, iid: None
        record = client._build_mr_record("g/p", 1, self._mr(title="Bump encoding to UTF-8"))
        self.assertEqual(record["jira_key"], "")
        self.assertIn("UTF-8", record["jira_key_candidates"])

    def test_real_key_found_after_a_false_positive_candidate(self):
        client = _client()
        client._mr_detail = lambda pid, iid: {}
        client._mr_commit_count = lambda pid, iid: None
        record = client._build_mr_record("g/p", 1, self._mr(title="Bump to UTF-8, fixes PROJ-9"))
        self.assertEqual(record["jira_key"], "PROJ-9")
        self.assertEqual(record["jira_key_candidates"], ["UTF-8", "PROJ-9"])

    def test_source_branch_is_searched_and_recorded(self):
        client = _client()
        client._mr_detail = lambda pid, iid: {}
        client._mr_commit_count = lambda pid, iid: None
        record = client._build_mr_record("g/p", 1, self._mr(source_branch="feature/PROJ-99-something"))
        self.assertEqual(record["source_branch"], "feature/PROJ-99-something")
        self.assertEqual(record["jira_key"], "PROJ-99")

    def test_missing_source_branch_defaults_to_empty_string(self):
        client = _client()
        client._mr_detail = lambda pid, iid: {}
        client._mr_commit_count = lambda pid, iid: None
        record = client._build_mr_record("g/p", 1, self._mr())
        self.assertEqual(record["source_branch"], "")


class ReadOnlyGuaranteeTests(unittest.TestCase):
    """There is no `method`/`data` parameter anywhere in this client — every
    request it can build is a GET by construction."""

    def test_request_built_by_the_client_is_always_get(self):
        captured = {}

        class FakeResp:
            status = 200
            headers = {}

            def read(self):
                return b"{}"

        class FakeOpener:
            def open(self, req, timeout=None):
                captured["method"] = req.get_method()
                captured["has_data"] = req.data is not None
                return FakeResp()

        client = gc.GitLabClient(
            "https://gitlab.example.com", "tok", opener=FakeOpener(), sleep=lambda _d: None
        )
        client._get_json("Op", "/api/v4/projects/1")
        self.assertEqual(captured["method"], "GET")
        self.assertFalse(captured["has_data"])


class ProxyOptOutTests(unittest.TestCase):
    """trust_env_proxy (default True, unchanged behavior): the opener keeps
    urllib's default ProxyHandler, so http_proxy/https_proxy from the
    environment are honored — often desired on a corporate network.
    trust_env_proxy=False builds the opener without a working proxy handler,
    for a caller that wants every request to go direct regardless of the
    environment. Mirrors JiraClient's identical parameter/tests
    (test_jira_client.py's ProxyOptOutTests) for symmetry."""

    def _proxy_handler(self, client):
        return next((h for h in client._opener.handlers if isinstance(h, urllib.request.ProxyHandler)), None)

    def test_default_keeps_the_environment_proxy_handler(self):
        with unittest.mock.patch.dict("os.environ", {"https_proxy": "http://proxy.example.com:8080"}):
            client = gc.GitLabClient("https://gitlab.example.com", "tok", sleep=lambda _d: None)
            handler = self._proxy_handler(client)
            self.assertIsNotNone(handler)
            self.assertEqual(handler.proxies.get("https"), "http://proxy.example.com:8080")

    def test_opt_out_ignores_the_environment_proxy(self):
        with unittest.mock.patch.dict("os.environ", {"https_proxy": "http://proxy.example.com:8080"}):
            client = gc.GitLabClient(
                "https://gitlab.example.com", "tok", sleep=lambda _d: None, trust_env_proxy=False
            )
            handler = self._proxy_handler(client)
            # No active proxy handler at all -> every request goes direct.
            self.assertIsNone(handler)

    def test_explicit_opener_bypasses_the_proxy_setting_entirely(self):
        # A caller-supplied opener (as ReadOnlyGuaranteeTests uses) is used
        # as-is; trust_env_proxy is irrelevant once `opener` is given.
        sentinel = object()
        client = gc.GitLabClient("https://gitlab.example.com", "tok", opener=sentinel, trust_env_proxy=False)
        self.assertIs(client._opener, sentinel)


class MergeRequestsFanOutTests(unittest.TestCase):
    def test_fans_out_over_authors_and_states_and_preserves_all_results(self):
        client = _client(max_workers=2)

        def fake_mr_list(project_id, author, state, window):
            return [{"iid": f"{author}-{state}"}]

        client._mr_list = fake_mr_list
        client._build_mr_record = lambda project_path, project_id, mr, **kw: {"author": mr["iid"]}

        records = client.merge_requests("g/p", 1, ["alice", "bob"], states=("merged", "closed"))
        self.assertEqual(
            sorted(r["author"] for r in records),
            sorted(["alice-merged", "alice-closed", "bob-merged", "bob-closed"]),
        )

    def test_empty_author_list_returns_empty(self):
        client = _client()
        self.assertEqual(client.merge_requests("g/p", 1, []), [])


class MergeRequestsFaultToleranceTests(unittest.TestCase):
    """m3: one failing (author,state) pair used to kill the whole project's
    MR collection. AUTH_FAILED must still be fatal (B1)."""

    def test_non_auth_failure_for_one_pair_does_not_abort_others(self):
        client = _client()

        def fake_mr_list(project_id, author, state, window):
            if author == "ghost":
                raise gc.GitLabError("ListMergeRequests", "user not found", code="NOT_FOUND", status_code=404)
            return [{"iid": f"{author}-{state}"}]

        client._mr_list = fake_mr_list
        client._build_mr_record = lambda project_path, project_id, mr, **kw: {"author": mr["iid"]}
        errors = []
        records = client.merge_requests("g/p", 1, ["alice", "ghost"], states=("merged",), errors=errors)
        self.assertEqual([r["author"] for r in records], ["alice-merged"])
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["author"], "ghost")
        self.assertEqual(errors[0]["code"], "NOT_FOUND")

    def test_auth_failed_for_one_pair_propagates(self):
        client = _client()

        def fake_mr_list(project_id, author, state, window):
            raise gc.GitLabError("ListMergeRequests", "bad token", code="AUTH_FAILED", status_code=401)

        client._mr_list = fake_mr_list
        client._build_mr_record = lambda project_path, project_id, mr, **kw: {}
        with self.assertRaises(gc.GitLabError) as ctx:
            client.merge_requests("g/p", 1, ["alice"], states=("merged",))
        self.assertEqual(ctx.exception.code, "AUTH_FAILED")

    def test_errors_param_defaults_to_none_and_is_optional(self):
        client = _client()

        def fake_mr_list(project_id, author, state, window):
            raise gc.GitLabError("ListMergeRequests", "gone", code="NOT_FOUND", status_code=404)

        client._mr_list = fake_mr_list
        client._build_mr_record = lambda project_path, project_id, mr, **kw: {}
        records = client.merge_requests("g/p", 1, ["ghost"], states=("merged",))  # no errors= given
        self.assertEqual(records, [])


class ProjectIdErrorPathTests(unittest.TestCase):
    """B1: project_id() used to swallow AUTH_FAILED and NOT_FOUND alike."""

    def test_success_returns_id(self):
        client = _client()
        client._attempt = lambda url: (json.dumps({"id": 42}).encode(), 200, {}, None)
        self.assertEqual(client.project_id("group/project"), 42)

    def test_auth_failed_propagates(self):
        client = _client()
        client._attempt = lambda url: (b"unauthorized", 401, {}, None)
        with self.assertRaises(gc.GitLabError) as ctx:
            client.project_id("group/project")
        self.assertEqual(ctx.exception.code, "AUTH_FAILED")

    def test_not_found_propagates_with_code(self):
        client = _client()
        client._attempt = lambda url: (b"nope", 404, {}, None)
        with self.assertRaises(gc.GitLabError) as ctx:
            client.project_id("group/missing")
        self.assertEqual(ctx.exception.code, "NOT_FOUND")


class CurrentUserTests(unittest.TestCase):
    def test_returns_user_on_success(self):
        client = _client()
        client._attempt = lambda url: (json.dumps({"id": 7, "username": "alice"}).encode(), 200, {}, None)
        self.assertEqual(client.current_user(), {"id": 7, "username": "alice"})

    def test_auth_failed_propagates(self):
        client = _client()
        client._attempt = lambda url: (b"unauthorized", 401, {}, None)
        with self.assertRaises(gc.GitLabError) as ctx:
            client.current_user()
        self.assertEqual(ctx.exception.code, "AUTH_FAILED")

    def test_unreachable_propagates(self):
        client = _client(max_retries=0)
        client._attempt = lambda url: (None, 0, {}, OSError("connection refused"))
        with self.assertRaises(gc.GitLabError) as ctx:
            client.current_user()
        self.assertEqual(ctx.exception.code, "UNREACHABLE")



class CoverageRecordTests(unittest.TestCase):
    def test_builds_record_from_pipeline_and_detail(self):
        client = _client()

        def fake_get_json(op, path, params=None):
            if op == "ListPipelinesForCoverage":
                return [{"id": 5, "ref": "main", "created_at": "2026-01-01T00:00:00Z"}]
            return {"coverage": "87.5"}

        client._get_json = fake_get_json
        records = client.coverage("group/project", 1)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["pipeline_id"], 5)
        self.assertEqual(records[0]["coverage"], 87.5)

    def test_non_list_body_raises_instead_of_keyerror(self):
        """m4: coverage() used to raise a bare KeyError (`dict[:1]`) on a
        non-list body; `_get_paginated` already handled this correctly."""
        client = _client()
        client._get_json = lambda op, path, params=None: {"message": "server error"}
        with self.assertRaises(gc.GitLabError):
            client.coverage("group/project", 1)

    def test_empty_list_yields_no_records(self):
        client = _client()
        client._get_json = lambda op, path, params=None: []
        self.assertEqual(client.coverage("group/project", 1), [])

    def test_pipeline_detail_fetch_failure_is_skipped_not_fatal(self):
        client = _client()

        def fake_get_json(op, path, params=None):
            if op == "ListPipelinesForCoverage":
                return [{"id": 5, "ref": "main", "created_at": "2026-01-01T00:00:00Z"}]
            raise gc.GitLabError("GetPipeline", "gone", code="NOT_FOUND", status_code=404)

        client._get_json = fake_get_json
        self.assertEqual(client.coverage("group/project", 1), [])


class PipelineJobUserTests(unittest.TestCase):
    """M4: this used to feed JOBS_PAGE_SIZE=1 into `_get_paginated`, walking
    every job page just to keep jobs[0]. It must now be a single request."""

    def test_single_request_used_not_full_pagination(self):
        client = _client()
        calls = []

        def fake_get_json(op, path, params=None):
            calls.append((op, path, params))
            return [{"user": {"username": "alice", "name": "Alice A"}}]

        client._get_json = fake_get_json
        username, name = client._pipeline_job_user(1, 42)
        self.assertEqual(username, "alice")
        self.assertEqual(name, "Alice A")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2], {"page": 1, "per_page": 1})

    def test_no_jobs_returns_empty_strings(self):
        client = _client()
        client._get_json = lambda op, path, params=None: []
        self.assertEqual(client._pipeline_job_user(1, 42), ("", ""))

    def test_error_returns_empty_strings_not_raise(self):
        client = _client()

        def boom(op, path, params=None):
            raise gc.GitLabError("ListPipelineJobs", "gone", code="NOT_FOUND", status_code=404)

        client._get_json = boom
        self.assertEqual(client._pipeline_job_user(1, 42), ("", ""))


class FetchPipelineUserOptOutTests(unittest.TestCase):
    """Audit finding 2a: fetch_pipeline_user=False must skip the per-
    pipeline jobs fan-out entirely, and the skip must be distinguishable
    (user_lookup_available=False) from "looked up, genuinely nobody"."""

    def test_opt_out_skips_the_fan_out_entirely(self):
        client = _client()

        def boom(*a, **kw):
            raise AssertionError("fetch_pipeline_user=False must not call _pipeline_job_user")

        client._pipeline_list = lambda project_id, window: [{"id": 1, "status": "success"}]
        client._pipeline_job_user = boom
        records = client.pipelines("g/p", 1, fetch_pipeline_user=False)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["user_username"], "")
        self.assertEqual(records[0]["user_name"], "")
        self.assertFalse(records[0]["user_lookup_available"])

    def test_default_still_looks_up_and_marks_available(self):
        client = _client()
        client._pipeline_list = lambda project_id, window: [{"id": 1, "status": "success"}]
        client._pipeline_job_user = lambda project_id, pipeline_id: ("alice", "Alice A")
        records = client.pipelines("g/p", 1)  # fetch_pipeline_user defaults to True
        self.assertEqual(records[0]["user_username"], "alice")
        self.assertTrue(records[0]["user_lookup_available"])

    def test_lookup_attempted_but_genuinely_empty_still_marks_available(self):
        # Distinguishes "we tried and found nobody" from "we never tried".
        client = _client()
        client._pipeline_list = lambda project_id, window: [{"id": 1, "status": "success"}]
        client._pipeline_job_user = lambda project_id, pipeline_id: ("", "")
        records = client.pipelines("g/p", 1)
        self.assertEqual(records[0]["user_username"], "")
        self.assertTrue(records[0]["user_lookup_available"])

    def test_fetch_team_data_threads_the_flag_through(self):
        client = _client()
        client.project_id = lambda path: 1
        client.merge_requests = lambda path, pid, employees, **kw: []
        client.deployments = lambda path, pid, window=None: []
        client.coverage = lambda path, pid, window=None: []

        captured = {}

        def fake_pipelines(path, pid, window=None, fetch_pipeline_user=True):
            captured["fetch_pipeline_user"] = fetch_pipeline_user
            return []

        client.pipelines = fake_pipelines
        gc.fetch_team_data(client, projects=["g/a"], employees=["alice"], fetch_pipeline_user=False)
        self.assertFalse(captured["fetch_pipeline_user"])


class FetchTeamDataTests(unittest.TestCase):
    def test_concatenates_across_projects(self):
        client = _client()
        client.project_id = lambda path: {"g/a": 1, "g/b": 2}[path]
        client.merge_requests = lambda path, pid, employees, **kw: [{"project": path}]
        client.pipelines = lambda path, pid, **kw: [{"project": path}]
        client.deployments = lambda path, pid, window=None: [{"project": path}]
        client.coverage = lambda path, pid, window=None: [{"project": path}]
        result = gc.fetch_team_data(client, projects=["g/a", "g/b"], employees=["alice"])
        self.assertEqual(len(result["merge_requests"]), 2)
        self.assertEqual({r["project"] for r in result["merge_requests"]}, {"g/a", "g/b"})
        self.assertEqual(len(result["pipelines"]), 2)
        self.assertEqual(len(result["deployments"]), 2)
        self.assertEqual(len(result["coverage"]), 2)
        self.assertEqual(result["skipped_projects"], [])

    def test_auth_failed_project_id_propagates_and_fails_whole_run(self):
        client = _client()

        def boom(path):
            raise gc.GitLabError("ProjectID", "bad token", code="AUTH_FAILED", status_code=401)

        client.project_id = boom
        with self.assertRaises(gc.GitLabError) as ctx:
            gc.fetch_team_data(client, projects=["g/a"], employees=["alice"])
        self.assertEqual(ctx.exception.code, "AUTH_FAILED")

    def test_not_found_project_is_skipped_not_fatal(self):
        client = _client()

        def resolve(path):
            if path == "g/missing":
                raise gc.GitLabError("ProjectID", "no such project", code="NOT_FOUND", status_code=404)
            return 1

        client.project_id = resolve
        client.merge_requests = lambda path, pid, employees, **kw: []
        client.pipelines = lambda path, pid, **kw: []
        client.deployments = lambda path, pid, window=None: []
        client.coverage = lambda path, pid, window=None: []
        result = gc.fetch_team_data(client, projects=["g/missing", "g/ok"], employees=["alice"])
        self.assertEqual(
            result["skipped_projects"],
            [{"project": "g/missing", "code": "NOT_FOUND", "message": "no such project"}],
        )

    def test_none_project_id_is_also_skipped_not_fatal(self):
        client = _client()
        client.project_id = lambda path: None
        result = gc.fetch_team_data(client, projects=["g/weird"], employees=["alice"])
        self.assertEqual(result["skipped_projects"], [{"project": "g/weird", "code": "NOT_FOUND", "message": "project id not returned"}])
        self.assertEqual(result["merge_requests"], [])

    def test_mr_fetch_errors_collected_not_fatal(self):
        client = _client()
        client.project_id = lambda path: 1

        def fake_merge_requests(path, pid, employees, window=None, errors=None, **kw):
            if errors is not None:
                errors.append(
                    {"project": path, "author": "renamed-user", "state": "merged", "code": "NOT_FOUND", "message": "user not found"}
                )
            return [{"project": path, "author": "alice"}]

        client.merge_requests = fake_merge_requests
        client.pipelines = lambda path, pid, **kw: []
        client.deployments = lambda path, pid, window=None: []
        client.coverage = lambda path, pid, window=None: []
        result = gc.fetch_team_data(client, projects=["g/a"], employees=["alice", "renamed-user"])
        self.assertEqual(len(result["merge_requests"]), 1)
        self.assertEqual(len(result["mr_fetch_errors"]), 1)
        self.assertEqual(result["mr_fetch_errors"][0]["author"], "renamed-user")


class _FakeResp:
    def __init__(self, status, body):
        self.status = status
        self.headers = {}
        self._body = body

    def read(self):
        return json.dumps(self._body).encode()


class _FakeOpener:
    """A fake urllib opener so the REAL GitLabClient._attempt() executes
    (and increments the real request counter) — request-count tests must
    exercise the actual counting code, not a stubbed _attempt that skips
    over it."""

    def __init__(self, responder):
        self._responder = responder

    def open(self, req, timeout=None):
        status, body = self._responder(req.full_url)
        return _FakeResp(status, body)


def _fixture_responder(url):
    if "/merge_requests" in url:
        return 200, []
    if "/pipelines" in url:
        return 200, []
    if "/deployments" in url:
        return 200, []
    return 200, {"id": 1}  # the plain project_id lookup


class RequestCountTests(unittest.TestCase):
    """Audit finding 2b: fetch_team_data() must return an accurate count of
    actual HTTP round trips made during that call, scoped to just that call
    (never cumulative across a client reused for multiple runs)."""

    def test_request_count_matches_a_known_fixture_run(self):
        client = gc.GitLabClient(
            "https://gitlab.example.com", "tok", opener=_FakeOpener(_fixture_responder), sleep=lambda d: None
        )
        result = gc.fetch_team_data(client, projects=["g/p"], employees=["alice"])
        # 1 project_id + 2 MR-state fetches (merged, closed) + 1 pipeline
        # list (empty -> no per-pipeline fan-out) + 1 deployments list +
        # 1 coverage list (empty -> no GetPipeline follow-up) = 6 requests.
        self.assertEqual(result["request_count"], 6)
        self.assertEqual(client.request_count, 6)

    def test_request_count_is_scoped_to_one_call_not_cumulative(self):
        client = gc.GitLabClient(
            "https://gitlab.example.com", "tok", opener=_FakeOpener(_fixture_responder), sleep=lambda d: None
        )
        first = gc.fetch_team_data(client, projects=["g/p"], employees=["alice"])
        second = gc.fetch_team_data(client, projects=["g/p"], employees=["alice"])
        self.assertEqual(first["request_count"], second["request_count"])  # not doubled on the 2nd run
        self.assertEqual(client.request_count, first["request_count"] + second["request_count"])

    def test_request_count_includes_retries(self):
        calls = {"n": 0}

        def flaky_then_ok(url):
            calls["n"] += 1
            if calls["n"] < 3:
                return 503, "down"
            return 200, {"id": 1}

        client = gc.GitLabClient(
            "https://gitlab.example.com", "tok", opener=_FakeOpener(flaky_then_ok), sleep=lambda d: None
        )
        client.project_id("g/p")
        self.assertEqual(client.request_count, 3)  # 2 failed attempts + 1 success, all real HTTP round trips


if __name__ == "__main__":
    unittest.main()
