"""Read-only recommendation and audit queries for delivery adapters."""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from trader.application.ports import (
    CurrentQuoteReaderPort,
    CurrentSnapshotReaderPort,
    EventReaderPort,
    SnapshotRepositoryPort,
)
from trader.application.schedule import freeze_due_at, shanghai_now, trade_date_at
from trader.domain.models import LiveOverlay, LiveQuote, RecommendationSnapshot, Strategy


@dataclass(frozen=True)
class SnapshotLookup:
    status: str
    snapshot: RecommendationSnapshot | None
    historical: bool
    overlay: LiveOverlay | None = None
    current_trade_date: str | None = None
    current_quotes: Mapping[str, LiveQuote] | None = None

    @property
    def etag(self) -> str | None:
        if self.snapshot is None:
            return None
        values = [self.snapshot.snapshot_id, self.current_trade_date or self.snapshot.trade_date]
        if self.overlay is not None:
            values.append(self.overlay.version)
        return ":".join(values)


class RecommendationQueries:
    _RESIDENT_HISTORY_DAYS = 20
    _RESIDENT_HISTORY_VIEWS = 60

    def __init__(
        self,
        repository: SnapshotRepositoryPort,
        events: EventReaderPort,
        *,
        now: Callable[[], datetime],
        current_quote_reader: CurrentQuoteReaderPort | None = None,
        current_snapshot_reader: CurrentSnapshotReaderPort | None = None,
    ) -> None:
        self._repository = repository
        self._events = events
        self._now = now
        self._current_quote_reader = current_quote_reader
        self._current_snapshot_reader = current_snapshot_reader or repository
        self._uses_runtime_snapshot_index = current_snapshot_reader is not None
        self._history_lock = threading.Lock()
        self._history: OrderedDict[tuple[Strategy, str], RecommendationSnapshot] = OrderedDict()

    def initialize(self) -> Mapping[str, int]:
        loaded = 0
        for strategy in (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25):
            dates = tuple(self._repository.recommendation_dates(strategy))[: self._RESIDENT_HISTORY_DAYS]
            for trade_date in dates:
                if self._historical_snapshot(strategy, trade_date) is not None:
                    loaded += 1
        return {"historical_views_preloaded": loaded}

    def recommendation(self, strategy: Strategy, trade_date: str | None = None) -> SnapshotLookup:
        if trade_date is None:
            now = self._now()
            current_date = trade_date_at(now)
            if strategy is not Strategy.LONG and strategy.value in freeze_due_at(now, is_trading_day=True):
                frozen = self._current_snapshot_reader.latest(strategy)
                if frozen is not None and (frozen.trade_date != current_date.isoformat() or not frozen.frozen):
                    frozen = None
                if frozen is None and not self._uses_runtime_snapshot_index:
                    frozen = self._historical_snapshot(strategy, current_date.isoformat())
                if frozen is not None:
                    return self._current_lookup(strategy, current_date.isoformat(), frozen)
                latest = self._current_snapshot_reader.latest(strategy)
                if latest is None or latest.trade_date == current_date.isoformat() or not latest.frozen:
                    return SnapshotLookup(
                        "not_ready",
                        None,
                        False,
                        current_trade_date=current_date.isoformat(),
                    )
                return self._current_lookup(strategy, current_date.isoformat(), latest)
            snapshot = self._current_snapshot_reader.latest(strategy)
            return self._current_lookup(strategy, current_date.isoformat(), snapshot)
        if strategy is Strategy.LONG:
            snapshot = self._current_snapshot_reader.latest(strategy) if trade_date == self.today() else None
        else:
            snapshot = self._historical_snapshot(strategy, trade_date)
        current_trade_date = self.today()
        current_quotes = self._historical_current_quotes(strategy, snapshot, current_trade_date)
        return SnapshotLookup(
            "ready" if snapshot is not None else "not_found",
            snapshot,
            True,
            current_trade_date=current_trade_date,
            current_quotes=current_quotes,
        )

    def _historical_current_quotes(
        self,
        strategy: Strategy,
        snapshot: RecommendationSnapshot | None,
        current_trade_date: str,
    ) -> Mapping[str, LiveQuote]:
        if snapshot is None:
            return {}
        if self._current_quote_reader is not None:
            codes = tuple(item.features.quote.code for item in snapshot.recommendations)
            quotes = self._current_quote_reader.current_quotes(codes)
            return {
                code: quote
                for code, quote in quotes.items()
                if code in codes and shanghai_now(quote.source_time).date().isoformat() == current_trade_date
            }
        current_snapshot = self._repository.latest(strategy)
        if current_snapshot is None or current_snapshot.trade_date != current_trade_date:
            return {}
        current_quotes = _snapshot_quotes(current_snapshot)
        current_overlay = self._repository.load_live_overlay(strategy, current_snapshot.trade_date)
        if current_overlay is not None and current_overlay.snapshot_id == current_snapshot.snapshot_id:
            current_quotes.update(current_overlay.quotes)
        return current_quotes

    def _current_lookup(
        self,
        strategy: Strategy,
        current_date: str,
        snapshot: RecommendationSnapshot | None,
    ) -> SnapshotLookup:
        if snapshot is None or snapshot.trade_date != current_date:
            return SnapshotLookup("not_ready", None, False, current_trade_date=current_date)
        overlay = self._current_snapshot_reader.load_live_overlay(strategy, snapshot.trade_date)
        if overlay is not None and overlay.snapshot_id != snapshot.snapshot_id:
            overlay = None
        return SnapshotLookup(
            "ready",
            _delivery_snapshot(snapshot),
            False,
            overlay=overlay,
            current_trade_date=current_date,
        )

    def recommendation_dates(self, strategy: Strategy) -> Sequence[str]:
        return self._repository.recommendation_dates(strategy)

    def pipeline_events(self, *, cursor: int, limit: int) -> Sequence[Mapping[str, object]]:
        return self._events.list_events(cursor=cursor, limit=limit)

    def today(self) -> str:
        return trade_date_at(self._now()).isoformat()

    def _historical_snapshot(self, strategy: Strategy, trade_date: str) -> RecommendationSnapshot | None:
        key = (strategy, trade_date)
        with self._history_lock:
            if key in self._history:
                cached = self._history.pop(key)
                self._history[key] = cached
                return cached
        snapshot = self._repository.load_frozen(strategy, trade_date)
        if snapshot is None:
            return None
        delivery = _delivery_snapshot(snapshot)
        with self._history_lock:
            self._history[key] = delivery
            while len(self._history) > self._RESIDENT_HISTORY_VIEWS:
                self._history.popitem(last=False)
        return delivery


def _snapshot_quotes(snapshot: RecommendationSnapshot) -> dict[str, LiveQuote]:
    return {
        recommendation.features.quote.code: LiveQuote(
            code=recommendation.features.quote.code,
            price=recommendation.features.quote.price,
            pct_change=recommendation.features.quote.pct_change,
            source=recommendation.features.quote.source,
            source_time=recommendation.features.quote.source_time,
            received_time=recommendation.features.quote.received_time,
            data_version=recommendation.features.quote.data_version,
        )
        for recommendation in snapshot.recommendations
    }


def _delivery_snapshot(snapshot: RecommendationSnapshot) -> RecommendationSnapshot:
    if not snapshot.filter_details and snapshot.replay_input is None:
        return snapshot
    return replace(snapshot, filter_details=(), replay_input=None)


__all__ = ["RecommendationQueries", "SnapshotLookup"]
