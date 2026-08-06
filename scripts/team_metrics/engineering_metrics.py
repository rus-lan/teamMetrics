"""Team-level GitLab engineering metrics: pipelines, deploys, coverage
(`.research/ai-integration-metrics/SPEC.md` §5.5, §5.9 infra rows, §6
"Team-only" split).

Input is the flat `pipelines`/`deployments`/`coverage` lists
`gitlab_client.fetch_team_data()` returns (or the equivalent per-project
lists from `GitLabClient.pipelines()`/`.deployments()`/`.coverage()`
concatenated across projects) — this module does no fetching of its own.

Bug fix vs the source skill: `merge_report.py`'s `report_team.csv` summed
each employee's row, and every employee row carried the *same* whole-team
pipeline/deployment counters (`merge_report.py:232-233` — "pipe_rows =
pipelines, dep_rows = deployments — the WHOLE team's rows, not filtered by
user"), so `total_pipelines`/`total_deployments` came out as
`team_count * len(employees)` (SPEC.md §5.5/GAPS §11.5). This module never
aggregates through a per-employee loop — every function here reads the flat
list exactly once, so that multiplication has no code path to happen through.
"""

from __future__ import annotations

import statistics
from datetime import datetime
from typing import Iterable, Optional

WARN_DIVISION_BY_ZERO = "WARN_DIVISION_BY_ZERO"

# The GitLab window params this data was (or wasn't) fetched with — see
# gitlab_client.Window/fetch_team_data(). Only used for the per-project
# "weeks in span" frequency, hence the 1/168-week floor (a single day).
_MIN_WEEKS = 1.0 / 168.0


def _avg(values: Iterable[Optional[float]]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return round(statistics.mean(vals), 2) if vals else None


def _pct_or_zero(num: float, den: float, warnings: list, metric_name: str) -> float:
    if not den:
        warnings.append(f"{WARN_DIVISION_BY_ZERO}:{metric_name}")
        return 0.0
    return round(num / den * 100.0, 1)


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


def _weeks_in_span(rows: list, date_key: str) -> Optional[float]:
    dates = [d for r in rows if (d := _parse_iso(r.get(date_key)))]
    if len(dates) < 2:
        return None
    days = (max(dates) - min(dates)).total_seconds() / 86400.0
    if days <= 0:
        return None
    return max(days / 7.0, _MIN_WEEKS)


def _success_rate_pct(rows: list, warnings: list, metric_name: str) -> float:
    """(total - failed) / total, matching merge_report.py's success_rate:
    only a literal status == "failed" counts as failure — other terminal
    statuses (canceled, skipped, ...) count toward the numerator, same as
    the source skill (SPEC.md §5.7)."""
    total = len(rows)
    failed = sum(1 for r in rows if r.get("status") == "failed")
    return _pct_or_zero(total - failed, total, warnings, metric_name)


def _group_by_project(rows: list) -> dict:
    groups: dict = {}
    for r in rows:
        groups.setdefault(r.get("project") or "", []).append(r)
    return groups


# --------------------------------------------------------------------------
# Pipelines
# --------------------------------------------------------------------------


def team_pipeline_metrics(pipelines: list) -> dict:
    warnings: list = []
    total = len(pipelines)
    failed = sum(1 for p in pipelines if p.get("status") == "failed")
    success_rate_pct = _success_rate_pct(pipelines, warnings, "pipeline_success_rate_pct")
    # Bucketed on created_at — faithful to the source skill, and consistent
    # with gitlab_client.py's pipelines() window filter, which stays on
    # updated_after/before (SPEC.md M5's inherited-mismatch note applies to
    # the filter vs the window, not to this bucketing key itself).
    weeks = _weeks_in_span(pipelines, "created_at")
    if weeks is None and total > 0:
        # Data exists but no usable span to rate it over — distinct from
        # "genuinely zero pipelines" (n2), so a renderer can tell them apart.
        warnings.append("WARN_PER_WEEK_UNAVAILABLE")
    per_week = round(total / weeks, 2) if weeks else None

    per_project = []
    for project, rows in sorted(_group_by_project(pipelines).items()):
        row_warnings: list = []
        per_project.append(
            {
                "project": project,
                "count": len(rows),
                "failed": sum(1 for r in rows if r.get("status") == "failed"),
                "success_rate_pct": _success_rate_pct(rows, row_warnings, "success_rate_pct"),
                "warnings": row_warnings,
            }
        )

    return {
        "count": total,
        "failed": failed,
        "success_rate_pct": success_rate_pct,
        "per_week": per_week,
        "per_project": per_project,
        "warnings": warnings,
    }


# --------------------------------------------------------------------------
# Deployments
# --------------------------------------------------------------------------


def team_deployment_metrics(deployments: list) -> dict:
    warnings: list = []
    total = len(deployments)
    failed = sum(1 for d in deployments if d.get("status") == "failed")
    success_rate_pct = _success_rate_pct(deployments, warnings, "deploy_success_rate_pct")
    # Bucketed on finished_at (M5) — consistent with gitlab_client.py's
    # deployments() window filter, which now uses finished_after/before;
    # created_at would count a deploy started before the window but
    # finished inside it as outside the span, and vice versa.
    weeks = _weeks_in_span(deployments, "finished_at")
    if weeks is None and total > 0:
        warnings.append("WARN_PER_WEEK_UNAVAILABLE")
    per_week = round(total / weeks, 2) if weeks else None

    per_project = []
    for project, rows in sorted(_group_by_project(deployments).items()):
        row_warnings: list = []
        per_project.append(
            {
                "project": project,
                "count": len(rows),
                "failed": sum(1 for r in rows if r.get("status") == "failed"),
                "success_rate_pct": _success_rate_pct(rows, row_warnings, "success_rate_pct"),
                "warnings": row_warnings,
            }
        )

    return {
        "count": total,
        "failed": failed,
        "success_rate_pct": success_rate_pct,
        "per_week": per_week,
        "per_project": per_project,
        "warnings": warnings,
    }


# --------------------------------------------------------------------------
# Coverage — unit is 0-100 percent throughout, never re-multiplied
# --------------------------------------------------------------------------


def team_coverage_metrics(coverage: list) -> dict:
    vals = [c.get("coverage") for c in coverage if c.get("coverage") is not None]
    warnings: list = []
    if coverage and not vals:
        warnings.append("WARN_COVERAGE_UNAVAILABLE")

    per_project = []
    for project, rows in sorted(_group_by_project(coverage).items()):
        project_vals = [r.get("coverage") for r in rows if r.get("coverage") is not None]
        per_project.append(
            {
                "project": project,
                "coverage_avg_pct": _avg(project_vals),
                "sample_count": len(project_vals),
            }
        )

    return {
        "coverage_avg_pct": _avg(vals),
        "sample_count": len(vals),
        "per_project": per_project,
        "warnings": warnings,
    }


# --------------------------------------------------------------------------
# Top-level combiner
# --------------------------------------------------------------------------


def build_engineering_metrics(
    *,
    pipelines: list,
    deployments: list,
    coverage: list,
    window_applied: Optional[dict] = None,
) -> dict:
    """`window_applied` should be the flag dict gitlab_client.fetch_team_data()
    returns (or an equivalent caller-built one) — passed through unchanged so
    a report layer can disclaim unfiltered pipeline/deploy/coverage numbers."""
    return {
        "pipelines": team_pipeline_metrics(pipelines),
        "deployments": team_deployment_metrics(deployments),
        "coverage": team_coverage_metrics(coverage),
        "window_applied": window_applied or {},
    }
