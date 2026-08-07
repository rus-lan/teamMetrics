"""Per-sprint bucketing: the shared X axis (`sprint_axis`) and the aligned
per-sprint arrays built on top of it — `team_series`, `people_series`,
`people[].by_sprint`, `engineering.by_sprint` (SPEC §C.10, §B).

One record (a done Jira issue, a merge request, a pipeline, a deployment) is
assigned to AT MOST one axis sprint, by ONE tie-break rule applied uniformly
to every record kind (`bucket_index`): the sprint whose
`[start, end-of-day 23:59:59]` interval covers the record's own bucketing
timestamp, ties broken by earliest end then earliest start. A record whose
timestamp falls outside every axis sprint (or has no timestamp at all)
contributes to nothing here — the caller (report_data.py) counts those
separately for the report-level WARN_OUTSIDE_SPRINTS warning.

Per-bucket aggregate convention (frozen, applies everywhere in this module):
counts and sums are 0 when their bucket has no contributing record (a
genuinely measured zero); averages, medians and success/rate ratios are None
when their bucket has no contributing record (nothing to average — SPEC
§C.10: "null when the bucket has no rows").
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

UTC = timezone.utc


@dataclass(frozen=True)
class AxisSprint:
    id: int
    name: str
    state: str
    start: datetime
    end: datetime
    target: bool


def build_axis(resolved: list) -> list[AxisSprint]:
    """`resolved`: report_data._ResolvedSprint-like objects (`.sprint` with
    `.id`/`.name`/`.state`/`.start_at`/`.end_at`, and `.target`), any order —
    the axis is always emitted ascending by start."""
    axis = [
        AxisSprint(id=rs.sprint.id, name=rs.sprint.name, state=rs.sprint.state, start=rs.sprint.start_at, target=rs.target,
                   end=rs.sprint.end_at)
        for rs in resolved
    ]
    axis.sort(key=lambda a: a.start)
    return axis


def _end_of_day(d: datetime) -> datetime:
    return d.replace(hour=23, minute=59, second=59, microsecond=0)


def bucket_index(dt: Optional[datetime], axis: list[AxisSprint]) -> Optional[int]:
    """Index into `axis` of the one sprint `dt` belongs to, or None when `dt`
    is absent or outside every axis sprint. Ties (dt covered by more than one
    sprint's window) resolve to the sprint with the earliest end, then the
    earliest start (SPEC §C.10 — the SAME rule for every record kind).

    `a.start.year > 1` excludes a sprint whose start decoded to
    model.ZERO_TIME (a missing Jira startDate, year 1) from ever matching —
    without this guard such a sprint's `[start, end]` window would cover
    essentially every real timestamp, and being the earliest-ending
    candidate in a tie, would swallow every older record into itself."""
    if dt is None:
        return None
    candidates = [(i, a) for i, a in enumerate(axis) if a.start.year > 1 and a.start <= dt <= _end_of_day(a.end)]
    if not candidates:
        return None
    best_i, _ = min(candidates, key=lambda ia: (ia[1].end, ia[1].start))
    return best_i


def _parse_iso(s: Any) -> Optional[datetime]:
    if not s:
        return None
    text = str(s).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        d = datetime.fromisoformat(text)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.astimezone(UTC)


def _mr_bucket_dt(mr: dict) -> Optional[datetime]:
    return _parse_iso(mr.get("merged_at")) or _parse_iso(mr.get("created_at"))


def _pipeline_bucket_dt(p: dict) -> Optional[datetime]:
    return _parse_iso(p.get("created_at")) or _parse_iso(p.get("updated_at"))


def _deployment_bucket_dt(d: dict) -> Optional[datetime]:
    return _parse_iso(d.get("finished_at")) or _parse_iso(d.get("created_at"))


def _jira_bucket_dt(flow: dict) -> Optional[datetime]:
    return flow.get("done_at")


def _bucket_records(records: list, ts_fn, axis: list[AxisSprint]) -> tuple[list[list], int]:
    n = len(axis)
    buckets: list[list] = [[] for _ in range(n)]
    outside = 0
    for r in records:
        idx = bucket_index(ts_fn(r), axis)
        if idx is None:
            outside += 1
            continue
        buckets[idx].append(r)
    return buckets, outside


def _avg(values) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return round(statistics.mean(vals), 2) if vals else None


def _median(values) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return round(statistics.median(vals), 2) if vals else None


def _sum0(values) -> float:
    vals = [v for v in values if v is not None]
    return round(sum(vals), 2) if vals else 0.0


def _success_rate_or_none(rows: list) -> Optional[float]:
    total = len(rows)
    if total == 0:
        return None
    failed = sum(1 for r in rows if r.get("status") == "failed")
    return round((total - failed) / total * 100.0, 1)


def _mr_cycle_values(mr_bucket: list) -> list:
    return [m.get("cycle_time_hours") for m in mr_bucket]


def _mr_changes_values(mr_bucket: list) -> list:
    return [m.get("changes_count") for m in mr_bucket if m.get("changes_count_available")]


# --------------------------------------------------------------------------
# engineering.by_sprint
# --------------------------------------------------------------------------


def build_engineering_by_sprint(axis: list[AxisSprint], mrs: list, pipelines: list, deployments: list) -> list[dict]:
    mr_buckets, _ = _bucket_records(mrs, _mr_bucket_dt, axis)
    pipe_buckets, _ = _bucket_records(pipelines, _pipeline_bucket_dt, axis)
    dep_buckets, _ = _bucket_records(deployments, _deployment_bucket_dt, axis)

    rows = []
    for i, a in enumerate(axis):
        mr_b = mr_buckets[i]
        rows.append(
            {
                "sprint_id": a.id,
                "sprint_name": a.name,
                "pipeline_count": len(pipe_buckets[i]),
                "pipeline_success_rate_pct": _success_rate_or_none(pipe_buckets[i]),
                "deployment_count": len(dep_buckets[i]),
                "deployment_success_rate_pct": _success_rate_or_none(dep_buckets[i]),
                "mr_count": len(mr_b),
                "mr_merged_count": sum(1 for m in mr_b if m.get("state") == "merged"),
                "avg_mr_cycle_time_hours": _avg(_mr_cycle_values(mr_b)),
                "avg_mr_changes_count": _avg(_mr_changes_values(mr_b)),
            }
        )
    return rows


# --------------------------------------------------------------------------
# team_series — 11 fixed charts (SPEC §D tab 03)
# --------------------------------------------------------------------------


def build_team_series(
    axis: list[AxisSprint], people_flows: list, mrs: list, pipelines: list, deployments: list
) -> tuple[list[dict], dict]:
    jira_buckets, jira_outside = _bucket_records(people_flows, _jira_bucket_dt, axis)
    mr_buckets, mr_outside = _bucket_records(mrs, _mr_bucket_dt, axis)
    pipe_buckets, pipe_outside = _bucket_records(pipelines, _pipeline_bucket_dt, axis)
    dep_buckets, dep_outside = _bucket_records(deployments, _deployment_bucket_dt, axis)

    throughput_vals, cycle_avg_vals, cycle_median_vals = [], [], []
    rework_total_vals, rework_rate_vals = [], []
    mr_count_vals, mr_merged_vals, pr_cycle_vals = [], [], []
    pipe_count_vals, dep_count_vals = [], []
    pipe_success_vals, dep_success_vals = [], []
    sp_sum_vals, sp_avg_vals, qa_avg_vals, mr_weight_vals = [], [], [], []

    for i in range(len(axis)):
        jb = jira_buckets[i]
        mb = mr_buckets[i]
        throughput_vals.append(len(jb))
        cycle_avg_vals.append(_avg(f.get("cycle_time_hours") for f in jb))
        cycle_median_vals.append(_median(f.get("cycle_time_hours") for f in jb))
        rework_total_vals.append(_sum0(f.get("rework_count", 0) for f in jb))
        rework_with = sum(1 for f in jb if (f.get("rework_count") or 0) > 0)
        rework_rate_vals.append(None if not jb else round(rework_with / len(jb) * 100.0, 1))
        mr_count_vals.append(len(mb))
        mr_merged_vals.append(sum(1 for m in mb if m.get("state") == "merged"))
        pr_cycle_vals.append(_avg(_mr_cycle_values(mb)))
        pipe_count_vals.append(len(pipe_buckets[i]))
        dep_count_vals.append(len(dep_buckets[i]))
        pipe_success_vals.append(_success_rate_or_none(pipe_buckets[i]))
        dep_success_vals.append(_success_rate_or_none(dep_buckets[i]))
        sp_sum_vals.append(_sum0(f.get("story_points") for f in jb))
        sp_avg_vals.append(_avg(f.get("story_points") for f in jb))
        qa_avg_vals.append(_avg(f.get("qa_estimation") for f in jb))
        mr_weight_vals.append(_avg(_mr_changes_values(mb)))

    series = [
        {
            "key": "throughput", "title_ru": "Throughput по спринтам", "unit_ru": "задач",
            "hint_ru": "Сколько задач завершено в каждом спринте. Резкое падение к концу периода может значить, "
                       "что спринт ещё не закончен; устойчивый тренд вниз — повод разобраться.",
            "show_trend": True,
            "series": [{"key": "tasks_done", "name_ru": "Завершено задач", "values": throughput_vals}],
        },
        {
            "key": "task_cycle_time", "title_ru": "Cycle time по спринтам, ч", "unit_ru": "часы",
            "hint_ru": "Если cycle time растёт от спринта к спринту — команда замедляется. Медиана устойчивее "
                       "среднего к выбросам.",
            "show_trend": True,
            "series": [
                {"key": "avg", "name_ru": "Среднее", "values": cycle_avg_vals},
                {"key": "median", "name_ru": "Медиана", "values": cycle_median_vals},
            ],
        },
        {
            "key": "rework", "title_ru": "Rework по спринтам", "unit_ru": "возвраты",
            "hint_ru": "Сколько раз задачи возвращались в работу в каждом спринте. Рост rework при росте "
                       "скорости — сигнал, что качество с первого раза падает.",
            "show_trend": True,
            "series": [{"key": "rework_total", "name_ru": "Возвратов в работу", "values": rework_total_vals}],
        },
        {
            "key": "rework_rate", "title_ru": "Rework rate по спринтам, %", "unit_ru": "%",
            "hint_ru": "Доля задач с хотя бы одним возвратом. Чем выше — тем больше переделок.",
            "show_trend": True,
            "series": [{"key": "rework_rate_pct", "name_ru": "Rework rate", "values": rework_rate_vals}],
        },
        {
            "key": "mr", "title_ru": "MR по спринтам", "unit_ru": "шт",
            "hint_ru": "Сколько merge request'ов открыто и сколько дошло до merge в каждом спринте.",
            "show_trend": True,
            "series": [
                {"key": "mr_count", "name_ru": "Всего MR", "values": mr_count_vals},
                {"key": "mr_merged_count", "name_ru": "Доведено до merge", "values": mr_merged_vals},
            ],
        },
        {
            "key": "pr_cycle_time", "title_ru": "PR cycle time по спринтам, ч", "unit_ru": "часы",
            "hint_ru": "Среднее время MR от открытия до merge. Рост от спринта к спринту — код дольше дожидается "
                       "проверки/деплоя (выкатки).",
            "show_trend": True,
            "series": [{"key": "avg_mr_cycle_time_hours", "name_ru": "PR cycle time", "values": pr_cycle_vals}],
        },
        {
            "key": "pipelines_deployments", "title_ru": "Пайплайны и деплои по спринтам", "unit_ru": "шт",
            "hint_ru": "Объём CI/CD-активности. Резкие всплески без роста выпускаемых задач — признак переделок "
                       "и повторных запусков.",
            "show_trend": False,
            "series": [
                {"key": "pipeline_count", "name_ru": "Пайплайны", "values": pipe_count_vals},
                {"key": "deployment_count", "name_ru": "Деплои", "values": dep_count_vals},
            ],
        },
        {
            "key": "ci_deploy_success", "title_ru": "Успешность CI и деплоев, %", "unit_ru": "%",
            "hint_ru": "Доля успешных пайплайнов и деплоев. Падение при растущем темпе — качество досталось "
                       "ценой стабильности.",
            "show_trend": False,
            "series": [
                {"key": "pipeline_success_rate_pct", "name_ru": "Пайплайны", "values": pipe_success_vals},
                {"key": "deployment_success_rate_pct", "name_ru": "Деплои", "values": dep_success_vals},
            ],
        },
        {
            "key": "story_points_sum", "title_ru": "Story Points по спринтам (сумма)", "unit_ru": "SP",
            "hint_ru": "Сколько «стори-поинтов» (относительной сложности) команда закрыла за спринт. Падение — "
                       "меньше комплексной работы закрыто за спринт.",
            "show_trend": True,
            "series": [{"key": "story_points_total", "name_ru": "Story Points", "values": sp_sum_vals}],
        },
        {
            "key": "avg_estimates", "title_ru": "Средние оценки на задачу по спринтам", "unit_ru": "ед.",
            "hint_ru": "Средняя сложность задачи и средние трудозатраты на тестирование. Каждый отдельный "
                       "показатель читается только в сравнении с собственным baseline.",
            "show_trend": False,
            "series": [
                {"key": "story_points_avg", "name_ru": "Story Points (ср.)", "values": sp_avg_vals},
                {"key": "qa_estimation_avg", "name_ru": "QA Estimation (ср.)", "values": qa_avg_vals},
            ],
        },
        {
            "key": "mr_weight", "title_ru": "Средний вес MR (число файлов) по спринтам", "unit_ru": "файлов",
            "hint_ru": "Сколько файлов в среднем трогали за спринт. Чем больше — тем шире зона доработки.",
            "show_trend": True,
            "series": [{"key": "avg_mr_changes_count", "name_ru": "Файлов на MR", "values": mr_weight_vals}],
        },
    ]

    outside_counts = {
        "issues": jira_outside,
        "merge_requests": mr_outside,
        "pipelines": pipe_outside,
        "deployments": dep_outside,
    }
    return series, outside_counts


# --------------------------------------------------------------------------
# people[].by_sprint — one call per person
# --------------------------------------------------------------------------


def build_people_by_sprint(axis: list[AxisSprint], person_flows: list, person_mrs: list) -> list[dict]:
    jira_buckets, _ = _bucket_records(person_flows, _jira_bucket_dt, axis)
    mr_buckets, _ = _bucket_records(person_mrs, _mr_bucket_dt, axis)

    rows = []
    for i, a in enumerate(axis):
        jb = jira_buckets[i]
        mb = mr_buckets[i]
        has_data = bool(jb) or bool(mb)
        if not has_data:
            rows.append(
                {
                    "sprint_id": a.id, "sprint_name": a.name, "has_data": False,
                    "throughput": None, "avg_cycle_time_hours": None,
                    "story_points_total": None, "qa_estimation_total": None,
                    "rework_total": None, "mr_count": None, "avg_mr_cycle_hours": None,
                    "avg_mr_changes_count": None,
                }
            )
            continue
        rows.append(
            {
                "sprint_id": a.id, "sprint_name": a.name, "has_data": True,
                "throughput": len(jb),
                "avg_cycle_time_hours": _avg(f.get("cycle_time_hours") for f in jb),
                "story_points_total": _sum0(f.get("story_points") for f in jb),
                "qa_estimation_total": _sum0(f.get("qa_estimation") for f in jb),
                "rework_total": _sum0(f.get("rework_count", 0) for f in jb),
                "mr_count": len(mb),
                "avg_mr_cycle_hours": _avg(_mr_cycle_values(mb)),
                "avg_mr_changes_count": _avg(_mr_changes_values(mb)),
            }
        )
    return rows


# --------------------------------------------------------------------------
# people_series — 8 fixed charts (SPEC §D tab 06), reshaped from people[].by_sprint
# --------------------------------------------------------------------------

_PEOPLE_SERIES_DEFS = [
    ("throughput_by_person", "Throughput по спринтам (по сотрудникам)", "задач",
     "Сколько задач завершил каждый сотрудник. Расхождение линий — разный объём.", "throughput"),
    ("task_cycle_time_by_person", "Cycle time задач по спринтам (ср., ч)", "часы",
     "Среднее время задачи в работе. Расхождение линий — разная скорость доставки.", "avg_cycle_time_hours"),
    ("rework_by_person", "Rework по спринтам (по сотрудникам)", "возвраты",
     "Сколько раз задачи сотрудника возвращались в работу. Рост у одного при спаде у других — точечная проблема.",
     "rework_total"),
    ("story_points_by_person", "Story Points по спринтам (сумма)", "SP",
     "Сумма относительной сложности, закрытой каждым сотрудником.", "story_points_total"),
    ("qa_estimation_by_person", "QA Estimation по спринтам (сумма)", "ед.",
     "Сумма оценок трудозатрат на тестирование, закрытых каждым сотрудником.", "qa_estimation_total"),
    ("mr_count_by_person", "Число MR по спринтам (по сотрудникам)", "шт",
     "Сколько merge request'ов довёл до merge каждый сотрудник.", "mr_count"),
    ("pr_cycle_time_by_person", "PR cycle time по спринтам (ср., ч)", "часы",
     "Среднее время MR от открытия до merge по каждому сотруднику.", "avg_mr_cycle_hours"),
    ("mr_weight_by_person", "Средний вес MR по спринтам (файлов)", "файлов",
     "Сколько файлов в среднем затрагивает MR сотрудника.", "avg_mr_changes_count"),
]


def build_people_series(axis: list[AxisSprint], people: list) -> list[dict]:
    """`people`: the v2 `people[]` list — each entry already carries an
    aligned `by_sprint` (len(axis)) built by `build_people_by_sprint`."""
    out = []
    for key, title_ru, unit_ru, hint_ru, field in _PEOPLE_SERIES_DEFS:
        series = []
        for p in people:
            values = [row.get(field) for row in p.get("by_sprint", [])]
            series.append({"login": p["login"], "display_name": p["display_name"], "values": values})
        out.append({"key": key, "title_ru": title_ru, "unit_ru": unit_ru, "hint_ru": hint_ru, "series": series})
    return out
