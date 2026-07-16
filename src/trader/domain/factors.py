"""Deterministic factor math and score normalization."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from decimal import ROUND_HALF_UP, Decimal


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
    finite = sorted((float(value), key) for key, value in values.items() if value is not None and math.isfinite(value))
    result = {key: 50.0 for key in values}
    if not finite:
        return result
    if len(finite) == 1:
        only_key = finite[0][1]
        result[only_key] = 50.0
        return result

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
    return result


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
    "percentile_scores",
    "round_score",
    "weighted_score",
    "winsorize",
]
