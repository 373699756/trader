#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv/bin/python}"

case "${1:-all}" in
  unit)
    exec "$PYTHON_BIN" -m pytest -q "$ROOT_DIR/tests/unit"
    ;;
  component)
    exec "$PYTHON_BIN" -m pytest -q "$ROOT_DIR/tests/component"
    ;;
  contract)
    exec "$PYTHON_BIN" -m pytest -q "$ROOT_DIR/tests/contract"
    ;;
  integration)
    exec "$PYTHON_BIN" -m pytest -q "$ROOT_DIR/tests/integration"
    ;;
  all)
    exec "$PYTHON_BIN" -m pytest -q "$ROOT_DIR/tests"
    ;;
  *)
    printf 'Usage: %s [unit|component|contract|integration|all]\n' "$0" >&2
    exit 2
    ;;
esac
