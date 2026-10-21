"""Local final score with optional evidence-backed risk review."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
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
from trader.recommendation.domain.market.factors import RoundScore
from trader.recommendation.domain.market.models import Evidence
from trader.recommendation.domain.publication.models import (
    FusionMode,
    ScoreBreakdown,
)
from trader.recommendation.domain.risk.rules import AggregateRiskPenalty, MapDeepseekRiskFacts, RiskMappingRequest

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
    local_risk_cap: float


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
    risk_rules: Mapping[str, RiskRule]
    fusion_mode: FusionMode
    policy: FusionPolicy
    evidence: Sequence[Evidence] = ()
    evaluated_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "local_risk_facts", tuple(self.local_risk_facts))
        object.__setattr__(self, "risk_rules", MappingProxyType(dict(self.risk_rules)))
        object.__setattr__(self, "evidence", tuple(self.evidence))


def FuseScore(request: FusionRequest) -> FusionResult:
    policy = request.policy
    _validate_policy(policy)
    local_risk_penalty = AggregateRiskPenalty(request.local_risk_facts, cap=policy.local_risk_cap)
    local_score = _bounded_decimal(Decimal(str(request.local.base_score)) - Decimal(str(local_risk_penalty)))
    score = ScoreBreakdown(
        components=request.local.components,
        base_score=RoundScore(request.local.base_score),
        local_risk_penalty=RoundScore(local_risk_penalty),
        local_score=RoundScore(local_score),
        deepseek_score=None,
        confidence_coverage=0.0,
        deepseek_risk_penalty=0.0,
        final_score=RoundScore(local_score),
        fusion_mode=request.fusion_mode,
        fusion_applied=False,
    )
    return ApplyStructuredReview(score, request)


def ApplyStructuredReview(score: ScoreBreakdown, request: FusionRequest) -> FusionResult:
    """Apply evidence-backed review to an already risk-deducted local score."""
    mapped_risk_facts: tuple[RiskFact, ...] = ()
    veto = any(fact.veto for fact in request.local_risk_facts)
    if (
        request.fusion_mode is FusionMode.HYBRID
        and request.review is not None
        and request.review.outcome is ReviewOutcome.APPLIED
    ):
        mapped_risk_facts, _mapped_penalty, mapped_veto = MapDeepseekRiskFacts(
            RiskMappingRequest(
                raw_facts=request.review.risk_facts,
                rules=request.risk_rules,
                local_fact_ids=frozenset(fact.risk_fact_id for fact in request.local_risk_facts),
                cap=0.0,
                evidence=request.evidence,
                evaluated_at=request.evaluated_at or request.review.completed_at,
            )
        )
        mapped_risk_facts = tuple(replace(fact, penalty=0.0) for fact in mapped_risk_facts)
        veto = veto or mapped_veto

    return FusionResult(score, mapped_risk_facts, veto)


def _bounded_decimal(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise ValueError("score must be finite")
    return min(Decimal("100"), max(Decimal("0"), value))


def _validate_policy(policy: FusionPolicy) -> None:
    if not math.isfinite(policy.local_risk_cap) or not 0.0 <= policy.local_risk_cap <= 100.0:
        raise ValueError("local risk cap must be finite and between 0 and 100")


__all__ = [
    "DIMENSION_NAMES",
    "STRUCTURED_REVIEW_FEATURES",
    "FusionPolicy",
    "FusionRequest",
    "FusionResult",
    "FuseScore",
    "ApplyStructuredReview",
]
