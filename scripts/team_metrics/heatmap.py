"""Heatmap: task x working-day matrix for target sprints.

Python port of internal/domain/metrics/heatmap.go + heatmapFromPayload
(internal/app/board/report.go). No numeric buckets or color thresholds live
here (SPEC §5) — only the raw status name + category per cell; a renderer
decides how to color status_category.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import labels_ru
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
    # v2 enrichment (SPEC §B.8) — the renderer needs no other lookup to draw
    # a row header or its tooltip.
    epic_key: str = ""
    assignee_login: str = ""
    assignee_display_name: str = ""
    story_points: float = 0.0
    qa_estimation: float = 0.0
    role: str = ""
    role_ru: str = ""
    labels: list[str] = field(default_factory=list)
    status_initial: str = ""
    status_end: str = ""


@dataclass
class Heatmap:
    sprint_id: int
    days: list[str]
    rows: list[HeatmapRow]


def build_heatmap(
    sprint_id: int, working_days: list[str], issues: list, display_names: Optional[dict[str, str]] = None
) -> Heatmap:
    display_names = display_names or {}
    rows = []
    for is_ in issues:
        cells = [HeatmapCell(date=ds.date, status=ds.status, status_category=ds.status_category) for ds in is_.day_statuses]
        rows.append(
            HeatmapRow(
                issue_key=is_.key,
                cells=cells,
                epic_key=is_.epic_key,
                assignee_login=is_.assignee,
                assignee_display_name=display_names.get(is_.assignee, is_.assignee),
                story_points=is_.story_points,
                qa_estimation=is_.qa_estimation,
                role=is_.role,
                role_ru=labels_ru.role_label_ru(is_.role) if is_.role else "",
                labels=list(is_.labels),
                status_initial=is_.status_initial,
                status_end=is_.status_end,
            )
        )
    return Heatmap(sprint_id=sprint_id, days=list(working_days), rows=rows)


def heatmap_from_payload(payload: "metrics_mod.Payload", display_names: Optional[dict[str, str]] = None) -> Heatmap:
    """Same as heatmapFromPayload (report.go): the payload already stores
    working_days as YYYY-MM-DD strings, so no parse/reformat round-trip is
    needed (Go re-parses them to time.Time only to hand them to a function
    that immediately reformats them back to the same strings)."""
    return build_heatmap(payload.sprint.id, payload.sprint.working_days, payload.issues, display_names)
