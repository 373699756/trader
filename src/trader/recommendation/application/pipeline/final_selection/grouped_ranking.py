"""Independent-group ranking boundary for scored recommendations."""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from trader.recommendation.application.pipeline.dynamic_filter.filter_executor import (
    ScoredSelectionIdentity,
    ScoredSelectionOptions,
    FilterFeatureCandidates,
)
from trader.recommendation.application.pipeline.policy import RecommendationPolicy
from trader.recommendation.domain.market.models import FeatureSnapshot
from trader.recommendation.domain.publication.models import ScoredSelectionResult
from trader.recommendation.domain.selection.scored_selection import (
    BuildScoredCandidateReferences,
    FilteredCandidateInputs,
    RankFilteredCandidates,
    ScorePlannedLocalEvidence,
)


@dataclass(frozen=True)
class RankingSelectionResult:
    selection: ScoredSelectionResult
    operation_durations: tuple[tuple[str, int | None], ...]


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
        population_qualifications: FilteredCandidateInputs | None = None,
    ) -> RankingSelectionResult: ...


@dataclass(frozen=True)
class RankingSelectionService(RankingSelectionPort):
    """Apply deterministic TopK, concentration, and action rules."""

    monotonic: Callable[[], float] = field(default=time.monotonic, kw_only=True)

    def select(
        self,
        features: Sequence[FeatureSnapshot],
        policy: RecommendationPolicy,
        options: ScoredSelectionOptions,
        identity: ScoredSelectionIdentity,
        *,
        execution_gate_reasons: Mapping[str, str] | None = None,
        population_qualifications: FilteredCandidateInputs | None = None,
    ) -> RankingSelectionResult:
        started = self.monotonic()
        filtered = FilterFeatureCandidates(
            features,
            policy,
            options,
            identity,
            execution_gate_reasons=execution_gate_reasons,
            population_qualifications=population_qualifications,
        )
        qualified = self.monotonic()
        references = BuildScoredCandidateReferences(filtered)
        referenced = self.monotonic()
        plan = RankFilteredCandidates(references)
        ranked = self.monotonic()
        selection = ScorePlannedLocalEvidence(filtered.request, plan)
        completed = self.monotonic()
        return RankingSelectionResult(selection, (
            ("candidate_preparation_and_qualification", max(0, int((qualified - started) * 1000))),
            ("board_reference_build", max(0, int((referenced - qualified) * 1000))),
            ("candidate_policy_and_ranking", max(0, int((ranked - referenced) * 1000))),
            ("local_component_scoring", max(0, int((completed - ranked) * 1000))),
        ))


__all__ = ["RankingSelectionPort", "RankingSelectionResult", "RankingSelectionService"]
