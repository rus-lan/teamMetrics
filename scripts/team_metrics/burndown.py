"""Burndown series reconstructed from an already-built sprint payload.

Python port of burndownFromPayload (internal/app/board/report.go) — the
production code path (the alternative domain implementation in Go's
burndown.go has no production caller, per SPEC §6, and is intentionally not
ported here).

Calendar days (weekends included), status carried forward across weekends
from the last known working-day snapshot, remaining excludes done/cancelled,
ideal is a linear burn of committed_sp over the window.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from . import metrics as metrics_mod
from . import model


@dataclass
class BurndownPoint:
    date: str
    remaining_items: int
    remaining_sp: float
    ideal_sp: float


def _advance_day_status_index(days: list[model.DayStatus], from_idx: int, date_str: str) -> int:
    """Advance the pointer to the last DayStatus with date <= date_str (carry-forward across weekends)."""
    idx = from_idx
    i = from_idx + 1
    while i < len(days):
        if days[i].date > date_str:
            break
        idx = i
        i += 1
    return idx


def burndown_from_payload(payload: "metrics_mod.Payload", now: datetime) -> list[BurndownPoint]:
    start = payload.sprint.start_at
    end = payload.sprint.end_at
    if payload.sprint.complete_at is not None:
        end = payload.sprint.complete_at
    elif payload.sprint.state != "closed":
        end = now

    loc = start.tzinfo
    first = model.civil_day(start, loc)
    last = model.civil_day(end, loc)
    if last < first:
        return []
    total_days = (last - first).days

    last_known: dict[str, int] = {is_.key: -1 for is_ in payload.issues}

    out: list[BurndownPoint] = []
    k = 0
    d = first
    while d <= last:
        date_str = model.format_date(d)
        remaining_sp = 0.0
        remaining_items = 0
        for is_ in payload.issues:
            idx = _advance_day_status_index(is_.day_statuses, last_known[is_.key], date_str)
            last_known[is_.key] = idx
            if idx < 0:
                continue  # not a member yet on this day
            ds = is_.day_statuses[idx]
            if ds.status_category in (model.CATEGORY_DONE, model.CATEGORY_CANCELLED):
                continue
            remaining_sp += is_.story_points
            remaining_items += 1

        if total_days == 0:
            ideal = payload.metrics.committed_sp
        else:
            ideal = payload.metrics.committed_sp * (1 - k / total_days)

        out.append(
            BurndownPoint(date=date_str, remaining_items=remaining_items, remaining_sp=remaining_sp, ideal_sp=ideal)
        )
        k += 1
        d += timedelta(days=1)
    return out
