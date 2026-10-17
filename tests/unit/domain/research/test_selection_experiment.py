from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from trader.training.domain.evaluation.selection_experiment import (
    EvaluatePairedSelectionExperiment,
    PairedSelectionRow,
    PairedSelectionSpecification,
    SelectionTargetEvidence,
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
                candidate_rank_by_board=index // 3 + 1,
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
    assert len(report.results) == 50
    assert {item.variant.arm for item in report.results} == {
        "rule_only",
        "rule_model_gate",
        "industry_model_utility",
        "pooled_model_utility",
    }
    fixed = next(
        item
        for item in report.results
        if item.variant.arm == "rule_only"
        and item.variant.capacity_per_board == 60
        and item.variant.selection_mode == "fixed_score_top6"
    )
    refilled = next(
        item
        for item in report.results
        if item.variant.arm == "rule_only"
        and item.variant.capacity_per_board == 60
        and item.variant.selection_mode == "gate_then_constrained_top6"
    )
    assert fixed.metrics.selected_rows == 5
    assert refilled.metrics.selected_rows == 6
    assert refilled.metrics.capacity_shortfall_rate == pytest.approx(1 / 9)
    assert report.production_authority is False
    assert report.terminal_holdout_opened is False


def test_fixed_score_control_never_admits_rank_seven_after_concentration() -> None:
    rows = tuple(
        replace(
            row,
            board="main" if index < 6 else "star",
            candidate_rank_by_board=index + 1 if index < 6 else index - 5,
            capacity_sufficient=True,
        )
        for index, row in enumerate(_rows())
    )
    report = EvaluatePairedSelectionExperiment(_specification(), rows)
    fixed = next(
        result
        for result in report.results
        if result.variant.arm == "rule_only"
        and result.variant.capacity_per_board is None
        and result.variant.selection_mode == "fixed_score_top6"
    )
    refilled = next(
        result
        for result in report.results
        if result.variant.arm == "rule_only"
        and result.variant.capacity_per_board is None
        and result.variant.selection_mode == "gate_then_constrained_top6"
    )
    assert fixed.metrics.selected_rows == 4
    assert refilled.metrics.selected_rows == 6


def test_d25_experiment_compares_only_preregistered_realizable_targets() -> None:
    report = EvaluatePairedSelectionExperiment(_specification("d25"), _rows("d25"))

    assert len(report.results) == 200
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
        and item.variant.capacity_per_board == 60
        and item.variant.selection_mode == "gate_then_constrained_top6"
        and item.variant.target == "mean_t2_t5"
    )
    assert len(aggregate.metrics.horizon_mean_net_excess_returns_20bp) == 4


def test_experiment_rejects_incomplete_point_in_time_rows() -> None:
    rows = list(_rows())
    row = rows[0]
    rows[0] = PairedSelectionRow(
        trade_date=row.trade_date,
        code=row.code,
        board=row.board,
        industry=row.industry,
        parent_row_hash=row.parent_row_hash,
        candidate_rank_by_board=row.candidate_rank_by_board,
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
