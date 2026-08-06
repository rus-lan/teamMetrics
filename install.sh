#!/usr/bin/env sh
# Установщик jira-metrics-report.
#
# Работает в двух режимах, определяемых автоматически по тому, как скрипт
# запущен:
#
#   ЛОКАЛЬНЫЙ — запущен как ./install.sh (или sh install.sh) внутри клона
#               репозитория: ставит прямо из текущего каталога, без сети.
#   УДАЛЁННЫЙ — запущен через `curl ... | sh` без репозитория рядом:
#               скачивает архив последнего релиза с GitHub, проверяет его
#               sha256 и ставит из него.
#
# Usage:
#   curl -fsSL https://github.com/rus-lan/teamMetrics/releases/latest/download/install.sh | sh
#   ИЛИ, внутри клона репозитория team-metrics:
#   ./install.sh
#
# Оба режима кладут скилл в ~/.claude/skills/jira-metrics-report/ (общий
# каталог, который читают и Claude Code, и opencode) и команду jira-metrics
# в ~/.local/bin/.
#
# Переменные окружения (действуют только в удалённом режиме):
#   INSTALL_DIR                Каталог для команды jira-metrics
#                               (по умолчанию: $HOME/.local/bin)
#   TEAMMETRICS_VERSION         Поставить конкретную версию вместо latest,
#                               например: TEAMMETRICS_VERSION=1.0.0 sh install.sh
#   TEAMMETRICS_SKIP_CHECKSUM   1, чтобы пропустить проверку sha256, если её
#                               в принципе нельзя выполнить (нет sha256sum/
#                               shasum, не скачался checksums-sha256.txt).
#                               Настоящее несовпадение checksum всегда
#                               фатально, этот флаг его не обходит.
#
# Флаги: --uninstall   удалить всё, что поставил этот скрипт
#        -h, --help    показать эту справку
#
# Написан на POSIX sh (без bash-измов) -- скрипт пайпится в `sh` при
# удалённой установке и должен работать под sh/dash/bash/zsh одинаково.

set -eu

SKILL_NAME="jira-metrics-report"
OWNER="rus-lan"
REPO="teamMetrics"
ASSET_TARBALL="jira-metrics-report.tar.gz"
ASSET_CHECKSUMS="checksums-sha256.txt"

SKILL_DEST="$HOME/.claude/skills/$SKILL_NAME"
BIN_DIR="${INSTALL_DIR:-$HOME/.local/bin}"
LAUNCHER="$BIN_DIR/jira-metrics"
LAUNCHER_MARKER="# jira-metrics-report launcher -- installed by install.sh, safe to remove"

# Обязательные файлы -- один и тот же список для локального клона и для
# распакованного архива релиза (см. scripts/release.sh, BUNDLE_ITEMS).
BUNDLE_ITEMS="SKILL.md README.md scripts templates .jira-metrics.example.json VERSION"

err() { printf 'ошибка: %s\n' "$1" >&2; }
info() { printf '%s\n' "$1"; }

usage() {
  cat <<'EOF'
Использование:
  curl -fsSL https://github.com/rus-lan/teamMetrics/releases/latest/download/install.sh | sh
  ./install.sh                    (внутри клона репозитория -- ставит из текущего каталога)
  ./install.sh --uninstall
  ./install.sh -h|--help

Без флагов -- устанавливает или обновляет skill jira-metrics-report:
  - копирует его в ~/.claude/skills/jira-metrics-report/
  - кладёт команду jira-metrics в ~/.local/bin/ (или $INSTALL_DIR)

  --uninstall   удалить skill и команду jira-metrics, поставленные этим скриптом
  -h, --help    показать эту справку
EOF
}

check_python() {
  if ! command -v python3 >/dev/null 2>&1; then
    err "python3 не найден."
    echo "Установите Python 3.9 или новее и запустите install.sh ещё раз:" >&2
    echo "  Ubuntu/Debian:  sudo apt install python3" >&2
    echo "  Fedora/RHEL:    sudo dnf install python3" >&2
    echo "  macOS:          brew install python3   (или скачайте с https://www.python.org/downloads/)" >&2
    exit 1
  fi

  if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
    ver="$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"
    err "нужен python3 >= 3.9, найден $ver."
    echo "Обновите Python и запустите install.sh ещё раз." >&2
    exit 1
  fi
}

# Определяет, откуда ставить, если это ЛОКАЛЬНЫЙ режим: печатает каталог
# репозитория в stdout и возвращает 0. Возвращает 1 (без вывода), если это
# УДАЛЁННЫЙ режим -- в частности, `curl ... | sh` устанавливает $0 в "sh"
# (имя интерпретатора, а не путь к файлу), так что `[ -f "$0" ]` там всегда
# ложно.
detect_source_dir() {
  if [ -f "$0" ]; then
    candidate="$(cd "$(dirname "$0")" && pwd)"
    if [ -f "$candidate/SKILL.md" ] && [ -d "$candidate/scripts/jira_metrics" ]; then
      printf '%s' "$candidate"
      return 0
    fi
  fi
  return 1
}

# Проверяет, что все обязательные файлы есть в $1 (каталог-источник -- либо
# корень клона репозитория, либо распакованный архив релиза). $2 -- строка
# подсказки, печатается в конце сообщения об ошибке.
check_source() {
  src="$1"
  hint="$2"
  for item in $BUNDLE_ITEMS; do
    if [ ! -e "$src/$item" ]; then
      err "не найден $src/$item"
      [ -n "$hint" ] && echo "$hint" >&2
      exit 1
    fi
  done
}

# Копирует BUNDLE_ITEMS из $1 в SKILL_DEST. demo/ копируется отдельно и не
# фатально при отсутствии: локальный клон репозитория его содержит, а
# минимальный архив релиза, который скачивает удалённая установка, -- нет
# (демо не нужно инструменту в рантайме, это просто пример для человека).
install_skill() {
  src="$1"
  was_installed=0
  [ -d "$SKILL_DEST" ] && was_installed=1

  mkdir -p "$(dirname "$SKILL_DEST")"
  rm -rf "$SKILL_DEST"
  mkdir -p "$SKILL_DEST"
  for item in $BUNDLE_ITEMS; do
    cp -R "$src/$item" "$SKILL_DEST/$item"
  done
  if [ -d "$src/demo" ]; then
    cp -R "$src/demo" "$SKILL_DEST/demo"
  fi
  find "$SKILL_DEST" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
  find "$SKILL_DEST" -type f -name '*.py[co]' -delete 2>/dev/null || true
  # scripts/release.sh is a build-time tool, not something the skill needs
  # at runtime -- drop it if present (local installs from a checkout have it).
  rm -f "$SKILL_DEST/scripts/release.sh"
  chmod +x "$SKILL_DEST/scripts/jira-metrics"

  if [ "$was_installed" = "1" ]; then
    info "skill обновлён в $SKILL_DEST"
  else
    info "skill установлен в $SKILL_DEST"
  fi
}

install_launcher() {
  mkdir -p "$BIN_DIR"
  cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
$LAUNCHER_MARKER
export JIRA_METRICS_BIN="jira-metrics"
exec python3 "$SKILL_DEST/scripts/jira-metrics" "\$@"
EOF
  chmod +x "$LAUNCHER"
  info "команда jira-metrics установлена в $LAUNCHER"
}

check_path() {
  case ":$PATH:" in
    *":$BIN_DIR:"*) return 0 ;;
  esac

  shell_name="$(basename "${SHELL:-}")"
  case "$shell_name" in
    zsh) rc_file="$HOME/.zshrc" ;;
    bash) rc_file="$HOME/.bashrc" ;;
    *) rc_file="" ;;
  esac

  echo ""
  echo "$BIN_DIR ещё не в PATH -- команда jira-metrics сама не найдётся."
  if [ -n "$rc_file" ]; then
    echo "Добавьте эту строку в $rc_file, затем откройте новый терминал (или выполните: . $rc_file):"
  else
    echo "Добавьте эту строку в rc-файл вашей оболочки (~/.bashrc, ~/.zshrc и т.п.), затем откройте новый терминал:"
  fi
  echo "  export PATH=\"$BIN_DIR:\$PATH\""
}

# --- удалённый режим: скачать релиз с GitHub, проверить checksum, поставить ---

# Проверяет $1 (скачанный файл) против sha256 из checksums-sha256.txt,
# который лежит рядом с ним по адресу $3/checksums-sha256.txt. $2 -- имя
# файла, под которым он должен встречаться в checksums-sha256.txt.
# Несовпадение checksum всегда фатально. Три случая, когда проверку в
# принципе нельзя выполнить (нет sha256sum/shasum, не скачался
# checksums-sha256.txt, в нём нет строки для этого файла), можно понизить
# до предупреждения через TEAMMETRICS_SKIP_CHECKSUM=1.
verify_checksum() {
  file="$1"
  asset="$2"
  base_url="$3"

  sha_cmd=""
  if command -v sha256sum >/dev/null 2>&1; then
    sha_cmd="sha256sum"
  elif command -v shasum >/dev/null 2>&1; then
    sha_cmd="shasum -a 256"
  fi

  if [ -z "$sha_cmd" ]; then
    if [ "${TEAMMETRICS_SKIP_CHECKSUM:-}" = "1" ]; then
      echo "jira-metrics: ПРЕДУПРЕЖДЕНИЕ: нет ни sha256sum, ни shasum -- пропускаю проверку целостности." >&2
      return 0
    fi
    err "нет ни sha256sum, ни shasum -- не могу проверить скачанный файл."
    echo "  установите TEAMMETRICS_SKIP_CHECKSUM=1, чтобы поставить без проверки." >&2
    exit 1
  fi

  checksums_url="${base_url}/${ASSET_CHECKSUMS}"
  tmp_checksums=$(mktemp "${TMPDIR:-/tmp}/jira-metrics-sha.XXXXXX")
  if ! curl -fsSL -o "$tmp_checksums" "$checksums_url"; then
    rm -f "$tmp_checksums"
    if [ "${TEAMMETRICS_SKIP_CHECKSUM:-}" = "1" ]; then
      echo "jira-metrics: ПРЕДУПРЕЖДЕНИЕ: не удалось скачать $ASSET_CHECKSUMS -- пропускаю проверку целостности." >&2
      return 0
    fi
    err "не удалось скачать $ASSET_CHECKSUMS -- не могу проверить скачанный файл."
    echo "  установите TEAMMETRICS_SKIP_CHECKSUM=1, чтобы поставить без проверки." >&2
    exit 1
  fi

  expected=$(grep -E "  ${asset}\$" "$tmp_checksums" | awk '{print $1}' | head -1)
  rm -f "$tmp_checksums"
  if [ -z "$expected" ]; then
    if [ "${TEAMMETRICS_SKIP_CHECKSUM:-}" = "1" ]; then
      echo "jira-metrics: ПРЕДУПРЕЖДЕНИЕ: нет записи checksum для '$asset' -- пропускаю проверку целостности." >&2
      return 0
    fi
    err "нет записи checksum для '$asset' в $ASSET_CHECKSUMS -- не могу проверить скачанный файл."
    echo "  установите TEAMMETRICS_SKIP_CHECKSUM=1, чтобы поставить без проверки." >&2
    exit 1
  fi

  actual=$($sha_cmd "$file" | awk '{print $1}')
  if [ "$expected" != "$actual" ]; then
    err "несовпадение checksum для $asset -- установка прервана."
    echo "  ожидалось: $expected" >&2
    echo "  получено:  $actual" >&2
    exit 1
  fi
  info "checksum проверен ($asset)."
}

# "latest" резолвится в один конкретный тег ОДНИМ пробным запросом редиректа
# (без обращений к GitHub REST API -- никакого rate limit), а дальше архив и
# его checksum скачиваются из этого же releases/download/<tag>/ пути. Так
# архив и его checksum никогда не могут прийти из двух разных релизов.
run_remote_install() {
  info "jira-metrics: удалённая установка -- ищу релиз на GitHub..."

  if [ -n "${TEAMMETRICS_VERSION:-}" ]; then
    resolved_tag="v${TEAMMETRICS_VERSION#v}"
    info "jira-metrics: ставлю зафиксированную версию ${resolved_tag}..."
  else
    probe_url="https://github.com/${OWNER}/${REPO}/releases/latest/download/${ASSET_CHECKSUMS}"
    if ! redirect_url=$(curl -fsS --no-location -o /dev/null -w '%{redirect_url}' "$probe_url"); then
      err "не удалось найти последний релиз на GitHub (сеть, rate limit или ошибка HTTP -- см. сообщение curl выше)."
      echo "  запрошено: $probe_url" >&2
      exit 1
    fi
    if [ -z "$redirect_url" ]; then
      err "GitHub не сделал редирект при поиске последнего релиза -- возможно, релизов ещё нет."
      echo "  запрошено: $probe_url" >&2
      exit 1
    fi
    resolved_tag=$(printf '%s' "$redirect_url" | sed -n 's#.*/releases/download/\(v[^/]*\)/.*#\1#p')
    if [ -z "$resolved_tag" ]; then
      err "не удалось разобрать тег релиза из редиректа GitHub."
      echo "  адрес редиректа: $redirect_url" >&2
      exit 1
    fi
    info "jira-metrics: последний релиз -- ${resolved_tag}."
  fi

  base_url="https://github.com/${OWNER}/${REPO}/releases/download/${resolved_tag}"

  work_dir=$(mktemp -d "${TMPDIR:-/tmp}/jira-metrics-install.XXXXXX")
  cleanup() { rm -rf "$work_dir" 2>/dev/null; }
  trap cleanup EXIT
  trap 'cleanup; exit 129' HUP
  trap 'cleanup; exit 130' INT
  trap 'cleanup; exit 131' QUIT
  trap 'cleanup; exit 143' TERM

  download_url="${base_url}/${ASSET_TARBALL}"
  tmp_file=$(mktemp "$work_dir/.dl.XXXXXX")
  info "jira-metrics: скачиваю ${ASSET_TARBALL}..."
  if ! curl -fsSL -o "$tmp_file" "$download_url"; then
    err "не удалось скачать $ASSET_TARBALL с $download_url"
    if [ -n "${TEAMMETRICS_VERSION:-}" ]; then
      echo "  проверьте, что релиз ${resolved_tag} существует и содержит этот файл:" >&2
      echo "  https://github.com/${OWNER}/${REPO}/releases/tag/${resolved_tag}" >&2
    else
      echo "  проверьте подключение к сети или список релизов:" >&2
      echo "  https://github.com/${OWNER}/${REPO}/releases/latest" >&2
    fi
    exit 1
  fi

  verify_checksum "$tmp_file" "$ASSET_TARBALL" "$base_url"

  tarball="$work_dir/$ASSET_TARBALL"
  mv "$tmp_file" "$tarball"

  extract_dir="$work_dir/extracted"
  mkdir -p "$extract_dir"
  tar xzf "$tarball" -C "$extract_dir"

  src_dir="$extract_dir/$SKILL_NAME"
  if [ ! -d "$src_dir" ]; then
    err "архив релиза распакован, но каталог $SKILL_NAME внутри него не найден -- повреждённый или несовместимый релиз."
    exit 1
  fi

  check_source "$src_dir" "Архив релиза повреждён или несовместим -- попробуйте переустановить."
  install_skill "$src_dir"
  install_launcher
}

do_uninstall() {
  if [ -d "$SKILL_DEST" ]; then
    rm -rf "$SKILL_DEST"
    info "удалён $SKILL_DEST"
  else
    info "$SKILL_DEST не найден, пропускаю"
  fi

  if [ -f "$LAUNCHER" ] && grep -qF "$LAUNCHER_MARKER" "$LAUNCHER" 2>/dev/null; then
    rm -f "$LAUNCHER"
    info "удалён $LAUNCHER"
  elif [ -e "$LAUNCHER" ]; then
    info "$LAUNCHER существует, но не похож на файл, созданный install.sh -- оставляю как есть"
  else
    info "$LAUNCHER не найден, пропускаю"
  fi
  exit 0
}

finish() {
  echo ""
  echo "Готово."
  check_path

  echo ""
  echo "Дальше выполните по порядку:"
  echo "  1. jira-metrics init"
  echo "  2. export JIRA_BASE_URL=\"https://jira.example.com\" JIRA_TOKEN=\"<ваш Jira PAT>\""
  echo "  3. jira-metrics check --sprint-names \"Sprint 42\""
  echo ""
  echo "Или сразу проверьте окружение одной командой:"
  echo "  jira-metrics doctor"
}

main() {
  case "${1:-}" in
    --uninstall) do_uninstall ;;
    -h|--help) usage; exit 0 ;;
    "") ;;
    *)
      err "неизвестный флаг: $1"
      usage
      exit 2
      ;;
  esac

  check_python

  if source_dir=$(detect_source_dir); then
    info "jira-metrics: локальная установка из $source_dir"
    check_source "$source_dir" "Запускайте install.sh из корня репозитория team-metrics."
    install_skill "$source_dir"
    install_launcher
  else
    run_remote_install
  fi

  finish
}

main "$@"
