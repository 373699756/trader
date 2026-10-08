"""Fixed local and DeepSeek score fusion boundary."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from trader.recommendation.application.pipeline.final_selection.decision_projection import (
    ScoredLocalProjection,
    ScoredReviewProjection,
    build_scored_hybrid,
    validate_review_manifests,
)
from trader.recommendation.application.pipeline.policy import RecommendationPolicy
from trader.recommendation.domain.evidence.review import DeepSeekReview


class ScoreFusionPort(Protocol):
    """Combine a local projection with validated optional review facts."""

    def fuse(
        self,
        projection: ScoredLocalProjection,
        policy: RecommendationPolicy,
        reviews: Mapping[str, DeepSeekReview],
        *,
        review_deadline: datetime,
        review_latency_ms: int = 0,
    ) -> ScoredReviewProjection | None: ...

    def manifests_match(
        self,
        projection: ScoredLocalProjection,
        reviews: Mapping[str, DeepSeekReview],
        expected: Mapping[str, str],
    ) -> bool: ...


@dataclass(frozen=True)
class ScoreFusionService(ScoreFusionPort):
    """Execute the fixed 68/32 fusion through the domain-backed projection."""

    monotonic: Callable[[], float] = field(default=time.monotonic, kw_only=True)

    def fuse(
        self,
        projection: ScoredLocalProjection,
        policy: RecommendationPolicy,
        reviews: Mapping[str, DeepSeekReview],
        *,
        review_deadline: datetime,
        review_latency_ms: int = 0,
    ) -> ScoredReviewProjection | None:
        return build_scored_hybrid(
            projection,
            policy,
            reviews,
            review_deadline=review_deadline,
            monotonic=self.monotonic,
            review_latency_ms=review_latency_ms,
        )

    def manifests_match(
        self,
        projection: ScoredLocalProjection,
        reviews: Mapping[str, DeepSeekReview],
        expected: Mapping[str, str],
    ) -> bool:
        return validate_review_manifests(projection, reviews, expected)


__all__ = ["ScoreFusionPort", "ScoreFusionService"]
