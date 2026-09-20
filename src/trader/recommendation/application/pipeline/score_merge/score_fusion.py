"""Fixed local and DeepSeek score fusion boundary."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from trader.recommendation.application.pipeline.final_selection.decision_projection import (
    ScoredLocalProjection,
    build_scored_hybrid,
    validate_review_manifests,
)
from trader.recommendation.application.pipeline.policy import RecommendationPolicy
from trader.recommendation.domain.evidence.review import DeepSeekReview
from trader.recommendation.domain.publication.decision_identity import ScoredDecision


class ScoreFusionPort(Protocol):
    """Combine a local projection with validated optional review facts."""

    def fuse(
        self,
        projection: ScoredLocalProjection,
        policy: RecommendationPolicy,
        reviews: Mapping[str, DeepSeekReview],
        *,
        review_deadline: datetime,
    ) -> ScoredDecision | None: ...

    def manifests_match(
        self,
        projection: ScoredLocalProjection,
        reviews: Mapping[str, DeepSeekReview],
        expected: Mapping[str, str],
    ) -> bool: ...


@dataclass(frozen=True)
class ScoreFusionService(ScoreFusionPort):
    """Execute the fixed 68/32 fusion through the domain-backed projection."""

    def fuse(
        self,
        projection: ScoredLocalProjection,
        policy: RecommendationPolicy,
        reviews: Mapping[str, DeepSeekReview],
        *,
        review_deadline: datetime,
    ) -> ScoredDecision | None:
        return build_scored_hybrid(
            projection,
            policy,
            reviews,
            review_deadline=review_deadline,
        )

    def manifests_match(
        self,
        projection: ScoredLocalProjection,
        reviews: Mapping[str, DeepSeekReview],
        expected: Mapping[str, str],
    ) -> bool:
        return validate_review_manifests(projection, reviews, expected)


__all__ = ["ScoreFusionPort", "ScoreFusionService"]
