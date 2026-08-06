"""Fake HTTP transport for jira_client.JiraClient / gitlab_client.GitLabClient.

Both clients build their own `urllib.request` opener internally:
JiraClient.__init__ always does `self._opener =
urllib.request.build_opener(_NoRedirect)` with no constructor parameter to
override it; GitLabClient.__init__ *does* accept an `opener=` kwarg, but
cli.py's `main()` never threads one through (cmd_check/cmd_run only accept
jira_client_cls/gitlab_client_cls overrides, and main() calls both with
neither of those either). So through a real `cli.main(argv, environ)`
invocation, `opener=` is not a reachable seam for either client — the one
seam both actually share is `urllib.request.build_opener` itself. Tests patch
that name (see test_e2e.py) to return a `FakeOpener`, which both clients then
use exactly like a real socket-backed opener, down to raising
`urllib.error.HTTPError` on a non-2xx status.

FakeOpener only implements the one surface both clients call:
`.open(request, timeout=None)` -> an object with `.status`/`.read()`/
`.headers` (mirrors `http.client.HTTPResponse`), or raises `HTTPError`.
"""

from __future__ import annotations

import io
import json as json_mod
import re
import urllib.error
import urllib.parse
from email.message import Message
from typing import Any, Callable, Optional

Handler = Callable[[Any, dict, Any], tuple]


class RouteNotFound(AssertionError):
    """No fixture route matched a request — a fixture gap, not a code bug."""


def _headers_message(headers: Optional[dict]) -> Message:
    msg = Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    return msg


def json_body(obj: Any) -> bytes:
    return json_mod.dumps(obj).encode("utf-8")


def get_header(request, name: str) -> Optional[str]:
    """`urllib.request.Request.get_header` does no case-folding of its own —
    `add_header` stores every name under `str.capitalize()`, so callers must
    query with that exact form (`Authorization`, `Private-token` — not
    `PRIVATE-TOKEN`) or the lookup silently misses."""
    return request.get_header(name.capitalize())


def bearer_token(request) -> Optional[str]:
    value = get_header(request, "Authorization") or ""
    return value[len("Bearer "):] if value.startswith("Bearer ") else None


def private_token(request) -> Optional[str]:
    return get_header(request, "Private-token")


class FakeResponse:
    """Stands in for `http.client.HTTPResponse`: `.status`, `.read()`, `.headers`."""

    def __init__(self, status: int, body: bytes, headers: Optional[dict] = None):
        self.status = status
        self._body = body
        self.headers = _headers_message(headers)

    def read(self) -> bytes:
        return self._body


class FakeOpener:
    """A tiny path-routed HTTP stub, good enough for both clients' `_attempt()`
    (`self._opener.open(req, timeout=...)`).

    One instance registers BOTH Jira (`/rest/...`) and GitLab (`/api/v4/...`)
    routes — real `cli.main()` builds one JiraClient and, for `run`/`check`,
    one GitLabClient, and both end up sharing the single opener
    `urllib.request.build_opener` is patched to return, so one FakeOpener
    naturally serves both.

    A handler is `(match, query, request) -> (status, body, response_headers)`.
    `body` is a dict (JSON-encoded automatically) or raw bytes. A non-2xx
    `status` raises `urllib.error.HTTPError`, exactly like a real opener —
    both clients' `_attempt()` catch that specifically.
    """

    def __init__(self, name: str = "fake"):
        self.name = name
        self._routes: list[tuple["re.Pattern[str]", Handler]] = []
        self.calls: list[tuple[str, dict]] = []

    def route(self, path_pattern: str, handler: Handler) -> "FakeOpener":
        self._routes.append((re.compile(path_pattern + r"$"), handler))
        return self

    def open(self, request, timeout=None):
        full_url = request.full_url
        parsed = urllib.parse.urlsplit(full_url)
        query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
        self.calls.append((parsed.path, query))

        for regex, handler in self._routes:
            m = regex.match(parsed.path)
            if not m:
                continue
            status, body, headers = handler(m, query, request)
            body_bytes = body if isinstance(body, (bytes, bytearray)) else json_body(body)
            if 200 <= status < 300:
                return FakeResponse(status, body_bytes, headers)
            raise urllib.error.HTTPError(
                full_url, status, "mock error", _headers_message(headers), io.BytesIO(body_bytes)
            )

        raise RouteNotFound(f"[{self.name}] no fixture route for GET {parsed.path} (query={query})")

    def paths_called(self) -> list[str]:
        return [p for p, _q in self.calls]
