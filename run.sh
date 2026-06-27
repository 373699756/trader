#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-$ROOT_DIR/.venv}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-5000}"

cd "$ROOT_DIR"

find_python() {
  if command -v python3.11 >/dev/null 2>&1; then
    command -v python3.11
    return
  fi
  if [ -x /home/c/.local/bin/python3.11 ]; then
    printf '%s\n' /home/c/.local/bin/python3.11
    return
  fi
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return
  fi
  return 1
}

PYTHON_BIN="$(find_python || true)"
if [ -z "$PYTHON_BIN" ]; then
  printf '未找到 Python。请安装 Python 3.9+，推荐 Python 3.11。\n' >&2
  exit 1
fi

"$PYTHON_BIN" - <<'PY'
import sys

if sys.version_info < (3, 9):
    raise SystemExit("当前 Python 版本过低，需要 Python 3.9+，推荐 Python 3.11。")
PY

if [ ! -x "$VENV_DIR/bin/python" ]; then
  printf '创建虚拟环境: %s\n' "$VENV_DIR"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

printf '安装/更新依赖...\n'
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install -r requirements.txt

printf '\n启动看板: http://%s:%s\n' "$HOST" "$PORT"
printf '历史因子: ENABLE_HISTORY_FACTORS=%s（可显式设为 0 关闭）\n' "${ENABLE_HISTORY_FACTORS:-1}"
printf '按 Ctrl+C 停止。\n\n'

export FLASK_RUN_HOST="$HOST"
export FLASK_RUN_PORT="$PORT"
export ENABLE_HISTORY_FACTORS="${ENABLE_HISTORY_FACTORS:-1}"
exec "$VENV_DIR/bin/python" app.py
