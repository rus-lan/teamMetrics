"""HTTP client for Jira Server/DC: auth, pagination, retries, rate limiting.

Python port of internal/adapters/jira/{client,sprints,search,changelog,fields,
statuses,timeparse,sprintfield,membership,issuefacts,picker}.go, stdlib-only
(urllib.request). Auth is `Authorization: Bearer <PAT>` — SPEC.md §2 and the
Go source (client.go: `req.Header.Set("Authorization", "Bearer "+c.token)`)
both use Bearer, not Basic; this file follows that authoritative source.
"""

from __future__ import annotations

import http.client
import json
import logging
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import logging_setup
from . import model

log = logging_setup.get_logger("jira_client")

UTC = timezone.utc

DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_PARALLEL = 4
DEFAULT_MAX_RETRIES = 3
DEFAULT_BASE_BACKOFF = 0.5
DEFAULT_MAX_BACKOFF = 8.0
DEFAULT_MAX_RETRY_AFTER = 60.0
SEARCH_PAGE_SIZE = 100
BOARD_SPRINTS_PAGE_SIZE = 50

# Bounds every paginated fetch loop (BoardSprints/SearchIssues) so a server
# that ignores startAt (or keeps reporting isLast=false) cannot hang the
# process forever — mirrors service.go's buildJobTimeout (10-minute bounded
# job lifetime, internal/app/board/service.go:42).
MAX_PAGINATION_SECONDS = 600.0

CODE_JIRA_UNREACHABLE = "ERR_JIRA_UNREACHABLE"
CODE_JIRA_AUTH_FAILED = "ERR_JIRA_AUTH_FAILED"

STORY_POINTS_FIELD_NAMES = ["Story Points", "Story point estimate"]
QA_ESTIMATION_FIELD_NAMES = ["QA Estimation", "QA estimation"]
ROLE_FIELD_NAMES = ["Role", "Роль"]
EPIC_LINK_FIELD_NAMES = ["Epic Link"]
# "resolutiondate" is fetched alongside the rest so personal_metrics.py's
# JiraIssueInput.resolutiondate (its done_at fallback for an issue that never
# shows a changelog-tracked transition into a final status) comes from THIS
# same Jira pass — see fetch_sprint_issues.
COMMON_SEARCH_FIELD_NAMES = ["summary", "status", "issuetype", "created", "labels", "assignee", "resolutiondate"]


class JiraError(Exception):
    """Mirrors adapters/jira.Error: Code is '' | ERR_JIRA_AUTH_FAILED | ERR_JIRA_UNREACHABLE."""

    def __init__(self, op: str, message: str, code: str = "", status_code: int = 0):
        if status_code:
            text = f"jira: {op}: HTTP {status_code}: {message}"
        else:
            text = f"jira: {op}: {message}"
        super().__init__(text)
        self.op = op
        self.code = code
        self.status_code = status_code
        self.message = message


# --------------------------------------------------------------------------
# Time parsing — every Jira timestamp is normalized to UTC (SPEC §2.6).
# --------------------------------------------------------------------------

_TIME_RE = re.compile(
    r"^(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})T"
    r"(?P<h>\d{2}):(?P<mi>\d{2}):(?P<s>\d{2})"
    r"(?:\.(?P<frac>\d+))?"
    r"(?P<tz>Z|[+-]\d{2}:?\d{2})$"
)


def parse_jira_time(s: str) -> datetime:
    if not s:
        raise ValueError("empty jira time string")
    m = _TIME_RE.match(s.strip())
    if not m:
        raise ValueError(f"unrecognized jira time format: {s!r}")
    frac = m.group("frac") or "0"
    microsecond = int((frac + "000000")[:6])
    tz = m.group("tz")
    if tz == "Z":
        tzinfo = UTC
    else:
        sign = 1 if tz[0] == "+" else -1
        digits = tz[1:].replace(":", "")
        hh, mm = int(digits[:2]), int(digits[2:4])
        tzinfo = timezone(sign * timedelta(hours=hh, minutes=mm))
    dt = datetime(
        int(m.group("y")), int(m.group("mo")), int(m.group("d")),
        int(m.group("h")), int(m.group("mi")), int(m.group("s")),
        microsecond, tzinfo=tzinfo,
    )
    return dt.astimezone(UTC)


def parse_optional_jira_time(s: str) -> datetime:
    """Future sprints may have empty startDate/endDate/completeDate. Go's
    parseOptionalJiraTime (timeparse.go:37-42) returns the zero time.Time{}
    for an empty string, not an error and not a null — this returns the same
    sentinel (model.ZERO_TIME) rather than None, so every caller always holds
    a real, comparable datetime (SPEC §2.6). Callers that need to know
    "was this date actually present" compare against model.ZERO_TIME."""
    if not s:
        return model.ZERO_TIME
    return parse_jira_time(s)


def _parse_optional_datetime_or_none(s: Optional[str]) -> Optional[datetime]:
    """Like parse_optional_jira_time, but returns None (not model.ZERO_TIME)
    for a missing value. Used for per-issue optional date fields consumed by
    the ai-metrics module (IssueFacts.resolutiondate), which needs a genuine
    "absent" marker — unlike Sprint start/end/complete, a missing
    resolutiondate must never be treated as a real (if degenerate) date."""
    if not s:
        return None
    return parse_jira_time(s)


# --------------------------------------------------------------------------
# Small wire-format helpers
# --------------------------------------------------------------------------


def _classify_status(status: int) -> str:
    if status in (401, 403):
        return CODE_JIRA_AUTH_FAILED
    if status == 429 or status >= 500:
        return CODE_JIRA_UNREACHABLE
    return ""


def _retryable_status(status: int) -> bool:
    return status == 429 or status >= 500


# No query param this client sends today carries a token/secret — every
# request authenticates via the Authorization header, never a query string —
# but a per-request debug log is exactly the kind of line that would leak
# one straight into a log file if that ever changed, so redact on the way
# out rather than trust it never will.
_TOKEN_LIKE_QUERY_KEYS = ("token", "password", "secret", "authorization", "apikey", "credential")


def _redact_query(query: Optional[dict]) -> dict:
    if not query:
        return {}
    return {k: ("***" if any(sub in str(k).lower() for sub in _TOKEN_LIKE_QUERY_KEYS) else v) for k, v in query.items()}


def _err_body(data: bytes) -> str:
    s = (data or b"").decode("utf-8", errors="replace").replace("\n", " ").strip()
    max_len = 500
    if len(s) > max_len:
        s = s[:max_len] + "..."
    return s or "(empty body)"


def _parse_retry_after_seconds(s: str) -> Optional[float]:
    s = (s or "").strip()
    if not s:
        return None
    try:
        n = int(s)
    except ValueError:
        return None
    if n < 0:
        return None
    return float(n)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects are not followed (SPEC §2: 'Redirects are not followed')."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


# --------------------------------------------------------------------------
# Wire DTOs
# --------------------------------------------------------------------------


@dataclass
class Sprint:
    id: int
    name: str
    state: str  # active|closed|future, lowercased
    board_id: int
    # Never None: a missing/empty Jira date decodes to model.ZERO_TIME (see
    # parse_optional_jira_time), matching Go's time.Time zero value — compare
    # against model.ZERO_TIME to test "was this date actually present".
    start_at: datetime
    end_at: datetime
    complete_at: datetime


@dataclass
class Board:
    id: int
    name: str
    type: str


@dataclass
class Status:
    id: str
    name: str
    category_key: str


@dataclass
class ServerInfo:
    """GET /rest/api/2/serverInfo, the fields cli.py's `check` uses to name
    which Jira it reached and whether this tool can talk to it at all
    (deploymentType, PAT/Bearer support by version). Every field defaults to
    its zero value — the endpoint's exact response shape isn't assumed, only
    read defensively (see JiraClient.server_info())."""

    version: str = ""
    version_numbers: list[int] = field(default_factory=list)
    deployment_type: str = ""
    build_number: int = 0
    server_title: str = ""


@dataclass
class SprintSuggestion:
    sprint_id: int
    name: str
    board_id: int = 0
    state: str = ""


@dataclass
class ChangelogEntry:
    date: datetime
    field: str
    from_: str
    to: str
    from_string: str
    to_string: str


@dataclass
class RawIssue:
    key: str
    type: str
    created: datetime
    status: str
    initial_status: str
    initial_status_id: str
    status_category_key: str
    assignee: str
    labels: list[str]
    fields: dict[str, Any]  # field id -> raw decoded JSON value
    changelog: list[ChangelogEntry]
    resolutiondate: Optional[datetime] = None
    # fields.assignee.displayName — the human name schema v2 shows instead of
    # the bare login (empty when the issue has no assignee).
    assignee_display_name: str = ""
    summary: str = ""


@dataclass
class RawStatusChange:
    at: datetime
    from_name: str
    to_name: str
    from_id: str
    to_id: str


@dataclass
class RawSPChange:
    at: datetime
    from_value: float
    to_value: float


@dataclass
class IssueFacts:
    key: str
    epic_key: str
    type: str
    role: str
    labels: list[str]
    assignee: str
    story_points: float
    qa_estimation: float
    created: datetime
    initial_status: str
    initial_status_id: str
    status_history: list[RawStatusChange]
    sp_events: list[RawSPChange]
    current_status: str
    current_status_category_key: str
    # tz-aware UTC or None; feeds personal_metrics.JiraIssueInput.resolutiondate
    # (its done_at fallback for an issue with no changelog-tracked transition
    # into a final status) — never model.ZERO_TIME, see
    # _parse_optional_datetime_or_none.
    resolutiondate: Optional[datetime] = None
    membership_by_sprint: dict[int, list["model.Interval"]] = field(default_factory=dict)
    # fields.assignee.displayName / fields.summary — schema v2's display-name
    # resolution (§B.13) and CSV export (jira_issues.csv) read these.
    assignee_display_name: str = ""
    summary: str = ""


def _sprint_from_dto(dto: dict) -> Sprint:
    return Sprint(
        id=dto.get("id", 0),
        name=dto.get("name", ""),
        state=(dto.get("state") or "").lower(),
        board_id=dto.get("originBoardId", 0),
        start_at=parse_optional_jira_time(dto.get("startDate") or ""),
        end_at=parse_optional_jira_time(dto.get("endDate") or ""),
        complete_at=parse_optional_jira_time(dto.get("completeDate") or ""),
    )


def _entries_from_histories(histories: list[dict]) -> list[ChangelogEntry]:
    out = []
    for h in histories:
        ts = parse_jira_time(h.get("created", ""))
        for it in h.get("items", []) or []:
            out.append(
                ChangelogEntry(
                    date=ts,
                    field=it.get("field") or "",
                    from_=it.get("from") or "",
                    to=it.get("to") or "",
                    from_string=it.get("fromString") or "",
                    to_string=it.get("toString") or "",
                )
            )
    out.sort(key=lambda e: e.date)
    return out


def _changelog_truncated(env: dict) -> bool:
    max_results = env.get("maxResults", 0) or 0
    total = env.get("total", 0) or 0
    return max_results > 0 and total > max_results


def _first_status_from(entries: list[ChangelogEntry]) -> str:
    for e in entries:
        if e.field == "status":
            return e.from_string
    return ""


def _first_status_id_from(entries: list[ChangelogEntry]) -> str:
    for e in entries:
        if e.field == "status":
            return e.from_
    return ""


def _parse_float_or_zero(s: str) -> float:
    s = (s or "").strip()
    if not s:
        return 0.0
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return 0.0


def _decode_numeric_field(raw: Any) -> float:
    """Decode a Jira custom-field value into a float, tolerant of the
    `{"value": <number>}` shape as well as `{"value": "<string>"}`.

    Deliberate divergence from Go: Go decodes this field into
    struct{Value string}, so a JSON NUMBER (rather than a string) under
    "value" fails that unmarshal and silently yields 0. This function
    tolerates either shape and parses the real number instead — a correct
    value beats a silently wrong 0."""
    if raw is None:
        return 0.0
    if isinstance(raw, bool):
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        return _parse_float_or_zero(raw)
    if isinstance(raw, dict):
        return _parse_float_or_zero(str(raw.get("value", "")))
    return 0.0


def _decode_string_field(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        value = raw.get("value") or ""
        if value:
            return str(value)
        name = raw.get("name") or ""
        if name:
            return str(name)
        return ""
    if isinstance(raw, list):
        return _decode_string_field(raw[0]) if raw else ""
    return ""


def _role_from_labels(labels: list[str]) -> str:
    for l in labels:
        low = l.strip().lower()
        if low in ("dev", "qa"):
            return low
    return ""


def _attr_between(s: str, key: str, stop: str) -> str:
    i = s.find(key)
    if i < 0:
        return ""
    rest = s[i + len(key) :]
    j = rest.find(stop)
    return rest[:j] if j >= 0 else rest


def _parse_sprint_field_entry(raw: Any) -> dict:
    if raw is None:
        raise ValueError("sprint field: empty entry")
    if isinstance(raw, dict):
        return {
            "id": raw.get("id", 0),
            "board_id": raw.get("boardId", 0),
            "state": (raw.get("state") or "").lower(),
            "name": raw.get("name", ""),
        }
    if isinstance(raw, str):
        lb, rb = raw.find("["), raw.rfind("]")
        if lb < 0 or rb <= lb:
            raise ValueError(f"sprint field: no [..] in {raw!r}")
        inner = raw[lb + 1 : rb]
        state = _attr_between(inner, "state=", ",").lower()
        name = _attr_between(inner, "name=", ",startDate=")
        id_str = _attr_between(inner, "id=", ",")
        try:
            sid = int(id_str)
        except ValueError as exc:
            raise ValueError(f"sprint field: attr id={id_str!r} not int") from exc
        board_id_str = _attr_between(inner, "rapidViewId=", ",")
        try:
            board_id = int(board_id_str)
        except ValueError:
            board_id = 0  # rapidViewId can be <null> on very old sprints
        return {"id": sid, "board_id": board_id, "state": state, "name": name}
    raise ValueError(f"sprint field: unsupported entry type {type(raw)!r}")


def _parse_sprint_field_array(raw: Any) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("sprint field: not an array")
    return [_parse_sprint_field_entry(it) for it in raw]


def _parse_id_list(s: str) -> list[int]:
    if not s or not s.strip():
        return []
    out = []
    for p in s.split(","):
        p = p.strip()
        try:
            out.append(int(p))
        except ValueError:
            continue
    return out


def _sprint_changes_from_entries(entries: list[ChangelogEntry]) -> list[model.SprintChange]:
    out = []
    for e in entries:
        if e.field != "Sprint":
            continue
        out.append(model.SprintChange(date=e.date, from_ids=_parse_id_list(e.from_), to_ids=_parse_id_list(e.to)))
    return out


def _status_changes_from_entries(entries: list[ChangelogEntry]) -> list[RawStatusChange]:
    out = []
    for e in entries:
        if e.field != "status":
            continue
        out.append(RawStatusChange(at=e.date, from_name=e.from_string, to_name=e.to_string, from_id=e.from_, to_id=e.to))
    return out


def _sp_changes_from_entries(entries: list[ChangelogEntry], sp_field_name: str) -> list[RawSPChange]:
    if not sp_field_name:
        return []
    out = []
    for e in entries:
        if e.field != sp_field_name:
            continue
        out.append(RawSPChange(at=e.date, from_value=_parse_float_or_zero(e.from_string), to_value=_parse_float_or_zero(e.to_string)))
    return out


def _first_field_id(field_ids: dict[str, str], names: list[str]) -> tuple[str, str, bool]:
    for candidate in names:
        if candidate in field_ids:
            return field_ids[candidate], candidate, True
    for candidate in names:
        for k, v in field_ids.items():
            if k.strip().casefold() == candidate.strip().casefold():
                return v, k, True
    return "", "", False


def _escape_jql_string(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


@dataclass
class _ResolvedFields:
    sprint_field_id: str
    story_points_field_id: str = ""
    story_points_field_name: str = ""
    has_story_points: bool = False
    qa_estimation_field_id: str = ""
    has_qa_estimation: bool = False
    role_field_id: str = ""
    has_role: bool = False
    epic_link_field_id: str = ""
    has_epic_link: bool = False

    def search_fields(self) -> list[str]:
        fields = list(COMMON_SEARCH_FIELD_NAMES)
        fields.append(self.sprint_field_id)
        if self.has_story_points:
            fields.append(self.story_points_field_id)
        if self.has_qa_estimation:
            fields.append(self.qa_estimation_field_id)
        if self.has_role:
            fields.append(self.role_field_id)
        if self.has_epic_link:
            fields.append(self.epic_link_field_id)
        return fields


def _resolve_fields(field_ids: dict[str, str], story_points_field_id_override: str = "") -> _ResolvedFields:
    if "Sprint" not in field_ids:
        raise JiraError("FetchSprintIssues", 'custom field "Sprint" not found')
    rf = _ResolvedFields(sprint_field_id=field_ids["Sprint"])
    if story_points_field_id_override:
        name = next((k for k, v in field_ids.items() if v == story_points_field_id_override), story_points_field_id_override)
        rf.story_points_field_id = story_points_field_id_override
        rf.story_points_field_name = name
        rf.has_story_points = True
    else:
        rf.story_points_field_id, rf.story_points_field_name, rf.has_story_points = _first_field_id(field_ids, STORY_POINTS_FIELD_NAMES)
    rf.qa_estimation_field_id, _, rf.has_qa_estimation = _first_field_id(field_ids, QA_ESTIMATION_FIELD_NAMES)
    rf.role_field_id, _, rf.has_role = _first_field_id(field_ids, ROLE_FIELD_NAMES)
    rf.epic_link_field_id, _, rf.has_epic_link = _first_field_id(field_ids, EPIC_LINK_FIELD_NAMES)
    return rf


def _flexible_int(v: Any) -> Optional[int]:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        try:
            return int(v.strip())
        except ValueError:
            return None
    return None


def _parse_picker_entry(entry: dict) -> Optional[SprintSuggestion]:
    if not isinstance(entry, dict) or "id" not in entry:
        return None
    sid = _flexible_int(entry.get("id"))
    if sid is None:
        return None
    state = entry.get("stateKey") or entry.get("state") or ""
    return SprintSuggestion(sprint_id=sid, name=entry.get("name") or "", state=str(state).lower())


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


class JiraClient:
    """One Jira Server/DC connection: one base URL + one PAT.

    <=4 parallel requests, 30s per-attempt timeout, 3 retries (4 attempts) on
    429/5xx/network errors with backoff min(500ms*2^attempt, 8s), Retry-After
    honored on 429 capped at 60s; other 4xx never retried (SPEC §2).

    Redirects: the opener is built with _NoRedirect, whose redirect_request()
    always returns None — per CPython's urllib.request.HTTPRedirectHandler,
    that makes a 3xx fall through to an HTTPError instead of urllib silently
    issuing a second request to whatever host the Location header names,
    carrying the Authorization: Bearer <PAT> header with it.

    Proxies: by default (trust_env_proxy=True, unchanged from before this
    parameter existed) the opener still includes urllib's default
    ProxyHandler, so http_proxy/https_proxy/no_proxy from the environment
    are honored and the PAT travels through whatever they point at — normal
    and often desired on a corporate network. Pass trust_env_proxy=False to
    build the opener without a working ProxyHandler (an empty-dict
    ProxyHandler({}) replaces the default one and picks up no environment
    proxy), so every request goes direct regardless of the environment.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_parallel: int = DEFAULT_MAX_PARALLEL,
        max_retries: int = DEFAULT_MAX_RETRIES,
        base_backoff: float = DEFAULT_BASE_BACKOFF,
        max_backoff: float = DEFAULT_MAX_BACKOFF,
        max_retry_after: float = DEFAULT_MAX_RETRY_AFTER,
        sleep=time.sleep,
        opener=None,
        trust_env_proxy: bool = True,
    ):
        # Reject a CR/LF-bearing token here, before it ever reaches an HTTP
        # header: http.client validates header values itself and raises a
        # bare ValueError that embeds the value verbatim, which would put the
        # PAT in a traceback. config.load_env() already rejects this at the
        # environment-variable boundary; this is defense in depth for any
        # other caller that constructs a JiraClient directly.
        if "\r" in token or "\n" in token:
            raise ValueError("JiraClient: token must not contain carriage-return or line-feed characters")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.timeout = timeout
        self.max_retries = max_retries
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self.max_retry_after = max_retry_after
        self._sem = threading.Semaphore(max_parallel)
        if opener is not None:
            self._opener = opener
        elif trust_env_proxy:
            self._opener = urllib.request.build_opener(_NoRedirect)
        else:
            self._opener = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))
        self._sleep = sleep

    def _backoff_delay(self, attempt: int) -> float:
        d = self.base_backoff * (2**attempt)
        if d <= 0 or d > self.max_backoff:
            return self.max_backoff
        return d

    def _attempt(self, url: str):
        self._sem.acquire()
        try:
            req = urllib.request.Request(
                url,
                headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
            )
            try:
                resp = self._opener.open(req, timeout=self.timeout)
                data = resp.read()
                return data, resp.status, resp.headers.get("Retry-After", ""), None
            except urllib.error.HTTPError as e:
                data = e.read()
                retry_after = e.headers.get("Retry-After", "") if e.headers else ""
                return data, e.code, retry_after, None
            except urllib.error.URLError as e:
                return None, 0, "", e
            except socket.timeout as e:
                # On Python 3.9, socket.timeout is its own class, not yet an
                # alias of TimeoutError (that alias starts in 3.10) — a read
                # timeout raised straight from resp.read() (rather than
                # wrapped in URLError) would otherwise skip both retry
                # branches and crash the request outright.
                return None, 0, "", e
            except TimeoutError as e:
                return None, 0, "", e
        finally:
            self._sem.release()

    def _redact_token(self, text: str) -> str:
        """Drops any occurrence of this client's own token from `text` before
        it can leave the client in an exception message — an upstream error
        body that happens to echo back the Authorization header value (some
        servers do, on a malformed-auth response) must not carry the token
        into stdout (`check`) or `out/raw/` (JiraError text embedded in a
        skipped-item message)."""
        if self._token and self._token in text:
            return text.replace(self._token, "***")
        return text

    def _do(self, op: str, path: str, query: Optional[dict] = None) -> bytes:
        full_url = self.base_url + path
        if query:
            full_url += "?" + urllib.parse.urlencode(query)

        attempt = 0
        while True:
            if log.isEnabledFor(logging.DEBUG):
                log.debug("Jira запрос: GET %s %s (попытка %d)", path, _redact_query(query), attempt + 1)
            try:
                data, status, retry_after, transport_err = self._attempt(full_url)
            except http.client.InvalidURL:
                # Raised deep inside urllib/http.client while opening the
                # connection, before any request is sent -- observed when
                # JIRA_BASE_URL embeds credentials (a colon in the userinfo
                # makes http.client's own host:port split misparse the
                # password as a "port"). The exception text quotes the
                # mis-split fragment verbatim, which can contain that
                # password -- never pass it through, raise a fixed message
                # instead of str(e).
                raise JiraError(
                    op,
                    "некорректный URL Jira (проверьте JIRA_BASE_URL — учётные данные вида user:pass@host в адресе "
                    "не поддерживаются, токен передаётся отдельно через JIRA_TOKEN)",
                    code=CODE_JIRA_UNREACHABLE,
                )

            if transport_err is not None:
                if attempt >= self.max_retries:
                    raise JiraError(op, self._redact_token(str(transport_err)), code=CODE_JIRA_UNREACHABLE)
                delay = self._backoff_delay(attempt)
                log.info(
                    "Jira: повтор через %.2fs (попытка %d/%d) — %s",
                    delay, attempt + 1, self.max_retries, self._redact_token(str(transport_err)),
                )
                self._sleep(delay)
                attempt += 1
                continue

            if 200 <= status < 300:
                return data

            if not _retryable_status(status) or attempt >= self.max_retries:
                raise JiraError(op, self._redact_token(_err_body(data)), code=_classify_status(status), status_code=status)

            delay = self._backoff_delay(attempt)
            if status == 429:
                ra = _parse_retry_after_seconds(retry_after)
                if ra is not None:
                    if ra > self.max_retry_after:
                        ra = self.max_retry_after
                    if ra > delay:
                        delay = ra
            log.info("Jira: повтор через %.2fs (попытка %d/%d) — HTTP %d", delay, attempt + 1, self.max_retries, status)
            self._sleep(delay)
            attempt += 1

    def _get_json(self, op: str, path: str, query: Optional[dict] = None) -> Any:
        data = self._do(op, path, query)
        if not data:
            return None
        try:
            return json.loads(data)
        except json.JSONDecodeError as e:
            # Mirrors Go's getJSON (client.go:277-279): wraps a decode
            # failure as an Error with an empty Code, which keeps
            # isGenuineJiraFault false so callers like SuggestSprints treat
            # it as "nothing to suggest", not a hard failure.
            raise JiraError(op, f"decode response: {e}", code="") from e

    # -- agile API -----------------------------------------------------

    def sprint(self, sprint_id: int) -> Sprint:
        dto = self._get_json("Sprint", f"/rest/agile/1.0/sprint/{sprint_id}")
        return _sprint_from_dto(dto or {})

    def board(self, board_id: int) -> Board:
        dto = self._get_json("Board", f"/rest/agile/1.0/board/{board_id}") or {}
        return Board(id=dto.get("id", 0), name=dto.get("name", ""), type=dto.get("type", ""))

    def board_sprints(self, board_id: int, state: Optional[str] = None) -> list[Sprint]:
        out: list[Sprint] = []
        start_at = 0
        deadline = time.monotonic() + MAX_PAGINATION_SECONDS
        while True:
            if time.monotonic() > deadline:
                raise JiraError(
                    "BoardSprints",
                    f"pagination did not finish within {MAX_PAGINATION_SECONDS:.0f}s "
                    "(server may be ignoring startAt/isLast)",
                    code=CODE_JIRA_UNREACHABLE,
                )
            q: dict[str, Any] = {"startAt": start_at, "maxResults": BOARD_SPRINTS_PAGE_SIZE}
            if state:
                q["state"] = state
            page = self._get_json("BoardSprints", f"/rest/agile/1.0/board/{board_id}/sprint", q) or {}
            values = page.get("values", []) or []
            for v in values:
                out.append(_sprint_from_dto(v))
            got = len(values)
            if page.get("isLast", True) or got == 0:
                break
            start_at += got  # server may silently cap maxResults below what was requested
        return out

    def closed_sprints(self, board_id: int) -> list[Sprint]:
        return self.board_sprints(board_id, "closed")

    # -- field/status catalogs ------------------------------------------

    def field_ids(self) -> dict[str, str]:
        dto = self._get_json("FieldIDs", "/rest/api/2/field") or []
        return {f.get("name", ""): f.get("id", "") for f in dto}

    def list_statuses(self) -> list[Status]:
        dto = self._get_json("ListStatuses", "/rest/api/2/status") or []
        return [
            Status(id=s.get("id", ""), name=s.get("name", ""), category_key=(s.get("statusCategory") or {}).get("key", ""))
            for s in dto
        ]

    def server_info(self) -> ServerInfo:
        """Plain read, no parameters. Some instances restrict this endpoint
        (403/404) or answer with a shape this method doesn't expect — every
        field is read with .get() and defensively coerced, never assumed
        present, so an odd or truncated response degrades to zero values
        instead of raising. Callers (cli.py's `check`) treat a JiraError from
        this call as "version unknown", not a hard failure."""
        dto = self._get_json("ServerInfo", "/rest/api/2/serverInfo")
        if not isinstance(dto, dict):
            dto = {}

        raw_numbers = dto.get("versionNumbers")
        version_numbers: list[int] = []
        if isinstance(raw_numbers, list):
            for n in raw_numbers:
                parsed = _flexible_int(n)
                if parsed is not None:
                    version_numbers.append(parsed)

        return ServerInfo(
            version=str(dto.get("version") or ""),
            version_numbers=version_numbers,
            deployment_type=str(dto.get("deploymentType") or ""),
            build_number=_flexible_int(dto.get("buildNumber")) or 0,
            server_title=str(dto.get("serverTitle") or ""),
        )

    # -- issue search + changelog ----------------------------------------

    def issue_changelog(self, key: str) -> list[ChangelogEntry]:
        """Re-read the FULL changelog when the /search-embedded one is truncated (SPEC §2.5)."""
        data = self._get_json("IssueChangelog", f"/rest/api/2/issue/{key}", {"expand": "changelog"}) or {}
        env = data.get("changelog") or {}
        return _entries_from_histories(env.get("histories", []) or [])

    def _issue_from_dto(self, dto: dict) -> tuple[Optional[RawIssue], bool]:
        fields = dto.get("fields") or {}
        if not fields:
            raise JiraError("SearchIssues", f"issue {dto.get('key')}: missing fields object")

        issue_type = (fields.get("issuetype") or {}).get("name", "")
        if model.is_epic(issue_type):
            return None, True

        created = parse_jira_time(fields.get("created", ""))
        status_obj = fields.get("status") or {}
        status_name = status_obj.get("name", "")
        status_id = status_obj.get("id", "")
        status_category_key = (status_obj.get("statusCategory") or {}).get("key", "")

        assignee_obj = fields.get("assignee")
        assignee = assignee_obj.get("name", "") if assignee_obj else ""
        assignee_display_name = assignee_obj.get("displayName", "") if assignee_obj else ""
        labels = list(fields.get("labels") or [])
        resolutiondate = _parse_optional_datetime_or_none(fields.get("resolutiondate"))
        summary = fields.get("summary") or ""

        changelog_raw = dto.get("changelog") or {}
        entries = _entries_from_histories(changelog_raw.get("histories", []) or [])
        if _changelog_truncated(changelog_raw):
            entries = self.issue_changelog(dto.get("key", ""))

        initial_status = _first_status_from(entries) or status_name
        initial_status_id = _first_status_id_from(entries) or status_id

        issue = RawIssue(
            key=dto.get("key", ""),
            type=issue_type,
            created=created,
            status=status_name,
            initial_status=initial_status,
            initial_status_id=initial_status_id,
            status_category_key=status_category_key,
            assignee=assignee,
            labels=labels,
            fields=fields,
            changelog=entries,
            resolutiondate=resolutiondate,
            assignee_display_name=assignee_display_name,
            summary=summary,
        )
        return issue, False

    def search_issues(self, jql: str, fields: list[str]) -> list[RawIssue]:
        """GET /rest/api/2/search, paginated by actual returned count vs total (SPEC §2.1)."""
        log.info("JQL: %s", jql)
        out: list[RawIssue] = []
        start_at = 0
        deadline = time.monotonic() + MAX_PAGINATION_SECONDS
        while True:
            if time.monotonic() > deadline:
                raise JiraError(
                    "SearchIssues",
                    f"pagination did not finish within {MAX_PAGINATION_SECONDS:.0f}s "
                    "(server may be ignoring startAt/total)",
                    code=CODE_JIRA_UNREACHABLE,
                )
            q: dict[str, Any] = {"jql": jql, "expand": "changelog", "startAt": start_at, "maxResults": SEARCH_PAGE_SIZE}
            if fields:
                q["fields"] = ",".join(fields)
            page = self._get_json("SearchIssues", "/rest/api/2/search", q) or {}
            issues = page.get("issues", []) or []
            total = page.get("total", 0) or 0
            for dto in issues:
                issue, skip = self._issue_from_dto(dto)
                if not skip and issue is not None:
                    out.append(issue)
            got = len(issues)
            log.info("Jira: получено %d из %d задач", start_at + got, total)
            if got == 0 or start_at + got >= total:
                break
            start_at += got
        return out

    # -- sprint issue facts (membership + per-issue fields) --------------

    def fetch_sprint_issues(self, sprint_ids: list[int], *, story_points_field_id: str = "") -> list[IssueFacts]:
        """One JQL `sprint in (...)` covering every requested sprint; membership
        intervals are then rebuilt independently per sprint id from the same
        fetched changelog (SPEC §2.2/§2.7, §3.2)."""
        field_ids = self.field_ids()
        rf = _resolve_fields(field_ids, story_points_field_id)

        jql = "sprint in (" + ",".join(str(i) for i in sprint_ids) + ")"
        raw_issues = self.search_issues(jql, rf.search_fields())

        out: list[IssueFacts] = []
        for ri in raw_issues:
            story_points = _decode_numeric_field(ri.fields.get(rf.story_points_field_id)) if rf.has_story_points else 0.0
            qa_estimation = _decode_numeric_field(ri.fields.get(rf.qa_estimation_field_id)) if rf.has_qa_estimation else 0.0
            role = _decode_string_field(ri.fields.get(rf.role_field_id)) if rf.has_role else ""
            if not role:
                role = _role_from_labels(ri.labels)
            epic_key = _decode_string_field(ri.fields.get(rf.epic_link_field_id)) if rf.has_epic_link else ""

            sprint_field_raw = ri.fields.get(rf.sprint_field_id)
            current_refs = _parse_sprint_field_array(sprint_field_raw)
            current_ids = [r["id"] for r in current_refs]
            events = _sprint_changes_from_entries(ri.changelog)

            membership_by_sprint: dict[int, list[model.Interval]] = {}
            for sid in sprint_ids:
                membership_by_sprint[sid] = model.build_membership_intervals(ri.created, events, current_ids, sid)

            out.append(
                IssueFacts(
                    key=ri.key,
                    epic_key=epic_key,
                    type=ri.type,
                    role=role,
                    labels=ri.labels,
                    assignee=ri.assignee,
                    story_points=story_points,
                    qa_estimation=qa_estimation,
                    created=ri.created,
                    initial_status=ri.initial_status,
                    initial_status_id=ri.initial_status_id,
                    status_history=_status_changes_from_entries(ri.changelog),
                    sp_events=_sp_changes_from_entries(ri.changelog, rf.story_points_field_name),
                    current_status=ri.status,
                    current_status_category_key=ri.status_category_key,
                    resolutiondate=ri.resolutiondate,
                    membership_by_sprint=membership_by_sprint,
                    assignee_display_name=ri.assignee_display_name,
                    summary=ri.summary,
                )
            )
        return out

    # -- sprint name resolution (SuggestSprints) -------------------------

    def sprint_picker(self, query: str) -> list[SprintSuggestion]:
        data = self._do("SprintPicker", "/rest/greenhopper/1.0/sprint/picker", {"query": query})
        try:
            dto = json.loads(data) if data else {}
        except json.JSONDecodeError as e:
            # Empty Code, same as _get_json — suggest_sprints()'s caller
            # treats this as "picker unavailable, fall through to the JQL
            # fallback", not a genuine Jira fault (picker.go:34-40).
            raise JiraError("SprintPicker", f"decode response: {e}", code="") from e
        seen: set[int] = set()
        out = []
        for raw in list(dto.get("suggestions", []) or []) + list(dto.get("allMatches", []) or []):
            sug = _parse_picker_entry(raw)
            if sug is None or sug.sprint_id in seen:
                continue
            seen.add(sug.sprint_id)
            out.append(sug)
        return out

    def _suggest_sprints_via_jql(self, query: str) -> list[SprintSuggestion]:
        field_ids = self.field_ids()
        sprint_field_id = field_ids.get("Sprint")
        if not sprint_field_id:
            raise JiraError("SuggestSprints", 'custom field "Sprint" not found')
        jql = f'sprint = "{_escape_jql_string(query)}"'
        issues = self.search_issues(jql, [sprint_field_id])
        if not issues:
            raise JiraError("SuggestSprints", "jql sprint fallback: no matching issues")
        raw = issues[0].fields.get(sprint_field_id)
        refs = _parse_sprint_field_array(raw)
        for ref in refs:
            if model.status_equal(ref["name"], query):
                return [SprintSuggestion(sprint_id=ref["id"], name=ref["name"], board_id=ref["board_id"], state=ref["state"])]
        raise JiraError("SuggestSprints", f"issue {issues[0].key}: no matching sprint field value")

    def suggest_sprints(self, query: str) -> list[SprintSuggestion]:
        """Picker first, JQL exact-match fallback; both soft-failing -> empty list, not an error (SPEC §2.8)."""
        try:
            return self.sprint_picker(query)
        except JiraError as picker_err:
            try:
                return self._suggest_sprints_via_jql(query)
            except JiraError as fallback_err:
                if picker_err.code:
                    raise picker_err
                if fallback_err.code:
                    raise fallback_err
                return []
