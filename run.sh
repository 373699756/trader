#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-$ROOT_DIR/.venv}"
CONFIG_PATH="${TRADER_CONFIG:-$ROOT_DIR/config/runtime.json}"
MODE=""
FORWARD_ARGS=()

usage() {
  printf '%s\n' \
    "本地 A 股研究看板" \
    "" \
    "日常使用（不做离线研究）:" \
    "  ./run.sh                         同时启动全部档位 V1、V2、V3、V5" \
    "  ./run.sh v2                      只启动 V2" \
    "  ./run.sh v1,v2,v3                同时启动所列档位，Web按钮互斥切换" \
    "  ./run.sh help                    查看本帮助" \
    "" \
    "离线研究（仅在明确执行研究任务时使用）:" \
    "  ./run.sh sync                    依次更新参考、完整历史、行情窗口与公司资料" \
    "  ./run.sh sync history            单独更新完整历史，自动续传" \
    "  ./run.sh sync market             单独更新行情窗口" \
    "  ./run.sh sync company            单独更新公司资料" \
    "  ./run.sh train                   历史维护后处理全部档位 V1、V2、V3、V5" \
    "  ./run.sh train v1,v5             历史维护后只训练所列档位" \
    "" \
    "档位以逗号分隔，可自由组合；省略时选择全部。V1为工程Shadow，V5需可选CPU依赖。" \
    "本地检测使用 scripts/diagnose_runtime.py --profile local。" \
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

case "${1:-}" in
  help|-h|--help)
    usage
    exit 0
    ;;
  ""|v[0-9]*)
    COMMAND_KIND="server"
    ;;
  sync|train)
    MODE="$1"
    shift
    COMMAND_KIND="cli"
    ;;
  *)
    printf '未知命令: %s\n' "$1" >&2
    printf '日常启动直接运行: ./run.sh\n' >&2
    printf '查看全部命令: ./run.sh help\n' >&2
    exit 2
    ;;
esac

if (($# > 1)); then
  printf '%s\n' '用法：./run.sh [档位列表] 或 ./run.sh train [档位列表]；列表以逗号分隔。' >&2
  exit 2
fi
if (($#)); then
  if [[ "$MODE" == "sync" ]]; then
    if [[ "$1" != "history" && "$1" != "market" && "$1" != "company" ]]; then
      printf '%s\n' '用法：./run.sh sync [history|market|company]' >&2
      exit 2
    fi
  else
    if [[ ! "$1" =~ ^v(1|2|3|5)(,v(1|2|3|5))*$ ]]; then
      printf '%s\n' '档位列表只能包含 v1、v2、v3、v5，以逗号分隔且不得重复。' >&2
      exit 2
    fi
    IFS=',' read -r -a SELECTED_PROFILES <<< "$1"
    SEEN_PROFILES=","
    for profile in "${SELECTED_PROFILES[@]}"; do
      if [[ "$SEEN_PROFILES" == *",$profile,"* ]]; then
        printf '档位不能重复: %s\n' "$profile" >&2
        exit 2
      fi
      SEEN_PROFILES+="$profile,"
    done
  fi
  FORWARD_ARGS=("$1")
fi

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

if [[ "$COMMAND_KIND" == "server" ]]; then
  exec "$ENTRYPOINT" --config "$CONFIG_PATH" "${FORWARD_ARGS[@]}"
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
        -- "$ENTRYPOINT" --config "$CONFIG_PATH" "$MODE" "${FORWARD_ARGS[@]}"
    fi
    printf '%s\n' '警告：当前用户 systemd scope 不可用；联合下载训练仍使用有界资源，但不能提供 cgroup 硬隔离。' >&2
  fi
  exec "$ENTRYPOINT" --config "$CONFIG_PATH" "$MODE" "${FORWARD_ARGS[@]}"
fi
exec "$ENTRYPOINT" --config "$CONFIG_PATH" "$MODE" "${FORWARD_ARGS[@]}"
