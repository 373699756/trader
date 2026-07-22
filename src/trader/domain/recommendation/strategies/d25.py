"""Two-to-five-day recommendation score composition."""

from __future__ import annotations

from collections.abc import Mapping

from trader.domain.market.factors import clamp
from trader.domain.market.models import FeatureSnapshot
from trader.domain.recommendation.strategies.composition import LocalScoreResult, compose, liquidity_score, normalized

COMPONENT_WEIGHTS = {
    "momentum": 0.30,
    "trend": 0.25,
    "liquidity": 0.20,
    "execution": 0.15,
    "not_overheated": 0.10,
}


def score_d25(snapshot: FeatureSnapshot, component_weights: Mapping[str, float] | None = None) -> LocalScoreResult:
    component_weights = COMPONENT_WEIGHTS if component_weights is None else component_weights
    momentum = (
        0.30 * normalized(snapshot, "relative_strength_5d")
        + 0.30 * normalized(snapshot, "relative_strength_10d")
        + 0.25 * normalized(snapshot, "relative_strength_20d")
        + 0.15 * normalized(snapshot, "price_volume_confirmation")
    )
    trend = (
        0.35 * normalized(snapshot, "ma20_60_structure")
        + 0.30 * normalized(snapshot, "ma_slope")
        + 0.20 * normalized(snapshot, "breakout_20d")
        + 0.15 * normalized(snapshot, "industry_trend")
    )
    execution = (
        0.40 * normalized(snapshot, "capacity_score")
        + 0.30 * normalized(snapshot, "moderate_amplitude")
        + 0.30 * normalized(snapshot, "price_executability")
    )
    not_overheated = (
        0.40 * normalized(snapshot, "ma20_deviation_inverse")
        + 0.35 * normalized(snapshot, "return_20d_not_overheated")
        + 0.25 * normalized(snapshot, "low_volatility_score")
    )
    composed = compose(
        {
            "momentum": momentum,
            "trend": trend,
            "liquidity": liquidity_score(snapshot),
            "execution": execution,
            "not_overheated": not_overheated,
        },
        component_weights,
    )
    adjusted = clamp(
        composed.base_score * snapshot.value("d25_overheat_factor", 1.0) * snapshot.value("market_regime_factor", 1.0)
    )
    return LocalScoreResult(components=composed.components, base_score=adjusted)


__all__ = ["score_d25"]
