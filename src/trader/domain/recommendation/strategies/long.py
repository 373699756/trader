"""Fixed long-watch recommendation score composition."""

from __future__ import annotations

from collections.abc import Mapping

from trader.domain.market.models import FeatureSnapshot
from trader.domain.recommendation.strategies.composition import LocalScoreResult, compose, normalized

COMPONENT_WEIGHTS = {
    "value": 6 / 17,
    "growth": 5 / 17,
    "quality": 4 / 17,
    "protection": 2 / 17,
}


def score_long(snapshot: FeatureSnapshot, component_weights: Mapping[str, float] | None = None) -> LocalScoreResult:
    component_weights = COMPONENT_WEIGHTS if component_weights is None else component_weights
    components = {
        "value": normalized(snapshot, "value_score"),
        "growth": normalized(snapshot, "growth_score"),
        "quality": normalized(snapshot, "quality_score"),
        "protection": normalized(snapshot, "risk_protection_score"),
    }
    if "industry_policy" in component_weights:
        components["industry_policy"] = normalized(snapshot, "industry_policy_score")
    return compose(components, component_weights)


__all__ = ["score_long"]
