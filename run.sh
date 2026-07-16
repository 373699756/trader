#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-$ROOT_DIR/.venv}"
CONFIG_PATH="${TRADER_CONFIG:-$ROOT_DIR/config/v2/runtime.json}"
MODE="${1:-serve}"

usage() {
  printf '%s\n' \
    "用法: ./run.sh [serve|validate-config]" \
    "" \
    "环境变量:" \
    "  TRADER_CONFIG=/absolute/path/runtime.json" \
    "  TRADER_HOST=127.0.0.1" \
    "  TRADER_PORT=5000" \
    "  DEEPSEEK_API_KEY=..." \
    "  FORCE_INSTALL_DEPS=1"
}

if [[ "$MODE" == "-h" || "$MODE" == "--help" ]]; then
  usage
  exit 0
fi

find_python() {
  local candidate
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(not ((3, 10) <= sys.version_info[:2] < (3, 15)))'; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  PYTHON_BIN="$(find_python || true)"
  if [[ -z "$PYTHON_BIN" ]]; then
    printf '需要 Python 3.10-3.14。\n' >&2
    exit 1
  fi
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

if [[ ! -x "$VENV_DIR/bin/trader-server" || "$ROOT_DIR/pyproject.toml" -nt "$VENV_DIR/bin/trader-server" || "${FORCE_INSTALL_DEPS:-0}" == "1" ]]; then
  "$VENV_DIR/bin/python" -m pip install --disable-pip-version-check -e "$ROOT_DIR"
fi

export TRADER_HOST="${TRADER_HOST:-${HOST:-127.0.0.1}}"
export TRADER_PORT="${TRADER_PORT:-${PORT:-5000}}"

case "$MODE" in
  serve|app)
    exec "$VENV_DIR/bin/trader-server" --config "$CONFIG_PATH"
    ;;
  validate-config)
    exec "$VENV_DIR/bin/trader-cli" --config "$CONFIG_PATH" validate-config
    ;;
  *)
    printf '未知模式: %s\n\n' "$MODE" >&2
    usage >&2
    exit 2
    ;;
esac
