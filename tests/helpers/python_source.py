"""Read-only source inspection shared within one pytest worker.

Only inspect checkout sources that remain unchanged for the entire test run.
Tests exercising filesystem mutation must read their own temporary files.
"""

from __future__ import annotations

import ast
from functools import cache
from pathlib import Path


@cache
def source_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@cache
def source_tree(path: Path) -> ast.Module:
    return ast.parse(source_text(path), filename=str(path))
