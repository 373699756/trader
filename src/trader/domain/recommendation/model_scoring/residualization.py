"""Pure exposure residualization shared by scoring heads."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence


def residualize_exposure(
    values: Sequence[float],
    boards: Sequence[str],
    average_amounts: Sequence[float],
) -> tuple[float, ...]:
    if not values or len(values) != len(boards) or len(values) != len(average_amounts):
        raise ValueError("scoring exposure vectors must have the same non-empty length")
    if any(not math.isfinite(value) for value in values) or any(
        not math.isfinite(amount) or amount <= 0.0 for amount in average_amounts
    ):
        raise ValueError("scoring exposure values must be finite and amounts positive")
    market_mean = math.fsum(values) / len(values)
    grouped: dict[str, list[int]] = defaultdict(list)
    for index, board in enumerate(boards):
        grouped[board].append(index)
    centered = [0.0] * len(values)
    amount_centered = [0.0] * len(values)
    for indices in grouped.values():
        board_mean = math.fsum(values[index] - market_mean for index in indices) / len(indices)
        logs = tuple(math.log(average_amounts[index]) for index in indices)
        amount_mean = math.fsum(logs) / len(logs)
        for index, logged_amount in zip(indices, logs, strict=True):
            centered[index] = values[index] - market_mean - board_mean
            amount_centered[index] = logged_amount - amount_mean
    denominator = math.fsum(value * value for value in amount_centered)
    slope = (
        math.fsum(value * amount for value, amount in zip(centered, amount_centered, strict=True)) / denominator
        if denominator > 0.0
        else 0.0
    )
    return tuple(value - slope * amount for value, amount in zip(centered, amount_centered, strict=True))


__all__ = ["residualize_exposure"]
