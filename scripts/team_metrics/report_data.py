"""Assembles one JSON-serializable dict holding everything a report needs.

Orchestration layer: Python port of internal/app/board/{translate,service,
report,forecast}.go and the two export tables from export.go (minus the XLSX
container itself — CSV text plus a header/rows table for both, since no
xlsx writer exists in the stdlib and rendering is another agent's job).

This module never imports urllib directly — it only talks to the JiraClient
interface (jira_client.JiraClient) and the GitLabClient interface
(gitlab_client.GitLabClient), so tests can substitute any duck-typed stub
client and never touch the network.

build_report() covers the Jira sprint-metrics tab alone (unchanged public
contract). build_combined_report() wraps it and additionally wires in the
GitLab-derived personal/engineering tabs (.research/ai-integration-metrics/
SPEC.md) from the SAME Jira fetch — see the "GitLab/personal/engineering
wiring" section below.
"""

from __future__ import annotations

import dataclasses
import json
import random
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from . import burndown as burndown_mod
from . import config as config_mod
from . import engineering_metrics
from . import forecast as forecast_mod
from . import gitlab_client as glc
from . import heatmap as heatmap_mod
from . import jira_client as jc
from . import metrics as metrics_mod
from . import model
from . import personal_metrics

UTC = timezone.utc

# SMA5 window is fixed at 5 regardless of --history (SPEC §4.1) — not to be
# confused with config.MAX_HISTORY_SPRINTS (how many base sprints are
# fetched/shown). The same constant also caps Forecast's own base-sprint
# population (_pick_forecast_sprints) — Go's Service.Forecast reuses the
# identical maxBaseSprints for both purposes.
MAX_BASE_SPRINTS = 5


class ReportError(Exception):
    """User-facing input/resolution error (sprint not found, mismatched boards, ...)."""


def _sort_key(sprint: jc.Sprint) -> datetime:
    # sprint.start_at is never None (jira_client.parse_optional_jira_time
    # returns model.ZERO_TIME for a missing date), so a sprint with no
    # startDate sorts first — same place datetime.min used to put it.
    return sprint.start_at


# --------------------------------------------------------------------------
# translate.go equivalent: Jira wire shapes -> engine input
# --------------------------------------------------------------------------


def _id_to_current_name(jira_statuses: list[jc.Status]) -> dict[str, str]:
    return {s.id: s.name for s in jira_statuses if s.id}


def _canonical_status_name(status_id: str, historical: str, id_to_name: dict[str, str]) -> str:
    if not status_id:
        return historical
    return id_to_name.get(status_id, historical)


def _augment_status_category_config(
    cfg: model.StatusCategoryConfig, facts: list[jc.IssueFacts]
) -> model.StatusCategoryConfig:
    """Fold each fetched issue's own current status category into `cfg` for
    any status name the catalog doesn't already have — mirrors Go's
    toStatusCategoryConfig (internal/app/board/translate.go:20-38), which
    combines the /rest/api/2/status catalog with a per-issue observed
    category so an incomplete/unavailable catalog still classifies. Only
    fills gaps; the existing catalog always wins."""
    categories = dict(cfg.jira_status_categories)
    for f in facts:
        if not f.current_status or not f.current_status_category_key:
            continue
        categories.setdefault(f.current_status, f.current_status_category_key)
    return model.StatusCategoryConfig(
        status_map=dict(cfg.status_map),
        cancelled_statuses=list(cfg.cancelled_statuses),
        jira_status_categories=categories,
    )


def _to_status_category_config(
    status_map: dict[str, str],
    cancelled_statuses: list[str],
    jira_statuses: list[jc.Status],
    facts: list[jc.IssueFacts],
) -> model.StatusCategoryConfig:
    base = model.StatusCategoryConfig(
        status_map=dict(status_map),
        cancelled_statuses=list(cancelled_statuses),
        jira_status_categories={s.name: s.category_key for s in jira_statuses},
    )
    return _augment_status_category_config(base, facts)


def _to_sprint_timeline(sprint: jc.Sprint, now: datetime) -> model.SprintTimeline:
    classify_end = sprint.complete_at
    if sprint.state != "closed" or classify_end == model.ZERO_TIME:
        classify_end = now
    return model.SprintTimeline(start=sprint.start_at, schedule_end=sprint.end_at, classify_end=classify_end)


def _to_sprint_input(sprint: jc.Sprint, board_name: str, now: datetime) -> metrics_mod.SprintInput:
    complete_at = sprint.complete_at if sprint.state == "closed" and sprint.complete_at != model.ZERO_TIME else None
    return metrics_mod.SprintInput(
        id=sprint.id,
        name=sprint.name,
        board_id=sprint.board_id,
        board_name=board_name,
        state=sprint.state,
        timeline=_to_sprint_timeline(sprint, now),
        complete_at=complete_at,
    )


def _to_issue_input(fact: jc.IssueFacts, id_to_name: dict[str, str]) -> model.IssueInput:
    history = [
        model.StatusChange(
            at=ch.at,
            from_status=_canonical_status_name(ch.from_id, ch.from_name, id_to_name),
            to_status=_canonical_status_name(ch.to_id, ch.to_name, id_to_name),
        )
        for ch in fact.status_history
    ]
    sp_events = [model.SPChange(at=ev.at, from_value=ev.from_value, to_value=ev.to_value) for ev in fact.sp_events]
    return model.IssueInput(
        key=fact.key,
        epic_key=fact.epic_key,
        type=fact.type,
        role=fact.role,
        labels=list(fact.labels),
        assignee=fact.assignee,
        story_points=fact.story_points,
        qa_estimation=fact.qa_estimation,
        created=fact.created,
        initial_status=_canonical_status_name(fact.initial_status_id, fact.initial_status, id_to_name),
        status_history=history,
        sp_events=sp_events,
    )


def _to_sprint_issues(
    facts: list[jc.IssueFacts], sprint_id: int, id_to_name: dict[str, str]
) -> list[metrics_mod.SprintIssue]:
    out = []
    for f in facts:
        intervals = f.membership_by_sprint.get(sprint_id) or []
        if not intervals:
            continue
        out.append(metrics_mod.SprintIssue(issue=_to_issue_input(f, id_to_name), intervals=intervals))
    return out


# --------------------------------------------------------------------------
# service.go equivalent: sprint resolution + base-sprint pick + two-pass velocity
# --------------------------------------------------------------------------


def _resolve_sprint_names(client: jc.JiraClient, names: list[str]) -> list[int]:
    """Exact match (case-insensitive, trimmed), tie-break = smallest sprint id (SPEC §1.1)."""
    ids = []
    for name in names:
        trimmed = name.strip()
        suggestions = client.suggest_sprints(trimmed)
        match_id: Optional[int] = None
        for sg in suggestions:
            if not model.status_equal(sg.name, trimmed):
                continue
            if match_id is None or sg.sprint_id < match_id:
                match_id = sg.sprint_id
        if match_id is None:
            raise ReportError(f"sprint not found: {trimmed!r}")
        ids.append(match_id)
    return ids


def _resolve_target_sprints(client: jc.JiraClient, sprint_ids: list[int], sprint_names: list[str]) -> list[jc.Sprint]:
    if sprint_ids and sprint_names:
        raise ReportError("exactly one of sprint_ids or sprint_names must be set")
    if not sprint_ids and not sprint_names:
        raise ReportError("sprint_ids or sprint_names is required")

    ids = list(sprint_ids) if sprint_ids else _resolve_sprint_names(client, list(sprint_names))

    seen: set[int] = set()
    sprints = []
    for sid in ids:
        if sid in seen:
            continue
        seen.add(sid)
        sprints.append(client.sprint(sid))
    if not sprints:
        raise ReportError("sprint not found")

    board_id = sprints[0].board_id
    for sp in sprints[1:]:
        if sp.board_id != board_id:
            raise ReportError("target sprints belong to different boards")

    sprints.sort(key=_sort_key)
    return sprints


def _pick_base_sprints(closed: list[jc.Sprint], targets: list[jc.Sprint], limit: int) -> list[jc.Sprint]:
    """Up to `limit` most recent closed sprints (excluding targets), ascending by start_at (SPEC §1.1).

    This is the DISPLAYED history population (board_table/KPI/velocity trend),
    sized by history_sprint_count. It is NOT the forecast's population — see
    _pick_forecast_sprints, which is a separate, fixed-size, target-inclusive
    set."""
    target_ids = {t.id for t in targets}
    candidates = [c for c in closed if c.id not in target_ids]
    candidates.sort(key=_sort_key, reverse=True)
    candidates = candidates[:limit]
    candidates.sort(key=_sort_key)
    return candidates


def _pick_forecast_sprints(closed: list[jc.Sprint], limit: int = MAX_BASE_SPRINTS) -> list[jc.Sprint]:
    """Forecast's own base-sprint population — independent of --history and
    of the target/base split used for the board's displayed rows. Mirrors Go
    (internal/adapters/persistence/sprint_snapshot_repo.go:110-112
    ListClosedByBoard + internal/app/board/forecast.go:44): the board's
    CLOSED sprints ordered by end_at descending, capped at `limit`
    (MAX_BASE_SPRINTS=5, matching Go's maxBaseSprints), then re-sorted
    ascending. A CLOSED target sprint is included, not excluded — Go reads
    persisted snapshots with no notion of "target" at all."""
    ordered = sorted(closed, key=lambda s: s.end_at, reverse=True)[:limit]
    ordered.sort(key=lambda s: s.end_at)
    return ordered


@dataclass
class _ResolvedSprint:
    sprint: jc.Sprint
    target: bool


def _velocity_history_for(
    all_sprints: list[_ResolvedSprint], idx: int, payloads: dict[int, metrics_mod.Payload]
) -> list[float]:
    """<=5 velocity_sp of base sprints STRICTLY preceding index idx (SPEC §4.1)."""
    out = []
    for i in range(idx):
        rs = all_sprints[i]
        if rs.target:
            continue
        p = payloads.get(rs.sprint.id)
        if p is not None:
            out.append(p.metrics.velocity_sp)
    if len(out) > MAX_BASE_SPRINTS:
        out = out[-MAX_BASE_SPRINTS:]
    return out


# --------------------------------------------------------------------------
# forecast.go (app) equivalent: default target_items resolution
# --------------------------------------------------------------------------


def _resolve_target_items(
    all_payloads: list[metrics_mod.Payload],
    client: jc.JiraClient,
    board_id: int,
    cfg: model.StatusCategoryConfig,
    id_to_name: dict[str, str],
    now: datetime,
    *,
    story_points_field_id: str = "",
) -> Optional[int]:
    """Remaining items of the board's active sprint (SPEC §1.2): committed +
    added - removed - delivered, clamped >= 0. This standalone tool has no
    persisted snapshot repository, so if the active sprint isn't already one
    of the fetched/computed sprints it is fetched and computed on demand;
    None means "no active sprint found" (caller decides how to degrade)."""
    active_payload = next((p for p in all_payloads if p.sprint.state != "closed"), None)

    if active_payload is None:
        active_sprints = client.board_sprints(board_id, "active")
        if not active_sprints:
            return None
        active_sprint = active_sprints[0]
        facts = client.fetch_sprint_issues([active_sprint.id], story_points_field_id=story_points_field_id)
        issues = _to_sprint_issues(facts, active_sprint.id, id_to_name)
        sprint_input = _to_sprint_input(active_sprint, "", now)
        # This on-demand fetch covers a sprint the main pass never saw, so it
        # needs the same story_points_field_id override AND the same
        # per-sprint status-category augmentation the main pass applies
        # (translate.go:31-38) — otherwise a status unique to this sprint's
        # issues would spuriously warn as unmapped.
        augmented_cfg = _augment_status_category_config(cfg, facts)
        active_payload, _ = metrics_mod.build_payload(sprint_input, issues, augmented_cfg, [])

    m = active_payload.metrics
    remaining = m.committed_items + m.scope_added_items - m.scope_removed_items - m.delivered_items
    return max(remaining, 0)


def _run_forecast(
    forecast_payloads: list[metrics_mod.Payload],
    all_payloads: list[metrics_mod.Payload],
    client: jc.JiraClient,
    board_id: int,
    cfg: model.StatusCategoryConfig,
    id_to_name: dict[str, str],
    now: datetime,
    *,
    target_items: Optional[int],
    seed: int,
    iterations: int,
    story_points_field_id: str = "",
) -> tuple[Optional[forecast_mod.ForecastOutput], Optional[str], Optional[int]]:
    resolved_target_items = target_items
    if resolved_target_items is None:
        resolved_target_items = _resolve_target_items(
            all_payloads, client, board_id, cfg, id_to_name, now, story_points_field_id=story_points_field_id
        )
    if resolved_target_items is None:
        return None, "no active sprint found to derive target_items; pass --target-items explicitly", None

    daily_throughput = forecast_mod.calendar_daily_throughput(forecast_payloads)
    rng = random.Random(seed)
    try:
        result = forecast_mod.forecast(
            daily_throughput, len(forecast_payloads), resolved_target_items, rng, iterations=iterations
        )
    except forecast_mod.NotEnoughDataError:
        return None, "ERR_FORECAST_NOT_ENOUGH_DATA", resolved_target_items
    return result, None, resolved_target_items


# --------------------------------------------------------------------------
# export.go equivalent: heatmap CSV/table + 17-column board table
# --------------------------------------------------------------------------

_WEEKDAY_RU = {0: "пн", 1: "вт", 2: "ср", 3: "чт", 4: "пт", 5: "сб", 6: "вс"}

_BOARD_HEADERS = [
    "sprint", "state", "start", "end",
    "committed_sp", "delivered_sp", "added_sp", "removed_sp", "estimation_change_sp",
    "performance_pct", "load_pct", "scope_change_pct",
    "velocity_sp", "sma5_sp",
    "throughput_count", "closure_rate_count_pct", "closure_rate_sp_pct",
]


def _format_number(v: float) -> str:
    """Shortest round-trip fixed-point text (mirrors Go's FormatFloat(v,'f',-1,64))."""
    if v == int(v) and abs(v) < 1e15:
        return str(int(v))
    r = repr(float(v))
    if "e" in r or "E" in r:
        r = f"{v:.15f}".rstrip("0").rstrip(".")
    return r


def _day_header(sprint_id: int, day: str) -> str:
    weekday = ""
    try:
        y, m, d = (int(x) for x in day.split("-"))
        weekday = _WEEKDAY_RU[datetime(y, m, d).weekday()]
    except (ValueError, KeyError):
        pass
    return f"{sprint_id} / {day} / {weekday}"


def _csv_guard(s: str) -> str:
    if s and s[0] in "=+-@":
        return "\t" + s
    return s


def _sanitize_csv(s: str) -> str:
    if any(c in s for c in (";", "\r", "\n")):
        s = s.replace(";", ",").replace("\r", " ").replace("\n", " ")
    return _csv_guard(s)


def _safe_cell(s: str) -> str:
    """Formula-injection guard for board_table cells — mirrors Go's safeCell
    (internal/app/board/export.go:388-397): prefix a single quote when the
    cell starts with a character a spreadsheet could read as a formula.
    Applied uniformly to every column, sprint name through numeric fields
    (a negative number's leading '-' also gets guarded, same as Go)."""
    if s and s[0] in "=+-@\t\r":
        return "'" + s
    return s


def _build_heatmap_table(target_payloads: list[metrics_mod.Payload]) -> tuple[list[str], list[list[str]]]:
    header = ["Epic", "Issue", "Story Points", "QA Estimation", "Роль", "Labels"]
    for p in target_payloads:
        header.append("Before")
        for d in p.sprint.working_days:
            header.append(_day_header(p.sprint.id, d))
        header.append("End")

    order: list[str] = []
    seen: set[str] = set()
    first_seen: dict[str, model.Issue] = {}
    for p in target_payloads:
        for is_ in p.issues:
            if is_.key in seen:
                continue
            seen.add(is_.key)
            order.append(is_.key)
            first_seen[is_.key] = is_

    rows: list[list[str]] = []
    for key in order:
        first = first_seen[key]
        row = [
            first.epic_key, key, _format_number(first.story_points),
            _format_number(first.qa_estimation), first.role, ",".join(first.labels),
        ]
        for p in target_payloads:
            is_ = next((x for x in p.issues if x.key == key), None)
            if is_ is None:
                row.append("")
                row.extend("" for _ in p.sprint.working_days)
                row.append("")
                continue
            row.append(is_.status_initial)
            by_date = {ds.date: ds.status for ds in is_.day_statuses}
            row.extend(by_date.get(d, "") for d in p.sprint.working_days)
            row.append(is_.status_end)
        rows.append(row)
    return header, rows


def _heatmap_csv(header: list[str], rows: list[list[str]]) -> str:
    """jiratools-compatible CSV: `;` separator, `\\n` EOL, UTF-8 BOM, no quoting (SPEC §9.1)."""
    lines = ["﻿" + ";".join(header)]
    for row in rows:
        lines.append(";".join(_sanitize_csv(c) for c in row))
    return "\n".join(lines) + "\n"


def _build_board_table(sprint_results: list[dict]) -> tuple[list[str], list[list[str]]]:
    rows = []
    for sr in sprint_results:
        p: metrics_mod.Payload = sr["payload"]
        m = p.metrics
        row = [
            p.sprint.name,
            p.sprint.state,
            model.format_date(p.sprint.start_at),
            model.format_date(p.sprint.end_at),
            _format_number(m.committed_sp),
            _format_number(m.delivered_sp),
            _format_number(m.scope_added_sp),
            _format_number(m.scope_removed_sp),
            _format_number(m.scope_estimation_change_sp),
            _format_number(m.performance_pct),
            _format_number(m.load_pct),
            _format_number(m.scope_change_pct),
            _format_number(m.velocity_sp),
            _format_number(m.velocity_sma5_sp),
            str(m.throughput_items),
            _format_number(m.closure_pct_items),
            _format_number(m.closure_pct_sp),
        ]
        rows.append([_safe_cell(c) for c in row])
    return list(_BOARD_HEADERS), rows


def _build_export(sprint_results: list[dict]) -> dict:
    target_payloads = [sr["payload"] for sr in sprint_results if sr["target"]]
    heatmap_header, heatmap_rows = _build_heatmap_table(target_payloads)
    board_header, board_rows = _build_board_table(sprint_results)
    return {
        "heatmap_table": {"header": heatmap_header, "rows": heatmap_rows},
        "heatmap_csv": _heatmap_csv(heatmap_header, heatmap_rows),
        "board_table": {"header": board_header, "rows": board_rows},
    }


# --------------------------------------------------------------------------
# JSON-serialization helper
# --------------------------------------------------------------------------


def _to_jsonable(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return obj


# --------------------------------------------------------------------------
# Top-level entry point
# --------------------------------------------------------------------------


def _build_report_core(
    client: jc.JiraClient,
    *,
    sprint_ids: list[int] = (),
    sprint_names: list[str] = (),
    board_id_override: Optional[int] = None,
    history_sprint_count: int = 5,
    status_map: Optional[dict[str, str]] = None,
    cancelled_statuses: Optional[list[str]] = None,
    story_points_field_id: str = "",
    seed: int = config_mod.DEFAULT_SEED,
    iterations: int = 0,
    target_items: Optional[int] = None,
    now: Optional[datetime] = None,
) -> tuple[dict, list[jc.IssueFacts], list[jc.Sprint]]:
    """Builds the full report dict (pre-JSON-serialization) PLUS the raw
    fetched facts and resolved target sprints, so build_combined_report() can
    adapt those same facts into the personal/engineering halves without a
    second Jira fetch. build_report() is the public, JSON-ready wrapper
    around this that drops the extra return values."""
    status_map = status_map or {}
    cancelled_statuses = cancelled_statuses or list(config_mod.DEFAULT_CANCELLED_STATUSES)
    now = now or datetime.now(UTC)

    targets = _resolve_target_sprints(client, list(sprint_ids), list(sprint_names))
    board_id = targets[0].board_id
    if board_id_override is not None and board_id_override != board_id:
        raise ReportError(
            f"--board-id {board_id_override} does not match the resolved target sprint's board {board_id}"
        )

    board = client.board(board_id)
    board_name = board.name

    closed = client.closed_sprints(board_id)
    base_sprints = _pick_base_sprints(closed, targets, history_sprint_count)
    forecast_sprints = _pick_forecast_sprints(closed, MAX_BASE_SPRINTS)

    all_sprints: list[_ResolvedSprint] = [_ResolvedSprint(sprint=t, target=True) for t in targets]
    all_sprints += [_ResolvedSprint(sprint=b, target=False) for b in base_sprints]
    all_sprints.sort(key=lambda rs: _sort_key(rs.sprint))

    all_ids = [rs.sprint.id for rs in all_sprints]
    # The forecast population (B1) can name a closed sprint outside the
    # display/base set above — widen the one fetch to cover it too, rather
    # than issuing a second Jira pass just for the forecast.
    fetch_ids = list(dict.fromkeys(all_ids + [s.id for s in forecast_sprints]))
    facts = client.fetch_sprint_issues(fetch_ids, story_points_field_id=story_points_field_id)
    jira_statuses = client.list_statuses()
    cfg = _to_status_category_config(status_map, cancelled_statuses, jira_statuses, facts)
    id_to_name = _id_to_current_name(jira_statuses)

    # PASS 1 — base sprints only, empty history: velocity_sp == delivered_sp
    # never depends on velocity_history, only velocity_sma5_sp/load_pct do.
    prelim: dict[int, metrics_mod.Payload] = {}
    for rs in all_sprints:
        if rs.target:
            continue
        issues = _to_sprint_issues(facts, rs.sprint.id, id_to_name)
        sprint_input = _to_sprint_input(rs.sprint, board_name, now)
        payload, _ = metrics_mod.build_payload(sprint_input, issues, cfg, [])
        prelim[rs.sprint.id] = payload

    base_velocities = [prelim[rs.sprint.id].metrics.velocity_sp for rs in all_sprints if not rs.target]

    idx_by_id = {rs.sprint.id: i for i, rs in enumerate(all_sprints)}

    # PASS 2 (final) — every sprint (target + base), each with its own
    # strictly-preceding base-sprint velocity history.
    final: dict[int, metrics_mod.Payload] = {}
    fresh_warnings: list[str] = []
    for rs in all_sprints:
        issues = _to_sprint_issues(facts, rs.sprint.id, id_to_name)
        sprint_input = _to_sprint_input(rs.sprint, board_name, now)
        history = _velocity_history_for(all_sprints, idx_by_id[rs.sprint.id], prelim)
        payload, warns = metrics_mod.build_payload(sprint_input, issues, cfg, history)
        final[rs.sprint.id] = payload
        if rs.target:
            fresh_warnings.extend(warns)

    sprint_results: list[dict] = []
    all_payloads: list[metrics_mod.Payload] = []
    base_payloads: list[metrics_mod.Payload] = []
    heatmaps = []
    burndowns = []
    for rs in all_sprints:
        p = final[rs.sprint.id]
        sprint_results.append({"payload": p, "target": rs.target})
        all_payloads.append(p)
        if not rs.target:
            base_payloads.append(p)
        else:
            heatmaps.append(heatmap_mod.heatmap_from_payload(p))
            burndowns.append({"sprint_id": p.sprint.id, "points": burndown_mod.burndown_from_payload(p, now)})

    kpi, kpi_warnings = metrics_mod.build_kpi(base_payloads, base_velocities, forecast_mod.MIN_NON_ZERO_POINTS)

    warnings = list(kpi_warnings)
    w = metrics_mod.status_unmapped_warning(all_payloads)
    if w:
        warnings.append(w)
    w = metrics_mod.sprint_active_partial_warning(all_payloads)
    if w:
        warnings.append(w)
    warnings.extend(fresh_warnings)
    warnings = model.dedupe_warnings(warnings)

    # Forecast's own population (B1) — closed sprints of the board ordered by
    # end_at, capped at MAX_BASE_SPRINTS, target-inclusive. Reuse an
    # already-computed payload when one of these sprints is also in `final`;
    # its velocity_history doesn't matter here — only throughput_daily and
    # the sprint's own dates feed the forecast, and neither depends on it.
    forecast_payloads: list[metrics_mod.Payload] = []
    for s in forecast_sprints:
        p = final.get(s.id)
        if p is None:
            issues = _to_sprint_issues(facts, s.id, id_to_name)
            sprint_input = _to_sprint_input(s, board_name, now)
            p, _ = metrics_mod.build_payload(sprint_input, issues, cfg, [])
        forecast_payloads.append(p)

    forecast_result, forecast_error, resolved_target_items = _run_forecast(
        forecast_payloads,
        all_payloads,
        client,
        board_id,
        cfg,
        id_to_name,
        now,
        target_items=target_items,
        seed=seed,
        iterations=iterations,
        story_points_field_id=story_points_field_id,
    )

    export = _build_export(sprint_results)

    report = {
        "schema_version": metrics_mod.SCHEMA_VERSION,
        "board": {"id": board_id, "name": board_name},
        "sprints": sprint_results,
        "heatmaps": heatmaps,
        "burndowns": burndowns,
        "kpi": kpi,
        "forecast": forecast_result,
        "forecast_error": forecast_error,
        "warnings": warnings,
        "export": export,
        "params": {
            "sprint_ids": list(sprint_ids),
            "sprint_names": list(sprint_names),
            "board_id": board_id,
            "history_sprint_count": history_sprint_count,
            "seed": seed,
            "iterations": iterations if iterations > 0 else forecast_mod.DEFAULT_ITERATIONS,
            "target_items_requested": target_items,
            "target_items_resolved": resolved_target_items,
            "generated_at": now,
        },
    }
    return report, facts, targets


def build_report(
    client: jc.JiraClient,
    *,
    sprint_ids: list[int] = (),
    sprint_names: list[str] = (),
    board_id_override: Optional[int] = None,
    history_sprint_count: int = 5,
    status_map: Optional[dict[str, str]] = None,
    cancelled_statuses: Optional[list[str]] = None,
    story_points_field_id: str = "",
    seed: int = config_mod.DEFAULT_SEED,
    iterations: int = 0,
    target_items: Optional[int] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Builds the full report_data dict: sprint metrics, board KPI, heatmaps,
    burndowns, forecast, both export tables, warnings and the parameter echo.
    The returned dict is json.dumps-able as-is. See build_combined_report()
    for the wider report that also wires in the GitLab-derived tabs."""
    report, _facts, _targets = _build_report_core(
        client,
        sprint_ids=sprint_ids,
        sprint_names=sprint_names,
        board_id_override=board_id_override,
        history_sprint_count=history_sprint_count,
        status_map=status_map,
        cancelled_statuses=cancelled_statuses,
        story_points_field_id=story_points_field_id,
        seed=seed,
        iterations=iterations,
        target_items=target_items,
        now=now,
    )
    return _to_jsonable(report)


# --------------------------------------------------------------------------
# GitLab/personal/engineering wiring — one Jira pass feeds every tab
# --------------------------------------------------------------------------


def _to_personal_issue_input(fact: jc.IssueFacts) -> personal_metrics.JiraIssueInput:
    """Adapts one already-fetched IssueFacts into personal_metrics' input
    shape — the SAME Jira pass that builds the sprint-metrics tab feeds this,
    no second fetch.

    story_points is fact.story_points, the CURRENT field value at fetch
    time — NOT model.Issue.story_points (the sprint tab's end-of-membership
    value, replayed from the SP changelog). Using the wrong one here would
    silently produce wrong SP sums; see semantics_notes in
    build_combined_report()'s output for the user-facing explanation.

    resolutiondate comes from fact.resolutiondate (jira_client.py's own
    "resolutiondate" search field, fetched in the same pass) — personal_
    metrics.py uses it as a done_at fallback for an issue with no
    changelog-tracked transition into a final status."""
    return personal_metrics.JiraIssueInput(
        key=fact.key,
        issuetype=fact.type,
        assignee=fact.assignee,
        story_points=fact.story_points,
        qa_estimation=fact.qa_estimation,
        resolutiondate=fact.resolutiondate,
        status_events=[personal_metrics.StatusEvent(at=ch.at, to_status=ch.to_name) for ch in fact.status_history],
    )


# Cross-tab divergences a report renderer must disclose verbatim to users
# (Russian prose, not codes — the report renders these directly). See
# .research/ai-integration-metrics/SPEC.md §9 for the citations these
# summarize.
_SEMANTICS_NOTES_RU = [
    "Привязка задачи к спринту: вкладка «Спринт» относит задачу к спринту по "
    "интервалам членства в поле Sprint (задача могла заходить и выходить из "
    "спринта несколько раз), а персональная и инженерная статистика — по дате "
    "завершения задачи, попавшей в календарные даты спринта. Для одной и той "
    "же задачи эти два способа могут указать на разные спринты.",
    "Определение «сделано»: вкладка «Спринт» считает задачу выполненной, если "
    "на конец спринта её статус относится к категории Jira «done» (с учётом "
    "списка отменённых статусов). Персональная статистика считает задачу "
    "выполненной по первому попаданию в один из именованных финальных "
    "статусов, включая предрелизные «To Test» и «Ready to Deploy». Число "
    "выполненных задач по этим двум вкладкам не совпадает и не должно "
    "сравниваться напрямую.",
    "Throughput (день сдачи задачи): вкладка «Спринт» берёт день ПОСЛЕДНЕГО "
    "перехода задачи в категорию «done» внутри спринта, а персональная "
    "статистика — день ПЕРВОГО перехода в финальный статус. При повторных "
    "переоткрытиях задачи эти даты могут разойтись на недели.",
    "Story Points: вкладка «Спринт» показывает значение поля на момент выхода "
    "задачи из спринта (реплей истории изменений SP), а персональная "
    "статистика — текущее значение поля на момент выгрузки отчёта. Если "
    "оценку меняли уже после спринта, суммы SP по двум вкладкам разойдутся.",
]


def build_combined_report(
    client: jc.JiraClient,
    *,
    sprint_ids: list[int] = (),
    sprint_names: list[str] = (),
    board_id_override: Optional[int] = None,
    history_sprint_count: int = 5,
    status_map: Optional[dict[str, str]] = None,
    cancelled_statuses: Optional[list[str]] = None,
    story_points_field_id: str = "",
    seed: int = config_mod.DEFAULT_SEED,
    iterations: int = 0,
    target_items: Optional[int] = None,
    now: Optional[datetime] = None,
    gitlab_client_obj: Optional[glc.GitLabClient] = None,
    gitlab_projects: list[str] = (),
    employees: list[str] = (),
    final_statuses: Iterable[str] = personal_metrics.FINAL_STATUSES_DEFAULT,
    include_personal: bool = True,
    fetch_mr_details: bool = True,
    fetch_pipeline_user: bool = True,
) -> dict:
    """build_report()'s output PLUS `personal` (build_personal_report),
    `engineering` (build_engineering_metrics), `semantics_notes` and
    `gitlab_fetch_issues`, fed from the SAME Jira fetch build_report()
    already makes — no second Jira pass.

    `personal`/`engineering` are `{"available": False, "reason": ...}`
    instead of raising when `gitlab_client_obj` is None (GITLAB_URL/
    GITLAB_TOKEN absent, or --no-gitlab) — a missing or disabled GitLab half
    must never fail the whole run. `include_personal=False` (--no-personal)
    disables only the personal tab; engineering (team-level pipelines/
    deployments/coverage) still runs as long as GitLab is configured.

    `fetch_mr_details`/`fetch_pipeline_user` (default True, unchanged
    behavior) pass straight through to gitlab_client.fetch_team_data() —
    audit finding 2a: these two fan-outs are the largest GitLab
    request-volume drivers (one request per MR, twice; one per pipeline).
    Whatever value each actually ran with is echoed into
    `report["params"]["gitlab_fetch_mr_details"/"gitlab_fetch_pipeline_
    user"]` (None if GitLab was never configured), together with
    `report["params"]["gitlab_request_count"]` (the actual HTTP round-trip
    count this run made — audit finding 2b) — a report produced with the
    heavy fetches skipped must say so on its face, not just show a
    suspiciously low number.

    A genuine GitLab-side fault DURING a configured fetch is not degraded —
    glc.GitLabError propagates out of this function (most notably
    AUTH_FAILED, which gitlab_client.py deliberately never swallows: a
    revoked token must not look like a clean, all-zero report)."""
    now = now or datetime.now(UTC)
    report, facts, targets = _build_report_core(
        client,
        sprint_ids=sprint_ids,
        sprint_names=sprint_names,
        board_id_override=board_id_override,
        history_sprint_count=history_sprint_count,
        status_map=status_map,
        cancelled_statuses=cancelled_statuses,
        story_points_field_id=story_points_field_id,
        seed=seed,
        iterations=iterations,
        target_items=target_items,
        now=now,
    )

    personal_available, personal_reason, personal_data = False, "GitLab is not configured for this run", None
    engineering_available, engineering_reason, engineering_data = False, "GitLab is not configured for this run", None
    gitlab_fetch_issues = {"skipped_projects": [], "mr_fetch_errors": [], "deployment_warnings": []}
    # None (not False/0) when GitLab was never configured: neither fetch ran
    # one way or the other, so True/False would misleadingly imply a
    # decision was actually made about a fetch that never happened.
    report["params"]["gitlab_request_count"] = None
    report["params"]["gitlab_fetch_mr_details"] = None
    report["params"]["gitlab_fetch_pipeline_user"] = None

    if gitlab_client_obj is not None:
        # Window = the TARGET sprints' own dates (min start / max end) — the
        # sprints this report is actually about, not the extra history
        # sprints only used for tab 1's velocity trend.
        window = glc.Window(start=min(t.start_at for t in targets), end=max(t.end_at for t in targets))

        # An empty `employees` list makes GitLabClient.merge_requests() fetch
        # nothing (its author x state task list is empty) — passing [] here
        # for --no-personal skips the MR calls entirely rather than fetching
        # and discarding them, while pipelines/deployments/coverage (team-
        # level, not employee-scoped) still run for the engineering tab.
        effective_employees = list(employees) if include_personal else []
        gitlab_data = glc.fetch_team_data(
            gitlab_client_obj,
            projects=list(gitlab_projects),
            employees=effective_employees,
            window=window,
            fetch_mr_details=fetch_mr_details,
            fetch_pipeline_user=fetch_pipeline_user,
        )
        report["params"]["gitlab_request_count"] = gitlab_data.get("request_count")
        report["params"]["gitlab_fetch_mr_details"] = fetch_mr_details
        report["params"]["gitlab_fetch_pipeline_user"] = fetch_pipeline_user
        # Per-project/per-author failures fetch_team_data tolerates rather
        # than raising (e.g. a renamed login, one unreachable project) —
        # surfaced so the report can disclose exactly what was skipped even
        # though the run as a whole still succeeded. AUTH_FAILED is the one
        # class that does NOT reach here: it propagates out of
        # fetch_team_data and fails the whole run (see main()'s except
        # clause) — a revoked token must never look like a clean report.
        #
        # deployment_warnings carries two distinct codes a renderer must
        # treat differently:
        #   FILTER_REJECTED_FALLBACK — the server rejected the date-filtered
        #     request; the client retried unfiltered and windowed client-side.
        #     The numbers are still correct, the run just cost more requests.
        #   PAGINATION_LIMIT — the fetch hit the page cap or time deadline and
        #     returned a PARTIAL deployment list. deployment counts and the
        #     success rate computed over that subset are understated and must
        #     not be read as complete — this is exactly the "truncated list
        #     that looks like a plausible lower number" failure mode this
        #     whole audit exists to surface, not hide.
        gitlab_fetch_issues = {
            "skipped_projects": gitlab_data.get("skipped_projects", []),
            "mr_fetch_errors": gitlab_data.get("mr_fetch_errors", []),
            "deployment_warnings": gitlab_data.get("deployment_warnings", []),
        }

        engineering_data = engineering_metrics.build_engineering_metrics(
            pipelines=gitlab_data["pipelines"],
            deployments=gitlab_data["deployments"],
            coverage=gitlab_data["coverage"],
            window_applied=gitlab_data["window_applied"],
        )
        engineering_available, engineering_reason = True, None

        if include_personal:
            # Scope personal to the SAME target sprints tab 1 reports on, not
            # the wider history/base set — an issue only fetched for
            # velocity-trend context on tab 1 must not inflate tab 2's totals.
            target_ids = {t.id for t in targets}
            personal_facts = [f for f in facts if any(f.membership_by_sprint.get(tid) for tid in target_ids)]
            personal_issues = [_to_personal_issue_input(f) for f in personal_facts]
            personal_sprints = [personal_metrics.Sprint(name=t.name, start=t.start_at, end=t.end_at) for t in targets]
            try:
                personal_data = personal_metrics.build_personal_report(
                    mrs=gitlab_data["merge_requests"],
                    issues=personal_issues,
                    sprints=personal_sprints,
                    date_window=(window.start, window.end),
                    final_statuses=final_statuses,
                )
                # build_personal_report() has no parameter to thread
                # `pipelines` through to personal_metrics() (its
                # pipeline_success_rate would stay None otherwise) — patch it
                # in per person via the same public helper personal_metrics()
                # calls internally, using the team-wide pipelines list this
                # module's own docstring documents as personal_pipeline_
                # success()'s contract (it filters by user_username itself).
                for person in personal_data.get("people", []):
                    person["pipeline_success_rate"] = personal_metrics.personal_pipeline_success(
                        person["user"], gitlab_data["pipelines"]
                    )
                personal_available, personal_reason = True, None
            except ValueError as e:
                # Enforce explicit scope, never an instance-wide unscoped
                # fetch: _resolve_target_sprints() already guarantees
                # `targets` is non-empty, so personal_metrics' own empty-
                # sprints guard should be unreachable here — this only turns
                # it into a soft "unavailable" instead of an uncaught
                # traceback if that invariant is ever broken.
                personal_reason = str(e)
        else:
            personal_reason = "--no-personal"

    report["personal"] = {"available": personal_available, "reason": personal_reason, "data": personal_data}
    report["engineering"] = {
        "available": engineering_available,
        "reason": engineering_reason,
        "data": engineering_data,
    }
    report["semantics_notes"] = list(_SEMANTICS_NOTES_RU)
    report["gitlab_fetch_issues"] = gitlab_fetch_issues

    return _to_jsonable(report)


def main(argv: Optional[list[str]] = None) -> int:
    try:
        run_cfg = config_mod.parse_args(argv)
    except config_mod.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    client = jc.JiraClient(run_cfg.env.base_url, run_cfg.env.token)

    gitlab_cli = None
    if run_cfg.gitlab_env is not None and not run_cfg.no_gitlab:
        gitlab_cli = glc.GitLabClient(run_cfg.gitlab_env.base_url, run_cfg.gitlab_env.token)

    try:
        report = build_combined_report(
            client,
            sprint_ids=run_cfg.sprint_ids,
            sprint_names=run_cfg.sprint_names,
            board_id_override=run_cfg.board_id,
            history_sprint_count=run_cfg.history_sprint_count,
            status_map=run_cfg.file_config.status_map,
            cancelled_statuses=run_cfg.file_config.cancelled_statuses,
            story_points_field_id=run_cfg.file_config.story_points_field_id,
            seed=run_cfg.seed,
            iterations=run_cfg.iterations,
            target_items=run_cfg.target_items,
            gitlab_client_obj=gitlab_cli,
            gitlab_projects=run_cfg.file_config.gitlab_projects,
            employees=run_cfg.file_config.employees,
            final_statuses=run_cfg.file_config.final_statuses,
            include_personal=not run_cfg.no_personal,
            fetch_mr_details=run_cfg.fetch_mr_details,
            fetch_pipeline_user=run_cfg.fetch_pipeline_user,
        )
    except (ReportError, jc.JiraError, glc.GitLabError) as e:
        # glc.GitLabError here is a genuine GitLab-side fault during a
        # CONFIGURED fetch (most notably AUTH_FAILED, which
        # gitlab_client.fetch_team_data() deliberately lets propagate rather
        # than degrade — a revoked token must never look like a clean,
        # all-zero report). This is a different case from GitLab being
        # merely unconfigured/--no-gitlab, which build_combined_report()
        # already turns into report["personal"/"engineering"]["available"] =
        # False without raising.
        print(f"error: {e}", file=sys.stderr)
        return 1

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if run_cfg.out_path:
        with open(run_cfg.out_path, "w", encoding="utf-8") as f:
            f.write(text)
    if run_cfg.json_out or not run_cfg.out_path:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
