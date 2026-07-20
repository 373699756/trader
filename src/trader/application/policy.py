"""Validated application policies independent from configuration transport."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from trader.domain.fusion import FusionPolicy
from trader.domain.models import RiskRule, Strategy


@dataclass(frozen=True)
class SelectionPolicy:
    default_top_k: int
    maximum_top_k: int
    maximum_per_industry: int
    observation_margin: float
    thresholds: Mapping[str, float]

    def __post_init__(self) -> None:
        object.__setattr__(self, "thresholds", MappingProxyType(dict(self.thresholds)))
        if not 0 <= self.default_top_k <= self.maximum_top_k <= 18:
            raise ValueError("TopK bounds must satisfy 0 <= default <= maximum <= 18")
        if self.maximum_per_industry < 1:
            raise ValueError("maximum_per_industry must be positive")
        if self.observation_margin < 0.0:
            raise ValueError("observation_margin cannot be negative")


@dataclass(frozen=True)
class RecommendationPolicy:
    strategy_version: str
    fusion_version: str
    fusion: FusionPolicy
    selection: SelectionPolicy
    candidate_weights: Mapping[str, float]
    dimension_weights: Mapping[Strategy, Mapping[str, float]]
    local_strategy_weights: Mapping[Strategy, Mapping[str, float]]
    risk_rules: Mapping[str, RiskRule]

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_weights", MappingProxyType(dict(self.candidate_weights)))
        object.__setattr__(
            self,
            "dimension_weights",
            MappingProxyType(
                {strategy: MappingProxyType(dict(weights)) for strategy, weights in self.dimension_weights.items()}
            ),
        )
        object.__setattr__(
            self,
            "local_strategy_weights",
            MappingProxyType(
                {strategy: MappingProxyType(dict(weights)) for strategy, weights in self.local_strategy_weights.items()}
            ),
        )
        object.__setattr__(self, "risk_rules", MappingProxyType(dict(self.risk_rules)))


__all__ = ["RecommendationPolicy", "SelectionPolicy"]
