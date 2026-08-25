"""Scored-strategy decision policies and review-time normalization."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime

from trader.application.policy import RecommendationPolicy
from trader.domain.recommendation.models import Strategy
from trader.domain.recommendation.scored_fusion import ScoredDecisionPolicy
from trader.domain.review.models import DeepSeekReview, ReviewOutcome


def tomorrow_decision_policy(policy: RecommendationPolicy) -> ScoredDecisionPolicy:
    return _decision_policy(policy, Strategy.TOMORROW, "tomorrow")


def d25_decision_policy(policy: RecommendationPolicy) -> ScoredDecisionPolicy:
    return _decision_policy(policy, Strategy.D25, "d25")


def v2_decision_policy(
    policy: RecommendationPolicy,
    strategy: Strategy,
    *,
    phase: str = "",
) -> ScoredDecisionPolicy:
    if strategy is Strategy.TODAY:
        return today_decision_policy(policy, phase)
    if strategy is Strategy.TOMORROW:
        return tomorrow_decision_policy(policy)
    if strategy is Strategy.D25:
        return d25_decision_policy(policy)
    raise ValueError("unsupported V2 strategy for DeepSeek policy")


def today_decision_policy(policy: RecommendationPolicy, phase: str) -> ScoredDecisionPolicy:
    threshold_key = "today_late" if phase == "today_late" else "today_main"
    return _decision_policy(
        policy,
        Strategy.TODAY,
        threshold_key,
        executable_enabled=phase != "today_observe",
    )


def _decision_policy(
    policy: RecommendationPolicy,
    strategy: Strategy,
    threshold_key: str,
    *,
    executable_enabled: bool = True,
) -> ScoredDecisionPolicy:
    return ScoredDecisionPolicy(
        strategy=strategy,
        dimension_weights=policy.dimension_weights[strategy],
        risk_rules=policy.risk_rules,
        executable_threshold=policy.selection.thresholds[threshold_key],
        observation_margin=policy.selection.observation_margin,
        review_candidate_limit=min(policy.selection.review_candidate_limit, 28),
        top_k=min(policy.selection.default_top_k, 10),
        observation_limit=min(max(0, policy.selection.maximum_top_k - policy.selection.default_top_k), 8),
        maximum_per_industry=policy.selection.maximum_per_industry,
        maximum_board_fraction=min(policy.selection.maximum_board_fraction, 0.60),
        fusion=policy.fusion,
        executable_enabled=executable_enabled,
    )


def normalize_scored_review_times(
    reviews: Mapping[str, DeepSeekReview],
    deadline: datetime,
) -> dict[str, DeepSeekReview] | None:
    normalized: dict[str, DeepSeekReview] = {}
    for code, review in reviews.items():
        if review.completed_at.tzinfo is None or review.completed_at.utcoffset() is None:
            return None
        if any(fact.observed_at.tzinfo is None or fact.observed_at.utcoffset() is None for fact in review.risk_facts):
            return None
        completed_at = review.completed_at.astimezone(deadline.tzinfo)
        risk_facts = tuple(
            replace(fact, observed_at=fact.observed_at.astimezone(deadline.tzinfo)) for fact in review.risk_facts
        )
        if any(fact.observed_at > completed_at for fact in risk_facts):
            return None
        is_late = completed_at > deadline
        normalized[code] = replace(
            review,
            outcome=ReviewOutcome.LATE if is_late else review.outcome,
            completed_at=completed_at,
            risk_facts=risk_facts,
            error="review_completed_after_deadline" if is_late else review.error,
        )
    return normalized


__all__ = [
    "d25_decision_policy",
    "normalize_scored_review_times",
    "today_decision_policy",
    "tomorrow_decision_policy",
    "v2_decision_policy",
]
