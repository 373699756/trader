from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "docs" / "implementation-plan.md"
DESIGN = ROOT / "docs" / "software-business-design.md"
STRATEGY = ROOT / "docs" / "recommendation-strategy.md"
BOOTSTRAP = ROOT / "src" / "trader" / "bootstrap.py"
PIPELINE_STAGES = ROOT / "src" / "trader" / "application" / "pipeline_stages.py"


def test_v2_e4_remains_complete_after_e5_progression() -> None:
    plan = PLAN.read_text(encoding="utf-8")

    assert "### V2-E4：Tomorrow 正式接管（已完成）" in plan


def test_authoritative_contract_uses_one_v2_identity_for_current_freeze_and_trace() -> None:
    design = DESIGN.read_text(encoding="utf-8")
    strategy = STRATEGY.read_text(encoding="utf-8")

    for token in (
        "TomorrowV2Runtime",
        "local ScoredDecision",
        "V2DecisionCheckpoint",
        "CommittedDecisionRecord",
        "14:49:20",
        "15:00 后",
    ):
        assert token in design
    assert "`UnifiedDecisionIndex` CAS 的最新完整 `ScoredDecision`" in strategy


def test_production_composition_has_no_tomorrow_shadow_or_cutover_dependencies() -> None:
    tree = ast.parse(BOOTSTRAP.read_text(encoding="utf-8"))
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module is not None}

    forbidden = (
        "trader.application.tomorrow_shadow",
        "trader.application.tomorrow_shadow_runtime",
        "trader.infra.persistence.tomorrow_shadow_evidence",
        "trader.infra.persistence.tomorrow_decision_freezes",
        "trader.application.current_decisions",
    )
    assert not imports.intersection(forbidden)


def test_tomorrow_freeze_control_seals_before_the_boundary_scoring_offer() -> None:
    source = PIPELINE_STAGES.read_text(encoding="utf-8")

    first_control = source.index("for control in pipeline._v2_controls:")
    scoring = source.index("snapshots = list(_score_strategies_on_workers")
    second_control = source.index("for control in pipeline._v2_controls:", first_control + 1)
    assert first_control < scoring < second_control
