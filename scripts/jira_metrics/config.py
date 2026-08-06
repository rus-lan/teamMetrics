"""CLI args + JSON config file + environment variables.

JIRA_BASE_URL / JIRA_TOKEN and GITLAB_URL / GITLAB_TOKEN come only from the
environment — never from the config file, never logged, never written to
output (team-lead brief, hard constraint). The optional JSON config file
only carries non-secret knobs: status_map, cancelled_statuses, a story-point
field id override, history_sprint_count, the GitLab project list, the
employee login list, and the Jira final-statuses list the personal-metrics
half uses.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

DEFAULT_CANCELLED_STATUSES = ["Cancelled", "Отменено", "Rejected"]
DEFAULT_HISTORY_SPRINTS = 5
MAX_HISTORY_SPRINTS = 20
DEFAULT_SEED = 42
DEFAULT_CONFIG_FILENAME = ".jira-metrics.json"

# Mirrors personal_metrics.FINAL_STATUSES_DEFAULT — duplicated here (rather
# than imported) so config.py, which only the CLI/env layer depends on,
# doesn't have to import the GitLab-wiring modules just for one constant.
DEFAULT_FINAL_STATUSES = (
    "To Test",
    "Ready for Initial Test",
    "Done",
    "Closed",
    "Ready to Deploy",
)

# Substrings, not whole keys — matched against a normalized (lowercased,
# non-alphanumeric stripped) key so "private_token", "Access-Token",
# "api_key" and similar all get caught, not just the handful of exact names
# this list used to require. checked against the project's own schema
# (status_map/cancelled_statuses/story_points_field_id/history_sprint_count/
# gitlab.projects/employees/jira.final_statuses) — none of those normalize to
# a string containing any of these substrings.
_TOKEN_LIKE_SUBSTRINGS = ("token", "pat", "password", "passwd", "secret", "apikey", "credential")


class ConfigError(Exception):
    pass


def _reject_crlf(name: str, value: str) -> None:
    """Rejects a token containing an embedded CR/LF before it ever reaches an
    HTTP header — http.client's own header validation raises a bare
    ValueError that embeds the value verbatim, which would put the PAT in a
    traceback. The message here never echoes `value`."""
    if "\r" in value or "\n" in value:
        raise ConfigError(f"{name} must not contain carriage-return or line-feed characters")


def _normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.strip().lower())


def _is_token_like_key(key: str) -> bool:
    normalized = _normalize_key(key)
    return any(sub in normalized for sub in _TOKEN_LIKE_SUBSTRINGS)


def _check_no_token_keys(obj: dict, path: str = "") -> None:
    """Recursively rejects any token/secret-shaped key, at the top level or
    nested one level in (e.g. under "gitlab": {...}) — the same rule that
    already guarded the Jira token now also guards a GitLab token someone
    might otherwise be tempted to put in the config file."""
    for key, value in obj.items():
        if _is_token_like_key(key):
            full = f"{path}.{key}" if path else key
            raise ConfigError(
                f"config file must not contain a token/secret (found key {full!r}); use environment variables instead"
            )
        if isinstance(value, dict):
            _check_no_token_keys(value, path=f"{path}.{key}" if path else key)


@dataclass
class FileConfig:
    status_map: dict[str, str] = field(default_factory=dict)
    cancelled_statuses: list[str] = field(default_factory=lambda: list(DEFAULT_CANCELLED_STATUSES))
    story_points_field_id: str = ""
    history_sprint_count: int = 0
    gitlab_projects: list[str] = field(default_factory=list)
    employees: list[str] = field(default_factory=list)
    final_statuses: list[str] = field(default_factory=lambda: list(DEFAULT_FINAL_STATUSES))


def load_file_config(path: Optional[str]) -> FileConfig:
    if path is None:
        default_path = Path(DEFAULT_CONFIG_FILENAME)
        if not default_path.exists():
            return FileConfig()
        path = str(default_path)

    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config file not found: {path}")
    with p.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise ConfigError(f"config file must be a JSON object: {path}")

    _check_no_token_keys(raw)

    gitlab_raw = raw.get("gitlab") or {}
    if not isinstance(gitlab_raw, dict):
        raise ConfigError('config file "gitlab" key must be a JSON object')
    jira_raw = raw.get("jira") or {}
    if not isinstance(jira_raw, dict):
        raise ConfigError('config file "jira" key must be a JSON object')

    cfg = FileConfig()
    cfg.status_map = dict(raw.get("status_map") or {})
    cancelled = raw.get("cancelled_statuses")
    cfg.cancelled_statuses = list(cancelled) if cancelled else list(DEFAULT_CANCELLED_STATUSES)
    cfg.story_points_field_id = str(raw.get("story_points_field_id") or "")
    cfg.history_sprint_count = int(raw.get("history_sprint_count") or 0)
    cfg.gitlab_projects = list(gitlab_raw.get("projects") or [])
    cfg.employees = list(raw.get("employees") or [])
    final_statuses = jira_raw.get("final_statuses")
    cfg.final_statuses = list(final_statuses) if final_statuses else list(DEFAULT_FINAL_STATUSES)
    return cfg


def resolve_history_sprint_count(requested: int) -> int:
    """0/empty -> default 5; clamp to 20 (SPEC §1.1 history_sprint_count)."""
    n = requested
    if n <= 0:
        n = DEFAULT_HISTORY_SPRINTS
    if n > MAX_HISTORY_SPRINTS:
        n = MAX_HISTORY_SPRINTS
    return n


@dataclass
class Env:
    base_url: str
    token: str


def load_env(environ: Optional[dict] = None) -> Env:
    environ = environ if environ is not None else os.environ
    base_url = (environ.get("JIRA_BASE_URL") or "").strip()
    token = (environ.get("JIRA_TOKEN") or "").strip()
    if not base_url:
        raise ConfigError("JIRA_BASE_URL environment variable is required")
    if not token:
        raise ConfigError("JIRA_TOKEN environment variable is required")
    _reject_crlf("JIRA_TOKEN", token)
    return Env(base_url=base_url, token=token)


@dataclass
class GitLabEnv:
    base_url: str
    token: str


def load_gitlab_env(environ: Optional[dict] = None) -> Optional[GitLabEnv]:
    """GITLAB_URL/GITLAB_TOKEN, env-only, same rules as load_env(). Both
    absent -> None (GitLab tabs unavailable, not a hard failure — team-lead
    brief). Exactly one present is treated as a misconfiguration, since a
    half-configured GitLab connection can only fail loudly later."""
    environ = environ if environ is not None else os.environ
    base_url = (environ.get("GITLAB_URL") or "").strip()
    token = (environ.get("GITLAB_TOKEN") or "").strip()
    if not base_url and not token:
        return None
    if not base_url:
        raise ConfigError("GITLAB_URL environment variable is required when GITLAB_TOKEN is set")
    if not token:
        raise ConfigError("GITLAB_TOKEN environment variable is required when GITLAB_URL is set")
    _reject_crlf("GITLAB_TOKEN", token)
    return GitLabEnv(base_url=base_url, token=token)


def add_pipeline_args(p: argparse.ArgumentParser) -> None:
    """Adds every report_data.py flag except `--json`/`--out`, which differ
    between report_data.py's own CLI (JSON-only, unchanged below) and the
    `run` subcommand of the `jira-metrics` dispatcher (writes JSON + HTML,
    see cli.py) — shared here so the two never drift apart."""
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--sprint-ids", help="Comma-separated Jira sprint ids (target sprints)")
    target.add_argument("--sprint-names", help="Comma-separated Jira sprint names (target sprints)")
    p.add_argument(
        "--board-id",
        type=int,
        default=None,
        help="Optional board id, cross-checked against the resolved target sprints' board",
    )
    p.add_argument(
        "--history",
        type=int,
        default=0,
        help="Previous closed sprints analyzed in addition to targets (0 -> default 5, clamp 20)",
    )
    p.add_argument("--seed", type=int, default=DEFAULT_SEED, help="Monte-Carlo bootstrap RNG seed (deterministic)")
    p.add_argument(
        "--target-items",
        type=int,
        default=None,
        help="Forecast target item count; default: remaining items of the board's active sprint",
    )
    p.add_argument("--iterations", type=int, default=0, help="Monte-Carlo iterations (0 -> default 5000)")
    p.add_argument("--config", default=None, help=f"Path to JSON config file (default: ./{DEFAULT_CONFIG_FILENAME} if present)")
    p.add_argument(
        "--no-gitlab",
        action="store_true",
        help="Skip both GitLab-derived tabs (personal + engineering) even if GITLAB_URL/GITLAB_TOKEN are set",
    )
    p.add_argument(
        "--no-personal",
        action="store_true",
        help="Skip the personal-metrics tab; engineering (team-level pipelines/deployments/coverage) still runs",
    )


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jira_metrics", description="Jira team metrics report data builder")
    add_pipeline_args(p)
    p.add_argument("--json", action="store_true", help="Print the full report_data dict as JSON to stdout")
    p.add_argument("--out", default=None, help="Write the JSON report to this file")
    return p


@dataclass
class RunConfig:
    env: Env
    file_config: FileConfig
    sprint_ids: list[int]
    sprint_names: list[str]
    board_id: Optional[int]
    history_sprint_count: int
    seed: int
    target_items: Optional[int]
    iterations: int
    json_out: bool
    out_path: Optional[str]
    gitlab_env: Optional[GitLabEnv]
    no_gitlab: bool
    no_personal: bool


def build_run_config_from_args(args: argparse.Namespace, environ: Optional[dict] = None) -> RunConfig:
    """Builds a RunConfig from an already-parsed Namespace holding at least the
    args `add_pipeline_args()` adds (sprint_ids/sprint_names/board_id/history/
    seed/target_items/iterations/config/no_gitlab/no_personal).

    `json_out`/`out_path` come from `args.json`/`args.out` when present,
    defaulting to False/None otherwise. report_data.py's own CLI (below)
    supplies both; the `run` subcommand of the `jira-metrics` dispatcher
    (cli.py) supplies neither — it manages its own --out/--json-out file
    writing and passes a Namespace with those two attributes stripped so this
    function's defaults apply."""
    env = load_env(environ)
    gitlab_env = load_gitlab_env(environ)
    file_config = load_file_config(args.config)

    sprint_ids: list[int] = []
    sprint_names: list[str] = []
    if args.sprint_ids:
        sprint_ids = [int(x.strip()) for x in args.sprint_ids.split(",") if x.strip()]
    if args.sprint_names:
        sprint_names = [x.strip() for x in args.sprint_names.split(",") if x.strip()]

    history = args.history if args.history else file_config.history_sprint_count
    history_sprint_count = resolve_history_sprint_count(history)

    return RunConfig(
        env=env,
        file_config=file_config,
        sprint_ids=sprint_ids,
        sprint_names=sprint_names,
        board_id=args.board_id,
        history_sprint_count=history_sprint_count,
        seed=args.seed,
        target_items=args.target_items,
        iterations=args.iterations,
        json_out=getattr(args, "json", False),
        out_path=getattr(args, "out", None),
        gitlab_env=gitlab_env,
        no_gitlab=args.no_gitlab,
        no_personal=args.no_personal,
    )


def parse_args(argv: Optional[list[str]] = None, environ: Optional[dict] = None) -> RunConfig:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    return build_run_config_from_args(args, environ)
