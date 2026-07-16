"""Two-to-five-day strategy score composition."""

from __future__ import annotations

from trader.domain.factors import clamp
from trader.domain.models import FeatureSnapshot
from trader.domain.strategies.composition import LocalScoreResult, compose, liquidity_score, normalized

COMPONENT_WEIGHTS = {
    "momentum": 0.30,
    "trend": 0.25,
    "liquidity": 0.20,
    "execution": 0.15,
    "not_overheated": 0.10,
}

REGIME_FACTORS = {"risk_on": 1.03, "neutral": 1.0, "risk_off": 0.92}


def score_d25(snapshot: FeatureSnapshot) -> LocalScoreResult:
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
        COMPONENT_WEIGHTS,
    )
    adjusted = clamp(
        composed.base_score
        * _overheat_factor(snapshot.optional_value("return_20d"))
        * _regime_factor(snapshot.market_regime)
    )
    return LocalScoreResult(components=composed.components, base_score=adjusted)


def _overheat_factor(return_20d: float | None) -> float:
    if return_20d is None or return_20d <= 15.0:
        return 1.0
    if return_20d > 30.0:
        return 0.75
    return 1.0 - (return_20d - 15.0) * 0.15 / 15.0


def _regime_factor(regime: str) -> float:
    return REGIME_FACTORS.get(regime, 1.0)


__all__ = ["score_d25"]
