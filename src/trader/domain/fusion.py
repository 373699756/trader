"""Confidence-aware DeepSeek score fusion with deterministic risk handling."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from trader.domain.factors import clamp, round_score
from trader.domain.models import (
    DeepSeekReview,
    FusionMode,
    ReviewOutcome,
    RiskFact,
    RiskRule,
    ScoreBreakdown,
)
from trader.domain.risk import aggregate_risk_penalty, map_deepseek_risk_facts
from trader.domain.strategies.composition import LocalScoreResult

DIMENSION_NAMES = (
    "value_quality",
    "financial_health",
    "market_flow",
    "industry_policy",
    "risk_quality",
)


@dataclass(frozen=True)
class FusionPolicy:
    local_weight: float = 0.68
    deepseek_weight: float = 0.32
    confidence_coverage_min: float = 0.50
    minimum_known_dimensions: int = 2
    local_risk_cap: float = 25.0
    deepseek_risk_cap: float = 30.0


@dataclass(frozen=True)
class FusionResult:
    score: ScoreBreakdown
    deepseek_risk_facts: tuple[RiskFact, ...]
    veto: bool


def fuse_score(
    local: LocalScoreResult,
    local_risk_facts: tuple[RiskFact, ...],
    review: DeepSeekReview | None,
    dimension_weights: Mapping[str, float],
    risk_rules: Mapping[str, RiskRule],
    fusion_mode: FusionMode,
    policy: FusionPolicy | None = None,
) -> FusionResult:
    policy = policy or FusionPolicy()
    _validate_policy(policy)
    local_risk_penalty = aggregate_risk_penalty(local_risk_facts, cap=policy.local_risk_cap)
    local_score = clamp(local.base_score - local_risk_penalty)
    deepseek_score, coverage, known_dimensions, review_applies = _review_score(review, dimension_weights)
    review_applies = (
        review_applies
        and coverage >= policy.confidence_coverage_min
        and known_dimensions >= policy.minimum_known_dimensions
    )

    mapped_risk_facts: tuple[RiskFact, ...] = ()
    mapped_penalty = 0.0
    veto = False
    if review is not None:
        mapped_risk_facts, mapped_penalty, veto = map_deepseek_risk_facts(
            review.risk_facts,
            risk_rules,
            {fact.risk_fact_id for fact in local_risk_facts},
            cap=policy.deepseek_risk_cap,
        )

    fusion_applied = review_applies and fusion_mode is FusionMode.HYBRID and deepseek_score is not None
    if fusion_applied:
        assert deepseek_score is not None
        raw_final = local_score * policy.local_weight + deepseek_score * policy.deepseek_weight - mapped_penalty
        final_score = round_score(raw_final)
        applied_penalty = mapped_penalty
    else:
        final_score = round_score(local_score)
        applied_penalty = 0.0

    return FusionResult(
        score=ScoreBreakdown(
            components=local.components,
            base_score=round_score(local.base_score),
            local_risk_penalty=round_score(local_risk_penalty),
            local_score=round_score(local_score),
            deepseek_score=round_score(deepseek_score) if deepseek_score is not None else None,
            confidence_coverage=round(coverage, 4),
            deepseek_risk_penalty=round_score(applied_penalty),
            final_score=final_score,
            fusion_mode=fusion_mode,
            fusion_applied=fusion_applied,
        ),
        deepseek_risk_facts=mapped_risk_facts,
        veto=veto,
    )


def _review_score(
    review: DeepSeekReview | None,
    weights: Mapping[str, float],
) -> tuple[float | None, float, int, bool]:
    if set(weights) != set(DIMENSION_NAMES) or abs(sum(weights.values()) - 1.0) > 1e-9:
        raise ValueError("DeepSeek dimension weights must contain five dimensions and sum to 1.0")
    if review is None or review.outcome is not ReviewOutcome.APPLIED:
        return None, 0.0, 0, False
    total = 0.0
    coverage = 0.0
    known = 0
    for name in DIMENSION_NAMES:
        dimension = review.dimensions.get(name)
        if dimension is None or dimension.is_unknown:
            total += 50.0 * weights[name]
            continue
        score = clamp(dimension.score)
        confidence = clamp(dimension.confidence, 0.0, 1.0)
        effective = 50.0 + (score - 50.0) * confidence
        total += effective * weights[name]
        coverage += confidence * weights[name]
        known += 1
    return clamp(total), coverage, known, True


def _validate_policy(policy: FusionPolicy) -> None:
    if abs(policy.local_weight + policy.deepseek_weight - 1.0) > 1e-9:
        raise ValueError("fusion weights must sum to 1.0")
    if abs(policy.local_weight - 0.68) > 1e-9 or abs(policy.deepseek_weight - 0.32) > 1e-9:
        raise ValueError("fusion weights are fixed at 0.68/0.32")
    if not 0.0 <= policy.confidence_coverage_min <= 1.0:
        raise ValueError("confidence coverage must be between 0 and 1")


__all__ = ["DIMENSION_NAMES", "FusionPolicy", "FusionResult", "fuse_score"]
