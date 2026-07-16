"""Candidate preselection, action policy and industry-constrained TopK."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import replace

from trader.domain.factors import band_score, clamp, weighted_score
from trader.domain.models import (
    FeatureSnapshot,
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
    "industry_strength",
)


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
    threshold_key = (
        "today_late"
        if recommendation.strategy is Strategy.TODAY and phase == "today_late"
        else recommendation.strategy.value
    )
    if recommendation.strategy is Strategy.TODAY and threshold_key == Strategy.TODAY.value:
        threshold_key = "today_main"
    threshold = thresholds.get(threshold_key)
    if threshold is None:
        return RecommendationAction.UNAVAILABLE, "outside_execution_window"
    if recommendation.score.final_score >= threshold:
        return RecommendationAction.EXECUTABLE, "score_threshold_met"
    if recommendation.score.final_score >= threshold - observation_margin:
        return RecommendationAction.OBSERVE, "near_score_threshold"
    return RecommendationAction.UNAVAILABLE, "below_score_threshold"


def select_top_k(
    recommendations: Iterable[Recommendation],
    *,
    top_k: int,
    maximum_per_industry: int,
) -> tuple[Recommendation, ...]:
    if top_k < 0:
        raise ValueError("top_k cannot be negative")
    if maximum_per_industry < 1:
        raise ValueError("maximum_per_industry must be positive")
    ordered = sorted(
        recommendations,
        key=lambda item: (
            item.veto,
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


__all__ = ["CORE_FIELDS", "action_for", "candidate_score", "select_top_k"]
