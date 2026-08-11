from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "docs" / "implementation-plan.md"
DESIGN = ROOT / "docs" / "software-business-design.md"
STRATEGY = ROOT / "docs" / "recommendation-strategy.md"
BOOTSTRAP = ROOT / "src" / "trader" / "bootstrap.py"


def test_v2_e6_is_complete() -> None:
    plan = PLAN.read_text(encoding="utf-8")

    assert "### V2-E6：D25 正式接管（已完成）" in plan


def test_authoritative_contract_defines_d25_freeze_recovery_and_isolation() -> None:
    design = DESIGN.read_text(encoding="utf-8")
    strategy = STRATEGY.read_text(encoding="utf-8")

    for token in (
        "D25NativeInput",
        "strategy=d25 + trade_date",
        "合法空结果与非空结果使用同一提交语义",
        "待重试的 14:50 封口",
        "纯本地 d25 评分",
    ):
        assert token in design
    for token in (
        "d25 v2 原生决策",
        "strategy=d25",
        "不新增模型请求链",
        "不得作为 D25 输入或降级源",
    ):
        assert token in strategy


def test_production_composition_installs_d25_v2_and_unified_query() -> None:
    source = BOOTSTRAP.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}

    assert "trader.application.tomorrow_v2_runtime" in imports
    assert "d25_queries=publication.d25_queries" in source
    assert "strategy=Strategy.D25" in source
