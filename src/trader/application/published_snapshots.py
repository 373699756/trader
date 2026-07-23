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
            "rejected_late_drafts": 0,
            "rejected_frozen_replacements": 0,
            "rejected_oversize_views": 0,
        }

    def initialize(self) -> Mapping[str, int]:
        date_sets = {
            strategy: set(self._archive.recommendation_dates(strategy)) for strategy in self._HISTORICAL_STRATEGIES
        }
        resident_dates = sorted(set().union(*date_sets.values()), reverse=True)[: self._resident_days]
        loaded_dates = 0
        loaded_views = 0
        for trade_date in resident_dates:
            accepted = self._compact_fitting_views(self._load_available_date(trade_date, date_sets))
            if accepted:
                loaded_dates += 1
                loaded_views += len(accepted)
                with self._lock:
                    for delivery in accepted:
                        self._resident[(delivery.strategy, trade_date)] = delivery
        for strategy in self._HISTORICAL_STRATEGIES:
            strategy_dates = tuple(sorted(date_sets[strategy], reverse=True))
            with self._lock:
                self._dates[strategy] = strategy_dates
                latest = next(
                    (
                        self._resident[(strategy, trade_date)]
                        for trade_date in strategy_dates
                        if (strategy, trade_date) in self._resident
                    ),
                    None,
                )
            if latest is None:
                continue
            overlay = self._archive.load_live_overlay(strategy, latest.trade_date)
            with self._lock:
                self._current[strategy] = latest
                if overlay is not None and overlay.snapshot_id == latest.snapshot_id:
                    self._overlays[(strategy, latest.trade_date)] = overlay
        return {"resident_dates_preloaded": loaded_dates, "historical_views_preloaded": loaded_views}

    def publish(self, snapshot: RecommendationSnapshot) -> bool:
        delivery = _delivery_snapshot(snapshot)
        with self._lock:
            if not self._fits(delivery):
                self._counters["rejected_oversize_views"] += 1
                raise ValueError("P6 view exceeds the configured per-view byte limit")
            current = self._current.get(delivery.strategy)
            if current is not None and delivery.trade_date < current.trade_date:
                self._record_committed_locked(snapshot, delivery)
                return False
            if not self._accept_current_locked(delivery):
                return False
            self._counters["published"] += 1
            self._record_committed_locked(snapshot, delivery)
            return True

    def _record_committed_locked(
        self,
        snapshot: RecommendationSnapshot,
        delivery: RecommendationSnapshot,
    ) -> None:
        if not snapshot.frozen or snapshot.strategy not in self._HISTORICAL_STRATEGIES:
            return
        key = (snapshot.strategy, snapshot.trade_date)
        self._dates[snapshot.strategy] = tuple(
            sorted({snapshot.trade_date, *self._dates[snapshot.strategy]}, reverse=True)
        )
        allowed_dates = set(
            sorted(
                {trade_date for dates in self._dates.values() for trade_date in dates},
                reverse=True,
            )[: self._resident_days]
        )
        if snapshot.trade_date in allowed_dates:
            self._resident[key] = delivery
        else:
            self._cold[key] = delivery
            self._evict_cold_dates_locked()
        for resident_key in tuple(self._resident):
            if resident_key[1] not in allowed_dates:
                self._resident.pop(resident_key, None)

    def _accept_current_locked(self, delivery: RecommendationSnapshot) -> bool:
        current = self._current.get(delivery.strategy)
        if current is None or delivery.trade_date > current.trade_date:
            self._current[delivery.strategy] = delivery
            self._discard_mismatched_overlay(delivery)
            return True
        if delivery.trade_date < current.trade_date:
            return False
        if not current.frozen:
            self._current[delivery.strategy] = delivery
            self._discard_mismatched_overlay(delivery)
            return True
        if not delivery.frozen:
            self._counters["rejected_late_drafts"] += 1
            return False
        if current.snapshot_id != delivery.snapshot_id or current != delivery:
            self._counters["rejected_frozen_replacements"] += 1
            return False
        return True

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
        future, owner = self._cold_future(trade_date)
        if not owner:
            return future.result()
        try:
            result = self._load_cold_date(trade_date)
            future.set_result(result)
            return result
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self._lock:
                self._inflight.pop(trade_date, None)

    def _cold_future(self, trade_date: str) -> tuple[Future[tuple[RecommendationSnapshot, ...]], bool]:
        with self._lock:
            future = self._inflight.get(trade_date)
            owner = future is None
            if future is None:
                future = Future()
                self._inflight[trade_date] = future
                self._counters["cold_loads"] += 1
            else:
                self._counters["cold_coalesced"] += 1
        return future, owner

    def _load_cold_date(self, trade_date: str) -> tuple[RecommendationSnapshot, ...]:
        snapshots = self._load_available_date(trade_date)
        if not snapshots:
            with self._lock:
                self._counters["rejected_incomplete_dates"] += 1
            return ()
        deliveries = self._compact_fitting_views(snapshots)
        if not deliveries:
            return ()
        with self._lock:
            for delivery in deliveries:
                self._cold[(delivery.strategy, trade_date)] = delivery
            self._evict_cold_dates_locked()
            return tuple(
                self._cold[(strategy, trade_date)]
                for strategy in self._HISTORICAL_STRATEGIES
                if (strategy, trade_date) in self._cold
            )

    def _evict_cold_dates_locked(self) -> None:
        while len(self._cold) > self._cold_slots:
            oldest_date = next(iter(self._cold))[1]
            for strategy in self._HISTORICAL_STRATEGIES:
                self._cold.pop((strategy, oldest_date), None)

    def _load_available_date(
        self,
        trade_date: str,
        date_sets: Mapping[Strategy, set[str]] | None = None,
    ) -> tuple[RecommendationSnapshot, ...]:
        available = date_sets or {strategy: set(self._dates[strategy]) for strategy in self._HISTORICAL_STRATEGIES}
        return tuple(
            snapshot
            for strategy in self._HISTORICAL_STRATEGIES
            if trade_date in available[strategy]
            if (snapshot := self._archive.load_frozen(strategy, trade_date)) is not None
        )

    def _compact_fitting_views(
        self,
        snapshots: Sequence[RecommendationSnapshot],
    ) -> tuple[RecommendationSnapshot, ...]:
        accepted: list[RecommendationSnapshot] = []
        for snapshot in snapshots:
            delivery = _delivery_snapshot(snapshot)
            if self._fits(delivery):
                accepted.append(delivery)
            else:
                with self._lock:
                    self._counters["rejected_oversize_views"] += 1
        return tuple(accepted)

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
