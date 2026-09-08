from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DESIGN = ROOT / "docs" / "02_工程设计.md"
RUNTIME = ROOT / "src" / "trader" / "application" / "runtime" / "scheduler_runtime.py"


def test_authoritative_contract_freezes_runtime_capacity_and_shutdown() -> None:
    design = DESIGN.read_text(encoding="utf-8")

    for token in (
        "SchedulerRuntime",
        "每策略一个 running 加一个 pending",
        "尚未开始的旧 pending 可以被最新输入替换",
        "DeepSeek 物理请求、预算、缓存与 single-flight",
        "全局 168 上限",
        "AsyncDecisionObserver",
        "只能读取同一个 deadline 的剩余时间",
    ):
        assert token in design


def test_runtime_is_application_owned_without_infrastructure_or_web_imports() -> None:
    tree = ast.parse(RUNTIME.read_text(encoding="utf-8"))
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module is not None}

    assert all(not name.startswith(("trader.infra", "trader.web", "flask", "stock_analyzer")) for name in imports)
