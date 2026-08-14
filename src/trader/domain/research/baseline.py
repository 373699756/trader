"""Pure Score-R3 metrics for deterministic historical baseline reports."""

from __future__ import annotations

import math
from collections import defaultdict


def stock_net_contribution(
    selected_weight: float,
    gross_excess_return: float,
    turnover: float,
    cost_rate: float,
) -> float:
    """Apply the preregistered round-trip cost to one immutable settlement basis."""

    _finite((selected_weight, gross_excess_return, turnover, cost_rate))
    if not 0.0 <= selected_weight <= 1.0 or turnover < 0.0 or cost_rate < 0.0:
        raise ValueError("research contribution weights, turnover, and cost must be non-negative")
    return selected_weight * gross_excess_return - selected_weight * turnover * cost_rate


def population_spearman(pairs: tuple[tuple[float, float], ...]) -> float | None:
    """Return population Spearman correlation with average ties, or None below five pairs."""

    if len(pairs) < 5:
        return None
    if any(not math.isfinite(left) or not math.isfinite(right) for left, right in pairs):
        raise ValueError("rank correlation inputs must be finite")
    left = _average_ranks(tuple(item[0] for item in pairs))
    right = _average_ranks(tuple(item[1] for item in pairs))
    left_mean = math.fsum(left) / len(left)
    right_mean = math.fsum(right) / len(right)
    covariance = math.fsum((x - left_mean) * (y - right_mean) for x, y in zip(left, right, strict=True))
    left_variance = math.fsum((value - left_mean) ** 2 for value in left)
    right_variance = math.fsum((value - right_mean) ** 2 for value in right)
    if left_variance == 0.0 or right_variance == 0.0:
        return None
    return covariance / math.sqrt(left_variance * right_variance)


def mean_rank_ic(values: tuple[float | None, ...]) -> float | None:
    known = tuple(value for value in values if value is not None)
    if not known:
        return None
    _finite(known)
    return math.fsum(known) / len(known)


def quantile_bucket(rows: tuple[tuple[str, float], ...]) -> dict[str, int]:
    """Assign five nearly equal score groups with stable code ordering for ties."""

    if len({code for code, _score in rows}) != len(rows) or any(not code for code, _score in rows):
        raise ValueError("score bucket identities must be unique")
    if any(not math.isfinite(score) for _code, score in rows):
        raise ValueError("score bucket values must be finite")
    if not rows:
        return {}
    score_ranks = _average_ranks(tuple(score for _code, score in rows))
    ranked = tuple((code, rank) for (code, _score), rank in zip(rows, score_ranks, strict=True))
    ordered = tuple(sorted(ranked, key=lambda item: (-item[1], item[0])))
    return {
        code: max(1, 5 - math.floor((position - 1) * 5.0 / len(ordered)))
        for position, (code, _score) in enumerate(ordered, start=1)
    }


def _average_ranks(values: tuple[float, ...]) -> tuple[float, ...]:
    positions: dict[float, list[int]] = defaultdict(list)
    for position, (_index, value) in enumerate(sorted(enumerate(values), key=lambda item: item[1]), start=1):
        positions[value].append(position)
    average = {value: math.fsum(ranks) / len(ranks) for value, ranks in positions.items()}
    return tuple(average[value] for value in values)


def _finite(values: tuple[float, ...]) -> None:
    if any(not math.isfinite(value) for value in values):
        raise ValueError("research metric inputs must be finite")


__all__ = ["mean_rank_ic", "population_spearman", "quantile_bucket", "stock_net_contribution"]
