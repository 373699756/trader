from __future__ import annotations

import pytest

from trader.domain.models import FeatureSnapshot, Strategy
from trader.domain.strategies import score_strategy


@pytest.mark.parametrize("strategy", list(Strategy))
def test_strategy_scores_are_deterministic_and_bounded(feature_factory, strategy) -> None:
    snapshot = feature_factory()

    first = score_strategy(strategy, snapshot)
    second = score_strategy(strategy, snapshot)

    assert first == second
    assert 0.0 <= first.base_score <= 100.0
    assert all(0.0 <= value <= 100.0 for value in first.components.values())


def test_d25_applies_overheat_and_market_regime_coefficients(feature_factory) -> None:
    neutral = feature_factory(values={"d25_overheat_factor": 1.0, "market_regime_factor": 1.0})
    overheated = FeatureSnapshot(
        quote=neutral.quote,
        values={**neutral.values, "d25_overheat_factor": 0.75, "market_regime_factor": 0.92},
        observed_at=neutral.observed_at,
        history_days=neutral.history_days,
        market_regime="risk_off",
    )

    neutral_score = score_strategy(Strategy.D25, neutral).base_score
    overheated_score = score_strategy(Strategy.D25, overheated).base_score

    assert overheated_score == pytest.approx(neutral_score * 0.75 * 0.92)


def test_d25_overheat_boundary_keeps_thirty_percent_at_point_eighty_five(feature_factory) -> None:
    baseline = score_strategy(Strategy.D25, feature_factory(values={"d25_overheat_factor": 1.0})).base_score
    at_boundary = score_strategy(Strategy.D25, feature_factory(values={"d25_overheat_factor": 0.85})).base_score
    above_boundary = score_strategy(Strategy.D25, feature_factory(values={"d25_overheat_factor": 0.75})).base_score

    assert at_boundary == pytest.approx(baseline * 0.85)
    assert above_boundary == pytest.approx(baseline * 0.75)


def test_today_score_matches_all_documented_component_and_subcomponent_weights(feature_factory) -> None:
    result = score_strategy(Strategy.TODAY, feature_factory())

    assert result.components == pytest.approx(
        {
            "momentum": 88.75,
            "liquidity": 82.0,
            "industry": 60.0,
            "sentiment": 59.5,
            "protection": 66.75,
        }
    )
    assert result.base_score == pytest.approx(76.1375)


def test_tomorrow_score_matches_all_documented_component_and_subcomponent_weights(feature_factory) -> None:
    result = score_strategy(
        Strategy.TOMORROW,
        feature_factory(
            values={
                "relative_strength_5d": 10.0,
                "relative_strength_20d": 20.0,
                "price_volume_confirmation": 30.0,
                "moderate_daily_return": 40.0,
                "ma20_60_position": 11.0,
                "ma_slope": 22.0,
                "breakout_20d": 33.0,
                "industry_trend": 44.0,
                "risk_adjusted_return_20d": 12.0,
                "low_drawdown_score": 24.0,
                "upward_consistency": 36.0,
                "capacity_score": 13.0,
                "moderate_amplitude": 26.0,
                "limit_distance_safety": 39.0,
                "tail_return_30m": 14.0,
                "tail_volume_ratio": 28.0,
                "close_location": 42.0,
            }
        ),
    )

    assert result.components == pytest.approx(
        {
            "liquidity": 82.0,
            "momentum": 22.0,
            "trend": 25.3,
            "historical_edge": 21.6,
            "execution": 24.7,
            "tail_structure": 28.0,
        }
    )
    assert result.base_score == pytest.approx(38.77)


def test_tomorrow_missing_tail_inputs_are_neutral_without_becoming_zero(feature_factory) -> None:
    result = score_strategy(
        Strategy.TOMORROW,
        feature_factory(values={"tail_return_30m": None, "tail_volume_ratio": None}),
    )

    assert result.components["tail_structure"] == pytest.approx(57.0)
