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
    neutral = feature_factory(values={"return_20d": 10.0})
    overheated = FeatureSnapshot(
        quote=neutral.quote,
        values={**neutral.values, "return_20d": 35.0},
        observed_at=neutral.observed_at,
        history_days=neutral.history_days,
        market_regime="risk_off",
    )

    neutral_score = score_strategy(Strategy.D25, neutral).base_score
    overheated_score = score_strategy(Strategy.D25, overheated).base_score

    assert overheated_score == pytest.approx(neutral_score * 0.75 * 0.92)


def test_d25_overheat_boundary_keeps_thirty_percent_at_point_eighty_five(feature_factory) -> None:
    baseline = score_strategy(Strategy.D25, feature_factory(values={"return_20d": 15.0})).base_score
    at_boundary = score_strategy(Strategy.D25, feature_factory(values={"return_20d": 30.0})).base_score
    above_boundary = score_strategy(Strategy.D25, feature_factory(values={"return_20d": 30.01})).base_score

    assert at_boundary == pytest.approx(baseline * 0.85)
    assert above_boundary == pytest.approx(baseline * 0.75)
