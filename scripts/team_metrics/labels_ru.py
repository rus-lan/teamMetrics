"""Single source of Russian user-facing text for schema v2 (SPEC §E).

Every other module that needs a Russian metric label, a warning/error message,
a role name, a status-category name, or a glossary/risk text imports it from
here — no other module in this package may hardcode one. report_data.py
copies these constants (or the result of calling `warn_message`/
`metric_label_ru`) verbatim into the JSON; render_html.py (a different track)
never imports this module at all — it reads the JSON only.
"""

from __future__ import annotations

from typing import Optional

# --------------------------------------------------------------------------
# E.1 — METRIC_DEFS_RU: all 28 source METRIC_DEFS entries (report_generator.py
# :512-573), mapped to our snake_case keys. "category": gl|jira|link|infra.
# "scope": person|team|both (infra rows are always "team").
# --------------------------------------------------------------------------

METRIC_DEFS_RU: list[dict] = [
    {"key": "mr_count", "label_ru": "Число MR", "unit_ru": "шт",
     "comment_ru": "Сколько мерж-реквестов открыто за период.", "category": "gl", "is_pct": False, "scope": "both"},
    {"key": "mr_merged_count", "label_ru": "MR, доведённых до merge", "unit_ru": "шт",
     "comment_ru": "Из них дошли до merge.", "category": "gl", "is_pct": False, "scope": "both"},
    {"key": "mr_merge_rate_pct", "label_ru": "Доля merge-запросов", "unit_ru": "%",
     "comment_ru": "Доля открытых MR, дошедших до merge.", "category": "gl", "is_pct": True, "scope": "both"},
    {"key": "mr_cycle_time_avg_hours", "label_ru": "PR cycle time (среднее)", "unit_ru": "часы",
     "comment_ru": "Среднее время от открытия MR до merge.", "category": "gl", "is_pct": False, "scope": "both"},
    {"key": "mr_cycle_time_median_hours", "label_ru": "PR cycle time (медиана)", "unit_ru": "часы",
     "comment_ru": "Медиана времени MR — устойчивее к выбросам.", "category": "gl", "is_pct": False, "scope": "both"},
    {"key": "mr_diff_size_avg", "label_ru": "Средний размер диффа", "unit_ru": "строк",
     "comment_ru": "additions + deletions в среднем на MR.", "category": "gl", "is_pct": False, "scope": "both"},
    {"key": "mr_commits_avg", "label_ru": "Среднее число коммитов на MR", "unit_ru": "шт",
     "comment_ru": "Среднее число коммитов в одном MR.", "category": "gl", "is_pct": False, "scope": "both"},
    {"key": "mr_changes_count_avg", "label_ru": "Средний вес MR (файлов)", "unit_ru": "шт",
     "comment_ru": "Сколько файлов в среднем затрагивает один MR.", "category": "gl", "is_pct": False, "scope": "both"},
    {"key": "mr_changes_count_sum", "label_ru": "Суммарное число файлов", "unit_ru": "шт",
     "comment_ru": "Общий объём доработок за период (по числу файлов).", "category": "gl", "is_pct": False, "scope": "both"},
    {"key": "tasks_done", "label_ru": "Завершённых задач", "unit_ru": "шт",
     "comment_ru": "Сколько задач доведено до done за период.", "category": "jira", "is_pct": False, "scope": "both"},
    {"key": "task_cycle_time_avg_hours", "label_ru": "Cycle time задач (среднее)", "unit_ru": "часы",
     "comment_ru": "Среднее время задачи в работе.", "category": "jira", "is_pct": False, "scope": "both"},
    {"key": "task_cycle_time_median_hours", "label_ru": "Cycle time задач (медиана)", "unit_ru": "часы",
     "comment_ru": "Медиана времени задачи.", "category": "jira", "is_pct": False, "scope": "both"},
    {"key": "defect_rate_pct", "label_ru": "Defect rate", "unit_ru": "%",
     "comment_ru": "Доля задач, закрытых как баг/ошибка.", "category": "jira", "is_pct": True, "scope": "both"},
    {"key": "rework_total", "label_ru": "Rework (всего возвратов)", "unit_ru": "шт",
     "comment_ru": "Сколько раз задачи возвращались в работу.", "category": "jira", "is_pct": False, "scope": "both"},
    {"key": "rework_rate_pct", "label_ru": "Rework rate", "unit_ru": "%",
     "comment_ru": "Доля задач с хотя бы одним возвратом.", "category": "jira", "is_pct": True, "scope": "both"},
    {"key": "story_points_total", "label_ru": "Сумма Story Points", "unit_ru": "шт",
     "comment_ru": "Сумма оценок сложности задач за период.", "category": "jira", "is_pct": False, "scope": "both"},
    {"key": "story_points_avg", "label_ru": "Среднее Story Points на задачу", "unit_ru": "шт",
     "comment_ru": "Относительная оценка сложности в расчёте на задачу.", "category": "jira", "is_pct": False, "scope": "both"},
    {"key": "qa_estimation_total", "label_ru": "Сумма QA Estimation", "unit_ru": "шт",
     "comment_ru": "Сумма оценок трудозатрат на тестирование.", "category": "jira", "is_pct": False, "scope": "both"},
    {"key": "qa_estimation_avg", "label_ru": "Среднее QA Estimation на задачу", "unit_ru": "шт",
     "comment_ru": "Средние трудозатраты на тестирование задачи.", "category": "jira", "is_pct": False, "scope": "both"},
    {"key": "linked_tasks", "label_ru": "Задач, связанных с MR", "unit_ru": "шт",
     "comment_ru": "Сколько задач нашли связь с MR по ключу (jira_key).", "category": "link", "is_pct": False, "scope": "both"},
    {"key": "mr_per_task", "label_ru": "MR на задачу (среднее)", "unit_ru": "шт",
     "comment_ru": "Сколько в среднем MR приходится на связанную задачу.", "category": "link", "is_pct": False, "scope": "both"},
    {"key": "pipelines_count", "label_ru": "Пайплайнов (всего)", "unit_ru": "шт",
     "comment_ru": "Число запусков CI за период.", "category": "infra", "is_pct": False, "scope": "team"},
    {"key": "pipeline_success_rate_pct", "label_ru": "Доля успешных пайплайнов", "unit_ru": "%",
     "comment_ru": "Сколько пайплайнов завершилось успешно.", "category": "infra", "is_pct": True, "scope": "team"},
    {"key": "pipelines_per_week", "label_ru": "Частота пайплайнов", "unit_ru": "шт/нед",
     "comment_ru": "Сколько пайплайнов в среднем в неделю.", "category": "infra", "is_pct": False, "scope": "team"},
    {"key": "deployments_count", "label_ru": "Деплоев (всего)", "unit_ru": "шт",
     "comment_ru": "Число выкатов за период.", "category": "infra", "is_pct": False, "scope": "team"},
    {"key": "deploy_success_rate_pct", "label_ru": "Доля успешных деплоев", "unit_ru": "%",
     "comment_ru": "Доля деплоев без сбоя.", "category": "infra", "is_pct": True, "scope": "team"},
    {"key": "deployments_per_week", "label_ru": "Частота деплоев", "unit_ru": "шт/нед",
     "comment_ru": "Сколько деплоев в среднем в неделю.", "category": "infra", "is_pct": False, "scope": "team"},
    {"key": "coverage_avg_pct", "label_ru": "Покрытие тестами", "unit_ru": "%",
     "comment_ru": "Среднее покрытие по пайплайнам (если есть).", "category": "infra", "is_pct": True, "scope": "team"},
]

_METRIC_DEFS_BY_KEY = {d["key"]: d for d in METRIC_DEFS_RU}

# --------------------------------------------------------------------------
# E.1 tail — person-only keys not among the 28 source METRIC_DEFS, but
# present in people[].metrics and therefore in labels.columns so the
# comparison table (tab 05) can show them.
# --------------------------------------------------------------------------

_EXTRA_PERSON_COLUMN_LABELS_RU: dict[str, str] = {
    "mr_closed_count": "MR закрыто без merge",
    "bug_count": "Из них багов",
    "rework_tasks": "Задач с возвратами",
    "issue_count": "Задач в расчёте cycle time",
    "mr_with_jira_key": "MR с ключом Jira",
    "mr_diff_size_available_count": "MR с данными о диффе",
    "mr_commits_sum": "Суммарное число коммитов",
    # Overrides the general "pipeline_success_rate_pct" entry below (this
    # dict is spread last into COLUMN_LABELS_RU) with the personal-scope
    # wording the people[]-comparison table needs (§E.1).
    "pipeline_success_rate_pct": "Доля успешных пайплайнов (личная)",
}

# --------------------------------------------------------------------------
# E.2 — COLUMN_LABELS_RU: every other machine key the renderer prints.
# --------------------------------------------------------------------------

COLUMN_LABELS_RU: dict[str, str] = {
    "sprint": "Спринт",
    "state": "Состояние",
    "start": "Начало",
    "end": "Конец",
    "committed_sp": "Обязательство, SP",
    "delivered_sp": "Поставлено, SP",
    "added_sp": "Добавлено, SP",
    "scope_added_sp": "Добавлено, SP",
    "removed_sp": "Убрано, SP",
    "scope_removed_sp": "Убрано, SP",
    "estimation_change_sp": "Изменение оценок, SP",
    "scope_estimation_change_sp": "Изменение оценок, SP",
    "performance_pct": "Performance, %",
    "load_pct": "Загрузка, %",
    "scope_change_pct": "Изменение объёма, %",
    "velocity_sp": "Velocity, SP",
    "sma5_sp": "SMA5, SP",
    "velocity_sma5_sp": "SMA5, SP",
    "throughput_count": "Throughput, задач",
    "throughput_items": "Throughput, задач",
    "closure_rate_count_pct": "% закрытия (задачи)",
    "closure_pct_items": "% закрытия (задачи)",
    "closure_rate_sp_pct": "% закрытия (SP)",
    "closure_pct_sp": "% закрытия (SP)",
    "committed_items": "Обязательство, задач",
    "delivered_items": "Поставлено, задач",
    "scope_added_items": "Добавлено, задач",
    "scope_removed_items": "Убрано, задач",
    "count": "Всего запусков",
    "failed": "Упавших",
    "success_rate_pct": "Доля успешных, %",
    "per_week": "В неделю, шт",
    "coverage_avg_pct": "Покрытие, %",
    "sample_count": "Число замеров",
    "per_project": "По проектам",
    "window_applied": "Окно дат применено",
    "project": "Проект",
    "throughput": "Завершено задач",
    "avg_cycle_time_hours": "Cycle (ср., ч)",
    "avg_mr_cycle_hours": "PR cycle (ср., ч)",
    "avg_mr_cycle_time_hours": "PR cycle (ср., ч)",
    "avg_mr_changes_count": "Вес MR (файл.)",
    "mr_merged_count": "Доведено до merge",
    "pipeline_count": "Пайплайны",
    "deployment_count": "Деплои",
    "pipeline_success_rate_pct": "Успешность CI, %",
    "deployment_success_rate_pct": "Успешность деплоев, %",
    "sprint_ids": "Целевые спринты",
    "sprint_names": "Целевые спринты",
    "board_id": "Доска",
    "history_sprint_count": "Спринтов истории",
    "seed": "Seed",
    "iterations": "Итераций Monte-Carlo",
    "target_items_requested": "Целевое число задач (запрошено)",
    "target_items_resolved": "Целевое число задач (вычислено)",
    "generated_at": "Сформирован",
    "tool_version": "Версия инструмента",
    "out_dir": "Папка вывода",
    "gitlab_window": "Окно GitLab",
    "gitlab_request_count": "HTTP-запросов к GitLab",
    "gitlab_fetch_mr_details": "Детали MR запрашивались",
    "gitlab_fetch_pipeline_user": "Пользователи пайплайнов запрашивались",
    "no_gitlab": "GitLab отключён",
    "no_personal": "Персональные метрики отключены",
    "calc_schema_version": "Версия схемы данных",
    "employees": "Сотрудников",
    "mr_total": "MR (всего)",
    "tasks_done_total": "Завершённых задач",
    "pr_cycle_time_avg_hours": "PR cycle time (средн.)",
    "task_cycle_time_avg_hours": "Cycle time задач (средн.)",
    "deploy_success_rate_pct": "Успешные деплои",
    "Epic": "Эпик",
    "Issue": "Задача",
    "Story Points": "Story Points",
    "QA Estimation": "QA Estimation",
    "Labels": "Метки",
    "Before": "До спринта",
    "End": "Конец",
    **_EXTRA_PERSON_COLUMN_LABELS_RU,
}

JIRA_LABEL_NOTE_RU = "Значение метки задаётся в вашей Jira — инструмент показывает её как есть."


def metric_label_ru(key: str) -> str:
    """Russian label for a metric/column machine key — checks METRIC_DEFS_RU
    first, then COLUMN_LABELS_RU, falling back to the key itself so a caller
    never crashes on an unmapped key (used to build a warning object's
    `detail` from a `_pct_or_zero(..., metric_name)`-style suffix)."""
    d = _METRIC_DEFS_BY_KEY.get(key)
    if d is not None:
        return d["label_ru"]
    return COLUMN_LABELS_RU.get(key, key)


# --------------------------------------------------------------------------
# E.5 — Roles
# --------------------------------------------------------------------------

ROLES_RU: dict[str, str] = {
    "FE": "Frontend-разработчик",
    "BE": "Backend-разработчик",
    "BA": "Бизнес-аналитик",
    "SA": "Системный аналитик",
    "QA": "Инженер по тестированию",
    "TL": "Технический лидер (тимлид)",
}


def role_label_ru(code: str) -> str:
    """Russian role name, or the code itself for an unknown role (E.5: shown
    as-is with a tooltip, never invented)."""
    return ROLES_RU.get(code, code)


# --------------------------------------------------------------------------
# E.8 — Status categories (verbatim ru.json)
# --------------------------------------------------------------------------

STATUS_CATEGORIES_RU: dict[str, str] = {
    "new": "To do",
    "indeterminate": "В работе",
    "done": "Готово",
    "cancelled": "Отменено",
    "unmapped": "Не сопоставлено",
}


def status_category_label_ru(category: str) -> str:
    return STATUS_CATEGORIES_RU.get(category or "unmapped", STATUS_CATEGORIES_RU["unmapped"])


# --------------------------------------------------------------------------
# E.3 — Glossary: 12 source entries verbatim + 7 additions, in this order.
# --------------------------------------------------------------------------

GLOSSARY_RU: list[dict] = [
    {"term": "PR cycle time",
     "definition_ru": "Сколько времени в среднем MR ждёт от открытия до merge. Чем меньше — тем быстрее команда "
                       "доводит изменения. Среднее чувствительно к «долгим» MR; медиана устойчивее."},
    {"term": "Cycle time (Jira)",
     "definition_ru": "Время от взятия задачи в работу до закрытия. Ведущий показатель скорости доставки."},
    {"term": "Throughput", "definition_ru": "Число завершённых задач за период. Объём результата команды."},
    {"term": "Rework",
     "definition_ru": "Возврат задачи в работу после того, как её уже посчитали сделанной. Много возвратов = "
                       "низкое качество с первого раза."},
    {"term": "Defect rate",
     "definition_ru": "Доля завершённых задач, закрытых как баг/ошибка. Выше — хуже качество поставки."},
    {"term": "Доля успешных пайплайнов",
     "definition_ru": "Сколько запусков CI завершились успешно. Падение при росте скорости — сигнал, что "
                       "скорость досталась ценой качества."},
    {"term": "Доля успешных деплоев",
     "definition_ru": "Сколько выкатов прошло без сбоя. Частота и стабильность деплоев — ключевые DORA-метрики."},
    {"term": "Покрытие тестами",
     "definition_ru": "Доля кода, покрытого автотестами. Чем выше — тем ниже риск регрессий."},
    {"term": "Story Points",
     "definition_ru": "Относительная оценка сложности задачи. Не переводится в часы напрямую: это мера объёма "
                       "усилий в масштабе команды."},
    {"term": "QA Estimation",
     "definition_ru": "Оценка трудозатрат на тестирование задачи (сколько усилий заложено на QA)."},
    {"term": "Вес MR",
     "definition_ru": "Сколько файлов затронул реквест (поле changes_count). Косвенно показывает объём "
                       "доработки: чем больше файлов — тем шире зона изменений."},
    {"term": "Baseline (P0)",
     "definition_ru": "Исходные показатели за первый замеряемый период, с которым сравниваются последующие. "
                       "Позволяет смотреть на динамику, а не на разовые значения."},
    {"term": "Velocity",
     "definition_ru": "Сколько Story Points команда фактически поставила за спринт. Основа планирования "
                       "следующих спринтов."},
    {"term": "Velocity SMA5",
     "definition_ru": "Скользящее среднее velocity по пяти предыдущим закрытым спринтам. Сглаживает разовые "
                       "всплески и провалы."},
    {"term": "Performance (Say/Do)",
     "definition_ru": "Отношение поставленного к обещанному на старте спринта, в SP. 100% — сделали ровно что "
                       "обещали; цель — от 80%."},
    {"term": "Загрузка",
     "definition_ru": "Обязательство спринта относительно средней скорости команды (SMA5). 80–100% — здоровая "
                       "загрузка; выше — перегруз, ниже — недогруз."},
    {"term": "Изменение объёма",
     "definition_ru": "Насколько менялся состав спринта после старта: добавленные и убранные задачи плюс "
                       "изменения оценок, относительно обязательства."},
    {"term": "Прогноз Monte-Carlo",
     "definition_ru": "Симуляция: тысячи случайных «проигрываний» будущего на основе прошлых дневных закрытий. "
                       "Даёт не одну дату, а вероятностные сроки (P50/P85/P95)."},
    {"term": "CV (коэффициент вариации)",
     "definition_ru": "Мера нестабильности потока: разброс недельных сумм закрытий относительно их среднего, "
                       "в процентах. Выше 50% — прогнозу доверять осторожно."},
]

# --------------------------------------------------------------------------
# E.6 — Risk texts (thresholds live in report_data.py; texts live here)
# --------------------------------------------------------------------------

RISK_TITLES_RU: dict[str, str] = {
    "speed_vs_quality": "Скорость против качества",
    "defect_rate": "Высокий defect rate",
    "rework": "Много возвратов в работу",
    "coverage": "Низкое покрытие тестами",
    "all_ok": "Всё в норме",
}

RISK_BODY_SPEED_VS_QUALITY_RU = (
    "Успешных пайплайнов менее 80% при быстром cycle time. Если темп растёт, а пайплайны чаще падают — это "
    "тревожный сигнал: скорость досталась ценой качества. Усильте ревью и тесты."
)
RISK_BODY_ALL_OK_RU = (
    "Явных тревожных сигналов по собранным данным нет. Продолжайте следить за парой «скорость ↔ качество» и "
    "сверяйте с собственным baseline."
)


def format_pct1(value: float) -> str:
    """1-decimal percent text with a trailing '.0' stripped (E.6: '55%', '63.4%')."""
    s = f"{value:.1f}"
    if s.endswith(".0"):
        s = s[:-2]
    return s


def risk_body_defect_rate_ru(defect_rate_pct: float) -> str:
    return (
        f"{format_pct1(defect_rate_pct)}% завершённых задач закрываются как баг/ошибка. "
        "Стоит больше тестировать и пересмотреть определение готовности."
    )


def risk_body_rework_ru(rework_rate_pct: float) -> str:
    return (
        f"Rework rate {format_pct1(rework_rate_pct)}% — заметная часть задач переделывается. "
        "Снизьте текучку: чётче ТЗ и раннее согласование."
    )


def risk_body_coverage_ru(coverage_avg_pct: float) -> str:
    return f"Покрытие {format_pct1(coverage_avg_pct)}% — ниже 60%. Высокий риск регрессий при быстрых изменениях."


# --------------------------------------------------------------------------
# E.4 — WARN_ERR_RU: warning/error code dictionary
# --------------------------------------------------------------------------

WARN_ERR_RU: dict[str, str] = {
    "ERR_UNKNOWN": "Неизвестная ошибка.",
    "ERR_JIRA_UNREACHABLE": "Jira недоступна (сеть, таймаут или 5xx).",
    "ERR_JIRA_AUTH_FAILED": "Jira отклонила токен (401/403). Проверьте токен подключения.",
    "ERR_SPRINT_NOT_FOUND": "Спринт не найден.",
    "ERR_SPRINTS_DIFFERENT_BOARDS": "Выбранные спринты принадлежат разным бордам.",
    "ERR_FORECAST_NOT_ENOUGH_DATA": "Недостаточно данных для прогноза (менее 10 дневных точек).",
    "ERR_FORECAST_NO_ACTIVE_SPRINT": "Нет активного спринта, чтобы определить целевое число задач — "
                                     "задайте --target-items явно.",
    "WARN_THROUGHPUT_UNSTABLE": "Поток нестабилен — относитесь к перцентилям с осторожностью.",
    "WARN_SPRINT_ACTIVE_PARTIAL": "Включён активный спринт — его метрики промежуточные.",
    "WARN_STATUS_UNMAPPED": "Встречен несопоставленный статус — категория взята из statusCategory Jira.",
    "WARN_BASELINE_SHORT": "Закрытых спринтов для базы меньше 5 — SMA5/загрузка считаются по имеющимся.",
    "WARN_DIVISION_BY_ZERO": "Знаменатель формулы равен нулю — метрика обнулена.",
    "WARN_DIFF_STATS_UNAVAILABLE": "GitLab не отдал размер диффа ни по одному MR — средний размер диффа "
                                   "показан как «нет данных».",
    "WARN_COMMITS_UNAVAILABLE": "GitLab не отдал число коммитов ни по одному MR — метрики коммитов показаны "
                                "как «нет данных».",
    "WARN_CHANGES_COUNT_UNAVAILABLE": "GitLab не отдал число изменённых файлов ни по одному MR — вес MR "
                                      "показан как «нет данных».",
    "WARN_MR_CYCLE_TIME_UNAVAILABLE": "Ни у одного MR нет вычислимого времени цикла — PR cycle time показан "
                                      "как «нет данных».",
    "WARN_TASK_CYCLE_TIME_UNAVAILABLE": "Ни у одной задачи нет вычислимого времени цикла (нет перехода "
                                        "«В работе» → финальный статус) — cycle time задач показан как «нет данных».",
    "WARN_PIPELINE_SUCCESS_UNAVAILABLE": "Атрибуция пайплайнов пользователям не собиралась "
                                         "(--no-pipeline-users/--light) — личная успешность CI показана как «нет данных».",
    "WARN_PER_WEEK_UNAVAILABLE": "Недостаточно дат, чтобы посчитать частоту в неделю — показано «нет данных».",
    "WARN_COVERAGE_UNAVAILABLE": "GitLab не вернул значение покрытия ни по одному пайплайну — покрытие "
                                 "показано как «нет данных».",
    "WARN_OUTSIDE_SPRINTS": "Часть записей завершена вне всех анализируемых спринтов и не попала в разрезы по спринтам.",
    "ERR_GITLAB_UNREACHABLE": "GitLab недоступен (сеть, таймаут или 5xx).",
    "AUTH_FAILED": "GitLab отклонил токен (401/403). Проверьте токен.",
    "NOT_FOUND": "Проект не найден в GitLab — пропущен целиком.",
    "FILTER_REJECTED_FALLBACK": "GitLab отклонил фильтр по дате — деплои получены без фильтра и отобраны на "
                                "стороне инструмента; числа корректны, запросов ушло больше.",
    "PAGINATION_LIMIT": "Список деплоев обрезан по лимиту страниц или времени — числа деплоев и их успешность "
                        "занижены, не читайте их как полные.",
    "MR_FETCH_ERROR": "Не удалось получить MR этого автора в этом проекте — его данные по MR неполные.",
}


def warn_message(code: str) -> str:
    """Russian message for a warning/error code, or the code itself prefixed
    with 'Предупреждение: ' when unmapped — never raises (E.4)."""
    mapped = WARN_ERR_RU.get(code)
    if mapped is not None:
        return mapped
    return f"Предупреждение: {code}"


def warning_obj(code: str, detail: Optional[str] = None) -> dict:
    """One warning/error object `{code, message_ru, detail}` (§B.4)."""
    return {"code": code, "message_ru": warn_message(code), "detail": detail}


def warning_obj_from_suffixed(code_with_suffix: str) -> dict:
    """Parses a personal_metrics.py/engineering_metrics.py-style bare warning
    string. Two shapes: a plain code (`WARN_DIFF_STATS_UNAVAILABLE`), or a
    code with a `:metric_name` suffix (`WARN_DIVISION_BY_ZERO:mr_merge_rate_pct`)
    added by their own `_pct_or_zero` helper — the suffix becomes `detail` as
    that metric's Russian label."""
    code, sep, metric_name = code_with_suffix.partition(":")
    detail = metric_label_ru(metric_name) if sep else None
    return warning_obj(code, detail)
