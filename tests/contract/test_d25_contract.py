from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DESIGN = ROOT / "docs" / "02_工程设计.md"
STRATEGY = ROOT / "docs" / "01_评分逻辑.md"
BOOTSTRAP = ROOT / "src" / "trader" / "bootstrap.py"


def test_authoritative_contract_defines_d25_freeze_recovery_and_isolation() -> None:
    design = DESIGN.read_text(encoding="utf-8")
    strategy = STRATEGY.read_text(encoding="utf-8")
    compact_strategy = " ".join(strategy.split())

    for token in (
        "Tomorrow/D25分别持有输入身份",
        "合法空结果与非空结果使用相同提交语义",
        "同日正式记录缺失且没有待重试封口",
        "完整同日 official-close 输入执行一次纯本地补算",
    ):
        assert token in design
    for token in (
        "d25 原生决策",
        "strategy=d25",
        "V2/V3 均由本档独立 D25 head",
        "不得作为 D25 输入或降级源",
    ):
        assert token in compact_strategy


def test_production_composition_installs_d25_and_unified_query() -> None:
    source = BOOTSTRAP.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}

    assert "trader.recommendation.application.pipeline.freeze_publish.freeze_coordinator" in imports
    assert "decision_queries=publication.decision_queries" in source
    assert "decision_events=publication.decision_events" in source
    assert "strategy=Strategy.D25" in source
