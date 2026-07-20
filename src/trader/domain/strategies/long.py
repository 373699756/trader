"""Fixed long-watch score composition."""

from __future__ import annotations

from collections.abc import Mapping

from trader.domain.models import FeatureSnapshot
from trader.domain.strategies.composition import LocalScoreResult, compose, normalized

COMPONENT_WEIGHTS = {
    "value": 0.30,
    "growth": 0.25,
    "quality": 0.20,
    "industry_policy": 0.15,
    "protection": 0.10,
}


def score_long(snapshot: FeatureSnapshot, component_weights: Mapping[str, float] | None = None) -> LocalScoreResult:
    component_weights = COMPONENT_WEIGHTS if component_weights is None else component_weights
    return compose(
        {
            "value": normalized(snapshot, "value_score"),
            "growth": normalized(snapshot, "growth_score"),
            "quality": normalized(snapshot, "quality_score"),
            "industry_policy": normalized(snapshot, "industry_policy_score"),
            "protection": normalized(snapshot, "risk_protection_score"),
        },
        component_weights,
    )


__all__ = ["score_long"]
