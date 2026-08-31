from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from trader.domain.market.models import Board, Evidence, FeatureSnapshot, MarketQuote
from trader.domain.recommendation.models import (
    RecommendationAction,
    ScoredDisposition,
    ScoredSelectionResult,
    ScoredStockEvaluation,
)
from trader.domain.recommendation.risk_fusion.scored_fusion import (
    DecisionSelectionLimits,
    ScoredDecisionPolicy,
    ScoredDecisionRequest,
    build_scored_decision_epoch,
    select_scored_review_candidates,
)
from trader.domain.review.models import DeepSeekReview, DimensionAssessment, ReviewOutcome, RiskFact, RiskRule

NOW = datetime(2026, 7, 28, 14, 40, tzinfo=ZoneInfo("Asia/Shanghai"))
DIMENSIONS = ("value_quality", "financial_health", "market_flow", "industry_policy", "risk_quality")


def test_review_candidates_are_deterministic_bounded_and_exclude_non_pass() -> None:
    evaluations = tuple(
        _evaluation(
            index,
            local_score=95.0 - index / 2,
            disposition=ScoredDisposition.OBSERVE_ONLY if index in {0, 1} else ScoredDisposition.PASS,
        )
        for index in range(40)
    )
    selection = _selection(tuple(reversed(evaluations)))

    forward = select_scored_review_candidates(selection, _policy())
    reverse = select_scored_review_candidates(
        replace(selection, evaluations=tuple(reversed(selection.evaluations))),
        _policy(),
    )

    assert len(forward) == 28
    assert tuple(item.code for item in forward) == tuple(item.code for item in reverse)
    assert {"600000", "600001"}.isdisjoint(item.code for item in forward)
    assert all(item.context.in_protection_set for item in forward)


def test_hybrid_decision_uses_fixed_fusion_without_repeating_local_risk() -> None:
    local_fact = _risk_fact("local-risk", "local_rule", penalty=2.0)
    deepseek_fact = _risk_fact(
        "deepseek-risk",
        "regulatory_risk",
        penalty=0.0,
        evidence_ids=("official-risk",),
    )
    evaluation = _evaluation(
        1,
        local_score=80.0,
        local_base_score=82.0,
        local_risk_penalty=2.0,
        local_risk_facts=(local_fact,),
        evidence=(_evidence(),),
    )
    review = _review("600001", 100.0, risk_facts=(deepseek_fact,))

    epoch = build_scored_decision_epoch(
        _request(
            _selection((evaluation,)),
            reviews={"600001": review},
            projection_stage="hybrid",
            review_candidate_codes=("600001",),
            parent_decision_version="decision:local",
        )
    )

    decision = epoch.entries[0]
    assert decision.score.local_score == 80.0
    assert decision.score.deepseek_score == 100.0
    assert decision.score.deepseek_risk_penalty == 3.0
    assert decision.score.final_score == 83.40
    assert decision.score.fusion_applied is True
    assert decision.action is RecommendationAction.EXECUTABLE


def test_downside_guard_downgrades_executable_without_changing_fixed_fusion() -> None:
    evaluation = _evaluation(
        1,
        local_score=80.0,
        values={"trend_breakdown": 1.0},
        evidence=(_evidence(),),
    )
    review = _review("600001", 100.0)

    epoch = build_scored_decision_epoch(
        _request(
            _selection((evaluation,)),
            reviews={"600001": review},
            projection_stage="hybrid",
            review_candidate_codes=("600001",),
            parent_decision_version="decision:local",
        )
    )

    decision = epoch.entries[0]
    assert decision.score.final_score == 86.40
    assert decision.action is RecommendationAction.OBSERVE
    assert decision.action_reason == "downside_guard:trend_breakdown"
    assert decision.selected is True


def test_missing_downside_input_fails_closed_after_execution_threshold() -> None:
    evaluation = _evaluation(1, local_score=80.0, values={"atr20_pct": None})

    epoch = build_scored_decision_epoch(_request(_selection((evaluation,))))

    decision = epoch.entries[0]
    assert decision.score.final_score == 80.0
    assert decision.action is RecommendationAction.OBSERVE
    assert decision.action_reason == "downside_guard:downside_inputs_missing"


def test_late_review_cannot_change_score_or_create_model_risk() -> None:
    late_evaluation = _evaluation(1, local_score=80.0, evidence=(_evidence(),))
    current_evaluation = _evaluation(2, local_score=79.0, evidence=(_evidence(),))
    review = replace(
        _review(
            "600001",
            100.0,
            risk_facts=(
                _risk_fact(
                    "deepseek-risk",
                    "regulatory_risk",
                    penalty=0.0,
                    evidence_ids=("official-risk",),
                ),
            ),
        ),
        completed_at=NOW + timedelta(seconds=1),
    )

    epoch = build_scored_decision_epoch(
        _request(
            _selection((late_evaluation, current_evaluation)),
            reviews={"600001": review, "600002": _review("600002", 90.0)},
            projection_stage="hybrid",
            review_candidate_codes=("600001", "600002"),
            parent_decision_version="decision:local",
        )
    )

    decision = next(item for item in epoch.entries if item.code == "600001")
    assert decision.score.final_score == 80.0
    assert decision.score.deepseek_risk_penalty == 0.0
    assert decision.deepseek_risk_facts == ()
    assert decision.review_outcome is ReviewOutcome.LATE


def test_abstain_review_cannot_apply_model_risk_or_veto() -> None:
    evaluation = _evaluation(1, local_score=80.0, evidence=(_evidence(),))
    abstain = replace(
        _review(
            "600001",
            100.0,
            risk_facts=(
                _risk_fact(
                    "deepseek-risk",
                    "regulatory_risk",
                    penalty=0.0,
                    evidence_ids=("official-risk",),
                ),
            ),
        ),
        outcome=ReviewOutcome.ABSTAIN,
    )

    epoch = build_scored_decision_epoch(
        _request(
            _selection((evaluation,)),
            reviews={"600001": abstain},
            projection_stage="hybrid",
            review_candidate_codes=("600001",),
            parent_decision_version="decision:local",
        )
    )

    decision = epoch.entries[0]
    assert decision.score.final_score == decision.score.local_score == 80.0
    assert decision.score.deepseek_risk_penalty == 0.0
    assert decision.deepseek_risk_facts == ()
    assert decision.veto is False


def test_hybrid_requires_a_current_applied_or_abstain_review() -> None:
    evaluation = _evaluation(1, local_score=80.0)
    rejected = replace(_review("600001", 100.0), outcome=ReviewOutcome.REJECTED)

    with pytest.raises(ValueError, match="usable DeepSeek review"):
        build_scored_decision_epoch(
            _request(
                _selection((evaluation,)),
                reviews={"600001": rejected},
                projection_stage="hybrid",
                review_candidate_codes=("600001",),
                parent_decision_version="decision:local",
            )
        )


def test_decision_rejects_risk_fact_from_after_its_observation_time() -> None:
    future_fact = replace(
        _risk_fact("future-risk", "deepseek_rule", penalty=1.0),
        observed_at=NOW + timedelta(seconds=1),
    )
    evaluation = _evaluation(
        1,
        local_score=80.0,
        local_base_score=81.0,
        local_risk_penalty=1.0,
        local_risk_facts=(future_fact,),
    )

    with pytest.raises(ValueError, match="future risk"):
        build_scored_decision_epoch(_request(_selection((evaluation,))))


def test_final_action_pools_apply_stable_board_and_industry_limits() -> None:
    evaluations = tuple(
        _evaluation(
            index,
            local_score=95.0 - index,
            board=Board.MAIN if index < 10 else Board.CHINEXT,
            industry="concentrated" if index < 4 else f"industry-{index}",
        )
        for index in range(16)
    )

    epoch = build_scored_decision_epoch(_request(_selection(tuple(reversed(evaluations)))))
    selected = tuple(item for item in epoch.entries if item.selected)
    executable = tuple(item for item in selected if item.action is RecommendationAction.EXECUTABLE)

    assert len(executable) == 6
    assert sum(item.features.quote.board is Board.MAIN for item in executable) <= 4
    assert sum(item.features.quote.industry == "concentrated" for item in executable) <= 2
    assert tuple(item.rank for item in selected) == tuple(range(1, len(selected) + 1))
    assert tuple(item.code for item in executable) == tuple(
        item.code
        for item in sorted(
            executable,
            key=lambda item: (-item.score.final_score, -item.score.local_score, item.code),
        )
    )


def test_observe_only_candidate_still_requires_the_observation_score_floor() -> None:
    below = _evaluation(
        1,
        local_score=72.99,
        disposition=ScoredDisposition.OBSERVE_ONLY,
    )
    eligible = _evaluation(
        2,
        local_score=73.0,
        disposition=ScoredDisposition.OBSERVE_ONLY,
    )

    epoch = build_scored_decision_epoch(_request(_selection((below, eligible))))
    by_code = {item.code: item for item in epoch.entries}

    assert by_code["600001"].action is RecommendationAction.UNAVAILABLE
    assert by_code["600001"].action_reason == "below_score_threshold"
    assert by_code["600001"].selected is False
    assert by_code["600002"].action is RecommendationAction.OBSERVE
    assert by_code["600002"].selected is True


def test_decision_epoch_hash_is_stable_and_rejects_review_outside_protection_set() -> None:
    evaluations = tuple(_evaluation(index, local_score=90.0 - index) for index in range(3))
    selection = _selection(evaluations)
    first = build_scored_decision_epoch(_request(selection))
    second = build_scored_decision_epoch(
        _request(replace(selection, evaluations=tuple(reversed(selection.evaluations))))
    )

    assert first.content_hash == second.content_hash
    assert first.version == second.version

    changed = replace(
        evaluations[0],
        features=replace(evaluations[0].features, merge_epoch="market:2"),
    )
    changed_epoch = build_scored_decision_epoch(_request(_selection((changed, *evaluations[1:]))))
    assert changed_epoch.content_hash != first.content_hash

    with pytest.raises(ValueError, match="outside the review candidate set"):
        build_scored_decision_epoch(
            _request(
                selection,
                reviews={"600001": _review("600001", 80.0)},
                projection_stage="hybrid",
                review_candidate_codes=("600000",),
                parent_decision_version="decision:local",
            )
        )


def test_decision_epoch_carries_and_enforces_its_selection_limits() -> None:
    evaluations = tuple(_evaluation(index, local_score=90.0 - index) for index in range(4))
    epoch = build_scored_decision_epoch(_request(_selection(evaluations)))

    assert epoch.selection_limits == DecisionSelectionLimits(
        top_k=6,
        observation_limit=6,
        maximum_per_industry=2,
        maximum_board_fraction=0.60,
    )
    with pytest.raises(ValueError, match="exceed their decision limits"):
        replace(epoch, selection_limits=replace(epoch.selection_limits, top_k=0))
    with pytest.raises(ValueError, match="board limit"):
        replace(epoch, selection_limits=replace(epoch.selection_limits, maximum_board_fraction=0.30))


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        ({"maximum_per_industry": 3}, "industry limit"),
        ({"maximum_board_fraction": 0.61}, "board fraction"),
    ),
)
def test_decision_policy_cannot_relax_fixed_concentration_limits(
    changes: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        replace(_policy(), **changes)


def _policy() -> ScoredDecisionPolicy:
    return ScoredDecisionPolicy(
        dimension_weights={
            "value_quality": 0.1875,
            "financial_health": 0.25,
            "market_flow": 0.3125,
            "industry_policy": 0.0,
            "risk_quality": 0.25,
        },
        risk_rules={
            "regulatory_risk": RiskRule(
                risk_code="regulatory_risk",
                severity="medium",
                penalty=3.0,
                minimum_confidence=0.7,
                group="deepseek",
                evidence_ttl_hours=24,
                allowed_evidence_types=("regulatory_filing",),
            )
        },
        executable_threshold=78.0,
        observation_margin=5.0,
        review_candidate_limit=28,
        top_k=6,
        observation_limit=6,
        maximum_per_industry=2,
        maximum_board_fraction=0.60,
    )


def _request(
    selection: ScoredSelectionResult,
    *,
    reviews: dict[str, DeepSeekReview] | None = None,
    projection_stage: str = "local",
    review_candidate_codes: tuple[str, ...] = (),
    parent_decision_version: str | None = None,
) -> ScoredDecisionRequest:
    return ScoredDecisionRequest(
        selection=selection,
        reviews=reviews or {},
        observed_at=NOW,
        trade_date=NOW.date(),
        sequence=10 if projection_stage == "local" else 11,
        config_version="runtime-v2",
        strategy_version="tomorrow-v2",
        fusion_version="fusion_local68_deepseek32",
        market_epoch_version="market:1",
        candidate_epoch_version=None,
        research_epoch_version="research:1" if projection_stage == "hybrid" else None,
        projection_stage=projection_stage,
        parent_decision_version=parent_decision_version,
        review_candidate_codes=review_candidate_codes,
        degraded_reasons=(),
        policy=_policy(),
    )


def _selection(evaluations: tuple[ScoredStockEvaluation, ...]) -> ScoredSelectionResult:
    return ScoredSelectionResult(
        evaluations=evaluations,
        scored_candidates=evaluations,
        observations=tuple(item for item in evaluations if item.disposition is ScoredDisposition.OBSERVE_ONLY),
        selected=(),
        population_versions={},
    )


def _evaluation(
    index: int,
    *,
    local_score: float,
    local_base_score: float | None = None,
    local_risk_penalty: float = 0.0,
    local_risk_facts: tuple[RiskFact, ...] = (),
    disposition: ScoredDisposition = ScoredDisposition.PASS,
    board: Board = Board.MAIN,
    industry: str | None = None,
    evidence: tuple[Evidence, ...] = (),
    values: dict[str, float | None] | None = None,
) -> ScoredStockEvaluation:
    code = f"600{index:03d}"
    feature = FeatureSnapshot(
        quote=MarketQuote(
            code=code,
            name=code,
            price=10.0,
            previous_close=9.8,
            open_price=9.9,
            high=10.1,
            low=9.7,
            pct_change=2.0,
            change_5m=0.1,
            speed=0.2,
            volume_ratio=1.2,
            turnover_rate=2.5,
            amount=100_000_000.0,
            amplitude=4.0,
            market_cap=10_000_000_000.0,
            industry=industry or f"industry-{index}",
            source="fixture",
            source_time=NOW,
            received_time=NOW,
            data_version="market-1",
            board=board,
            board_source="security_master",
            board_reliability="verified",
            listing_age_sessions=100,
        ),
        values={
            "atr20_pct": 2.0,
            "volatility_20d": 2.0,
            "max_drawdown_20d": -8.0,
            "low_volatility_score": 70.0,
            "low_drawdown_score": 70.0,
            "close_location": 70.0,
            "market_breadth": 60.0,
            "tail_return_30m_pct": 0.5,
            "trend_breakdown": 0.0,
            **(values or {}),
        },
        observed_at=NOW,
        evidence=evidence,
        board_data_reliability=0.9,
        merge_epoch="market:1",
    )
    return ScoredStockEvaluation(
        features=feature,
        disposition=disposition,
        candidate_score=local_score,
        candidate_rank=index + 1,
        local_components={"test": local_base_score if local_base_score is not None else local_score},
        local_base_score=local_base_score if local_base_score is not None else local_score,
        local_risk_penalty=local_risk_penalty,
        local_score=local_score,
        local_risk_facts=local_risk_facts,
        board_rank=index + 1,
    )


def _review(
    code: str,
    score: float,
    *,
    risk_facts: tuple[RiskFact, ...] = (),
) -> DeepSeekReview:
    dimensions = {
        name: DimensionAssessment(
            name=name,
            score=score,
            confidence=1.0 if name != "industry_policy" else 0.0,
            assessment="fixture",
            evidence_ids=("official-risk",) if name != "industry_policy" else (),
            is_unknown=name == "industry_policy",
        )
        for name in DIMENSIONS
    }
    return DeepSeekReview(
        code=code,
        outcome=ReviewOutcome.APPLIED,
        dimensions=dimensions,
        risk_facts=risk_facts,
        completed_at=NOW,
        evidence_manifest_hash="manifest",
    )


def _risk_fact(
    fact_id: str,
    risk_code: str,
    *,
    penalty: float,
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


def _evidence() -> Evidence:
    return Evidence(
        evidence_id="official-risk",
        evidence_type="regulatory_filing",
        title="官方风险公告",
        source="exchange",
        published_at=NOW - timedelta(hours=1),
        received_at=NOW - timedelta(minutes=30),
        data_version="research-1",
    )
