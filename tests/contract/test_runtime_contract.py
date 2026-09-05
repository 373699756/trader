from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DESIGN = ROOT / "docs" / "software-business-design.md"
RUNTIME = ROOT / "src" / "trader" / "application" / "runtime" / "scheduler_runtime.py"


def test_authoritative_contract_freezes_runtime_capacity_and_shutdown() -> None:
    design = DESIGN.read_text(encoding="utf-8")

    for token in (
        "SchedulerRuntime",
        "每策略一个运行中任务和一个 latest-wins 待处理槽",
        "tomorrow 独占完整决策 lane",
        "SharedDeepSeekRuntimeContract",
        "daily_physical_limit=168",
        "AsyncDecisionObserver",
        "同一个 `ShutdownDeadline`",
    ):
        assert token in design


def test_runtime_is_application_owned_without_infrastructure_or_web_imports() -> None:
    tree = ast.parse(RUNTIME.read_text(encoding="utf-8"))
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module is not None}

    assert all(not name.startswith(("trader.infra", "trader.web", "flask", "stock_analyzer")) for name in imports)
