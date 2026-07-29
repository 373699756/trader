"""Bounded P6 recommendation read model for active recommendation dates."""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from dataclasses import replace

from trader.application.cache import canonical_json_bytes
from trader.application.ports.snapshots import SnapshotReaderPort
from trader.domain.recommendation.models import (
    LiveOverlay,
    RecommendationAction,
    RecommendationSnapshot,
    Strategy,
)


class PublishedSnapshotIndex:
    """Own the Web read path for at most the latest 20 recommendation dates."""

    _HISTORICAL_STRATEGIES = (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25)

    def __init__(
        self,
        archive: SnapshotReaderPort,
        *,
        resident_days: int = 20,
        maximum_view_bytes: int = 160 * 1024,
        maximum_long_view_bytes: int = 512 * 1024,
    ) -> None:
        if resident_days < 1 or maximum_view_bytes < 1 or maximum_long_view_bytes < 1:
            raise ValueError("published snapshot index limits must be positive")
        self._archive = archive
        self._resident_days = resident_days
        self._maximum_view_bytes = maximum_view_bytes
        self._maximum_long_view_bytes = maximum_long_view_bytes
        self._lock = threading.RLock()
        self._current: dict[Strategy, RecommendationSnapshot] = {}
        self._resident: dict[tuple[Strategy, str], RecommendationSnapshot] = {}
        self._dates: dict[Strategy, tuple[str, ...]] = {strategy: () for strategy in self._HISTORICAL_STRATEGIES}
        self._overlays: dict[tuple[Strategy, str], LiveOverlay] = {}
        self._last_oversize_rejection: dict[str, object] | None = None
        self._counters: dict[str, int] = {
            "published": 0,
            "resident_hits": 0,
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
                    for view_snapshot in accepted:
                        self._resident[(view_snapshot.strategy, trade_date)] = view_snapshot
        for strategy in self._HISTORICAL_STRATEGIES:
            strategy_dates = tuple(
                trade_date for trade_date in sorted(date_sets[strategy], reverse=True) if trade_date in resident_dates
            )
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
        view_snapshot = _view_snapshot(snapshot)
        with self._lock:
            actual_bytes = len(canonical_json_bytes(view_snapshot))
            limit_bytes = self._limit_for(view_snapshot.strategy)
            if actual_bytes > limit_bytes:
                self._counters["rejected_oversize_views"] += 1
                self._last_oversize_rejection = {
                    "strategy": view_snapshot.strategy.value,
                    "actual_bytes": actual_bytes,
                    "limit_bytes": limit_bytes,
                }
                raise ValueError(
                    f"{view_snapshot.strategy.value} P6 view exceeds byte limit: "
                    f"actual={actual_bytes}, limit={limit_bytes}"
                )
            current = self._current.get(view_snapshot.strategy)
            if current is not None and view_snapshot.trade_date < current.trade_date:
                self._record_committed_locked(snapshot, view_snapshot)
                return False
            if not self._accept_current_locked(view_snapshot):
                return False
            self._counters["published"] += 1
            self._record_committed_locked(snapshot, view_snapshot)
            return True

    def _record_committed_locked(
        self,
        snapshot: RecommendationSnapshot,
        view_snapshot: RecommendationSnapshot,
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
        for strategy in self._HISTORICAL_STRATEGIES:
            self._dates[strategy] = tuple(
                trade_date for trade_date in self._dates[strategy] if trade_date in allowed_dates
            )
        if snapshot.trade_date in allowed_dates:
            self._resident[key] = view_snapshot
        for resident_key in tuple(self._resident):
            if resident_key[1] not in allowed_dates:
                self._resident.pop(resident_key, None)
        for overlay_key in tuple(self._overlays):
            if overlay_key[0] in self._HISTORICAL_STRATEGIES and overlay_key[1] not in allowed_dates:
                self._overlays.pop(overlay_key, None)

    def _accept_current_locked(self, view_snapshot: RecommendationSnapshot) -> bool:
        current = self._current.get(view_snapshot.strategy)
        if current is None or view_snapshot.trade_date > current.trade_date:
            self._current[view_snapshot.strategy] = view_snapshot
            self._discard_mismatched_overlay(view_snapshot)
            return True
        if view_snapshot.trade_date < current.trade_date:
            return False
        if not current.frozen:
            self._current[view_snapshot.strategy] = view_snapshot
            self._discard_mismatched_overlay(view_snapshot)
            return True
        if not view_snapshot.frozen:
            self._counters["rejected_late_drafts"] += 1
            return False
        if current.snapshot_id != view_snapshot.snapshot_id or current != view_snapshot:
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
            if trade_date not in self._dates.get(strategy, ()):
                return None
            resident = self._resident.get(key)
            if resident is not None:
                self._counters["resident_hits"] += 1
                return resident
        return None

    def recommendation_dates(self, strategy: Strategy) -> Sequence[str]:
        with self._lock:
            return self._dates.get(strategy, ())

    def status(self) -> Mapping[str, object]:
        with self._lock:
            return {
                "current_views": len(self._current),
                "resident_views": len(self._resident),
                "maximum_views": 4 + self._resident_days * 3,
                "maximum_view_bytes": self._maximum_view_bytes,
                "maximum_view_bytes_by_strategy": {
                    Strategy.TODAY.value: self._maximum_view_bytes,
                    Strategy.TOMORROW.value: self._maximum_view_bytes,
                    Strategy.D25.value: self._maximum_view_bytes,
                    Strategy.LONG.value: self._maximum_long_view_bytes,
                },
                "last_oversize_rejection": (
                    dict(self._last_oversize_rejection) if self._last_oversize_rejection is not None else None
                ),
                **self._counters,
            }

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
            view_snapshot = _view_snapshot(snapshot)
            if self._fits(view_snapshot):
                accepted.append(view_snapshot)
            else:
                with self._lock:
                    self._counters["rejected_oversize_views"] += 1
                    self._last_oversize_rejection = {
                        "strategy": view_snapshot.strategy.value,
                        "actual_bytes": len(canonical_json_bytes(view_snapshot)),
                        "limit_bytes": self._limit_for(view_snapshot.strategy),
                    }
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
        return len(canonical_json_bytes(snapshot)) <= self._limit_for(snapshot.strategy)

    def _limit_for(self, strategy: Strategy) -> int:
        return self._maximum_long_view_bytes if strategy is Strategy.LONG else self._maximum_view_bytes


def _view_snapshot(snapshot: RecommendationSnapshot) -> RecommendationSnapshot:
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
        if not snapshot.frozen or item.action is RecommendationAction.EXECUTABLE
    )
    return replace(
        snapshot,
        recommendations=recommendations,
        filter_reasons={},
        filter_details=(),
        metadata=_view_metadata(snapshot.metadata),
        replay_input=None,
    )


def _view_metadata(metadata: Mapping[str, object]) -> dict[str, object]:
    view_metadata: dict[str, object] = {}
    diagnostics = metadata.get("selection_diagnostics")
    if isinstance(diagnostics, Mapping):
        view_metadata["selection_diagnostics"] = dict(diagnostics)
    long_groups = metadata.get("long_groups")
    if isinstance(long_groups, (tuple, list)):
        view_metadata["long_groups"] = [dict(group) for group in long_groups if isinstance(group, Mapping)]
    score_status = metadata.get("score_status")
    if isinstance(score_status, str):
        view_metadata["score_status"] = score_status
    return view_metadata


__all__ = ["PublishedSnapshotIndex"]
