SHELL := /bin/bash
PYTHON ?= .venv/bin/python
SOURCE_PATHS := src/trader tests

.PHONY: help install-dev format format-check lint type-check test quality package

help:
	@echo "make install-dev   - install editable package and development tools"
	@echo "make format        - format Python sources and tests"
	@echo "make quality       - format, lint, type and test gates"
	@echo "make package       - build wheel and source distribution"

install-dev:
	$(PYTHON) -m pip install -e ".[dev]"

format:
	$(PYTHON) -m ruff format $(SOURCE_PATHS)
	$(PYTHON) -m ruff check --select E,F,I,B,UP --ignore E501 --fix $(SOURCE_PATHS)

format-check:
	$(PYTHON) -m ruff format --check $(SOURCE_PATHS)

lint:
	$(PYTHON) -m ruff check --select E,F,I,B,UP --ignore E501 $(SOURCE_PATHS)

type-check:
	$(PYTHON) -m mypy src/trader

test:
	$(PYTHON) -m pytest -q tests

quality: format-check lint type-check test

package:
	$(PYTHON) -m build
