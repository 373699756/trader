"""Ranking and action-selection boundary for scored recommendations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from trader.application.recommendation.policy import RecommendationPolicy
from trader.recommendation.application.pipeline.dynamic_filter.filter_executor import (
    ScoredSelectionIdentity,
    ScoredSelectionOptions,
    select_scored_features,
)
from trader.recommendation.domain.market.models import FeatureSnapshot
from trader.recommendation.domain.publication.models import ScoredSelectionResult


class RankingSelectionPort(Protocol):
    """Select ranked candidates without owning input acquisition."""

    def select(
        self,
        features: Sequence[FeatureSnapshot],
        policy: RecommendationPolicy,
        options: ScoredSelectionOptions,
        identity: ScoredSelectionIdentity,
        *,
        execution_gate_reasons: Mapping[str, str] | None = None,
    ) -> ScoredSelectionResult: ...


@dataclass(frozen=True)
class RankingSelectionService(RankingSelectionPort):
    """Apply deterministic TopK, concentration, and action rules."""

    def select(
        self,
        features: Sequence[FeatureSnapshot],
        policy: RecommendationPolicy,
        options: ScoredSelectionOptions,
        identity: ScoredSelectionIdentity,
        *,
        execution_gate_reasons: Mapping[str, str] | None = None,
    ) -> ScoredSelectionResult:
        return select_scored_features(
            features,
            policy,
            options,
            identity,
            execution_gate_reasons=execution_gate_reasons,
        )


__all__ = ["RankingSelectionPort", "RankingSelectionService"]
