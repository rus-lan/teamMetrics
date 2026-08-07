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
import statistics
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from . import burndown as burndown_mod
from . import config as config_mod
from . import engineering_metrics
from . import forecast as forecast_mod
from . import gitlab_client as glc
from . import heatmap as heatmap_mod
from . import jira_client as jc
from . import labels_ru
from . import logging_setup
from . import metrics as metrics_mod
from . import model
from . import personal_metrics
from . import sprint_series

log = logging_setup.get_logger("report_data")

# scripts/team_metrics/report_data.py -> scripts/team_metrics -> scripts -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]
_VERSION_PATH = _REPO_ROOT / "VERSION"

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
        return None, "ERR_FORECAST_NO_ACTIVE_SPRINT", None

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
        rows.append(row)
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
        # §B rule 5: YYYY-MM-DDTHH:MM:SSZ, no sub-second part -- drop
        # microseconds before formatting (a Jira timestamp can carry them).
        return obj.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
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
    per_sprint_warnings: dict[int, list[str]] = {}
    for rs in all_sprints:
        issues = _to_sprint_issues(facts, rs.sprint.id, id_to_name)
        sprint_input = _to_sprint_input(rs.sprint, board_name, now)
        history = _velocity_history_for(all_sprints, idx_by_id[rs.sprint.id], prelim)
        payload, warns = metrics_mod.build_payload(sprint_input, issues, cfg, history)
        final[rs.sprint.id] = payload
        per_sprint_warnings[rs.sprint.id] = warns
        if rs.target:
            fresh_warnings.extend(warns)

    sprint_results: list[dict] = []
    all_payloads: list[metrics_mod.Payload] = []
    base_payloads: list[metrics_mod.Payload] = []
    for rs in all_sprints:
        p = final[rs.sprint.id]
        sprint_results.append({"payload": p, "target": rs.target})
        all_payloads.append(p)
        if not rs.target:
            base_payloads.append(p)

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

    core = {
        "board_id": board_id,
        "board_name": board_name,
        "all_sprints": all_sprints,
        "final": final,
        "base_payloads": base_payloads,
        "all_payloads": all_payloads,
        "sprint_results": sprint_results,
        "kpi": kpi,
        "forecast_result": forecast_result,
        "forecast_error": forecast_error,
        "resolved_target_items": resolved_target_items,
        "forecast_payloads": forecast_payloads,
        "warnings": warnings,
        "export": export,
        "cfg": cfg,
        "_per_sprint_warnings": per_sprint_warnings,
        "now": now,
        "sprint_ids": list(sprint_ids),
        "sprint_names": list(sprint_names),
        "history_sprint_count": history_sprint_count,
        "seed": seed,
        "iterations": iterations if iterations > 0 else forecast_mod.DEFAULT_ITERATIONS,
        "target_items_requested": target_items,
    }
    return core, facts, targets


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
    "Период персональной и инженерной статистики: с версии 3.0.0 он покрывает "
    "все анализируемые спринты (базовые + целевые), а не только целевые. "
    "Суммарные числа по людям и CI/CD поэтому шире, чем метрики одного "
    "целевого спринта на вкладке «Спринт».",
]

_GITLAB_NOT_CONFIGURED_RU = "GitLab не настроен для этого запуска"
_NO_PERSONAL_RU = "Отключено флагом --no-personal"


def _tool_version() -> str:
    try:
        text = _VERSION_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return "неизвестна"
    return text or "неизвестна"


# --------------------------------------------------------------------------
# v2 assembly — small pure builders, one per JSON section (SPEC §B)
# --------------------------------------------------------------------------


def _build_board_kpi(
    axis: list["sprint_series.AxisSprint"],
    sprints_by_id: dict[int, metrics_mod.Payload],
    primary_target: metrics_mod.Payload,
    kpi: metrics_mod.Kpi,
) -> dict:
    def series_for(field_name: str) -> list:
        return [getattr(sprints_by_id[a.id].metrics, field_name) for a in axis]

    m = primary_target.metrics
    tiles = [
        {
            "key": "commitment", "label_ru": "Поставлено / обязательство (SP)",
            "value_text": f"{_format_number(m.delivered_sp)} / {_format_number(m.committed_sp)}",
            "delivered_sp": m.delivered_sp, "committed_sp": m.committed_sp,
            "target_ru": None, "status": "none", "series": series_for("delivered_sp"),
            "hint_ru": "Сколько SP команда поставила к концу спринта против обязательства на его старте.",
        },
        {
            "key": "performance_pct", "label_ru": "Performance (Say/Do)",
            "value": m.performance_pct, "unit_ru": "%", "target_ru": "цель 80%",
            "status": "good" if m.performance_pct >= 80 else "warn",
            "series": series_for("performance_pct"),
            "hint_ru": "Поставлено SP / обязательство SP × 100. Ниже 80% — предупреждение.",
        },
        {
            "key": "load_pct", "label_ru": "Загрузка",
            "value": m.load_pct, "unit_ru": "%", "target_ru": "цель 80–100%",
            "status": "good" if 80 <= m.load_pct <= 100 else "warn",
            "series": series_for("load_pct"),
            "hint_ru": "Обязательство SP / Velocity SMA5 × 100. Вне 80–100% — предупреждение.",
        },
        {
            "key": "scope_change_pct", "label_ru": "Изменение объёма",
            "value": m.scope_change_pct, "unit_ru": "%", "target_ru": None, "status": "none",
            "series": series_for("scope_change_pct"),
            "hint_ru": "(Добавлено SP + |изменение оценок| + убрано SP) / обязательство SP × 100.",
        },
        {
            "key": "velocity_sma5_sp", "label_ru": "Velocity SMA5 (SP)",
            "value": kpi.velocity_sma5_sp, "unit_ru": "SP", "target_ru": None, "status": "none",
            "series": series_for("velocity_sma5_sp"),
            "hint_ru": "Средняя velocity до 5 предыдущих закрытых спринтов базы.",
        },
        {
            "key": "throughput_avg_items", "label_ru": "Средний throughput (задач)",
            "value": kpi.throughput_avg_items, "unit_ru": "задач", "target_ru": None, "status": "none",
            "series": series_for("throughput_items"),
            "hint_ru": "Среднее число закрытых задач за спринт по базовым спринтам.",
        },
        {
            "key": "closure_pct_items", "label_ru": "% закрытия (задачи)",
            "value": m.closure_pct_items, "unit_ru": "%", "target_ru": None, "status": "none",
            "series": series_for("closure_pct_items"),
            "hint_ru": "Поставленные задачи / (обязательство + добавлено − убрано) × 100.",
        },
        {
            "key": "closure_pct_sp", "label_ru": "% закрытия (SP)",
            "value": m.closure_pct_sp, "unit_ru": "%", "target_ru": None, "status": "none",
            "series": series_for("closure_pct_sp"),
            "hint_ru": "Поставленные SP / (обязательство + добавлено − убрано) SP × 100.",
        },
    ]
    return {"target_sprint_id": primary_target.sprint.id, "forecast_available": kpi.forecast_available, "tiles": tiles}


def _div0_detail_for_sprint(m: metrics_mod.Metrics) -> Optional[str]:
    """Attribution of a sprint-level WARN_DIVISION_BY_ZERO to every metric
    whose denominator was actually <= 0 — checked in the same order
    compute_metrics() computes its ratios. compute_metrics()/dedupe_warnings()
    collapse any number of individual div0s into ONE bare code per sprint, so
    this is the only place that can still tell the reader which metrics were
    actually affected — naming just the first one found would silently hide
    the rest."""
    labels = []
    if m.committed_sp <= 0:
        labels.append(labels_ru.metric_label_ru("performance_pct"))
    if m.velocity_sma5_sp <= 0:
        labels.append(labels_ru.metric_label_ru("load_pct"))
    if m.committed_sp <= 0:
        labels.append(labels_ru.metric_label_ru("scope_change_pct"))
    if (m.committed_items + m.scope_added_items - m.scope_removed_items) <= 0:
        labels.append(labels_ru.metric_label_ru("closure_rate_count_pct"))
    if (m.committed_sp + m.scope_added_sp - m.scope_removed_sp) <= 0:
        labels.append(labels_ru.metric_label_ru("closure_rate_sp_pct"))
    return ", ".join(labels) if labels else None


def _sprint_warning_objects(payload: metrics_mod.Payload, warns: list[str]) -> list[dict]:
    out = []
    for code in warns:
        detail = _div0_detail_for_sprint(payload.metrics) if code == model.WARN_DIVISION_BY_ZERO else None
        out.append(labels_ru.warning_obj(code, detail))
    return out


def _build_sprints_section(
    axis: list["sprint_series.AxisSprint"],
    sprints_by_id: dict[int, metrics_mod.Payload],
    per_sprint_warnings: dict[int, list[str]],
    target_ids: set,
) -> list[dict]:
    out = []
    for a in axis:
        p = sprints_by_id[a.id]
        out.append(
            {
                "target": a.id in target_ids,
                "meta": {
                    "id": p.sprint.id, "name": p.sprint.name, "board_id": p.sprint.board_id,
                    "board_name": p.sprint.board_name, "state": p.sprint.state,
                    "start_at": p.sprint.start_at, "end_at": p.sprint.end_at,
                    "complete_at": p.sprint.complete_at, "working_days": list(p.sprint.working_days),
                },
                "metrics": p.metrics,
                "throughput_daily": p.throughput_daily,
                "warnings": _sprint_warning_objects(p, per_sprint_warnings.get(a.id, [])),
            }
        )
    return out


def _build_issue_breakdown(target_payloads: list[metrics_mod.Payload], cfg: model.StatusCategoryConfig) -> list[dict]:
    out = []
    for p in target_payloads:
        rows = []
        for is_ in p.issues:
            category, _ = model.effective_status_category(is_.status_end, cfg)
            rows.append({"key": is_.key, "final_status": is_.status_end, "final_status_category": category, "delivered": is_.delivered})
        out.append({"sprint_id": p.sprint.id, "sprint_name": p.sprint.name, "rows": rows})
    return out


def _build_burndown_v2(target_payloads: list[metrics_mod.Payload], now: datetime) -> list[dict]:
    out = []
    for p in target_payloads:
        points = burndown_mod.burndown_from_payload(p, now)
        out.append(
            {
                "sprint_id": p.sprint.id, "sprint_name": p.sprint.name,
                "points": [
                    {
                        "date": pt.date, "remaining_items": pt.remaining_items, "remaining_sp": pt.remaining_sp,
                        "ideal_sp": pt.ideal_sp, "ideal_items": pt.ideal_items,
                    }
                    for pt in points
                ],
            }
        )
    return out


def _build_heatmap_v2(target_payloads: list[metrics_mod.Payload], resolve_name) -> list[dict]:
    out = []
    for p in target_payloads:
        logins = sorted({is_.assignee for is_ in p.issues if is_.assignee})
        display_names = {login: resolve_name(login)[0] for login in logins}
        hm = heatmap_mod.heatmap_from_payload(p, display_names)
        rows = [
            {
                "issue_key": r.issue_key, "epic_key": r.epic_key,
                "assignee_login": r.assignee_login, "assignee_display_name": r.assignee_display_name,
                "story_points": r.story_points, "qa_estimation": r.qa_estimation,
                "role": r.role, "role_ru": r.role_ru, "labels": list(r.labels),
                "status_initial": r.status_initial, "status_end": r.status_end,
                "cells": [{"date": c.date, "status": c.status, "status_category": c.status_category} for c in r.cells],
            }
            for r in hm.rows
        ]
        out.append({"sprint_id": p.sprint.id, "sprint_name": p.sprint.name, "days": list(hm.days), "rows": rows})
    return out


def _build_people_individual_jira(target_payloads: list[metrics_mod.Payload], cfg: model.StatusCategoryConfig, resolve_name) -> list[dict]:
    out = []
    for p in target_payloads:
        raw_rows = metrics_mod.per_assignee_metrics(p.issues, cfg)
        enriched = []
        for r in raw_rows:
            display = resolve_name(r.assignee)[0] if r.assignee else "Без исполнителя"
            enriched.append((r.assignee == "", display, r))
        enriched.sort(key=lambda t: (t[0], t[1], t[2].assignee))
        rows = []
        for _unassigned, display, r in enriched:
            rows.append(
                {
                    "assignee_login": r.assignee, "assignee_display_name": display,
                    "committed_sp": r.committed_sp, "committed_items": r.committed_items,
                    "delivered_sp": r.delivered_sp, "delivered_items": r.delivered_items,
                    "performance_pct": r.performance_pct, "velocity_sp": r.velocity_sp,
                    "throughput_items": r.throughput_items,
                    "warnings": [labels_ru.warning_obj(w, labels_ru.metric_label_ru("performance_pct")) for w in r.warnings],
                }
            )
        out.append({"sprint_id": p.sprint.id, "sprint_name": p.sprint.name, "rows": rows})
    return out


def _build_forecast_v2(
    forecast_result: Optional[forecast_mod.ForecastOutput],
    forecast_error: Optional[str],
    resolved_target_items: Optional[int],
    target_items_requested: Optional[int],
    forecast_payloads: list[metrics_mod.Payload],
) -> dict:
    target_items_source_ru = (
        "Задано флагом --target-items" if target_items_requested is not None
        else "Авто (незакрытые задачи активного спринта)"
    )
    if forecast_result is None:
        daily = forecast_mod.calendar_daily_throughput(forecast_payloads)
        return {
            "available": False,
            "error": labels_ru.warning_obj(forecast_error or "ERR_UNKNOWN"),
            # §B types target_items as int, never null -- when no simulation
            # ever ran (no active sprint to derive it from, none requested
            # either) there is no "what the simulation ran with" value, so 0
            # is the only int that does not fabricate a real target.
            "target_items": resolved_target_items if resolved_target_items is not None else 0,
            "target_items_source_ru": target_items_source_ru,
            "sample_sprints": len(forecast_payloads),
            "sample_days": len(daily),
            "throughput_cv_pct": forecast_mod.weekly_cv(daily) if daily else 0.0,
            "cv_warning_ru": None,
            "sample_hint_ru": f"Выборка: {len(forecast_payloads)} спринтов, {len(daily)} дневных точек.",
            "percentiles": [],
            "histogram": [],
            "warnings": [],
        }

    cv = forecast_result.throughput_cv_pct
    cv_warning_ru = None
    if cv > forecast_mod.CV_WARN_THRESHOLD_PCT:
        cv_warning_ru = f"Поток нестабилен (CV {labels_ru.format_pct1(cv)}%). Относитесь к перцентилям с осторожностью."
    return {
        "available": True,
        "error": None,
        "target_items": forecast_result.target_items,
        "target_items_source_ru": target_items_source_ru,
        "sample_sprints": forecast_result.sample_sprints,
        "sample_days": forecast_result.sample_days,
        "throughput_cv_pct": cv,
        "cv_warning_ru": cv_warning_ru,
        "sample_hint_ru": f"Выборка: {forecast_result.sample_sprints} спринтов, {forecast_result.sample_days} дневных точек.",
        "percentiles": [
            {"percentile": pc.percentile, "days": pc.days, "label_ru": f"P{pc.percentile} (дней)"}
            for pc in forecast_result.percentiles
        ],
        "histogram": [{"days": b.days, "count": b.count} for b in forecast_result.histogram],
        "warnings": [labels_ru.warning_obj(w) for w in forecast_result.warnings],
    }


def _resolve_display_name_fn(facts: list["jc.IssueFacts"], mrs: list, pipelines: list):
    jira_names: dict[str, str] = {}
    for f in facts:
        if f.assignee and f.assignee_display_name and f.assignee not in jira_names:
            jira_names[f.assignee] = f.assignee_display_name
    gitlab_names: dict[str, str] = {}
    for m in mrs:
        author = m.get("author")
        name = m.get("author_name")
        if author and name and author not in gitlab_names:
            gitlab_names[author] = name
    for p in pipelines:
        u = p.get("user_username")
        name = p.get("user_name")
        if u and name and u not in gitlab_names:
            gitlab_names[u] = name

    def resolve(login: str) -> tuple:
        if not login:
            return login, "login"
        if login in jira_names:
            return jira_names[login], "jira"
        if login in gitlab_names:
            return gitlab_names[login], "gitlab"
        return login, "login"

    return resolve


def _build_done_flows(personal_facts: list["jc.IssueFacts"], final_statuses: Iterable[str]) -> list[dict]:
    """One dict per Jira issue that reached a final status inside the axis
    scope — the shared raw material for team_series, people[].by_sprint,
    issue-type distributions and the CSV export (bucketing timestamp =
    done_at, SPEC §C.10)."""
    out = []
    for f in personal_facts:
        issue_input = _to_personal_issue_input(f)
        flow = personal_metrics.compute_issue_flow(issue_input, final_statuses)
        if not flow.is_done:
            continue
        out.append(
            {
                "key": f.key, "login": f.assignee, "type": f.type, "is_bug": flow.is_bug,
                "first_in_progress": flow.first_in_progress,
                "done_at": flow.done_at, "cycle_time_hours": flow.cycle_time_hours,
                "rework_count": flow.rework_count, "story_points": f.story_points, "qa_estimation": f.qa_estimation,
            }
        )
    return out


def _issue_type_dist(flows: Iterable[dict]) -> dict:
    counter = Counter(f["type"] for f in flows if f.get("type"))
    return dict(sorted(counter.items()))


def _outside_warning(counts: dict) -> Optional[dict]:
    order = [("issues", "задач"), ("merge_requests", "MR"), ("pipelines", "пайплайнов"), ("deployments", "деплоев")]
    parts = [f"{label}: {counts.get(key, 0)}" for key, label in order if counts.get(key, 0)]
    if not parts:
        return None
    return labels_ru.warning_obj("WARN_OUTSIDE_SPRINTS", ", ".join(parts))


def _dedupe_warning_objs(objs: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for o in objs:
        key = (o["code"], o.get("detail"))
        if key in seen:
            continue
        seen.add(key)
        out.append(o)
    return out


def _collect_status_names(payloads: list[metrics_mod.Payload]) -> set:
    names = set()
    for p in payloads:
        for is_ in p.issues:
            for s in (is_.status_initial, is_.status_before_end, is_.status_end):
                if s:
                    names.add(s)
            for ds in is_.day_statuses:
                if ds.status:
                    names.add(ds.status)
    return names


def _build_statuses_labels(payloads: list[metrics_mod.Payload], cfg: model.StatusCategoryConfig, status_labels_cfg: dict) -> dict:
    out = {}
    for name in sorted(_collect_status_names(payloads)):
        category, _ = model.effective_status_category(name, cfg)
        out[name] = {
            "category": category,
            "category_ru": labels_ru.status_category_label_ru(category),
            "override_ru": status_labels_cfg.get(name),
        }
    return out


def _convert_engineering_section_warnings(section: dict, *, per_project_has_warnings: bool = True) -> dict:
    """Converts a pipelines/deployments/coverage section's bare warning
    codes into objects. Pipeline/deployment per_project rows carry their own
    `warnings` (engineering_metrics.py's own shape); coverage per_project
    rows never had that key at all — `per_project_has_warnings=False` keeps
    it that way instead of injecting a spurious empty list (SPEC §B)."""
    section = dict(section)
    section["warnings"] = [labels_ru.warning_obj_from_suffixed(w) for w in section.get("warnings", [])]
    if "per_project" in section:
        if per_project_has_warnings:
            section["per_project"] = [
                {**row, "warnings": [labels_ru.warning_obj_from_suffixed(w) for w in row.get("warnings", [])]}
                for row in section["per_project"]
            ]
        else:
            section["per_project"] = [dict(row) for row in section["per_project"]]
    return section


def _build_engineering_v2(
    available: bool,
    reason_ru: Optional[str],
    gitlab_data: Optional[dict],
    axis: list["sprint_series.AxisSprint"],
) -> dict:
    if not available:
        return {
            "available": False, "reason_ru": reason_ru,
            "pipelines": {"count": 0, "failed": 0, "success_rate_pct": None, "per_week": None, "per_project": [], "warnings": []},
            "deployments": {"count": 0, "failed": 0, "success_rate_pct": None, "per_week": None, "per_project": [], "warnings": []},
            "coverage": {"coverage_avg_pct": None, "sample_count": 0, "per_project": [], "warnings": []},
            "window_applied": {"merge_requests": False, "pipelines": False, "deployments": False, "coverage": False},
            "by_sprint": [
                {
                    "sprint_id": a.id, "sprint_name": a.name,
                    "pipeline_count": 0, "pipeline_success_rate_pct": None,
                    "deployment_count": 0, "deployment_success_rate_pct": None,
                    "mr_count": 0, "mr_merged_count": 0,
                    "avg_mr_cycle_time_hours": None, "avg_mr_changes_count": None,
                }
                for a in axis
            ],
        }
    eng = engineering_metrics.build_engineering_metrics(
        pipelines=gitlab_data["pipelines"], deployments=gitlab_data["deployments"],
        coverage=gitlab_data["coverage"], window_applied=gitlab_data["window_applied"],
    )
    return {
        "available": True,
        "reason_ru": None,
        "pipelines": _convert_engineering_section_warnings(eng["pipelines"]),
        "deployments": _convert_engineering_section_warnings(eng["deployments"]),
        "coverage": _convert_engineering_section_warnings(eng["coverage"], per_project_has_warnings=False),
        "window_applied": eng["window_applied"],
        "by_sprint": sprint_series.build_engineering_by_sprint(
            axis, gitlab_data["merge_requests"], gitlab_data["pipelines"], gitlab_data["deployments"]
        ),
    }


def _build_risks(overview: dict, engineering_data: dict, team_rework_rate_pct: Optional[float]) -> list[dict]:
    risks = []
    # overview["pipeline_success_rate_pct"] is already null when no pipeline
    # was ever measured (count == 0) -- reusing it here (instead of reading
    # engineering_data directly) keeps the "zero pipelines is not a measured
    # rate" rule in one place.
    pipeline_rate = overview["pipeline_success_rate_pct"]
    if overview["pr_cycle_time_avg_hours"] is not None and pipeline_rate is not None and pipeline_rate < 80:
        risks.append({"key": "speed_vs_quality", "title_ru": labels_ru.RISK_TITLES_RU["speed_vs_quality"],
                      "body_ru": labels_ru.RISK_BODY_SPEED_VS_QUALITY_RU})
    if overview["defect_rate_pct"] is not None and overview["defect_rate_pct"] > 20:
        risks.append({"key": "defect_rate", "title_ru": labels_ru.RISK_TITLES_RU["defect_rate"],
                      "body_ru": labels_ru.risk_body_defect_rate_ru(overview["defect_rate_pct"])})
    if team_rework_rate_pct is not None and team_rework_rate_pct > 30:
        risks.append({"key": "rework", "title_ru": labels_ru.RISK_TITLES_RU["rework"],
                      "body_ru": labels_ru.risk_body_rework_ru(team_rework_rate_pct)})
    coverage_pct = engineering_data["coverage"]["coverage_avg_pct"] if engineering_data["available"] else None
    if coverage_pct is not None and coverage_pct < 60:
        risks.append({"key": "coverage", "title_ru": labels_ru.RISK_TITLES_RU["coverage"],
                      "body_ru": labels_ru.risk_body_coverage_ru(coverage_pct)})
    if not risks:
        risks.append({"key": "all_ok", "title_ru": labels_ru.RISK_TITLES_RU["all_ok"], "body_ru": labels_ru.RISK_BODY_ALL_OK_RU})
    return risks


def _heatmap_header_ru(header: list[str]) -> list[str]:
    out = []
    for h in header:
        if h == "Роль":
            out.append("Роль")
        elif h in labels_ru.COLUMN_LABELS_RU:
            out.append(labels_ru.COLUMN_LABELS_RU[h])
        else:
            out.append(h)
    return out


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
    out_dir: str = "out",
    no_gitlab: bool = False,
    status_labels: Optional[dict[str, str]] = None,
) -> dict:
    """Builds the full schema-v2 report dict (json.dumps-able as-is, once run
    through `_to_jsonable`) from one Jira fetch plus an optional GitLab fetch.
    See `build_combined_report_with_raw` for the variant that also returns
    the raw fetched material `cli.py` needs for `out/raw/*` and the CSVs."""
    report, _raw = build_combined_report_with_raw(
        client,
        sprint_ids=sprint_ids, sprint_names=sprint_names, board_id_override=board_id_override,
        history_sprint_count=history_sprint_count, status_map=status_map, cancelled_statuses=cancelled_statuses,
        story_points_field_id=story_points_field_id, seed=seed, iterations=iterations, target_items=target_items,
        now=now, gitlab_client_obj=gitlab_client_obj, gitlab_projects=gitlab_projects, employees=employees,
        final_statuses=final_statuses, include_personal=include_personal, fetch_mr_details=fetch_mr_details,
        fetch_pipeline_user=fetch_pipeline_user, out_dir=out_dir, no_gitlab=no_gitlab, status_labels=status_labels,
    )
    return report


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
    """Jira-only convenience wrapper: the same schema-v2 report as
    `build_combined_report`, with `people`/`engineering` unavailable (no
    GitLab configured)."""
    return build_combined_report(
        client, sprint_ids=sprint_ids, sprint_names=sprint_names, board_id_override=board_id_override,
        history_sprint_count=history_sprint_count, status_map=status_map, cancelled_statuses=cancelled_statuses,
        story_points_field_id=story_points_field_id, seed=seed, iterations=iterations, target_items=target_items,
        now=now, gitlab_client_obj=None,
    )


def build_combined_report_with_raw(
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
    out_dir: str = "out",
    no_gitlab: bool = False,
    status_labels: Optional[dict[str, str]] = None,
) -> tuple[dict, dict]:
    """Same report as `build_combined_report`, PLUS a `raw` bundle
    (`{"facts", "mrs", "pipelines", "deployments", "coverage", "done_flows",
    "axis", "cfg", "final_statuses", "gitlab_configured"}`) — the pre-JSON
    material `csv_export.write_all`/`out_writer.write_raw` need that has no
    place in the JSON contract itself. `cli.py`'s `run` command is the only
    caller of this variant; every other caller (tests, `report_data.main()`)
    uses `build_combined_report`."""
    now = now or datetime.now(UTC)
    status_labels = status_labels or {}

    log.info("Определение спринтов и загрузка задач из Jira")
    core, facts, targets = _build_report_core(
        client, sprint_ids=sprint_ids, sprint_names=sprint_names, board_id_override=board_id_override,
        history_sprint_count=history_sprint_count, status_map=status_map, cancelled_statuses=cancelled_statuses,
        story_points_field_id=story_points_field_id, seed=seed, iterations=iterations, target_items=target_items,
        now=now,
    )
    log.info("Метрики спринтов посчитаны")

    all_sprints: list[_ResolvedSprint] = core["all_sprints"]
    sprints_by_id: dict[int, metrics_mod.Payload] = core["final"]
    target_ids = {rs.sprint.id for rs in all_sprints if rs.target}
    axis = sprint_series.build_axis(all_sprints)
    axis_window_start = min(rs.sprint.start_at for rs in all_sprints)
    axis_window_end = max(rs.sprint.end_at for rs in all_sprints)

    per_sprint_warnings: dict[int, list[str]] = core.pop("_per_sprint_warnings", {})

    target_payloads = [sprints_by_id[rs.sprint.id] for rs in all_sprints if rs.target]
    cfg: model.StatusCategoryConfig = core["cfg"]

    # -- personal facts: v3 scope is ANY axis sprint, not target-only ------
    axis_ids = {rs.sprint.id for rs in all_sprints}
    personal_facts = [f for f in facts if any(f.membership_by_sprint.get(sid) for sid in axis_ids)]

    gitlab_configured = gitlab_client_obj is not None
    people_available = False
    people_reason_ru: Optional[str] = None
    engineering_available = False
    engineering_reason_ru: Optional[str] = _GITLAB_NOT_CONFIGURED_RU
    gitlab_fetch_issues = {"skipped_projects": [], "mr_fetch_errors": [], "deployment_warnings": []}
    gitlab_data: Optional[dict] = None
    mrs: list = []
    pipelines: list = []
    deployments: list = []
    coverage: list = []
    gitlab_request_count = None
    gitlab_fetch_mr_details_echo = None
    gitlab_fetch_pipeline_user_echo = None

    if gitlab_configured:
        window = glc.Window(start=axis_window_start, end=axis_window_end)
        effective_employees = list(employees) if include_personal else []
        log.info("Загрузка данных GitLab (окно %s — %s)", model.format_date(axis_window_start), model.format_date(axis_window_end))
        gitlab_data = glc.fetch_team_data(
            gitlab_client_obj, projects=list(gitlab_projects), employees=effective_employees, window=window,
            fetch_mr_details=fetch_mr_details, fetch_pipeline_user=fetch_pipeline_user,
        )
        log.info("Данные GitLab получены (запросов: %s)", gitlab_data.get("request_count"))
        gitlab_request_count = gitlab_data.get("request_count")
        gitlab_fetch_mr_details_echo = fetch_mr_details
        gitlab_fetch_pipeline_user_echo = fetch_pipeline_user
        gitlab_fetch_issues = {
            "skipped_projects": [
                {**s, "message_ru": labels_ru.warn_message(s.get("code") or "ERR_UNKNOWN")}
                for s in gitlab_data.get("skipped_projects", [])
            ],
            "mr_fetch_errors": [
                {**e, "message_ru": labels_ru.warn_message("MR_FETCH_ERROR")}
                for e in gitlab_data.get("mr_fetch_errors", [])
            ],
            "deployment_warnings": [
                {**w, "message_ru": labels_ru.warn_message(w.get("code") or "ERR_UNKNOWN")}
                for w in gitlab_data.get("deployment_warnings", [])
            ],
        }
        mrs = gitlab_data["merge_requests"]
        pipelines = gitlab_data["pipelines"]
        deployments = gitlab_data["deployments"]
        coverage = gitlab_data["coverage"]
        engineering_available, engineering_reason_ru = True, None
        people_available = include_personal
        people_reason_ru = None if include_personal else _NO_PERSONAL_RU
    else:
        people_reason_ru = _GITLAB_NOT_CONFIGURED_RU

    log.info("Вычисление персональных метрик")
    resolve_name = _resolve_display_name_fn(facts, mrs, pipelines)
    final_statuses = list(final_statuses)
    all_done_flows = _build_done_flows(personal_facts, final_statuses)

    people: list = []
    if people_available:
        personal_issues = [_to_personal_issue_input(f) for f in personal_facts]
        for user in personal_metrics._all_users(mrs, personal_issues):
            # pipelines=pipelines lets personal_metrics() run its own
            # _pipeline_user_attribution_missing check -- that is the ONLY
            # test that can tell "attribution was never collected" apart
            # from "this person simply triggered zero pipelines" (both give
            # personal_pipeline_success() == None). Re-deriving that
            # distinction here from a bare "pipelines is non-empty" check
            # would warn every zero-pipeline person even when attribution
            # worked fine for everyone else.
            snapshot = personal_metrics.personal_metrics(
                user, mrs=mrs, issues=personal_issues, pipelines=pipelines, final_statuses=final_statuses
            )
            person_warnings = list(snapshot["warnings"])
            tasks_done = snapshot["tasks_done"]
            if tasks_done == 0:
                rework_rate_pct = 0.0
                person_warnings.append(f"{model.WARN_DIVISION_BY_ZERO}:rework_rate_pct")
            else:
                rework_rate_pct = round(snapshot["rework_tasks"] / tasks_done * 100.0, 1)
            pipeline_rate = snapshot["pipeline_success_rate"]
            pipeline_success_rate_pct = None if pipeline_rate is None else round(pipeline_rate * 100.0, 1)

            display_name, display_source = resolve_name(user)
            person_flows = [f for f in all_done_flows if f["login"] == user]
            person_mrs = [m for m in mrs if m.get("author") == user]
            people.append(
                {
                    "login": user, "display_name": display_name, "display_name_source": display_source,
                    "metrics": {
                        "mr_count": snapshot["mr_count"], "mr_merged_count": snapshot["mr_merged_count"],
                        "mr_closed_count": snapshot["mr_closed_count"], "mr_merge_rate_pct": snapshot["mr_merge_rate_pct"],
                        "mr_cycle_time_avg_hours": snapshot["mr_cycle_time_avg_hours"],
                        "mr_cycle_time_median_hours": snapshot["mr_cycle_time_median_hours"],
                        "mr_diff_size_avg": snapshot["mr_diff_size_avg"],
                        "mr_diff_size_available_count": snapshot["mr_diff_size_available_count"],
                        "mr_commits_avg": snapshot["mr_commits_avg"], "mr_commits_sum": snapshot["mr_commits_sum"],
                        "mr_changes_count_avg": snapshot["mr_changes_count_avg"],
                        "mr_changes_count_sum": snapshot["mr_changes_count_sum"],
                        "tasks_done": snapshot["tasks_done"], "issue_count": snapshot["issue_count"],
                        "bug_count": snapshot["bug_count"], "defect_rate_pct": snapshot["defect_rate_pct"],
                        "task_cycle_time_avg_hours": snapshot["task_cycle_time_avg_hours"],
                        "task_cycle_time_median_hours": snapshot["task_cycle_time_median_hours"],
                        "rework_total": snapshot["rework_total"], "rework_tasks": snapshot["rework_tasks"],
                        "rework_rate_pct": rework_rate_pct,
                        "story_points_total": snapshot["story_points_total"], "story_points_avg": snapshot["story_points_avg"],
                        "qa_estimation_total": snapshot["qa_estimation_total"], "qa_estimation_avg": snapshot["qa_estimation_avg"],
                        "linked_tasks": snapshot["linked_tasks"], "mr_with_jira_key": snapshot["mr_with_jira_key"],
                        "mr_per_task": snapshot["mr_per_task"], "pipeline_success_rate_pct": pipeline_success_rate_pct,
                    },
                    "issue_type_dist": _issue_type_dist(person_flows),
                    "warnings": [labels_ru.warning_obj_from_suffixed(w) for w in person_warnings],
                    "by_sprint": sprint_series.build_people_by_sprint(axis, person_flows, person_mrs),
                }
            )
        people.sort(key=lambda p: (-p["metrics"]["tasks_done"], -p["metrics"]["mr_count"], p["display_name"], p["login"]))

    log.info("Вычисление инженерных метрик")
    engineering_data = _build_engineering_v2(engineering_available, engineering_reason_ru, gitlab_data, axis)

    log.info("Построение динамики по спринтам")
    team_series, outside_counts = sprint_series.build_team_series(axis, all_done_flows, mrs, pipelines, deployments)
    people_series = sprint_series.build_people_series(axis, people) if people_available else []

    # -- overview ------------------------------------------------------
    tasks_done_total = sum(p["metrics"]["tasks_done"] for p in people)
    mr_total = sum(p["metrics"]["mr_count"] for p in people)
    bug_total = sum(p["metrics"]["bug_count"] for p in people)
    pr_cycle_avg = statistics.mean(v) if (v := [p["metrics"]["mr_cycle_time_avg_hours"] for p in people if p["metrics"]["mr_cycle_time_avg_hours"] is not None]) else None
    task_cycle_avg = statistics.mean(v) if (v := [p["metrics"]["task_cycle_time_avg_hours"] for p in people if p["metrics"]["task_cycle_time_avg_hours"] is not None]) else None
    top_warnings_extra: list[dict] = []
    if tasks_done_total == 0 and people_available:
        top_warnings_extra.append(labels_ru.warning_obj(model.WARN_DIVISION_BY_ZERO, labels_ru.metric_label_ru("defect_rate_pct")))
        defect_rate_pct = 0.0
    elif not people_available:
        defect_rate_pct = None
    else:
        defect_rate_pct = round(bug_total / tasks_done_total * 100.0, 1)
    team_rework_rate_pct = None
    if people_available and tasks_done_total > 0:
        team_rework_rate_pct = round(sum(p["metrics"]["rework_tasks"] for p in people) / tasks_done_total * 100.0, 1)

    overview = {
        "employees": len(people),
        "mr_total": mr_total,
        "tasks_done_total": tasks_done_total,
        "pr_cycle_time_avg_hours": round(pr_cycle_avg, 2) if pr_cycle_avg is not None else None,
        "task_cycle_time_avg_hours": round(task_cycle_avg, 2) if task_cycle_avg is not None else None,
        "pipeline_success_rate_pct": (
            engineering_data["pipelines"]["success_rate_pct"]
            if engineering_data["available"] and engineering_data["pipelines"]["count"] > 0
            else None
        ),
        "deploy_success_rate_pct": (
            engineering_data["deployments"]["success_rate_pct"]
            if engineering_data["available"] and engineering_data["deployments"]["count"] > 0
            else None
        ),
        "defect_rate_pct": defect_rate_pct,
        # Derived from the SAME source as employees/mr_total/tasks_done_total
        # (the people[] list) so the donut's slices never outnumber its own
        # centre value: when people are unavailable those totals are all 0,
        # so the distribution must be empty too, not built from the wider
        # (GitLab-independent) all_done_flows set.
        "issue_type_dist": _issue_type_dist(f for f in all_done_flows if f["login"]) if people_available else {},
    }

    primary_target = next(p for a, p in [(a, sprints_by_id[a.id]) for a in reversed(axis)] if a.id in target_ids)
    board_kpi = _build_board_kpi(axis, sprints_by_id, primary_target, core["kpi"])

    # core["warnings"] may already carry a bare WARN_SPRINT_ACTIVE_PARTIAL
    # (metrics_mod.sprint_active_partial_warning, detail-less) whenever any
    # sprint in scope is not closed -- the same condition `active` checks
    # below, which attaches the sprint name as detail. Drop the bare one so
    # only the more informative, named version survives _dedupe_warning_objs
    # (it keys on (code, detail), so a None-detail and a named-detail copy of
    # the same code would otherwise both survive).
    core_warnings = [w for w in core["warnings"] if w != model.WARN_SPRINT_ACTIVE_PARTIAL]
    warnings = [labels_ru.warning_obj(w) for w in core_warnings]
    active = next((rs for rs in all_sprints if rs.sprint.state != "closed"), None)
    if active is not None:
        warnings.append(labels_ru.warning_obj(model.WARN_SPRINT_ACTIVE_PARTIAL, active.sprint.name))
    outside_warning = _outside_warning(outside_counts)
    if outside_warning is not None:
        warnings.append(outside_warning)
    warnings.extend(top_warnings_extra)
    warnings = _dedupe_warning_objs(warnings)

    forecast_obj = _build_forecast_v2(
        core["forecast_result"], core["forecast_error"], core["resolved_target_items"], target_items, core["forecast_payloads"]
    )

    risks = _build_risks(overview, engineering_data, team_rework_rate_pct)

    export = core["export"]
    export_tables = {
        "heatmap": {
            "header": export["heatmap_table"]["header"],
            "header_ru": _heatmap_header_ru(export["heatmap_table"]["header"]),
            "rows": export["heatmap_table"]["rows"],
            "csv_filename": "heatmap.csv",
        },
        "board": {
            "header": export["board_table"]["header"],
            "header_ru": [labels_ru.COLUMN_LABELS_RU[h] for h in export["board_table"]["header"]],
            "rows": export["board_table"]["rows"],
            "csv_filename": "board.csv",
        },
    }

    log.info("Сборка итогового отчёта")
    report = {
        "schema_version": metrics_mod.SCHEMA_VERSION,
        "board": {"id": core["board_id"], "name": core["board_name"]},
        "params": {
            "sprint_ids": core["sprint_ids"], "sprint_names": core["sprint_names"],
            "board_id": core["board_id"], "history_sprint_count": core["history_sprint_count"],
            "seed": core["seed"], "iterations": core["iterations"],
            "target_items_requested": core["target_items_requested"], "target_items_resolved": core["resolved_target_items"],
            "generated_at": now, "tool_version": _tool_version(), "out_dir": out_dir,
            "no_gitlab": no_gitlab, "no_personal": not include_personal,
            "gitlab_window": {"start": axis_window_start, "end": axis_window_end} if gitlab_configured else None,
            "gitlab_request_count": gitlab_request_count,
            "gitlab_fetch_mr_details": gitlab_fetch_mr_details_echo,
            "gitlab_fetch_pipeline_user": gitlab_fetch_pipeline_user_echo,
        },
        "warnings": warnings,
        "sprint_axis": axis,
        "sprints": _build_sprints_section(axis, sprints_by_id, per_sprint_warnings, target_ids),
        "board_kpi": board_kpi,
        "overview": overview,
        "burndown": _build_burndown_v2(target_payloads, now),
        "heatmap": _build_heatmap_v2(target_payloads, resolve_name),
        "issue_breakdown": _build_issue_breakdown(target_payloads, cfg),
        "forecast": forecast_obj,
        "people_available": people_available,
        "people_reason_ru": people_reason_ru,
        "people": people,
        "people_individual_jira": _build_people_individual_jira(target_payloads, cfg, resolve_name),
        "engineering": engineering_data,
        "team_series": team_series,
        "people_series": people_series,
        "export_tables": export_tables,
        "glossary": labels_ru.GLOSSARY_RU,
        "metric_defs": labels_ru.METRIC_DEFS_RU,
        "risks": risks,
        "labels": {
            "roles": labels_ru.ROLES_RU,
            "status_categories": labels_ru.STATUS_CATEGORIES_RU,
            "statuses": _build_statuses_labels(core["all_payloads"], cfg, status_labels),
            "columns": labels_ru.COLUMN_LABELS_RU,
            "jira_label_note_ru": labels_ru.JIRA_LABEL_NOTE_RU,
        },
        "semantics_notes": list(_SEMANTICS_NOTES_RU),
        "gitlab_fetch_issues": gitlab_fetch_issues,
    }

    raw = {
        "facts": personal_facts, "mrs": mrs, "pipelines": pipelines, "deployments": deployments, "coverage": coverage,
        "done_flows": all_done_flows, "axis": axis, "cfg": cfg, "final_statuses": final_statuses,
        "gitlab_configured": gitlab_configured, "heatmap_csv_text": export["heatmap_csv"],
    }
    return _to_jsonable(report), raw


def main(argv: Optional[list[str]] = None) -> int:
    try:
        run_cfg = config_mod.parse_args(argv)
    except config_mod.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    logging_setup.setup_logging(verbose=run_cfg.verbose, quiet=run_cfg.quiet)

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
            out_dir=run_cfg.out_dir,
            no_gitlab=run_cfg.no_gitlab,
            status_labels=run_cfg.file_config.status_labels,
        )
    except (ReportError, jc.JiraError, glc.GitLabError) as e:
        # glc.GitLabError here is a genuine GitLab-side fault during a
        # CONFIGURED fetch (most notably AUTH_FAILED, which
        # gitlab_client.fetch_team_data() deliberately lets propagate rather
        # than degrade — a revoked token must never look like a clean,
        # all-zero report). This is a different case from GitLab being
        # merely unconfigured/--no-gitlab, which build_combined_report()
        # already turns into report["engineering"/"people_available"] =
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
