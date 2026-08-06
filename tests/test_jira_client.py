import _pathfix  # noqa: F401

import json
import socket
import unittest
from datetime import timezone

from jira_metrics import jira_client as jc
from jira_metrics import model


def _issue_dto(key, issue_type="Story", changelog=None):
    return {
        "key": key,
        "fields": {
            "issuetype": {"name": issue_type},
            "created": "2026-01-01T00:00:00.000+0000",
            "status": {"name": "To Do", "id": "1", "statusCategory": {"key": "new"}},
            "assignee": None,
            "labels": [],
        },
        "changelog": changelog or {"histories": [], "maxResults": 0, "total": 0},
    }


class ParseJiraTimeTests(unittest.TestCase):
    """All 5 Go time layouts collapse to one regex; result is always UTC — SPEC §2.6."""

    def test_millis_offset_no_colon(self):
        t = jc.parse_jira_time("2026-05-11T10:00:00.000+0300")
        self.assertEqual(t.tzinfo, timezone.utc)
        self.assertEqual(t.isoformat(), "2026-05-11T07:00:00+00:00")

    def test_no_millis_offset_no_colon(self):
        t = jc.parse_jira_time("2026-05-11T10:00:00+0300")
        self.assertEqual(t.isoformat(), "2026-05-11T07:00:00+00:00")

    def test_millis_offset_with_colon(self):
        t = jc.parse_jira_time("2026-05-11T10:00:00.500+03:00")
        self.assertEqual(t.hour, 7)
        self.assertEqual(t.microsecond, 500000)

    def test_zulu_suffix(self):
        t = jc.parse_jira_time("2026-05-11T10:00:00Z")
        self.assertEqual(t.isoformat(), "2026-05-11T10:00:00+00:00")

    def test_rfc3339nano(self):
        t = jc.parse_jira_time("2026-05-11T10:00:00.123456789+00:00")
        self.assertEqual(t.microsecond, 123456)  # truncated to microsecond precision

    def test_empty_string_raises(self):
        with self.assertRaises(ValueError):
            jc.parse_jira_time("")

    def test_optional_variant_empty_is_zero_time_sentinel(self):
        # Mirrors Go's parseOptionalJiraTime: empty -> zero time.Time{}, not
        # an error and not None (a future sprint may lack startDate/endDate).
        self.assertEqual(jc.parse_optional_jira_time(""), model.ZERO_TIME)


class SprintFieldParsingTests(unittest.TestCase):
    def test_json_object_form(self):
        entry = jc._parse_sprint_field_entry({"id": 181, "boardId": 24, "state": "CLOSED", "name": "SP-42"})
        self.assertEqual(entry, {"id": 181, "board_id": 24, "state": "closed", "name": "SP-42"})

    def test_greenhopper_tostring_form(self):
        raw = (
            "com.atlassian.greenhopper.service.sprint.Sprint@6dba7c[id=181,rapidViewId=24,"
            "state=CLOSED,name=SP-42 (fix, retest),startDate=2026-05-11T10:00:00.000+03:00,"
            "endDate=2026-05-24T18:00:00.000+03:00,completeDate=2026-05-25T10:12:00.000+03:00,sequence=181,goal=]"
        )
        entry = jc._parse_sprint_field_entry(raw)
        self.assertEqual(entry["id"], 181)
        self.assertEqual(entry["board_id"], 24)
        self.assertEqual(entry["state"], "closed")
        self.assertEqual(entry["name"], "SP-42 (fix, retest)")  # name may itself contain commas

    def test_null_rapid_view_id_falls_back_to_zero(self):
        raw = "com.atlassian.greenhopper...[id=5,rapidViewId=<null>,state=FUTURE,name=X,startDate=...]"
        entry = jc._parse_sprint_field_entry(raw)
        self.assertEqual(entry["board_id"], 0)

    def test_id_list_parsing_skips_non_numeric_tokens(self):
        self.assertEqual(jc._parse_id_list("181, 205, foo, 12"), [181, 205, 12])

    def test_id_list_empty_string(self):
        self.assertEqual(jc._parse_id_list(""), [])
        self.assertEqual(jc._parse_id_list("   "), [])


class DecodeFieldTests(unittest.TestCase):
    def test_numeric_field_variants(self):
        self.assertEqual(jc._decode_numeric_field(5), 5.0)
        self.assertEqual(jc._decode_numeric_field(5.5), 5.5)
        self.assertEqual(jc._decode_numeric_field("3,5"), 3.5)  # comma decimal separator
        self.assertEqual(jc._decode_numeric_field({"value": "8"}), 8.0)
        self.assertEqual(jc._decode_numeric_field(None), 0.0)
        self.assertEqual(jc._decode_numeric_field("not a number"), 0.0)

    def test_string_field_variants(self):
        self.assertEqual(jc._decode_string_field("bare"), "bare")
        self.assertEqual(jc._decode_string_field({"value": "v"}), "v")
        self.assertEqual(jc._decode_string_field({"name": "n"}), "n")
        self.assertEqual(jc._decode_string_field([{"value": "first"}, {"value": "second"}]), "first")
        self.assertEqual(jc._decode_string_field(None), "")

    def test_role_from_labels_case_insensitive(self):
        self.assertEqual(jc._role_from_labels(["backend", "DEV"]), "dev")
        self.assertEqual(jc._role_from_labels(["qa-team", "QA"]), "qa")
        self.assertEqual(jc._role_from_labels(["nothing"]), "")


class RetryAfterParsingTests(unittest.TestCase):
    def test_parses_positive_integer_seconds(self):
        self.assertEqual(jc._parse_retry_after_seconds("30"), 30.0)

    def test_rejects_negative_and_garbage(self):
        self.assertIsNone(jc._parse_retry_after_seconds("-1"))
        self.assertIsNone(jc._parse_retry_after_seconds("soon"))
        self.assertIsNone(jc._parse_retry_after_seconds(""))


class RetryLoopTests(unittest.TestCase):
    """Exercises Client._do's retry/backoff state machine with a fake transport
    (Client._attempt monkeypatched) — no network involved."""

    def _client(self):
        return jc.JiraClient("https://jira.example.com", "tok", sleep=lambda _d: None)

    def test_success_on_first_attempt(self):
        client = self._client()
        client._attempt = lambda url: (b'{"ok":true}', 200, "", None)
        self.assertEqual(client._do("Op", "/x"), b'{"ok":true}')

    def test_retries_5xx_then_succeeds(self):
        client = self._client()
        calls = {"n": 0}

        def fake_attempt(url):
            calls["n"] += 1
            if calls["n"] < 3:
                return b"server error", 503, "", None
            return b"ok", 200, "", None

        client._attempt = fake_attempt
        self.assertEqual(client._do("Op", "/x"), b"ok")
        self.assertEqual(calls["n"], 3)

    def test_gives_up_after_max_retries_and_classifies_unreachable(self):
        client = self._client()
        client._attempt = lambda url: (b"still down", 503, "", None)
        with self.assertRaises(jc.JiraError) as ctx:
            client._do("Op", "/x")
        self.assertEqual(ctx.exception.code, jc.CODE_JIRA_UNREACHABLE)
        self.assertEqual(ctx.exception.status_code, 503)

    def test_401_is_not_retried_and_classified_as_auth_failed(self):
        client = self._client()
        calls = {"n": 0}

        def fake_attempt(url):
            calls["n"] += 1
            return b"unauthorized", 401, "", None

        client._attempt = fake_attempt
        with self.assertRaises(jc.JiraError) as ctx:
            client._do("Op", "/x")
        self.assertEqual(calls["n"], 1)  # 4xx (except 429) never retried
        self.assertEqual(ctx.exception.code, jc.CODE_JIRA_AUTH_FAILED)

    def test_network_error_retries_then_raises_unreachable(self):
        client = self._client()
        client._attempt = lambda url: (None, 0, "", ConnectionError("boom"))
        with self.assertRaises(jc.JiraError) as ctx:
            client._do("Op", "/x")
        self.assertEqual(ctx.exception.code, jc.CODE_JIRA_UNREACHABLE)

    def test_retry_after_header_honored_and_capped(self):
        client = self._client()
        client.max_retry_after = 5.0
        sleeps = []
        client._sleep = lambda d: sleeps.append(d)
        calls = {"n": 0}

        def fake_attempt(url):
            calls["n"] += 1
            if calls["n"] == 1:
                return b"", 429, "9999", None  # far above the 5s cap
            return b"ok", 200, "", None

        client._attempt = fake_attempt
        client._do("Op", "/x")
        self.assertEqual(sleeps[0], 5.0)  # capped at max_retry_after, not the raw header value


class SearchIssuesPaginationTests(unittest.TestCase):
    def _client(self):
        return jc.JiraClient("https://jira.example.com", "tok", sleep=lambda _d: None)

    def test_stops_once_returned_count_reaches_total(self):
        client = self._client()
        page1 = json.dumps({"issues": [_issue_dto(f"P-{i}") for i in range(1, 101)], "total": 101}).encode()
        page2 = json.dumps({"issues": [_issue_dto("P-101")], "total": 101}).encode()
        pages = [page1, page2]
        calls = {"n": 0}

        def fake_attempt(url):
            data = pages[calls["n"]]
            calls["n"] += 1
            return data, 200, "", None

        client._attempt = fake_attempt
        out = client.search_issues("sprint in (1)", ["summary"])
        self.assertEqual([i.key for i in out], [f"P-{i}" for i in range(1, 102)])
        self.assertEqual(calls["n"], 2)

    def test_single_page_stops_immediately(self):
        client = self._client()
        client._attempt = lambda url: (json.dumps({"issues": [_issue_dto("P-1")], "total": 1}).encode(), 200, "", None)
        out = client.search_issues("sprint in (1)", ["summary"])
        self.assertEqual(len(out), 1)

    def test_truncated_embedded_changelog_triggers_full_refetch(self):
        client = self._client()
        truncated_changelog = {
            "histories": [
                {"created": "2026-01-02T00:00:00.000+0000", "items": [
                    {"field": "status", "from": "1", "to": "2", "fromString": "To Do", "toString": "In Progress"},
                ]},
            ],
            "maxResults": 1,
            "total": 5,  # total > maxResults -> truncated
        }
        full_changelog = {
            "changelog": {
                "histories": [
                    {"created": "2026-01-02T00:00:00.000+0000", "items": [
                        {"field": "status", "from": "1", "to": "2", "fromString": "To Do", "toString": "In Progress"},
                    ]},
                    {"created": "2026-01-03T00:00:00.000+0000", "items": [
                        {"field": "status", "from": "2", "to": "3", "fromString": "In Progress", "toString": "Done"},
                    ]},
                ],
            }
        }
        search_response = json.dumps(
            {"issues": [_issue_dto("P-1", changelog=truncated_changelog)], "total": 1}
        ).encode()
        refetch_response = json.dumps(full_changelog).encode()

        def fake_attempt(url):
            if "/rest/api/2/search" in url:
                return search_response, 200, "", None
            if "/rest/api/2/issue/" in url:
                return refetch_response, 200, "", None
            raise AssertionError(f"unexpected url: {url}")

        client._attempt = fake_attempt
        out = client.search_issues("sprint in (1)", ["summary"])
        self.assertEqual(len(out), 1)
        self.assertEqual(len(out[0].changelog), 2)  # full re-fetch, not the truncated 1-entry embedded one


class BoardSprintsPaginationTests(unittest.TestCase):
    def _client(self):
        return jc.JiraClient("https://jira.example.com", "tok", sleep=lambda _d: None)

    def test_stops_at_is_last_true(self):
        client = self._client()
        pages = [
            json.dumps({"values": [{"id": 1, "name": "S1"}, {"id": 2, "name": "S2"}], "isLast": False}).encode(),
            json.dumps({"values": [{"id": 3, "name": "S3"}], "isLast": True}).encode(),
        ]
        calls = {"n": 0}

        def fake_attempt(url):
            data = pages[calls["n"]]
            calls["n"] += 1
            return data, 200, "", None

        client._attempt = fake_attempt
        out = client.board_sprints(1)
        self.assertEqual([s.id for s in out], [1, 2, 3])
        self.assertEqual(calls["n"], 2)

    def test_stops_when_a_page_returns_no_values(self):
        client = self._client()
        client._attempt = lambda url: (json.dumps({"values": [], "isLast": False}).encode(), 200, "", None)
        out = client.board_sprints(1)
        self.assertEqual(out, [])


class PaginationBoundTests(unittest.TestCase):
    """m4: a server that never converges (always isLast=False / total that
    never catches up) must not hang the process forever."""

    def _client(self):
        return jc.JiraClient("https://jira.example.com", "tok", sleep=lambda _d: None)

    def _with_expired_deadline(self, body):
        original = jc.MAX_PAGINATION_SECONDS
        jc.MAX_PAGINATION_SECONDS = -1.0  # deadline is already in the past before the first check
        try:
            body()
        finally:
            jc.MAX_PAGINATION_SECONDS = original

    def test_board_sprints_raises_when_server_never_converges(self):
        client = self._client()
        client._attempt = lambda url: (json.dumps({"values": [{"id": 1, "name": "S"}], "isLast": False}).encode(), 200, "", None)

        def body():
            with self.assertRaises(jc.JiraError) as ctx:
                client.board_sprints(1)
            self.assertEqual(ctx.exception.code, jc.CODE_JIRA_UNREACHABLE)

        self._with_expired_deadline(body)

    def test_search_issues_raises_when_server_never_converges(self):
        client = self._client()
        client._attempt = lambda url: (json.dumps({"issues": [_issue_dto("P-1")], "total": 999999}).encode(), 200, "", None)

        def body():
            with self.assertRaises(jc.JiraError) as ctx:
                client.search_issues("sprint in (1)", ["summary"])
            self.assertEqual(ctx.exception.code, jc.CODE_JIRA_UNREACHABLE)

        self._with_expired_deadline(body)


class SocketTimeoutHandlingTests(unittest.TestCase):
    """m5: on Python 3.9, socket.timeout is not yet a TimeoutError alias — a
    bare read timeout raised straight from resp.read() must still be caught
    inside _attempt(), not just by whatever wraps it."""

    def test_attempt_catches_bare_socket_timeout(self):
        class FakeOpener:
            def open(self, req, timeout=None):
                raise socket.timeout("timed out")

        client = jc.JiraClient("https://jira.example.com", "tok", sleep=lambda _d: None)
        client._opener = FakeOpener()
        data, status, retry_after, err = client._attempt("https://jira.example.com/x")
        self.assertIsNone(data)
        self.assertEqual(status, 0)
        self.assertIsInstance(err, socket.timeout)

    def test_do_retries_a_socket_timeout_like_any_other_transport_error(self):
        client = jc.JiraClient("https://jira.example.com", "tok", sleep=lambda _d: None)
        calls = {"n": 0}

        def fake_attempt(url):
            calls["n"] += 1
            if calls["n"] < 2:
                return None, 0, "", socket.timeout("timed out")
            return b'{"ok":true}', 200, "", None

        client._attempt = fake_attempt
        self.assertEqual(client._do("Op", "/x"), b'{"ok":true}')
        self.assertEqual(calls["n"], 2)


class TokenValidationTests(unittest.TestCase):
    """m1: a token containing embedded CR/LF must never reach an HTTP header
    — http.client's own header validation raises a bare ValueError that
    embeds the value verbatim, which would put the PAT in a traceback."""

    def test_rejects_token_with_embedded_crlf(self):
        with self.assertRaises(ValueError):
            jc.JiraClient("https://jira.example.com", "tok\r\nInjected: header")

    def test_error_message_never_echoes_the_token_value(self):
        secret = "tok\r\nInjected: header"
        try:
            jc.JiraClient("https://jira.example.com", secret)
            self.fail("expected ValueError")
        except ValueError as e:
            self.assertNotIn(secret, str(e))
            self.assertNotIn("Injected", str(e))

    def test_plain_token_is_accepted(self):
        jc.JiraClient("https://jira.example.com", "a-normal-token")  # must not raise


class JsonDecodeErrorWrappingTests(unittest.TestCase):
    """m3: a JSON decode failure must become a JiraError with an empty Code
    (not a bare JSONDecodeError) so suggest_sprints()'s `except JiraError`
    fallback actually fires — mirrors Go's getJSON (client.go:277-279)."""

    def _client(self):
        return jc.JiraClient("https://jira.example.com", "tok", sleep=lambda _d: None)

    def test_get_json_wraps_decode_failure(self):
        client = self._client()
        client._attempt = lambda url: (b"not json{{{", 200, "", None)
        with self.assertRaises(jc.JiraError) as ctx:
            client._get_json("Op", "/x")
        self.assertEqual(ctx.exception.code, "")

    def test_sprint_picker_wraps_decode_failure(self):
        client = self._client()
        client._attempt = lambda url: (b"not json{{{", 200, "", None)
        with self.assertRaises(jc.JiraError) as ctx:
            client.sprint_picker("query")
        self.assertEqual(ctx.exception.code, "")

    def test_suggest_sprints_falls_through_to_jql_on_picker_decode_failure(self):
        client = self._client()

        def fake_attempt(url):
            if "picker" in url:
                return b"not json{{{", 200, "", None
            if "/rest/api/2/field" in url:
                return json.dumps([{"name": "Sprint", "id": "customfield_1"}]).encode(), 200, "", None
            if "/rest/api/2/search" in url:
                return json.dumps({"issues": [], "total": 0}).encode(), 200, "", None
            raise AssertionError(f"unexpected url: {url}")

        client._attempt = fake_attempt
        out = client.suggest_sprints("Sprint 1")
        self.assertEqual(out, [])  # soft "nothing to suggest", not a crash


class EscapeJqlStringTests(unittest.TestCase):
    def test_escapes_backslash_and_double_quote(self):
        self.assertEqual(jc._escape_jql_string('back\\slash "quoted"'), 'back\\\\slash \\"quoted\\"')

    def test_plain_string_unchanged(self):
        self.assertEqual(jc._escape_jql_string("Sprint 42"), "Sprint 42")


class FirstFieldIdTests(unittest.TestCase):
    def test_exact_name_match_preferred_over_case_insensitive(self):
        field_ids = {"story points": "customfield_2", "Story Points": "customfield_1"}
        fid, name, ok = jc._first_field_id(field_ids, ["Story Points", "Story point estimate"])
        self.assertTrue(ok)
        self.assertEqual(fid, "customfield_1")
        self.assertEqual(name, "Story Points")

    def test_case_insensitive_fallback_when_no_exact_match(self):
        field_ids = {"story points": "customfield_9"}
        fid, name, ok = jc._first_field_id(field_ids, ["Story Points"])
        self.assertTrue(ok)
        self.assertEqual(fid, "customfield_9")

    def test_not_found_returns_empty_and_false(self):
        fid, name, ok = jc._first_field_id({}, ["Story Points"])
        self.assertFalse(ok)
        self.assertEqual(fid, "")
        self.assertEqual(name, "")


class ResolveFieldsTests(unittest.TestCase):
    def test_missing_sprint_field_raises(self):
        with self.assertRaises(jc.JiraError):
            jc._resolve_fields({})

    def test_story_points_override_takes_precedence_over_discovery(self):
        field_ids = {"Sprint": "customfield_1", "Story Points": "customfield_2", "Custom SP": "customfield_3"}
        rf = jc._resolve_fields(field_ids, story_points_field_id_override="customfield_3")
        self.assertEqual(rf.story_points_field_id, "customfield_3")
        self.assertEqual(rf.story_points_field_name, "Custom SP")
        self.assertTrue(rf.has_story_points)

    def test_search_fields_includes_common_names_plus_resolved_customs(self):
        field_ids = {"Sprint": "customfield_1", "Story Points": "customfield_2"}
        rf = jc._resolve_fields(field_ids)
        fields = rf.search_fields()
        self.assertIn("customfield_1", fields)
        self.assertIn("customfield_2", fields)
        for common in jc.COMMON_SEARCH_FIELD_NAMES:
            self.assertIn(common, fields)

    def test_missing_optional_fields_are_simply_absent_not_errors(self):
        field_ids = {"Sprint": "customfield_1"}  # no Story Points/QA/Role/Epic Link
        rf = jc._resolve_fields(field_ids)
        self.assertFalse(rf.has_story_points)
        self.assertFalse(rf.has_qa_estimation)
        self.assertFalse(rf.has_role)
        self.assertFalse(rf.has_epic_link)


if __name__ == "__main__":
    unittest.main()
