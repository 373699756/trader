from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_risk_cost_uncertainty_components_live_in_their_authoritative_layers() -> None:
    alpha = (ROOT / "src/trader/domain/recommendation/scoring/alpha.py").read_text(encoding="utf-8")
    risk = (ROOT / "src/trader/domain/recommendation/risk_fusion/decision.py").read_text(encoding="utf-8")
    cost = (ROOT / "src/trader/domain/recommendation/selection/execution_cost.py").read_text(encoding="utf-8")
    research = (ROOT / "src/trader/domain/research/risk_cost_uncertainty.py").read_text(encoding="utf-8")

    assert "class AlphaScore" in alpha
    assert "class RiskDecision" in risk
    assert "class ExecutionCost" in cost
    assert "class SelectionUtility" in research
    assert "local_weight = 0.68" in research
    assert "deepseek_weight = 0.32" in research


def test_authoritative_contracts_record_fail_closed_risk_cost_and_deepseek_boundaries() -> None:
    strategy = (ROOT / "docs/01_评分逻辑.md").read_text(encoding="utf-8")
    design = (ROOT / "docs/02_工程设计.md").read_text(encoding="utf-8")
    combined = strategy + design

    for required in (
        "Brier",
        "ECE",
        "预测区间覆盖率",
        "训练窗口分歧",
        "OOD",
        "缺失不确定性",
        "local-only",
        "结构化 facts veto",
        "固定 68/32",
        "自由文本不能直接扣分",
    ):
        assert required in combined
