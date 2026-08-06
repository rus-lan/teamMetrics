#!/usr/bin/env bash
# Установщик jira-metrics-report.
#
# Копирует этот репозиторий в ~/.claude/skills/jira-metrics-report/ (оттуда
# его читают и Claude Code, и opencode — оба ищут скиллы в одном каталоге) и
# кладёт команду jira-metrics в ~/.local/bin/, чтобы её можно было вызывать
# из любого каталога без пути до скрипта.
#
# Работает на Linux и macOS. Безопасно перезапускать — повторный запуск
# просто обновляет уже установленную копию. Для удаления: ./install.sh --uninstall

set -euo pipefail

SKILL_NAME="jira-metrics-report"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
SKILL_DEST="$HOME/.claude/skills/$SKILL_NAME"
BIN_DIR="$HOME/.local/bin"
LAUNCHER="$BIN_DIR/jira-metrics"
LAUNCHER_MARKER="# jira-metrics-report launcher -- installed by install.sh, safe to remove"

BUNDLE_ITEMS="SKILL.md README.md scripts templates .jira-metrics.example.json demo"

err() { printf 'ошибка: %s\n' "$1" >&2; }
info() { printf '%s\n' "$1"; }

usage() {
  cat <<'EOF'
Использование: ./install.sh [--uninstall] [-h|--help]

Без флагов — устанавливает или обновляет skill jira-metrics-report:
  - копирует его в ~/.claude/skills/jira-metrics-report/
  - кладёт команду jira-metrics в ~/.local/bin/

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

check_source() {
  for item in $BUNDLE_ITEMS; do
    if [ ! -e "$SOURCE_DIR/$item" ]; then
      err "не найден $SOURCE_DIR/$item"
      echo "Запускайте install.sh из корня репозитория team-metrics." >&2
      exit 1
    fi
  done
}

install_skill() {
  local was_installed=0
  [ -d "$SKILL_DEST" ] && was_installed=1

  mkdir -p "$(dirname "$SKILL_DEST")"
  rm -rf "$SKILL_DEST"
  mkdir -p "$SKILL_DEST"
  for item in $BUNDLE_ITEMS; do
    cp -R "$SOURCE_DIR/$item" "$SKILL_DEST/$item"
  done
  find "$SKILL_DEST" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
  find "$SKILL_DEST" -type f -name '*.py[co]' -delete 2>/dev/null || true
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

  local shell_name rc_file
  shell_name="$(basename "${SHELL:-}")"
  case "$shell_name" in
    zsh) rc_file="$HOME/.zshrc" ;;
    bash) rc_file="$HOME/.bashrc" ;;
    *) rc_file="" ;;
  esac

  echo ""
  echo "$BIN_DIR ещё не в PATH — команда jira-metrics сама не найдётся."
  if [ -n "$rc_file" ]; then
    echo "Добавьте эту строку в $rc_file, затем откройте новый терминал (или выполните: source $rc_file):"
  else
    echo "Добавьте эту строку в rc-файл вашей оболочки (~/.bashrc, ~/.zshrc и т.п.), затем откройте новый терминал:"
  fi
  echo "  export PATH=\"$BIN_DIR:\$PATH\""
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
    info "$LAUNCHER существует, но не похож на файл, созданный install.sh — оставляю как есть"
  else
    info "$LAUNCHER не найден, пропускаю"
  fi
  exit 0
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
  check_source
  install_skill
  install_launcher

  echo ""
  echo "Готово."
  check_path

  echo ""
  echo "Дальше выполните по порядку:"
  echo "  1. jira-metrics init"
  echo "  2. export JIRA_BASE_URL=\"https://jira.example.com\" JIRA_TOKEN=\"<ваш Jira PAT>\""
  echo "  3. jira-metrics check --sprint-names \"Sprint 42\""
}

main "$@"
