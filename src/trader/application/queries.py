"""Read-only recommendation and audit queries for delivery adapters."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from trader.application.ports import EventReaderPort, SnapshotRepositoryPort
from trader.application.schedule import freeze_due_at, trade_date_at
from trader.domain.models import LiveOverlay, LiveQuote, RecommendationSnapshot, Strategy


@dataclass(frozen=True)
class SnapshotLookup:
    status: str
    snapshot: RecommendationSnapshot | None
    historical: bool
    overlay: LiveOverlay | None = None
    fallback_date: str | None = None
    fallback_reason: str | None = None
    current_trade_date: str | None = None
    current_quotes: Mapping[str, LiveQuote] | None = None

    @property
    def etag(self) -> str | None:
        if self.snapshot is None:
            return None
        values = [self.snapshot.snapshot_id, self.current_trade_date or self.snapshot.trade_date]
        if self.overlay is not None:
            values.append(self.overlay.version)
        if self.fallback_date is not None:
            values.extend(("fallback", self.fallback_date, self.fallback_reason or ""))
        return ":".join(values)


class RecommendationQueries:
    def __init__(
        self,
        repository: SnapshotRepositoryPort,
        events: EventReaderPort,
        *,
        now: Callable[[], datetime],
    ) -> None:
        self._repository = repository
        self._events = events
        self._now = now

    def recommendation(self, strategy: Strategy, trade_date: str | None = None) -> SnapshotLookup:
        if trade_date is None:
            now = self._now()
            current_date = trade_date_at(now)
            if strategy is not Strategy.LONG and strategy.value in freeze_due_at(now, is_trading_day=True):
                frozen = self._repository.load_frozen(strategy, current_date.isoformat())
                if frozen is not None:
                    return self._current_lookup(strategy, current_date.isoformat(), frozen)
                latest = self._repository.latest(strategy)
                if latest is None or latest.trade_date == current_date.isoformat() or not latest.frozen:
                    return SnapshotLookup(
                        "not_ready",
                        None,
                        False,
                        current_trade_date=current_date.isoformat(),
                    )
                return self._current_lookup(strategy, current_date.isoformat(), latest)
            snapshot = self._repository.latest(strategy)
            return self._current_lookup(strategy, current_date.isoformat(), snapshot)
        if strategy is Strategy.LONG:
            snapshot = self._repository.latest(strategy) if trade_date == self.today() else None
        else:
            snapshot = self._repository.load_frozen(strategy, trade_date)
        current_trade_date = self.today()
        current_snapshot = self._repository.latest(strategy)
        current_overlay = (
            self._repository.load_live_overlay(strategy, current_snapshot.trade_date)
            if current_snapshot is not None and current_snapshot.trade_date == current_trade_date
            else None
        )
        current_quotes = (
            _snapshot_quotes(current_snapshot)
            if current_snapshot is not None and current_snapshot.trade_date == current_trade_date
            else {}
        )
        if (
            current_snapshot is not None
            and current_overlay is not None
            and current_overlay.snapshot_id == current_snapshot.snapshot_id
        ):
            current_quotes.update(current_overlay.quotes)
        return SnapshotLookup(
            "ready" if snapshot is not None else "not_found",
            snapshot,
            True,
            current_trade_date=current_trade_date,
            current_quotes=current_quotes,
        )

    def _current_lookup(
        self,
        strategy: Strategy,
        current_date: str,
        snapshot: RecommendationSnapshot | None,
    ) -> SnapshotLookup:
        if snapshot is None:
            return SnapshotLookup("not_ready", None, False, current_trade_date=current_date)
        overlay = self._repository.load_live_overlay(strategy, snapshot.trade_date)
        if overlay is not None and overlay.snapshot_id != snapshot.snapshot_id:
            overlay = None
        if snapshot.trade_date == current_date:
            return SnapshotLookup(
                "ready",
                snapshot,
                False,
                overlay=overlay,
                current_trade_date=current_date,
            )
        if strategy is not Strategy.LONG and not snapshot.frozen:
            return SnapshotLookup("not_ready", None, False, current_trade_date=current_date)
        reasons = tuple(dict.fromkeys((*snapshot.degraded_reasons, "previous_trade_date_fallback")))
        stale = replace(snapshot, stale=True, degraded_reasons=reasons)
        return SnapshotLookup(
            "ready",
            stale,
            False,
            overlay=overlay,
            fallback_date=snapshot.trade_date,
            fallback_reason="previous_trade_date_snapshot",
            current_trade_date=current_date,
        )

    def recommendation_dates(self, strategy: Strategy) -> Sequence[str]:
        return self._repository.recommendation_dates(strategy)

    def pipeline_events(self, *, cursor: int, limit: int) -> Sequence[Mapping[str, object]]:
        return self._events.list_events(cursor=cursor, limit=limit)

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


__all__ = ["RecommendationQueries", "SnapshotLookup"]
