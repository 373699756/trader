"""Pure deterministic ranking functions."""

from __future__ import annotations

from collections.abc import Sequence

from trader.domain.market.factors import average_rank_percentiles


def percentile_ranks(values: Sequence[float]) -> tuple[float, ...]:
    return average_rank_percentiles(values)


__all__ = ["percentile_ranks"]
