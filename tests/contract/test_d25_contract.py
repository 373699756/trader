from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DESIGN = ROOT / "docs" / "software-business-design.md"
STRATEGY = ROOT / "docs" / "recommendation-strategy.md"
BOOTSTRAP = ROOT / "src" / "trader" / "bootstrap.py"


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
        "d25 原生决策",
        "strategy=d25",
        "不新增模型请求链",
        "不得作为 D25 输入或降级源",
    ):
        assert token in strategy


def test_production_composition_installs_d25_and_unified_query() -> None:
    source = BOOTSTRAP.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}

    assert "trader.application.recommendation.scored_freezing" in imports
    assert "decision_queries=publication.decision_queries" in source
    assert "decision_events=publication.decision_events" in source
    assert "strategy=Strategy.D25" in source
