"""Standalone Jira team-metrics data+math core.

Python 3.9+ standard library only. Reproduces the numbers of the Go module
apps/module-jira-metrics (see .research/jira-metrics-source/SPEC.md).

Module map:
    model        issue/sprint dataclasses, changelog replay, membership intervals
    metrics      sprint + board metrics formulas (ComputeMetrics, SMA5, KPI)
    heatmap      task x working-day matrix for target sprints
    burndown     calendar-day burndown reconstructed from a sprint payload
    forecast     Monte-Carlo bootstrap forecast over calendar daily throughput
    jira_client  HTTP client: auth, pagination, retries, rate limiting
    config       CLI args + JSON config file + environment variables
    report_data  assembles the full report dict (the orchestration entry point)
"""

from .report_data import build_report, main

__all__ = ["build_report", "main"]
