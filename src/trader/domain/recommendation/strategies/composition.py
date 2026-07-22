"""Shared recommendation score composition primitives."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from trader.domain.market.factors import band_score, clamp, weighted_score
from trader.domain.market.models import FeatureSnapshot


@dataclass(frozen=True)
class LocalScoreResult:
    components: Mapping[str, float]
    base_score: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "components", MappingProxyType(dict(self.components)))


def normalized(snapshot: FeatureSnapshot, name: str, default: float = 50.0) -> float:
    return clamp(snapshot.value(name, default))


def liquidity_score(snapshot: FeatureSnapshot) -> float:
    return 0.6 * normalized(snapshot, "amount_percentile_20d") + 0.4 * band_score(
        snapshot.quote.turnover_rate,
        0.5,
        1.5,
        8.0,
        15.0,
    )


def compose(components: Mapping[str, float], weights: Mapping[str, float]) -> LocalScoreResult:
    return LocalScoreResult(components=components, base_score=weighted_score(components, weights))


__all__ = ["LocalScoreResult", "compose", "liquidity_score", "normalized"]
