#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-$ROOT_DIR/.venv}"
CONFIG_PATH="${TRADER_CONFIG:-$ROOT_DIR/config/v2/runtime.json}"
MODE="${1:-serve}"

usage() {
  printf '%s\n' \
    "本地 A 股研究看板" \
    "" \
    "日常使用（不做离线研究）:" \
    "  ./run.sh                         启动本地 A 股研究看板（推荐）" \
    "  ./run.sh serve                   显式启动，等同于无参数运行" \
    "  ./run.sh validate-config         校验配置后退出，不启动服务" \
    "  ./run.sh research-status         只读查看研究数据准备状态" \
    "  ./run.sh performance-check       离线运行活动生产函数性能门禁" \
    "  ./run.sh help                    查看本帮助" \
    "" \
    "离线研究（仅在明确执行研究任务时使用）:" \
    "  ./run.sh research-history-download        下载并续传离线历史日线归档" \
    "  ./run.sh research-backtest                只读运行固定历史回测" \
    "  ./run.sh research-r6-screen                运行并封存 R6 历史筛选" \
    "  ./run.sh research-r6-daily-screen          运行并封存 R6 日线趋势筛选" \
    "  ./run.sh research-r6-stability-screen      运行并封存 R6 稳定性诊断" \
    "  ./run.sh research-r7-dossier --research-identity <ID>" \
    "                                                生成待人工审查的 R7 档案" \
    "" \
    "日常启动不需要填写任何参数，直接运行 ./run.sh 即可。" \
    "" \
    "高级配置（一般无需设置）:" \
    "  TRADER_CONFIG=/absolute/path/runtime.json" \
    "  TRADER_HOST=127.0.0.1" \
    "  TRADER_PORT=5000" \
    "  DEEPSEEK_API_KEY=..." \
    "  DEEPSEEK_API_KEY_FILE=/protected/path（默认读取项目根目录 .token_key）" \
    "  TUSHARE_TOKEN_FILE=/protected/path（默认读取项目根目录 .token_key）" \
    "  FORCE_INSTALL_DEPS=1"
}

case "$MODE" in
  help|-h|--help)
    usage
    exit 0
    ;;
  serve|app)
    COMMAND_KIND="server"
    ;;
  validate-config|performance-check|research-status|research-history-download|research-backtest|research-r6-screen|research-r6-daily-screen|research-r6-stability-screen|research-r7-dossier)
    COMMAND_KIND="cli"
    ;;
  *)
    printf '未知命令: %s\n' "$MODE" >&2
    printf '日常启动直接运行: ./run.sh\n' >&2
    printf '查看全部命令: ./run.sh help\n' >&2
    exit 2
    ;;
esac

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

export TRADER_HOST="${TRADER_HOST:-127.0.0.1}"
export TRADER_PORT="${TRADER_PORT:-5000}"

if [[ "$COMMAND_KIND" == "server" ]]; then
  exec "$VENV_DIR/bin/trader-server" --config "$CONFIG_PATH"
fi
exec "$VENV_DIR/bin/trader-cli" --config "$CONFIG_PATH" "$MODE" "${@:2}"
