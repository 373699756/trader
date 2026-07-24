"""Recommendation preselection, action policy, and constrained TopK."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType

from trader.domain.market.factors import band_score, clamp, weighted_score
from trader.domain.market.models import (
    Board,
    FeatureSnapshot,
)
from trader.domain.recommendation.models import (
    Recommendation,
    RecommendationAction,
    SelectionSkip,
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
    Strategy.TODAY: frozenset({"today_main", "today_late", "close_fallback"}),
    Strategy.TOMORROW: frozenset({"afternoon", "final_review", "final_quote", "close_fallback"}),
    Strategy.D25: frozenset({"afternoon", "final_review", "final_quote", "close_fallback"}),
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


@dataclass(frozen=True)
class SelectionPolicy:
    top_k: int
    maximum_per_industry: int
    minimum_final_score: float = 0.0
    maximum_board_fraction: float = 1.0
    competition_group_limits: Mapping[Board, int] = field(default_factory=dict)
    enforce_competition_group_limits: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "competition_group_limits", MappingProxyType(dict(self.competition_group_limits)))
        _validate_selection_policy(self)


@dataclass
class _SelectionState:
    selected: list[Recommendation] = field(default_factory=list)
    skips: list[SelectionSkip] = field(default_factory=list)
    industry_counts: Counter[str] = field(default_factory=Counter)
    board_counts: Counter[Board] = field(default_factory=Counter)
    competition_counts: Counter[tuple[Board, str]] = field(default_factory=Counter)


def candidate_score(snapshot: FeatureSnapshot, weights: Mapping[str, float]) -> float:
    liquidity = 0.65 * snapshot.value("amount_percentile_20d") + 0.35 * band_score(
        snapshot.quote.turnover_rate,
        0.5,
        1.5,
        8.0,
        15.0,
    )
    short_momentum = (
        0.40 * band_score(snapshot.quote.change_5m, 0.0, 0.2, 1.8, 3.5)
        + 0.35 * snapshot.value("relative_strength_5d")
        + 0.25 * band_score(snapshot.quote.volume_ratio, 0.8, 1.2, 3.5, 6.0)
    )
    values = {
        "liquidity": clamp(liquidity),
        "short_momentum": clamp(short_momentum),
        "trend": clamp(snapshot.value("trend_score")),
        "data_completeness": 100.0 * (1.0 - snapshot.missing_ratio(CORE_FIELDS)),
    }
    if "industry_strength" in weights:
        values["industry_strength"] = clamp(snapshot.value("industry_strength"))
    return weighted_score(values, weights)


def action_for(
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
            recommendation.features.value("corporate_risk_history_unavailable", 0.0) > 0.0,
            RecommendationAction.OBSERVE,
            "corporate_risk_history_unavailable",
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
        (
            recommendation.strategy is Strategy.TODAY and policy.phase == "today_observe",
            RecommendationAction.OBSERVE,
            "observation_window",
        ),
    )
    return next(((action, reason) for blocked, action, reason in checks if blocked), None)


def select_top_k(
    recommendations: Iterable[Recommendation],
    policy: SelectionPolicy,
) -> tuple[Recommendation, ...]:
    selected, _skips = select_top_k_with_audit(recommendations, policy)
    return selected


def select_top_k_with_audit(
    recommendations: Iterable[Recommendation],
    policy: SelectionPolicy,
) -> tuple[tuple[Recommendation, ...], tuple[SelectionSkip, ...]]:
    ordered = sorted(
        (item for item in recommendations if item.score.final_score >= policy.minimum_final_score),
        key=lambda item: (
            -item.score.final_score,
            -item.score.local_score,
            item.features.quote.code,
        ),
    )
    state = _SelectionState()
    maximum_per_board = math.ceil(policy.top_k * policy.maximum_board_fraction)
    for global_index, item in enumerate(ordered, start=1):
        limit = _selection_limit(
            item,
            state,
            policy=policy,
            maximum_per_board=maximum_per_board,
        )
        if limit is not None:
            reason, maximum = limit
            state.skips.append(_selection_skip(item, global_index, reason, maximum))
            continue
        board = item.features.quote.board
        group = item.features.competition_group_id or item.features.quote.industry or "unknown"
        group_limit = policy.competition_group_limits.get(board) if policy.enforce_competition_group_limits else None
        if group_limit is not None:
            state.competition_counts[(board, group)] += 1
        else:
            industry = item.features.quote.industry.strip() or "unknown"
            state.industry_counts[industry] += 1
        state.board_counts[board] += 1
        state.selected.append(
            replace(
                item,
                rank=len(state.selected) + 1,
                competition_group_limit=group_limit,
            )
        )
    return tuple(state.selected), tuple(state.skips)


def _validate_selection_policy(policy: SelectionPolicy) -> None:
    if not 0 <= policy.top_k <= 18:
        raise ValueError("top_k must be between 0 and 18")
    if policy.maximum_per_industry < 1:
        raise ValueError("maximum_per_industry must be positive")
    if not 0.0 <= policy.minimum_final_score <= 100.0:
        raise ValueError("minimum_final_score must be between 0 and 100")
    if not 0.0 < policy.maximum_board_fraction <= 1.0:
        raise ValueError("maximum_board_fraction must be in (0, 1]")


def _selection_limit(
    item: Recommendation,
    state: _SelectionState,
    *,
    policy: SelectionPolicy,
    maximum_per_board: int,
) -> tuple[str, int] | None:
    board = item.features.quote.board
    industry = item.features.quote.industry.strip() or "unknown"
    group = item.features.competition_group_id or item.features.quote.industry or "unknown"
    group_limit = policy.competition_group_limits.get(board) if policy.enforce_competition_group_limits else None
    checks = (
        (len(state.selected) >= policy.top_k, "top_k_limit", policy.top_k),
        (state.board_counts[board] >= maximum_per_board, "board_fraction_limit", maximum_per_board),
        (
            group_limit is not None and state.competition_counts[(board, group)] >= group_limit,
            "competition_group_limit",
            group_limit or 0,
        ),
        (
            group_limit is None and state.industry_counts[industry] >= policy.maximum_per_industry,
            "industry_limit",
            policy.maximum_per_industry,
        ),
    )
    return next(((reason, maximum) for reached, reason, maximum in checks if reached), None)


def _selection_skip(item: Recommendation, global_rank: int, reason: str, limit: int) -> SelectionSkip:
    return SelectionSkip(
        stock_code=item.features.quote.code,
        board=item.features.quote.board,
        competition_group_id=item.features.competition_group_id or item.features.quote.industry or "unknown",
        board_rank=item.board_rank,
        global_rank=global_rank,
        reason=reason,
        limit=limit,
        policy_version=item.features.board_policy_version,
        observed_at=item.features.observed_at,
    )


def minimum_selection_score(
    strategy: Strategy,
    thresholds: Mapping[str, float],
    *,
    phase: str,
    observation_margin: float,
) -> float | None:
    if observation_margin < 0.0:
        raise ValueError("observation_margin cannot be negative")
    if strategy is Strategy.LONG or (strategy is Strategy.TODAY and phase == "today_observe"):
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
    if strategy is Strategy.TODAY:
        return "today_late" if phase in {"today_late", "close_fallback"} else "today_main"
    if strategy is Strategy.TOMORROW:
        return "tomorrow"
    if strategy is Strategy.D25:
        return "d25"
    return None


__all__ = [
    "ActionPolicy",
    "CORE_FIELDS",
    "SelectionPolicy",
    "action_for",
    "candidate_score",
    "minimum_selection_score",
    "select_top_k",
    "select_top_k_with_audit",
]
