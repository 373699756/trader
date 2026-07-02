#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-$ROOT_DIR/.venv}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-5000}"
PYTHON_MIN_MINOR="${PYTHON_MIN_MINOR:-9}"
PYTHON_MAX_MINOR="${PYTHON_MAX_MINOR:-11}"

# 代理模式:
#   auto: 优先沿用已有环境变量；否则自动扫描本机/WSL 常见代理端口
#   on:   必须连上指定代理，否则退出
#   off:  清理代理环境变量后直连
PROXY_MODE="${PROXY_MODE:-auto}"
PROXY_SCHEME="${PROXY_SCHEME:-http}"
PROXY_HOST="${PROXY_HOST:-}"
PROXY_PORT="${PROXY_PORT:-}"
INTERNET_CHECK_URLS="${INTERNET_CHECK_URLS:-https://pypi.org/simple/pip/ https://www.google.com/generate_204}"
SKIP_PROXY_CHECK="${SKIP_PROXY_CHECK:-0}"

PIP_TIMEOUT="${PIP_TIMEOUT:-60}"
PIP_RETRIES="${PIP_RETRIES:-5}"

cd "$ROOT_DIR"

usage() {
  cat <<'EOF'
用法:
  ./run.sh

常用环境变量:
  PORT=5050 ./run.sh
  PROXY_MODE=on PROXY_PORT=7890 ./run.sh
  PROXY_MODE=off ./run.sh
  PROXY_HOST=172.20.48.1 PROXY_PORT=7890 ./run.sh   # WSL/远端宿主机代理
  PROXY_SCHEME=socks5h PROXY_PORT=1080 ./run.sh     # 仅在确认 pip 支持 socks 时使用
  INTERNET_CHECK_URLS="https://pypi.org/simple/pip/" ./run.sh
  SKIP_PROXY_CHECK=1 ./run.sh                        # 明确跳过启动前外网检查

默认会自动探测 127.0.0.1、WSL nameserver、host.docker.internal 的常见代理端口。
EOF
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  usage
  exit 0
fi

clear_proxy_env() {
  unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
  unset PIP_PROXY
}

tcp_open() {
  local host="$1"
  local port="$2"

  timeout 2 bash -c "</dev/tcp/$host/$port" >/dev/null 2>&1
}

detect_wsl_host() {
  if [ -r /etc/resolv.conf ]; then
    awk '/^nameserver[[:space:]]+/ {print $2; exit}' /etc/resolv.conf
  fi
}

candidate_hosts() {
  local hosts=()
  local wsl_host

  if [ -n "$PROXY_HOST" ]; then
    hosts+=("$PROXY_HOST")
  else
    hosts+=("127.0.0.1" "localhost")
    wsl_host="$(detect_wsl_host || true)"
    if [ -n "$wsl_host" ]; then
      hosts+=("$wsl_host")
    fi
    hosts+=("host.docker.internal")
  fi

  printf '%s\n' "${hosts[@]}" | awk 'NF && !seen[$0]++'
}

candidate_ports() {
  if [ -n "$PROXY_PORT" ]; then
    printf '%s\n' "$PROXY_PORT"
  else
    printf '%s\n' 7890 7897 7891 10809 10808 20171 2080 8080 8118 1080
  fi
}

set_proxy_env() {
  local host="$1"
  local port="$2"
  local url="${PROXY_SCHEME}://${host}:${port}"

  export HTTP_PROXY="$url"
  export HTTPS_PROXY="$url"
  export ALL_PROXY="$url"
  export http_proxy="$url"
  export https_proxy="$url"
  export all_proxy="$url"
  export PIP_PROXY="$url"

  local default_no_proxy="localhost,127.0.0.1,::1"
  export NO_PROXY="${NO_PROXY:-$default_no_proxy}"
  export no_proxy="${no_proxy:-$NO_PROXY}"
}

has_proxy_env() {
  [ -n "${HTTP_PROXY:-}${HTTPS_PROXY:-}${http_proxy:-}${https_proxy:-}${ALL_PROXY:-}${all_proxy:-}" ]
}

detect_proxy() {
  local host
  local port

  while IFS= read -r host; do
    while IFS= read -r port; do
      if tcp_open "$host" "$port"; then
        printf '%s %s\n' "$host" "$port"
        return 0
      fi
    done < <(candidate_ports)
  done < <(candidate_hosts)

  return 1
}

show_proxy_env() {
  local name
  local shown=0

  for name in HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY; do
    if [ -n "${!name:-}" ]; then
      if [ "$shown" -eq 0 ]; then
        printf '当前代理环境变量:\n'
        shown=1
      fi
      printf '  %s=%s\n' "$name" "${!name}"
    fi
  done
}

configure_proxy() {
  local detected
  local host
  local port

  if [ "${CLEAR_PROXY:-0}" = "1" ]; then
    PROXY_MODE=off
  fi

  case "$PROXY_MODE" in
    off)
      clear_proxy_env
      printf '代理模式: off，已清理代理环境变量。\n'
      ;;
    on)
      host="${PROXY_HOST:-127.0.0.1}"
      port="${PROXY_PORT:-7890}"
      if ! tcp_open "$host" "$port"; then
        printf '代理模式: on，但无法连接 %s:%s。\n' "$host" "$port" >&2
        printf '请确认代理客户端已启动，并开启 HTTP/Mixed 代理端口；或用 PROXY_HOST/PROXY_PORT 指定。\n' >&2
        exit 1
      fi
      set_proxy_env "$host" "$port"
      printf '代理模式: on，使用 %s://%s:%s\n' "$PROXY_SCHEME" "$host" "$port"
      ;;
    auto)
      if has_proxy_env; then
        printf '代理模式: auto，沿用当前代理环境变量。\n'
        if [ -z "${PIP_PROXY:-}" ]; then
          export PIP_PROXY="${HTTPS_PROXY:-${https_proxy:-${HTTP_PROXY:-${http_proxy:-${ALL_PROXY:-${all_proxy:-}}}}}}"
        fi
      elif detected="$(detect_proxy)"; then
        host="${detected% *}"
        port="${detected#* }"
        set_proxy_env "$host" "$port"
        printf '代理模式: auto，检测到代理 %s://%s:%s\n' "$PROXY_SCHEME" "$host" "$port"
      else
        printf '代理模式: auto，未检测到可用代理，本次直连。\n'
        printf '如需强制使用代理: PROXY_MODE=on PROXY_PORT=7890 ./run.sh\n'
      fi
      ;;
    *)
      printf 'PROXY_MODE 只能是 auto、on 或 off，当前为: %s\n' "$PROXY_MODE" >&2
      exit 1
      ;;
  esac
}

find_python() {
  if [ -x "$ROOT_DIR/.runtime/python-3.11/bin/python3.11" ]; then
    printf '%s\n' "$ROOT_DIR/.runtime/python-3.11/bin/python3.11"
    return
  fi
  if command -v python3.11 >/dev/null 2>&1; then
    command -v python3.11
    return
  fi
  if [ -x /home/c/.local/bin/python3.11 ]; then
    printf '%s\n' /home/c/.local/bin/python3.11
    return
  fi
  if command -v python3.10 >/dev/null 2>&1; then
    command -v python3.10
    return
  fi
  if command -v python3.9 >/dev/null 2>&1; then
    command -v python3.9
    return
  fi
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return
  fi
  return 1
}

python_version() {
  "$1" - <<'PY'
import sys
print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
PY
}

check_python() {
  "$1" - "$PYTHON_MIN_MINOR" "$PYTHON_MAX_MINOR" <<'PY'
import sys

min_minor = int(sys.argv[1])
max_minor = int(sys.argv[2])
version = sys.version_info

if version.major != 3 or version.minor < min_minor or version.minor > max_minor:
    raise SystemExit(
        f"当前 Python 版本为 {version.major}.{version.minor}.{version.micro}，"
        f"本项目依赖要求 Python 3.{min_minor}-3.{max_minor}，推荐 Python 3.11。\n"
        "请先安装兼容的 Python 后重试；如果已有旧虚拟环境，请删除后重建。"
    )
PY
}

check_internet_connectivity() {
  local python_bin="$1"
  local url

  if [ "$SKIP_PROXY_CHECK" = "1" ]; then
    printf '已跳过启动前外网检查。\n'
    return 0
  fi

  printf '启动前检查外网连通性...\n'
  for url in $INTERNET_CHECK_URLS; do
    printf '  尝试: %s\n' "$url"
    if "$python_bin" - "$url" <<'PY'
import sys
import urllib.request

url = sys.argv[1]
request = urllib.request.Request(url, headers={"User-Agent": "trader-run-sh/1.0"})
with urllib.request.urlopen(request, timeout=8) as response:
    if response.status >= 400:
        raise SystemExit(f"HTTP {response.status}")
PY
    then
      printf '外网检查通过。\n'
      return 0
    fi
  done

  printf '外网检查失败，已停止启动。\n' >&2
  printf '请确认代理已开启，并检查 HTTP/Mixed 端口是否正确。\n' >&2
  printf '常见用法: PROXY_MODE=on PROXY_PORT=7897 ./run.sh\n' >&2
  printf '如代理在宿主机/WSL 网关: PROXY_HOST=<网关IP> PROXY_PORT=7897 ./run.sh\n' >&2
  printf '确认不需要检查时，可显式使用: SKIP_PROXY_CHECK=1 ./run.sh\n' >&2
  exit 1
}

ensure_venv() {
  local python_bin

  if [ -x "$VENV_DIR/bin/python" ]; then
    if ! check_python "$VENV_DIR/bin/python"; then
      printf '\n已有虚拟环境不兼容: %s\n' "$VENV_DIR" >&2
      printf '当前虚拟环境 Python 版本: %s\n' "$(python_version "$VENV_DIR/bin/python")" >&2
      printf '处理方式：删除旧虚拟环境并重建：\n' >&2
      printf '  rm -rf %q\n' "$VENV_DIR" >&2
      printf '  ./run.sh\n' >&2
      exit 1
    fi
    return
  fi

  python_bin="$(find_python || true)"
  if [ -z "$python_bin" ]; then
    printf '未找到 Python。请安装 Python 3.9-3.11，推荐 Python 3.11。\n' >&2
    exit 1
  fi

  check_python "$python_bin"

  printf '创建虚拟环境: %s\n' "$VENV_DIR"
  if ! "$python_bin" -m venv "$VENV_DIR"; then
    printf '创建虚拟环境失败。Ubuntu 可先安装: sudo apt install python3-venv\n' >&2
    exit 1
  fi
}

pip_install() {
  local pip_args=(
    --disable-pip-version-check
    --default-timeout "$PIP_TIMEOUT"
    --retries "$PIP_RETRIES"
  )

  if [ -n "${PIP_PROXY:-}" ]; then
    pip_args+=(--proxy "$PIP_PROXY")
  fi

  printf '安装/更新依赖...\n'
  "$VENV_DIR/bin/python" -m pip install "${pip_args[@]}" --upgrade pip
  "$VENV_DIR/bin/python" -m pip install "${pip_args[@]}" --prefer-binary -r requirements.txt
}

mkdir -p "$ROOT_DIR/.runtime"
configure_proxy
show_proxy_env
printf '\n'

ensure_venv
check_internet_connectivity "$VENV_DIR/bin/python"
pip_install

printf '\n启动看板: http://%s:%s\n' "$HOST" "$PORT"
printf '历史因子: ENABLE_HISTORY_FACTORS=%s（可显式设为 0 关闭）\n' "${ENABLE_HISTORY_FACTORS:-1}"
printf '按 Ctrl+C 停止。\n\n'

export HOST
export PORT
export FLASK_RUN_HOST="$HOST"
export FLASK_RUN_PORT="$PORT"
export ENABLE_HISTORY_FACTORS="${ENABLE_HISTORY_FACTORS:-1}"
exec "$VENV_DIR/bin/python" app.py
