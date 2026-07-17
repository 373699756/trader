from __future__ import annotations

from dataclasses import replace

import pytest

from trader.domain.models import (
    FusionMode,
    Recommendation,
    RecommendationAction,
    ScoreBreakdown,
    Strategy,
)
from trader.domain.ranking import action_for, candidate_score, select_top_k

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


def _recommendation(features, final_score: float) -> Recommendation:
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
        review=None,
        action=RecommendationAction.OBSERVE,
        action_reason="fixture",
        veto=False,
    )
