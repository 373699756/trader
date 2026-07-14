#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv/bin/python}"
PYTEST_ARGS="${PYTEST_ARGS:-}"
USE_XDIST="${USE_XDIST:-${PYTEST_XDIST_REQUIRED:-0}}"
CI_USE_XDIST="${CI_USE_XDIST:-$USE_XDIST}"
PYTEST_XDIST_WORKERS="${PYTEST_XDIST_WORKERS:-auto}"

if [ "${CI_USE_XDIST,,}" = "false" ] && [ "${PYTEST_XDIST_WORKERS}" = "auto" ]; then
  PYTEST_XDIST_WORKERS="0"
fi

has_xdist() {
  "${PYTHON_BIN}" - <<'PY'
import importlib.util
import sys
sys.exit(0 if importlib.util.find_spec("xdist") is not None else 1)
PY
}

run_pytest() {
  local -a args=("$@")
  local workers="${PYTEST_XDIST_WORKERS:-0}"
  if [ "${workers}" != "0" ]; then
    if has_xdist; then
      if [ "${workers}" = "auto" ]; then
        args=(-n auto --dist loadscope "${args[@]}")
      else
        args=(-n "${workers}" --dist loadscope "${args[@]}")
      fi
    elif [ "${PYTEST_XDIST_REQUIRED:-0}" = "1" ] || [ "${USE_XDIST:-0}" = "1" ] || [ "${CI_USE_XDIST}" = "1" ]; then
      echo "error: pytest-xdist requested (PYTEST_XDIST_WORKERS=${workers}) but pytest-xdist is not installed." >&2
      echo "install with: pip install pytest-xdist" >&2
      exit 2
    else
      echo "warning: pytest-xdist is not installed, fallback to serial mode." >&2
    fi
  fi
  PYTHONPATH=. "${PYTHON_BIN}" -m pytest -q "${args[@]}"
}

case "${1:-fast}" in
  fast)
    run_pytest -m "not (slow or integration)" --durations=50 ${PYTEST_ARGS}
    ;;
  fast-parallel)
    run_pytest -m "not (slow or integration)" --durations=50 ${PYTEST_ARGS}
    ;;
  integration)
    run_pytest -m "integration and not slow" --durations=50 ${PYTEST_ARGS}
    ;;
  slow)
    run_pytest -m "slow and not integration" --durations=50 ${PYTEST_ARGS}
    ;;
  slow-integration)
    run_pytest -m "slow and integration" --durations=50 ${PYTEST_ARGS}
    ;;
  all)
    run_pytest -m "not slow" --durations=50 ${PYTEST_ARGS}
    ;;
  *)
    echo "Usage: $0 [fast|fast-parallel|integration|slow|slow-integration|all]" >&2
    echo "  fast           default fast path (not slow, not integration)"
    echo "  fast-parallel  parallel fast path with xdist"
    echo "  integration    integration tests"
    echo "  slow           slow-only tests"
    echo "  slow-integration heavy tests"
    echo "  all            everything except slow-only tests"
    echo "Environment:"
    echo "  PYTEST_XDIST_WORKERS=auto # parallel worker count: auto, 0, N"
    echo "  PYTEST_XDIST_REQUIRED=1  # enforce failure when fast-parallel lacks pytest-xdist"
    echo "  USE_XDIST=1               # alias of PYTEST_XDIST_REQUIRED"
    exit 2
    ;;
esac
