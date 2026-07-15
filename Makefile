SHELL := /bin/bash
PYTEST_XDIST_WORKERS ?= auto

.PHONY: help quality lint lint-strict format-check type-check coverage-fast package
.PHONY: test-fast test-fast-parallel test-fast-parallel-strict test-integration test-slow test-slow-integration test-all
.PHONY: test

PYTHON ?= .venv/bin/python
MODERNIZED_PYTHON_FILES := scripts/dependency_fingerprint.py stock_analyzer/background_workers.py stock_analyzer/config.py stock_analyzer/deepseek_scheduler.py stock_analyzer/realtime_quotes.py stock_analyzer/runtime.py stock_analyzer/server_security.py stock_analyzer/services/recommendation_cache.py stock_analyzer/snapshot_writer.py tests/test_background_workers.py tests/test_config_environment.py tests/test_deepseek_scheduler.py tests/test_dependency_fingerprint.py tests/test_recommendation_cache_services.py tests/test_runtime_supervisor.py tests/test_server_security.py tests/test_web_quote_background.py
ISOLATED_MYPY_FILES := stock_analyzer/services/recommendation_cache.py stock_analyzer/snapshot_writer.py

help:
	@echo "make test-fast             - fast tests (not slow and not integration)"
	@echo "make test-fast-parallel    - parallel fast tests (requires pytest-xdist)"
	@echo "make test-fast-parallel-strict - same as above, fail if pytest-xdist missing"
	@echo "make test-integration      - integration tests"
	@echo "make test-slow             - slow tests (not integration)"
	@echo "make test-slow-integration - slow integration tests"
	@echo "make test-all              - all tests except slow"
	@echo "make quality               - repository baseline lint, strict migrated lint, format and type checks"
	@echo "make coverage-fast         - fast-test branch coverage report"
	@echo "make package               - build wheel and source distribution"
	@echo "PYTEST_XDIST_WORKERS       - default: auto (0 disables xdist, 'auto' uses pytest-xdist default)"

test: test-fast

quality: lint lint-strict format-check type-check

lint:
	$(PYTHON) -m ruff check stock_analyzer app.py

lint-strict:
	$(PYTHON) -m ruff check --select E,F,I,B,UP --ignore E501 $(MODERNIZED_PYTHON_FILES)

format-check:
	$(PYTHON) -m ruff format --check $(MODERNIZED_PYTHON_FILES)

type-check:
	$(PYTHON) -m mypy
	$(PYTHON) -m mypy --follow-imports=skip $(ISOLATED_MYPY_FILES)

coverage-fast:
	$(PYTHON) -m coverage run -m pytest -m "not (slow or integration)"
	$(PYTHON) -m coverage report

package:
	$(PYTHON) -m build

test-fast:
	PYTEST_XDIST_WORKERS=$(PYTEST_XDIST_WORKERS) ./scripts/test.sh fast

test-fast-parallel:
	PYTEST_XDIST_WORKERS=$(PYTEST_XDIST_WORKERS) ./scripts/test.sh fast-parallel

test-fast-parallel-strict:
	PYTEST_XDIST_REQUIRED=1 PYTEST_XDIST_WORKERS=$(PYTEST_XDIST_WORKERS) ./scripts/test.sh fast-parallel

test-integration:
	PYTEST_XDIST_WORKERS=$(PYTEST_XDIST_WORKERS) ./scripts/test.sh integration

test-slow:
	PYTEST_XDIST_WORKERS=$(PYTEST_XDIST_WORKERS) ./scripts/test.sh slow

test-slow-integration:
	PYTEST_XDIST_WORKERS=$(PYTEST_XDIST_WORKERS) ./scripts/test.sh slow-integration

test-all:
	PYTEST_XDIST_WORKERS=$(PYTEST_XDIST_WORKERS) ./scripts/test.sh all
