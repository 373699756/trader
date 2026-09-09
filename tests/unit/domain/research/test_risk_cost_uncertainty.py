from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from trader.domain.recommendation.risk_fusion.decision import (
    PredictionInterval,
    RiskDecision,
    UncertaintyAssessment,
)
from trader.domain.recommendation.scoring.alpha import AlphaScore
from trader.domain.recommendation.selection.execution_cost import ExecutionCost, ExecutionCostScenario
from trader.domain.research.risk_cost_uncertainty import (
    DeepSeekResearchReview,
    RiskCostUncertaintySample,
    build_selection_utility,
    evaluate_risk_cost_uncertainty,
)


def _sample(
    code: str,
    *,
    signal_score: float,
    actual_net: float,
    severe: bool,
    review: DeepSeekResearchReview,
    trade_date: date = date(2026, 8, 31),
) -> RiskCostUncertaintySample:
    alpha = AlphaScore(code, "v3", "industry_ridge_lightgbm", "a" * 64, "b" * 64, signal_score, 0.01)
    uncertainty = UncertaintyAssessment(
        severe_loss_probability=0.8 if severe else 0.2,
        prediction_interval=PredictionInterval(-0.04, 0.03),
        model_disagreement=0.01,
        training_window_disagreement=0.02,
        ood_distance=0.1,
        missing_uncertainty=0.0,
    )
    risk = RiskDecision(code, 10.0, False, ("local_tail_risk",), uncertainty)
    cost = ExecutionCost(
        code,
        0.002,
        10_000_000.0,
        0.01,
        (
            ExecutionCostScenario("cost_20bp", 0.002),
            ExecutionCostScenario("cost_50bp", 0.005),
            ExecutionCostScenario("cost_100bp", 0.01),
        ),
    )
    return RiskCostUncertaintySample(
        trade_date,
        "main",
        f"industry_{code}",
        alpha,
        risk,
        cost,
        review,
        actual_alpha_return=actual_net + 0.002,
        actual_net_excess_return=actual_net,
        actual_severe_loss=severe,
    )


def test_signal_risk_cost_and_research_utility_keep_units_and_ownership_separate() -> None:
    review = DeepSeekResearchReview("failed", None, 0.0, False, (), "看多，不应产生扣分")
    sample = _sample("600001", signal_score=90.0, actual_net=0.01, severe=False, review=review)

    utility = build_selection_utility(sample.alpha, sample.risk, sample.cost)

    assert sample.alpha.signal_score == 90.0
    assert sample.risk.penalty_points == 10.0
    assert sample.cost.estimated_round_trip_return == 0.002
    assert utility.local_score == 80.0
    assert utility.expected_net_excess_return == pytest.approx(0.008)
    assert utility.eligible is True


def test_uncertainty_and_deepseek_ablation_reports_all_required_metrics_and_fallbacks() -> None:
    applied = DeepSeekResearchReview(
        "applied",
        95.0,
        3.0,
        False,
        ("verified_regulatory_fact",),
        "任意自由文本",
    )
    failed = DeepSeekResearchReview("failed", None, 0.0, False, (), "强烈看空并扣一百分")
    samples = (
        _sample("600001", signal_score=90.0, actual_net=0.02, severe=False, review=applied),
        _sample("600002", signal_score=70.0, actual_net=-0.03, severe=True, review=failed),
    )

    report = evaluate_risk_cost_uncertainty("c" * 64, "d" * 64, samples)

    assert report.status == "evaluated"
    assert report.evidence_hash is not None
    assert report.calibration.brier_score == pytest.approx(0.04)
    assert report.calibration.expected_calibration_error == pytest.approx(0.2)
    assert report.calibration.probability_coverage == 1.0
    assert report.calibration.prediction_interval_coverage == 1.0
    assert report.calibration.model_disagreement_coverage == 1.0
    assert report.calibration.training_window_disagreement_coverage == 1.0
    assert report.calibration.ood_coverage == 1.0
    assert report.calibration.missing_uncertainty_coverage == 1.0
    assert {item.dimension for item in report.calibration.uncertainty_buckets} == {
        "model_disagreement",
        "training_window_disagreement",
        "ood_distance",
        "missing_uncertainty",
    }
    assert all(item.sample_count == 2 for item in report.calibration.uncertainty_buckets)
    decisions = {(item.arm, item.code): item for item in report.decisions}
    assert decisions[("fixed_68_32", "600001")].score == 81.8
    assert decisions[("fixed_68_32", "600002")].score == 60.0
    assert {item.arm for item in report.ablation} == {
        "local_only",
        "structured_facts_veto",
        "fixed_68_32",
    }
    assert all(item.opportunity_cost >= 0.0 for item in report.ablation)
    assert report.production_authority is False
    assert report.terminal_holdout_opened is False


def test_deepseek_narrative_cannot_change_penalty_veto_score_or_report_identity() -> None:
    first = DeepSeekResearchReview("applied", 80.0, 4.0, True, ("verified_fact",), "看多")
    second = replace(first, narrative="看空、建议直接扣分")

    left = evaluate_risk_cost_uncertainty(
        "c" * 64,
        "d" * 64,
        (_sample("600001", signal_score=90.0, actual_net=0.01, severe=False, review=first),),
    )
    right = evaluate_risk_cost_uncertainty(
        "c" * 64,
        "d" * 64,
        (_sample("600001", signal_score=90.0, actual_net=0.01, severe=False, review=second),),
    )

    assert left == right


def test_opportunity_cost_uses_a_separate_constrained_oracle_for_each_day() -> None:
    review = DeepSeekResearchReview("failed", None, 0.0, False, (), "ignored")
    samples = tuple(
        _sample(
            f"600{offset:03d}",
            signal_score=score,
            actual_net=actual_net,
            severe=False,
            review=review,
            trade_date=trade_date,
        )
        for trade_date, base, returns in (
            (date(2026, 8, 28), 0, (0.10, 0.09, 0.08, 0.07)),
            (date(2026, 8, 31), 4, (0.01, 0.009, 0.008, 0.007)),
        )
        for offset, score, actual_net in (
            (base + 1, 90.0, returns[0]),
            (base + 2, 80.0, returns[1]),
            (base + 3, 70.0, returns[2]),
            (base + 4, 60.0, returns[3]),
        )
    )

    report = evaluate_risk_cost_uncertainty("c" * 64, "d" * 64, samples)

    assert all(item.selected_count == 6 for item in report.ablation)
    assert all(item.opportunity_cost == pytest.approx(0.0) for item in report.ablation)


@pytest.mark.parametrize("outcome", ["failed", "late", "budget_exhausted", "abstained"])
def test_non_applied_deepseek_outcomes_cannot_carry_score_penalty_or_veto(outcome: str) -> None:
    with pytest.raises(ValueError, match="non-applied"):
        DeepSeekResearchReview(outcome, 100.0, 10.0, True, ("fact",), "ignored")
