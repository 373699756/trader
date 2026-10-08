"""Confidence-aware recommendation score fusion."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType

from trader.recommendation.domain.candidate.composition import LocalScoreResult
from trader.recommendation.domain.evidence.review import (
    DeepSeekReview,
    ReviewOutcome,
    RiskFact,
    RiskRule,
)
from trader.recommendation.domain.market.factors import clamp, round_score
from trader.recommendation.domain.market.models import Evidence
from trader.recommendation.domain.publication.models import (
    FusionMode,
    ScoreBreakdown,
)
from trader.recommendation.domain.risk.rules import RiskMappingRequest, aggregate_risk_penalty, map_deepseek_risk_facts

DIMENSION_NAMES = (
    "value_quality",
    "financial_health",
    "market_flow",
    "industry_policy",
    "risk_quality",
)

STRUCTURED_REVIEW_FEATURES = frozenset(
    {
        "amount_median_20d",
        "volatility_20d",
        "max_drawdown_20d",
        "ma_slope",
        "upward_consistency",
        "news_sentiment",
        "evidence_freshness",
        "financial_deterioration",
        "reduction_or_unlock",
        "pledge_risk",
        "negative_announcement_level",
        "value_score",
        "growth_score",
        "quality_score",
        "industry_policy_score",
        "risk_protection_score",
    }
)


@dataclass(frozen=True)
class FusionPolicy:
    local_weight: float
    deepseek_weight: float
    confidence_coverage_min: float
    minimum_known_dimensions: int
    local_risk_cap: float
    deepseek_risk_cap: float


@dataclass(frozen=True)
class FusionResult:
    score: ScoreBreakdown
    deepseek_risk_facts: tuple[RiskFact, ...]
    veto: bool


@dataclass(frozen=True)
class FusionRequest:
    local: LocalScoreResult
    local_risk_facts: tuple[RiskFact, ...]
    review: DeepSeekReview | None
    dimension_weights: Mapping[str, float]
    risk_rules: Mapping[str, RiskRule]
    fusion_mode: FusionMode
    policy: FusionPolicy
    evidence: Sequence[Evidence] = ()
    evaluated_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "local_risk_facts", tuple(self.local_risk_facts))
        object.__setattr__(self, "dimension_weights", MappingProxyType(dict(self.dimension_weights)))
        object.__setattr__(self, "risk_rules", MappingProxyType(dict(self.risk_rules)))
        object.__setattr__(self, "evidence", tuple(self.evidence))


def fuse_score(request: FusionRequest) -> FusionResult:
    policy = request.policy
    _validate_policy(policy)
    local_risk_penalty = aggregate_risk_penalty(request.local_risk_facts, cap=policy.local_risk_cap)
    local_score = _bounded_decimal(Decimal(str(request.local.base_score)) - Decimal(str(local_risk_penalty)))
    deepseek_score, coverage, known_dimensions, review_applies = _review_score(
        request.review,
        request.dimension_weights,
    )
    review_applies = (
        review_applies
        and coverage >= policy.confidence_coverage_min
        and known_dimensions >= policy.minimum_known_dimensions
    )

    mapped_risk_facts: tuple[RiskFact, ...] = ()
    mapped_penalty = 0.0
    veto = any(fact.veto for fact in request.local_risk_facts)
    if request.review is not None:
        mapped_risk_facts, mapped_penalty, mapped_veto = map_deepseek_risk_facts(
            RiskMappingRequest(
                raw_facts=request.review.risk_facts,
                rules=request.risk_rules,
                local_fact_ids=frozenset(fact.risk_fact_id for fact in request.local_risk_facts),
                cap=policy.deepseek_risk_cap,
                evidence=request.evidence,
                evaluated_at=request.evaluated_at or request.review.completed_at,
            )
        )
        veto = veto or mapped_veto

    fusion_applied = review_applies and request.fusion_mode is FusionMode.HYBRID and deepseek_score is not None
    if fusion_applied:
        assert deepseek_score is not None
        raw_final = (
            local_score * Decimal(str(policy.local_weight))
            + deepseek_score * Decimal(str(policy.deepseek_weight))
            - Decimal(str(mapped_penalty))
        )
        final_score = round_score(raw_final)
        applied_penalty = mapped_penalty
    else:
        final_score = round_score(local_score)
        applied_penalty = 0.0

    return FusionResult(
        score=ScoreBreakdown(
            components=request.local.components,
            base_score=round_score(request.local.base_score),
            local_risk_penalty=round_score(local_risk_penalty),
            local_score=round_score(local_score),
            deepseek_score=round_score(deepseek_score) if deepseek_score is not None else None,
            confidence_coverage=round(coverage, 4),
            deepseek_risk_penalty=round_score(applied_penalty),
            final_score=final_score,
            fusion_mode=request.fusion_mode,
            fusion_applied=fusion_applied,
        ),
        deepseek_risk_facts=mapped_risk_facts,
        veto=veto,
    )


def _review_score(
    review: DeepSeekReview | None,
    weights: Mapping[str, float],
) -> tuple[Decimal | None, float, int, bool]:
    if set(weights) != set(DIMENSION_NAMES) or abs(sum(weights.values()) - 1.0) > 1e-9:
        raise ValueError("DeepSeek dimension weights must contain five dimensions and sum to 1.0")
    if review is None or review.outcome is not ReviewOutcome.APPLIED:
        return None, 0.0, 0, False
    total = Decimal("0")
    coverage = Decimal("0")
    known = 0
    for name in DIMENSION_NAMES:
        if weights[name] == 0.0:
            continue
        dimension = review.dimensions.get(name)
        if dimension is None or dimension.is_unknown:
            total += Decimal("50") * Decimal(str(weights[name]))
            continue
        score = Decimal(str(clamp(dimension.score)))
        confidence = Decimal(str(clamp(dimension.confidence, 0.0, 1.0)))
        effective = Decimal("50") + (score - Decimal("50")) * confidence
        weight = Decimal(str(weights[name]))
        total += effective * weight
        coverage += confidence * weight
        known += 1
    return _bounded_decimal(total), float(coverage), known, True


def _bounded_decimal(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise ValueError("score must be finite")
    return min(Decimal("100"), max(Decimal("0"), value))


def _validate_policy(policy: FusionPolicy) -> None:
    if any(
        not math.isfinite(value) or not 0.0 <= value <= 1.0 for value in (policy.local_weight, policy.deepseek_weight)
    ):
        raise ValueError("fusion weights must be finite and between 0 and 1")
    if abs(policy.local_weight + policy.deepseek_weight - 1.0) > 1e-9:
        raise ValueError("fusion weights must sum to 1.0")
    if not 0.0 <= policy.confidence_coverage_min <= 1.0:
        raise ValueError("confidence coverage must be between 0 and 1")
    if policy.minimum_known_dimensions < 1:
        raise ValueError("minimum known dimensions must be positive")
    if any(
        not math.isfinite(value) or not 0.0 <= value <= 100.0
        for value in (policy.local_risk_cap, policy.deepseek_risk_cap)
    ):
        raise ValueError("risk caps must be finite and between 0 and 100")


__all__ = [
    "DIMENSION_NAMES",
    "STRUCTURED_REVIEW_FEATURES",
    "FusionPolicy",
    "FusionRequest",
    "FusionResult",
    "fuse_score",
]
