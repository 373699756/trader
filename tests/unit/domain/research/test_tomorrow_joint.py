from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date, timedelta

import pytest

from trader.domain.research.tomorrow_joint import (
    TOMORROW_JOINT_CANDIDATES,
    TOMORROW_JOINT_LAMBDAS,
    TomorrowJointAlignedRow,
    TomorrowJointCandidateFamily,
    TomorrowJointDailyPortfolioEvidence,
    TomorrowJointFittedModel,
    TomorrowJointWeights,
    evaluate_tomorrow_joint_validation,
    fit_tomorrow_joint_candidate,
    fit_tomorrow_joint_candidate_family,
    predict_tomorrow_joint,
    select_tomorrow_joint_candidate,
)


def _row(
    day_index: int,
    code_index: int,
    *,
    v1: float,
    v2: float,
    c3: float,
    actual: float,
) -> TomorrowJointAlignedRow:
    trade_date = date(2025, 1, 1) + timedelta(days=day_index)
    return TomorrowJointAlignedRow(
        trade_date=trade_date,
        code=f"60{code_index:04d}",
        candidate_order=code_index,
        label_matured_at=trade_date + timedelta(days=1),
        actual_net_excess_20bp=actual,
        actual_net_excess_50bp=actual - 0.003,
        severe_loss=actual < -0.04,
        v1_predicted_net_excess_20bp=v1,
        v2_predicted_net_excess_20bp=v2,
        c3_predicted_net_excess_20bp=c3,
    )


def _fit_rows(start_day: int, days: int) -> tuple[TomorrowJointAlignedRow, ...]:
    return tuple(
        _row(
            start_day + day_index,
            code_index,
            v1=signal * 0.8,
            v2=-signal * 500.0,
            c3=signal * 1.2,
            actual=signal,
        )
        for day_index in range(days)
        for code_index in range(1, 7)
        for signal in ((code_index - 3.5) / 100.0,)
    )


def test_joint_contract_is_fixed_and_inputs_are_immutable() -> None:
    assert TOMORROW_JOINT_CANDIDATES == ("c3", "v1_c3", "v1_v2_c3")
    assert TOMORROW_JOINT_LAMBDAS == (0.1, 1.0, 10.0, 100.0)
    row = _fit_rows(0, 1)[0]

    with pytest.raises(FrozenInstanceError):
        row.candidate_order = 99  # type: ignore[misc]


def test_three_way_candidate_can_shrink_v2_exactly_to_zero() -> None:
    rows = tuple(
        _row(day_index, code_index, v1=0.02, v2=10.0, c3=0.02, actual=0.01)
        for day_index in range(12)
        for code_index in range(1, 7)
    )
    model = fit_tomorrow_joint_candidate(
        rows,
        candidate_id="v1_v2_c3",
        regularization_lambda=0.1,
    )

    assert model.weights.v2 == 0.0
    assert model.weights.v1 >= 0.0
    assert model.weights.c3 >= 0.0
    assert model.weights.v1 + model.weights.v2 + model.weights.c3 == pytest.approx(1.0)


def test_development_freezes_all_structures_without_using_mse_as_profit_selection() -> None:
    training = _fit_rows(0, 10)
    tuning = _fit_rows(20, 5)

    family = fit_tomorrow_joint_candidate_family(training, tuning)

    assert tuple(item.candidate_id for item in family.candidates) == TOMORROW_JOINT_CANDIDATES
    assert family.selection_status == "portfolio_evidence_required"
    assert family.production_authority is False
    assert all(
        item.regularization_lambda in TOMORROW_JOINT_LAMBDAS for item in family.candidates if item.candidate_id != "c3"
    )

    with pytest.raises(ValueError, match="strictly precede"):
        fit_tomorrow_joint_candidate_family(training, training)


def test_prediction_is_one_pre_score_net_excess_value() -> None:
    model = TomorrowJointFittedModel(
        candidate_id="v1_v2_c3",
        regularization_lambda=1.0,
        weights=TomorrowJointWeights(0.25, 0.0, 0.75),
        training_rows=60,
        tuning_rows=30,
        tuning_mean_squared_error=0.001,
    )
    row = _row(0, 1, v1=0.02, v2=0.99, c3=0.06, actual=0.04)

    prediction = predict_tomorrow_joint(model, (row,))[0]

    assert prediction.predicted_net_excess_20bp == pytest.approx(0.05)
    assert prediction.prediction_semantics == "pre_base_score_cost_adjusted_net_excess"
    assert not hasattr(prediction, "base_score")
    assert not hasattr(prediction, "action")


def test_validation_reports_paired_profit_risk_turnover_and_rank_gates() -> None:
    rows = tuple(
        _row(
            day_index,
            code_index,
            v1=rank / 100.0,
            v2=rank / 110.0,
            c3=rank / 90.0,
            actual=rank / 80.0,
        )
        for day_index in range(12)
        for code_index in range(1, 11)
        for rank in (code_index - 5.5,)
    )
    model = TomorrowJointFittedModel(
        candidate_id="v1_c3",
        regularization_lambda=10.0,
        weights=TomorrowJointWeights(0.5, 0.0, 0.5),
        training_rows=100,
        tuning_rows=50,
        tuning_mean_squared_error=0.001,
    )
    predictions = predict_tomorrow_joint(model, rows)
    portfolios = tuple(
        TomorrowJointDailyPortfolioEvidence(
            trade_date=date(2025, 1, 1) + timedelta(days=day_index),
            v1_net_excess_20bp=0.002,
            joint_net_excess_20bp=0.012,
            v1_net_excess_50bp=0.001,
            joint_net_excess_50bp=0.008,
            v1_severe_loss_rate=0.10,
            joint_severe_loss_rate=0.05,
            v1_turnover=0.20,
            joint_turnover=0.22,
        )
        for day_index in range(12)
    )

    report = evaluate_tomorrow_joint_validation(predictions, portfolios)

    assert report.paired_increment_20bp == pytest.approx(0.01)
    assert report.paired_increment_50bp == pytest.approx(0.007)
    assert report.bootstrap_20bp.confidence_lower > 0.0  # type: ignore[operator]
    assert report.bootstrap_50bp.confidence_lower > 0.0  # type: ignore[operator]
    assert report.severe_loss_rate_delta == pytest.approx(-0.05)
    assert report.turnover_increment_percentage_points == pytest.approx(2.0)
    assert report.mean_rank_ic == pytest.approx(1.0)
    assert report.mean_q5_minus_q1_20bp > 0.0  # type: ignore[operator]
    assert report.passed is True
    assert report.production_authority is False


def test_validation_fails_when_50bp_increment_is_not_positive() -> None:
    rows = _fit_rows(0, 6)
    model = TomorrowJointFittedModel(
        candidate_id="c3",
        regularization_lambda=None,
        weights=TomorrowJointWeights(0.0, 0.0, 1.0),
        training_rows=30,
        tuning_rows=12,
        tuning_mean_squared_error=0.001,
    )
    predictions = predict_tomorrow_joint(model, rows)
    portfolios = tuple(
        TomorrowJointDailyPortfolioEvidence(
            trade_date=date(2025, 1, 1) + timedelta(days=day_index),
            v1_net_excess_20bp=0.001,
            joint_net_excess_20bp=0.002,
            v1_net_excess_50bp=0.001,
            joint_net_excess_50bp=0.0,
            v1_severe_loss_rate=0.0,
            joint_severe_loss_rate=0.0,
            v1_turnover=0.1,
            joint_turnover=0.1,
        )
        for day_index in range(6)
    )

    report = evaluate_tomorrow_joint_validation(predictions, portfolios)

    assert report.paired_increment_50bp < 0.0
    assert report.passed is False
    assert "paired_50bp_not_positive" in report.failure_reasons


def test_final_structure_selection_uses_complete_profit_evidence_not_tuning_mse() -> None:
    rows = _fit_rows(0, 6)
    models = (
        TomorrowJointFittedModel("c3", None, TomorrowJointWeights(0.0, 0.0, 1.0), 30, 12, 0.0001),
        TomorrowJointFittedModel("v1_c3", 1.0, TomorrowJointWeights(0.5, 0.0, 0.5), 30, 12, 0.0003),
        TomorrowJointFittedModel("v1_v2_c3", 0.1, TomorrowJointWeights(0.5, 0.0, 0.5), 30, 12, 0.0002),
    )
    family = TomorrowJointCandidateFamily(models)
    reports = []
    for model, increment in zip(models, (0.004, 0.009, 0.006), strict=True):
        portfolios = tuple(
            TomorrowJointDailyPortfolioEvidence(
                trade_date=date(2025, 1, 1) + timedelta(days=day_index),
                v1_net_excess_20bp=0.001,
                joint_net_excess_20bp=0.001 + increment,
                v1_net_excess_50bp=0.0005,
                joint_net_excess_50bp=0.0005 + increment,
                v1_severe_loss_rate=0.1,
                joint_severe_loss_rate=0.05,
                v1_turnover=0.1,
                joint_turnover=0.1,
            )
            for day_index in range(6)
        )
        reports.append(evaluate_tomorrow_joint_validation(predict_tomorrow_joint(model, rows), portfolios))

    selection = select_tomorrow_joint_candidate(family, tuple(reports))

    assert selection.status == "selected_by_portfolio_evidence"
    assert selection.selected_model is models[1]
    assert models[0].tuning_mean_squared_error < models[1].tuning_mean_squared_error
