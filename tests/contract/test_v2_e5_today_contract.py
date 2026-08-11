from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "docs" / "implementation-plan.md"
DESIGN = ROOT / "docs" / "software-business-design.md"
STRATEGY = ROOT / "docs" / "recommendation-strategy.md"
BOOTSTRAP = ROOT / "src" / "trader" / "bootstrap.py"
PIPELINE_STAGES = ROOT / "src" / "trader" / "application" / "pipeline_stages.py"


def test_v2_e5_remains_complete_after_e6_progression() -> None:
    plan = PLAN.read_text(encoding="utf-8")

    assert "### V2-E5：Today 正式接管（已完成）" in plan


def test_authoritative_contract_defines_today_missed_freeze_and_overlay_only_behavior() -> None:
    design = DESIGN.read_text(encoding="utf-8")
    strategy = STRATEGY.read_text(encoding="utf-8")

    for token in (
        "TodayV2Runtime",
        "11:19:59",
        "11:20:00",
        "missed_freeze",
        "禁止 checkpoint",
        "DecisionOverlay",
    ):
        assert token in design
    for token in ("Today 原生输入", "11:20", "not_ready", "只更新报价 overlay"):
        assert token in strategy


def test_production_composition_installs_today_v2_without_legacy_today_scoring() -> None:
    source = BOOTSTRAP.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module is not None}

    assert "trader.application.today_v2_runtime" in imports
    assert "v2_owned_strategies=(Strategy.TODAY, Strategy.TOMORROW, Strategy.D25)" in source


def test_all_v2_freeze_controls_run_before_and_after_scoring() -> None:
    source = PIPELINE_STAGES.read_text(encoding="utf-8")

    first_control = source.index("for control in pipeline._v2_controls:")
    scoring = source.index("snapshots = list(_score_strategies_on_workers")
    second_control = source.index("for control in pipeline._v2_controls:", first_control + 1)
    assert first_control < scoring < second_control
