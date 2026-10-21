from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from trader.recommendation.domain.evidence.review import (
    DeepSeekReview,
    ReviewOutcome,
)
from trader.recommendation.domain.publication.models import (
    FusionMode,
    Recommendation,
    RecommendationAction,
    ScoreBreakdown,
    Strategy,
)
from trader.recommendation.domain.risk.downside import DownsideAssessment
from trader.recommendation.domain.risk.rules import Rating
from trader.recommendation.domain.selection.ranking import (
    ActionFor,
    ActionPolicy,
    MinimumSelectionScore,
)


def test_tomorrow_and_d25_have_morning_draft_selection_floors() -> None:
    thresholds = {"tomorrow": 78.0, "d25": 76.0}

    assert (
        MinimumSelectionScore(
            Strategy.TOMORROW,
            thresholds,
            phase="morning_main",
            observation_margin=5.0,
        )
        == 73.0
    )
    assert (
        MinimumSelectionScore(
            Strategy.D25,
            thresholds,
            phase="midday",
            observation_margin=5.0,
        )
        == 71.0
    )


@pytest.mark.parametrize(
    ("strategy", "phase", "score", "expected", "reason"),
    (
        (Strategy.TOMORROW, "afternoon", 72.0, RecommendationAction.EXECUTABLE, "score_threshold_met"),
        (Strategy.D25, "final_quote", 70.0, RecommendationAction.EXECUTABLE, "score_threshold_met"),
        (Strategy.TOMORROW, "morning_main", 100.0, RecommendationAction.EXECUTABLE, "score_threshold_met"),
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

    action, actual_reason = _action_for(
        recommendation,
        {"morning_main": 70.0, "morning_late": 76.0, "tomorrow": 72.0, "d25": 70.0},
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

    action, reason = _action_for(
        recommendation,
        {"tomorrow": 72.0},
        phase="afternoon",
        is_stale=False,
        observation_margin=5.0,
    )

    assert action is RecommendationAction.OBSERVE
    assert reason == "insufficient_core_features"


def test_action_policy_observes_incomplete_corporate_risk_history(feature_factory) -> None:
    recommendation = _recommendation(
        feature_factory(values={"corporate_risk_window_unavailable": 1.0}),
        90.0,
    )

    action, reason = _action_for(
        recommendation,
        {"tomorrow": 72.0},
        phase="afternoon",
        is_stale=False,
        observation_margin=5.0,
    )

    assert action is RecommendationAction.OBSERVE
    assert reason == "corporate_risk_window_unavailable"


def test_action_policy_does_not_apply_bearish_audit_rating(feature_factory) -> None:
    recommendation = replace(_recommendation(feature_factory(), 90.0), strategy=Strategy.TOMORROW)
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

    action, reason = _action_for(
        recommendation,
        {"tomorrow": 70.0},
        phase="morning_main",
        is_stale=False,
        observation_margin=5.0,
    )

    assert action is RecommendationAction.EXECUTABLE
    assert reason == "score_threshold_met"


def test_action_policy_does_not_apply_neutral_audit_rating(feature_factory) -> None:
    recommendation = replace(_recommendation(feature_factory(), 90.0), strategy=Strategy.TOMORROW)
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

    action, reason = _action_for(
        recommendation,
        {"tomorrow": 70.0},
        phase="morning_main",
        is_stale=False,
        observation_margin=5.0,
    )

    assert action is RecommendationAction.EXECUTABLE
    assert reason == "score_threshold_met"


def test_action_policy_downgrades_only_executable_downside_failure(feature_factory) -> None:
    recommendation = replace(
        _recommendation(feature_factory(), 90.0),
        downside=DownsideAssessment(
            status="observe",
            reasons=("trend_breakdown",),
            atr20_pct=2.0,
            intraday_reversal_atr=0.0,
            historical_drawdown_pct=-8.0,
            setup_type="shrink_pullback",
        ),
    )

    action, reason = _action_for(
        recommendation,
        {"tomorrow": 72.0},
        phase="afternoon",
        is_stale=False,
        observation_margin=5.0,
    )

    assert action is RecommendationAction.OBSERVE
    assert reason == "downside_guard:trend_breakdown"


def _action_for(
    recommendation: Recommendation,
    thresholds: dict[str, float],
    *,
    phase: str,
    is_stale: bool,
    observation_margin: float,
    minimum_board_reliability: float = 0.0,
) -> tuple[RecommendationAction, str]:
    return ActionFor(
        recommendation,
        ActionPolicy(
            thresholds=thresholds,
            phase=phase,
            is_stale=is_stale,
            observation_margin=observation_margin,
            minimum_board_reliability=minimum_board_reliability,
        ),
    )


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
