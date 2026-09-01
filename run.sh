#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-$ROOT_DIR/.venv}"
CONFIG_PATH="${TRADER_CONFIG:-$ROOT_DIR/config/v2/runtime.json}"
MODE="serve"
MODE_SET=0
SCORING_PROFILE="v1"
FORWARD_ARGS=()

usage() {
  printf '%s\n' \
    "本地 A 股研究看板" \
    "" \
    "日常使用（不做离线研究）:" \
    "  ./run.sh                         以默认 V1 启动本地 A 股研究看板" \
    "  ./run.sh serve                   显式启动，等同于无参数运行" \
    "  ./run.sh --profile v2            显式使用 V2 启动" \
    "  ./run.sh check                   依次校验配置、研究状态和性能门禁" \
    "  ./run.sh help                    查看本帮助" \
    "" \
    "离线研究（仅在明确执行研究任务时使用）:" \
    "  ./run.sh research-history        下载/续传历史归档后运行固定回测" \
    "  ./run.sh research-screen         依次运行并封存六项历史筛选/诊断" \
    "  ./run.sh train-tomorrow          从封存状态推导并连续运行可用 Tomorrow 训练阶段" \
    "" \
    "所有命令都可追加 --profile v1|v2；未指定时为 V1。" \
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

while (($#)); do
  case "$1" in
    --profile)
      if (($# < 2)); then
        printf '%s\n' '缺少 --profile 的值（v1 或 v2）。' >&2
        exit 2
      fi
      SCORING_PROFILE="$2"
      shift 2
      ;;
    --profile=*)
      SCORING_PROFILE="${1#--profile=}"
      shift
      ;;
    help|-h|--help|serve|app|check|research-history|research-screen|train-tomorrow)
      if ((MODE_SET)); then
        FORWARD_ARGS+=("$1")
      else
        MODE="$1"
        MODE_SET=1
      fi
      shift
      ;;
    *)
      if ((MODE_SET)); then
        FORWARD_ARGS+=("$1")
        shift
      else
        printf '未知命令: %s\n' "$1" >&2
        printf '日常启动直接运行: ./run.sh\n' >&2
        printf '查看全部命令: ./run.sh help\n' >&2
        exit 2
      fi
      ;;
  esac
done

if [[ "$SCORING_PROFILE" != "v1" && "$SCORING_PROFILE" != "v2" ]]; then
  printf '评分档位只能是 v1 或 v2: %s\n' "$SCORING_PROFILE" >&2
  exit 2
fi

case "$MODE" in
  help|-h|--help)
    usage
    exit 0
    ;;
  serve|app)
    COMMAND_KIND="server"
    ;;
  check|research-history|research-screen|train-tomorrow)
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
  exec "$VENV_DIR/bin/trader-server" --config "$CONFIG_PATH" --profile "$SCORING_PROFILE" "${FORWARD_ARGS[@]}"
fi
exec "$VENV_DIR/bin/trader-cli" --config "$CONFIG_PATH" --profile "$SCORING_PROFILE" "$MODE" "${FORWARD_ARGS[@]}"
