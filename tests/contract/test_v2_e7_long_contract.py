from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "docs" / "implementation-plan.md"
DESIGN = ROOT / "docs" / "software-business-design.md"
STRATEGY = ROOT / "docs" / "recommendation-strategy.md"
BOOTSTRAP = ROOT / "src" / "trader" / "bootstrap.py"
LONG_RUNTIME = ROOT / "src" / "trader" / "application" / "long_v2_runtime.py"


def test_v2_e7_is_complete() -> None:
    plan = PLAN.read_text(encoding="utf-8")

    assert "V2-E0 至 V2-E11" in plan
    assert "V2-E10：删除旧生产链" in plan
    assert "### V2-E7：Long 正式接管（已完成）" in plan


def test_authoritative_contract_defines_long_v2_current_only_boundary() -> None:
    design = DESIGN.read_text(encoding="utf-8")
    strategy = STRATEGY.read_text(encoding="utf-8")

    for token in (
        "LongRefreshRequest",
        "LongProjection",
        "score_status=not_applicable",
        "不创建正式记录",
        "同交易日最近有效报价",
    ):
        assert token in design
    for token in (
        "long v2 原生当前投影",
        "完整固定名单",
        "不得自动换股",
        "物理 HTTP 请求始终为 0",
    ):
        assert token in strategy


def test_production_composition_installs_long_v2_without_legacy_snapshot_publication() -> None:
    source = BOOTSTRAP.read_text(encoding="utf-8")
    runtime_source = LONG_RUNTIME.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}

    assert "trader.application.long_v2_runtime" in imports
    assert "long_v2_runtime=publication.long_runtime" in source
    assert "V2DecisionBuildDependencies(" in source
    assert "publication.long_runtime," in source
    for forbidden in ("RecommendationSnapshot", "DeepSeek", "freeze", "repository", "settle"):
        assert forbidden not in runtime_source
