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
    "  ./run.sh sync history --fill     自动用BaoStock补历史缺口" \
    "  ./run.sh sync market             单独更新行情窗口" \
    "  ./run.sh sync market --fill      自动用BaoStock补行情窗口缺口" \
    "  ./run.sh sync company            单独更新公司资料" \
    "  ./run.sh sync company --fill     补充历史缺口和待补正文，自动OCR/扩限，复用已完成资料" \
    "  ./run.sh train                   使用本地已发布历史训练全部档位 V1、V2、V3、V5" \
    "  ./run.sh train v1,v5             使用本地已发布历史，只训练所列档位" \
    "" \
    "档位以逗号分隔，可自由组合；省略时选择全部。V1已正式接入，V5保持工程Shadow并需可选CPU依赖。" \
    "本地检测使用 scripts/tools.py --business recommendation --module pipeline --action snapshot。" \
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

if [[ "$MODE" != "sync" ]] && (($# > 1)); then
  printf '%s\n' '用法：./run.sh [档位列表] 或 ./run.sh train [档位列表]；列表以逗号分隔。' >&2
  exit 2
fi
if (($#)); then
  if [[ "$MODE" == "sync" ]]; then
    if [[ "$1" != "history" && "$1" != "market" && "$1" != "company" ]]; then
      printf '%s\n' '用法：./run.sh sync [history|market|company]' >&2
      exit 2
    fi
    if (($# != 1)) && { (($# != 2)) || [[ "$2" != "--fill" ]]; }; then
      printf '%s\n' '用法：./run.sh sync [history|market|company]；指定分项后可加 --fill 补充下载。' >&2
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
  FORWARD_ARGS=("$@")
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

if [[ "$MODE" == "sync" || "$MODE" == "train" ]]; then
  if [[ -x "$VENV_DIR/bin/python3" ]]; then
    COMMAND_PYTHON="$VENV_DIR/bin/python3"
  else
    COMMAND_PYTHON="$(find_python || true)"
  fi
  if [[ -z "$COMMAND_PYTHON" ]]; then
    printf '需要 Python 3.10-3.14，%s 尚未启动。\n' "$MODE" >&2
    exit 1
  fi
  TRADER_COMMAND_STARTED_MONOTONIC="$("$COMMAND_PYTHON" -c 'import time; print(time.monotonic())')"
  export TRADER_COMMAND_STARTED_MONOTONIC
  command_log() {
    "$COMMAND_PYTHON" "$ROOT_DIR/src/trader/infra/progress_format.py" "$@"
  }
  command_log 开始 检查运行环境
  trap 'command_log 失败 运行环境准备失败' ERR
fi

if [[ ! -x "$VENV_DIR/bin/python3" ]]; then
  PYTHON_BIN="$(find_python || true)"
  if [[ -z "$PYTHON_BIN" ]]; then
    printf '需要 Python 3.10-3.14。\n' >&2
    exit 1
  fi
  if [[ "$MODE" == "sync" || "$MODE" == "train" ]]; then
    "$PYTHON_BIN" -m venv "$VENV_DIR" 2>&1 | command_log --pipe
  else
    "$PYTHON_BIN" -m venv "$VENV_DIR"
  fi
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
  # pip 25.1.1 can raise InvalidVersion: '' while resolving extras on Python 3.14.
  if "$VENV_DIR/bin/python3" -c 'import pip, sys; raise SystemExit(not (sys.version_info[:2] == (3, 14) and int(pip.__version__.split(".", 1)[0]) < 26))'; then
    if [[ "$MODE" == "sync" || "$MODE" == "train" ]]; then
      "$VENV_DIR/bin/python3" -m pip install --disable-pip-version-check --upgrade 'pip>=26,<27' 2>&1 | command_log --pipe
    else
      "$VENV_DIR/bin/python3" -m pip install --disable-pip-version-check --upgrade 'pip>=26,<27' >&2
    fi
  fi
  # Let pip provision the build requirements declared in pyproject.toml;
  # a fresh virtual environment may contain pip without setuptools.
  if [[ "$MODE" == "sync" || "$MODE" == "train" ]]; then
    "$VENV_DIR/bin/python3" -m pip install --disable-pip-version-check -e "$ROOT_DIR" 2>&1 | command_log --pipe
  else
    "$VENV_DIR/bin/python3" -m pip install --disable-pip-version-check -e "$ROOT_DIR" >&2
  fi
fi

export TRADER_HOST="${TRADER_HOST:-127.0.0.1}"
export TRADER_PORT="${TRADER_PORT:-5000}"

if [[ "$COMMAND_KIND" == "server" ]]; then
  exec "$ENTRYPOINT" --config "$CONFIG_PATH" "${FORWARD_ARGS[@]}"
fi
if [[ "$MODE" == "sync" || "$MODE" == "train" ]]; then
  command_log 完成 运行环境检查完成
  trap - ERR
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
    command_log 处理中 '警告：当前用户 systemd scope 不可用；训练仍使用有界资源，但不能提供 cgroup 硬隔离。'
  fi
  exec "$ENTRYPOINT" --config "$CONFIG_PATH" "$MODE" "${FORWARD_ARGS[@]}"
fi
exec "$ENTRYPOINT" --config "$CONFIG_PATH" "$MODE" "${FORWARD_ARGS[@]}"
