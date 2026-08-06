# Changelog

Все заметные изменения проекта фиксируются в этом файле. Формат основан на
[Keep a Changelog](https://keepachangelog.com/ru/1.0.0/).

## [2.1.0] - 2026-08-07

### Добавлено

- Новый пункт в `check` — версия и тип развёртывания Jira («версия Jira»). Печатает, к какой
  именно Jira подключился инструмент (например, `Jira Server 9.12.28`), вместо того чтобы дать
  несовместимости обнаружиться позже, посреди `run`. Предупреждает (`[ПРЕДУПРЕЖДЕНИЕ]`, никогда
  `[ОШИБКА]`):
  - если это Jira Cloud — инструмент рассчитан на Server/Data Center, а у Cloud другая схема
    авторизации (email + API-токен через Basic) и частично другой API;
  - если версия ниже 8.14 — именно в этой версии появились Personal Access Token с заголовком
    `Authorization: Bearer`, которым авторизуется инструмент, — ниже авторизация работать не будет;
  - если версию определить не удалось (например, эндпоинт закрыт на конкретном инстансе) — сам по
    себе этот факт не повод блокировать иначе рабочий запуск.

## [2.0.0] - 2026-08-06

### Изменено

- **Переименование проекта: `jira-metrics-report` → `team-metrics`**, вслед за именем репозитория
  (`rus-lan/teamMetrics`):
  - Python-пакет `scripts/jira_metrics/` → `scripts/team_metrics/` (и `python3 -m jira_metrics` →
    `python3 -m team_metrics`).
  - Исполняемый скрипт `scripts/jira-metrics` → `scripts/team-metrics`.
  - Skill/каталог установки `jira-metrics-report` → `team-metrics`, теперь ставится в
    `~/.claude/skills/team-metrics/`.
  - Конфиг-файл `.jira-metrics.json` → `.team-metrics.json` (пример — `.jira-metrics.example.json`
    → `.team-metrics.example.json`).
  - Переменная окружения `JIRA_METRICS_BIN` → `TEAM_METRICS_BIN`.
  - Архив релиза `jira-metrics-report.tar.gz` → `team-metrics.tar.gz`.
  - `JIRA_BASE_URL`/`JIRA_TOKEN`/`GITLAB_URL`/`GITLAB_TOKEN` **не переименованы** — они называют
    внешние системы (Jira, GitLab), а не сам инструмент.
- **Миграция для тех, кто уже ставил 1.x**: `install.sh` при каждой установке сам находит и удаляет
  старый skill (`~/.claude/skills/jira-metrics-report/`) и старую команду `jira-metrics` в
  `INSTALL_DIR` — но только если это наш собственный лаунчер (проверяется по тому же маркеру, что и
  `--uninstall`), никогда не трогает чужой файл. `team-metrics init`/`team-metrics doctor`
  предупреждают, если в рабочей папке остался `.jira-metrics.json` без `.team-metrics.json` рядом, и
  подсказывают команду для переименования — не читают и не переименовывают его автоматически.
- Бандловый `.team-metrics.example.json` сокращён до двух ключей, которые реально нужно задать
  почти всегда — `gitlab.projects` и `employees`. `status_map`, `cancelled_statuses`,
  `story_points_field_id`, `history_sprint_count`, `jira.final_statuses` по-прежнему
  поддерживаются конфиг-загрузчиком с теми же значениями по умолчанию — задавайте их вручную, если
  понадобятся (см. README.md, раздел «Конфигурационный файл»).

### Добавлено

По итогам независимого аудита безопасности/надёжности:

- Новые флаги `--no-mr-details` и `--no-pipeline-users` (и их комбинация одним флагом `--light`) у
  `run` — отключают два самых тяжёлых обхода GitLab (детали+коммиты каждого MR, пользователь
  каждого pipeline) по отдельности или разом, экономя до ~1200 запросов из ~1660 в среднем `run`.
  `--no-mr-details`/`--no-pipeline-users` доступны и у `report_data.py` напрямую.
- Новый флаг `--no-proxy` у `check`/`run` — игнорирует `HTTP_PROXY`/`HTTPS_PROXY` из окружения для
  запросов и к Jira, и к GitLab.
- `run`/`report_data.py` считает и печатает число фактических HTTP-запросов к GitLab
  (`gitlab_request_count`) в конце `run`.

### Исправлено

По итогам того же аудита:

- GitLab-клиент уважает заголовок `Retry-After` от сервера при 429/5xx (с потолком
  `max_retry_after`) вместо фиксированной экспоненциальной задержки между ретраями.
- `install.sh` отказывается перезаписать чужой файл на месте команды `team-metrics`
  (бывш. `jira-metrics`) без явного `--force`, чтобы случайно не стереть что-то не своё.
- Форматирование дат в отчёте починено под Python 3.9 (нуль в начале года).

## [1.0.0] - 2026-08-06

Первый публичный релиз. Инструмент готов к использованию: 517 тестов,
без сторонних зависимостей — чистый stdlib Python 3.9+.

### Добавлено

- Единая командная поверхность `jira-metrics {init|check|run|report|doctor}` —
  без `pip install`, работает как исполняемый скрипт из репозитория или как
  установленная команда после `install.sh`.
- Вкладка «Команда»: KPI спринта (committed/delivered/scope-change по story
  points и по числу задач), velocity и SMA5, load %, closure %, бёрндаун,
  хитмап статусов, Monte-Carlo-прогноз по спринту.
- Вкладка «Персональные»: метрики каждого разработчика по GitLab (доля
  смёрженных merge request'ов, cycle time, размер диффа, rework) и по Jira
  (story points, доля дефектов, разбивка по спринтам).
- Вкладка «Инженерия»: командные показатели по пайплайнам, деплоям и
  покрытию тестами из GitLab.
- Самодостаточный HTML-отчёт — открывается в любом браузере, сеть после
  генерации не нужна.
- `jira-metrics check` — проверка подключения к Jira/GitLab и конфигурации
  без построения отчёта.
- `jira-metrics doctor` — диагностика окружения и установки skill, без
  сети и без токенов.
- `jira-metrics --version` — печатает установленную версию.
- Установка одной командой: `curl -fsSL
  https://github.com/rus-lan/teamMetrics/releases/latest/download/install.sh | sh`
  — скачивает архив последнего релиза, проверяет его sha256 и ставит.
  Тот же `install.sh`, запущенный внутри клона репозитория, ставит локально
  без сети.
