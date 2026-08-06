"""HTTP client for GitLab REST API v4: auth, pagination, retries, read-only guarantee.

Python 3.9+ standard library only (urllib.request) — no `requests`. Auth is the
`PRIVATE-TOKEN` header (SPEC.md `.research/ai-integration-metrics/SPEC.md` §4.2).

Read-only guarantee: every request this client can ever issue is a GET. This
is not a runtime check on a `method` argument (the original `ai-metrics`
skill's `SafeSession._request_raw` raised `ReadOnlyViolation` if `method !=
"GET"`) — here there simply is no `method` or `data` parameter anywhere in
this file, on any public or private function. `urllib.request.Request`
defaults to GET whenever no request body is attached, and no code path in
this module ever attaches one. A non-GET call is therefore not merely
unused, it has no expressible call site.

This module never reads a config file or an environment variable and never
logs the token — `base_url`/`token` are constructor arguments, matching
`jira_client.JiraClient`'s shape so wiring code treats both clients the same
way.

Error channel: this module has no logging by design. `fetch_team_data()`
reports every per-project / per-author failure it tolerates (as opposed to
propagates) through its own return dict (`skipped_projects`,
`mr_fetch_errors`) — that dict IS the channel a caller inspects.

Load profile: three per-item fan-outs exist (`_mr_detail`, `_mr_commit_count`,
`_pipeline_job_user` — one request per MR, twice, and one per pipeline).
`fetch_mr_details`/`fetch_pipeline_user` on `merge_requests()`/`pipelines()`/
`fetch_team_data()` opt out of them at the library level; skipped data is
marked UNAVAILABLE via the existing `*_available` flags, never faked as 0.
`fetch_team_data()`'s `request_count` reports how many actual HTTP round
trips (including retries) a given run made. On a 429, the retry delay
honours the server's own `Retry-After` (capped at `max_retry_after`) instead
of only ever sleeping our own backoff schedule.
"""

from __future__ import annotations

import http.client
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

UTC = timezone.utc

DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_BASE_BACKOFF = 0.5
DEFAULT_MAX_BACKOFF = 8.0
DEFAULT_MAX_WORKERS = 8
DEFAULT_MAX_RETRY_AFTER = 60.0

MR_PAGE_SIZE = 100
PIPELINE_PAGE_SIZE = 100
DEPLOYMENT_PAGE_SIZE = 100
JOBS_PAGE_SIZE = 1

MR_STATES = ("merged", "closed")

# A page cap for `_get_paginated` (n4): a server that ignores the `page`
# param (or one whose X-Next-Page never converges) would otherwise loop
# forever. 10000 pages is generous headroom above any real project's
# history at the page sizes this module uses.
_MAX_PAGINATION_PAGES = 10000

# A wall-clock deadline alongside the page cap: a server that keeps
# answering (so the page cap never trips) but does so slowly could still
# hold a run open indefinitely. Mirrors jira_client.py's
# MAX_PAGINATION_SECONDS.
_MAX_PAGINATION_SECONDS = 600.0

# Same extraction rule as the source skill's gitlab_collector.py:39, now also
# searched over source_branch (m5) — the first candidate not in the
# false-positive denylist wins for the single best-guess `jira_key`; every
# raw match is kept in `jira_key_candidates` for a wiring layer that has the
# real issue-key list to validate/disambiguate against.
JIRA_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9_]*-\d+\b")

# Well-known non-Jira "WORD-NUMBER" tokens that match JIRA_KEY_RE but are
# never real Jira project keys (m5) — MR titles/descriptions/branches
# routinely reference these.
_JIRA_KEY_FALSE_POSITIVES = frozenset({"UTF", "CVE", "RFC", "ISO", "ECMA", "SHA", "MD", "PEP"})

_RETRYABLE_STATUSES = (429, 500, 502, 503)


class GitLabError(Exception):
    """code: '' | 'AUTH_FAILED' | 'NOT_FOUND' | 'UNREACHABLE'."""

    def __init__(self, op: str, message: str, code: str = "", status_code: int = 0):
        text = f"gitlab: {op}: HTTP {status_code}: {message}" if status_code else f"gitlab: {op}: {message}"
        super().__init__(text)
        self.op = op
        self.code = code
        self.status_code = status_code
        self.message = message


# --------------------------------------------------------------------------
# Small wire-format helpers
# --------------------------------------------------------------------------


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    text = s.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        d = datetime.fromisoformat(text)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.astimezone(UTC)


def _format_window_bound(d: datetime) -> str:
    # Not strftime("%Y-..."): %Y delegates to the platform C library, which
    # does not zero-pad years below 1000 on Python 3.9 (e.g. "1-01-01"
    # instead of "0001-01-01"), while 3.10+ does. Manual formatting is
    # version-independent.
    d = d.astimezone(UTC)
    return f"{d.year:04d}-{d.month:02d}-{d.day:02d}T{d.hour:02d}:{d.minute:02d}:{d.second:02d}Z"


def _qs(v: Any) -> str:
    """Quote a value for use as a URL path segment (m9) — every project/MR/
    pipeline id embedded in an f-string path goes through this, matching the
    quoting `project_id()` already applied to project paths."""
    return urllib.parse.quote(str(v), safe="")


def _int_or_none(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _changes_count(v: Any) -> tuple:
    """GitLab returns the literal string "1000+" when a MR's changes_count is
    capped; that is a KNOWN value (>=1000), not an unavailable one (m1) — the
    generic `_int_or_none` would silently discard it as unparseable and bias
    exactly the largest MRs toward "unknown"."""
    if isinstance(v, str) and v.strip() == "1000+":
        return 1000, True
    return _int_or_none(v), False


def _float_or_none(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _jira_key_candidates(text: str) -> list:
    return [m.group(0) for m in JIRA_KEY_RE.finditer(text)]


def _best_jira_key(candidates: list) -> str:
    for c in candidates:
        if c.split("-", 1)[0] not in _JIRA_KEY_FALSE_POSITIVES:
            return c
    return ""


def _cycle_time_seconds(created_at: Optional[str], merged_at: Optional[str]) -> Optional[int]:
    c = _parse_iso(created_at)
    m = _parse_iso(merged_at)
    if c is None or m is None:
        return None
    delta = (m - c).total_seconds()
    if delta < 0:
        return None
    return int(delta)


def _hours(seconds: Optional[int]) -> Optional[float]:
    return round(seconds / 3600.0, 2) if seconds is not None else None


def _parse_retry_after_seconds(s: str) -> Optional[float]:
    """Only the integer delay-seconds form of `Retry-After` (RFC 7231 also
    allows an HTTP-date form, but GitLab's own docs do not specify which
    form it sends — this mirrors jira_client.py's identical parser, which
    only handles delay-seconds too). An unparseable/date-form value simply
    falls back to our own backoff, same as no header at all."""
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


def _err_body(data: Optional[bytes]) -> str:
    s = (data or b"").decode("utf-8", errors="replace").replace("\n", " ").strip()
    max_len = 500
    if len(s) > max_len:
        s = s[:max_len] + "..."
    return s or "(empty body)"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


@dataclass(frozen=True)
class Window:
    """Inclusive analysis window, e.g. the sprint dates the caller resolved
    elsewhere.

    CONTRACT (B2): `end` is the raw window end exactly as the caller has it —
    callers must NOT pre-apply end-of-day. `params()` applies 23:59:59 UTC to
    the "before" bound internally, matching the source skill's
    `f"{date_to}T23:59:59Z"` (gitlab_collector.py), so a window ending on the
    last day of a sprint does not truncate that day.
    """

    start: datetime
    end: datetime

    def params(self, after_key: str, before_key: str) -> dict:
        end_of_day = self.end.astimezone(UTC).replace(hour=23, minute=59, second=59, microsecond=0)
        return {after_key: _format_window_bound(self.start), before_key: _format_window_bound(end_of_day)}


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


class GitLabClient:
    """One GitLab connection: one base URL + one PAT.

    Bounded parallelism (<=max_workers concurrent requests), 30s per-attempt
    timeout, 3 retries (4 attempts) on 429/500/502/503 and on network errors,
    backoff min(base_backoff*2^attempt, max_backoff), capped at max_retry_after
    when a 429 carries a Retry-After we should honour instead; other 4xx
    never retried.

    Proxies: by default (trust_env_proxy=True, unchanged behaviour) the
    opener still includes urllib's default ProxyHandler, so
    http_proxy/https_proxy/no_proxy from the environment are honoured and
    the PAT travels through whatever they point at — normal and often
    desired on a corporate network. Pass trust_env_proxy=False to build the
    opener without a working ProxyHandler (an empty-dict ProxyHandler({})
    replaces the default one and picks up no environment proxy), so every
    request goes direct regardless of the environment. Mirrors
    jira_client.JiraClient's identical parameter/mechanism. An explicitly
    supplied `opener` always wins over `trust_env_proxy` — the parameter is
    simply not consulted once a caller hands in their own opener.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        base_backoff: float = DEFAULT_BASE_BACKOFF,
        max_backoff: float = DEFAULT_MAX_BACKOFF,
        max_retry_after: float = DEFAULT_MAX_RETRY_AFTER,
        max_workers: int = DEFAULT_MAX_WORKERS,
        sleep=time.sleep,
        opener=None,
        trust_env_proxy: bool = True,
    ):
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.timeout = timeout
        self.max_retries = max_retries
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self.max_retry_after = max_retry_after
        self.max_workers = max_workers
        self._sleep = sleep
        if opener is not None:
            self._opener = opener
        elif trust_env_proxy:
            self._opener = urllib.request.build_opener(_NoRedirect)
        else:
            self._opener = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))
        self._sem = threading.Semaphore(max_workers)
        # Every real HTTP round trip this client makes goes through
        # _attempt() exactly once, including retries — counted here (not
        # derived from _do()'s success path) so it reflects actual load, not
        # just logical calls. fetch_team_data() reports the delta over one
        # run; the lock guards concurrent increments from the thread pools.
        self.request_count = 0
        self._request_count_lock = threading.Lock()

    # -- transport ---------------------------------------------------------

    def _backoff_delay(self, attempt: int) -> float:
        d = self.base_backoff * (2**attempt)
        if d <= 0 or d > self.max_backoff:
            return self.max_backoff
        return d

    def _attempt(self, url: str):
        self._sem.acquire()
        try:
            with self._request_count_lock:
                self.request_count += 1
            req = urllib.request.Request(url, headers={"PRIVATE-TOKEN": self._token, "Accept": "application/json"})
            try:
                resp = self._opener.open(req, timeout=self.timeout)
                data = resp.read()
                headers = {k.lower(): v for k, v in resp.headers.items()}
                return data, resp.status, headers, None
            except urllib.error.HTTPError as e:
                try:
                    data = e.read()
                except (OSError, http.client.IncompleteRead) as read_err:
                    return None, 0, {}, read_err
                headers = {k.lower(): v for k, v in e.headers.items()} if e.headers else {}
                return data, e.code, headers, None
            except (OSError, http.client.IncompleteRead) as e:
                # Covers urllib.error.URLError and TimeoutError (both
                # OSError subclasses), ConnectionResetError, and — on Python
                # 3.9, where socket.timeout is a distinct OSError subclass
                # rather than an alias of TimeoutError — a bare read
                # timeout, plus http.client.IncompleteRead, which is NOT an
                # OSError subclass and needs its own arm (M6). A response
                # that fails mid-body must retry exactly like one that never
                # connected.
                return None, 0, {}, e
        finally:
            self._sem.release()

    def _do(self, op: str, path: str, params: Optional[dict] = None) -> tuple[bytes, dict]:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)

        attempt = 0
        while True:
            data, status, headers, transport_err = self._attempt(url)

            if transport_err is not None:
                if attempt >= self.max_retries:
                    raise GitLabError(op, str(transport_err), code="UNREACHABLE")
                self._sleep(self._backoff_delay(attempt))
                attempt += 1
                continue

            if 200 <= status < 300:
                return data, headers

            if status in (401, 403):
                raise GitLabError(op, _err_body(data), code="AUTH_FAILED", status_code=status)
            if status == 404:
                raise GitLabError(op, _err_body(data), code="NOT_FOUND", status_code=status)
            if status not in _RETRYABLE_STATUSES or attempt >= self.max_retries:
                raise GitLabError(op, _err_body(data), code="", status_code=status)

            delay = self._backoff_delay(attempt)
            if status == 429:
                # Honour the server's own Retry-After instead of hammering
                # it on our own schedule (a rate-limited GitLab is exactly
                # the load pattern a "read-only" client can still turn into
                # an incident) — same rule as jira_client.py: only ever
                # widens the delay, and is capped at max_retry_after so a
                # misbehaving server can't stall a run indefinitely.
                ra = _parse_retry_after_seconds(headers.get("retry-after", ""))
                if ra is not None:
                    if ra > self.max_retry_after:
                        ra = self.max_retry_after
                    if ra > delay:
                        delay = ra
            self._sleep(delay)
            attempt += 1

    def _get_json(self, op: str, path: str, params: Optional[dict] = None) -> Any:
        data, _ = self._do(op, path, params)
        if not data:
            return None
        return json.loads(data)

    def _get_paginated(self, op: str, path: str, params: Optional[dict] = None, *, per_page: int = 100) -> list:
        """GitLab-style pagination: X-Next-Page header, falling back to a plain
        page counter with a short-page stop when the header is absent."""
        items: list = []
        page = 1
        pages_fetched = 0
        # Wall-clock deadline alongside the page cap (n4/audit-3): a server
        # that keeps answering, just slowly, would never trip the page cap
        # but could still hold a run open indefinitely. Mirrors
        # jira_client.py's MAX_PAGINATION_SECONDS deadline.
        deadline = time.monotonic() + _MAX_PAGINATION_SECONDS
        while True:
            if time.monotonic() > deadline:
                raise GitLabError(
                    op,
                    f"pagination did not finish within {_MAX_PAGINATION_SECONDS:.0f}s fetching {path} "
                    "(server may be slow or ignoring `page`)",
                    code="UNREACHABLE",
                )
            page_params = dict(params or {})
            page_params["page"] = page
            page_params["per_page"] = per_page
            data, headers = self._do(op, path, page_params)
            batch = json.loads(data) if data else []
            if not isinstance(batch, list):
                raise GitLabError(op, f"expected a list response from {path} (page {page})")
            items.extend(batch)
            pages_fetched += 1
            if pages_fetched > _MAX_PAGINATION_PAGES:
                raise GitLabError(
                    op, f"exceeded {_MAX_PAGINATION_PAGES} pages fetching {path} — a server ignoring `page` could loop forever"
                )

            next_page = headers.get("x-next-page") or ""
            if next_page.strip().isdigit() and int(next_page) > page:
                page = int(next_page)
            elif not next_page and len(batch) >= per_page:
                page += 1
            else:
                break
        return items

    # -- project / user lookups ---------------------------------------------

    def project_id(self, project_path: str) -> Optional[int]:
        """Raises GitLabError on any failure (B1) — a revoked token must never
        look like a project that resolved to zero MRs. Callers (fetch_team_data)
        decide what to do with each error code: AUTH_FAILED propagates and fails
        the whole run, everything else is per-project and can be disclaimed."""
        encoded = urllib.parse.quote(project_path, safe="")
        data = self._get_json("ProjectID", f"/api/v4/projects/{encoded}")
        return (data or {}).get("id")

    def current_user(self) -> dict:
        """GET /api/v4/user — the token's own identity. Validates reachability
        and auth without needing any configured project (used by the `check`
        CLI command's GitLab connectivity probe)."""
        return self._get_json("CurrentUser", "/api/v4/user") or {}

    # -- merge requests -------------------------------------------------------

    def _mr_list(self, project_id: int, author: str, state: str, window: Optional[Window]) -> list:
        params: dict = {"state": state, "author_username": author}
        if window is not None:
            params.update(window.params("created_after", "created_before"))
        return self._get_paginated(
            "ListMergeRequests", f"/api/v4/projects/{_qs(project_id)}/merge_requests", params, per_page=MR_PAGE_SIZE
        )

    def _mr_detail(self, project_id: int, mr_iid) -> dict:
        if not mr_iid:
            return {}
        try:
            return (
                self._get_json(
                    "GetMergeRequest", f"/api/v4/projects/{_qs(project_id)}/merge_requests/{_qs(mr_iid)}"
                )
                or {}
            )
        except GitLabError:
            return {}

    def _mr_commit_count(self, project_id: int, mr_iid) -> Optional[int]:
        """Dedicated commits endpoint — more reliable than hoping the MR
        resource carries a `commits`/`commits_count` field (bug fix: the
        source skill only ever read `mr.get("commits")` off the list row,
        which GitLab does not populate there)."""
        if not mr_iid:
            return None
        try:
            commits = self._get_paginated(
                "ListMRCommits", f"/api/v4/projects/{_qs(project_id)}/merge_requests/{_qs(mr_iid)}/commits", per_page=100
            )
        except GitLabError:
            return None
        return len(commits)

    def _build_mr_record(self, project_path: str, project_id: int, mr: dict, *, fetch_details: bool = True) -> dict:
        created_at = mr.get("created_at")
        merged_at = mr.get("merged_at") or mr.get("closed_at")
        cycle_seconds = _cycle_time_seconds(created_at, merged_at)

        additions = _int_or_none(mr.get("additions"))
        deletions = _int_or_none(mr.get("deletions"))
        changes_count, changes_count_capped = _changes_count(mr.get("changes_count"))
        commits_count = _int_or_none(mr.get("commits"))

        # GitLab's list endpoint documents none of additions/deletions/
        # changes_count/commit-count on the MR resource (only the single-MR
        # endpoint documents changes_count, and even that is best-effort —
        # see GitLab issue gitlab-org/gitlab#464260). Fall back to the detail
        # endpoint, then to a dedicated commits fetch; never assume 0.
        #
        # fetch_details=False (audit finding 2, opt-out for the N+1 fan-out:
        # one detail request + one commits request PER MR) skips both
        # fallbacks entirely — whatever the list row already carried is all
        # this record gets. The *_available flags below already exist for
        # exactly this "genuinely missing, not zero" case, so opting out
        # degrades to the same UNAVAILABLE+warning path callers already use
        # when GitLab's list response itself omits a field.
        if fetch_details and (additions is None or deletions is None or changes_count is None):
            detail = self._mr_detail(project_id, mr.get("iid"))
            if additions is None:
                additions = _int_or_none(detail.get("additions"))
            if deletions is None:
                deletions = _int_or_none(detail.get("deletions"))
            if changes_count is None:
                changes_count, changes_count_capped = _changes_count(detail.get("changes_count"))
            if commits_count is None:
                commits_count = _int_or_none(detail.get("commits"))

        diff_stats_available = additions is not None and deletions is not None

        if fetch_details and commits_count is None:
            commits_count = self._mr_commit_count(project_id, mr.get("iid"))
        commits_count_available = commits_count is not None

        title = mr.get("title") or ""
        description = mr.get("description") or ""
        source_branch = mr.get("source_branch") or ""
        candidates = _jira_key_candidates(f"{title} {description} {source_branch}")
        jira_key = _best_jira_key(candidates)

        return {
            "project": project_path,
            "project_id": project_id,
            "mr_id": mr.get("iid"),
            "title": title,
            "author": (mr.get("author") or {}).get("username"),
            "state": mr.get("state"),
            "web_url": mr.get("web_url"),
            "source_branch": source_branch,
            "created_at": created_at,
            "merged_at": merged_at,
            "cycle_time_seconds": cycle_seconds,
            "cycle_time_hours": _hours(cycle_seconds),
            "additions": additions,
            "deletions": deletions,
            "diff_stats_available": diff_stats_available,
            "commits_count": commits_count,
            "commits_count_available": commits_count_available,
            "changes_count": changes_count,
            "changes_count_available": changes_count is not None,
            "changes_count_capped": changes_count_capped,
            # Best-guess single key (first regex match not in the
            # false-positive denylist) plus every raw match, so a wiring
            # layer holding the real issue-key list can validate/
            # disambiguate (m5). `linked_tasks` in personal_metrics.py
            # counts distinct values of THIS field, not of the candidates.
            "jira_key": jira_key,
            "jira_key_candidates": candidates,
        }

    def merge_requests(
        self,
        project_path: str,
        project_id: int,
        authors: list,
        *,
        states: tuple = MR_STATES,
        window: Optional[Window] = None,
        errors: Optional[list] = None,
        fetch_mr_details: bool = True,
    ) -> list:
        """MRs authored by each of `authors`, in each of `states`, fanned out
        across a bounded thread pool (one task per author x state pair).

        A per-(author,state) failure no longer aborts the whole call (m3) —
        one renamed/deleted login must not discard everyone else's data.
        AUTH_FAILED is the one code that still propagates (B1): a bad token
        is never a legitimate "0 MRs" for that pair. When `errors` is given,
        tolerated failures are appended to it as
        `{"project","author","state","code","message"}`.

        `fetch_mr_details=False` (audit finding 2a) skips the per-MR detail
        GET and the per-MR commits GET for every MR this call would
        otherwise build — the single largest request-volume driver in this
        client (one request per MR, twice, on top of the list fetch). The
        affected fields degrade to their existing UNAVAILABLE+flag
        representation (`diff_stats_available`, `commits_count_available`,
        `changes_count_available`), never a fabricated 0 — see
        `_build_mr_record`.
        """
        tasks = [(a, s) for a in authors for s in states]
        results: list = [None] * len(tasks)

        def work(i: int, author: str, state: str) -> None:
            try:
                rows = self._mr_list(project_id, author, state, window)
            except GitLabError as e:
                if e.code == "AUTH_FAILED":
                    raise
                if errors is not None:
                    errors.append(
                        {"project": project_path, "author": author, "state": state, "code": e.code, "message": e.message}
                    )
                results[i] = []
                return
            results[i] = [
                self._build_mr_record(project_path, project_id, mr, fetch_details=fetch_mr_details) for mr in rows
            ]

        if tasks:
            with ThreadPoolExecutor(max_workers=self.max_workers) as ex:
                futures = [ex.submit(work, i, a, s) for i, (a, s) in enumerate(tasks)]
                for f in futures:
                    f.result()

        records: list = []
        for r in results:
            records.extend(r or [])
        return records

    # -- pipelines -------------------------------------------------------------

    def _pipeline_list(self, project_id: int, window: Optional[Window]) -> list:
        # Filtered on updated_after/updated_before — faithful to the source
        # skill — even though team_pipeline_metrics (engineering_metrics.py)
        # buckets its per-week frequency on created_at. That mismatch is
        # inherited from the source skill (SPEC.md M5) and left as-is here;
        # only deployments' filter/bucket pair was made mutually consistent.
        params: dict = {}
        if window is not None:
            params.update(window.params("updated_after", "updated_before"))
        return self._get_paginated(
            "ListPipelines", f"/api/v4/projects/{_qs(project_id)}/pipelines", params, per_page=PIPELINE_PAGE_SIZE
        )

    def _pipeline_job_user(self, project_id: int, pipeline_id) -> tuple:
        """Single-request lookup (page=1&per_page=1, the same technique
        coverage() uses) — M4 fix: this used to feed JOBS_PAGE_SIZE=1 into
        `_get_paginated`, which walks every job page just to keep jobs[0]
        (~15,000 requests for 500 pipelines with 30 jobs each)."""
        try:
            jobs = (
                self._get_json(
                    "ListPipelineJobs",
                    f"/api/v4/projects/{_qs(project_id)}/pipelines/{_qs(pipeline_id)}/jobs",
                    {"page": 1, "per_page": JOBS_PAGE_SIZE},
                )
                or []
            )
        except GitLabError:
            return "", ""
        if not jobs:
            return "", ""
        user = jobs[0].get("user") or {}
        return user.get("username") or "", user.get("name") or ""

    def pipelines(
        self, project_path: str, project_id: int, *, window: Optional[Window] = None, fetch_pipeline_user: bool = True
    ) -> list:
        """`fetch_pipeline_user=False` (audit finding 2a) skips the per-
        pipeline jobs GET this uses to attribute a pipeline to a person —
        one request per pipeline, the second-largest volume driver in this
        client. GitLab's pipeline LIST response does not carry a `user`
        field at all (verified against GitLab's own API docs — only the
        single-pipeline detail endpoint does), so this fan-out cannot be
        eliminated outright; it can only be made optional. Skipped records
        get `user_username`/`user_name` = "" and `user_lookup_available =
        False`, distinct from "looked up but genuinely attributed to
        nobody" (`user_lookup_available = True` with empty strings) — a
        consumer must not read a bare empty string as "0 pipelines by
        anyone" either way."""
        raw = self._pipeline_list(project_id, window)
        user_by_pipeline: dict = {}
        if raw and fetch_pipeline_user:
            with ThreadPoolExecutor(max_workers=self.max_workers) as ex:
                futures = {ex.submit(self._pipeline_job_user, project_id, p.get("id")): p.get("id") for p in raw}
                for fut, pid in futures.items():
                    user_by_pipeline[pid] = fut.result()

        records = []
        for p in raw:
            username, name = user_by_pipeline.get(p.get("id"), ("", ""))
            records.append(
                {
                    "project": project_path,
                    "project_id": project_id,
                    "pipeline_id": p.get("id"),
                    "ref": p.get("ref"),
                    "sha": p.get("sha"),
                    "status": p.get("status"),
                    "created_at": p.get("created_at"),
                    "updated_at": p.get("updated_at"),
                    "web_url": p.get("web_url"),
                    "user_username": username,
                    "user_name": name,
                    "user_lookup_available": fetch_pipeline_user,
                }
            )
        return records

    # -- deployments -------------------------------------------------------------

    def deployments(self, project_path: str, project_id: int, *, window: Optional[Window] = None) -> list:
        """Filtered on finished_after/finished_before (M5) — GitLab
        documents these specifically for "a deployment finished in this
        window", which is what an analysis window is meant to express;
        updated_after/before would also match a deployment merely edited
        (e.g. retried) during the window without actually finishing in it.
        Bug fix vs the source skill: deployments were never window-filtered
        there at all even though the endpoint supports it."""
        params: dict = {}
        if window is not None:
            params.update(window.params("finished_after", "finished_before"))
        raw = self._get_paginated(
            "ListDeployments", f"/api/v4/projects/{_qs(project_id)}/deployments", params, per_page=DEPLOYMENT_PAGE_SIZE
        )
        records = []
        for d in raw:
            user = d.get("user") or {}
            env = d.get("environment") or {}
            records.append(
                {
                    "project": project_path,
                    "project_id": project_id,
                    "deployment_id": d.get("id"),
                    "status": d.get("status"),
                    "environment": env.get("name"),
                    "ref": d.get("ref"),
                    "sha": d.get("sha"),
                    "created_at": d.get("created_at"),
                    "finished_at": d.get("finished_at"),
                    "web_url": d.get("web_url"),
                    "user_username": user.get("username") or "",
                    "user_name": user.get("name") or "",
                }
            )
        return records

    # -- coverage -------------------------------------------------------------

    def coverage(self, project_path: str, project_id: int, *, window: Optional[Window] = None) -> list:
        """Coverage of the most recent successful pipeline in the (optionally
        windowed) range. Bug fix vs the source skill: window was never applied
        here either, even though it reuses the pipelines endpoint."""
        params: dict = {"status": "success", "page": 1, "per_page": 1}
        if window is not None:
            params.update(window.params("updated_after", "updated_before"))
        path = f"/api/v4/projects/{_qs(project_id)}/pipelines"
        raw = self._get_json("ListPipelinesForCoverage", path, params) or []
        if not isinstance(raw, list):
            raise GitLabError("ListPipelinesForCoverage", f"expected a list response from {path}")

        records = []
        for p in raw[:1]:
            pipeline_id = p.get("id")
            try:
                detail = (
                    self._get_json("GetPipeline", f"/api/v4/projects/{_qs(project_id)}/pipelines/{_qs(pipeline_id)}")
                    or {}
                )
            except GitLabError:
                continue
            records.append(
                {
                    "project": project_path,
                    "project_id": project_id,
                    "pipeline_id": pipeline_id,
                    "ref": p.get("ref"),
                    "coverage": _float_or_none(detail.get("coverage")),
                    "created_at": p.get("created_at"),
                }
            )
        return records


# --------------------------------------------------------------------------
# Top-level orchestration
# --------------------------------------------------------------------------


def fetch_team_data(
    client: GitLabClient,
    *,
    projects: list,
    employees: list,
    window: Optional[Window] = None,
    fetch_mr_details: bool = True,
    fetch_pipeline_user: bool = True,
) -> dict:
    """Resolves every project path to an id, then fetches merge requests
    (per employee, per state), pipelines, deployments and coverage for each
    project.

    AUTH_FAILED from project_id() propagates and fails the whole run (B1) —
    a revoked token must never look like a clean all-zero report. Every
    other per-project resolution error (NOT_FOUND, etc.) is collected into
    `skipped_projects` instead of silently dropping the project, so a report
    layer can disclaim exactly what was skipped and why. Per-MR-author
    fetch failures are collected the same way into `mr_fetch_errors` (m3).

    `fetch_mr_details`/`fetch_pipeline_user` (audit finding 2a) pass straight
    through to `merge_requests()`/`pipelines()` — set either to False to
    drop that fan-out for the whole run; see their docstrings for what
    degrades and how it's marked unavailable rather than faked as zero.

    `request_count` (audit finding 2b) is the number of actual HTTP round
    trips this call made (including retries) — a request-count delta over
    `client.request_count`, so a caller reusing the same client across
    multiple runs still gets a figure scoped to just this call. `client` is
    accepted duck-typed (tests elsewhere pass fakes that only implement the
    fetch methods): a client without a `request_count` attribute reports 0
    rather than raising, and `fetch_mr_details`/`fetch_pipeline_user` are
    only ever forwarded to `client.merge_requests()`/`client.pipelines()`
    when the caller actually opts out (False) — left at their True default,
    a duck-typed fake built against the pre-opt-out signature still works
    unchanged.
    """
    request_count_before = getattr(client, "request_count", 0)

    project_ids: dict = {}
    skipped_projects: list = []
    for path in projects:
        try:
            pid = client.project_id(path)
        except GitLabError as e:
            if e.code == "AUTH_FAILED":
                raise
            skipped_projects.append({"project": path, "code": e.code, "message": e.message})
            continue
        if pid is None:
            skipped_projects.append({"project": path, "code": "NOT_FOUND", "message": "project id not returned"})
            continue
        project_ids[path] = pid

    merge_requests: list = []
    pipelines: list = []
    deployments: list = []
    coverage: list = []
    mr_fetch_errors: list = []
    mr_kwargs: dict = {"window": window, "errors": mr_fetch_errors}
    if not fetch_mr_details:
        mr_kwargs["fetch_mr_details"] = fetch_mr_details
    pipeline_kwargs: dict = {"window": window}
    if not fetch_pipeline_user:
        pipeline_kwargs["fetch_pipeline_user"] = fetch_pipeline_user

    for path, pid in project_ids.items():
        merge_requests.extend(client.merge_requests(path, pid, employees, **mr_kwargs))
        pipelines.extend(client.pipelines(path, pid, **pipeline_kwargs))
        deployments.extend(client.deployments(path, pid, window=window))
        coverage.extend(client.coverage(path, pid, window=window))

    applied = window is not None
    return {
        "merge_requests": merge_requests,
        "pipelines": pipelines,
        "deployments": deployments,
        "coverage": coverage,
        "skipped_projects": skipped_projects,
        "mr_fetch_errors": mr_fetch_errors,
        "request_count": getattr(client, "request_count", 0) - request_count_before,
        "window_applied": {
            "merge_requests": applied,
            "pipelines": applied,
            "deployments": applied,
            "coverage": applied,
        },
    }
