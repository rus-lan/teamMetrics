"""Per-person GitLab + Jira metrics — the personal-metrics half of the
`ai-metrics` skill port (`.research/ai-integration-metrics/SPEC.md` §5.1,
§5.4, §5.8, §5.10).

INPUT CONTRACT (this module fetches nothing itself — SPEC.md's own note:
"the tool makes ONE Jira pass for both halves"):

- `mrs`: the `merge_requests` list gitlab_client.fetch_team_data()/
  GitLabClient.merge_requests() returns (plain dicts, keys documented there —
  `author`, `state`, `created_at`, `merged_at`, `cycle_time_hours`,
  `additions`/`deletions`/`diff_stats_available`, `changes_count`/
  `changes_count_available`, `commits_count`/`commits_count_available`,
  `jira_key`).
- `issues`: a list of `JiraIssueInput` (this module's own dataclass, below).
  The sibling Jira module (`jira_client.py`/`model.py`) already fetches
  everything needed to build one per Jira issue — the caller adapts
  `jira_client.IssueFacts` like this:

    JiraIssueInput(
        key=fact.key,
        issuetype=fact.type,
        assignee=fact.assignee,
        story_points=fact.story_points,      # CURRENT field value — NOT
                                              # model.story_points_at(...),
                                              # which is the sprint module's
                                              # end-of-membership value; the
                                              # ai-metrics spec wants the
                                              # value at fetch time (SPEC.md
                                              # §5.1).
        qa_estimation=fact.qa_estimation,
        resolutiondate=fact.resolutiondate,  # tz-aware UTC; done_at fallback
                                              # when no changelog transition
                                              # into a final status exists
                                              # (M1) — jira_collector.py's
                                              # `done_dt = flow["done_at_dt"]
                                              # or resolved`.
        status_events=[StatusEvent(at=ch.at, to_status=ch.to_name) for ch in fact.status_history],
    )

  `fact.status_history` already has changelog-truncation handled upstream
  (`jira_client._issue_from_dto` re-reads the full changelog when the
  embedded one is truncated) — this module never needs to re-fetch anything.
- `sprints`: a list of `Sprint` (this module's own dataclass) for the
  per-sprint breakdown, adapted from the Jira module's `jira_client.Sprint`:
  `Sprint(name=s.name, start=s.start_at, end=s.end_at)`.
- `pipelines` (optional): the flat `pipelines` list gitlab_client's
  GitLabClient.pipelines()/fetch_team_data() returns, only used for the
  per-person pipeline-success share (`personal_pipeline_success`, m6). When
  every record's `user_lookup_available` is False (gitlab_client.py's
  `fetch_pipeline_user=False` opt-out), `pipeline_success_rate` is None for
  everyone AND `WARN_PIPELINE_SUCCESS_UNAVAILABLE` is added to
  `person["warnings"]`, so a renderer can show "нет данных" instead of a
  bare dash indistinguishable from "genuinely zero pipelines".

Source-skill bug NOT ported: if no sprint scope is given, the source skill's
JQL silently falls back to "every issue in a final status assigned to the
employees, instance-wide" (SPEC.md GAPS §11.3). This module refuses that
class of mistake at the one point it can enforce it — `personal_sprint_breakdown`
always requires a non-empty `sprints` list (there is no other way to bucket a
per-sprint breakdown), and `build_personal_report` raises ValueError only
when BOTH `sprints` is empty AND no explicit `date_window` was given (m8):
a caller that already scoped `mrs`/`issues` to an explicit date window
upstream has a legitimate reason to skip the per-sprint breakdown, and must
not be blocked by the same guard that exists to catch a truly unscoped,
instance-wide fetch.

Every ratio is computed with the same "0 + warning, never crash or NaN"
discipline the sibling `model.py`/`metrics.py` use (`WARN_DIVISION_BY_ZERO`).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional

WARN_DIVISION_BY_ZERO = "WARN_DIVISION_BY_ZERO"
WARN_DIFF_STATS_UNAVAILABLE = "WARN_DIFF_STATS_UNAVAILABLE"
WARN_COMMITS_UNAVAILABLE = "WARN_COMMITS_UNAVAILABLE"
WARN_CHANGES_COUNT_UNAVAILABLE = "WARN_CHANGES_COUNT_UNAVAILABLE"
WARN_MR_CYCLE_TIME_UNAVAILABLE = "WARN_MR_CYCLE_TIME_UNAVAILABLE"
WARN_TASK_CYCLE_TIME_UNAVAILABLE = "WARN_TASK_CYCLE_TIME_UNAVAILABLE"
WARN_PIPELINE_SUCCESS_UNAVAILABLE = "WARN_PIPELINE_SUCCESS_UNAVAILABLE"

# Source skill's jira_collector.py constants (SPEC.md §8), ported verbatim —
# these are the ai-metrics spec's own vocabulary, distinct from (and not to
# be confused with) the sibling sprint-metrics module's status *categories*.
FINAL_STATUSES_DEFAULT = (
    "To Test",
    "Ready for Initial Test",
    "Done",
    "Closed",
    "Ready to Deploy",
)
START_WORK_STATUSES = frozenset(
    {
        "in progress",
        "inprogress",
        "development",
        "в работе",
        "разработка",
        "разработка/в работе",
    }
)
REOPEN_STATUSES = frozenset({"reopened", "reopen", "открыто повторно"})


# --------------------------------------------------------------------------
# Input types
# --------------------------------------------------------------------------


def _require_utc(value: Optional[datetime], field_name: str) -> Optional[datetime]:
    """Raise a clear, field-named error on a naive datetime (m7) instead of
    letting a later comparison fail with a bare "can't compare offset-naive
    and offset-aware datetimes" TypeError.

    Only awareness is enforced, not a literal zero UTC offset: the Jira
    wiring this module documents as its input contract
    (jira_client.py's `parse_jira_time`) keeps each timestamp's ORIGINAL
    Jira-server offset (e.g. `+03:00`) rather than normalizing it to `Z` —
    Python compares/subtracts differently-offset aware datetimes correctly,
    so rejecting a valid non-UTC-but-aware value here would just trade one
    spurious crash for another."""
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware (got a naive datetime): {value!r}")
    return value


@dataclass(frozen=True)
class StatusEvent:
    """One changelog transition into a status. `at` must be tz-aware UTC."""

    at: datetime
    to_status: str

    def __post_init__(self):
        _require_utc(self.at, "StatusEvent.at")


@dataclass
class JiraIssueInput:
    key: str
    issuetype: str
    assignee: str
    story_points: Optional[float] = None
    qa_estimation: Optional[float] = None
    status_events: list = field(default_factory=list)  # list[StatusEvent], any order
    resolutiondate: Optional[datetime] = None  # tz-aware UTC; done_at fallback (M1)

    def __post_init__(self):
        _require_utc(self.resolutiondate, "JiraIssueInput.resolutiondate")


@dataclass(frozen=True)
class Sprint:
    """Inclusive [start, end] window, e.g. a resolved Jira sprint's dates.

    CONTRACT (B2): `end` is the raw sprint end exactly as the caller has it
    — end-of-day (23:59:59) is applied internally via `end_of_day` wherever
    `end` is used as an upper bound, matching the source skill's
    `end.replace(hour=23, minute=59, second=59)` (report_generator.py).
    """

    name: str
    start: datetime
    end: datetime

    def __post_init__(self):
        _require_utc(self.start, "Sprint.start")
        _require_utc(self.end, "Sprint.end")

    @property
    def end_of_day(self) -> datetime:
        return self.end.replace(hour=23, minute=59, second=59, microsecond=0)


@dataclass
class IssueFlow:
    key: str
    first_in_progress: Optional[datetime]
    done_at: Optional[datetime]  # transition-based, falls back to resolutiondate (M1)
    cycle_time_hours: Optional[float]  # ALWAYS from the transition-based done_at, never the resolutiondate fallback
    rework_count: int
    is_bug: bool

    @property
    def is_done(self) -> bool:
        return self.done_at is not None


# --------------------------------------------------------------------------
# Small numeric helpers — never raise, never produce NaN
# --------------------------------------------------------------------------


def _avg(values: Iterable[Optional[float]]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return round(statistics.mean(vals), 2) if vals else None


def _median(values: Iterable[Optional[float]]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return round(statistics.median(vals), 2) if vals else None


def _sum_or_none(values: Iterable[Optional[float]]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return round(sum(vals), 2) if vals else None


def _safe_div(num: float, den: float) -> Optional[float]:
    if not den:
        return None
    return round(num / den, 3)


def _pct_or_zero(num: float, den: float, warnings: list, metric_name: str) -> float:
    """Percentage (0-100). Zero/absent denominator -> 0.0 + a warning entry,
    matching the sibling model.py's WARN_DIVISION_BY_ZERO convention —
    required so callers (and their tests) never see a crash or a NaN."""
    if not den:
        warnings.append(f"{WARN_DIVISION_BY_ZERO}:{metric_name}")
        return 0.0
    return round(num / den * 100.0, 1)


def _hours(start: Optional[datetime], end: Optional[datetime]) -> Optional[float]:
    if start is None or end is None:
        return None
    delta = (end - start).total_seconds()
    if delta < 0:
        return None
    return round(delta / 3600.0, 2)


def _parse_iso(s) -> Optional[datetime]:
    if not s:
        return None
    text = str(s).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _in_window(dt: Optional[datetime], start: datetime, end: datetime) -> bool:
    return dt is not None and start <= dt <= end


# --------------------------------------------------------------------------
# Per-issue flow: cycle time + rework (SPEC.md §5.1)
# --------------------------------------------------------------------------


def _first_entry(events: list, statuses: frozenset) -> Optional[datetime]:
    """Date of the FIRST transition into any of `statuses`. `events` must
    already be chronologically ascending (the first match wins — a later,
    repeat entry into the same status must not overwrite it)."""
    for ev in events:
        if ev.to_status and ev.to_status.strip().lower() in statuses:
            return ev.at
    return None


def _rework_count(events: list) -> int:
    """Repeat entries into a start-work status (2nd and later) + every entry
    into a reopen status — ported from jira_collector.py's _rework_count."""
    count = 0
    in_progress_seen = False
    for ev in events:
        low = (ev.to_status or "").strip().lower()
        if low in START_WORK_STATUSES:
            if in_progress_seen:
                count += 1
            in_progress_seen = True
        elif low in REOPEN_STATUSES:
            count += 1
    return count


def compute_issue_flow(issue: JiraIssueInput, final_statuses: Iterable[str] = FINAL_STATUSES_DEFAULT) -> IssueFlow:
    final_lower = frozenset(s.strip().lower() for s in final_statuses if s.strip())
    events = sorted(issue.status_events, key=lambda e: e.at)
    first_in_progress = _first_entry(events, START_WORK_STATUSES)
    transition_done_at = _first_entry(events, final_lower)
    # M1: fall back to resolutiondate when the issue never shows a
    # changelog-tracked transition into a final status (e.g. it was created
    # directly in a final status) — ported from jira_collector.py's
    # `done_dt = flow["done_at_dt"] or resolved`. Cycle time keeps using the
    # transition-only value: a resolutiondate with no In-Progress/final
    # transition still has no computable cycle time.
    done_at = transition_done_at or issue.resolutiondate
    return IssueFlow(
        key=issue.key,
        first_in_progress=first_in_progress,
        done_at=done_at,
        cycle_time_hours=_hours(first_in_progress, transition_done_at),
        rework_count=_rework_count(events),
        is_bug=(issue.issuetype or "").strip().lower() == "bug",
    )


# --------------------------------------------------------------------------
# Per-person pipeline success (SPEC.md §5.12 last note; m6)
# --------------------------------------------------------------------------


def personal_pipeline_success(user: str, pipelines: list) -> Optional[float]:
    """Share (0-1) of this person's pipelines with status == "success", keyed
    by `user_username` (gitlab_client.py's pipeline job-user lookup). None
    when there is no pipeline data at all, or none attributed to this user —
    never a fabricated 0.0."""
    if not pipelines:
        return None
    user_pipes = [p for p in pipelines if p.get("user_username") == user]
    if not user_pipes:
        return None
    successful = sum(1 for p in user_pipes if p.get("status") == "success")
    return _safe_div(successful, len(user_pipes))


def _pipeline_user_attribution_missing(pipelines: list) -> bool:
    """True when pipeline data exists but its per-user attribution was
    never collected — gitlab_client.py's `pipelines(fetch_pipeline_user=
    False)` opt-out (audit finding 2a) sets every record's
    `user_lookup_available` to False, which makes `personal_pipeline_success`
    return None for EVERYONE the same way it would for a person who
    genuinely triggered zero pipelines. Those two states must stay
    distinguishable: "we did not ask" is not "the answer is zero". A record
    with no `user_lookup_available` key at all (older/hand-built data) is
    treated as available, matching pre-opt-out behaviour.

    Deliberately NOT inferred from `user_username == ""` counts or sample
    size — that's exactly the state-blurring the renderer refused to guess
    at; the signal must come from the collector's own flag."""
    return any(not p.get("user_lookup_available", True) for p in pipelines)


# --------------------------------------------------------------------------
# Per-person snapshot (SPEC.md §5.4/§5.8)
# --------------------------------------------------------------------------


def personal_metrics(
    user: str,
    *,
    mrs: list,
    issues: list,
    pipelines: Optional[list] = None,
    final_statuses: Iterable[str] = FINAL_STATUSES_DEFAULT,
) -> dict:
    """One person's metrics over whatever `mrs`/`issues`/`pipelines` were
    handed in — the caller owns scoping (date window, sprint membership,
    project set). `pipelines` is optional (m6): omit it and
    `pipeline_success_rate` is simply None."""
    warnings: list = []

    my_mrs = [m for m in mrs if m.get("author") == user]
    my_issues = [i for i in issues if i.assignee == user]

    flows = [compute_issue_flow(i, final_statuses) for i in my_issues]
    flows_by_key = {f.key: f for f in flows}
    done_flows = [f for f in flows if f.is_done]
    done_issues = [i for i in my_issues if flows_by_key[i.key].is_done]

    merged = [m for m in my_mrs if m.get("state") == "merged"]
    closed = [m for m in my_mrs if m.get("state") == "closed"]

    diff_candidates = [m for m in my_mrs if m.get("diff_stats_available")]
    diff_vals = [
        (m.get("additions") or 0) + (m.get("deletions") or 0)
        for m in diff_candidates
        if m.get("additions") is not None and m.get("deletions") is not None
    ]
    if my_mrs and not diff_candidates:
        warnings.append(WARN_DIFF_STATS_UNAVAILABLE)

    commit_candidates = [m for m in my_mrs if m.get("commits_count_available")]
    commit_vals = [m.get("commits_count") for m in commit_candidates if m.get("commits_count") is not None]
    if my_mrs and not commit_candidates:
        warnings.append(WARN_COMMITS_UNAVAILABLE)

    changes_candidates = [m for m in my_mrs if m.get("changes_count_available")]
    changes_vals = [m.get("changes_count") for m in changes_candidates if m.get("changes_count") is not None]
    if my_mrs and not changes_candidates:
        warnings.append(WARN_CHANGES_COUNT_UNAVAILABLE)

    mr_cycle_vals = [m.get("cycle_time_hours") for m in my_mrs if m.get("cycle_time_hours") is not None]
    if my_mrs and not mr_cycle_vals:
        warnings.append(WARN_MR_CYCLE_TIME_UNAVAILABLE)

    bug_flows = [f for f in done_flows if f.is_bug]

    task_cycle_vals = [f.cycle_time_hours for f in done_flows if f.cycle_time_hours is not None]
    if done_flows and not task_cycle_vals:
        warnings.append(WARN_TASK_CYCLE_TIME_UNAVAILABLE)

    # M3: tasks with at least one rework event, and their share of all done
    # tasks — report_generator.py's `rework_tasks = count with rework>0`,
    # `rework_share = rework_tasks/len(my_thru)`. Both computed only over
    # DONE flows, same population as tasks_done/rework_total.
    rework_tasks = sum(1 for f in done_flows if f.rework_count > 0)

    sp_vals = [i.story_points for i in done_issues if i.story_points is not None]
    qa_vals = [i.qa_estimation for i in done_issues if i.qa_estimation is not None]

    linked_keys = {m.get("jira_key") for m in my_mrs if m.get("jira_key")}
    mr_with_key = sum(1 for m in my_mrs if m.get("jira_key"))

    pipelines = pipelines or []
    if _pipeline_user_attribution_missing(pipelines):
        warnings.append(WARN_PIPELINE_SUCCESS_UNAVAILABLE)

    return {
        "user": user,
        "mr_count": len(my_mrs),
        "mr_merged_count": len(merged),
        "mr_closed_count": len(closed),
        "mr_merge_rate_pct": _pct_or_zero(len(merged), len(my_mrs), warnings, "mr_merge_rate_pct"),
        "mr_cycle_time_avg_hours": _avg(mr_cycle_vals),
        "mr_cycle_time_median_hours": _median(mr_cycle_vals),
        "mr_diff_size_avg": _avg(diff_vals),
        "mr_diff_size_available_count": len(diff_vals),
        "mr_commits_avg": _avg(commit_vals),
        "mr_commits_sum": _sum_or_none(commit_vals),
        "mr_changes_count_avg": _avg(changes_vals),
        "mr_changes_count_sum": _sum_or_none(changes_vals),
        "tasks_done": len(done_flows),
        # SPEC.md §5.8's `issue_count` (len(my_cycle)) — in this data model
        # cycle/throughput rows are built from the same filtered issue set,
        # so this always equals tasks_done; kept as its own field to match
        # the source's report shape (M3).
        "issue_count": len(done_flows),
        "defect_rate_pct": _pct_or_zero(len(bug_flows), len(done_flows), warnings, "defect_rate_pct"),
        "bug_count": len(bug_flows),
        "task_cycle_time_avg_hours": _avg(task_cycle_vals),
        "task_cycle_time_median_hours": _median(task_cycle_vals),
        "rework_total": sum(f.rework_count for f in done_flows),
        "rework_tasks": rework_tasks,
        "rework_share": _safe_div(rework_tasks, len(done_flows)),
        "story_points_total": _sum_or_none(sp_vals),
        "story_points_avg": _avg(sp_vals),
        "qa_estimation_total": _sum_or_none(qa_vals),
        "qa_estimation_avg": _avg(qa_vals),
        # Distinct jira_key values regex-matched off this person's MRs
        # (gitlab_client.py's best-guess extraction, m5) — NOT validated
        # against the real Jira issue list, so a false-positive match (or a
        # key belonging to someone else's issue) can inflate this count.
        "linked_tasks": len(linked_keys),
        "mr_with_jira_key": mr_with_key,
        "mr_per_task": _safe_div(mr_with_key, len(linked_keys)),
        "pipeline_success_rate": personal_pipeline_success(user, pipelines),
        "warnings": warnings,
    }


# --------------------------------------------------------------------------
# Per-person per-sprint breakdown (SPEC.md §5.10)
# --------------------------------------------------------------------------


def _completion_sprint(done_at: Optional[datetime], sprints: list) -> Optional[Sprint]:
    """The one sprint a task's completion counts toward — candidates are
    sprints whose [start, end_of_day] cover `done_at`; ties broken by the
    earliest end date (ported from jira_collector.py's `_completion_sprint`,
    SPEC.md §5.1) so a task in overlapping sprints is never double-counted
    (M2)."""
    if done_at is None:
        return None
    candidates = [s for s in sprints if s.start <= done_at <= s.end_of_day]
    if not candidates:
        return None
    return min(candidates, key=lambda s: (s.end_of_day, s.start))


def personal_sprint_breakdown(
    user: str,
    *,
    mrs: list,
    issues: list,
    sprints: list,
    final_statuses: Iterable[str] = FINAL_STATUSES_DEFAULT,
) -> list:
    """One row per sprint the person actually closed a task in — sprints with
    no completed Jira task for this person are skipped, matching
    report_generator.py's employee_sprint_breakdown (§5.10). Each task is
    attributed to exactly one sprint via `_completion_sprint` (M2), even
    when sprint date ranges overlap. Raises ValueError on an empty
    `sprints` list: see the module docstring's note on the "unscoped
    instance-wide fetch" source bug."""
    if not sprints:
        raise ValueError(
            "personal_sprint_breakdown: no sprint scope provided — refusing to compute "
            "an unscoped per-sprint breakdown (see the source-bug note in this module's docstring)"
        )

    my_mrs = [m for m in mrs if m.get("author") == user]
    my_issues = [i for i in issues if i.assignee == user]
    flows = [compute_issue_flow(i, final_statuses) for i in my_issues]
    issues_by_key = {i.key: i for i in my_issues}

    flows_by_sprint: dict = {s: [] for s in sprints}
    for f in flows:
        if not f.is_done:
            continue
        sprint = _completion_sprint(f.done_at, sprints)
        if sprint is not None:
            flows_by_sprint[sprint].append(f)

    rows = []
    for sprint in sprints:
        done_in_sprint = flows_by_sprint[sprint]
        if not done_in_sprint:
            continue

        sp_vals = [issues_by_key[f.key].story_points for f in done_in_sprint if issues_by_key[f.key].story_points is not None]
        qa_vals = [issues_by_key[f.key].qa_estimation for f in done_in_sprint if issues_by_key[f.key].qa_estimation is not None]

        mr_rows = [
            m
            for m in my_mrs
            if _in_window(_parse_iso(m.get("merged_at") or m.get("created_at")), sprint.start, sprint.end_of_day)
        ]
        changes_vals = [
            m.get("changes_count")
            for m in mr_rows
            if m.get("changes_count_available") and m.get("changes_count") is not None
        ]

        rows.append(
            {
                "sprint": sprint.name,
                "throughput": len(done_in_sprint),
                "avg_cycle_time_hours": _avg(f.cycle_time_hours for f in done_in_sprint),
                "story_points_total": _sum_or_none(sp_vals),
                "qa_estimation_total": _sum_or_none(qa_vals),
                "rework_total": sum(f.rework_count for f in done_in_sprint),
                "mr_count": len(mr_rows),
                "avg_mr_cycle_hours": _avg(m.get("cycle_time_hours") for m in mr_rows),
                "avg_mr_changes_count": _avg(changes_vals),
            }
        )
    return rows


# --------------------------------------------------------------------------
# Top-level: every person mentioned by either data source
# --------------------------------------------------------------------------


def _all_users(mrs: list, issues: list) -> list:
    users = {m.get("author") for m in mrs if m.get("author")}
    users.update(i.assignee for i in issues if i.assignee)
    return sorted(users)


def build_personal_report(
    *,
    mrs: list,
    issues: list,
    sprints: list,
    date_window: Optional[tuple] = None,
    final_statuses: Iterable[str] = FINAL_STATUSES_DEFAULT,
) -> dict:
    """Convenience entry point: personal_metrics() for every person seen in
    `mrs`/`issues`, plus personal_sprint_breakdown() when `sprints` is given.

    Requires an explicit scope (m8): a non-empty `sprints` list, an explicit
    `date_window` (start, end) tz-aware UTC the caller already used to scope
    `mrs`/`issues` upstream, or both. Refuses only when BOTH are absent —
    that is exactly the "unscoped instance-wide fetch" the source skill's
    JQL falls back to silently (see the module docstring). When `sprints`
    is empty but `date_window` is given, each person's `sprints` breakdown
    is simply an empty list — there is nothing to bucket by.
    """
    if not sprints and date_window is None:
        raise ValueError(
            "build_personal_report: no scope provided (neither sprints nor an explicit "
            "date_window) — refusing to build an unscoped report (see the source-bug note "
            "in this module's docstring)"
        )
    if date_window is not None:
        start, end = date_window
        _require_utc(start, "date_window[0]")
        _require_utc(end, "date_window[1]")

    people = []
    for user in _all_users(mrs, issues):
        snapshot = personal_metrics(user, mrs=mrs, issues=issues, final_statuses=final_statuses)
        snapshot["sprints"] = (
            personal_sprint_breakdown(user, mrs=mrs, issues=issues, sprints=sprints, final_statuses=final_statuses)
            if sprints
            else []
        )
        people.append(snapshot)
    return {"people": people}
