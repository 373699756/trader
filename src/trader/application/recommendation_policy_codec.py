"""Pure feature projection used before V2 selection."""

from __future__ import annotations

from dataclasses import replace

from trader.domain.market.models import FeatureSnapshot
from trader.domain.recommendation.ranking import CORE_FIELDS

_PRESELECTION_VALUE_FIELDS = (*CORE_FIELDS, "amount_median_20d", "trend_score")


def preselection_replay_feature(feature: FeatureSnapshot) -> FeatureSnapshot:
    """Remove post-selection evidence from the market population view."""

    return replace(
        feature,
        values={name: feature.values.get(name) for name in dict.fromkeys(_PRESELECTION_VALUE_FIELDS)},
        normalization=feature.normalization,
        evidence=(),
        external_risk_facts=(),
    )


__all__ = ["preselection_replay_feature"]
