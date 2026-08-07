"""Standalone Jira/GitLab team-metrics data+math core.

Python 3.9+ standard library only. Ports the numbers of an internal Go
sprint-metrics module and an internal GitLab personal/engineering-metrics
skill (see report_data.py's own docstring for the full provenance note).

Module map:
    model               issue/sprint dataclasses, changelog replay, membership intervals
    metrics             sprint + board metrics formulas (ComputeMetrics, SMA5, KPI)
    heatmap             task x working-day matrix for target sprints
    burndown            calendar-day burndown reconstructed from a sprint payload
    forecast            Monte-Carlo bootstrap forecast over calendar daily throughput
    jira_client         HTTP client: auth, pagination, retries, rate limiting
    gitlab_client       HTTP client: auth, pagination, retries, read-only GitLab REST v4
    personal_metrics    per-person GitLab MR + Jira issue metrics
    engineering_metrics team-level pipeline/deployment/coverage metrics
    sprint_series       per-sprint bucketing for the dynamics/trend charts
    labels_ru           the single Russian dictionary (labels, warnings, roles, glossary)
    csv_export          the 18 CSV files written under out/
    out_writer          writes CSV/JSON/text/raw files under one output directory
    logging_setup       central logging config (--verbose/--quiet)
    config              CLI args + JSON config file + environment variables
    report_data         assembles the full report dict (the orchestration entry point)
    render_html         renders report.json into the self-contained HTML report
    cli                 the `team-metrics init/check/run/report/doctor` dispatcher
"""

from .report_data import build_report, main

__all__ = ["build_report", "main"]
