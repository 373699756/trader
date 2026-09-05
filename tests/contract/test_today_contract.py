from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DESIGN = ROOT / "docs" / "software-business-design.md"
STRATEGY = ROOT / "docs" / "recommendation-strategy.md"
BOOTSTRAP = ROOT / "src" / "trader" / "bootstrap.py"


def test_authoritative_contract_defines_today_missed_freeze_and_overlay_only_behavior() -> None:
    design = DESIGN.read_text(encoding="utf-8")
    strategy = STRATEGY.read_text(encoding="utf-8")

    for token in (
        "SchedulerRuntime",
        "MarketDataAdapter",
        "11:19:59",
        "11:20:00",
        "missed_freeze",
        "禁止 checkpoint",
        "DecisionOverlay",
    ):
        assert token in design
    for token in ("Today 原生输入", "11:20", "not_ready", "只更新报价 overlay"):
        assert token in strategy


def test_production_composition_installs_today_without_legacy_today_scoring() -> None:
    source = BOOTSTRAP.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module is not None}

    assert "trader.application.recommendation.today_freezing" in imports


def test_today_freeze_control_is_part_of_the_scheduler_dependencies() -> None:
    source = BOOTSTRAP.read_text(encoding="utf-8")
    assert "FreezeAdapter(" in source
    assert "publication.today_freezer" in source


def test_retired_strategy_specific_today_runtime_is_absent() -> None:
    assert not (ROOT / "src/trader/application/today_scheduler_runtime.py").exists()
