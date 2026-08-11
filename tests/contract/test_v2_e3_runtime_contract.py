from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "docs" / "implementation-plan.md"
DESIGN = ROOT / "docs" / "software-business-design.md"
RUNTIME = ROOT / "src" / "trader" / "application" / "v2_runtime.py"


def test_v2_e3_remains_complete_after_e4_progression() -> None:
    plan = PLAN.read_text(encoding="utf-8")

    assert "V2-E0、V2-E1、V2-E2、V2-E3、V2-E4、V2-E5、Score-R0、Score-R1 已完成" in plan
    assert "下一工程章节为 V2-E6" in plan
    assert "### V2-E3：独立调度与生命周期（已完成）" in plan


def test_authoritative_contract_freezes_v2_runtime_capacity_and_shutdown() -> None:
    design = DESIGN.read_text(encoding="utf-8")

    for token in (
        "V2SchedulerRuntime",
        "每策略一个运行中任务和一个 latest-wins 待处理槽",
        "tomorrow 独占完整决策 lane",
        "SharedDeepSeekRuntimeContract",
        "daily_physical_limit=168",
        "AsyncDecisionObserver",
        "同一个 `ShutdownDeadline`",
    ):
        assert token in design


def test_v2_runtime_is_application_owned_without_infrastructure_or_web_imports() -> None:
    tree = ast.parse(RUNTIME.read_text(encoding="utf-8"))
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module is not None}

    assert all(not name.startswith(("trader.infra", "trader.web", "flask", "stock_analyzer")) for name in imports)
