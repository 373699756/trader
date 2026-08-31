from __future__ import annotations

from dataclasses import replace

import pytest

from trader.domain.research.tomorrow_profile_comparison import (
    TOMORROW_PROFILE_COMPARISON_SPEC,
    TOMORROW_V2_RISK_CHALLENGER_SPEC,
    newey_west_long_run_std,
    required_independent_days,
)


def test_forward_day_requirement_is_derived_from_frozen_historical_power_inputs() -> None:
    spec = TOMORROW_PROFILE_COMPARISON_SPEC

    assert spec.required_independent_days == 522
    assert spec.required_independent_days != 20
    assert spec.required_independent_days == required_independent_days(
        spec.historical_long_run_difference_std_pct,
        spec.minimum_economic_effect_pct,
        alpha=spec.alpha,
        target_power=spec.target_power,
    )
    assert spec.production_authority is False
    assert spec.automatic_profile_switch is False


def test_power_identity_rejects_an_arbitrary_fixed_day_override() -> None:
    with pytest.raises(ValueError, match="derived from frozen power inputs"):
        replace(TOMORROW_PROFILE_COMPARISON_SPEC, required_independent_days=20)


def test_power_uses_a_lag_four_long_run_variance_instead_of_independent_daily_noise() -> None:
    values = (1.0, 2.0, 1.5, 3.0, 2.5, 4.0)

    estimate = newey_west_long_run_std(values, lag_days=4)

    assert estimate == pytest.approx(1.0628403594282774)
    assert TOMORROW_PROFILE_COMPARISON_SPEC.power_variance_estimator == "newey_west_bartlett_lag4_v1"
    assert (
        TOMORROW_PROFILE_COMPARISON_SPEC.historical_long_run_difference_std_pct
        > TOMORROW_PROFILE_COMPARISON_SPEC.historical_daily_difference_std_pct
    )


def test_v2_risk_challenger_is_preregistered_without_claiming_a_loss_probability_model() -> None:
    spec = TOMORROW_V2_RISK_CHALLENGER_SPEC

    assert spec.parent_comparison_spec_hash == TOMORROW_PROFILE_COMPARISON_SPEC.content_hash
    assert spec.label == "mae_atr20_le_negative_1_5_v1"
    assert spec.calibration_days == 20
    assert spec.independent_test_days == 40
    assert spec.production_authority is False
    assert spec.online_learning is False
