"""Read-only recommendation and audit queries for delivery adapters."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from trader.application.ports import EventAuditPort, SnapshotRepositoryPort
from trader.application.schedule import freeze_due_at, trade_date_at
from trader.domain.models import RecommendationSnapshot, Strategy


@dataclass(frozen=True)
class SnapshotLookup:
    status: str
    snapshot: RecommendationSnapshot | None
    historical: bool


class RecommendationQueries:
    def __init__(
        self,
        repository: SnapshotRepositoryPort,
        events: EventAuditPort,
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
                    return SnapshotLookup("ready", frozen, False)
                latest = self._repository.latest(strategy)
                if latest is None or latest.trade_date == current_date.isoformat() or not latest.frozen:
                    return SnapshotLookup("not_ready", None, False)
                return SnapshotLookup("ready", latest, False)
            snapshot = self._repository.latest(strategy)
            return SnapshotLookup("ready" if snapshot is not None else "not_ready", snapshot, False)
        if strategy is Strategy.LONG:
            snapshot = self._repository.latest(strategy) if trade_date == self.today() else None
        else:
            snapshot = self._repository.load_frozen(strategy, trade_date)
        return SnapshotLookup("ready" if snapshot is not None else "not_found", snapshot, True)

    def recommendation_dates(self, strategy: Strategy) -> Sequence[str]:
        return self._repository.recommendation_dates(strategy)

    def pipeline_events(self, *, cursor: int, limit: int) -> Sequence[Mapping[str, object]]:
        return self._events.list_events(cursor=cursor, limit=limit)

    def today(self) -> str:
        return trade_date_at(self._now()).isoformat()


__all__ = ["RecommendationQueries", "SnapshotLookup"]
