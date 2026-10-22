#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-$ROOT_DIR/.venv}"
CONFIG_PATH="${TRADER_CONFIG:-$ROOT_DIR/config/runtime.json}"
MODE=""
MODE_SET=0
SCORING_PROFILE=""
SCORING_PROFILE_SET=0
FORWARD_ARGS=()

usage() {
  printf '%s\n' \
    "本地 A 股研究看板" \
    "" \
    "日常使用（不做离线研究）:" \
    "  ./run.sh                         以默认 V2 启动并加载共享双头模型" \
    "  ./run.sh --profile v2|v3         单独启动 V2 或 V3" \
    "  ./run.sh --profile both          同一进程同时启动 V2 和 V3" \
    "  ./run.sh --profile v1            单独启动全新 V1 Shadow（需先显式训练）" \
    "  ./run.sh help                    查看本帮助" \
    "" \
    "离线研究（仅在明确执行研究任务时使用）:" \
    "  ./run.sh sync                    依次更新参考、完整历史、行情窗口与公司资料" \
    "  ./run.sh sync history            单独更新完整历史，自动续传" \
    "  ./run.sh sync market             单独更新行情窗口" \
    "  ./run.sh sync company            单独更新公司资料" \
    "  ./run.sh train                   历史维护后联合训练或复用 V2/V3" \
    "  ./run.sh train --profile v1      历史维护后训练全新 V1 双头 Shadow 工件" \
    "" \
    "看板可追加 --profile v1|v2|v3|both；both 保持 V2/V3。本地检测使用 scripts/diagnose_runtime.py --profile local。" \
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
        printf '%s\n' '缺少 --profile 的值（v1、v2、v3 或 both）。' >&2
        exit 2
      fi
      SCORING_PROFILE="$2"
      SCORING_PROFILE_SET=1
      shift 2
      ;;
    --profile=*)
      SCORING_PROFILE="${1#--profile=}"
      SCORING_PROFILE_SET=1
      shift
      ;;
    help|-h|--help|sync|train)
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

if ((SCORING_PROFILE_SET)) && [[ "$SCORING_PROFILE" != "v1" && "$SCORING_PROFILE" != "v2" && "$SCORING_PROFILE" != "v3" && "$SCORING_PROFILE" != "both" ]]; then
  printf '评分档位只能是 v1、v2、v3 或 both: %s\n' "$SCORING_PROFILE" >&2
  exit 2
fi

if [[ "$MODE" == "sync" || ( "$MODE" == "train" && "$SCORING_PROFILE" != "v1" ) ]] && ((SCORING_PROFILE_SET)); then
  printf '%s\n' "$MODE 不接受 --profile。" >&2
  exit 2
fi
if [[ "$MODE" == "train" ]] && (("${#FORWARD_ARGS[@]}" > 0)); then
  printf '%s\n' 'train 不接受额外参数。' >&2
  exit 2
fi
if [[ "$MODE" == "sync" ]] && ((${#FORWARD_ARGS[@]} > 0)); then
  if ((${#FORWARD_ARGS[@]} != 1)) || [[ "${FORWARD_ARGS[0]}" != "history" && "${FORWARD_ARGS[0]}" != "market" && "${FORWARD_ARGS[0]}" != "company" ]]; then
    printf '%s\n' '用法：./run.sh sync [history|market|company]' >&2
    exit 2
  fi
fi

case "$MODE" in
  help|-h|--help)
    usage
    exit 0
    ;;
  "")
    COMMAND_KIND="server"
    ;;
  sync|train)
    COMMAND_KIND="cli"
    ;;
  *)
    printf '未知命令: %s\n' "$MODE" >&2
    printf '日常启动直接运行: ./run.sh\n' >&2
    printf '查看全部命令: ./run.sh help\n' >&2
    exit 2
    ;;
esac

if [[ "$MODE" == "sync" ]]; then
  printf '%s: 正在检查运行环境。\n' "$MODE" >&2
fi

find_python() {
  local candidate
  for candidate in python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(not ((3, 10) <= sys.version_info[:2] < (3, 15)))'; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

if [[ ! -x "$VENV_DIR/bin/python3" ]]; then
  PYTHON_BIN="$(find_python || true)"
  if [[ -z "$PYTHON_BIN" ]]; then
    printf '需要 Python 3.10-3.14。\n' >&2
    exit 1
  fi
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

if [[ "$COMMAND_KIND" == "server" ]]; then
  ENTRYPOINT="$VENV_DIR/bin/trader-server"
else
  ENTRYPOINT="$VENV_DIR/bin/trader-cli"
fi

NEEDS_INSTALL=0
if [[ ! -x "$ENTRYPOINT" || "$ROOT_DIR/pyproject.toml" -nt "$ENTRYPOINT" || "${FORCE_INSTALL_DEPS:-0}" == "1" ]]; then
  NEEDS_INSTALL=1
elif ! "$ENTRYPOINT" --help >/dev/null 2>&1; then
  NEEDS_INSTALL=1
fi

if ((NEEDS_INSTALL)); then
  # Let pip provision the build requirements declared in pyproject.toml;
  # a fresh virtual environment may contain pip without setuptools.
  "$VENV_DIR/bin/python3" -m pip install --disable-pip-version-check -e "$ROOT_DIR" >&2
fi

export TRADER_HOST="${TRADER_HOST:-127.0.0.1}"
export TRADER_PORT="${TRADER_PORT:-5000}"

PROFILE_ARGS=()
if ((SCORING_PROFILE_SET)); then
  PROFILE_ARGS=(--profile "$SCORING_PROFILE")
fi

if [[ "$COMMAND_KIND" == "server" ]]; then
  exec "$ENTRYPOINT" --config "$CONFIG_PATH" "${PROFILE_ARGS[@]}" "${FORWARD_ARGS[@]}"
fi
if [[ "$MODE" == "sync" ]]; then
  exec "$ENTRYPOINT" --config "$CONFIG_PATH" sync "${FORWARD_ARGS[@]}"
fi
if [[ "$MODE" == "train" ]]; then
  TRAINING_MEMORY_HIGH=4608M
  TRAINING_MEMORY_MAX=5120M
  if [[ "$(uname -s)" == "Linux" ]]; then
    if command -v systemd-run >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
      exec systemd-run --user --scope --quiet --collect \
        --unit="trader-${MODE}-training" \
        --slice=background.slice \
        --property="MemoryHigh=$TRAINING_MEMORY_HIGH" \
        --property="MemoryMax=$TRAINING_MEMORY_MAX" \
        --property=MemorySwapMax=2048M \
        --property=CPUWeight=20 \
        --property=IOWeight=20 \
        -- "$ENTRYPOINT" --config "$CONFIG_PATH" "$MODE" "${PROFILE_ARGS[@]}"
    fi
    printf '%s\n' '警告：当前用户 systemd scope 不可用；联合下载训练仍使用有界资源，但不能提供 cgroup 硬隔离。' >&2
  fi
  exec "$ENTRYPOINT" --config "$CONFIG_PATH" "$MODE" "${PROFILE_ARGS[@]}"
fi
exec "$ENTRYPOINT" --config "$CONFIG_PATH" "${PROFILE_ARGS[@]}" "$MODE" "${FORWARD_ARGS[@]}"
