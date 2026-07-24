"""Read-only recommendation queries for delivery adapters."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from trader.application.ports.market import QuoteReaderPort
from trader.application.ports.snapshots import PublishedSnapshotReadPort, SnapshotReaderPort
from trader.application.recommendations import RecommendationEngine
from trader.application.schedule import freeze_due_at, shanghai_now, trade_date_at
from trader.domain.market.models import LiveQuote
from trader.domain.recommendation.models import (
    LiveOverlay,
    RecommendationSnapshot,
    Strategy,
)


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


@dataclass(frozen=True)
class CloseFallbackReplay:
    archive: SnapshotReaderPort
    engine: RecommendationEngine


class RecommendationQueries:
    def __init__(
        self,
        snapshots: PublishedSnapshotReadPort,
        *,
        now: Callable[[], datetime],
        current_quote_reader: QuoteReaderPort | None = None,
        close_fallback_replay: CloseFallbackReplay | None = None,
    ) -> None:
        self._snapshots = snapshots
        self._now = now
        self._current_quote_reader = current_quote_reader
        self._close_fallback_replay = close_fallback_replay

    def initialize(self) -> Mapping[str, int]:
        return {"historical_views_preloaded": 0}

    def recommendation(
        self,
        strategy: Strategy,
        trade_date: str | None = None,
        *,
        live: bool = False,
    ) -> SnapshotLookup:
        if trade_date is None:
            now = self._now()
            current_date = trade_date_at(now)
            if live:
                snapshot = self._snapshots.latest(strategy)
                return self._current_lookup(strategy, current_date.isoformat(), snapshot)
            if strategy is not Strategy.LONG and strategy.value in freeze_due_at(now, is_trading_day=True):
                frozen = self._snapshots.latest(strategy)
                if frozen is not None and (frozen.trade_date != current_date.isoformat() or not frozen.frozen):
                    frozen = None
                if frozen is not None:
                    return self._current_lookup(strategy, current_date.isoformat(), frozen)
                latest = self._snapshots.latest(strategy)
                if latest is None or latest.trade_date == current_date.isoformat() or not latest.frozen:
                    return SnapshotLookup(
                        "not_ready",
                        None,
                        False,
                        current_trade_date=current_date.isoformat(),
                    )
                return self._current_lookup(strategy, current_date.isoformat(), latest)
            snapshot = self._snapshots.latest(strategy)
            return self._current_lookup(strategy, current_date.isoformat(), snapshot)
        if strategy is Strategy.LONG:
            snapshot = self._snapshots.latest(strategy) if trade_date == self.today() else None
        else:
            snapshot = self._snapshots.load_frozen(strategy, trade_date)
            snapshot = self._recover_empty_close_fallback(snapshot)
        current_trade_date = self.today()
        current_quotes = self._historical_current_quotes(strategy, snapshot, current_trade_date)
        return SnapshotLookup(
            "ready" if snapshot is not None else "not_found",
            snapshot,
            True,
            current_trade_date=current_trade_date,
            current_quotes=current_quotes,
        )

    def current_recommendation(self, strategy: Strategy) -> SnapshotLookup:
        current_date = trade_date_at(self._now()).isoformat()
        return self._current_lookup(strategy, current_date, self._snapshots.latest(strategy))

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
        current_snapshot = self._snapshots.latest(strategy)
        if current_snapshot is None or current_snapshot.trade_date != current_trade_date:
            return {}
        current_quotes = _snapshot_quotes(current_snapshot)
        current_overlay = self._snapshots.load_live_overlay(strategy, current_snapshot.trade_date)
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
        snapshot = self._recover_empty_close_fallback(snapshot)
        assert snapshot is not None
        overlay = self._snapshots.load_live_overlay(strategy, snapshot.trade_date)
        if overlay is not None and overlay.snapshot_id != snapshot.snapshot_id:
            overlay = None
        return SnapshotLookup(
            "ready",
            _delivery_snapshot(snapshot),
            False,
            overlay=overlay,
            current_trade_date=current_date,
        )

    def _recover_empty_close_fallback(
        self,
        snapshot: RecommendationSnapshot | None,
    ) -> RecommendationSnapshot | None:
        if not _needs_close_fallback_replay(snapshot) or self._close_fallback_replay is None:
            return snapshot
        assert snapshot is not None
        raw_snapshot = _raw_close_fallback_snapshot(snapshot, self._close_fallback_replay.archive)
        if raw_snapshot.replay_input is None:
            return snapshot
        try:
            recovered = self._close_fallback_replay.engine.replay(raw_snapshot)
        except (RuntimeError, ValueError):
            return snapshot
        if not recovered.recommendations:
            return snapshot
        return replace(recovered, frozen=raw_snapshot.frozen, config_version=raw_snapshot.config_version)

    def recommendation_dates(self, strategy: Strategy) -> Sequence[str]:
        return self._snapshots.recommendation_dates(strategy)

    def today(self) -> str:
        return trade_date_at(self._now()).isoformat()


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


def _needs_close_fallback_replay(snapshot: RecommendationSnapshot | None) -> bool:
    return (
        snapshot is not None
        and snapshot.strategy is not Strategy.LONG
        and snapshot.phase == "close_fallback"
        and snapshot.frozen
        and not snapshot.recommendations
    )


def _raw_close_fallback_snapshot(
    snapshot: RecommendationSnapshot,
    archive: SnapshotReaderPort,
) -> RecommendationSnapshot:
    if snapshot.replay_input is not None:
        return snapshot
    return archive.load_frozen(snapshot.strategy, snapshot.trade_date) or snapshot


def _delivery_snapshot(snapshot: RecommendationSnapshot) -> RecommendationSnapshot:
    if not snapshot.filter_details and snapshot.replay_input is None:
        return snapshot
    return replace(snapshot, filter_details=(), replay_input=None)


__all__ = ["CloseFallbackReplay", "RecommendationQueries", "SnapshotLookup"]
