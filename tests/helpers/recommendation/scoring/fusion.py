from __future__ import annotations
from datetime import datetime, timedelta, timezone
from trader.recommendation.domain.candidate.composition import LocalScoreResult
from trader.recommendation.domain.evidence.review import DeepSeekReview, DimensionAssessment, ReviewOutcome, RiskFact
from trader.recommendation.domain.market.models import Evidence
from trader.recommendation.domain.publication.models import FusionMode
from trader.recommendation.domain.risk.fusion import DIMENSION_NAMES, FuseScore, FusionPolicy, FusionRequest

DIMENSION_WEIGHTS = {name: 0.2 for name in DIMENSION_NAMES}

NOW = datetime(2026, 7, 16, 14, 30, tzinfo=timezone.utc)

FUSION_POLICY = FusionPolicy(25.0)


def _fuse_score(
    local: LocalScoreResult,
    local_risk_facts: tuple[RiskFact, ...],
    review: DeepSeekReview | None,
    _dimension_weights,
    risk_rules,
    fusion_mode: FusionMode,
    policy: FusionPolicy | None = None,
    *,
    evidence=(),
    evaluated_at=None,
):
    return FuseScore(
        FusionRequest(
            local=local,
            local_risk_facts=local_risk_facts,
            review=review,
            risk_rules=risk_rules,
            fusion_mode=fusion_mode,
            policy=policy or FUSION_POLICY,
            evidence=evidence,
            evaluated_at=evaluated_at,
        )
    )


def _review(score: float, *, risk_facts=()) -> DeepSeekReview:
    dimensions = {name: DimensionAssessment(name, score, 1.0, "positive") for name in DIMENSION_NAMES}
    return DeepSeekReview("600001", ReviewOutcome.APPLIED, dimensions, tuple(risk_facts), NOW)


def _risk_fact(
    fact_id: str,
    risk_code: str,
    penalty: float,
    *,
    severity: str = "medium",
    evidence_ids: tuple[str, ...] = (),
    veto: bool = False,
) -> RiskFact:
    return RiskFact(
        risk_fact_id=fact_id,
        risk_code=risk_code,
        severity=severity,
        penalty=penalty,
        source="fixture",
        observed_at=NOW,
        confidence=1.0,
        evidence_ids=evidence_ids,
        group=risk_code,
        veto=veto,
    )


def _evidence() -> Evidence:
    return Evidence("e-1", "announcement", "risk", "exchange", NOW - timedelta(hours=1))
