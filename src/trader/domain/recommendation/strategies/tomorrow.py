"""Next-session recommendation score composition."""

from __future__ import annotations

from collections.abc import Mapping

from trader.domain.market.models import FeatureSnapshot
from trader.domain.recommendation.strategies.composition import LocalScoreResult, compose, liquidity_score, normalized

COMPONENT_WEIGHTS = {
    "liquidity": 0.25,
    "momentum": 0.15,
    "trend": 0.20,
    "historical_edge": 0.15,
    "execution": 0.10,
    "tail_structure": 0.15,
}


def score_tomorrow(snapshot: FeatureSnapshot, component_weights: Mapping[str, float] | None = None) -> LocalScoreResult:
    component_weights = COMPONENT_WEIGHTS if component_weights is None else component_weights
    momentum = (
        0.35 * normalized(snapshot, "relative_strength_5d")
        + 0.25 * normalized(snapshot, "relative_strength_20d")
        + 0.25 * normalized(snapshot, "price_volume_confirmation")
        + 0.15 * normalized(snapshot, "moderate_daily_return")
    )
    trend = (
        0.375 * normalized(snapshot, "ma20_60_position")
        + 0.375 * normalized(snapshot, "ma_slope")
        + 0.25 * normalized(snapshot, "breakout_20d")
    )
    historical_edge = (
        0.45 * normalized(snapshot, "risk_adjusted_return_20d")
        + 0.30 * normalized(snapshot, "low_drawdown_score")
        + 0.25 * normalized(snapshot, "upward_consistency")
    )
    execution = (
        0.40 * normalized(snapshot, "capacity_score")
        + 0.30 * normalized(snapshot, "moderate_amplitude")
        + 0.30 * normalized(snapshot, "limit_distance_safety")
    )
    tail_structure = (
        0.35 * normalized(snapshot, "tail_return_30m")
        + 0.30 * normalized(snapshot, "tail_volume_ratio")
        + 0.35 * normalized(snapshot, "close_location")
    )
    return compose(
        {
            "liquidity": liquidity_score(snapshot),
            "momentum": momentum,
            "trend": trend,
            "historical_edge": historical_edge,
            "execution": execution,
            "tail_structure": tail_structure,
        },
        component_weights,
    )


__all__ = ["score_tomorrow"]
