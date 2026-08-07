"""Command surface for the `team-metrics` tool: init / check / run / report /
doctor, plus a top-level --version/-v.

Two ways to reach this dispatcher — pick either, both call `main()` below:

    <skill-dir>/scripts/team-metrics <command> ...        # executable script
    python3 -m team_metrics <command> ...                 # from <skill-dir>/scripts

report_data.py and render_html.py keep working exactly as before (unchanged
flags, unchanged behavior) — this module only adds a layer on top:

    init    write .team-metrics.json into the current directory
    check   verify Jira/GitLab reachability + config, no report built
    run     fetch + compute + write BOTH the JSON data file and the HTML report
    report  render HTML from an existing JSON data file — zero network calls
    doctor  check the environment/install (Python version, skill files, PATH,
            config file, env var presence) — no network, no tokens required

`report` never imports/constructs a Jira or GitLab client and never reads
JIRA_BASE_URL/JIRA_TOKEN/GITLAB_URL/GITLAB_TOKEN — see tests/test_cli.py's
ZeroNetworkTests for the enforcement. `doctor` makes the same guarantee for
the same reason: it is meant to run right after install, before any token is
ever set.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from . import config as config_mod
from . import csv_export
from . import gitlab_client as glc
from . import jira_client as jc
from . import logging_setup
from . import metrics as metrics_mod
from . import out_writer
from . import render_html as render_html_mod
from . import report_data

log = logging_setup.get_logger("cli")

# scripts/team_metrics/cli.py -> scripts/team_metrics -> scripts -> <skill-dir>
# Same derivation render_html.py uses for TEMPLATE_PATH — never an absolute,
# hardcoded path, since this whole tree gets copied into ~/.claude/skills/.
SKILL_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG_FILENAME = ".team-metrics.example.json"
EXAMPLE_CONFIG_PATH = SKILL_ROOT / EXAMPLE_CONFIG_FILENAME
VERSION_FILENAME = "VERSION"
VERSION_PATH = SKILL_ROOT / VERSION_FILENAME
GLOBAL_SKILL_DIR = Path.home() / ".claude" / "skills" / "team-metrics"

DEFAULT_RUN_HTML_OUT = "report.html"
DEFAULT_RUN_JSON_OUT = "report.json"


def _read_version() -> str:
    """Single source of truth is the VERSION file at the skill root — never a
    hardcoded string here. An installed copy with a missing/unreadable
    VERSION file must still run (`--version` degrades, doesn't crash)."""
    try:
        text = VERSION_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return "неизвестна (не найден файл VERSION)"
    return text or "неизвестна (файл VERSION пуст)"

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
    "out_dir": f"Папка для собранных данных и отчётов (по умолчанию: ./{config_mod.DEFAULT_OUT_DIR})",
    "verbose": "Подробные логи (уровень DEBUG)",
    "quiet": "Только ошибки (уровень ERROR)",
}


def _translate_pipeline_help(parser: argparse.ArgumentParser) -> None:
    for action in parser._actions:
        if action.dest in _RU_PIPELINE_ARG_HELP:
            action.help = _RU_PIPELINE_ARG_HELP[action.dest]


# Both JiraClient and GitLabClient accept trust_env_proxy=False (same
# semantics/precedence: an explicit opener= wins outright; otherwise True
# keeps urllib's default ProxyHandler honoring http(s)_proxy/no_proxy, False
# replaces it with an empty ProxyHandler({}) so requests go direct) — see
# cmd_check/cmd_run, where this flag is threaded to both client
# constructions.
NO_PROXY_HELP = "Игнорировать HTTP_PROXY/HTTPS_PROXY из окружения для запросов к Jira и GitLab — подключаться напрямую"

# gitlab_client.GitLabClient.merge_requests(..., fetch_mr_details=False) skips
# the per-MR detail+commits fan-out; .pipelines(..., fetch_pipeline_user=False)
# skips the per-pipeline job-user lookup. Both degrade the affected metrics
# through existing *_available flags (diff_stats_available,
# commits_count_available, changes_count_available, user_lookup_available) —
# «нет данных», never a fabricated 0. Deliberately defined directly on `run`'s
# own parser, NOT via config.add_pipeline_args() — report_data.py's
# build_arg_parser() defines its own copy of these two (same flag spelling,
# English help) rather than sharing, precisely so cli.py owns the Russian
# wording/--light shorthand here without a double-registration conflict; see
# config.py's build_arg_parser() for the matching comment on that side.
# config.build_run_config_from_args() reads these by getattr with a False
# default either way, so RunConfig.fetch_mr_details/fetch_pipeline_user comes
# out right regardless of which parser produced the Namespace.
#
# Measured audit: ~1660 GitLab requests per `run` on 5 projects/8 people/~150
# pipelines-per-project at concurrency 8 (up to ~6600 with retries); the two
# per-MR fan-outs account for ~800 of those, the per-pipeline lookup for
# ~750 — skipping both cuts roughly 1200.
NO_MR_DETAILS_HELP = (
    "Не запрашивать детали и коммиты каждого MR — экономит ~800 запросов к GitLab из ~1660 в среднем run; "
    "размер diff и число коммитов в персональных метриках станут «нет данных»"
)
NO_PIPELINE_USERS_HELP = (
    "Не определять пользователя для каждого pipeline — экономит ~750 запросов к GitLab из ~1660 в среднем run; "
    "связанные с пользователем поля пайплайнов станут «нет данных»"
)
LIGHT_HELP = (
    "Отключить оба тяжёлых обхода GitLab разом (детали+коммиты MR, пользователь pipeline) — "
    "экономит около 1200 запросов из ~1660 в среднем run; связанные метрики станут «нет данных»; "
    "то же самое, что --no-mr-details --no-pipeline-users вместе"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="team-metrics", description="Отчёты по метрикам команды из Jira и GitLab")
    # argparse's `version` action fires and exits immediately when it sees
    # -v/--version, the same way -h/--help does — before the "command is
    # required" check on the subparsers below, so `team-metrics --version`
    # (no subcommand) works.
    parser.add_argument("-v", "--version", action="version", version=f"team-metrics {_read_version()}")
    sub = parser.add_subparsers(dest="command", required=True, help="команда")

    p_init = sub.add_parser("init", help="Создать .team-metrics.json в текущей папке")
    p_init.add_argument("--force", action="store_true", help="Перезаписать существующий .team-metrics.json")

    p_check = sub.add_parser("check", help="Проверить настройку Jira/GitLab без построения отчёта")
    p_check.add_argument("--sprint-ids", default=None, metavar="ID[,ID...]", help="Список id спринтов через запятую — проверить, что они находятся")
    p_check.add_argument("--sprint-names", default=None, metavar="NAME[,NAME...]", help="Список названий спринтов через запятую — проверить, что они находятся")
    p_check.add_argument("--board-id", type=int, default=None, metavar="ID", help="Id доски — проверить, что она находится")
    p_check.add_argument("--config", default=None, metavar="ПУТЬ", help=f"Путь к JSON-файлу настроек (по умолчанию: ./{config_mod.DEFAULT_CONFIG_FILENAME}, если есть)")
    p_check.add_argument("--no-gitlab", action="store_true", help="Пропустить проверки GitLab, даже если заданы GITLAB_URL/GITLAB_TOKEN")
    p_check.add_argument("--no-proxy", action="store_true", help=NO_PROXY_HELP)

    p_run = sub.add_parser("run", help="Собрать данные из Jira/GitLab, посчитать метрики, записать JSON и HTML")
    config_mod.add_pipeline_args(p_run)
    _translate_pipeline_help(p_run)
    p_run.add_argument(
        "--out", default=None, metavar="ПУТЬ",
        help=f"Путь для HTML-отчёта (по умолчанию: <папка вывода>/{DEFAULT_RUN_HTML_OUT})",
    )
    p_run.add_argument(
        "--json-out", default=None, metavar="ПУТЬ",
        help=f"Путь для JSON-файла с данными (по умолчанию: <папка вывода>/{DEFAULT_RUN_JSON_OUT})",
    )
    p_run.add_argument("--no-proxy", action="store_true", help=NO_PROXY_HELP)
    p_run.add_argument("--no-mr-details", action="store_true", help=NO_MR_DETAILS_HELP)
    p_run.add_argument("--no-pipeline-users", action="store_true", help=NO_PIPELINE_USERS_HELP)
    p_run.add_argument("--light", action="store_true", help=LIGHT_HELP)

    p_report = sub.add_parser("report", help="Отрисовать HTML из уже полученного JSON-файла — без обращений к сети")
    p_report.add_argument("report_json", nargs="?", default=None, metavar="ПУТЬ", help="Путь к JSON-файлу report_data (по умолчанию: stdin)")
    p_report.add_argument("-o", "--out", default=None, metavar="ПУТЬ", help="Путь для HTML-файла (по умолчанию: stdout)")
    p_report.add_argument("--template", default=None, metavar="ПУТЬ", help="Свой путь к шаблону (по умолчанию: templates/report.html)")

    sub.add_parser("doctor", help="Проверить окружение и установку skill — без сети, без токенов")

    return parser


def _old_config_hint() -> Optional[str]:
    """Flags a pre-2.0.0 `.jira-metrics.json` sitting unused in the current
    directory. Never read or renamed automatically here — silently loading a
    file the user thinks is unused would be worse than telling them about it
    and letting them rename it themselves."""
    old_path = Path(config_mod.OLD_CONFIG_FILENAME)
    new_path = Path(config_mod.DEFAULT_CONFIG_FILENAME)
    if old_path.exists() and not new_path.exists():
        return f"найден {old_path} (имя файла настроек до переименования в team-metrics); переименуйте его: mv {old_path} {new_path}"
    return None


# --------------------------------------------------------------------------
# init
# --------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace, environ: dict, invocation: str) -> int:
    dest = Path(config_mod.DEFAULT_CONFIG_FILENAME)
    old_hint = _old_config_hint()
    if old_hint:
        print(old_hint)
    if dest.exists() and not args.force:
        print(f"{dest} уже существует; укажите --force для перезаписи", file=sys.stderr)
        return 1

    try:
        example_text = EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8")
        example_obj = json.loads(example_text)
    except OSError as e:
        print(f"ошибка: не удалось прочитать встроенный пример конфига {EXAMPLE_CONFIG_PATH} — похоже, установка повреждена: {e.strerror or e}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        print(f"ошибка: встроенный пример конфига {EXAMPLE_CONFIG_PATH} повреждён (не JSON) — похоже, установка повреждена: {e}", file=sys.stderr)
        return 1
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
        self.status = status  # "PASS" | "FAIL" | "SKIP" | "WARN" (internal only, never printed)
        self.detail = detail


# internal status -> printed Russian label
_STATUS_LABEL_RU = {"PASS": "УСПЕШНО", "FAIL": "ОШИБКА", "SKIP": "ПРОПУЩЕНО", "WARN": "ПРЕДУПРЕЖДЕНИЕ"}


def _print_check_items(items: list[_CheckItem]) -> bool:
    """Prints one `[СТАТУС] name — detail` line per item; returns True iff no
    item FAILed. WARN/SKIP never flip the result — `check`/`doctor` both exit
    non-zero only on a real FAIL, per their own contracts."""
    ok = True
    for item in items:
        line = f"[{_STATUS_LABEL_RU[item.status]}] {item.name}"
        if item.detail:
            line += f" — {item.detail}"
        print(line)
        if item.status == "FAIL":
            ok = False
    return ok


# jira_client.JiraClient authenticates with Authorization: Bearer <PAT> —
# Personal Access Tokens with that header only work on Jira Server/Data
# Center from version 8.14.0 onward (confirmed: .research/jira-912-compat/
# FINDINGS.md source 7). Below it, auth cannot work at all regardless of
# what else is configured correctly.
MIN_JIRA_VERSION_FOR_BEARER_AUTH = (8, 14)


def _parse_jira_major_minor(info: "jc.ServerInfo") -> Optional[tuple]:
    """(major, minor) from ServerInfo, preferring the structured
    versionNumbers list (e.g. [9, 12, 28]) and falling back to parsing the
    leading "N.N" off the version string when that list is missing or too
    short. None when neither yields a confident answer — the caller must
    WARN rather than guess at that point, never assume a version is fine."""
    if len(info.version_numbers) >= 2:
        return (info.version_numbers[0], info.version_numbers[1])
    if info.version:
        m = re.match(r"^\s*(\d+)\.(\d+)", info.version)
        if m:
            return (int(m.group(1)), int(m.group(2)))
    return None


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
        items.append(_CheckItem("переменные окружения Jira", "PASS", f"JIRA_BASE_URL={_redact_base_url(env.base_url)}"))
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
                items.append(_CheckItem("переменные окружения GitLab", "PASS", f"GITLAB_URL={_redact_base_url(gitlab_env.base_url)}"))
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
            jira_client_obj = jira_client_cls(env.base_url, env.token, trust_env_proxy=not args.no_proxy)
            field_ids = jira_client_obj.field_ids()
            items.append(_CheckItem("подключение к Jira", "PASS", f"видно полей: {len(field_ids)}"))
        except jc.JiraError as e:
            items.append(_CheckItem("подключение к Jira", "FAIL", str(e)))
    else:
        items.append(_CheckItem("подключение к Jira", "SKIP", "переменные окружения Jira не заданы"))

    # 5. Jira server version + deployment type — knowing WHICH server was
    # reached is more useful before the field/sprint items than after, since
    # a version/deployment mismatch explains failures those items would
    # otherwise report with no obvious cause.
    if jira_client_obj is None:
        items.append(_CheckItem("версия Jira", "SKIP", "проверка подключения к Jira не пройдена"))
    else:
        try:
            info = jira_client_obj.server_info()
        except jc.JiraError as e:
            # Some instances restrict this endpoint — being unable to READ
            # the version is not a reason to block a run that would
            # otherwise work.
            items.append(_CheckItem("версия Jira", "WARN", f"не удалось определить версию Jira: {e}"))
        else:
            deployment_type = info.deployment_type or ""
            version_label = info.version or "?"
            detail = f"Jira {deployment_type or '?'} {version_label} (deploymentType: {deployment_type or '?'})"

            concerns: list[str] = []
            if deployment_type.strip().lower() == "cloud":
                concerns.append(
                    "обнаружен Jira Cloud — инструмент рассчитан на Server/Data Center: Cloud использует другую "
                    "схему авторизации (email + API-токен через Basic) и частично другой API (в частности, "
                    "/rest/api/2/search на Cloud устарел в пользу /search/jql, которого здесь нет)"
                )

            major_minor = _parse_jira_major_minor(info)
            if major_minor is None:
                concerns.append(f"не удалось разобрать версию {version_label!r} — не могу проверить совместимость")
            elif major_minor < MIN_JIRA_VERSION_FOR_BEARER_AUTH:
                min_major, min_minor = MIN_JIRA_VERSION_FOR_BEARER_AUTH
                concerns.append(
                    f"версия {version_label} старше {min_major}.{min_minor} — Personal Access Token с заголовком "
                    "Authorization: Bearer появились только в 8.14.0, авторизация работать не будет"
                )

            if concerns:
                items.append(_CheckItem("версия Jira", "WARN", f"{detail}; " + "; ".join(concerns)))
            else:
                items.append(_CheckItem("версия Jira", "PASS", detail))

    # 6. story-point field discoverable
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

    # 7. named sprints / board resolvable (only if the user asked to check one)
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

    # 8. GitLab connectivity + auth
    gitlab_client_obj = None
    if args.no_gitlab:
        items.append(_CheckItem("подключение к GitLab", "SKIP", "--no-gitlab"))
    elif gitlab_env is None:
        items.append(_CheckItem("подключение к GitLab", "SKIP", "GitLab не настроен"))
    else:
        try:
            gitlab_client_obj = gitlab_client_cls(gitlab_env.base_url, gitlab_env.token, trust_env_proxy=not args.no_proxy)
            user = gitlab_client_obj.current_user()
            items.append(_CheckItem("подключение к GitLab", "PASS", f"выполнена аутентификация как {user.get('username', '?')!r}"))
        except glc.GitLabError as e:
            items.append(_CheckItem("подключение к GitLab", "FAIL", str(e)))
            gitlab_client_obj = None

    # 9. configured GitLab projects resolvable
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

    # 10. GitLab deployments probe — issues the SAME request `run` will make
    # (calls GitLabClient.deployments() itself, never reimplements its query
    # params here) for the first configured project only, with the narrowest
    # possible window. A project resolving (item 9) proves the project path
    # is valid; it does NOT prove the deployments endpoint accepts the
    # window-filtered request shape run actually sends — that gap is exactly
    # what let a live GitLab Server 19.0 reject `run` mid-collection after
    # `check` had already said everything was fine.
    if args.no_gitlab:
        items.append(_CheckItem("запрос деплоев GitLab", "SKIP", "--no-gitlab"))
    elif gitlab_client_obj is None:
        items.append(_CheckItem("запрос деплоев GitLab", "SKIP", "проверка подключения к GitLab не пройдена"))
    elif file_config is None:
        items.append(_CheckItem("запрос деплоев GitLab", "SKIP", "проверка файла настроек не пройдена"))
    elif not file_config.gitlab_projects:
        items.append(_CheckItem("запрос деплоев GitLab", "SKIP", "в gitlab.projects ничего не задано"))
    else:
        probe_project = file_config.gitlab_projects[0]
        try:
            pid = gitlab_client_obj.project_id(probe_project)
            if pid is None:
                items.append(
                    _CheckItem("запрос деплоев GitLab", "WARN", f"{probe_project}: не разрешился — см. пункт «проекты GitLab»")
                )
            else:
                now = datetime.now(timezone.utc)
                probe_window = glc.Window(start=now - timedelta(minutes=1), end=now)
                gitlab_client_obj.deployments(probe_project, pid, window=probe_window)
                items.append(_CheckItem("запрос деплоев GitLab", "PASS", f"{probe_project}: запрос принят"))
        except glc.GitLabError as e:
            if e.status_code == 400:
                # The request shape itself is rejected (e.g. a filter/sort
                # coupling GitLab requires but we didn't send) — this is
                # exactly "this run will not work", the whole point of the
                # probe.
                items.append(_CheckItem("запрос деплоев GitLab", "FAIL", f"{probe_project}: GitLab отклонил запрос — {e.message}"))
            elif e.code == "AUTH_FAILED":
                items.append(
                    _CheckItem("запрос деплоев GitLab", "WARN", f"{probe_project}: нет доступа к проекту (роль токена?) — {e.message}")
                )
            elif e.code == "NOT_FOUND":
                items.append(_CheckItem("запрос деплоев GitLab", "WARN", f"{probe_project}: проект не найден для этого запроса — {e.message}"))
            else:
                items.append(_CheckItem("запрос деплоев GitLab", "WARN", f"{probe_project}: пробный запрос не выполнен — {e.message}"))

    ok = _print_check_items(items)
    return 0 if ok else 1


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------


def _format_run_targets(run_cfg: config_mod.RunConfig) -> str:
    if run_cfg.sprint_ids:
        return ", ".join(str(i) for i in run_cfg.sprint_ids)
    return ", ".join(run_cfg.sprint_names)


def _resolve_out_path(explicit: Optional[str], out_dir: str, default_name: str) -> tuple:
    """Splits an explicit `--out`/`--json-out` value into (dir, filename) so
    it can go through `out_writer` (which only accepts a bare filename, never
    a path). `explicit=None` resolves to `<out_dir>/<default_name>`.

    A BARE filename (no `/` or `\\` anywhere in the string, e.g. "myreport.
    html") resolves under `--out-dir` the same way — it is not a relative
    path escaping to the current directory. A value that names any directory
    at all (relative, absolute, or starting with "..") is honored exactly as
    given: `--out`/`--json-out` are a deliberate escape hatch to write
    somewhere other than `out_dir`, and out_writer.py's own filename-safety
    guarantee is unaffected by that choice — it covers the `filename`
    argument passed to its writers, not the `out_dir` argument, which every
    caller in this module (including this one) supplies as a trusted path."""
    if explicit is None:
        return out_dir, default_name
    if "/" not in explicit and "\\" not in explicit:
        return out_dir, explicit
    p = Path(explicit)
    parent = str(p.parent) if str(p.parent) not in ("", ".") else "."
    return parent, p.name


def _same_path(a: str, b: str) -> bool:
    return os.path.abspath(a) == os.path.abspath(b)


def _validate_run_output_paths(out_dir: str, out_arg: Optional[str], json_out_arg: Optional[str]) -> tuple:
    """Resolves and validates every path `run` will write to — all BEFORE
    any client is built or any request sent. An `--out-dir` that already
    exists as a plain file, or an unsafe `--out`/`--json-out` filename, must
    fail fast here instead of only surfacing after a full fetch (up to
    ~1660 GitLab requests) has already completed.

    Returns `(json_dir, json_name, html_dir, html_name)`; raises `ValueError`
    with a ready-to-print Russian message on any problem."""
    if Path(out_dir).exists() and not Path(out_dir).is_dir():
        raise ValueError(f"«{out_dir}» уже существует и не является папкой — укажите другой --out-dir")

    json_dir, json_name = _resolve_out_path(json_out_arg, out_dir, DEFAULT_RUN_JSON_OUT)
    html_dir, html_name = _resolve_out_path(out_arg, out_dir, DEFAULT_RUN_HTML_OUT)

    for flag, name in (("--json-out", json_name), ("--out", html_name)):
        try:
            out_writer.check_safe_filename(name)
        except ValueError:
            raise ValueError(f"{flag}: недопустимое имя файла {name!r} — укажите простое имя файла без каталогов") from None

    for target_dir in (json_dir, html_dir):
        if not _same_path(target_dir, out_dir) and Path(target_dir).exists() and not Path(target_dir).is_dir():
            raise ValueError(f"«{target_dir}» уже существует и не является папкой")

    return json_dir, json_name, html_dir, html_name


def _swap_dir_into_place(staging: Path, out_dir: Path) -> None:
    """Atomically replaces `out_dir` with the fully-written `staging`
    directory. `os.rename` cannot rename onto a non-empty existing
    directory on POSIX, so an existing `out_dir` is renamed aside first,
    `staging` takes its place, and the old copy is removed last. If the
    final rename fails, the old `out_dir` is put back so a half-finished
    swap never leaves `out_dir` missing."""
    backup = None
    if out_dir.exists():
        backup = out_dir.parent / f".{out_dir.name}.previous-{os.getpid()}"
        os.rename(out_dir, backup)
    try:
        os.rename(staging, out_dir)
    except OSError:
        if backup is not None:
            os.rename(backup, out_dir)
        raise
    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)


def _atomic_write(write_fn: Callable[..., Path], out_dir: str, filename: str, payload: Any) -> Path:
    """Writes through `write_fn` (`out_writer.write_json`/`write_text`) to a
    hidden temp name in `out_dir`, then renames it over `filename` — used
    only when the target directory is NOT the run's own `out_dir` (which
    gets its own whole-directory staged swap instead), so report.json/
    report.html redirected elsewhere by an explicit --out/--json-out never
    exist on disk half-written."""
    out_writer.check_safe_filename(filename)
    tmp_name = f".{filename}.tmp-{os.getpid()}"
    write_fn(out_dir, tmp_name, payload)
    final = out_writer.ensure_out_dir(out_dir) / filename
    os.replace(Path(out_dir) / tmp_name, final)
    return final


def _write_run_outputs(out_dir: str, json_dir: str, json_name: str, html_dir: str, html_name: str, report: dict, raw: dict) -> tuple:
    """Writes raw dumps + 18 CSVs (always under `out_dir`) plus report.json/
    report.html, so that a failure partway through never leaves `out_dir`
    holding a mixture of this run's and a previous run's files (proven
    failure mode: `out/some.csv` turning into a directory, or the disk
    filling up, mid-list — see the review this fixes).

    Everything that belongs under `out_dir` is first written into a staging
    directory (a sibling of `out_dir`, guaranteed to share its filesystem)
    and swapped in with a single directory rename only once every write to
    it has succeeded — a failure before the swap leaves the previous
    `out_dir` completely untouched, and the incomplete staging directory is
    removed rather than left behind under a name that could be mistaken for
    a real result. report.json/report.html get the same staged-swap
    treatment when they land inside `out_dir` (the default, no --out/
    --json-out given); when redirected elsewhere by an explicit path they
    get their own independent atomic (temp-file + rename) write instead.

    Returns `(json_path, html_path, raw_written, csv_written)`; raises
    `OSError`/`ValueError` on any write failure."""
    out_dir_path = Path(out_dir).absolute()
    json_in_out_dir = _same_path(json_dir, out_dir)
    html_in_out_dir = _same_path(html_dir, out_dir)

    out_dir_path.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".team-metrics-run-", dir=str(out_dir_path.parent)))
    try:
        raw_written: list = [
            out_writer.write_raw(staging, "jira_issue_facts.json", report_data._to_jsonable(raw["facts"])),
            out_writer.write_raw(staging, "jira_sprints.json", report_data._to_jsonable(raw["axis"])),
        ]
        if raw["gitlab_configured"]:
            raw_written.append(out_writer.write_raw(staging, "gitlab_merge_requests.json", raw["mrs"]))
            raw_written.append(out_writer.write_raw(staging, "gitlab_pipelines.json", raw["pipelines"]))
            raw_written.append(out_writer.write_raw(staging, "gitlab_deployments.json", raw["deployments"]))
            raw_written.append(out_writer.write_raw(staging, "gitlab_coverage.json", raw["coverage"]))
            raw_written.append(out_writer.write_raw(staging, "gitlab_fetch_issues.json", report["gitlab_fetch_issues"]))

        csv_written = csv_export.write_all(staging, report, raw)

        if json_in_out_dir:
            out_writer.write_json(staging, json_name, report)
        if html_in_out_dir:
            out_writer.write_text(staging, html_name, render_html_mod.render_html(report))
    except (OSError, ValueError):
        shutil.rmtree(staging, ignore_errors=True)
        raise

    _swap_dir_into_place(staging, out_dir_path)

    json_path = out_dir_path / json_name if json_in_out_dir else _atomic_write(out_writer.write_json, json_dir, json_name, report)
    html_path = (
        out_dir_path / html_name
        if html_in_out_dir
        else _atomic_write(out_writer.write_text, html_dir, html_name, render_html_mod.render_html(report))
    )

    return json_path, html_path, raw_written, csv_written


def cmd_run(
    args: argparse.Namespace,
    environ: dict,
    *,
    jira_client_cls: Callable[..., Any] = jc.JiraClient,
    gitlab_client_cls: Callable[..., Any] = glc.GitLabClient,
) -> int:
    logging_setup.setup_logging(verbose=args.verbose, quiet=args.quiet)
    started_at = time.monotonic()

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

    try:
        json_dir, json_name, html_dir, html_name = _validate_run_output_paths(run_cfg.out_dir, args.out, args.json_out)
    except ValueError as e:
        print(f"ошибка настройки: {e}", file=sys.stderr)
        return 2

    log.info(
        "Запуск run: целевые спринты [%s], история %d предыдущих спринтов",
        _format_run_targets(run_cfg), run_cfg.history_sprint_count,
    )

    log.info("Подключение к Jira: %s", _redact_base_url(run_cfg.env.base_url))
    client = jira_client_cls(run_cfg.env.base_url, run_cfg.env.token, trust_env_proxy=not args.no_proxy)
    gitlab_cli = None
    if run_cfg.gitlab_env is not None and not run_cfg.no_gitlab:
        log.info("Подключение к GitLab: %s", _redact_base_url(run_cfg.gitlab_env.base_url))
        gitlab_cli = gitlab_client_cls(run_cfg.gitlab_env.base_url, run_cfg.gitlab_env.token, trust_env_proxy=not args.no_proxy)

    # run_cfg.fetch_mr_details/fetch_pipeline_user already fold in
    # --no-mr-details/--no-pipeline-user (config.build_run_config_from_args()
    # computes them as `not args.no_mr_details`/`not args.no_pipeline_user`).
    # --light is this dispatcher's own shorthand on top — ANDed in here rather
    # than re-parsed, so either the individual flags or --light alone (or
    # together) turn a fan-out off.
    fetch_mr_details = run_cfg.fetch_mr_details and not args.light
    fetch_pipeline_user = run_cfg.fetch_pipeline_user and not args.light

    # report_data.build_combined_report_with_raw() owns sprint resolution,
    # issue fetch, and every metrics computation as one call, logging one
    # INFO line per pipeline stage itself (resolving sprints / fetching
    # issues / computing sprint metrics / fetching GitLab / computing
    # personal metrics / computing engineering metrics / building series /
    # assembling the report) — this dispatcher only logs the file-writing
    # stage that happens after it returns.
    try:
        report, raw = report_data.build_combined_report_with_raw(
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
            fetch_mr_details=fetch_mr_details,
            fetch_pipeline_user=fetch_pipeline_user,
            out_dir=run_cfg.out_dir,
            no_gitlab=run_cfg.no_gitlab,
            status_labels=run_cfg.file_config.status_labels,
        )
    except (report_data.ReportError, jc.JiraError, glc.GitLabError) as e:
        print(f"ошибка: {e}", file=sys.stderr)
        return 1

    log.info("Запись файлов результата")
    out_dir = run_cfg.out_dir

    try:
        json_path, html_path, raw_written, csv_written = _write_run_outputs(
            out_dir, json_dir, json_name, html_dir, html_name, report, raw
        )
    except (OSError, ValueError) as e:
        print(f"ошибка записи результатов: {e}", file=sys.stderr)
        return 1

    log.info("Сырые данные API записаны: %d файлов в %s/raw", len(raw_written), out_dir)
    log.info("CSV-файлы записаны: %d файлов в %s", len(csv_written), out_dir)
    log.info("Файл report.json записан: %s", json_path)
    log.info("Файл report.html записан: %s", html_path)

    print(f"файл с данными JSON записан: {json_path}")
    print(f"HTML-отчёт записан: {html_path}")
    # report["params"]["gitlab_request_count"] is report_data.py's own count
    # of actual GitLab HTTP round trips (including retries) for this run —
    # None when GitLab wasn't used at all. JiraClient has no equivalent
    # counter yet, so this is labeled as the GitLab number specifically,
    # never implied to be the run's total.
    gitlab_request_count = report.get("params", {}).get("gitlab_request_count")
    if gitlab_request_count is not None:
        print(f"HTTP-запросов к GitLab: {gitlab_request_count} (без учёта Jira — там счётчик пока не ведётся)")

    elapsed = time.monotonic() - started_at
    log.info(
        "Готово за %.1fs; запросов к GitLab: %s",
        elapsed, gitlab_request_count if gitlab_request_count is not None else "н/д",
    )
    return 0


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------


def _check_schema_version(report: dict) -> None:
    got = report.get("schema_version")
    if got not in SUPPORTED_SCHEMA_VERSIONS:
        raise CliError(
            f"schema_version {got!r} в JSON-файле не поддерживается этой версией инструмента (нужна схема v2). "
            "Этот JSON создан старой версией team-metrics — пересоздайте его командой `team-metrics run`."
        )


def cmd_report(args: argparse.Namespace) -> int:
    """Renders HTML from an existing JSON data file. Makes zero network calls
    and never touches JIRA_*/GITLAB_* — no jira_client/gitlab_client import
    site anywhere in this function."""
    source = args.report_json or "stdin"
    try:
        text = Path(args.report_json).read_text(encoding="utf-8") if args.report_json else sys.stdin.read()
    except OSError as e:
        print(f"ошибка: не удалось прочитать {source!r}: {e.strerror or e}", file=sys.stderr)
        return 1

    try:
        report = json.loads(text)
    except json.JSONDecodeError as e:
        print(f"ошибка: {source!r} не похож на корректный JSON: {e}", file=sys.stderr)
        return 1
    if not isinstance(report, dict):
        print(f"ошибка: {source!r} должен быть JSON-объектом, получено {type(report).__name__}", file=sys.stderr)
        return 1

    try:
        _check_schema_version(report)
    except CliError as e:
        print(f"ошибка: {e}", file=sys.stderr)
        return 1

    template_path = Path(args.template) if args.template else None
    html_text = render_html_mod.render_html(report, template_path=template_path)

    if args.out:
        out_path = Path(args.out)
        if out_path.is_symlink():
            print(f"ошибка: отказ писать сквозь symlink: {out_path}", file=sys.stderr)
            return 1
        out_path.write_text(html_text, encoding="utf-8")
        print(f"HTML-отчёт записан: {args.out}")
    else:
        sys.stdout.write(html_text)
    return 0


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------


def _detect_proxy(environ: dict) -> tuple:
    """Checks HTTPS_PROXY before HTTP_PROXY (both case variants — POSIX env
    vars are case-sensitive, so a shell can legitimately export either), the
    same precedence urllib.request.getproxies_environment() uses for an
    https:// request — every Jira/GitLab base URL this tool has ever been
    documented with is https. Returns (scheme_label, raw_value) or
    (None, None) if neither is set."""
    for scheme_label, names in (("HTTPS", ("https_proxy", "HTTPS_PROXY")), ("HTTP", ("http_proxy", "HTTP_PROXY"))):
        for name in names:
            value = (environ.get(name) or "").strip()
            if value:
                return scheme_label, value
    return None, None


def _proxy_host_only(url: str) -> str:
    """Extracts just the host from a proxy URL. The url itself may embed
    credentials (`http://user:pass@host:port`) — never returned or printed
    whole, only the host, even if parsing the value fails outright."""
    candidate = url if "://" in url else f"http://{url}"
    try:
        host = urllib.parse.urlparse(candidate).hostname
    except ValueError:
        host = None
    return host or "хост не определён"


def _redact_base_url(url: str) -> str:
    """Strips embedded userinfo (`user:pass@`) from a Jira/GitLab base URL
    before it is ever printed or logged, keeping everything else (scheme,
    host, port, path) verbatim — the same "never shown whole" rule
    `_proxy_host_only` applies to a proxy URL, except a base URL keeps more
    than just the host, since that (not just the host) is exactly the
    diagnostic information `check`/`run` output needs. Never raises: an
    unparseable value degrades to a fixed placeholder rather than risking a
    partially-redacted string reaching stdout/logs."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "URL не определён (не удалось разобрать)"
    netloc = parts.netloc.rsplit("@", 1)[-1] if "@" in parts.netloc else parts.netloc
    try:
        return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except ValueError:
        return "URL не определён (не удалось разобрать)"


def _host_bypassed_by_no_proxy(host: str, no_proxy_value: str) -> bool:
    """Minimal no_proxy/NO_PROXY matcher: comma/space-separated hostnames or
    domain suffixes, `*` bypasses everything. Deliberately re-implemented
    against the `environ` dict every check/doctor item already threads
    through (rather than urllib.request.proxy_bypass_environment(), which
    reads the real os.environ and can't be pointed at a test's fake one)."""
    if not host or not no_proxy_value:
        return False
    entries = [e.strip().lower().lstrip(".") for e in no_proxy_value.replace(" ", ",").split(",") if e.strip()]
    if "*" in entries:
        return True
    host_l = host.lower()
    return any(host_l == e or host_l.endswith(f".{e}") for e in entries)


def cmd_doctor(args: argparse.Namespace, environ: dict, invocation: str) -> int:
    """Checks the ENVIRONMENT and the install, not Jira/GitLab credentials —
    `check` covers connectivity/auth; `doctor` must work with no tokens set
    and no network at all, right after a fresh install."""
    items: list[_CheckItem] = []

    # 1. Python version
    v = sys.version_info
    version_str = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) >= (3, 9):
        items.append(_CheckItem("версия Python", "PASS", version_str))
    else:
        items.append(_CheckItem("версия Python", "FAIL", f"{version_str} — нужен Python 3.9 или новее"))

    # 2. the skill's own files present and readable
    skill_files = {
        "templates/report.html": render_html_mod.TEMPLATE_PATH,
        EXAMPLE_CONFIG_FILENAME: EXAMPLE_CONFIG_PATH,
        VERSION_FILENAME: VERSION_PATH,
    }
    unreadable: list[str] = []
    for label, path in skill_files.items():
        try:
            Path(path).read_text(encoding="utf-8")
        except OSError as e:
            unreadable.append(f"{label} ({e.strerror or e})")
    if unreadable:
        items.append(_CheckItem("файлы skill", "FAIL", "; ".join(unreadable)))
    else:
        items.append(_CheckItem("файлы skill", "PASS", ", ".join(skill_files)))

    # 3. global skill install directory (informational — a checkout run is fine)
    if GLOBAL_SKILL_DIR.is_dir():
        items.append(_CheckItem("установка skill", "PASS", f"режим: глобальная установка ({GLOBAL_SKILL_DIR})"))
    else:
        items.append(
            _CheckItem(
                "установка skill",
                "WARN",
                f"режим: локальная копия (git checkout) — {GLOBAL_SKILL_DIR} не найдена; "
                "это нормально для разработки, но команда team-metrics не будет доступна из других папок без ./install.sh",
            )
        )

    # 4. `team-metrics` launcher reachable on PATH
    launcher = shutil.which("team-metrics")
    if launcher:
        items.append(_CheckItem("команда team-metrics в PATH", "PASS", launcher))
    else:
        items.append(
            _CheckItem(
                "команда team-metrics в PATH",
                "WARN",
                'не найдена; добавьте в PATH: export PATH="$HOME/.local/bin:$PATH" (и откройте новый терминал)',
            )
        )

    # 5. config file in the current directory
    cfg_path = Path(config_mod.DEFAULT_CONFIG_FILENAME)
    if cfg_path.exists():
        items.append(_CheckItem("файл настроек в текущей папке", "PASS", str(cfg_path)))
    else:
        old_hint = _old_config_hint()
        if old_hint:
            items.append(_CheckItem("файл настроек в текущей папке", "WARN", old_hint))
        else:
            items.append(
                _CheckItem("файл настроек в текущей папке", "WARN", f"{cfg_path} не найден; запустите: {invocation} init")
            )

    # 6. env vars — presence only, никогда значение
    def _state(name: str) -> str:
        return "задан" if (environ.get(name) or "").strip() else "не задан"

    jira_complete = bool((environ.get("JIRA_BASE_URL") or "").strip()) and bool((environ.get("JIRA_TOKEN") or "").strip())
    gitlab_set_count = sum(1 for name in ("GITLAB_URL", "GITLAB_TOKEN") if (environ.get(name) or "").strip())
    env_detail = ", ".join(f"{name}: {_state(name)}" for name in ("JIRA_BASE_URL", "JIRA_TOKEN", "GITLAB_URL", "GITLAB_TOKEN"))
    if not jira_complete or gitlab_set_count == 1:
        items.append(_CheckItem("переменные окружения", "WARN", env_detail))
    else:
        items.append(_CheckItem("переменные окружения", "PASS", env_detail))

    # 7. renderer self-test — catches a broken/truncated install
    try:
        template_text = render_html_mod._load_template()
        if not template_text.strip():
            raise OSError("шаблон пуст")
        items.append(_CheckItem("самопроверка шаблона отчёта", "PASS", f"{len(template_text)} байт прочитано"))
    except OSError as e:
        items.append(_CheckItem("самопроверка шаблона отчёта", "FAIL", str(e)))

    # 8. proxy exposure — urllib keeps its default ProxyHandler, so
    # http(s)_proxy is honored and the "Authorization: Bearer <token>" header
    # travels through whatever it points at. Often intentional on a
    # corporate network, so this is a WARN, never a FAIL — but it must be
    # legible, not silent.
    proxy_scheme, proxy_value = _detect_proxy(environ)
    if proxy_value is None:
        items.append(_CheckItem("прокси", "PASS", "не настроен (HTTP_PROXY/HTTPS_PROXY не заданы)"))
    else:
        proxy_host = _proxy_host_only(proxy_value)
        jira_base_url = (environ.get("JIRA_BASE_URL") or "").strip()
        jira_host = urllib.parse.urlparse(jira_base_url).hostname if jira_base_url else ""
        no_proxy_value = (environ.get("no_proxy") or environ.get("NO_PROXY") or "").strip()

        if jira_host and _host_bypassed_by_no_proxy(jira_host, no_proxy_value):
            items.append(
                _CheckItem(
                    "прокси",
                    "PASS",
                    f"{proxy_scheme}_PROXY указывает на {proxy_host}, но хост Jira ({jira_host}) исключён через "
                    "no_proxy — запросы к Jira через этот прокси не идут",
                )
            )
        else:
            if jira_host:
                note = f" (хост Jira {jira_host} не входит в no_proxy)"
            else:
                note = " (JIRA_BASE_URL не задан — не могу проверить, входит ли он в no_proxy)"
            items.append(
                _CheckItem(
                    "прокси",
                    "WARN",
                    f"{proxy_scheme}_PROXY указывает на {proxy_host} — заголовок Authorization: Bearer <токен> "
                    f"будет проходить через этот прокси" + note +
                    "; обойти можно флагом --no-proxy у check/run (для запросов и к Jira, и к GitLab)",
                )
            )

    ok = _print_check_items(items)
    return 0 if ok else 1


# --------------------------------------------------------------------------
# top-level dispatch
# --------------------------------------------------------------------------

# A launcher that execs the real script by absolute path (e.g. install.sh's
# ~/.local/bin/team-metrics wrapper — needed because a symlink would break
# this script's own __file__-based self-location) loses the short name the
# user actually typed: sys.argv[0] becomes that absolute path. Setting this
# env var lets such a launcher tell cli.py what name to print in hints
# instead of trusting argv[0]. See _resolve_invocation_name().
INVOCATION_NAME_ENV_VAR = "TEAM_METRICS_BIN"


def _resolve_invocation_name(candidate: Optional[str], environ: dict) -> str:
    """Never returns an absolute path — a printed hint is something the user
    should be able to retype, not the launcher's internal exec target.

    Priority: an explicit override (TEAM_METRICS_BIN) > a relative candidate
    (git checkout: `scripts/team-metrics`, or the friendly `python3 -m
    team_metrics` string __main__.py passes) printed as-is > the short
    conventional name `team-metrics` for an absolute path or nothing at all.
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

    return "team-metrics"


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
    if args.command == "doctor":
        return cmd_doctor(args, environ, invocation)
    parser.error(f"неизвестная команда {args.command!r}")
    return 2  # pragma: no cover - argparse.error() already exits


if __name__ == "__main__":
    raise SystemExit(main())
