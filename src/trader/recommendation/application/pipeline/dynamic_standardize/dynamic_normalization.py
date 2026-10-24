"""Stage-6 dynamic normalization boundary."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime

from trader.recommendation.domain.market.models import FeatureSnapshot


def NormalizeCandidateDiscoveryPopulation(
    features: Sequence[FeatureSnapshot],
    evaluated_at: datetime,
) -> tuple[FeatureSnapshot, ...]:
    """Normalize one discovery population once before strategy-specific planning."""

    normalized: list[FeatureSnapshot] = []
    for feature in features:
        if feature.preview_context is not None:
            normalized.append(feature)
            continue
        source_time = min(evaluated_at, feature.quote.received_time)
        normalized.append(
            feature
            if feature.quote.source_time == source_time
            else replace(feature, quote=replace(feature.quote, source_time=source_time))
        )
    return tuple(normalized)


__all__ = ["NormalizeCandidateDiscoveryPopulation"]
