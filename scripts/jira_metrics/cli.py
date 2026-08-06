"""Command surface for the `jira-metrics` tool: init / check / run / report.

Two ways to reach this dispatcher — pick either, both call `main()` below:

    <skill-dir>/scripts/jira-metrics <command> ...        # executable script
    python3 -m jira_metrics <command> ...                 # from <skill-dir>/scripts

report_data.py and render_html.py keep working exactly as before (unchanged
flags, unchanged behavior) — this module only adds a layer on top:

    init    write .jira-metrics.json into the current directory
    check   verify Jira/GitLab reachability + config, no report built
    run     fetch + compute + write BOTH the JSON data file and the HTML report
    report  render HTML from an existing JSON data file — zero network calls

`report` never imports/constructs a Jira or GitLab client and never reads
JIRA_BASE_URL/JIRA_TOKEN/GITLAB_URL/GITLAB_TOKEN — see tests/test_cli.py's
ZeroNetworkTests for the enforcement.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from . import config as config_mod
from . import gitlab_client as glc
from . import jira_client as jc
from . import metrics as metrics_mod
from . import render_html as render_html_mod
from . import report_data

# scripts/jira_metrics/cli.py -> scripts/jira_metrics -> scripts -> <skill-dir>
# Same derivation render_html.py uses for TEMPLATE_PATH — never an absolute,
# hardcoded path, since this whole tree gets copied into ~/.claude/skills/.
SKILL_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG_FILENAME = ".jira-metrics.example.json"
EXAMPLE_CONFIG_PATH = SKILL_ROOT / EXAMPLE_CONFIG_FILENAME

DEFAULT_RUN_HTML_OUT = "report.html"
DEFAULT_RUN_JSON_OUT = "report.json"

# The set of report_data.py schema_version values `report`/render_html.py can
# safely render. Bump alongside metrics.SCHEMA_VERSION when the report dict
# shape changes in a way old JSON can no longer feed into render_html.
SUPPORTED_SCHEMA_VERSIONS = frozenset({metrics_mod.SCHEMA_VERSION})


class CliError(Exception):
    """User-facing error for any of the four subcommands."""


# --------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jira-metrics", description="Jira/GitLab team metrics report tool")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Write .jira-metrics.json into the current directory")
    p_init.add_argument("--force", action="store_true", help="Overwrite an existing .jira-metrics.json")

    p_check = sub.add_parser("check", help="Verify Jira/GitLab setup without building a report")
    p_check.add_argument("--sprint-ids", default=None, help="Comma-separated Jira sprint ids to test resolution for")
    p_check.add_argument("--sprint-names", default=None, help="Comma-separated Jira sprint names to test resolution for")
    p_check.add_argument("--board-id", type=int, default=None, help="Board id to test resolution for")
    p_check.add_argument("--config", default=None, help=f"Path to JSON config file (default: ./{config_mod.DEFAULT_CONFIG_FILENAME} if present)")
    p_check.add_argument("--no-gitlab", action="store_true", help="Skip the GitLab checks even if GITLAB_URL/GITLAB_TOKEN are set")

    p_run = sub.add_parser("run", help="Fetch from Jira/GitLab, compute metrics, write JSON + HTML")
    config_mod.add_pipeline_args(p_run)
    p_run.add_argument("--out", default=DEFAULT_RUN_HTML_OUT, help=f"HTML report output path (default: {DEFAULT_RUN_HTML_OUT})")
    p_run.add_argument("--json-out", default=DEFAULT_RUN_JSON_OUT, help=f"JSON data output path (default: {DEFAULT_RUN_JSON_OUT})")

    p_report = sub.add_parser("report", help="Render HTML from an existing JSON data file — no network calls")
    p_report.add_argument("report_json", nargs="?", default=None, help="Path to a report_data JSON file (default: stdin)")
    p_report.add_argument("-o", "--out", default=None, help="Output HTML path (default: stdout)")
    p_report.add_argument("--template", default=None, help="Override the template path (default: templates/report.html)")

    return parser


# --------------------------------------------------------------------------
# init
# --------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace, environ: dict, invocation: str) -> int:
    dest = Path(config_mod.DEFAULT_CONFIG_FILENAME)
    if dest.exists() and not args.force:
        print(f"{dest} already exists; pass --force to overwrite", file=sys.stderr)
        return 1

    example_text = EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8")
    example_obj = json.loads(example_text)
    # Defense in depth: the bundled example never carries a token-shaped key,
    # but never write one to disk even if that ever regressed.
    config_mod._check_no_token_keys(example_obj)

    dest.write_text(example_text, encoding="utf-8")
    print(f"wrote {dest}")

    required = ["JIRA_BASE_URL", "JIRA_TOKEN"]
    optional = ["GITLAB_URL", "GITLAB_TOKEN"]
    missing_required = [name for name in required if not (environ.get(name) or "").strip()]
    missing_optional = [name for name in optional if not (environ.get(name) or "").strip()]

    if missing_required:
        print("still export (required):")
        for name in missing_required:
            print(f"  export {name}=...")
    else:
        print("JIRA_BASE_URL/JIRA_TOKEN are already set")

    if missing_optional and len(missing_optional) < len(optional):
        # Exactly one of GITLAB_URL/GITLAB_TOKEN set is a misconfiguration
        # (config.load_gitlab_env fails fast on it) — flag it now rather than
        # let the user discover it only at `check`/`run` time.
        print("warning: only one of GITLAB_URL/GITLAB_TOKEN is set — both or neither is required:")
        for name in missing_optional:
            print(f"  export {name}=...")
    elif missing_optional:
        print("still export (optional — enables the Персональные/Инженерия tabs):")
        for name in missing_optional:
            print(f"  export {name}=...")
    else:
        print("GITLAB_URL/GITLAB_TOKEN are already set")

    print(f"edit {dest} for your team's story-point field, GitLab projects, and employees")
    print(f"next: {invocation} check")
    return 0


# --------------------------------------------------------------------------
# check
# --------------------------------------------------------------------------


class _CheckItem:
    __slots__ = ("name", "status", "detail")

    def __init__(self, name: str, status: str, detail: str = ""):
        self.name = name
        self.status = status  # "PASS" | "FAIL" | "SKIP"
        self.detail = detail


def cmd_check(
    args: argparse.Namespace,
    environ: dict,
    *,
    jira_client_cls: Callable[..., Any] = jc.JiraClient,
    gitlab_client_cls: Callable[..., Any] = glc.GitLabClient,
) -> int:
    items: list[_CheckItem] = []

    # 1. Jira env vars
    env = None
    try:
        env = config_mod.load_env(environ)
        items.append(_CheckItem("jira env vars", "PASS", f"JIRA_BASE_URL={env.base_url}"))
    except config_mod.ConfigError as e:
        items.append(_CheckItem("jira env vars", "FAIL", str(e)))

    # 2. GitLab env vars
    gitlab_env = None
    if args.no_gitlab:
        items.append(_CheckItem("gitlab env vars", "SKIP", "--no-gitlab"))
    else:
        try:
            gitlab_env = config_mod.load_gitlab_env(environ)
            if gitlab_env is None:
                items.append(_CheckItem("gitlab env vars", "SKIP", "GITLAB_URL/GITLAB_TOKEN not set"))
            else:
                items.append(_CheckItem("gitlab env vars", "PASS", f"GITLAB_URL={gitlab_env.base_url}"))
        except config_mod.ConfigError as e:
            items.append(_CheckItem("gitlab env vars", "FAIL", str(e)))

    # 3. config file
    file_config = None
    try:
        file_config = config_mod.load_file_config(args.config)
        items.append(_CheckItem("config file", "PASS", args.config or f"./{config_mod.DEFAULT_CONFIG_FILENAME} (or defaults)"))
    except config_mod.ConfigError as e:
        items.append(_CheckItem("config file", "FAIL", str(e)))

    # 4. Jira connectivity + auth
    field_ids: Optional[dict] = None
    jira_client_obj = None
    if env is not None:
        try:
            jira_client_obj = jira_client_cls(env.base_url, env.token)
            field_ids = jira_client_obj.field_ids()
            items.append(_CheckItem("jira connectivity", "PASS", f"{len(field_ids)} fields visible"))
        except jc.JiraError as e:
            items.append(_CheckItem("jira connectivity", "FAIL", str(e)))
    else:
        items.append(_CheckItem("jira connectivity", "SKIP", "jira env vars missing"))

    # 5. story-point field discoverable
    if field_ids is not None and file_config is not None:
        override = file_config.story_points_field_id
        if override:
            if override in field_ids.values():
                items.append(_CheckItem("story point field", "PASS", f"using configured story_points_field_id {override}"))
            else:
                items.append(
                    _CheckItem("story point field", "FAIL", f"configured story_points_field_id {override!r} not found among Jira fields")
                )
        else:
            field_id, field_name, found = jc._first_field_id(field_ids, jc.STORY_POINTS_FIELD_NAMES)
            if found:
                items.append(_CheckItem("story point field", "PASS", f"auto-detected {field_name!r} ({field_id})"))
            else:
                items.append(_CheckItem("story point field", "FAIL", "no Story Points field found; set story_points_field_id in the config file"))
    else:
        items.append(_CheckItem("story point field", "SKIP", "jira connectivity/config check did not pass"))

    # 6. named sprints / board resolvable (only if the user asked to check one)
    sprint_ids_raw = (args.sprint_ids or "").strip()
    sprint_names_raw = (args.sprint_names or "").strip()
    if sprint_ids_raw or sprint_names_raw or args.board_id is not None:
        if jira_client_obj is None:
            items.append(_CheckItem("sprint/board resolution", "SKIP", "jira connectivity check did not pass"))
        else:
            try:
                sprint_ids = [int(x.strip()) for x in sprint_ids_raw.split(",") if x.strip()]
                sprint_names = [x.strip() for x in sprint_names_raw.split(",") if x.strip()]
                if sprint_ids or sprint_names:
                    targets = report_data._resolve_target_sprints(jira_client_obj, sprint_ids, sprint_names)
                    resolved_board_id = targets[0].board_id
                    detail = ", ".join(f"{s.name!r} (#{s.id})" for s in targets) + f", board {resolved_board_id}"
                    if args.board_id is not None and args.board_id != resolved_board_id:
                        items.append(
                            _CheckItem(
                                "sprint/board resolution",
                                "FAIL",
                                f"--board-id {args.board_id} does not match resolved board {resolved_board_id}",
                            )
                        )
                    else:
                        items.append(_CheckItem("sprint/board resolution", "PASS", detail))
                else:
                    board = jira_client_obj.board(args.board_id)
                    items.append(_CheckItem("sprint/board resolution", "PASS", f"board {board.id} ({board.name!r})"))
            except (jc.JiraError, report_data.ReportError) as e:
                items.append(_CheckItem("sprint/board resolution", "FAIL", str(e)))
    else:
        items.append(_CheckItem("sprint/board resolution", "SKIP", "no --sprint-ids/--sprint-names/--board-id given"))

    # 7. GitLab connectivity + auth
    gitlab_client_obj = None
    if args.no_gitlab:
        items.append(_CheckItem("gitlab connectivity", "SKIP", "--no-gitlab"))
    elif gitlab_env is None:
        items.append(_CheckItem("gitlab connectivity", "SKIP", "GitLab not configured"))
    else:
        try:
            gitlab_client_obj = gitlab_client_cls(gitlab_env.base_url, gitlab_env.token)
            user = gitlab_client_obj.current_user()
            items.append(_CheckItem("gitlab connectivity", "PASS", f"authenticated as {user.get('username', '?')!r}"))
        except glc.GitLabError as e:
            items.append(_CheckItem("gitlab connectivity", "FAIL", str(e)))
            gitlab_client_obj = None

    # 8. configured GitLab projects resolvable
    if args.no_gitlab:
        items.append(_CheckItem("gitlab projects", "SKIP", "--no-gitlab"))
    elif gitlab_client_obj is None:
        items.append(_CheckItem("gitlab projects", "SKIP", "gitlab connectivity check did not pass"))
    elif file_config is None:
        items.append(_CheckItem("gitlab projects", "SKIP", "config file check did not pass"))
    elif not file_config.gitlab_projects:
        items.append(_CheckItem("gitlab projects", "SKIP", "no gitlab.projects configured"))
    else:
        # Same per-project shape as gitlab_client.fetch_team_data()'s
        # skipped_projects ({"project","code","message"} dicts) — read the
        # fields, never stringify the dict (that was exactly the bug caught
        # in the render_html.py footer review).
        skipped: list[dict] = []
        try:
            for proj_path in file_config.gitlab_projects:
                try:
                    pid = gitlab_client_obj.project_id(proj_path)
                except glc.GitLabError as e:
                    skipped.append({"project": proj_path, "code": e.code, "message": e.message})
                    continue
                if pid is None:
                    skipped.append({"project": proj_path, "code": "NOT_FOUND", "message": "project id not returned"})
            if skipped:
                detail = "; ".join(f"{s['project']} [{s['code'] or 'ERR'}]: {s['message']}" for s in skipped)
                items.append(_CheckItem("gitlab projects", "FAIL", detail))
            else:
                items.append(_CheckItem("gitlab projects", "PASS", f"{len(file_config.gitlab_projects)}/{len(file_config.gitlab_projects)} resolve"))
        except glc.GitLabError as e:
            # AUTH_FAILED mid-loop (token revoked between item 7 and here) —
            # mirrors fetch_team_data(), which lets AUTH_FAILED propagate
            # rather than folding it into skipped_projects.
            items.append(_CheckItem("gitlab projects", "FAIL", str(e)))

    ok = True
    for item in items:
        line = f"[{item.status}] {item.name}"
        if item.detail:
            line += f" — {item.detail}"
        print(line)
        if item.status == "FAIL":
            ok = False
    return 0 if ok else 1


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------


def cmd_run(
    args: argparse.Namespace,
    environ: dict,
    *,
    jira_client_cls: Callable[..., Any] = jc.JiraClient,
    gitlab_client_cls: Callable[..., Any] = glc.GitLabClient,
) -> int:
    # Strip the run-specific --out/--json-out before handing the namespace to
    # build_run_config_from_args(): that function falls back to args.out for
    # RunConfig.out_path (report_data.py's own JSON-path meaning), which would
    # collide with `run`'s --out (the HTML path) if left in.
    pipeline_ns = argparse.Namespace(**{k: v for k, v in vars(args).items() if k not in ("out", "json_out", "command")})
    try:
        run_cfg = config_mod.build_run_config_from_args(pipeline_ns, environ)
    except config_mod.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    client = jira_client_cls(run_cfg.env.base_url, run_cfg.env.token)
    gitlab_cli = None
    if run_cfg.gitlab_env is not None and not run_cfg.no_gitlab:
        gitlab_cli = gitlab_client_cls(run_cfg.gitlab_env.base_url, run_cfg.gitlab_env.token)

    try:
        report = report_data.build_combined_report(
            client,
            sprint_ids=run_cfg.sprint_ids,
            sprint_names=run_cfg.sprint_names,
            board_id_override=run_cfg.board_id,
            history_sprint_count=run_cfg.history_sprint_count,
            status_map=run_cfg.file_config.status_map,
            cancelled_statuses=run_cfg.file_config.cancelled_statuses,
            story_points_field_id=run_cfg.file_config.story_points_field_id,
            seed=run_cfg.seed,
            iterations=run_cfg.iterations,
            target_items=run_cfg.target_items,
            gitlab_client_obj=gitlab_cli,
            gitlab_projects=run_cfg.file_config.gitlab_projects,
            employees=run_cfg.file_config.employees,
            final_statuses=run_cfg.file_config.final_statuses,
            include_personal=not run_cfg.no_personal,
        )
    except (report_data.ReportError, jc.JiraError, glc.GitLabError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    json_text = json.dumps(report, ensure_ascii=False, indent=2)
    json_path = Path(args.json_out)
    json_path.write_text(json_text, encoding="utf-8")

    html_text = render_html_mod.render_html(report)
    html_path = Path(args.out)
    html_path.write_text(html_text, encoding="utf-8")

    print(f"JSON data written to {json_path}")
    print(f"HTML report written to {html_path}")
    return 0


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------


def _check_schema_version(report: dict) -> None:
    got = report.get("schema_version")
    if got not in SUPPORTED_SCHEMA_VERSIONS:
        raise CliError(
            f"report JSON schema_version {got!r} is not supported by this tool "
            f"(supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)}); regenerate the JSON with `run` or report_data.py"
        )


def cmd_report(args: argparse.Namespace) -> int:
    """Renders HTML from an existing JSON data file. Makes zero network calls
    and never touches JIRA_*/GITLAB_* — no jira_client/gitlab_client import
    site anywhere in this function."""
    if args.report_json:
        report = json.loads(Path(args.report_json).read_text(encoding="utf-8"))
    else:
        report = json.loads(sys.stdin.read())

    try:
        _check_schema_version(report)
    except CliError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    template_path = Path(args.template) if args.template else None
    html_text = render_html_mod.render_html(report, template_path=template_path)

    if args.out:
        Path(args.out).write_text(html_text, encoding="utf-8")
        print(f"HTML report written to {args.out}")
    else:
        sys.stdout.write(html_text)
    return 0


# --------------------------------------------------------------------------
# top-level dispatch
# --------------------------------------------------------------------------


def main(argv: Optional[list] = None, environ: Optional[dict] = None, invocation: Optional[str] = None) -> int:
    environ = environ if environ is not None else os.environ
    invocation = invocation or (sys.argv[0] if sys.argv else "jira-metrics")

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "init":
        return cmd_init(args, environ, invocation)
    if args.command == "check":
        return cmd_check(args, environ)
    if args.command == "run":
        return cmd_run(args, environ)
    if args.command == "report":
        return cmd_report(args)
    parser.error(f"unknown command {args.command!r}")
    return 2  # pragma: no cover - argparse.error() already exits


if __name__ == "__main__":
    raise SystemExit(main())
