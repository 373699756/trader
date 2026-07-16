from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trader.domain.fusion import DIMENSION_NAMES, FusionPolicy, fuse_score
from trader.domain.models import (
    DeepSeekReview,
    DimensionAssessment,
    FusionMode,
    ReviewOutcome,
    RiskFact,
    RiskRule,
)
from trader.domain.strategies.composition import LocalScoreResult

DIMENSION_WEIGHTS = {name: 0.2 for name in DIMENSION_NAMES}
NOW = datetime(2026, 7, 16, 14, 30, tzinfo=timezone.utc)


def test_final_score_uses_68_32_and_does_not_repeat_local_risk() -> None:
    local_fact = _risk_fact("local-risk", "local_rule", 2.0)
    deepseek_fact = _risk_fact("deepseek-risk", "deepseek_rule", 0.0, evidence_ids=("e-1",))
    result = fuse_score(
        LocalScoreResult(components={"test": 82.0}, base_score=82.0),
        (local_fact,),
        _review(100.0, risk_facts=(deepseek_fact,)),
        DIMENSION_WEIGHTS,
        {"deepseek_rule": RiskRule("deepseek_rule", "medium", 3.0, 0.7, "deepseek")},
        FusionMode.HYBRID,
    )

    assert result.score.local_score == 80.0
    assert result.score.deepseek_score == 100.0
    assert result.score.deepseek_risk_penalty == 3.0
    assert result.score.final_score == 83.40


def test_same_risk_fact_is_not_deducted_twice() -> None:
    shared = _risk_fact("shared", "shared_rule", 2.0, evidence_ids=("e-1",))
    result = fuse_score(
        LocalScoreResult(components={"test": 82.0}, base_score=82.0),
        (shared,),
        _review(100.0, risk_facts=(shared,)),
        DIMENSION_WEIGHTS,
        {"shared_rule": RiskRule("shared_rule", "medium", 3.0, 0.7, "shared")},
        FusionMode.HYBRID,
    )

    assert result.score.deepseek_risk_penalty == 0.0
    assert result.score.final_score == 86.40


@pytest.mark.parametrize("mode", [FusionMode.LOCAL_DEGRADED, FusionMode.HYBRID])
def test_missing_or_degraded_review_falls_back_to_local(mode) -> None:
    review = None if mode is FusionMode.HYBRID else _review(100.0)
    result = fuse_score(
        LocalScoreResult(components={"test": 77.0}, base_score=77.0),
        (),
        review,
        DIMENSION_WEIGHTS,
        {},
        mode,
    )

    assert result.score.final_score == 77.0
    assert result.score.fusion_applied is False
    assert result.score.deepseek_risk_penalty == 0.0


def test_low_confidence_review_is_not_applied() -> None:
    dimensions = {name: DimensionAssessment(name, 100.0, 0.2, "positive") for name in DIMENSION_NAMES}
    review = DeepSeekReview("600001", ReviewOutcome.APPLIED, dimensions, (), NOW)

    result = fuse_score(
        LocalScoreResult(components={"test": 72.0}, base_score=72.0),
        (),
        review,
        DIMENSION_WEIGHTS,
        {},
        FusionMode.HYBRID,
        FusionPolicy(confidence_coverage_min=0.5),
    )

    assert result.score.confidence_coverage == 0.2
    assert result.score.final_score == 72.0


def test_fusion_policy_rejects_weights_other_than_fixed_68_32() -> None:
    with pytest.raises(ValueError, match="fixed at 0.68/0.32"):
        fuse_score(
            LocalScoreResult(components={"test": 72.0}, base_score=72.0),
            (),
            _review(80.0),
            DIMENSION_WEIGHTS,
            {},
            FusionMode.HYBRID,
            FusionPolicy(local_weight=0.5, deepseek_weight=0.5),
        )


def test_fusion_keeps_unrounded_local_precision_until_final_rounding() -> None:
    result = fuse_score(
        LocalScoreResult(components={"test": 80.005}, base_score=80.005),
        (),
        _review(100.0),
        DIMENSION_WEIGHTS,
        {},
        FusionMode.HYBRID,
    )

    assert result.score.local_score == 80.01
    assert result.score.final_score == 86.40


def _review(score: float, *, risk_facts=()) -> DeepSeekReview:
    dimensions = {name: DimensionAssessment(name, score, 1.0, "positive") for name in DIMENSION_NAMES}
    return DeepSeekReview("600001", ReviewOutcome.APPLIED, dimensions, tuple(risk_facts), NOW)


def _risk_fact(
    fact_id: str,
    risk_code: str,
    penalty: float,
    *,
    evidence_ids: tuple[str, ...] = (),
) -> RiskFact:
    return RiskFact(
        risk_fact_id=fact_id,
        risk_code=risk_code,
        severity="medium",
        penalty=penalty,
        source="fixture",
        observed_at=NOW,
        confidence=1.0,
        evidence_ids=evidence_ids,
        group=risk_code,
    )
