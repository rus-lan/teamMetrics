"""Shared synthetic-fixture builders for the test suite (not a test module itself)."""

from __future__ import annotations

import _pathfix  # noqa: F401  (must run before any `team_metrics` import — see tests/_pathfix.py)

from datetime import datetime, timezone

from team_metrics import model

UTC = timezone.utc


def dt(y: int, m: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> datetime:
    return datetime(y, m, d, h, mi, s, tzinfo=UTC)


def make_issue(
    key: str = "PROJ-1",
    *,
    story_points: float = 5.0,
    created: datetime = dt(2026, 1, 1),
    initial_status: str = "To Do",
    status_history: list | None = None,
    sp_events: list | None = None,
    labels: list | None = None,
    role: str = "",
    epic_key: str = "",
    assignee: str = "",
    qa_estimation: float = 0.0,
    issue_type: str = "Story",
) -> model.IssueInput:
    return model.IssueInput(
        key=key,
        epic_key=epic_key,
        type=issue_type,
        role=role,
        labels=labels or [],
        assignee=assignee,
        story_points=story_points,
        qa_estimation=qa_estimation,
        created=created,
        initial_status=initial_status,
        status_history=status_history or [],
        sp_events=sp_events or [],
    )


def make_timeline(start: datetime, schedule_end: datetime, classify_end: datetime | None = None) -> model.SprintTimeline:
    return model.SprintTimeline(start=start, schedule_end=schedule_end, classify_end=classify_end or schedule_end)


def make_cfg(
    status_map: dict | None = None,
    cancelled_statuses: list | None = None,
    jira_status_categories: dict | None = None,
) -> model.StatusCategoryConfig:
    return model.StatusCategoryConfig(
        status_map=status_map or {},
        cancelled_statuses=cancelled_statuses or [],
        jira_status_categories=jira_status_categories or {},
    )
