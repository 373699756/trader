from __future__ import annotations

from datetime import date, timedelta

from trader.application.research.tomorrow_historical_p2_screening import TomorrowHistoricalP2Row
from trader.domain.research.tomorrow_historical_p2 import TOMORROW_HISTORICAL_P2_SPEC
from trader.infra.research.tomorrow_historical_p2_model import TomorrowHistoricalP2EnsembleTrainer


def _rows(start: date, days: int) -> tuple[TomorrowHistoricalP2Row, ...]:
    result: list[TomorrowHistoricalP2Row] = []
    for day_index in range(days):
        for stock_index in range(30):
            value = (day_index * 30 + stock_index + 1) / 10_000.0
            result.append(
                TomorrowHistoricalP2Row(
                    trade_date=start + timedelta(days=day_index),
                    code=f"60{stock_index:04d}",
                    board="main" if stock_index % 2 == 0 else "chinext",
                    alpha_features=(value, value**2, -value, value / 2, value / 3, value / 4),
                    realized_volatility_20d=0.01,
                    downside_semivariance_20d=0.01,
                    drawdown_recovery_60d=0.9,
                    amihud_20d=0.001,
                    average_amount_20d=1_000_000.0,
                    baseline_score=float(stock_index),
                    gross_excess_return=value * 0.5,
                    mae_atr20=-0.5,
                )
            )
    return tuple(result)


def test_p2_ensemble_is_deterministic_and_keeps_the_official_validation_out_of_fitting() -> None:
    trainer = TomorrowHistoricalP2EnsembleTrainer()
    training = _rows(date(2025, 1, 2), 10)
    validation = _rows(date(2026, 1, 2), 2)

    first = trainer.fit(training, validation, TOMORROW_HISTORICAL_P2_SPEC.candidate)
    second = trainer.fit(training, validation, TOMORROW_HISTORICAL_P2_SPEC.candidate)

    assert first.artifact.content_hash == second.artifact.content_hash
    assert first.artifact.training_rows == len(training)
    assert first.artifact.internal_validation_rows == 60
    assert len(first.training_predictions) == len(training)
    assert len(first.validation_predictions) == len(validation)
    assert first.validation_predictions == second.validation_predictions
