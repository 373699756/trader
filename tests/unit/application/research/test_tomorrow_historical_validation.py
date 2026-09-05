from __future__ import annotations

from datetime import date, timedelta

import pytest

from trader.application.ports.tomorrow_model import TomorrowModelInput, TomorrowModelPrediction
from trader.application.research.tomorrow_historical_validation import (
    HISTORICAL_RISK_VALIDATION_SPEC,
    TomorrowHistoricalRiskRow,
    build_historical_risk_probability,
    evaluate_historical_risk_probability,
    evaluate_historical_selected_days,
)
from trader.domain.recommendation.model_scoring import V1_V2_EXPOSURE_CONTRACT


class _Predictor:
    profile_id = "v2"
    model_id = "test-risk-model"
    model_hash = "a" * 64
    feature_ids = (
        "qfq_return_1d",
        "qfq_return_3d",
        "qfq_return_5d",
        "qfq_residual_momentum_20d_skip5",
        "qfq_residual_momentum_40d_skip5",
        "qfq_residual_momentum_60d_skip5",
    )
    exposure_contract = V1_V2_EXPOSURE_CONTRACT

    def predict(self, inputs: tuple[TomorrowModelInput, ...]) -> tuple[TomorrowModelPrediction, ...]:
        return tuple(
            TomorrowModelPrediction(
                item.code,
                0.01 + item.alpha_features[0] * 0.01,
                abs(item.alpha_features[1]) * 0.01,
            )
            for item in inputs
        )


def _row(day: date, index: int, *, benchmark: float = 0.01) -> TomorrowHistoricalRiskRow:
    severe = index % 7 == 0
    gross_return = -0.03 if severe else 0.015 + (index % 5) / 1000.0
    return TomorrowHistoricalRiskRow(
        trade_date=day,
        code=f"60{index:04d}",
        board="main" if index % 2 == 0 else "chinext",
        alpha_features=(
            (index % 11) / 100.0,
            (index % 7) / 100.0,
            (index % 5) / 100.0,
            (index % 13) / 100.0,
            (index % 17) / 100.0,
            (index % 19) / 100.0,
        ),
        realized_volatility_20d=0.02,
        downside_semivariance_20d=0.01,
        drawdown_recovery_60d=0.9,
        amihud_20d=0.001 + index / 1_000_000.0,
        average_amount_20d=100_000_000.0,
        baseline_score=50.0,
        gross_return=gross_return,
        benchmark_return=benchmark,
        gross_excess_return=gross_return - benchmark,
        atr20_pct=0.02,
        mae_atr20=-2.0 if severe else -0.5,
    )


def test_legal_empty_selection_is_a_cash_day_not_missing_evidence() -> None:
    day = date(2025, 1, 2)
    rows = tuple(_row(day, index, benchmark=0.012) for index in range(10))

    result = evaluate_historical_selected_days(rows, utilities=(-0.1,) * len(rows))

    assert len(result) == 1
    assert result[0].status == "cash"
    assert result[0].selected_codes == ()
    assert result[0].gross_return == 0.0
    assert result[0].benchmark_return == 0.012
    assert result[0].net_excess_return_20bp == -0.012

    with pytest.raises(ValueError, match="unique"):
        evaluate_historical_selected_days((rows[0], rows[0]), utilities=(0.1, 0.1))


def test_historical_risk_validation_uses_ordered_60_20_40_splits_without_future_collection() -> None:
    start = date(2026, 1, 2)
    rows = tuple(
        _row(start + timedelta(days=day_index), stock_index) for day_index in range(122) for stock_index in range(20)
    )

    outcome = build_historical_risk_probability(rows, _Predictor())
    report = outcome.report

    assert report.spec_hash == HISTORICAL_RISK_VALIDATION_SPEC.content_hash
    assert report.training_trade_dates == 60
    assert report.calibration_trade_dates == 20
    assert report.test_trade_dates == 40
    assert report.embargo_trade_dates == 2
    assert report.status in {"historical_validated", "historical_rejected"}
    assert report.production_authority is False
    assert outcome.model_artifact is not None
    assert report.model_artifact_hash == outcome.model_artifact.content_hash
    assert outcome.model_artifact.production_authority is False
    probabilities = outcome.model_artifact.predict(((0.01, 0.1, 0.5, 0.02, 0.002),))
    assert len(probabilities) == 1 and 0.0 <= probabilities[0] <= 1.0
    assert 0.0 <= report.brier_score <= 1.0
    assert 0.0 <= report.expected_calibration_error <= 1.0


def test_historical_risk_validation_reports_insufficient_history_instead_of_collecting() -> None:
    start = date(2026, 1, 2)
    rows = tuple(
        _row(start + timedelta(days=day_index), stock_index) for day_index in range(121) for stock_index in range(5)
    )

    report = evaluate_historical_risk_probability(rows, _Predictor())

    assert report.status == "historical_data_insufficient"
    assert report.failure_reasons == ("historical_trade_dates_below_122",)
    assert report.production_authority is False
