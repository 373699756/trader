SHELL := /bin/bash
PYTHON ?= .venv/bin/python
SOURCE_PATHS := src/trader tests scripts/check_refactor_quality.py scripts/generate_long_watchlist_asset.py \
	scripts/measure_web_refresh_interval.py scripts/sample_tencent_quotes.py

.PHONY: help install-dev format format-check lint long-watchlist-check type-check test quality package

help:
	@echo "make install-dev   - install editable package and development tools"
	@echo "make format        - format Python sources and tests"
	@echo "make long-watchlist-check - verify the packaged long-watchlist asset"
	@echo "make quality       - format, lint, type and test gates"
	@echo "make package       - build wheel and source distribution"

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

quality: format-check lint type-check test

package:
	$(PYTHON) -m build
