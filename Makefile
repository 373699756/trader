SHELL := /bin/bash
PYTHON ?= .venv/bin/python3
PYTEST_WORKERS ?= 4
PYTEST_DISTRIBUTION ?= loadgroup
SOURCE_PATHS := src/trader tests scripts

.PHONY: help install-dev format format-check lint long-watchlist-check type-check test test-fast test-train test-history test-recommendation test-recommendation-runtime test-recommendation-suppliers test-static-contracts test-full test-unit test-component test-contract test-integration test-release quality package performance-check browser-performance-check diagnose-list diagnose-local

help:
	@echo "make install-dev   - install editable package and development tools"
	@echo "make format        - format Python sources and tests"
	@echo "make long-watchlist-check - verify the packaged long-watchlist asset"
	@echo "make quality       - format, lint, type and test gates"
	@echo "make test          - run the default fast test set"
	@echo "make test-fast     - run the fast local test set across business owners"
	@echo "make test-train    - run training, research and evaluation tests"
	@echo "make test-history  - run download, archive and conversion tests"
	@echo "make test-recommendation - run recommendation, delivery and Web tests"
	@echo "make test-recommendation-runtime - run recommendation scheduler and worker timing tests"
	@echo "make test-recommendation-suppliers - run recommendation supplier and gateway timing tests"
	@echo "make test-static-contracts - run repository-wide naming and static source scans"
	@echo "make test-full     - run all tests including slow tests; format_tools require explicit --run-format-tools"
	@echo "make test-unit     - run unit tests"
	@echo "make test-component - run component tests"
	@echo "make test-contract  - run contract tests"
	@echo "make test-integration - run integration tests"
	@echo "make test-release  - build and verify the wheel outside the repository"
	@echo "make package       - build wheel and source distribution"
	@echo "make performance-check - run the offline active-production performance gate"
	@echo "make browser-performance-check - run Firefox SSE patch-to-paint and refresh gate"
	@echo "make diagnose-list - list explicit diagnostic selections"
	@echo "make diagnose-local - run the explicitly selected local snapshot checks"

install-dev:
	$(PYTHON) -m pip install -e ".[dev]"

format:
	$(PYTHON) -m ruff format $(SOURCE_PATHS)
	$(PYTHON) -m ruff check --select E,F,I,B,UP --ignore E501 --fix $(SOURCE_PATHS)

format-check:
	$(PYTHON) -m ruff format --check $(SOURCE_PATHS)

lint: long-watchlist-check
	$(PYTHON) -m ruff check --select E,F,I,B,UP --ignore E501 $(SOURCE_PATHS)
	$(PYTHON) scripts/tools.py --business engineering --module quality --action debt

long-watchlist-check:
	$(PYTHON) scripts/tools.py --business recommendation --module assets --action consistency

type-check:
	$(PYTHON) -m mypy src/trader

test: test-fast

test-fast:
	$(PYTHON) -m pytest -q -n $(PYTEST_WORKERS) --dist $(PYTEST_DISTRIBUTION) tests -m "not slow"

test-train:
	$(PYTHON) -m pytest -q -n $(PYTEST_WORKERS) --dist $(PYTEST_DISTRIBUTION) tests/training

test-history:
	$(PYTHON) -m pytest -q -n $(PYTEST_WORKERS) --dist $(PYTEST_DISTRIBUTION) tests/download

test-recommendation:
	$(PYTHON) -m pytest -q -n $(PYTEST_WORKERS) --dist $(PYTEST_DISTRIBUTION) tests/recommendation tests/http_api tests/web

test-recommendation-runtime:
	$(PYTHON) -m pytest -q -n $(PYTEST_WORKERS) --dist $(PYTEST_DISTRIBUTION) tests/recommendation tests/infra -m "slow_runtime"

test-recommendation-suppliers:
	$(PYTHON) -m pytest -q -n $(PYTEST_WORKERS) --dist $(PYTEST_DISTRIBUTION) tests/recommendation tests/infra -m "slow_supplier"

test-static-contracts:
	$(PYTHON) -m pytest -q -n $(PYTEST_WORKERS) --dist $(PYTEST_DISTRIBUTION) tests/system/contract -m "slow_static"

test-full:
	$(PYTHON) -m pytest -q -n $(PYTEST_WORKERS) --dist $(PYTEST_DISTRIBUTION) tests

test-unit:
	$(PYTHON) -m pytest -q -n $(PYTEST_WORKERS) --dist $(PYTEST_DISTRIBUTION) tests -m "unit"

test-component:
	$(PYTHON) -m pytest -q -n $(PYTEST_WORKERS) --dist $(PYTEST_DISTRIBUTION) tests -m "component"

test-contract:
	$(PYTHON) -m pytest -q -n $(PYTEST_WORKERS) --dist $(PYTEST_DISTRIBUTION) tests -m "contract"

test-integration:
	$(PYTHON) -m pytest -q -n $(PYTEST_WORKERS) --dist $(PYTEST_DISTRIBUTION) tests -m "integration"

test-release: package
	$(PYTHON) scripts/tools.py --business engineering --module packaging --action install -- --dist-dir dist

quality: format-check lint type-check test

package:
	$(PYTHON) -m build

performance-check:
	$(PYTHON) scripts/tools.py --business recommendation --module pipeline --action latency --runtime-config config/runtime.json

browser-performance-check:
	$(PYTHON) scripts/tools.py --business recommendation --module web --action browser --browser-duration-seconds 35 --browser-minimum-updates 3 --report -

diagnose-list:
	$(PYTHON) scripts/tools.py --list

diagnose-local:
	$(PYTHON) scripts/tools.py --business recommendation --module pipeline --action snapshot --report -
