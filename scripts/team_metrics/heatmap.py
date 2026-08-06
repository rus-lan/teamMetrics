"""Heatmap: task x working-day matrix for target sprints.

Python port of internal/domain/metrics/heatmap.go + heatmapFromPayload
(internal/app/board/report.go). No numeric buckets or color thresholds live
here (SPEC §5) — only the raw status name + category per cell; a renderer
decides how to color status_category.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import metrics as metrics_mod


@dataclass
class HeatmapCell:
    date: str
    status: str
    status_category: str


@dataclass
class HeatmapRow:
    issue_key: str
    cells: list[HeatmapCell]


@dataclass
class Heatmap:
    sprint_id: int
    days: list[str]
    rows: list[HeatmapRow]


def build_heatmap(sprint_id: int, working_days: list[str], issues: list) -> Heatmap:
    rows = []
    for is_ in issues:
        cells = [HeatmapCell(date=ds.date, status=ds.status, status_category=ds.status_category) for ds in is_.day_statuses]
        rows.append(HeatmapRow(issue_key=is_.key, cells=cells))
    return Heatmap(sprint_id=sprint_id, days=list(working_days), rows=rows)


def heatmap_from_payload(payload: "metrics_mod.Payload") -> Heatmap:
    """Same as heatmapFromPayload (report.go): the payload already stores
    working_days as YYYY-MM-DD strings, so no parse/reformat round-trip is
    needed (Go re-parses them to time.Time only to hand them to a function
    that immediately reformats them back to the same strings)."""
    return build_heatmap(payload.sprint.id, payload.sprint.working_days, payload.issues)
