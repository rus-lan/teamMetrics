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


# Russian help text for the flags add_pipeline_args() adds — that function is
# shared with report_data.py's own (English, unchanged) CLI, so its `help=`
# strings must stay English there. Overriding `action.help` by dest AFTER the
# shared call keeps the flags themselves (name/type/default) single-sourced
# while letting `run --help` read in Russian — see _translate_pipeline_help().
_RU_PIPELINE_ARG_HELP = {
    "sprint_ids": "Список id спринтов Jira через запятую (целевые спринты)",
    "sprint_names": "Список названий спринтов через запятую (целевые спринты)",
    "board_id": "Id доски — сверяется с доской найденных целевых спринтов",
    "history": "Сколько предыдущих закрытых спринтов, кроме целевых, учитывать (0 -> по умолчанию 5, максимум 20)",
    "seed": "Seed для Monte-Carlo прогноза (детерминированный)",
    "target_items": "Целевое число задач для прогноза; по умолчанию — оставшиеся задачи активного спринта доски",
    "iterations": "Число итераций Monte-Carlo (0 -> по умолчанию 5000)",
    "config": f"Путь к JSON-файлу настроек (по умолчанию: ./{config_mod.DEFAULT_CONFIG_FILENAME}, если есть)",
    "no_gitlab": "Пропустить обе вкладки GitLab, даже если заданы GITLAB_URL/GITLAB_TOKEN",
    "no_personal": "Пропустить только вкладку персональных метрик; инженерная вкладка продолжит работать",
}


def _translate_pipeline_help(parser: argparse.ArgumentParser) -> None:
    for action in parser._actions:
        if action.dest in _RU_PIPELINE_ARG_HELP:
            action.help = _RU_PIPELINE_ARG_HELP[action.dest]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jira-metrics", description="Отчёты по метрикам команды из Jira и GitLab")
    sub = parser.add_subparsers(dest="command", required=True, help="команда")

    p_init = sub.add_parser("init", help="Создать .jira-metrics.json в текущей папке")
    p_init.add_argument("--force", action="store_true", help="Перезаписать существующий .jira-metrics.json")

    p_check = sub.add_parser("check", help="Проверить настройку Jira/GitLab без построения отчёта")
    p_check.add_argument("--sprint-ids", default=None, metavar="ID[,ID...]", help="Список id спринтов через запятую — проверить, что они находятся")
    p_check.add_argument("--sprint-names", default=None, metavar="NAME[,NAME...]", help="Список названий спринтов через запятую — проверить, что они находятся")
    p_check.add_argument("--board-id", type=int, default=None, metavar="ID", help="Id доски — проверить, что она находится")
    p_check.add_argument("--config", default=None, metavar="ПУТЬ", help=f"Путь к JSON-файлу настроек (по умолчанию: ./{config_mod.DEFAULT_CONFIG_FILENAME}, если есть)")
    p_check.add_argument("--no-gitlab", action="store_true", help="Пропустить проверки GitLab, даже если заданы GITLAB_URL/GITLAB_TOKEN")

    p_run = sub.add_parser("run", help="Собрать данные из Jira/GitLab, посчитать метрики, записать JSON и HTML")
    config_mod.add_pipeline_args(p_run)
    _translate_pipeline_help(p_run)
    p_run.add_argument("--out", default=DEFAULT_RUN_HTML_OUT, metavar="ПУТЬ", help=f"Путь для HTML-отчёта (по умолчанию: {DEFAULT_RUN_HTML_OUT})")
    p_run.add_argument("--json-out", default=DEFAULT_RUN_JSON_OUT, metavar="ПУТЬ", help=f"Путь для JSON-файла с данными (по умолчанию: {DEFAULT_RUN_JSON_OUT})")

    p_report = sub.add_parser("report", help="Отрисовать HTML из уже полученного JSON-файла — без обращений к сети")
    p_report.add_argument("report_json", nargs="?", default=None, metavar="ПУТЬ", help="Путь к JSON-файлу report_data (по умолчанию: stdin)")
    p_report.add_argument("-o", "--out", default=None, metavar="ПУТЬ", help="Путь для HTML-файла (по умолчанию: stdout)")
    p_report.add_argument("--template", default=None, metavar="ПУТЬ", help="Свой путь к шаблону (по умолчанию: templates/report.html)")

    return parser


# --------------------------------------------------------------------------
# init
# --------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace, environ: dict, invocation: str) -> int:
    dest = Path(config_mod.DEFAULT_CONFIG_FILENAME)
    if dest.exists() and not args.force:
        print(f"{dest} уже существует; укажите --force для перезаписи", file=sys.stderr)
        return 1

    example_text = EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8")
    example_obj = json.loads(example_text)
    # Defense in depth: the bundled example never carries a token-shaped key,
    # but never write one to disk even if that ever regressed.
    config_mod._check_no_token_keys(example_obj)

    dest.write_text(example_text, encoding="utf-8")
    print(f"файл {dest} создан")

    required = ["JIRA_BASE_URL", "JIRA_TOKEN"]
    optional = ["GITLAB_URL", "GITLAB_TOKEN"]
    missing_required = [name for name in required if not (environ.get(name) or "").strip()]
    missing_optional = [name for name in optional if not (environ.get(name) or "").strip()]

    if missing_required:
        print("нужно ещё задать (обязательно):")
        for name in missing_required:
            print(f"  export {name}=...")
    else:
        print("JIRA_BASE_URL/JIRA_TOKEN уже заданы")

    if missing_optional and len(missing_optional) < len(optional):
        # Exactly one of GITLAB_URL/GITLAB_TOKEN set is a misconfiguration
        # (config.load_gitlab_env fails fast on it) — flag it now rather than
        # let the user discover it only at `check`/`run` time.
        print("предупреждение: задан только один из GITLAB_URL/GITLAB_TOKEN — нужны либо оба, либо ни одного:")
        for name in missing_optional:
            print(f"  export {name}=...")
    elif missing_optional:
        print("нужно ещё задать (необязательно — включает вкладки «Персональные»/«Инженерия»):")
        for name in missing_optional:
            print(f"  export {name}=...")
    else:
        print("GITLAB_URL/GITLAB_TOKEN уже заданы")

    print(f"отредактируйте {dest}: поле story points, проекты GitLab, список сотрудников")
    print(f"дальше: {invocation} check")
    return 0


# --------------------------------------------------------------------------
# check
# --------------------------------------------------------------------------


class _CheckItem:
    __slots__ = ("name", "status", "detail")

    def __init__(self, name: str, status: str, detail: str = ""):
        self.name = name
        self.status = status  # "PASS" | "FAIL" | "SKIP" (internal only, never printed)
        self.detail = detail


# internal status -> printed Russian label
_STATUS_LABEL_RU = {"PASS": "УСПЕШНО", "FAIL": "ОШИБКА", "SKIP": "ПРОПУЩЕНО"}


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
        items.append(_CheckItem("переменные окружения Jira", "PASS", f"JIRA_BASE_URL={env.base_url}"))
    except config_mod.ConfigError as e:
        items.append(_CheckItem("переменные окружения Jira", "FAIL", str(e)))

    # 2. GitLab env vars
    gitlab_env = None
    if args.no_gitlab:
        items.append(_CheckItem("переменные окружения GitLab", "SKIP", "--no-gitlab"))
    else:
        try:
            gitlab_env = config_mod.load_gitlab_env(environ)
            if gitlab_env is None:
                items.append(_CheckItem("переменные окружения GitLab", "SKIP", "GITLAB_URL/GITLAB_TOKEN не заданы"))
            else:
                items.append(_CheckItem("переменные окружения GitLab", "PASS", f"GITLAB_URL={gitlab_env.base_url}"))
        except config_mod.ConfigError as e:
            items.append(_CheckItem("переменные окружения GitLab", "FAIL", str(e)))

    # 3. config file
    file_config = None
    try:
        file_config = config_mod.load_file_config(args.config)
        items.append(_CheckItem("файл настроек", "PASS", args.config or f"./{config_mod.DEFAULT_CONFIG_FILENAME} (или значения по умолчанию)"))
    except config_mod.ConfigError as e:
        items.append(_CheckItem("файл настроек", "FAIL", str(e)))

    # 4. Jira connectivity + auth
    field_ids: Optional[dict] = None
    jira_client_obj = None
    if env is not None:
        try:
            jira_client_obj = jira_client_cls(env.base_url, env.token)
            field_ids = jira_client_obj.field_ids()
            items.append(_CheckItem("подключение к Jira", "PASS", f"видно полей: {len(field_ids)}"))
        except jc.JiraError as e:
            items.append(_CheckItem("подключение к Jira", "FAIL", str(e)))
    else:
        items.append(_CheckItem("подключение к Jira", "SKIP", "переменные окружения Jira не заданы"))

    # 5. story-point field discoverable
    if field_ids is not None and file_config is not None:
        override = file_config.story_points_field_id
        if override:
            if override in field_ids.values():
                items.append(_CheckItem("поле Story Points", "PASS", f"используется заданный story_points_field_id {override}"))
            else:
                items.append(
                    _CheckItem("поле Story Points", "FAIL", f"заданный story_points_field_id {override!r} не найден среди полей Jira")
                )
        else:
            field_id, field_name, found = jc._first_field_id(field_ids, jc.STORY_POINTS_FIELD_NAMES)
            if found:
                items.append(_CheckItem("поле Story Points", "PASS", f"определено автоматически: {field_name!r} ({field_id})"))
            else:
                items.append(_CheckItem("поле Story Points", "FAIL", "поле Story Points не найдено; укажите story_points_field_id в файле настроек"))
    else:
        items.append(_CheckItem("поле Story Points", "SKIP", "проверка подключения к Jira/файла настроек не пройдена"))

    # 6. named sprints / board resolvable (only if the user asked to check one)
    sprint_ids_raw = (args.sprint_ids or "").strip()
    sprint_names_raw = (args.sprint_names or "").strip()
    if sprint_ids_raw or sprint_names_raw or args.board_id is not None:
        if jira_client_obj is None:
            items.append(_CheckItem("поиск спринта/доски", "SKIP", "проверка подключения к Jira не пройдена"))
        else:
            try:
                sprint_ids = [int(x.strip()) for x in sprint_ids_raw.split(",") if x.strip()]
                sprint_names = [x.strip() for x in sprint_names_raw.split(",") if x.strip()]
                if sprint_ids or sprint_names:
                    targets = report_data._resolve_target_sprints(jira_client_obj, sprint_ids, sprint_names)
                    resolved_board_id = targets[0].board_id
                    detail = ", ".join(f"{s.name!r} (#{s.id})" for s in targets) + f", доска {resolved_board_id}"
                    if args.board_id is not None and args.board_id != resolved_board_id:
                        items.append(
                            _CheckItem(
                                "поиск спринта/доски",
                                "FAIL",
                                f"--board-id {args.board_id} не совпадает с найденной доской {resolved_board_id}",
                            )
                        )
                    else:
                        items.append(_CheckItem("поиск спринта/доски", "PASS", detail))
                else:
                    board = jira_client_obj.board(args.board_id)
                    items.append(_CheckItem("поиск спринта/доски", "PASS", f"доска {board.id} ({board.name!r})"))
            except (jc.JiraError, report_data.ReportError) as e:
                items.append(_CheckItem("поиск спринта/доски", "FAIL", str(e)))
    else:
        items.append(_CheckItem("поиск спринта/доски", "SKIP", "не заданы --sprint-ids/--sprint-names/--board-id"))

    # 7. GitLab connectivity + auth
    gitlab_client_obj = None
    if args.no_gitlab:
        items.append(_CheckItem("подключение к GitLab", "SKIP", "--no-gitlab"))
    elif gitlab_env is None:
        items.append(_CheckItem("подключение к GitLab", "SKIP", "GitLab не настроен"))
    else:
        try:
            gitlab_client_obj = gitlab_client_cls(gitlab_env.base_url, gitlab_env.token)
            user = gitlab_client_obj.current_user()
            items.append(_CheckItem("подключение к GitLab", "PASS", f"выполнена аутентификация как {user.get('username', '?')!r}"))
        except glc.GitLabError as e:
            items.append(_CheckItem("подключение к GitLab", "FAIL", str(e)))
            gitlab_client_obj = None

    # 8. configured GitLab projects resolvable
    if args.no_gitlab:
        items.append(_CheckItem("проекты GitLab", "SKIP", "--no-gitlab"))
    elif gitlab_client_obj is None:
        items.append(_CheckItem("проекты GitLab", "SKIP", "проверка подключения к GitLab не пройдена"))
    elif file_config is None:
        items.append(_CheckItem("проекты GitLab", "SKIP", "проверка файла настроек не пройдена"))
    elif not file_config.gitlab_projects:
        items.append(_CheckItem("проекты GitLab", "SKIP", "в gitlab.projects ничего не задано"))
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
                items.append(_CheckItem("проекты GitLab", "FAIL", detail))
            else:
                items.append(_CheckItem("проекты GitLab", "PASS", f"найдено проектов: {len(file_config.gitlab_projects)}/{len(file_config.gitlab_projects)}"))
        except glc.GitLabError as e:
            # AUTH_FAILED mid-loop (token revoked between item 7 and here) —
            # mirrors fetch_team_data(), which lets AUTH_FAILED propagate
            # rather than folding it into skipped_projects.
            items.append(_CheckItem("проекты GitLab", "FAIL", str(e)))

    ok = True
    for item in items:
        line = f"[{_STATUS_LABEL_RU[item.status]}] {item.name}"
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
        print(f"ошибка настройки: {e}", file=sys.stderr)
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
        print(f"ошибка: {e}", file=sys.stderr)
        return 1

    json_text = json.dumps(report, ensure_ascii=False, indent=2)
    json_path = Path(args.json_out)
    json_path.write_text(json_text, encoding="utf-8")

    html_text = render_html_mod.render_html(report)
    html_path = Path(args.out)
    html_path.write_text(html_text, encoding="utf-8")

    print(f"файл с данными JSON записан: {json_path}")
    print(f"HTML-отчёт записан: {html_path}")
    return 0


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------


def _check_schema_version(report: dict) -> None:
    got = report.get("schema_version")
    if got not in SUPPORTED_SCHEMA_VERSIONS:
        raise CliError(
            f"schema_version {got!r} в JSON-файле не поддерживается этой версией инструмента "
            f"(поддерживается: {sorted(SUPPORTED_SCHEMA_VERSIONS)}); пересоздайте JSON командой `run` или report_data.py"
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
        print(f"ошибка: {e}", file=sys.stderr)
        return 1

    template_path = Path(args.template) if args.template else None
    html_text = render_html_mod.render_html(report, template_path=template_path)

    if args.out:
        Path(args.out).write_text(html_text, encoding="utf-8")
        print(f"HTML-отчёт записан: {args.out}")
    else:
        sys.stdout.write(html_text)
    return 0


# --------------------------------------------------------------------------
# top-level dispatch
# --------------------------------------------------------------------------

# A launcher that execs the real script by absolute path (e.g. install.sh's
# ~/.local/bin/jira-metrics wrapper — needed because a symlink would break
# this script's own __file__-based self-location) loses the short name the
# user actually typed: sys.argv[0] becomes that absolute path. Setting this
# env var lets such a launcher tell cli.py what name to print in hints
# instead of trusting argv[0]. See _resolve_invocation_name().
INVOCATION_NAME_ENV_VAR = "JIRA_METRICS_BIN"


def _resolve_invocation_name(candidate: Optional[str], environ: dict) -> str:
    """Never returns an absolute path — a printed hint is something the user
    should be able to retype, not the launcher's internal exec target.

    Priority: an explicit override (JIRA_METRICS_BIN) > a relative candidate
    (git checkout: `scripts/jira-metrics`, or the friendly `python3 -m
    jira_metrics` string __main__.py passes) printed as-is > the short
    conventional name `jira-metrics` for an absolute path or nothing at all.
    """
    override = (environ.get(INVOCATION_NAME_ENV_VAR) or "").strip()
    if override:
        return override

    if candidate and not os.path.isabs(candidate):
        return candidate

    if candidate:
        basename = os.path.basename(candidate)
        if basename:
            return basename

    return "jira-metrics"


def main(argv: Optional[list] = None, environ: Optional[dict] = None, invocation: Optional[str] = None) -> int:
    environ = environ if environ is not None else os.environ
    invocation = _resolve_invocation_name(invocation or (sys.argv[0] if sys.argv else None), environ)

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
    parser.error(f"неизвестная команда {args.command!r}")
    return 2  # pragma: no cover - argparse.error() already exits


if __name__ == "__main__":
    raise SystemExit(main())
