"""Strategy-specific score composition."""

from collections.abc import Mapping

from trader.domain.models import FeatureSnapshot, Strategy
from trader.domain.strategies.composition import LocalScoreResult, compose
from trader.domain.strategies.d25 import COMPONENT_WEIGHTS as D25_COMPONENT_WEIGHTS
from trader.domain.strategies.d25 import score_d25
from trader.domain.strategies.long import COMPONENT_WEIGHTS as LONG_COMPONENT_WEIGHTS
from trader.domain.strategies.long import score_long
from trader.domain.strategies.today import COMPONENT_WEIGHTS as TODAY_COMPONENT_WEIGHTS
from trader.domain.strategies.today import score_today
from trader.domain.strategies.tomorrow import (
    COMPONENT_WEIGHTS as TOMORROW_COMPONENT_WEIGHTS,
)
from trader.domain.strategies.tomorrow import (
    score_tomorrow,
)

DEFAULT_STRATEGY_WEIGHTS = {
    Strategy.TODAY: TODAY_COMPONENT_WEIGHTS,
    Strategy.TOMORROW: TOMORROW_COMPONENT_WEIGHTS,
    Strategy.D25: D25_COMPONENT_WEIGHTS,
    Strategy.LONG: LONG_COMPONENT_WEIGHTS,
}
"""Canonical local-score component weights per strategy."""


def score_strategy(
    strategy: Strategy,
    snapshot: FeatureSnapshot,
    strategy_weights: Mapping[Strategy, Mapping[str, float]] | None = None,
) -> LocalScoreResult:
    component_weights = None if strategy_weights is None else strategy_weights.get(strategy)
    scorers = {
        Strategy.TODAY: lambda item: score_today(item, component_weights=component_weights),
        Strategy.TOMORROW: lambda item: score_tomorrow(item, component_weights=component_weights),
        Strategy.D25: lambda item: score_d25(item, component_weights=component_weights),
        Strategy.LONG: lambda item: score_long(item, component_weights=component_weights),
    }
    return scorers[strategy](snapshot)


__all__ = [
    "D25_COMPONENT_WEIGHTS",
    "DEFAULT_STRATEGY_WEIGHTS",
    "LONG_COMPONENT_WEIGHTS",
    "LocalScoreResult",
    "TOMORROW_COMPONENT_WEIGHTS",
    "TODAY_COMPONENT_WEIGHTS",
    "compose",
    "score_strategy",
]
