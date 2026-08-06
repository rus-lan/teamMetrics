"""Issue/sprint data model, changelog replay, and membership-interval math.

Python port of the Go module's internal/domain/metrics package (types.go,
classify.go, dayline.go, category.go, assemble.go, sets.go, warnings.go) plus
internal/adapters/jira/membership.go. Field names are snake_case; formulas and
edge cases are otherwise reproduced literally — see
.research/jira-metrics-source/SPEC.md for the section-by-section citations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

UTC = timezone.utc

# Sentinel matching Go's zero time.Time{} (0001-01-01T00:00:00Z). Used inside
# membership_end() for the "should not happen" empty-interval-list edge case,
# and also by jira_client.parse_optional_jira_time() to stand in for a
# missing/empty Jira date (future sprints may lack startDate/endDate) so
# downstream code always holds a real, comparable datetime — never None.
ZERO_TIME = datetime.min.replace(tzinfo=UTC)

CATEGORY_NEW = "new"
CATEGORY_INDETERMINATE = "indeterminate"
CATEGORY_DONE = "done"
CATEGORY_CANCELLED = "cancelled"

WARN_DIVISION_BY_ZERO = "WARN_DIVISION_BY_ZERO"
WARN_SPRINT_ACTIVE_PARTIAL = "WARN_SPRINT_ACTIVE_PARTIAL"
WARN_STATUS_UNMAPPED = "WARN_STATUS_UNMAPPED"
WARN_BASELINE_SHORT = "WARN_BASELINE_SHORT"


def is_epic(issue_type: str) -> bool:
    return issue_type == "Epic"


def status_equal(a: str, b: str) -> bool:
    return a.strip().casefold() == b.strip().casefold()


# --------------------------------------------------------------------------
# Changelog-derived events
# --------------------------------------------------------------------------


@dataclass
class StatusChange:
    at: datetime
    from_status: str
    to_status: str


@dataclass
class SPChange:
    at: datetime
    from_value: float
    to_value: float


@dataclass
class SprintChange:
    date: datetime
    from_ids: list[int]
    to_ids: list[int]


# --------------------------------------------------------------------------
# Membership intervals
# --------------------------------------------------------------------------


@dataclass
class Interval:
    """Half-open [from_, until). until=None means the interval is still open."""

    from_: datetime
    until: Optional[datetime] = None

    def covers(self, t: datetime) -> bool:
        return self.from_ <= t and (self.until is None or self.until > t)

    def overlaps(self, start: datetime, end: datetime) -> bool:
        return self.from_ < end and (self.until is None or self.until > start)


def build_membership_intervals(
    created: datetime,
    events: list[SprintChange],
    current_ids: list[int],
    sprint_id: int,
) -> list[Interval]:
    """Replay the Sprint-field changelog into membership intervals (SPEC §3.2).

    Edge cases: no Sprint events at all but the issue's current Sprint field
    still names sprint_id -> created in the sprint (single open interval from
    `created`); the first event's From already names sprint_id -> the issue
    was in the sprint before changelog history starts (open from `created`,
    not from the first event's date).
    """

    def contains(ids: list[int]) -> bool:
        return sprint_id in ids

    if not events:
        if contains(current_ids):
            return [Interval(from_=created, until=None)]
        return []

    out: list[Interval] = []
    in_sprint = False
    start: Optional[datetime] = None

    if contains(events[0].from_ids):
        in_sprint, start = True, created

    for ev in events:
        has = contains(ev.to_ids)
        if not in_sprint and has:
            in_sprint, start = True, ev.date
        elif in_sprint and not has:
            out.append(Interval(from_=start, until=ev.date))
            in_sprint = False

    if in_sprint:
        out.append(Interval(from_=start, until=None))
    return out


@dataclass
class PayloadInterval:
    from_: datetime
    to: datetime


def close_payload_intervals(intervals: list[Interval], tl: "SprintTimeline") -> list[PayloadInterval]:
    """Close every open membership interval at tl.observed_end() (SPEC §3.6)."""
    out = []
    for iv in intervals:
        until = iv.until if iv.until is not None else tl.observed_end()
        out.append(PayloadInterval(from_=iv.from_, to=until))
    return out


def membership_end(payload_intervals: list[PayloadInterval]) -> datetime:
    """Last moment the issue was still a sprint member (max `to`).

    Should never be called with an empty list in practice — build_issue()
    only runs after Classify() proved at least one overlapping interval — but
    mirrors Go's membershipEnd() by returning the zero-time sentinel instead
    of raising, rather than special-casing a state that "should not happen".
    """
    end = ZERO_TIME
    for iv in payload_intervals:
        if iv.to > end:
            end = iv.to
    return end


def member_at(intervals: list[Interval], t: datetime) -> bool:
    return any(iv.covers(t) for iv in intervals)


# --------------------------------------------------------------------------
# Sprint timeline / status category config / issue input
# --------------------------------------------------------------------------


@dataclass
class SprintTimeline:
    start: datetime
    schedule_end: datetime
    classify_end: datetime

    def observed_end(self) -> datetime:
        return self.schedule_end if self.schedule_end < self.classify_end else self.classify_end


@dataclass
class StatusCategoryConfig:
    status_map: dict[str, str] = field(default_factory=dict)
    cancelled_statuses: list[str] = field(default_factory=list)
    jira_status_categories: dict[str, str] = field(default_factory=dict)


def _lookup_status(m: dict[str, str], status: str) -> tuple[Optional[str], bool]:
    if status in m:
        return m[status], True
    best_key: Optional[str] = None
    best_val: Optional[str] = None
    found = False
    for k, v in m.items():
        if not status_equal(k, status):
            continue
        if not found or k < best_key:  # type: ignore[operator]
            best_key, best_val, found = k, v, True
    return best_val, found


def effective_status_category(status: str, cfg: StatusCategoryConfig) -> tuple[str, str]:
    """Status -> (category, warn). Precedence: cancelled > status_map > Jira catalog > unmapped."""
    if status == "":
        return "", ""
    for cancelled in cfg.cancelled_statuses:
        if status_equal(cancelled, status):
            return CATEGORY_CANCELLED, ""
    cat, ok = _lookup_status(cfg.status_map, status)
    if ok:
        return cat, ""  # type: ignore[return-value]
    cat, ok = _lookup_status(cfg.jira_status_categories, status)
    if ok:
        return cat, ""  # type: ignore[return-value]
    return "", WARN_STATUS_UNMAPPED


def is_done_category(category: str) -> bool:
    return category == CATEGORY_DONE


@dataclass
class IssueInput:
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
    status_history: list[StatusChange]  # sorted ascending by `at`
    sp_events: list[SPChange]  # sorted ascending by `at`


@dataclass
class IssueRole:
    in_sprint: bool = False
    committed: bool = False
    added: bool = False
    removed: bool = False
    delivered: bool = False


def classify(
    issue: IssueInput,
    intervals: list[Interval],
    tl: SprintTimeline,
    cfg: StatusCategoryConfig,
) -> IssueRole:
    """committed/added/removed/delivered classification over [tl.start, tl.classify_end) (SPEC §3.3)."""
    if not any(iv.overlaps(tl.start, tl.classify_end) for iv in intervals):
        return IssueRole()

    role = IssueRole(in_sprint=True)
    role.committed = member_at(intervals, tl.start)
    if not role.committed:
        for iv in intervals:
            if iv.from_ > tl.start and iv.from_ < tl.classify_end:
                role.added = True
                break

    at_end = member_at(intervals, tl.classify_end)
    role.removed = not at_end
    if at_end:
        status = status_at(issue.created, issue.initial_status, issue.status_history, tl.classify_end)
        category, _ = effective_status_category(status, cfg)
        role.delivered = is_done_category(category)
    return role


def estimation_change(issue: IssueInput, intervals: list[Interval], tl: SprintTimeline) -> float:
    """Net SP delta inside [tl.start, tl.classify_end), only events fired while a member (SPEC §3.8)."""
    net = 0.0
    for ev in issue.sp_events:
        if ev.at < tl.start or ev.at >= tl.classify_end:
            continue
        if not member_at(intervals, ev.at):
            continue
        net += ev.to_value - ev.from_value
    return net


# --------------------------------------------------------------------------
# Point-in-time lookups
# --------------------------------------------------------------------------


def status_at(created: datetime, initial_status: str, history: list[StatusChange], t: datetime) -> str:
    if created > t:
        return ""
    last = initial_status
    for ch in history:
        if ch.at > t:
            break
        last = ch.to_status
    return last


def story_points_at(issue: IssueInput, t: datetime) -> float:
    if not issue.sp_events:
        return issue.story_points
    value = issue.sp_events[0].from_value
    for ev in issue.sp_events:
        if ev.at > t:
            break
        value = ev.to_value
    return value


def status_initial_at(intervals: list[Interval], tl: SprintTimeline, role: IssueRole) -> datetime:
    if role.committed or not role.added:
        return tl.start
    for iv in intervals:
        if iv.from_ > tl.start and iv.from_ < tl.classify_end:
            return iv.from_
    return tl.start


def status_before_end_at(working_days_list: list[datetime], fallback: datetime) -> datetime:
    if len(working_days_list) < 2:
        return fallback
    return end_of_day(working_days_list[-2])


# --------------------------------------------------------------------------
# Working-day calendar (Mon-Fri, UTC civil days — SPEC §3.5)
# --------------------------------------------------------------------------


def civil_day(t: datetime, loc: Optional[timezone] = None) -> datetime:
    """Truncate `t` to midnight of its civil day in `loc` (defaults to `t`'s
    own tzinfo). Mirrors Go's civilDay(t, loc): t.In(loc) first, then take
    the date — callers computing a [start, end] window (working_days(),
    burndown, the forecast calendar window) must pass the SAME loc for both
    ends, or a start/end pair with different tzinfos (e.g. a caller-supplied
    `now` that isn't UTC) can shift the derived day boundary by the offset."""
    if loc is not None:
        t = t.astimezone(loc)
    else:
        loc = t.tzinfo
    return datetime(t.year, t.month, t.day, tzinfo=loc)


def end_of_day(day: datetime) -> datetime:
    return day + timedelta(days=1) - timedelta(microseconds=1)


def format_date(t: datetime) -> str:
    return t.strftime("%Y-%m-%d")


def working_days(start: datetime, end: datetime) -> list[datetime]:
    loc = start.tzinfo
    first = civil_day(start, loc)
    last = civil_day(end, loc)
    if last < first:
        return []
    out = []
    d = first
    while d <= last:
        if d.weekday() < 5:  # Mon=0 .. Fri=4; Sat=5, Sun=6 excluded
            out.append(d)
        d += timedelta(days=1)
    return out


@dataclass
class DayStatus:
    date: str
    status: str
    status_category: str


def day_statuses_for(
    issue: IssueInput,
    member_intervals: list[Interval],
    working_days_list: list[datetime],
    cfg: StatusCategoryConfig,
) -> list[DayStatus]:
    """One record per working day the issue was a member of the sprint at end-of-day."""
    out = []
    for day in working_days_list:
        at = end_of_day(day)
        if not member_at(member_intervals, at):
            continue
        status = status_at(issue.created, issue.initial_status, issue.status_history, at)
        category, _ = effective_status_category(status, cfg)
        out.append(DayStatus(date=format_date(day), status=status, status_category=category))
    return out


# --------------------------------------------------------------------------
# Per-issue payload record
# --------------------------------------------------------------------------


@dataclass
class Issue:
    key: str
    epic_key: str
    story_points: float
    qa_estimation: float
    role: str
    labels: list[str]
    assignee: str
    membership_intervals: list[PayloadInterval]
    day_statuses: list[DayStatus]
    status_initial: str
    status_before_end: str
    status_end: str
    committed: bool
    added: bool
    removed: bool
    delivered: bool


def build_issue(
    issue: IssueInput,
    intervals: list[Interval],
    role: IssueRole,
    tl: SprintTimeline,
    working_days_list: list[datetime],
    cfg: StatusCategoryConfig,
) -> Issue:
    initial_at = status_initial_at(intervals, tl, role)
    before_end_at = status_before_end_at(working_days_list, initial_at)

    status_initial = status_at(issue.created, issue.initial_status, issue.status_history, initial_at)
    status_before_end = status_at(issue.created, issue.initial_status, issue.status_history, before_end_at)
    status_end = status_at(issue.created, issue.initial_status, issue.status_history, tl.classify_end)

    membership_intervals = close_payload_intervals(intervals, tl)
    story_points = story_points_at(issue, membership_end(membership_intervals))

    return Issue(
        key=issue.key,
        epic_key=issue.epic_key,
        story_points=story_points,
        qa_estimation=issue.qa_estimation,
        role=issue.role,
        labels=list(issue.labels) if issue.labels else [],
        assignee=issue.assignee,
        membership_intervals=membership_intervals,
        day_statuses=day_statuses_for(issue, intervals, working_days_list, cfg),
        status_initial=status_initial,
        status_before_end=status_before_end,
        status_end=status_end,
        committed=role.committed,
        added=role.added,
        removed=role.removed,
        delivered=role.delivered,
    )


# --------------------------------------------------------------------------
# Sets and warnings
# --------------------------------------------------------------------------


@dataclass
class Sets:
    committed_keys: list[str] = field(default_factory=list)
    added_keys: list[str] = field(default_factory=list)
    removed_keys: list[str] = field(default_factory=list)
    delivered_keys: list[str] = field(default_factory=list)


def build_sets(issues: list[Issue]) -> Sets:
    s = Sets()
    for is_ in issues:
        if is_.committed:
            s.committed_keys.append(is_.key)
        if is_.added:
            s.added_keys.append(is_.key)
        if is_.removed:
            s.removed_keys.append(is_.key)
        if is_.delivered:
            s.delivered_keys.append(is_.key)
    return s


def dedupe_warnings(warnings: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for w in warnings:
        if not w or w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out
