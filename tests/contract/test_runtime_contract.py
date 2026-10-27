from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "src" / "trader" / "recommendation" / "application" / "runtime" / "scheduler_runtime.py"


def test_runtime_is_application_owned_without_infrastructure_or_web_imports() -> None:
    tree = ast.parse(RUNTIME.read_text(encoding="utf-8"))
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module is not None}

    # Shutdown values are a shared technical primitive, not a business adapter.
    assert all(
        name == "trader.infra.shutdown"
        or not name.startswith(("trader.infra", "trader.web", "flask", "stock_analyzer"))
        for name in imports
    )
