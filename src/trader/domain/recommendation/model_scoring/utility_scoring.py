"""Pure cost-aware utility and deterministic ranking functions."""

from __future__ import annotations

from collections.abc import Sequence


def percentile_ranks(values: Sequence[float]) -> tuple[float, ...]:
    if len(values) <= 1:
        return (0.0,) * len(values)
    order = sorted(range(len(values)), key=lambda index: (values[index], index))
    ranks = [0.0] * len(values)
    for position, index in enumerate(order):
        ranks[index] = position / (len(values) - 1)
    return tuple(ranks)


def positive_utility_scores(values: Sequence[float]) -> tuple[float, ...]:
    if len(values) == 1:
        return (100.0 if values[0] > 0.0 else 0.0,)
    ranks = percentile_ranks(values)
    return tuple(100.0 * rank if value > 0.0 else 0.0 for value, rank in zip(values, ranks, strict=True))


__all__ = ["percentile_ranks", "positive_utility_scores"]
