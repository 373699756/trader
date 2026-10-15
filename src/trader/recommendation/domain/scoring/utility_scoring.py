"""Pure deterministic ranking functions."""

from __future__ import annotations

from collections.abc import Sequence

from trader.recommendation.domain.market.factors import AverageRankPercentiles


def PercentileRanks(values: Sequence[float]) -> tuple[float, ...]:
    return AverageRankPercentiles(values)


__all__ = ["PercentileRanks"]
