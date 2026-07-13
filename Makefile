SHELL := /bin/bash
PYTEST_XDIST_WORKERS ?= auto

.PHONY: help test-fast test-fast-parallel test-fast-parallel-strict test-integration test-slow test-slow-integration test-all
.PHONY: test

help:
	@echo "make test-fast             - fast tests (not slow and not integration)"
	@echo "make test-fast-parallel    - parallel fast tests (requires pytest-xdist)"
	@echo "make test-fast-parallel-strict - same as above, fail if pytest-xdist missing"
	@echo "make test-integration      - integration tests"
	@echo "make test-slow             - slow tests (not integration)"
	@echo "make test-slow-integration - slow integration tests"
	@echo "make test-all              - all tests except slow"
	@echo "PYTEST_XDIST_WORKERS       - default: auto (0 disables xdist, 'auto' uses pytest-xdist default)"

test: test-fast

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
