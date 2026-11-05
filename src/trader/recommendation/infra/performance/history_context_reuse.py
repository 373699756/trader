"""Two-entry offline workspace for equal short-window history metrics."""

from collections import OrderedDict
from dataclasses import dataclass

from trader.infra.market_data.history.history import (
    BuildHistoryContext, DailyBar, HistoryContext, HistoryProfile, SummarizeHistoryMetrics,
)
from trader.infra.serialization.canonical import ContentHash


@dataclass(frozen=True)
class HistoryContextReuseCounts:
    requests: int
    calculations: int
    hits: int
    entries: int


class ReusableHistoryContexts:
    """Keep independent anchors/counts/dates; reuse only the original metrics owner."""

    def __init__(self) -> None:
        self._metrics: OrderedDict[str, HistoryProfile] = OrderedDict()
        self._requests = self._calculations = self._hits = 0

    @property
    def counts(self) -> HistoryContextReuseCounts:
        return HistoryContextReuseCounts(self._requests, self._calculations, self._hits, len(self._metrics))

    def build(self, bars: tuple[DailyBar, ...], lookback: int) -> HistoryContext:
        return BuildHistoryContext(bars, lookback_sessions=lookback, summarize=self._summarize)

    def clear(self) -> None:
        self._metrics.clear()

    def _summarize(self, bars: tuple[DailyBar, ...]) -> HistoryProfile:
        self._requests += 1
        # SummarizeHistoryMetrics currently consumes at most MA60; its previous
        # profile has its own shifted tail. This is an isolated candidate contract.
        try:
            key = ContentHash(bars[-60:])
        except ValueError:
            self._calculations += 1
            return SummarizeHistoryMetrics(bars)
        if key in self._metrics:
            self._hits += 1
            self._metrics.move_to_end(key)
            return self._metrics[key]
        self._calculations += 1
        result = SummarizeHistoryMetrics(bars)
        self._metrics[key] = result
        while len(self._metrics) > 2:
            self._metrics.popitem(last=False)
        return result


__all__ = [
    "HistoryContextReuseCounts",
    "ReusableHistoryContexts",
]
