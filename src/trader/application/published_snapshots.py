"""Bounded P6 recommendation read model with resident and cold-date tiers."""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from concurrent.futures import Future
from dataclasses import replace

from trader.application.cache import canonical_json_bytes
from trader.application.ports.snapshots import SnapshotReaderPort
from trader.domain.recommendation.models import LiveOverlay, RecommendationSnapshot, Strategy


class PublishedSnapshotIndex:
    """Own the complete Web read path and isolate cold readers with date single-flight."""

    _HISTORICAL_STRATEGIES = (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25)

    def __init__(
        self,
        archive: SnapshotReaderPort,
        *,
        resident_days: int = 20,
        cold_slots: int = 8,
        maximum_view_bytes: int = 160 * 1024,
    ) -> None:
        if resident_days < 1 or cold_slots < 3 or maximum_view_bytes < 1:
            raise ValueError("published snapshot index limits must be positive")
        self._archive = archive
        self._resident_days = resident_days
        self._cold_slots = cold_slots
        self._maximum_view_bytes = maximum_view_bytes
        self._lock = threading.RLock()
        self._current: dict[Strategy, RecommendationSnapshot] = {}
        self._resident: dict[tuple[Strategy, str], RecommendationSnapshot] = {}
        self._pending_committed: dict[tuple[Strategy, str], RecommendationSnapshot] = {}
        self._cold: OrderedDict[tuple[Strategy, str], RecommendationSnapshot] = OrderedDict()
        self._dates: dict[Strategy, tuple[str, ...]] = {strategy: () for strategy in self._HISTORICAL_STRATEGIES}
        self._overlays: dict[tuple[Strategy, str], LiveOverlay] = {}
        self._inflight: dict[str, Future[tuple[RecommendationSnapshot, ...]]] = {}
        self._counters: dict[str, int] = {
            "published": 0,
            "resident_hits": 0,
            "cold_hits": 0,
            "cold_misses": 0,
            "cold_loads": 0,
            "cold_coalesced": 0,
            "rejected_incomplete_dates": 0,
            "rejected_oversize_views": 0,
        }

    def initialize(self) -> Mapping[str, int]:
        date_sets = {
            strategy: set(self._archive.recommendation_dates(strategy)) for strategy in self._HISTORICAL_STRATEGIES
        }
        complete_dates = sorted(set.intersection(*date_sets.values()), reverse=True)
        loaded_dates = 0
        accepted_dates: list[str] = []
        for trade_date in complete_dates:
            if loaded_dates >= self._resident_days:
                break
            snapshots = self._load_complete_date(trade_date)
            if len(snapshots) != len(self._HISTORICAL_STRATEGIES):
                self._counters["rejected_incomplete_dates"] += 1
                continue
            with self._lock:
                for snapshot in snapshots:
                    delivery = _delivery_snapshot(snapshot)
                    if not self._fits(delivery):
                        self._counters["rejected_oversize_views"] += 1
                        break
                    self._resident[(snapshot.strategy, trade_date)] = delivery
                else:
                    loaded_dates += 1
                    accepted_dates.append(trade_date)
                    continue
                for strategy in self._HISTORICAL_STRATEGIES:
                    self._resident.pop((strategy, trade_date), None)
        with self._lock:
            accepted = tuple(accepted_dates)
            for strategy in self._HISTORICAL_STRATEGIES:
                self._dates[strategy] = tuple(complete_dates)
                strategy_dates = tuple(sorted(date_sets[strategy], reverse=True))
                latest = self._resident.get((strategy, accepted[0])) if accepted else None
                if latest is None and strategy_dates:
                    archived = self._archive.load_frozen(strategy, strategy_dates[0])
                    latest = _delivery_snapshot(archived) if archived is not None else None
                if latest is not None and self._fits(latest):
                    self._current[strategy] = latest
                    overlay = self._archive.load_live_overlay(strategy, latest.trade_date)
                    if overlay is not None and overlay.snapshot_id == latest.snapshot_id:
                        self._overlays[(strategy, latest.trade_date)] = overlay
        return {"resident_dates_preloaded": loaded_dates, "historical_views_preloaded": loaded_dates * 3}

    def publish(self, snapshot: RecommendationSnapshot) -> None:
        delivery = _delivery_snapshot(snapshot)
        with self._lock:
            if not self._fits(delivery):
                self._counters["rejected_oversize_views"] += 1
                return
            self._current[snapshot.strategy] = delivery
            self._discard_mismatched_overlay(delivery)
            self._counters["published"] += 1
            if snapshot.frozen and snapshot.strategy in self._HISTORICAL_STRATEGIES:
                self._pending_committed[(snapshot.strategy, snapshot.trade_date)] = delivery
                complete = all(
                    (strategy, snapshot.trade_date) in self._pending_committed
                    for strategy in self._HISTORICAL_STRATEGIES
                )
                if complete:
                    for strategy in self._HISTORICAL_STRATEGIES:
                        key = (strategy, snapshot.trade_date)
                        self._resident[key] = self._pending_committed.pop(key)
                        dates = (snapshot.trade_date, *self._dates[strategy])
                        self._dates[strategy] = tuple(dict.fromkeys(dates))
                        allowed = set(self._dates[strategy][: self._resident_days])
                        for resident_key in tuple(self._resident):
                            if resident_key[0] is strategy and resident_key[1] not in allowed:
                                self._resident.pop(resident_key, None)

    def publish_overlay(self, overlay: LiveOverlay) -> None:
        with self._lock:
            snapshot = self._current.get(overlay.strategy)
            if snapshot is not None and snapshot.snapshot_id == overlay.snapshot_id:
                self._overlays[(overlay.strategy, overlay.trade_date)] = overlay

    def latest(self, strategy: Strategy) -> RecommendationSnapshot | None:
        with self._lock:
            return self._current.get(strategy)

    def load_live_overlay(self, strategy: Strategy, trade_date: str) -> LiveOverlay | None:
        with self._lock:
            return self._overlays.get((strategy, trade_date))

    def load_frozen(self, strategy: Strategy, trade_date: str) -> RecommendationSnapshot | None:
        key = (strategy, trade_date)
        with self._lock:
            resident = self._resident.get(key)
            if resident is not None:
                self._counters["resident_hits"] += 1
                return resident
            cold = self._cold.pop(key, None)
            if cold is not None:
                self._cold[key] = cold
                for other_strategy in self._HISTORICAL_STRATEGIES:
                    other_key = (other_strategy, trade_date)
                    other = self._cold.pop(other_key, None)
                    if other is not None:
                        self._cold[other_key] = other
                self._counters["cold_hits"] += 1
                return cold
            self._counters["cold_misses"] += 1
        snapshots = self._cold_date(trade_date)
        return next((snapshot for snapshot in snapshots if snapshot.strategy is strategy), None)

    def recommendation_dates(self, strategy: Strategy) -> Sequence[str]:
        with self._lock:
            return self._dates.get(strategy, ())

    def status(self) -> Mapping[str, object]:
        with self._lock:
            return {
                "current_views": len(self._current),
                "resident_views": len(self._resident),
                "cold_views": len(self._cold),
                "maximum_views": 4 + self._resident_days * 3 + self._cold_slots,
                "maximum_view_bytes": self._maximum_view_bytes,
                "inflight_dates": len(self._inflight),
                **self._counters,
            }

    def _cold_date(self, trade_date: str) -> tuple[RecommendationSnapshot, ...]:
        with self._lock:
            future = self._inflight.get(trade_date)
            owner = future is None
            if future is None:
                future = Future()
                self._inflight[trade_date] = future
                self._counters["cold_loads"] += 1
            else:
                self._counters["cold_coalesced"] += 1
        if not owner:
            return future.result()
        try:
            snapshots = self._load_complete_date(trade_date)
            if len(snapshots) != len(self._HISTORICAL_STRATEGIES):
                snapshots = ()
                with self._lock:
                    self._counters["rejected_incomplete_dates"] += 1
            with self._lock:
                for snapshot in snapshots:
                    key = (snapshot.strategy, trade_date)
                    delivery = _delivery_snapshot(snapshot)
                    if not self._fits(delivery):
                        self._counters["rejected_oversize_views"] += 1
                        snapshots = ()
                        break
                    self._cold[key] = delivery
                if not snapshots:
                    for strategy in self._HISTORICAL_STRATEGIES:
                        self._cold.pop((strategy, trade_date), None)
                while len(self._cold) > self._cold_slots:
                    oldest_key = next(iter(self._cold))
                    oldest_date = oldest_key[1]
                    for strategy in self._HISTORICAL_STRATEGIES:
                        self._cold.pop((strategy, oldest_date), None)
                if snapshots:
                    result = tuple(self._cold[(strategy, trade_date)] for strategy in self._HISTORICAL_STRATEGIES)
                    for strategy in self._HISTORICAL_STRATEGIES:
                        dates = (*self._dates[strategy], trade_date)
                        self._dates[strategy] = tuple(dict.fromkeys(dates))
                else:
                    result = ()
            future.set_result(result)
            return result
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self._lock:
                self._inflight.pop(trade_date, None)

    def _load_complete_date(self, trade_date: str) -> tuple[RecommendationSnapshot, ...]:
        snapshots = tuple(self._archive.load_frozen(strategy, trade_date) for strategy in self._HISTORICAL_STRATEGIES)
        if any(snapshot is None for snapshot in snapshots):
            return ()
        return tuple(snapshot for snapshot in snapshots if snapshot is not None)

    def _discard_mismatched_overlay(self, snapshot: RecommendationSnapshot) -> None:
        stale = tuple(
            key
            for key, overlay in self._overlays.items()
            if key[0] is snapshot.strategy and overlay.snapshot_id != snapshot.snapshot_id
        )
        for key in stale:
            self._overlays.pop(key, None)

    def _fits(self, snapshot: RecommendationSnapshot) -> bool:
        return len(canonical_json_bytes(snapshot)) <= self._maximum_view_bytes


def _delivery_snapshot(snapshot: RecommendationSnapshot) -> RecommendationSnapshot:
    recommendations = tuple(
        replace(
            item,
            features=replace(
                item.features,
                values={},
                missing_fields=(),
                evidence=(),
                external_risk_facts=(),
                normalization={},
                missing_reasons={},
                board_population=None,
            ),
        )
        for item in snapshot.recommendations
    )
    return replace(
        snapshot,
        recommendations=recommendations,
        filter_reasons={},
        filter_details=(),
        metadata={},
        replay_input=None,
    )


__all__ = ["PublishedSnapshotIndex"]
