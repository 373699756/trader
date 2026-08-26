from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DESIGN = ROOT / "docs" / "software-business-design.md"
STRATEGY = ROOT / "docs" / "recommendation-strategy.md"
BOOTSTRAP = ROOT / "src" / "trader" / "bootstrap.py"
LONG_RUNTIME = ROOT / "src" / "trader" / "application" / "long_v2_runtime.py"


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
