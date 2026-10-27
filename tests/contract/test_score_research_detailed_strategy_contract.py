from __future__ import annotations

from trader.training.application.tomorrow_historical_validation import (
    TOMORROW_HISTORICAL_RISK_VALIDATION_SPEC,
)


def test_historical_risk_probability_gate_is_fixed_before_production_use() -> None:
    assert TOMORROW_HISTORICAL_RISK_VALIDATION_SPEC.research_identity == "tomorrow_historical_risk_probability"
    assert (
        TOMORROW_HISTORICAL_RISK_VALIDATION_SPEC.training_trade_dates,
        TOMORROW_HISTORICAL_RISK_VALIDATION_SPEC.calibration_trade_dates,
        TOMORROW_HISTORICAL_RISK_VALIDATION_SPEC.test_trade_dates,
    ) == (60, 20, 40)
    assert TOMORROW_HISTORICAL_RISK_VALIDATION_SPEC.maximum_expected_calibration_error == 0.05
