from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from trader.recommendation.domain.risk.decision import (
    PredictionInterval,
    RiskDecision,
    UncertaintyAssessment,
)
from trader.recommendation.domain.scoring.alpha import AlphaScore
from trader.recommendation.domain.selection.execution_cost import ExecutionCost, ExecutionCostScenario
from trader.training.domain.evaluation.point_in_time_dataset import PointInTimeEventFact
from trader.training.domain.evaluation.risk_cost_population import RiskCostResearchIdentity
from trader.training.domain.evaluation.risk_cost_uncertainty import (
    DeepSeekResearchReview,
    RiskCostUncertaintyReport,
    RiskCostUncertaintySample,
    build_selection_utility,
)
from trader.training.domain.evaluation.risk_cost_uncertainty import (
    evaluate_risk_cost_uncertainty as evaluate_bound_risk_cost,
)


def evaluate_risk_cost_uncertainty(
    parent_hash: str, model_hash: str, samples: tuple[RiskCostUncertaintySample, ...]
) -> RiskCostUncertaintyReport:
    """Numerical fixtures declare synthetic identities; application tests prove parent binding."""
    identity = RiskCostResearchIdentity(
        "e" * 64,
        "f" * 64,
        "0" * 64,
        "calibration",
        tuple(sorted({item.trade_date for item in samples})),
        "v3",
        "industry_ridge_lightgbm",
        model_hash,
    )
    return evaluate_bound_risk_cost(parent_hash, model_hash, samples, research_identity=identity)


def _sample(
    code: str,
    *,
    signal_score: float,
    actual_net: float,
    severe: bool,
    review: DeepSeekResearchReview,
    trade_date: date = date(2026, 8, 31),
) -> RiskCostUncertaintySample:
    alpha = AlphaScore(code, "v3", "industry_ridge_lightgbm", "d" * 64, "b" * 64, signal_score, 0.01)
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
        dataset_row_hash="1" * 64,
        structured_facts=tuple(
            _fact(fact_id, trade_date) for fact_id in sorted(set(risk.structured_fact_ids + review.structured_fact_ids))
        ),
    )


def _fact(fact_id: str, trade_date: date) -> PointInTimeEventFact:
    anchor = datetime.combine(trade_date, datetime.min.time(), ZoneInfo("Asia/Shanghai")).replace(hour=15)
    return PointInTimeEventFact(fact_id, anchor, anchor, anchor, "a" * 64)


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


def test_deepseek_opportunity_cost_uses_exact_crossing_constraint_oracle() -> None:
    review = DeepSeekResearchReview("failed", None, 0.0, False, (), "ignored")
    rows = tuple(
        replace(
            _sample(code, signal_score=100.0 - index, actual_net=net, severe=False, review=review),
            board=board,
            industry=industry,
        )
        for index, (code, board, industry, net) in enumerate(
            (
                ("600001", "main", "X", 0.10),
                ("600002", "main", "X", 0.09),
                ("600003", "main", "Y", 0.08),
                ("600004", "main", "Z", 0.07),
                ("300001", "chinext", "X", 0.06),
                ("300002", "chinext", "X", 0.05),
            )
        )
    )

    report = evaluate_risk_cost_uncertainty("c" * 64, "d" * 64, rows)

    assert all(item.selected_count == 3 for item in report.ablation)
    assert all(item.opportunity_cost == pytest.approx(0.04) for item in report.ablation)
    for arm in report.ablation:
        day = arm.cost_sensitivity[0].days[0]
        assert day.slot_net_excess_return == pytest.approx(0.27 / 6)
        assert day.oracle_slot_net_excess_return == pytest.approx(0.31 / 6)
        assert day.slot_opportunity_cost == pytest.approx(0.04 / 6)


def test_cost_sensitivity_counts_empty_days_and_unused_slots_without_dropping_stress_losses() -> None:
    review = DeepSeekResearchReview("failed", None, 0.0, False, (), "ignored")
    first = _sample("600001", signal_score=90, actual_net=0.001, severe=False, review=review)
    second = replace(first, trade_date=date(2026, 9, 1), risk=replace(first.risk, veto=True))

    report = evaluate_risk_cost_uncertainty("c" * 64, "d" * 64, (first, second))

    for arm in report.ablation:
        assert arm.mean_selected_net_excess_return == pytest.approx(0.001)
        assert tuple(item.cost_bps for item in arm.cost_sensitivity) == (20, 50, 100)
        assert all(item.empty_day_count == 1 for item in arm.cost_sensitivity)
        for scenario, net in zip(arm.cost_sensitivity, (0.001, -0.002, -0.007), strict=True):
            assert scenario.mean_daily_slot_net_excess_return == pytest.approx(net / 6 / 2)
            assert tuple(day.selected_count for day in scenario.days) == (1, 0)
            assert scenario.days[1].slot_net_excess_return == 0.0
            assert scenario.days[1].slot_opportunity_cost == 0.0
        assert arm.cost_sensitivity[2].days[0].oracle_slot_net_excess_return == 0.0
        assert arm.cost_sensitivity[2].mean_daily_slot_opportunity_cost == pytest.approx(0.007 / 6 / 2)


def test_cost_scenarios_do_not_rerank_or_restore_a_structured_veto() -> None:
    veto = DeepSeekResearchReview("applied", 99, 0, True, ("verified_fact",), "ignored")
    first = _sample("600001", signal_score=90, actual_net=0.02, severe=False, review=veto)

    report = evaluate_risk_cost_uncertainty("c" * 64, "d" * 64, (first,))
    local, facts, fixed = report.ablation

    assert local.selected_count == 1
    assert facts.selected_count == fixed.selected_count == 0
    for local_cost, facts_cost, fixed_cost in zip(
        local.cost_sensitivity, facts.cost_sensitivity, fixed.cost_sensitivity, strict=True
    ):
        assert facts_cost.empty_day_count == fixed_cost.empty_day_count == 1
        assert facts_cost.mean_daily_slot_net_excess_return == fixed_cost.mean_daily_slot_net_excess_return == 0.0
        assert facts_cost.days[0].oracle_slot_net_excess_return == local_cost.days[0].oracle_slot_net_excess_return
        assert fixed_cost.days[0].oracle_slot_net_excess_return == local_cost.days[0].oracle_slot_net_excess_return


@pytest.mark.parametrize("bad_cost", (None, 0.006))
def test_missing_or_mislabeled_cost_scenario_fails_closed(bad_cost: float | None) -> None:
    review = DeepSeekResearchReview("failed", None, 0.0, False, (), "ignored")
    row = _sample("600001", signal_score=90, actual_net=0.02, severe=False, review=review)
    scenarios = tuple(item for item in row.cost.scenarios if item.scenario_id != "cost_50bp")
    if bad_cost is not None:
        scenarios = (*scenarios, ExecutionCostScenario("cost_50bp", bad_cost))
    row = replace(row, cost=replace(row.cost, scenarios=scenarios))

    with pytest.raises(ValueError, match="canonical 20/50/100bp"):
        evaluate_risk_cost_uncertainty("c" * 64, "d" * 64, (row,))


def test_fixed_fusion_uses_decimal_half_up_without_a_second_local_risk_deduction() -> None:
    review = DeepSeekResearchReview("applied", 77.03125, 0, False, (), "ignored")
    row = _sample("600001", signal_score=57.79, actual_net=0.02, severe=False, review=review)

    report = evaluate_risk_cost_uncertainty("c" * 64, "d" * 64, (row,))

    assert build_selection_utility(row.alpha, row.risk, row.cost).local_score == 47.79
    decision = next(item for item in report.decisions if item.arm == "fixed_68_32")
    # 47.79 * 0.68 + 77.03125 * 0.32 = 57.1472; use an exact half-cent next.
    tied = replace(row, deepseek=replace(review, score=77.024375))
    tied_report = evaluate_risk_cost_uncertainty("c" * 64, "d" * 64, (tied,))
    assert decision.score == 57.15
    assert next(item for item in tied_report.decisions if item.arm == "fixed_68_32").score == 57.15

    golden = replace(
        row,
        alpha=replace(row.alpha, signal_score=100),
        deepseek=replace(review, score=81.875, structured_risk_penalty=4, structured_fact_ids=("verified_fact",)),
        structured_facts=(_fact("local_tail_risk", row.trade_date), _fact("verified_fact", row.trade_date)),
    )
    golden_report = evaluate_risk_cost_uncertainty("c" * 64, "d" * 64, (golden,))
    assert next(item for item in golden_report.decisions if item.arm == "fixed_68_32").score == 83.40


def test_cost_report_uses_fixed_slots_with_unequal_daily_selected_counts() -> None:
    review = DeepSeekResearchReview("failed", None, 0.0, False, (), "ignored")
    first = _sample("600001", signal_score=90, actual_net=0.03, severe=False, review=review)
    next_day = tuple(
        _sample(
            f"{600010 + index:06d}",
            signal_score=90 - index,
            actual_net=0.01,
            severe=False,
            review=review,
            trade_date=date(2026, 9, 1),
        )
        for index in range(3)
    )

    report = evaluate_risk_cost_uncertainty("c" * 64, "d" * 64, (first, *next_day))

    for arm in report.ablation:
        cost = arm.cost_sensitivity[0]
        assert tuple(item.selected_count for item in cost.days) == (1, 3)
        assert cost.mean_daily_slot_net_excess_return == pytest.approx(0.03 / 6)
        assert arm.mean_selected_net_excess_return == pytest.approx(0.015)
        assert cost.empty_day_count == 0


@pytest.mark.parametrize("outcome", ("failed", "late", "budget_exhausted", "abstained"))
def test_every_fallback_keeps_local_cost_results_and_score(outcome: str) -> None:
    review = DeepSeekResearchReview(outcome, None, 0.0, False, (), "ignored")
    row = _sample("600001", signal_score=90, actual_net=0.02, severe=False, review=review)
    report = evaluate_risk_cost_uncertainty("c" * 64, "d" * 64, (row,))

    assert len({item.score for item in report.decisions}) == 1
    assert all(item.cost_sensitivity == report.ablation[0].cost_sensitivity for item in report.ablation)
    assert report.ablation[0].local_fallback_count == 0
    assert report.ablation[1].local_fallback_count == report.ablation[2].local_fallback_count == 1


def test_cost_report_rejects_partial_dates_false_aggregate_and_changed_oracle() -> None:
    review = DeepSeekResearchReview("failed", None, 0.0, False, (), "ignored")
    first = _sample("600001", signal_score=90, actual_net=0.02, severe=False, review=review)
    second = replace(first, trade_date=date(2026, 9, 1))
    report = evaluate_risk_cost_uncertainty("c" * 64, "d" * 64, (first, second))
    arm = report.ablation[0]
    scenario = arm.cost_sensitivity[0]
    with pytest.raises(ValueError, match="aggregates"):
        replace(scenario, mean_daily_slot_net_excess_return=99)
    with pytest.raises(ValueError, match="20/50/100bp"):
        replace(arm, cost_sensitivity=arm.cost_sensitivity[:2])
    with pytest.raises(ValueError, match="below realized"):
        replace(scenario.days[0], oracle_slot_net_excess_return=0.0)
    changed_day = replace(
        scenario.days[0],
        oracle_slot_net_excess_return=0.1,
        slot_opportunity_cost=0.1 - scenario.days[0].slot_net_excess_return,
    )
    changed = replace(
        scenario,
        days=(changed_day, scenario.days[1]),
        mean_daily_slot_opportunity_cost=(changed_day.slot_opportunity_cost + scenario.days[1].slot_opportunity_cost)
        / 2,
    )
    with pytest.raises(ValueError, match="share one oracle"):
        replace(
            report, ablation=(replace(arm, cost_sensitivity=(changed, *arm.cost_sensitivity[1:])), *report.ablation[1:])
        )
    shifted = replace(scenario, days=(scenario.days[0], replace(scenario.days[1], trade_date=date(2026, 9, 2))))
    with pytest.raises(ValueError, match="every evaluated date"):
        replace(
            report, ablation=(replace(arm, cost_sensitivity=(shifted, *arm.cost_sensitivity[1:])), *report.ablation[1:])
        )


def test_numerical_kernel_requires_model_and_partition_dates_and_hashes_parent_row() -> None:
    review = DeepSeekResearchReview("failed", None, 0, False, (), "ignored")
    sample = _sample("600001", signal_score=90, actual_net=0.02, severe=False, review=review)
    report = evaluate_risk_cost_uncertainty("c" * 64, "d" * 64, (sample,))
    assert report.research_identity is not None
    with pytest.raises(ValueError, match="model identity"):
        evaluate_bound_risk_cost(
            "c" * 64,
            "d" * 64,
            (replace(sample, alpha=replace(sample.alpha, model_hash="e" * 64)),),
            research_identity=report.research_identity,
        )
    with pytest.raises(ValueError, match="partition dates"):
        evaluate_bound_risk_cost(
            "c" * 64,
            "d" * 64,
            (sample,),
            research_identity=replace(report.research_identity, dates=(date(2026, 9, 1),)),
        )
    changed = evaluate_risk_cost_uncertainty("c" * 64, "d" * 64, (replace(sample, dataset_row_hash="2" * 64),))
    assert changed.evidence_hash != report.evidence_hash
    assert changed.content_hash != report.content_hash
    assert changed.ablation == report.ablation
    with pytest.raises(ValueError, match="bound research identity"):
        replace(report, research_identity=None)


def test_fact_references_require_exact_unique_typed_coverage_and_change_evidence_identity() -> None:
    sample = _sample(
        "600001",
        signal_score=90,
        actual_net=0.02,
        severe=False,
        review=DeepSeekResearchReview("failed", None, 0, False, (), "ignored"),
    )
    fact = sample.structured_facts[0]
    for facts in ((), (fact, fact), (fact, _fact("unreferenced_fact", sample.trade_date))):
        with pytest.raises(ValueError, match="exactly the unique"):
            replace(sample, structured_facts=facts)
    original = evaluate_risk_cost_uncertainty("c" * 64, "d" * 64, (sample,))
    changed = evaluate_risk_cost_uncertainty(
        "c" * 64,
        "d" * 64,
        (replace(sample, structured_facts=(replace(fact, content_hash="b" * 64),)),),
    )
    assert changed.evidence_hash != original.evidence_hash
    assert changed.content_hash != original.content_hash
    assert changed.ablation == original.ablation
