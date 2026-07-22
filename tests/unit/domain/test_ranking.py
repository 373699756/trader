from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from trader.domain.models import (
    Board,
    DeepSeekReview,
    FusionMode,
    Recommendation,
    RecommendationAction,
    ReviewOutcome,
    ScoreBreakdown,
    Strategy,
)
from trader.domain.ranking import action_for, candidate_score, select_top_k, select_top_k_with_audit
from trader.domain.risk import Rating

CANDIDATE_WEIGHTS = {
    "liquidity": 0.35,
    "short_momentum": 0.25,
    "trend": 0.20,
    "industry_strength": 0.10,
    "data_completeness": 0.10,
}


def test_candidate_score_is_bounded(feature_factory) -> None:
    assert 0.0 <= candidate_score(feature_factory(), CANDIDATE_WEIGHTS) <= 100.0


def test_top_k_enforces_industry_cap_and_stable_tie_break(feature_factory) -> None:
    rows = [
        _recommendation(feature_factory(code="600001", industry="A"), 90.0),
        _recommendation(feature_factory(code="600002", industry="A"), 89.0),
        _recommendation(feature_factory(code="600003", industry="A"), 88.0),
        _recommendation(feature_factory(code="600004", industry="B"), 87.0),
    ]

    selected = select_top_k(rows, top_k=3, maximum_per_industry=2)

    assert [row.features.quote.code for row in selected] == ["600001", "600002", "600004"]
    assert [row.rank for row in selected] == [1, 2, 3]
    assert select_top_k(rows, top_k=0, maximum_per_industry=2) == ()
    with pytest.raises(ValueError, match="between 0 and 18"):
        select_top_k(rows, top_k=19, maximum_per_industry=2)


def test_top_k_ignores_review_audit_fields_when_scores_tie(feature_factory) -> None:
    rows = [
        _recommendation(
            feature_factory(code="600001", industry="A"),
            final_score=90.0,
            review=_review(
                code="600001",
                challenger_status="failed",
                review_stage="secondary",
                rating=Rating.BULLISH.value,
                confidence=0.30,
            ),
        ),
        _recommendation(
            feature_factory(code="600002", industry="A"),
            final_score=90.0,
            review=_review(
                code="600002",
                challenger_status="passed",
                review_stage="secondary",
                rating=Rating.BULLISH.value,
                confidence=0.10,
            ),
        ),
        _recommendation(
            feature_factory(code="600003", industry="A"),
            final_score=90.0,
            review=_review(
                code="600003",
                challenger_status="not_run",
                review_stage="primary",
                rating=Rating.BULLISH.value,
                confidence=0.50,
            ),
        ),
    ]

    selected = select_top_k(rows, top_k=2, maximum_per_industry=3)

    assert [row.features.quote.code for row in selected] == ["600001", "600002"]


def test_top_k_does_not_lower_minimum_score_to_fill(feature_factory) -> None:
    rows = [
        _recommendation(feature_factory(code="600001", industry="A"), 66.0),
        _recommendation(feature_factory(code="600002", industry="B"), 64.99),
        _recommendation(feature_factory(code="600003", industry="C"), 40.0),
    ]

    selected = select_top_k(rows, top_k=10, maximum_per_industry=3, minimum_final_score=65.0)

    assert [row.features.quote.code for row in selected] == ["600001"]
    assert select_top_k(rows, top_k=10, maximum_per_industry=3, minimum_final_score=90.0) == ()


@pytest.mark.parametrize(
    ("strategy", "phase", "score", "expected", "reason"),
    (
        (Strategy.TODAY, "today_observe", 100.0, RecommendationAction.OBSERVE, "observation_window"),
        (Strategy.TODAY, "today_main", 70.0, RecommendationAction.EXECUTABLE, "score_threshold_met"),
        (Strategy.TODAY, "today_main", 69.99, RecommendationAction.OBSERVE, "near_score_threshold"),
        (Strategy.TODAY, "today_late", 76.0, RecommendationAction.EXECUTABLE, "score_threshold_met"),
        (Strategy.TOMORROW, "afternoon", 72.0, RecommendationAction.EXECUTABLE, "score_threshold_met"),
        (Strategy.D25, "final_quote", 70.0, RecommendationAction.EXECUTABLE, "score_threshold_met"),
        (Strategy.TOMORROW, "today_main", 100.0, RecommendationAction.UNAVAILABLE, "outside_execution_window"),
        (Strategy.TOMORROW, "afternoon", 66.99, RecommendationAction.UNAVAILABLE, "below_score_threshold"),
    ),
)
def test_action_policy_enforces_phase_and_threshold_boundaries(
    feature_factory,
    strategy,
    phase,
    score,
    expected,
    reason,
) -> None:
    recommendation = replace(_recommendation(feature_factory(), score), strategy=strategy)

    action, actual_reason = action_for(
        recommendation,
        {"today_main": 70.0, "today_late": 76.0, "tomorrow": 72.0, "d25": 70.0},
        phase=phase,
        is_stale=False,
        observation_margin=5.0,
    )

    assert action is expected
    assert actual_reason == reason


def test_action_policy_observes_missing_core_features(feature_factory) -> None:
    missing = feature_factory(
        values={
            name: None
            for name in (
                "amount_percentile_20d",
                "relative_strength_5d",
                "relative_strength_20d",
                "ma20_60_position",
            )
        }
    )
    recommendation = _recommendation(missing, 90.0)

    action, reason = action_for(
        recommendation,
        {"tomorrow": 72.0},
        phase="afternoon",
        is_stale=False,
        observation_margin=5.0,
    )

    assert action is RecommendationAction.OBSERVE
    assert reason == "insufficient_core_features"


def test_action_policy_does_not_apply_bearish_audit_rating(feature_factory) -> None:
    recommendation = replace(_recommendation(feature_factory(), 90.0), strategy=Strategy.TODAY)
    recommendation = replace(
        recommendation,
        review=DeepSeekReview(
            code="600001",
            outcome=ReviewOutcome.APPLIED,
            dimensions={},
            risk_facts=(),
            completed_at=datetime(2026, 7, 16, 10, tzinfo=timezone.utc),
            rating=Rating.BEARISH.value,
        ),
    )

    action, reason = action_for(
        recommendation,
        {"today_main": 70.0},
        phase="today_main",
        is_stale=False,
        observation_margin=5.0,
    )

    assert action is RecommendationAction.EXECUTABLE
    assert reason == "score_threshold_met"


def test_action_policy_does_not_apply_neutral_audit_rating(feature_factory) -> None:
    recommendation = replace(_recommendation(feature_factory(), 90.0), strategy=Strategy.TODAY)
    recommendation = replace(
        recommendation,
        review=DeepSeekReview(
            code="600001",
            outcome=ReviewOutcome.APPLIED,
            dimensions={},
            risk_facts=(),
            completed_at=datetime(2026, 7, 16, 10, tzinfo=timezone.utc),
            rating=Rating.NEUTRAL.value,
        ),
    )

    action, reason = action_for(
        recommendation,
        {"today_main": 70.0},
        phase="today_main",
        is_stale=False,
        observation_margin=5.0,
    )

    assert action is RecommendationAction.EXECUTABLE
    assert reason == "score_threshold_met"


@pytest.mark.parametrize(("top_k", "expected_main"), [(0, 0), (1, 1), (10, 6), (18, 11)])
def test_board_fraction_uses_ceil_at_all_topk_boundaries(feature_factory, top_k: int, expected_main: int) -> None:
    rows = []
    for index in range(18):
        board = Board.MAIN if index < 12 else Board.CHINEXT
        code = f"600{index:03d}" if board is Board.MAIN else f"300{index:03d}"
        feature = feature_factory(code=code, board=board, industry=f"I{index}")
        feature = replace(feature, competition_group_id=f"G{index}")
        rows.append(_recommendation(feature, 100.0 - index / 100.0))

    selected, _ = select_top_k_with_audit(
        rows,
        top_k=top_k,
        maximum_per_industry=18,
        maximum_board_fraction=0.6,
        competition_group_limits={Board.MAIN: 3, Board.CHINEXT: 2, Board.STAR: 2},
    )

    assert sum(item.features.quote.board is Board.MAIN for item in selected) == expected_main


@pytest.mark.parametrize(("board", "limit"), [(Board.MAIN, 3), (Board.CHINEXT, 2), (Board.STAR, 2)])
def test_competition_group_limit_records_first_skipped_boundary(feature_factory, board: Board, limit: int) -> None:
    prefix = {Board.MAIN: "600", Board.CHINEXT: "300", Board.STAR: "688"}[board]
    rows = []
    for index in range(limit + 1):
        feature = feature_factory(code=f"{prefix}{index:03d}", board=board, industry="同业")
        feature = replace(feature, competition_group_id="same", board_policy_version="v16")
        rows.append(replace(_recommendation(feature, 90.0 - index), board_rank=index + 1))

    selected, skips = select_top_k_with_audit(
        rows,
        top_k=10,
        maximum_per_industry=10,
        maximum_board_fraction=1.0,
        competition_group_limits={board: limit},
    )

    assert len(selected) == limit
    assert skips[0].global_rank == limit + 1
    assert skips[0].reason == "competition_group_limit"


def _recommendation(features, final_score: float, review: DeepSeekReview | None = None) -> Recommendation:
    score = ScoreBreakdown(
        components={"test": final_score},
        base_score=final_score,
        local_risk_penalty=0.0,
        local_score=final_score,
        deepseek_score=None,
        confidence_coverage=0.0,
        deepseek_risk_penalty=0.0,
        final_score=final_score,
        fusion_mode=FusionMode.LOCAL_DEGRADED,
        fusion_applied=False,
    )
    return Recommendation(
        strategy=Strategy.TOMORROW,
        features=features,
        score=score,
        local_risk_facts=(),
        deepseek_risk_facts=(),
        review=review,
        action=RecommendationAction.OBSERVE,
        action_reason="fixture",
        veto=False,
    )


def _review(
    code: str,
    *,
    challenger_status: str = "not_run",
    review_stage: str = "primary",
    rating: str = Rating.NEUTRAL.value,
    confidence: float | None = None,
) -> DeepSeekReview:
    return DeepSeekReview(
        code=code,
        outcome=ReviewOutcome.APPLIED,
        dimensions={},
        risk_facts=(),
        completed_at=datetime(2026, 7, 16, 10, tzinfo=timezone.utc),
        challenger_status=challenger_status,
        review_stage=review_stage,
        rating=rating,
        raw_confidence=confidence,
        calibrated_confidence=confidence,
    )
