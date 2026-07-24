"""Intraday recommendation score composition."""

from __future__ import annotations

from collections.abc import Mapping

from trader.domain.market.factors import band_score
from trader.domain.market.models import FeatureSnapshot
from trader.domain.recommendation.strategies.composition import LocalScoreResult, compose, liquidity_score, normalized

COMPONENT_WEIGHTS = {
    "momentum": 7 / 18,
    "liquidity": 5 / 18,
    "sentiment": 2 / 9,
    "protection": 1 / 9,
}


def score_today(snapshot: FeatureSnapshot, component_weights: Mapping[str, float] | None = None) -> LocalScoreResult:
    component_weights = COMPONENT_WEIGHTS if component_weights is None else component_weights
    momentum = (
        0.30 * band_score(snapshot.quote.change_5m, 0.0, 0.2, 1.8, 3.5)
        + 0.20 * normalized(snapshot, "speed_percentile")
        + 0.20 * band_score(snapshot.quote.pct_change, -1.0, 1.0, 5.5, 8.0)
        + 0.15 * band_score(snapshot.quote.volume_ratio, 0.8, 1.2, 3.5, 6.0)
        + 0.15 * normalized(snapshot, "relative_strength_3d")
    )
    sentiment = (
        0.50 * normalized(snapshot, "news_sentiment")
        + 0.30 * normalized(snapshot, "evidence_freshness")
        + 0.20 * normalized(snapshot, "market_breadth")
    )
    protection = (
        0.35 * normalized(snapshot, "low_volatility_score")
        + 0.35 * normalized(snapshot, "low_drawdown_score")
        + 0.30 * normalized(snapshot, "low_crowding_score")
    )
    components = {
        "momentum": momentum,
        "liquidity": liquidity_score(snapshot),
        "sentiment": sentiment,
        "protection": protection,
    }
    if "industry" in component_weights:
        components["industry"] = 0.70 * normalized(snapshot, "industry_strength") + 0.30 * normalized(
            snapshot, "industry_breadth"
        )
    return compose(components, component_weights)


__all__ = ["score_today"]
