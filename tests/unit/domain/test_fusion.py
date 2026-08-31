from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trader.domain.market.models import Evidence
from trader.domain.recommendation.models import FusionMode
from trader.domain.recommendation.risk_fusion.fusion import DIMENSION_NAMES, FusionPolicy, FusionRequest, fuse_score
from trader.domain.recommendation.strategies.composition import LocalScoreResult
from trader.domain.review.models import (
    DeepSeekReview,
    DimensionAssessment,
    ReviewOutcome,
    RiskFact,
    RiskRule,
)
from trader.domain.review.rules import deepseek_risk_rule_code

DIMENSION_WEIGHTS = {name: 0.2 for name in DIMENSION_NAMES}
NOW = datetime(2026, 7, 16, 14, 30, tzinfo=timezone.utc)


def _fuse_score(
    local: LocalScoreResult,
    local_risk_facts: tuple[RiskFact, ...],
    review: DeepSeekReview | None,
    dimension_weights,
    risk_rules,
    fusion_mode: FusionMode,
    policy: FusionPolicy | None = None,
    *,
    evidence=(),
    evaluated_at=None,
):
    return fuse_score(
        FusionRequest(
            local=local,
            local_risk_facts=local_risk_facts,
            review=review,
            dimension_weights=dimension_weights,
            risk_rules=risk_rules,
            fusion_mode=fusion_mode,
            policy=policy or FusionPolicy(),
            evidence=evidence,
            evaluated_at=evaluated_at,
        )
    )


def test_final_score_uses_68_32_and_does_not_repeat_local_risk() -> None:
    local_fact = _risk_fact("local-risk", "local_rule", 2.0)
    deepseek_fact = _risk_fact("deepseek-risk", "regulatory_risk", 0.0, evidence_ids=("e-1",))
    result = _fuse_score(
        LocalScoreResult(components={"test": 82.0}, base_score=82.0),
        (local_fact,),
        _review(100.0, risk_facts=(deepseek_fact,)),
        DIMENSION_WEIGHTS,
        {"regulatory_risk": RiskRule("regulatory_risk", "medium", 3.0, 0.7, "deepseek", 24, False, ("announcement",))},
        FusionMode.HYBRID,
        evidence=(_evidence(),),
        evaluated_at=NOW,
    )

    assert result.score.local_score == 80.0
    assert result.score.deepseek_score == 100.0
    assert result.score.deepseek_risk_penalty == 3.0
    assert result.score.final_score == 83.40


def test_same_risk_fact_is_not_deducted_twice() -> None:
    shared = _risk_fact("shared", "regulatory_risk", 2.0, evidence_ids=("e-1",))
    result = _fuse_score(
        LocalScoreResult(components={"test": 82.0}, base_score=82.0),
        (shared,),
        _review(100.0, risk_facts=(shared,)),
        DIMENSION_WEIGHTS,
        {"regulatory_risk": RiskRule("regulatory_risk", "medium", 3.0, 0.7, "shared", 24, False, ("announcement",))},
        FusionMode.HYBRID,
        evidence=(_evidence(),),
        evaluated_at=NOW,
    )

    assert result.score.deepseek_risk_penalty == 0.0
    assert result.score.final_score == 86.40


def test_local_rule_veto_is_preserved_without_model_review() -> None:
    local_fact = _risk_fact("local-veto", "regulatory_risk", 15.0, veto=True)

    result = _fuse_score(
        LocalScoreResult(components={"test": 80.0}, base_score=80.0),
        (local_fact,),
        None,
        DIMENSION_WEIGHTS,
        {},
        FusionMode.LOCAL_DEGRADED,
    )

    assert result.veto is True


def test_local_rule_veto_is_preserved_with_model_review() -> None:
    local_fact = _risk_fact("local-veto", "regulatory_risk", 15.0, veto=True)

    result = _fuse_score(
        LocalScoreResult(components={"test": 80.0}, base_score=80.0),
        (local_fact,),
        _review(80.0),
        DIMENSION_WEIGHTS,
        {},
        FusionMode.HYBRID,
    )

    assert result.veto is True


@pytest.mark.parametrize(
    ("raw_code", "severity", "expected_rule"),
    (
        ("regulatory_risk", "low", "regulatory_risk"),
        ("shareholder_reduction", "low", "reduction_or_unlock_low"),
        ("unlock_risk", "high", "reduction_or_unlock_high"),
        ("pledge_risk", "medium", "pledge_risk_medium"),
        ("litigation_risk", "high", "negative_announcement"),
        ("earnings_risk", "low", "negative_announcement"),
    ),
)
def test_v4_risk_facts_are_normalized_to_registered_local_rules(
    raw_code: str,
    severity: str,
    expected_rule: str,
) -> None:
    model_fact = _risk_fact(
        "deepseek-risk",
        raw_code,
        0.0,
        severity=severity,
        evidence_ids=("e-1",),
    )
    result = _fuse_score(
        LocalScoreResult(components={"test": 80.0}, base_score=80.0),
        (),
        _review(80.0, risk_facts=(model_fact,)),
        DIMENSION_WEIGHTS,
        {
            expected_rule: RiskRule(
                expected_rule,
                "medium",
                4.0,
                0.7,
                "event",
                24,
                False,
                ("announcement",),
            )
        },
        FusionMode.HYBRID,
        evidence=(_evidence(),),
        evaluated_at=NOW,
    )

    assert tuple(fact.risk_code for fact in result.deepseek_risk_facts) == (expected_rule,)
    assert result.score.deepseek_risk_penalty == 4.0


@pytest.mark.parametrize("severity", ("low", "medium", "high"))
@pytest.mark.parametrize(
    ("raw_code", "expected_template"),
    (
        ("regulatory_risk", "regulatory_risk"),
        ("shareholder_reduction", "reduction_or_unlock_{severity}"),
        ("unlock_risk", "reduction_or_unlock_{severity}"),
        ("pledge_risk", "pledge_risk_{severity}"),
        ("litigation_risk", "negative_announcement"),
        ("earnings_risk", "negative_announcement"),
    ),
)
def test_v4_risk_mapping_is_total_for_every_schema_severity(
    raw_code: str,
    expected_template: str,
    severity: str,
) -> None:
    assert deepseek_risk_rule_code(raw_code, severity) == expected_template.format(severity=severity)


def test_unregistered_model_risk_code_fails_closed() -> None:
    model_fact = _risk_fact("deepseek-risk", "unregistered_model_risk", 0.0, evidence_ids=("e-1",))
    result = _fuse_score(
        LocalScoreResult(components={"test": 80.0}, base_score=80.0),
        (),
        _review(80.0, risk_facts=(model_fact,)),
        DIMENSION_WEIGHTS,
        {
            "unregistered_model_risk": RiskRule(
                "unregistered_model_risk", "medium", 4.0, 0.7, "event", 24, False, ("announcement",)
            )
        },
        FusionMode.HYBRID,
        evidence=(_evidence(),),
        evaluated_at=NOW,
    )

    assert result.deepseek_risk_facts == ()
    assert result.score.deepseek_risk_penalty == 0.0


@pytest.mark.parametrize("mode", [FusionMode.LOCAL_DEGRADED, FusionMode.HYBRID])
def test_missing_or_degraded_review_falls_back_to_local(mode) -> None:
    review = None if mode is FusionMode.HYBRID else _review(100.0)
    result = _fuse_score(
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

    result = _fuse_score(
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
        _fuse_score(
            LocalScoreResult(components={"test": 72.0}, base_score=72.0),
            (),
            _review(80.0),
            DIMENSION_WEIGHTS,
            {},
            FusionMode.HYBRID,
            FusionPolicy(local_weight=0.5, deepseek_weight=0.5),
        )


def test_fusion_keeps_unrounded_local_precision_until_final_rounding() -> None:
    result = _fuse_score(
        LocalScoreResult(components={"test": 80.005}, base_score=80.005),
        (),
        _review(100.0),
        DIMENSION_WEIGHTS,
        {},
        FusionMode.HYBRID,
    )

    assert result.score.local_score == 80.01
    assert result.score.final_score == 86.40


@pytest.mark.parametrize(
    ("evidence", "expected_veto"),
    [
        (Evidence("e-1", "announcement", "risk", "exchange", NOW - timedelta(hours=1)), True),
        (Evidence("e-1", "news", "risk", "media", NOW - timedelta(hours=1)), False),
        (Evidence("e-1", "announcement", "risk", "exchange", NOW - timedelta(hours=25)), False),
    ],
)
def test_veto_is_mapped_only_by_local_rule_with_valid_fresh_evidence(
    evidence: Evidence,
    expected_veto: bool,
) -> None:
    model_fact = _risk_fact("deepseek-risk", "regulatory_risk", 0.0, evidence_ids=("e-1",), veto=True)
    result = _fuse_score(
        LocalScoreResult(components={"test": 80.0}, base_score=80.0),
        (),
        _review(80.0, risk_facts=(model_fact,)),
        DIMENSION_WEIGHTS,
        {"regulatory_risk": RiskRule("regulatory_risk", "high", 15.0, 0.7, "event", 24, True, ("announcement",))},
        FusionMode.HYBRID,
        evidence=(evidence,),
        evaluated_at=NOW,
    )

    assert result.veto is expected_veto
    assert bool(result.deepseek_risk_facts) is expected_veto


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
