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
    return d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    backoff min(base_backoff*2^attempt, max_backoff); other 4xx never retried.
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
        max_workers: int = DEFAULT_MAX_WORKERS,
        sleep=time.sleep,
        opener=None,
    ):
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.timeout = timeout
        self.max_retries = max_retries
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self.max_workers = max_workers
        self._sleep = sleep
        self._opener = opener or urllib.request.build_opener(_NoRedirect)
        self._sem = threading.Semaphore(max_workers)

    # -- transport ---------------------------------------------------------

    def _backoff_delay(self, attempt: int) -> float:
        d = self.base_backoff * (2**attempt)
        if d <= 0 or d > self.max_backoff:
            return self.max_backoff
        return d

    def _attempt(self, url: str):
        self._sem.acquire()
        try:
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

            self._sleep(self._backoff_delay(attempt))
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
        while True:
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

    def user_display_name(self, username: str) -> Optional[str]:
        try:
            data = self._get_json("UserByUsername", "/api/v4/users", {"username": username})
        except GitLabError:
            return None
        if data:
            return data[0].get("name") or username
        return None

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

    def _build_mr_record(self, project_path: str, project_id: int, mr: dict) -> dict:
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
        if additions is None or deletions is None or changes_count is None:
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

        if commits_count is None:
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
    ) -> list:
        """MRs authored by each of `authors`, in each of `states`, fanned out
        across a bounded thread pool (one task per author x state pair).

        A per-(author,state) failure no longer aborts the whole call (m3) —
        one renamed/deleted login must not discard everyone else's data.
        AUTH_FAILED is the one code that still propagates (B1): a bad token
        is never a legitimate "0 MRs" for that pair. When `errors` is given,
        tolerated failures are appended to it as
        `{"project","author","state","code","message"}`.
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
            results[i] = [self._build_mr_record(project_path, project_id, mr) for mr in rows]

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

    def pipelines(self, project_path: str, project_id: int, *, window: Optional[Window] = None) -> list:
        raw = self._pipeline_list(project_id, window)
        user_by_pipeline: dict = {}
        if raw:
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
    """
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
    for path, pid in project_ids.items():
        merge_requests.extend(client.merge_requests(path, pid, employees, window=window, errors=mr_fetch_errors))
        pipelines.extend(client.pipelines(path, pid, window=window))
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
        "window_applied": {
            "merge_requests": applied,
            "pipelines": applied,
            "deployments": applied,
            "coverage": applied,
        },
    }
