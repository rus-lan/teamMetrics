"""Monte-Carlo forecast: bootstrap over historical per-sprint delivered
story points.

Answers "how many SP is a sprint likely to deliver" — team-wide and per
person — by resampling (with replacement) the historical per-sprint SP
values. Each bootstrap iteration draws one historical sprint's value as a
simulated outcome for "the next sprint"; percentiles/histogram summarize
that distribution. RNG is injected (stdlib random.Random) so callers can pin
a --seed for determinism.

`weekly_cv`/`calendar_daily_throughput`/`sprint_calendar_window` are a
second, independent thing kept in this module: a weekly item-throughput
coefficient-of-variation used only as a stability signal for
report_data.py's recommendations block (WARN_THROUGHPUT_UNSTABLE) — they
feed no forecast math here anymore.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import timedelta
from random import Random
from typing import Optional

from . import metrics as metrics_mod
from . import model

DEFAULT_ITERATIONS = 5000
MIN_TEAM_SPRINTS = 3
MIN_PERSON_SPRINTS = 2
MIN_NON_ZERO_POINTS = 10
CV_WARN_THRESHOLD_PCT = 50.0
WARN_THROUGHPUT_UNSTABLE = "WARN_THROUGHPUT_UNSTABLE"

# Restores the qualitative percentile wording a previous version carried and
# a later one dropped in favour of a bare "P50 (дней)" label — the renderer
# must not have to invent this wording itself.
PERCENTILE_LABELS_RU: dict[int, str] = {
    50: "50% прогонов уложились",
    85: "рабочее обещание",
    95: "безопасный внешний срок",
}


class NotEnoughDataError(Exception):
    """Fewer than the minimum closed-sprint SP data points for a forecast."""


@dataclass
class Percentile:
    p: int
    sp: float
    label_ru: str


@dataclass
class Bucket:
    sp: float
    count: int


@dataclass
class ForecastOutput:
    percentiles: list[Percentile]
    histogram: list[Bucket]
    mean_sp: float
    sample_sprints: int
    cv_pct: Optional[float]
    warnings: list[str]


def percentile(sorted_vals: list, p: float):
    """Nearest-rank percentile, no interpolation."""
    if not sorted_vals:
        return 0
    rank = math.ceil(p / 100 * len(sorted_vals)) - 1
    if rank < 0:
        rank = 0
    if rank >= len(sorted_vals):
        rank = len(sorted_vals) - 1
    return sorted_vals[rank]


def sp_series_cv_pct(values: list[float]) -> Optional[float]:
    """Population-variance coefficient of variation over the historical
    per-sprint SP series itself (not a weekly-binned daily series — the SP
    forecast operates at sprint granularity). None when there are fewer than
    two values or their mean is zero (nothing meaningful to divide by)."""
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    if mean == 0:
        return None
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return math.sqrt(variance) / mean * 100.0


def _percentile_label_ru(p: int) -> str:
    return PERCENTILE_LABELS_RU.get(p, f"P{p}")


def build_histogram(sorted_outcomes: list[float]) -> list[Bucket]:
    """One bucket per distinct SP value; missing values get no zero-count
    bucket. The bootstrap only ever redraws a historical value verbatim
    (never sums/interpolates), so this is already an exact histogram, not an
    approximation that needs bin-width smoothing."""
    if not sorted_outcomes:
        return []
    buckets: list[Bucket] = []
    cur = sorted_outcomes[0]
    count = 0
    for v in sorted_outcomes:
        if v != cur:
            buckets.append(Bucket(sp=cur, count=count))
            cur = v
            count = 0
        count += 1
    buckets.append(Bucket(sp=cur, count=count))
    return buckets


def forecast_sp(
    values: list[float],
    rng: Random,
    *,
    iterations: int = 0,
    min_sprints: int = MIN_TEAM_SPRINTS,
) -> ForecastOutput:
    """Bootstrap-with-replacement over `values` (one float per historical
    sprint, index-order matters for determinism — callers pass a stable,
    e.g. chronological, order). Each iteration redraws one historical
    value as a simulated "next sprint" SP outcome."""
    if len(values) < min_sprints:
        raise NotEnoughDataError("forecast: not enough closed-sprint SP history")

    iters = iterations if iterations > 0 else DEFAULT_ITERATIONS
    n = len(values)
    outcomes = [values[rng.randrange(n)] for _ in range(iters)]
    outcomes.sort()

    p50 = percentile(outcomes, 50)
    p85 = percentile(outcomes, 85)
    p95 = percentile(outcomes, 95)

    cv = sp_series_cv_pct(values)
    warnings: list[str] = []
    if cv is not None and cv > CV_WARN_THRESHOLD_PCT:
        warnings.append(WARN_THROUGHPUT_UNSTABLE)

    return ForecastOutput(
        percentiles=[
            Percentile(p=50, sp=round(p50, 1), label_ru=_percentile_label_ru(50)),
            Percentile(p=85, sp=round(p85, 1), label_ru=_percentile_label_ru(85)),
            Percentile(p=95, sp=round(p95, 1), label_ru=_percentile_label_ru(95)),
        ],
        histogram=build_histogram(outcomes),
        mean_sp=round(statistics.mean(values), 2),
        sample_sprints=n,
        cv_pct=round(cv, 1) if cv is not None else None,
        warnings=warnings,
    )


# --------------------------------------------------------------------------
# Weekly item-throughput CV — a stability signal for recommendations only.
# --------------------------------------------------------------------------


def sprint_calendar_window(payload: "metrics_mod.Payload") -> list[int]:
    """Expand one closed sprint's sparse throughput_daily into a full
    calendar window [start_at..complete_at|end_at], zeros included."""
    sprint = payload.sprint
    end = sprint.complete_at if sprint.complete_at is not None else sprint.end_at

    loc = sprint.start_at.tzinfo
    start = model.civil_day(sprint.start_at, loc)
    last = model.civil_day(end, loc)
    if last < start:
        return []

    counts: dict[str, int] = {}
    for td in payload.throughput_daily:
        counts[td.date] = counts.get(td.date, 0) + td.count

    out = []
    d = start
    while d <= last:
        out.append(counts.get(model.format_date(d), 0))
        d += timedelta(days=1)
    return out


def calendar_daily_throughput(closed_payloads: list["metrics_mod.Payload"]) -> list[int]:
    """Concatenate the calendar windows of the given closed sprints, sorted
    ascending by start_at."""
    ordered = sorted(closed_payloads, key=lambda p: p.sprint.start_at)
    out: list[int] = []
    for p in ordered:
        out.extend(sprint_calendar_window(p))
    return out


def weekly_cv(daily_hist: list[int]) -> float:
    """Population-variance coefficient of variation over consecutive 7-day
    windows of a daily item-throughput series."""
    weeks: list[float] = []
    i = 0
    while i + 7 <= len(daily_hist):
        weeks.append(float(sum(daily_hist[i : i + 7])))
        i += 7
    if len(weeks) < 2:
        return 0.0
    mean = sum(weeks) / len(weeks)
    if mean == 0:
        return 0.0
    variance = sum((w - mean) ** 2 for w in weeks) / len(weeks)
    return math.sqrt(variance) / mean * 100.0
