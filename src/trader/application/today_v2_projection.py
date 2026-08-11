"""Native Today input projection to unified V2 scored-decision identities."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from trader.application.policy import RecommendationPolicy
from trader.application.ports.tomorrow import TodayNativeInput
from trader.application.tomorrow_deepseek_fusion import today_decision_policy
from trader.application.tomorrow_v2_projection import (
    TomorrowV2LocalProjection,
    build_scored_v2_hybrid,
    build_scored_v2_local,
    validate_review_manifests,
)
from trader.domain.recommendation.decision_identity import ScoredDecision
from trader.domain.recommendation.models import Strategy
from trader.domain.review.models import DeepSeekReview

TodayV2LocalProjection = TomorrowV2LocalProjection


def build_today_v2_local(
    native_input: TodayNativeInput,
    policy: RecommendationPolicy,
    *,
    sequence: int,
) -> TodayV2LocalProjection:
    return build_scored_v2_local(
        native_input,
        policy,
        decision_policy=today_decision_policy(policy, native_input.phase),
        strategy=Strategy.TODAY,
        sequence=sequence,
    )


def build_today_v2_hybrid(
    projection: TodayV2LocalProjection,
    policy: RecommendationPolicy,
    reviews: Mapping[str, DeepSeekReview],
    *,
    review_deadline: datetime,
) -> ScoredDecision | None:
    if projection.local.strategy is not Strategy.TODAY:
        return None
    return build_scored_v2_hybrid(
        projection,
        policy,
        reviews,
        decision_policy=today_decision_policy(policy, projection.native_input.phase),
        review_deadline=review_deadline,
    )


__all__ = [
    "TodayV2LocalProjection",
    "build_today_v2_hybrid",
    "build_today_v2_local",
    "validate_review_manifests",
]
