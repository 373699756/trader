from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from trader.training.domain.evaluation.point_in_time_dataset import PointInTimeDateSplit
from trader.training.domain.evaluation.selection_experiment import (
    EvaluatePairedSelectionExperiment,
    PairedSelectionRow,
    PairedSelectionSpecification,
    SelectionTargetEvidence,
)


def _split() -> PointInTimeDateSplit:
    return PointInTimeDateSplit(
        (date(2025, 1, 2),),
        (date(2025, 2, 3),),
        (date(2025, 3, 3),),
        tuple(date(2025, 4, 1) + timedelta(days=index) for index in range(5)),
        (date(2026, 1, 5),),
        tuple(date(2026, 1, 6) + timedelta(days=index) for index in range(5)),
        tuple(date(2026, 1, 12) + timedelta(days=index) for index in range(200)),
    )


def _specification(strategy: str = "tomorrow") -> PairedSelectionSpecification:
    return PairedSelectionSpecification(
        strategy=strategy,  # type: ignore[arg-type]
        dataset_hash="a" * 64,
        dataset_manifest_hash="f" * 64,
        candidate_recall_hash="b" * 64,
        industry_model_id="industry_ridge",
        industry_model_hash="c" * 64,
        pooled_ridge_model_id="pooled_ridge",
        pooled_ridge_model_hash="d" * 64,
        pooled_lightgbm_model_id="pooled_lightgbm",
        pooled_lightgbm_model_hash="e" * 64,
        research_dates=(date(2026, 1, 5),),
        predefined_exit_identity="close_at_first_target_or_t5",
        profile_id="v2",
        date_split=_split(),
        partition="confirmation",
        configuration_hash="2" * 64,
        cost_reference_hash="3" * 64,
        bootstrap_master_seed=20261022,
    )


def _target(target: str, value: float) -> SelectionTargetEvidence:
    return SelectionTargetEvidence(
        target=target,  # type: ignore[arg-type]
        industry_prediction_pre_cost=value,
        pooled_ridge_prediction_pre_cost=value + 0.001,
        pooled_lightgbm_prediction_pre_cost=value + 0.002,
        actual_pre_cost_excess_return=value,
        exit_untradable=False,
    )


def _rows(strategy: str = "tomorrow") -> tuple[PairedSelectionRow, ...]:
    rows: list[PairedSelectionRow] = []
    for index in range(9):
        evidence = (
            (_target("t1", 0.018 - index / 1000),)
            if strategy == "tomorrow"
            else (
                _target("t2", 0.016 - index / 1000),
                _target("t3", 0.017 - index / 1000),
                _target("t4", 0.018 - index / 1000),
                _target("t5", 0.019 - index / 1000),
                _target("predefined_exit", 0.015 - index / 1000),
            )
        )
        rows.append(
            PairedSelectionRow(
                trade_date=date(2026, 1, 5),
                code=f"600{index:03d}",
                board=("main", "chinext", "star")[index % 3],
                industry=f"industry-{index}",
                parent_row_hash=f"{index + 1:x}" * 64,
                rule_score=float(100 - index),
                estimated_round_trip_cost=0.002,
                risk_eligible=True,
                action_eligible=True,
                capacity_sufficient=index != 0,
                execution_capacity=1_000_000.0,
                severe_loss=False,
                mae_atr20=-0.2,
                target_evidence=evidence,
                point_in_time_parity=True,
            )
        )
    return tuple(rows)


def test_tomorrow_experiment_runs_four_arms_two_pooled_families_and_two_selection_modes() -> None:
    report = EvaluatePairedSelectionExperiment(_specification(), _rows())

    assert report.status == "historical_experiment_completed"
    assert len(report.results) == 10
    assert {item.variant.arm for item in report.results} == {
        "rule_only",
        "rule_model_gate",
        "industry_model_utility",
        "pooled_model_utility",
    }
    fixed = next(
        item
        for item in report.results
        if item.variant.arm == "rule_only" and item.variant.selection_mode == "fixed_score_top6"
    )
    refilled = next(
        item
        for item in report.results
        if item.variant.arm == "rule_only" and item.variant.selection_mode == "model_gate_then_fixed_top6"
    )
    assert fixed.metrics.selected_rows == 5
    assert refilled.metrics.selected_rows == 5
    assert len(refilled.days[0].fixed_codes) == 6
    assert "600006" not in refilled.days[0].selected_codes
    assert refilled.metrics.capacity_shortfall_rate == pytest.approx(1 / 9)
    assert report.production_authority is False
    assert report.terminal_holdout_opened is False


def test_board_limit_applies_before_fixing_seats_without_an_industry_limit() -> None:
    rows = tuple(
        replace(
            row,
            board="main" if index < 6 else "star",
            industry="same-industry",
            capacity_sufficient=True,
        )
        for index, row in enumerate(_rows())
    )
    report = EvaluatePairedSelectionExperiment(_specification(), rows)
    fixed = next(
        result
        for result in report.results
        if result.variant.arm == "rule_only" and result.variant.selection_mode == "fixed_score_top6"
    )
    refilled = next(
        result
        for result in report.results
        if result.variant.arm == "rule_only" and result.variant.selection_mode == "model_gate_then_fixed_top6"
    )
    assert fixed.metrics.selected_rows == 6
    assert fixed.days[0].fixed_codes == ("600000", "600001", "600002", "600003", "600006", "600007")
    assert refilled.metrics.selected_rows == 6


def test_d25_experiment_compares_only_preregistered_realizable_targets() -> None:
    report = EvaluatePairedSelectionExperiment(_specification("d25"), _rows("d25"))

    assert len(report.results) == 40
    assert {item.variant.target for item in report.results} == {
        "mean_t2_t5",
        "fixed_t5",
        "predefined_exit",
        "multi_horizon",
    }
    aggregate = next(
        item
        for item in report.results
        if item.variant.arm == "rule_only"
        and item.variant.selection_mode == "model_gate_then_fixed_top6"
        and item.variant.target == "mean_t2_t5"
    )
    assert len(aggregate.metrics.horizon_mean_net_excess_returns_20bp) == 4
    assert len(aggregate.days[0].horizon_six_slot_returns) == 4
    assert aggregate.days[0].six_slot_net_excess_returns == pytest.approx(
        tuple(sum(horizon[index] for horizon in aggregate.days[0].horizon_six_slot_returns) / 4 for index in range(3))
    )


def test_experiment_rejects_incomplete_point_in_time_rows() -> None:
    rows = list(_rows())
    row = rows[0]
    rows[0] = PairedSelectionRow(
        trade_date=row.trade_date,
        code=row.code,
        board=row.board,
        industry=row.industry,
        parent_row_hash=row.parent_row_hash,
        rule_score=row.rule_score,
        estimated_round_trip_cost=row.estimated_round_trip_cost,
        risk_eligible=row.risk_eligible,
        action_eligible=row.action_eligible,
        capacity_sufficient=row.capacity_sufficient,
        execution_capacity=row.execution_capacity,
        severe_loss=row.severe_loss,
        mae_atr20=row.mae_atr20,
        target_evidence=row.target_evidence,
        point_in_time_parity=False,
    )

    with pytest.raises(ValueError, match="point-in-time"):
        EvaluatePairedSelectionExperiment(_specification(), tuple(rows))


def test_model_gate_moves_before_fixed_seats_but_action_and_veto_never_refill() -> None:
    rows = list(_rows())
    rows[0] = replace(rows[0], capacity_sufficient=True, target_evidence=(_target("t1", -0.01),))
    rows[2] = replace(rows[2], risk_eligible=False)
    rows[3] = replace(rows[3], action_eligible=False)
    report = EvaluatePairedSelectionExperiment(_specification(), tuple(rows))
    fixed, production = tuple(item for item in report.results if item.variant.arm == "rule_model_gate")
    assert fixed.days[0].fixed_codes == tuple(f"600{index:03d}" for index in range(6))
    assert production.days[0].fixed_codes == tuple(f"600{index:03d}" for index in range(1, 7))
    assert production.days[0].selected_codes == ("600001", "600004", "600005", "600006")
    assert "600007" not in production.days[0].selected_codes


def test_empty_day_and_selected_mean_keep_distinct_denominators_and_cost_once() -> None:
    rows = tuple(replace(row, risk_eligible=False) for row in _rows())
    empty = EvaluatePairedSelectionExperiment(_specification(), rows).results[0]
    assert empty.metrics.empty_portfolio_days == 1
    assert empty.metrics.mean_six_slot_net_excess_returns == (0.0, 0.0, 0.0)
    assert empty.metrics.mean_selected_net_excess_returns == ()
    single = EvaluatePairedSelectionExperiment(
        _specification(), rows[:-1] + (replace(rows[-1], rule_score=100, risk_eligible=True),)
    ).results[0]
    assert single.metrics.mean_selected_net_excess_returns[0] == pytest.approx(0.008)
    assert single.metrics.mean_six_slot_net_excess_returns[0] == pytest.approx(0.008 / 6)


def test_complete_population_has_no_business_capacity_or_same_industry_cutoff() -> None:
    prototype = replace(_rows()[0], capacity_sufficient=True)
    rows = tuple(
        replace(
            prototype, code=f"{index:06d}", board=("main", "star")[index % 2], rule_score=90.0 if index == 390 else 80.0
        )
        for index in range(391)
    )
    report = EvaluatePairedSelectionExperiment(_specification(), rows)
    assert report.results[0].metrics.population_rows == 391
    assert report.results[0].days[0].fixed_codes[0] == "000390"
    assert len(report.results[0].days[0].fixed_codes) == 6


def test_research_identity_rejects_old_grid_partition_and_seals_cost_reference() -> None:
    spec = _specification()
    assert spec.research_identity == "eleven_layer_full_population"
    assert spec.bootstrap_block_days == 5
    assert _specification("d25").bootstrap_block_days == 10
    assert replace(spec, cost_reference_hash="4" * 64).content_hash != spec.content_hash
    with pytest.raises(ValueError, match="partition"):
        replace(spec, partition="training")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        replace(spec, capacities=(120,))


@pytest.mark.parametrize("strategy,threshold", [("tomorrow", -1.5), ("d25", -2.5)])
def test_severe_loss_labels_use_mae_atr_multiples_instead_of_return_percent(strategy, threshold) -> None:
    rows = _rows(strategy)
    with pytest.raises(ValueError, match="MAE/ATR20"):
        EvaluatePairedSelectionExperiment(_specification(strategy), (replace(rows[0], mae_atr20=threshold), *rows[1:]))
    correct = (replace(rows[0], mae_atr20=threshold, severe_loss=True), *rows[1:])
    assert (
        EvaluatePairedSelectionExperiment(_specification(strategy), correct).status == "historical_experiment_completed"
    )
