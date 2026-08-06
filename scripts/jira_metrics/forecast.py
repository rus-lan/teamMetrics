"""Monte-Carlo forecast: bootstrap-with-replacement over calendar daily throughput.

Python port of internal/domain/forecast/montecarlo.go + forecast.go and the
calendar-window assembly from internal/app/board/forecast.go
(calendarDailyThroughput/sprintCalendarWindow). Only item counts, never story
points (SPEC §8). RNG is injected (stdlib random.Random) so callers can pin a
--seed for determinism; production Go seeds from crypto/rand and is
intentionally non-deterministic, but nothing here depends on matching Go's
draws bit-for-bit — only on this implementation being internally
deterministic given the same seed.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import timedelta

from . import metrics as metrics_mod
from . import model

DEFAULT_ITERATIONS = 5000
MAX_DAYS_GUARD = 2000
MIN_NON_ZERO_POINTS = 10
CV_WARN_THRESHOLD_PCT = 50.0
WARN_THROUGHPUT_UNSTABLE = "WARN_THROUGHPUT_UNSTABLE"


class NotEnoughDataError(Exception):
    """Mirrors forecast.ErrNotEnoughData (ERR_FORECAST_NOT_ENOUGH_DATA)."""


@dataclass
class Percentile:
    percentile: int
    days: int


@dataclass
class Bucket:
    days: int
    count: int


@dataclass
class ForecastOutput:
    target_items: int
    sample_sprints: int
    sample_days: int
    throughput_cv_pct: float
    percentiles: list[Percentile]
    histogram: list[Bucket]
    warnings: list[str]
    outcomes: list[int]


def non_zero_points(daily_hist: list[int]) -> int:
    return sum(1 for v in daily_hist if v > 0)


def percentile(sorted_vals: list[int], p: float) -> int:
    """Nearest-rank percentile, no interpolation (SPEC §8.4)."""
    if not sorted_vals:
        return 0
    rank = math.ceil(p / 100 * len(sorted_vals)) - 1
    if rank < 0:
        rank = 0
    if rank >= len(sorted_vals):
        rank = len(sorted_vals) - 1
    return sorted_vals[rank]


def simulate_when(hist: list[int], target_items: int, iterations: int, rng: random.Random) -> tuple[list[int], int, int, int]:
    """Bootstrap 'when do we close target_items items' (SPEC §8.3)."""
    n = len(hist)
    outcomes: list[int] = []
    for _ in range(iterations):
        remaining, days = target_items, 0
        while remaining > 0 and days < MAX_DAYS_GUARD:
            remaining -= hist[rng.randrange(n)]
            days += 1
        outcomes.append(days)
    outcomes.sort()
    return outcomes, percentile(outcomes, 50), percentile(outcomes, 85), percentile(outcomes, 95)


def weekly_cv(daily_hist: list[int]) -> float:
    """Population-variance coefficient of variation over consecutive 7-day windows (SPEC §8.6)."""
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


def build_histogram(sorted_outcomes: list[int]) -> list[Bucket]:
    """One bucket per distinct duration; missing durations get no zero-count bucket (SPEC §8.5)."""
    if not sorted_outcomes:
        return []
    buckets: list[Bucket] = []
    cur = sorted_outcomes[0]
    count = 0
    for d in sorted_outcomes:
        if d != cur:
            buckets.append(Bucket(days=cur, count=count))
            cur = d
            count = 0
        count += 1
    buckets.append(Bucket(days=cur, count=count))
    return buckets


def forecast(
    daily_throughput: list[int],
    sample_sprints: int,
    target_items: int,
    rng: random.Random,
    iterations: int = 0,
) -> ForecastOutput:
    if non_zero_points(daily_throughput) < MIN_NON_ZERO_POINTS:
        raise NotEnoughDataError("forecast: not enough throughput history")

    iters = iterations if iterations > 0 else DEFAULT_ITERATIONS

    cv = weekly_cv(daily_throughput)
    outcomes, p50, p85, p95 = simulate_when(daily_throughput, target_items, iters, rng)

    warnings = []
    if cv > CV_WARN_THRESHOLD_PCT:
        warnings.append(WARN_THROUGHPUT_UNSTABLE)

    return ForecastOutput(
        target_items=target_items,
        sample_sprints=sample_sprints,
        sample_days=len(daily_throughput),
        throughput_cv_pct=cv,
        percentiles=[Percentile(50, p50), Percentile(85, p85), Percentile(95, p95)],
        histogram=build_histogram(outcomes),
        warnings=warnings,
        outcomes=outcomes,
    )


def sprint_calendar_window(payload: "metrics_mod.Payload") -> list[int]:
    """Expand one closed sprint's sparse throughput_daily into a full calendar
    window [start_at..complete_at|end_at], zeros included (SPEC §8.1)."""
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
        out.append(counts.get(d.strftime("%Y-%m-%d"), 0))
        d += timedelta(days=1)
    return out


def calendar_daily_throughput(closed_payloads: list["metrics_mod.Payload"]) -> list[int]:
    """Concatenate the calendar windows of up to 5 closed sprints, sorted ascending by start_at."""
    ordered = sorted(closed_payloads, key=lambda p: p.sprint.start_at)
    out: list[int] = []
    for p in ordered:
        out.extend(sprint_calendar_window(p))
    return out
