from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "docs" / "implementation-plan.md"
DESIGN = ROOT / "docs" / "software-business-design.md"
STRATEGY = ROOT / "docs" / "recommendation-strategy.md"
BOOTSTRAP = ROOT / "src" / "trader" / "bootstrap.py"


def test_v2_e4_remains_complete_after_e5_progression() -> None:
    plan = PLAN.read_text(encoding="utf-8")

    assert "### V2-E4：Tomorrow 正式接管（已完成）" in plan


def test_authoritative_contract_uses_one_v2_identity_for_current_freeze_and_trace() -> None:
    design = DESIGN.read_text(encoding="utf-8")
    strategy = STRATEGY.read_text(encoding="utf-8")

    for token in (
        "V2SchedulerRuntime",
        "V2MarketDataAdapter",
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


def test_tomorrow_freeze_control_is_owned_by_the_v2_scheduler() -> None:
    source = (ROOT / "src" / "trader" / "application" / "v2_runtime.py").read_text(encoding="utf-8")
    assert "self._dependencies.freezes.freeze" in source
    assert "submit_due" in source


def test_retired_strategy_specific_tomorrow_runtime_is_absent() -> None:
    assert not (ROOT / "src/trader/application/tomorrow_v2_runtime.py").exists()
