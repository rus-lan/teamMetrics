"""Writes the 18 `out/` CSVs (SPEC §C.12) via `out_writer.write_csv`/
`write_text`. Column lists are verbatim from the aiIntegrationMetrics skill's
collectors; the two source-format exceptions are `heatmap.csv` (jiratools
format: `;`, BOM, no header via DictWriter — written with `write_text`) and
`board.csv` (comma CSV with machine headers).

Every writer here reads from the SAME two things `report_data.py` already
built: the final schema-v2 `report` dict (for sprint_axis/people/engineering/
export_tables — no need to recompute anything already in the JSON) and the
`raw` bundle `build_combined_report_with_raw` returns (facts/mrs/pipelines/
deployments/coverage/done_flows — the pre-JSON material with no place in the
JSON contract). Fractions (rates) stay 0..1 here — the source skill's own
convention — even though the JSON carries the same ratios as 0..100 (SPEC
rule §B.2 is a JSON-only rule).
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import out_writer
from . import personal_metrics
from . import sprint_series

UTC = timezone.utc


def _parse_iso(s: Any) -> Optional[datetime]:
    if s is None:
        return None
    if isinstance(s, datetime):
        return s
    text = str(s).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        d = datetime.fromisoformat(text)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.astimezone(UTC)


def _fmt_dt(v: Any) -> str:
    d = _parse_iso(v)
    return f"{d.year:04d}-{d.month:02d}-{d.day:02d} {d.hour:02d}:{d.minute:02d}:{d.second:02d}" if d else ""


def _fmt_date(v: Any) -> str:
    d = _parse_iso(v)
    return f"{d.year:04d}-{d.month:02d}-{d.day:02d}" if d else ""


def _cell(v: Any) -> Any:
    return "" if v is None else v


def _frac(pct: Optional[float]) -> Any:
    """0..100 -> 0..1, "" when the source value is None (CSV convention)."""
    return "" if pct is None else round(pct / 100.0, 4)


# --------------------------------------------------------------------------
# 1-4: flat GitLab lists
# --------------------------------------------------------------------------


def _gitlab_mrs_rows(mrs: list) -> list:
    return [
        {
            "project": m.get("project", ""), "project_id": _cell(m.get("project_id")),
            "mr_id": _cell(m.get("mr_id")), "title": m.get("title", ""),
            "author": m.get("author", ""), "state": m.get("state", ""), "web_url": m.get("web_url", ""),
            "created_at": _fmt_dt(m.get("created_at")), "merged_at": _fmt_dt(m.get("merged_at")),
            "cycle_time_seconds": _cell(m.get("cycle_time_seconds")), "cycle_time_hours": _cell(m.get("cycle_time_hours")),
            "additions": _cell(m.get("additions")), "deletions": _cell(m.get("deletions")),
            "commits_count": _cell(m.get("commits_count")), "changes_count": _cell(m.get("changes_count")),
            "jira_key": m.get("jira_key", ""),
        }
        for m in mrs
    ]


def _gitlab_pipelines_rows(pipelines: list) -> list:
    return [
        {
            "project": p.get("project", ""), "project_id": _cell(p.get("project_id")),
            "pipeline_id": _cell(p.get("pipeline_id")), "ref": p.get("ref", ""), "sha": p.get("sha", ""),
            "status": p.get("status", ""), "created_at": _fmt_dt(p.get("created_at")),
            "updated_at": _fmt_dt(p.get("updated_at")), "web_url": p.get("web_url", ""),
            "user_username": p.get("user_username", ""), "user_name": p.get("user_name", ""),
        }
        for p in pipelines
    ]


def _gitlab_deployments_rows(deployments: list) -> list:
    return [
        {
            "project": d.get("project", ""), "project_id": _cell(d.get("project_id")),
            "deployment_id": _cell(d.get("deployment_id")), "status": d.get("status", ""),
            "environment": d.get("environment", ""), "ref": d.get("ref", ""), "sha": d.get("sha", ""),
            "created_at": _fmt_dt(d.get("created_at")), "finished_at": _fmt_dt(d.get("finished_at")),
            "web_url": d.get("web_url", ""), "user_username": d.get("user_username", ""), "user_name": d.get("user_name", ""),
        }
        for d in deployments
    ]


def _gitlab_coverage_rows(coverage: list) -> list:
    return [
        {
            "project": c.get("project", ""), "project_id": _cell(c.get("project_id")),
            "pipeline_id": _cell(c.get("pipeline_id")), "ref": c.get("ref", ""),
            "coverage": _cell(c.get("coverage")), "created_at": _fmt_dt(c.get("created_at")),
        }
        for c in coverage
    ]


# --------------------------------------------------------------------------
# 5-6: login -> display_name pairs
# --------------------------------------------------------------------------


def _gitlab_users_rows(mrs: list, pipelines: list) -> list:
    names: dict = {}
    for m in mrs:
        login, name = m.get("author"), m.get("author_name")
        if login and name and login not in names:
            names[login] = name
    for p in pipelines:
        login, name = p.get("user_username"), p.get("user_name")
        if login and name and login not in names:
            names[login] = name
    return [{"login": login, "display_name": names[login]} for login in sorted(names)]


def _jira_users_rows(facts: list) -> list:
    names: dict = {}
    for f in facts:
        if f.assignee and f.assignee_display_name and f.assignee not in names:
            names[f.assignee] = f.assignee_display_name
    return [{"login": login, "display_name": names[login]} for login in sorted(names)]


# --------------------------------------------------------------------------
# 7, 9, 10: per-issue Jira exports
# --------------------------------------------------------------------------


def _completion_sprint_name(fact, axis: list, done_at) -> str:
    if done_at is None:
        return ""
    idx = sprint_series.bucket_index(done_at, axis)
    return axis[idx].name if idx is not None else ""


def _jira_issues_rows(facts: list, axis: list, done_flows_by_key: dict) -> list:
    rows = []
    for f in facts:
        flow = done_flows_by_key.get(f.key)
        done_at = flow["done_at"] if flow else None
        rows.append(
            {
                "key": f.key, "summary": f.summary, "status": f.current_status, "issuetype": f.type,
                "assignee": f.assignee, "reporter": "", "sprint": _completion_sprint_name(f, axis, done_at),
                "created": _fmt_dt(f.created), "resolutiondate": _fmt_dt(f.resolutiondate), "resolution": "",
                "story_points": _cell(f.story_points), "qa_estimation": _cell(f.qa_estimation),
            }
        )
    return rows


def _jira_cycle_time_rows(done_flows: list) -> list:
    rows = []
    for flow in done_flows:
        hours = flow.get("cycle_time_hours")
        rows.append(
            {
                "key": flow["key"], "assignee": flow["login"], "issuetype": flow["type"],
                "first_in_progress": _fmt_dt(flow.get("first_in_progress")), "done_at": _fmt_dt(flow.get("done_at")),
                "cycle_time_hours": _cell(hours),
                "cycle_time_seconds": int(round(hours * 3600)) if hours is not None else "",
            }
        )
    return rows


def _final_status_lower_set(final_statuses) -> frozenset:
    return frozenset(s.strip().lower() for s in final_statuses if s.strip())


def _done_transitions(fact, final_lower: frozenset) -> int:
    return sum(1 for ch in fact.status_history if (ch.to_name or "").strip().lower() in final_lower)


def _rework_count_by_key(facts: list) -> dict:
    """Rebuilt via personal_metrics' own rule so this CSV needs no extra
    input beyond the raw facts already passed to every other writer here."""
    out = {}
    for f in facts:
        events = sorted(f.status_history, key=lambda ch: ch.at)
        out[f.key] = personal_metrics._rework_count(
            [personal_metrics.StatusEvent(at=ch.at, to_status=ch.to_name) for ch in events]
        )
    return out


def _jira_rework_rows(facts: list, final_statuses) -> list:
    final_lower = _final_status_lower_set(final_statuses)
    rework_by_key = _rework_count_by_key(facts)
    return [
        {
            "key": f.key, "assignee": f.assignee, "rework_count": rework_by_key.get(f.key, 0),
            "done_transitions": _done_transitions(f, final_lower),
        }
        for f in facts
    ]


def _jira_throughput_rows(done_flows: list, facts_by_key: dict) -> list:
    rows = []
    for flow in done_flows:
        fact = facts_by_key.get(flow["key"])
        rows.append(
            {
                "key": flow["key"], "assignee": flow["login"], "issuetype": flow["type"],
                "status": fact.current_status if fact else "", "resolved": _fmt_dt(flow.get("done_at")),
            }
        )
    return rows


# --------------------------------------------------------------------------
# 11-13: sprint tables
# --------------------------------------------------------------------------


def _sprints_rows(axis: list) -> list:
    return [{"name": a.name, "start_date": _fmt_date(a.start), "end_date": _fmt_date(a.end)} for a in axis]


def _avg_or_blank(values: list) -> Any:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else ""


def _jira_by_sprint_rows(axis: list, done_flows: list) -> list:
    buckets: list[list] = [[] for _ in axis]
    for flow in done_flows:
        idx = sprint_series.bucket_index(flow.get("done_at"), axis)
        if idx is not None:
            buckets[idx].append(flow)

    rows = []
    for a, bucket in zip(axis, buckets):
        throughput = len(bucket)
        cycle_vals = [f.get("cycle_time_hours") for f in bucket]
        sp_vals = [f.get("story_points") for f in bucket]
        qa_vals = [f.get("qa_estimation") for f in bucket]
        rework_total = sum((f.get("rework_count") or 0) for f in bucket)
        rework_tasks = sum(1 for f in bucket if (f.get("rework_count") or 0) > 0)
        bug_count = sum(1 for f in bucket if f.get("is_bug"))
        rows.append(
            {
                "name": a.name, "start_date": _fmt_date(a.start), "end_date": _fmt_date(a.end),
                "throughput": throughput,
                "avg_cycle_time_hours": _avg_or_blank(cycle_vals),
                "median_cycle_time_hours": _avg_or_blank(cycle_vals),  # median tie-broken below
                "rework_total": rework_total,
                "rework_rate": round(rework_tasks / throughput, 4) if throughput else 0,
                "defect_rate": round(bug_count / throughput, 4) if throughput else 0,
                "story_points_total": round(sum(v for v in sp_vals if v is not None), 2) if any(v is not None for v in sp_vals) else 0,
                "avg_story_points": _avg_or_blank(sp_vals),
                "qa_estimation_total": round(sum(v for v in qa_vals if v is not None), 2) if any(v is not None for v in qa_vals) else 0,
                "avg_qa_estimation": _avg_or_blank(qa_vals),
            }
        )
    # median_cycle_time_hours needs the real median, not the mean placeholder above.
    for row, bucket in zip(rows, buckets):
        vals = [f.get("cycle_time_hours") for f in bucket if f.get("cycle_time_hours") is not None]
        row["median_cycle_time_hours"] = round(statistics.median(vals), 2) if vals else ""
    return rows


def _mr_bucket_dt(mr: dict):
    return _parse_iso(mr.get("merged_at")) or _parse_iso(mr.get("created_at"))


def _pipeline_bucket_dt(p: dict):
    return _parse_iso(p.get("created_at")) or _parse_iso(p.get("updated_at"))


def _deployment_bucket_dt(d: dict):
    return _parse_iso(d.get("finished_at")) or _parse_iso(d.get("created_at"))


def _gitlab_by_sprint_rows(axis: list, mrs: list, pipelines: list, deployments: list) -> list:
    if not mrs and not pipelines and not deployments:
        # No GitLab data at all (not configured, or a genuinely empty
        # project) -- an all-zero row per axis sprint would look like real
        # measured data; skip the file instead, same as the other 5
        # gitlab_*.csv files (which skip naturally because their row lists
        # come straight from these now-empty source lists).
        return []
    mr_buckets: list[list] = [[] for _ in axis]
    for m in mrs:
        idx = sprint_series.bucket_index(_mr_bucket_dt(m), axis)
        if idx is not None:
            mr_buckets[idx].append(m)
    pipe_buckets: list[list] = [[] for _ in axis]
    for p in pipelines:
        idx = sprint_series.bucket_index(_pipeline_bucket_dt(p), axis)
        if idx is not None:
            pipe_buckets[idx].append(p)
    dep_buckets: list[list] = [[] for _ in axis]
    for d in deployments:
        idx = sprint_series.bucket_index(_deployment_bucket_dt(d), axis)
        if idx is not None:
            dep_buckets[idx].append(d)

    def success_rate(rows: list) -> Any:
        if not rows:
            return ""
        failed = sum(1 for r in rows if r.get("status") == "failed")
        return round((len(rows) - failed) / len(rows), 4)

    rows = []
    for i, a in enumerate(axis):
        mb, pb, db = mr_buckets[i], pipe_buckets[i], dep_buckets[i]
        changes = [m.get("changes_count") for m in mb if m.get("changes_count_available")]
        cycles = [m.get("cycle_time_hours") for m in mb if m.get("cycle_time_hours") is not None]
        rows.append(
            {
                "name": a.name, "start_date": _fmt_date(a.start), "end_date": _fmt_date(a.end), "bucket_type": "sprint",
                "mr_count": len(mb), "mr_merged_count": sum(1 for m in mb if m.get("state") == "merged"),
                "avg_mr_cycle_time_hours": _avg_or_blank(cycles), "avg_mr_changes_count": _avg_or_blank(changes),
                "total_mr_changes": round(sum(changes), 2) if changes else 0,
                "pipeline_count": len(pb), "pipeline_success_rate": success_rate(pb),
                "deployment_count": len(db), "deployment_success_rate": success_rate(db),
            }
        )
    return rows


# --------------------------------------------------------------------------
# 14-16: per-employee / team / merged
# --------------------------------------------------------------------------


def _sum_or_blank(values: list) -> Any:
    vals = [v for v in values if v is not None]
    return round(sum(vals), 2) if vals else ""


def _report_per_employee_rows(people: list, mrs: list, pipelines: list) -> list:
    rows = []
    for p in people:
        m = p["metrics"]
        login = p["login"]
        my_mrs = [x for x in mrs if x.get("author") == login]
        my_pipes = [x for x in pipelines if x.get("user_username") == login]
        pipe_failed = sum(1 for x in my_pipes if x.get("status") == "failed")
        rows.append(
            {
                "employee": p["display_name"], "gitlab_username": login, "jira_username": login,
                "email": "", "team": "",
                "mr_count": m["mr_count"], "mr_merged_count": m["mr_merged_count"], "mr_closed_count": m["mr_closed_count"],
                "mr_merge_rate": _frac(m["mr_merge_rate_pct"]),
                "avg_mr_cycle_time_hours": _cell(m["mr_cycle_time_avg_hours"]), "avg_mr_diff_size": _cell(m["mr_diff_size_avg"]),
                "avg_mr_changes_count": _cell(m["mr_changes_count_avg"]), "total_mr_changes": _cell(m["mr_changes_count_sum"]),
                "total_mr_additions": _sum_or_blank(x.get("additions") for x in my_mrs),
                "total_mr_deletions": _sum_or_blank(x.get("deletions") for x in my_mrs),
                "total_mr_commits": _cell(m["mr_commits_sum"]),
                "pipeline_count": len(my_pipes) if my_pipes else "",
                "pipeline_failed": pipe_failed if my_pipes else "",
                "pipeline_fail_rate": round(pipe_failed / len(my_pipes), 4) if my_pipes else "",
                "issues_count": m["issue_count"], "tasks_done": m["tasks_done"], "bug_count": m["bug_count"],
                "defect_rate": _frac(m["defect_rate_pct"]), "avg_issue_cycle_time_hours": _cell(m["task_cycle_time_avg_hours"]),
                "rework_count": m["rework_total"], "story_points_total": _cell(m["story_points_total"]),
                "avg_story_points": _cell(m["story_points_avg"]), "qa_estimation_total": _cell(m["qa_estimation_total"]),
                "avg_qa_estimation": _cell(m["qa_estimation_avg"]),
            }
        )
    return rows


def _report_team_row(report: dict, people: list, mrs: list) -> list:
    overview = report["overview"]
    engineering = report["engineering"]
    n = len(people)

    def team_avg(field: str) -> Any:
        vals = [p["metrics"][field] for p in people if p["metrics"][field] is not None]
        return round(sum(vals) / len(vals), 2) if vals else ""

    def team_sum(field: str) -> Any:
        vals = [p["metrics"][field] for p in people if p["metrics"][field] is not None]
        return round(sum(vals), 2) if vals else 0

    total_deployments = engineering["deployments"]["count"] if engineering["available"] else 0
    total_deployment_failed = engineering["deployments"]["failed"] if engineering["available"] else 0
    total_pipelines = engineering["pipelines"]["count"] if engineering["available"] else 0
    total_pipeline_failed = engineering["pipelines"]["failed"] if engineering["available"] else 0
    avg_defect_rate = team_avg("defect_rate_pct")

    row = {
        "metric": "team",
        "employees_count": n,
        "total_mr_count": overview["mr_total"],
        "total_mr_merged": team_sum("mr_merged_count"),
        "total_mr_closed": team_sum("mr_closed_count"),
        "avg_mr_merge_rate": _frac(team_avg("mr_merge_rate_pct")) if team_avg("mr_merge_rate_pct") != "" else "",
        "avg_mr_cycle_time_hours": team_avg("mr_cycle_time_avg_hours"),
        "avg_mr_diff_size": team_avg("mr_diff_size_avg"),
        "avg_mr_changes_count": team_avg("mr_changes_count_avg"),
        "total_mr_changes": team_sum("mr_changes_count_sum"),
        "total_mr_additions": _sum_or_blank(m.get("additions") for m in mrs),
        "total_mr_deletions": _sum_or_blank(m.get("deletions") for m in mrs),
        "total_mr_commits": team_sum("mr_commits_sum"),
        "total_deployments": total_deployments,
        "total_deployment_failed": total_deployment_failed,
        "avg_deployment_fail_rate": round(total_deployment_failed / total_deployments, 4) if total_deployments else 0,
        "total_pipelines": total_pipelines,
        "total_pipeline_failed": total_pipeline_failed,
        "avg_pipeline_fail_rate": round(total_pipeline_failed / total_pipelines, 4) if total_pipelines else 0,
        "total_issues": team_sum("issue_count"),
        "total_tasks_done": overview["tasks_done_total"],
        "total_bugs": team_sum("bug_count"),
        # §C.12 item 15: avg_defect_rate is the average of PER-EMPLOYEE
        # defect rates, not overview.defect_rate_pct's flat team ratio —
        # only avg_*_fail_rate (pipelines/deployments, above) reads flat
        # team numbers.
        "avg_defect_rate": _frac(avg_defect_rate) if avg_defect_rate != "" else "",
        "avg_issue_cycle_time_hours": team_avg("task_cycle_time_avg_hours"),
        "total_rework": team_sum("rework_total"),
        "total_story_points": team_sum("story_points_total"),
        "avg_story_points": team_avg("story_points_avg"),
        "total_qa_estimation": team_sum("qa_estimation_total"),
        "avg_qa_estimation": team_avg("qa_estimation_avg"),
    }
    return [row]


def _report_merged_rows(mrs: list, facts_by_key: dict) -> list:
    rows = []
    for m in mrs:
        fact = facts_by_key.get(m.get("jira_key") or "")
        rows.append(
            {
                "mr_id": _cell(m.get("mr_id")), "project": m.get("project", ""), "mr_title": m.get("title", ""),
                "mr_author": m.get("author", ""), "mr_state": m.get("state", ""),
                "mr_cycle_time_hours": _cell(m.get("cycle_time_hours")), "mr_additions": _cell(m.get("additions")),
                "mr_deletions": _cell(m.get("deletions")), "mr_changes_count": _cell(m.get("changes_count")),
                "jira_key": m.get("jira_key", ""),
                "jira_summary": fact.summary if fact else "", "jira_assignee": fact.assignee if fact else "",
                "jira_status": fact.current_status if fact else "", "jira_issuetype": fact.type if fact else "",
                "jira_story_points": _cell(fact.story_points) if fact else "",
                "jira_qa_estimation": _cell(fact.qa_estimation) if fact else "",
            }
        )
    return rows


# --------------------------------------------------------------------------
# top-level entry point
# --------------------------------------------------------------------------


def write_all(out_dir, report: dict, raw: dict) -> list[Path]:
    """Writes the 18 CSVs under `out_dir` (comma CSVs via
    `out_writer.write_csv`, `heatmap.csv` via `out_writer.write_text` in
    jiratools format). GitLab-named CSVs are still attempted with empty
    lists when GitLab was not configured — `out_writer.write_csv` warns and
    skips an empty row set rather than writing a headerless file."""
    facts = raw["facts"]
    mrs = raw["mrs"]
    pipelines = raw["pipelines"]
    deployments = raw["deployments"]
    coverage = raw["coverage"]
    done_flows = raw["done_flows"]
    axis = raw["axis"]
    final_statuses = raw["final_statuses"]
    people = report["people"]

    facts_by_key = {f.key: f for f in facts}
    done_flows_by_key = {f["key"]: f for f in done_flows}

    written: list[Optional[Path]] = []
    written.append(out_writer.write_csv(out_dir, "gitlab_mrs.csv", _gitlab_mrs_rows(mrs)))
    written.append(out_writer.write_csv(out_dir, "gitlab_pipelines.csv", _gitlab_pipelines_rows(pipelines)))
    written.append(out_writer.write_csv(out_dir, "gitlab_deployments.csv", _gitlab_deployments_rows(deployments)))
    written.append(out_writer.write_csv(out_dir, "gitlab_coverage.csv", _gitlab_coverage_rows(coverage)))
    written.append(out_writer.write_csv(out_dir, "gitlab_users.csv", _gitlab_users_rows(mrs, pipelines)))
    written.append(out_writer.write_csv(out_dir, "jira_users.csv", _jira_users_rows(facts)))
    written.append(out_writer.write_csv(out_dir, "jira_issues.csv", _jira_issues_rows(facts, axis, done_flows_by_key)))
    written.append(out_writer.write_csv(out_dir, "jira_cycle_time.csv", _jira_cycle_time_rows(done_flows)))
    written.append(out_writer.write_csv(out_dir, "jira_rework.csv", _jira_rework_rows(facts, final_statuses)))
    written.append(out_writer.write_csv(out_dir, "jira_throughput.csv", _jira_throughput_rows(done_flows, facts_by_key)))
    written.append(out_writer.write_csv(out_dir, "sprints.csv", _sprints_rows(axis)))
    written.append(out_writer.write_csv(out_dir, "jira_by_sprint.csv", _jira_by_sprint_rows(axis, done_flows)))
    written.append(out_writer.write_csv(out_dir, "gitlab_by_sprint.csv", _gitlab_by_sprint_rows(axis, mrs, pipelines, deployments)))
    written.append(out_writer.write_csv(out_dir, "report_per_employee.csv", _report_per_employee_rows(people, mrs, pipelines)))
    written.append(out_writer.write_csv(out_dir, "report_team.csv", _report_team_row(report, people, mrs)))
    written.append(out_writer.write_csv(out_dir, "report_merged.csv", _report_merged_rows(mrs, facts_by_key)))
    written.append(out_writer.write_text(out_dir, "heatmap.csv", raw["heatmap_csv_text"]))

    board = report["export_tables"]["board"]
    board_rows = [dict(zip(board["header"], row)) for row in board["rows"]]
    written.append(out_writer.write_csv(out_dir, "board.csv", board_rows))

    return [p for p in written if p is not None]
