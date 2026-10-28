from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_risk_cost_uncertainty_components_live_in_their_authoritative_layers() -> None:
    alpha = (ROOT / "src/trader/recommendation/domain/scoring/alpha.py").read_text(encoding="utf-8")
    risk = (ROOT / "src/trader/recommendation/domain/risk/freeze.py").read_text(encoding="utf-8")
    cost = (ROOT / "src/trader/recommendation/domain/selection/execution_cost.py").read_text(encoding="utf-8")
    research = (ROOT / "src/trader/training/domain/evaluation/risk_cost_uncertainty.py").read_text(encoding="utf-8")

    assert "class AlphaScore" in alpha
    assert "class RiskFreeze" in risk
    assert "class ExecutionCost" in cost
    assert "class SelectionUtility" in research
    assert "local_weight = 0.68" in research
    assert "deepseek_weight = 0.32" in research
