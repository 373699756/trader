"""Pure deterministic ranking functions."""

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


__all__ = ["percentile_ranks"]
