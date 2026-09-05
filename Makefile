SHELL := /bin/bash
PYTHON ?= .venv/bin/python
SOURCE_PATHS := src/trader tests scripts/check_refactor_quality.py scripts/generate_long_watchlist_asset.py \
	scripts/diagnose_runtime.py scripts/package_tomorrow_v1_model.py \
	scripts/verify_wheel_install.py \
	scripts/runtime_diagnostics/__init__.py scripts/runtime_diagnostics/common.py \
	scripts/runtime_diagnostics/web_health.py scripts/runtime_diagnostics/web_health_contract.py \
	scripts/runtime_diagnostics/browser_refresh.py \
	scripts/runtime_diagnostics/exchange_security_master.py \
	scripts/runtime_diagnostics/history_sources.py scripts/runtime_diagnostics/tencent_quotes.py \
	scripts/runtime_diagnostics/tushare_daily.py

.PHONY: help install-dev format format-check lint long-watchlist-check type-check test test-unit test-component test-contract test-integration test-release quality package performance-check browser-performance-check diagnose-live diagnose-full

help:
	@echo "make install-dev   - install editable package and development tools"
	@echo "make format        - format Python sources and tests"
	@echo "make long-watchlist-check - verify the packaged long-watchlist asset"
	@echo "make quality       - format, lint, type and test gates"
	@echo "make test          - run all tests"
	@echo "make test-unit     - run unit tests"
	@echo "make test-component - run component tests"
	@echo "make test-contract  - run contract tests"
	@echo "make test-integration - run integration tests"
	@echo "make test-release  - build and verify the wheel outside the repository"
	@echo "make package       - build wheel and source distribution"
	@echo "make performance-check - run the offline active-production performance gate"
	@echo "make browser-performance-check - run Firefox SSE patch-to-paint and refresh gate"
	@echo "make diagnose-live - run bounded Web and real-provider runtime diagnostics"
	@echo "make diagnose-full - add Firefox and offline performance diagnostics"

install-dev:
	$(PYTHON) -m pip install -e ".[dev]"

format:
	$(PYTHON) -m ruff format $(SOURCE_PATHS)
	$(PYTHON) -m ruff check --select E,F,I,B,UP --ignore E501 --fix $(SOURCE_PATHS)

format-check:
	$(PYTHON) -m ruff format --check $(SOURCE_PATHS)

lint: long-watchlist-check
	$(PYTHON) -m ruff check --select E,F,I,B,UP --ignore E501 $(SOURCE_PATHS)
	$(PYTHON) scripts/check_refactor_quality.py

long-watchlist-check:
	$(PYTHON) scripts/generate_long_watchlist_asset.py --check

type-check:
	$(PYTHON) -m mypy src/trader

test:
	$(PYTHON) -m pytest -q tests

test-unit:
	$(PYTHON) -m pytest -q tests/unit

test-component:
	$(PYTHON) -m pytest -q tests/component

test-contract:
	$(PYTHON) -m pytest -q tests/contract

test-integration:
	$(PYTHON) -m pytest -q tests/integration

test-release: package
	$(PYTHON) scripts/verify_wheel_install.py --dist-dir dist

quality: format-check lint type-check test

package:
	$(PYTHON) -m build

performance-check:
	$(PYTHON) -m trader.entrypoints.performance --config config/runtime.json

browser-performance-check:
	$(PYTHON) scripts/diagnose_runtime.py --profile browser --browser-duration-seconds 8 --browser-minimum-updates 3 --output -

diagnose-live:
	$(PYTHON) scripts/diagnose_runtime.py --profile live --output -

diagnose-full:
	$(PYTHON) scripts/diagnose_runtime.py --profile full --output -
