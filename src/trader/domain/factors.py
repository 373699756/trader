"""Deterministic factor math and score normalization."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from decimal import ROUND_HALF_UP, Decimal

from trader.domain.models import CrossSectionStats

PRODUCTION_FACTOR_IDS = frozenset(
    {
        "amount_median_20d",
        "amount_percentile_20d",
        "breakout_20d",
        "capacity_score",
        "close_location",
        "d25_overheat_factor",
        "evidence_freshness",
        "financial_deterioration",
        "growth_score",
        "industry_breadth",
        "industry_policy_score",
        "industry_strength",
        "industry_trend",
        "intraday_reversal",
        "limit_distance_safety",
        "limit_proximity",
        "liquidity_contraction",
        "low_crowding_score",
        "low_drawdown_score",
        "low_volatility_score",
        "ma20_60_position",
        "ma20_60_structure",
        "ma20_deviation_inverse",
        "ma_slope",
        "market_breadth",
        "market_regime_factor",
        "max_drawdown_20d",
        "moderate_amplitude",
        "moderate_daily_return",
        "negative_announcement_level",
        "news_sentiment",
        "pledge_risk",
        "price_executability",
        "price_volume_confirmation",
        "price_volume_divergence",
        "quality_score",
        "reduction_or_unlock",
        "shareholder_reduction_level",
        "short_term_overheat",
        "relative_strength_10d",
        "relative_strength_20d",
        "relative_strength_3d",
        "relative_strength_5d",
        "return_10d",
        "return_20d",
        "return_20d_not_overheated",
        "return_3d",
        "return_5d",
        "return_60d",
        "risk_adjusted_return_20d",
        "risk_protection_score",
        "speed_percentile",
        "tail_return_30m",
        "tail_return_30m_pct",
        "tail_volume_ratio",
        "tail_volume_ratio_raw",
        "trend_score",
        "trend_breakdown",
        "turnover_median_20d",
        "unlock_risk",
        "upward_consistency",
        "value_score",
        "volatility_20d",
    }
)


def clamp(value: float, lower: float = 0.0, upper: float = 100.0) -> float:
    if not math.isfinite(value):
        raise ValueError("score must be finite")
    if lower > upper:
        raise ValueError("lower bound cannot exceed upper bound")
    return min(upper, max(lower, value))


def round_score(value: float, decimals: int = 2) -> float:
    bounded = clamp(value)
    quantum = Decimal(1).scaleb(-decimals)
    return float(Decimal(str(bounded)).quantize(quantum, rounding=ROUND_HALF_UP))


def band_score(value: float | None, lower: float, optimal_low: float, optimal_high: float, upper: float) -> float:
    if not lower < optimal_low <= optimal_high < upper:
        raise ValueError("band boundaries must satisfy lower < optimal_low <= optimal_high < upper")
    if value is None or not math.isfinite(value):
        return 50.0
    if value <= lower or value >= upper:
        return 0.0
    if optimal_low <= value <= optimal_high:
        return 100.0
    if value < optimal_low:
        return 100.0 * (value - lower) / (optimal_low - lower)
    return 100.0 * (upper - value) / (upper - optimal_high)


def weighted_score(values: Mapping[str, float], weights: Mapping[str, float]) -> float:
    if set(values) != set(weights):
        missing = sorted(set(weights) - set(values))
        extra = sorted(set(values) - set(weights))
        raise ValueError(f"component mismatch: missing={missing}, extra={extra}")
    if abs(sum(weights.values()) - 1.0) > 1e-9:
        raise ValueError("weights must sum to 1.0")
    return clamp(sum(clamp(values[name]) * weights[name] for name in weights))


def percentile_scores(values: Mapping[str, float | None], *, inverse: bool = False) -> dict[str, float]:
    scores, _ = percentile_scores_with_metadata(values, inverse=inverse)
    return scores


def percentile_scores_with_metadata(
    values: Mapping[str, float | None],
    *,
    inverse: bool = False,
    lower_quantile: float = 0.025,
    upper_quantile: float = 0.975,
    population_data_version: str = "",
) -> tuple[dict[str, float], CrossSectionStats]:
    if not 0.0 <= lower_quantile <= upper_quantile <= 1.0:
        raise ValueError("invalid winsor quantiles")
    raw_finite = sorted(
        (float(value), key) for key, value in values.items() if value is not None and math.isfinite(value)
    )
    result = {key: 50.0 for key in values}
    missing_count = len(values) - len(raw_finite)
    if not raw_finite:
        return result, CrossSectionStats(
            None,
            None,
            0,
            missing_count,
            lower_quantile,
            upper_quantile,
            population_data_version,
        )
    raw_values = [value for value, _ in raw_finite]
    lower = _quantile(raw_values, lower_quantile)
    upper = _quantile(raw_values, upper_quantile)
    finite = sorted((min(upper, max(lower, value)), key) for value, key in raw_finite)
    metadata = CrossSectionStats(
        lower,
        upper,
        len(finite),
        missing_count,
        lower_quantile,
        upper_quantile,
        population_data_version,
    )
    if len(finite) == 1:
        only_key = finite[0][1]
        result[only_key] = 50.0
        return result, metadata

    index = 0
    while index < len(finite):
        end = index + 1
        while end < len(finite) and finite[end][0] == finite[index][0]:
            end += 1
        average_rank = (index + end - 1) / 2
        score = average_rank * 100.0 / (len(finite) - 1)
        if inverse:
            score = 100.0 - score
        for _, key in finite[index:end]:
            result[key] = score
        index = end
    return result, metadata


def winsorize(values: Iterable[float], lower_quantile: float = 0.025, upper_quantile: float = 0.975) -> list[float]:
    finite = sorted(float(value) for value in values if math.isfinite(value))
    if not finite:
        return []
    if not 0.0 <= lower_quantile <= upper_quantile <= 1.0:
        raise ValueError("invalid winsor quantiles")
    lower = _quantile(finite, lower_quantile)
    upper = _quantile(finite, upper_quantile)
    return [min(upper, max(lower, value)) for value in finite]


def inverse_score(value: float) -> float:
    return 100.0 - clamp(value)


def _quantile(sorted_values: list[float], quantile: float) -> float:
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = quantile * (len(sorted_values) - 1)
    lower_index = int(math.floor(position))
    upper_index = int(math.ceil(position))
    if lower_index == upper_index:
        return sorted_values[lower_index]
    fraction = position - lower_index
    return sorted_values[lower_index] * (1.0 - fraction) + sorted_values[upper_index] * fraction


__all__ = [
    "band_score",
    "clamp",
    "inverse_score",
    "percentile_scores_with_metadata",
    "PRODUCTION_FACTOR_IDS",
    "percentile_scores",
    "round_score",
    "weighted_score",
    "winsorize",
]
