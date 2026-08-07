"""Sprint and board metrics formulas.

Python port of internal/domain/metrics/metrics.go (ComputeMetrics/SMA5/
Average/ratioPct), throughput.go (BuildThroughputDaily), assemble.go
(BuildPayload) and internal/app/board/report.go (buildKpi + the two
recoverable-from-payload board warnings). Formulas are the canon (SPEC §4/§7)
and must not be "improved" — every division-by-zero yields 0 plus
WARN_DIVISION_BY_ZERO, never a crash or NaN.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from . import model

SCHEMA_VERSION = 3


def ratio_pct(num: float, den: float) -> tuple[float, str]:
    if den <= 0:
        return 0.0, model.WARN_DIVISION_BY_ZERO
    return num / den * 100.0, ""


def sma5(history: list[float]) -> tuple[float, str]:
    """Mean of at most 5 strictly-preceding CLOSED sprints (SPEC velocity_sma5)."""
    if not history:
        return 0.0, model.WARN_DIVISION_BY_ZERO
    avg = sum(history) / len(history)
    if len(history) < 5:
        return avg, model.WARN_BASELINE_SHORT
    return avg, ""


def average(values: list[float]) -> tuple[float, bool]:
    if not values:
        return 0.0, False
    return sum(values) / len(values), True


@dataclass
class Metrics:
    committed_sp: float = 0.0
    committed_items: int = 0
    delivered_sp: float = 0.0
    delivered_items: int = 0
    performance_pct: float = 0.0
    load_pct: float = 0.0
    scope_added_sp: float = 0.0
    scope_added_items: int = 0
    scope_removed_sp: float = 0.0
    scope_removed_items: int = 0
    scope_estimation_change_sp: float = 0.0
    scope_change_pct: float = 0.0
    velocity_sp: float = 0.0
    velocity_sma5_sp: float = 0.0
    throughput_items: int = 0
    closure_pct_items: float = 0.0
    closure_pct_sp: float = 0.0


@dataclass
class ThroughputDay:
    date: str
    count: int


def _throughput_day_for(issue: model.Issue, cfg: model.StatusCategoryConfig) -> tuple[str, bool]:
    """Working day the issue made its LAST transition into done (SPEC §4.2)."""
    if not issue.delivered:
        return "", False
    initial_category, _ = model.effective_status_category(issue.status_initial, cfg)
    prev_done = model.is_done_category(initial_category)

    last_enter = ""
    for ds in issue.day_statuses:
        cur_done = ds.status_category == model.CATEGORY_DONE
        if cur_done and not prev_done:
            last_enter = ds.date
        prev_done = cur_done
    if last_enter == "":
        return "", False
    return last_enter, True


def build_throughput_daily(issues: list[model.Issue], cfg: model.StatusCategoryConfig) -> list[ThroughputDay]:
    counts: dict[str, int] = {}
    order: list[str] = []
    for is_ in issues:
        date, ok = _throughput_day_for(is_, cfg)
        if not ok:
            continue
        if date not in counts:
            order.append(date)
        counts[date] = counts.get(date, 0) + 1
    order.sort()
    return [ThroughputDay(date=d, count=counts[d]) for d in order]


def compute_metrics(
    issues: list[model.Issue],
    scope_estimation_change_sp: float,
    throughput_daily: list[ThroughputDay],
    velocity_history: list[float],
    is_active: bool,
) -> tuple[Metrics, list[str]]:
    m = Metrics()
    warns: list[str] = []

    for is_ in issues:
        if is_.committed:
            m.committed_sp += is_.story_points
            m.committed_items += 1
        if is_.delivered:
            m.delivered_sp += is_.story_points
            m.delivered_items += 1
        if is_.added:
            m.scope_added_sp += is_.story_points
            m.scope_added_items += 1
        if is_.removed:
            m.scope_removed_sp += is_.story_points
            m.scope_removed_items += 1

    m.scope_estimation_change_sp = abs(scope_estimation_change_sp)

    m.performance_pct, w = ratio_pct(m.delivered_sp, m.committed_sp)
    warns.append(w)

    sma, w = sma5(velocity_history)
    m.velocity_sma5_sp = sma
    warns.append(w)

    m.load_pct, w = ratio_pct(m.committed_sp, m.velocity_sma5_sp)
    warns.append(w)

    scope_change_num = m.scope_added_sp + m.scope_estimation_change_sp + m.scope_removed_sp
    m.scope_change_pct, w = ratio_pct(scope_change_num, m.committed_sp)
    warns.append(w)

    m.velocity_sp = m.delivered_sp
    if is_active:
        warns.append(model.WARN_SPRINT_ACTIVE_PARTIAL)

    m.throughput_items = sum(td.count for td in throughput_daily)

    closure_items_den = float(m.committed_items + m.scope_added_items - m.scope_removed_items)
    m.closure_pct_items, w = ratio_pct(float(m.delivered_items), closure_items_den)
    warns.append(w)

    closure_sp_den = m.committed_sp + m.scope_added_sp - m.scope_removed_sp
    m.closure_pct_sp, w = ratio_pct(m.delivered_sp, closure_sp_den)
    warns.append(w)

    return m, model.dedupe_warnings(warns)


@dataclass
class SprintMeta:
    id: int
    name: str
    board_id: int
    board_name: str
    state: str  # active|closed
    start_at: datetime
    end_at: datetime
    complete_at: Optional[datetime]  # None for an active sprint
    working_days: list[str]


@dataclass
class Payload:
    schema_version: int
    sprint: SprintMeta
    issues: list[model.Issue]
    sets: model.Sets
    metrics: Metrics
    throughput_daily: list[ThroughputDay]


@dataclass
class SprintInput:
    id: int
    name: str
    board_id: int
    board_name: str
    state: str
    timeline: model.SprintTimeline
    complete_at: Optional[datetime]


@dataclass
class SprintIssue:
    issue: model.IssueInput
    intervals: list[model.Interval]


def _any_status_unmapped(payload_issues: list[model.Issue], cfg: model.StatusCategoryConfig) -> bool:
    for is_ in payload_issues:
        for status in (is_.status_initial, is_.status_before_end, is_.status_end):
            _, warn = model.effective_status_category(status, cfg)
            if warn:
                return True
        for ds in is_.day_statuses:
            if ds.status_category == "":
                return True
    return False


def build_payload(
    sprint: SprintInput,
    issues: list[SprintIssue],
    cfg: model.StatusCategoryConfig,
    velocity_history: list[float],
) -> tuple[Payload, list[str]]:
    """One sprint's full metrics payload (SPEC §2.1/§4) — the engine's single entry point."""
    working_days_list = model.working_days(sprint.timeline.start, sprint.timeline.observed_end())

    payload_issues: list[model.Issue] = []
    estimation_change_sum = 0.0
    for si in issues:
        role = model.classify(si.issue, si.intervals, sprint.timeline, cfg)
        if not role.in_sprint:
            continue
        payload_issues.append(model.build_issue(si.issue, si.intervals, role, sprint.timeline, working_days_list, cfg))
        if role.committed:
            estimation_change_sum += abs(model.estimation_change(si.issue, si.intervals, sprint.timeline))

    sets = model.build_sets(payload_issues)
    throughput_daily = build_throughput_daily(payload_issues, cfg)

    m, warnings = compute_metrics(
        payload_issues, estimation_change_sum, throughput_daily, velocity_history, sprint.state != "closed"
    )

    if _any_status_unmapped(payload_issues, cfg):
        warnings = model.dedupe_warnings(warnings + [model.WARN_STATUS_UNMAPPED])

    working_day_strings = [model.format_date(d) for d in working_days_list]

    payload = Payload(
        schema_version=SCHEMA_VERSION,
        sprint=SprintMeta(
            id=sprint.id,
            name=sprint.name,
            board_id=sprint.board_id,
            board_name=sprint.board_name,
            state=sprint.state,
            start_at=sprint.timeline.start,
            end_at=sprint.timeline.schedule_end,
            complete_at=sprint.complete_at,
            working_days=working_day_strings,
        ),
        issues=payload_issues,
        sets=sets,
        metrics=m,
        throughput_daily=throughput_daily,
    )
    return payload, warnings


@dataclass
class Kpi:
    velocity_sma5_sp: float = 0.0
    avg_performance_pct: float = 0.0
    avg_scope_change_pct: float = 0.0
    avg_closure_pct_items: float = 0.0
    avg_closure_pct_sp: float = 0.0
    throughput_avg_items: float = 0.0
    forecast_available: bool = False


def build_kpi(
    base_sprints: list[Payload],
    base_velocities: list[float],
    min_non_zero_points: int,
) -> tuple[Kpi, list[str]]:
    """Board KPI over base (closed, non-target) sprints only (SPEC §7)."""
    kpi = Kpi()
    warnings: list[str] = []

    sma, warn = sma5(base_velocities)
    kpi.velocity_sma5_sp = sma
    if warn:
        warnings.append(warn)

    perf: list[float] = []
    scope_change: list[float] = []
    closure_items: list[float] = []
    closure_sp: list[float] = []
    throughput: list[float] = []
    daily_throughput: list[int] = []

    for p in base_sprints:
        perf.append(p.metrics.performance_pct)
        scope_change.append(p.metrics.scope_change_pct)
        closure_items.append(p.metrics.closure_pct_items)
        closure_sp.append(p.metrics.closure_pct_sp)
        throughput.append(float(p.metrics.throughput_items))
        for td in p.throughput_daily:
            daily_throughput.append(td.count)

    v, ok = average(perf)
    if ok:
        kpi.avg_performance_pct = v
    v, ok = average(scope_change)
    if ok:
        kpi.avg_scope_change_pct = v
    v, ok = average(closure_items)
    if ok:
        kpi.avg_closure_pct_items = v
    v, ok = average(closure_sp)
    if ok:
        kpi.avg_closure_pct_sp = v
    v, ok = average(throughput)
    if ok:
        kpi.throughput_avg_items = v

    non_zero = sum(1 for x in daily_throughput if x > 0)
    kpi.forecast_available = non_zero >= min_non_zero_points

    return kpi, warnings


@dataclass
class IndividualMetrics:
    """Port of the Go module's IndividualMetrics (individual.go:12-21) — one
    assignee's slice of a single sprint's payload. Board-level values
    (load_pct, scope_change_pct, velocity_sma5_sp) have no per-assignee
    definition and are deliberately absent here (individual.go:8-11)."""

    assignee: str = ""
    committed_sp: float = 0.0
    committed_items: int = 0
    delivered_sp: float = 0.0
    delivered_items: int = 0
    performance_pct: float = 0.0
    velocity_sp: float = 0.0
    throughput_items: int = 0
    warnings: list[str] = field(default_factory=list)


def per_assignee_metrics(issues: list[model.Issue], cfg: model.StatusCategoryConfig) -> list[IndividualMetrics]:
    """Groups one sprint's payload issues by `assignee` (empty string =
    unassigned, itself a valid group — individual.go:23-24) and computes each
    group's committed/delivered/performance/velocity/throughput the same way
    compute_metrics() does for the whole sprint (SPEC §C.6)."""
    groups: dict[str, list[model.Issue]] = {}
    order: list[str] = []
    for is_ in issues:
        key = is_.assignee or ""
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(is_)

    out: list[IndividualMetrics] = []
    for assignee in sorted(order):
        group = groups[assignee]
        m = IndividualMetrics(assignee=assignee)
        for is_ in group:
            if is_.committed:
                m.committed_sp += is_.story_points
                m.committed_items += 1
            if is_.delivered:
                m.delivered_sp += is_.story_points
                m.delivered_items += 1
        m.performance_pct, w = ratio_pct(m.delivered_sp, m.committed_sp)
        if w:
            m.warnings.append(w)
        m.velocity_sp = m.delivered_sp
        throughput_daily = build_throughput_daily(group, cfg)
        m.throughput_items = sum(td.count for td in throughput_daily)
        out.append(m)
    return out


def status_unmapped_warning(sprints: list[Payload]) -> str:
    for p in sprints:
        for is_ in p.issues:
            for ds in is_.day_statuses:
                if ds.status_category == "":
                    return model.WARN_STATUS_UNMAPPED
    return ""


def sprint_active_partial_warning(sprints: list[Payload]) -> str:
    for p in sprints:
        if p.sprint.state != "closed":
            return model.WARN_SPRINT_ACTIVE_PARTIAL
    return ""
