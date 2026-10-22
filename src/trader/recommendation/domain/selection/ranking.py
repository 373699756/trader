"""Recommendation preselection, action policy, and constrained TopK."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from trader.recommendation.domain.publication.models import (
    Recommendation,
    RecommendationAction,
    Strategy,
)

CORE_FIELDS = (
    "amount_percentile_20d",
    "relative_strength_5d",
    "relative_strength_20d",
    "ma20_60_position",
    "volatility_20d",
    "max_drawdown_20d",
)

_EXECUTION_PHASES = {
    Strategy.TOMORROW: frozenset(
        {
            "before_close",
            "morning_observe",
            "morning_main",
            "morning_late",
            "midday",
            "afternoon",
            "final_review",
            "final_quote",
            "close_fallback",
        }
    ),
    Strategy.D25: frozenset(
        {
            "before_close",
            "morning_observe",
            "morning_main",
            "morning_late",
            "midday",
            "afternoon",
            "final_review",
            "final_quote",
            "close_fallback",
        }
    ),
}


@dataclass(frozen=True)
class ActionPolicy:
    thresholds: Mapping[str, float]
    phase: str
    is_stale: bool
    observation_margin: float
    minimum_board_reliability: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "thresholds", MappingProxyType(dict(self.thresholds)))


def ActionFor(
    recommendation: Recommendation,
    policy: ActionPolicy,
) -> tuple[RecommendationAction, str]:
    blocker = _action_blocker(recommendation, policy)
    if blocker is not None:
        return blocker
    threshold_key = _threshold_key(recommendation.strategy, policy.phase)
    threshold = policy.thresholds.get(threshold_key) if threshold_key is not None else None
    if threshold is None:
        return RecommendationAction.UNAVAILABLE, "outside_execution_window"
    if recommendation.score.final_score >= threshold:
        if recommendation.downside is not None and recommendation.downside.status == "observe":
            reasons = ",".join(recommendation.downside.reasons) or "unspecified"
            return RecommendationAction.OBSERVE, f"downside_guard:{reasons}"
        return RecommendationAction.EXECUTABLE, "score_threshold_met"
    if recommendation.score.final_score >= threshold - policy.observation_margin:
        return RecommendationAction.OBSERVE, "near_score_threshold"
    return RecommendationAction.UNAVAILABLE, "below_score_threshold"


def _action_blocker(
    recommendation: Recommendation,
    policy: ActionPolicy,
) -> tuple[RecommendationAction, str] | None:
    checks = (
        (recommendation.strategy is Strategy.LONG, RecommendationAction.OBSERVE, "long_watch_only"),
        (recommendation.veto, RecommendationAction.UNAVAILABLE, "risk_veto"),
        (policy.is_stale, RecommendationAction.OBSERVE, "stale_quote"),
        (
            recommendation.features.value("corporate_risk_window_unavailable", 0.0) > 0.0,
            RecommendationAction.OBSERVE,
            "corporate_risk_window_unavailable",
        ),
        (
            not recommendation.features.board_policy_id and recommendation.features.missing_ratio(CORE_FIELDS) > 0.30,
            RecommendationAction.OBSERVE,
            "insufficient_core_features",
        ),
        (
            recommendation.features.board_data_reliability < policy.minimum_board_reliability,
            RecommendationAction.OBSERVE,
            "board_data_reliability_below_threshold",
        ),
    )
    return next(((action, reason) for blocked, action, reason in checks if blocked), None)


def MinimumSelectionScore(
    strategy: Strategy,
    thresholds: Mapping[str, float],
    *,
    phase: str,
    observation_margin: float,
) -> float | None:
    if observation_margin < 0.0:
        raise ValueError("observation_margin cannot be negative")
    if strategy is Strategy.LONG:
        return 0.0
    threshold_key = _threshold_key(strategy, phase)
    if threshold_key is None:
        return None
    threshold = thresholds.get(threshold_key)
    if threshold is None:
        return None
    return max(0.0, threshold - observation_margin)


def _threshold_key(strategy: Strategy, phase: str) -> str | None:
    if phase not in _EXECUTION_PHASES.get(strategy, frozenset()):
        return None
    if strategy is Strategy.TOMORROW:
        return "tomorrow"
    if strategy is Strategy.D25:
        return "d25"
    return None


__all__ = [
    "ActionPolicy",
    "CORE_FIELDS",
    "ActionFor",
    "MinimumSelectionScore",
]
