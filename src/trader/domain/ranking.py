"""Candidate preselection, action policy and industry-constrained TopK."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import replace

from trader.domain.factors import band_score, clamp, weighted_score
from trader.domain.models import FeatureSnapshot, Recommendation, RecommendationAction, Strategy

CORE_FIELDS = (
    "amount_percentile_20d",
    "relative_strength_5d",
    "relative_strength_20d",
    "ma20_60_position",
    "volatility_20d",
    "max_drawdown_20d",
    "industry_strength",
)

_EXECUTION_PHASES = {
    Strategy.TODAY: frozenset({"today_main", "today_late"}),
    Strategy.TOMORROW: frozenset({"afternoon", "final_review", "final_quote"}),
    Strategy.D25: frozenset({"afternoon", "final_review", "final_quote"}),
}


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
        "industry_strength": clamp(snapshot.value("industry_strength")),
        "data_completeness": 100.0 * (1.0 - snapshot.missing_ratio(CORE_FIELDS)),
    }
    return weighted_score(values, weights)


def action_for(
    recommendation: Recommendation,
    thresholds: Mapping[str, float],
    *,
    phase: str,
    is_stale: bool,
    observation_margin: float,
) -> tuple[RecommendationAction, str]:
    if recommendation.strategy is Strategy.LONG:
        return RecommendationAction.OBSERVE, "long_watch_only"
    if recommendation.veto:
        return RecommendationAction.UNAVAILABLE, "risk_veto"
    if is_stale:
        return RecommendationAction.OBSERVE, "stale_quote"
    if recommendation.features.missing_ratio(CORE_FIELDS) > 0.30:
        return RecommendationAction.OBSERVE, "insufficient_core_features"
    if recommendation.strategy is Strategy.TODAY and phase == "today_observe":
        return RecommendationAction.OBSERVE, "observation_window"
    threshold_key = _threshold_key(recommendation.strategy, phase)
    if threshold_key is None:
        return RecommendationAction.UNAVAILABLE, "outside_execution_window"
    threshold = thresholds.get(threshold_key)
    if threshold is None:
        return RecommendationAction.UNAVAILABLE, "outside_execution_window"
    if recommendation.score.final_score >= threshold:
        return RecommendationAction.EXECUTABLE, "score_threshold_met"
    elif recommendation.score.final_score >= threshold - observation_margin:
        return RecommendationAction.OBSERVE, "near_score_threshold"
    return RecommendationAction.UNAVAILABLE, "below_score_threshold"


def select_top_k(
    recommendations: Iterable[Recommendation],
    *,
    top_k: int,
    maximum_per_industry: int,
    minimum_final_score: float = 0.0,
) -> tuple[Recommendation, ...]:
    if not 0 <= top_k <= 18:
        raise ValueError("top_k must be between 0 and 18")
    if maximum_per_industry < 1:
        raise ValueError("maximum_per_industry must be positive")
    if not 0.0 <= minimum_final_score <= 100.0:
        raise ValueError("minimum_final_score must be between 0 and 100")
    ordered = sorted(
        (item for item in recommendations if item.score.final_score >= minimum_final_score),
        key=lambda item: (
            -item.score.final_score,
            -item.score.local_score,
            item.features.quote.code,
        ),
    )
    selected: list[Recommendation] = []
    industry_counts: Counter[str] = Counter()
    for item in ordered:
        if len(selected) >= top_k:
            break
        industry = item.features.quote.industry or "unknown"
        if industry_counts[industry] >= maximum_per_industry:
            continue
        industry_counts[industry] += 1
        selected.append(replace(item, rank=len(selected) + 1))
    return tuple(selected)


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
        return "today_late" if phase == "today_late" else "today_main"
    if strategy is Strategy.TOMORROW:
        return "tomorrow"
    if strategy is Strategy.D25:
        return "d25"
    return None


__all__ = ["CORE_FIELDS", "action_for", "candidate_score", "minimum_selection_score", "select_top_k"]
