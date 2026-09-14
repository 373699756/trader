"""Optional review fusion boundary for recommendation decisions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from trader.application.recommendation.policy import RecommendationPolicy
from trader.application.recommendation.scored_projection import (
    ScoredLocalProjection,
    build_scored_hybrid,
    validate_review_manifests,
)
from trader.domain.recommendation.decision_identity import ScoredDecision
from trader.domain.review.models import DeepSeekReview


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
