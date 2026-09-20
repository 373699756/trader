"""Pure metrics for the native score-factor diagnostic report."""

from __future__ import annotations

import math
from collections import defaultdict


def population_pearson(pairs: tuple[tuple[float, float], ...]) -> float | None:
    """Return population Pearson correlation, preserving unknown small samples."""

    if len(pairs) < 5:
        return None
    _finite_pairs(pairs)
    left_mean = math.fsum(left for left, _right in pairs) / len(pairs)
    right_mean = math.fsum(right for _left, right in pairs) / len(pairs)
    covariance = math.fsum((left - left_mean) * (right - right_mean) for left, right in pairs)
    left_variance = math.fsum((left - left_mean) ** 2 for left, _right in pairs)
    right_variance = math.fsum((right - right_mean) ** 2 for _left, right in pairs)
    if left_variance == 0.0 or right_variance == 0.0:
        return None
    return covariance / math.sqrt(left_variance * right_variance)


def information_coefficient_ratio(values: tuple[float | None, ...]) -> float | None:
    """Return unannualized mean IC divided by its sample standard deviation."""

    known = tuple(value for value in values if value is not None)
    if len(known) < 2:
        return None
    _finite(known)
    mean = math.fsum(known) / len(known)
    variance = math.fsum((value - mean) ** 2 for value in known) / (len(known) - 1)
    if variance == 0.0:
        return None
    return mean / math.sqrt(variance)


def monotonicity(
    quintiles: tuple[float | None, float | None, float | None, float | None, float | None],
) -> tuple[float | None, float | None]:
    """Return adjacent non-decreasing share and Q5-minus-Q1."""

    if any(value is None for value in quintiles):
        return None, None
    known = tuple(value for value in quintiles if value is not None)
    _finite(known)
    adjacent = tuple(right >= left for left, right in zip(known[:-1], known[1:], strict=True))
    return sum(adjacent) / len(adjacent), known[-1] - known[0]


def top_bucket_turnover(previous: tuple[str, ...], current: tuple[str, ...]) -> float | None:
    """Return symmetric replacement share between two non-empty top buckets."""

    if not previous or not current:
        return None
    if len(previous) != len(set(previous)) or len(current) != len(set(current)):
        raise ValueError("top-bucket identities must be unique")
    overlap = len(set(previous).intersection(current))
    return 1.0 - overlap / max(len(previous), len(current))


def factor_concentration(contributions: tuple[tuple[str, float], ...]) -> tuple[float | None, float | None]:
    """Aggregate positive Q5 contributions by stock and return max/top-five shares."""

    if any(not code for code, _value in contributions):
        raise ValueError("factor contribution codes must not be empty")
    _finite(tuple(value for _code, value in contributions))
    contribution_by_code: dict[str, float] = defaultdict(float)
    for code, value in contributions:
        contribution_by_code[code] += value
    positive = tuple(value for value in contribution_by_code.values() if value > 0.0)
    total = math.fsum(positive)
    if total <= 0.0:
        return None, None
    ordered = sorted(positive, reverse=True)
    return ordered[0] / total, math.fsum(ordered[:5]) / total


def _finite_pairs(pairs: tuple[tuple[float, float], ...]) -> None:
    _finite(tuple(value for pair in pairs for value in pair))


def _finite(values: tuple[float, ...]) -> None:
    if any(not math.isfinite(value) for value in values):
        raise ValueError("factor diagnostic inputs must be finite")


__all__ = [
    "factor_concentration",
    "information_coefficient_ratio",
    "monotonicity",
    "population_pearson",
    "top_bucket_turnover",
]
