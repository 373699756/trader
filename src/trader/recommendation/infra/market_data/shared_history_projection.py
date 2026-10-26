"""One bounded history scan, immediately projected to every registered profile."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from trader.download.application.read_published_history import ReadPublishedHistoryUseCase
from trader.download.domain.published_history import PublishedHistoryManifest, PublishedHistoryWindow
from trader.infra.cache_contracts import CanonicalJsonBytes
from trader.infra.market_data.history.history import BuildHistoryContext
from trader.recommendation.infra.market_data.aligned_history_bars import AlignedHistoryBars, BuildAlignedHistoryBars

if TYPE_CHECKING:
    from trader.recommendation.infra.market_data.published_history_cache import (
        PublishedHistoryCache,
        PublishedHistoryEntry,
    )


@dataclass(frozen=True)
class _PreloadBudget:
    cancelled: Callable[[], bool]
    monotonic: Callable[[], float]
    deadline: float

    def require_time(self) -> None:
        if self.cancelled() or self.monotonic() >= self.deadline:
            raise RuntimeError("history_preload_cancelled" if self.cancelled() else "history_preload_deadline")


class SharedHistoryProjection:
    def __init__(
        self,
        history: ReadPublishedHistoryUseCase,
        *,
        monotonic: Callable[[], float],
        reserve_bytes: Callable[[int], bool] | None = None,
    ) -> None:
        self._history = history
        self._monotonic = monotonic
        self._reserve_bytes = reserve_bytes
        self._lock = threading.Lock()
        self._profiles: list[PublishedHistoryCache] = []
        self._initialized = False
        self._scan_count = 0
        self._window_count = 0

    @property
    def accounts_shared_bars(self) -> bool:
        return self._reserve_bytes is not None

    @property
    def is_loading(self) -> bool:
        return self._lock.locked()

    @property
    def scan_count(self) -> int:
        return self._scan_count

    @property
    def window_count(self) -> int:
        return self._window_count

    def register(self, profile: PublishedHistoryCache) -> None:
        with self._lock:
            if self._initialized:
                raise RuntimeError("history projection registration is closed")
            if profile not in self._profiles:
                self._profiles.append(profile)

    def initialize(self, cancelled: Callable[[], bool]) -> None:
        with self._lock:
            if self._initialized:
                return
            self._initialized = True
            budget = _PreloadBudget(cancelled, self._monotonic, self._monotonic() + 300.0)
            if not cancelled():
                for profile in self._profiles:
                    profile.initialize_outcome_projection()
            for _attempt in range(3):
                try:
                    self._load_projection(budget)
                    break
                except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
                    self._record_failure(exc)
                    if cancelled() or self._monotonic() >= budget.deadline:
                        break
            for profile in self._profiles:
                profile.record_projection_observation()

    def _record_failure(self, exc: Exception) -> None:
        declared = {
            "history_snapshot_unavailable",
            "history_snapshot_changed",
            "history_preload_capacity",
            "history_preload_cancelled",
            "history_preload_deadline",
        }
        reason = str(exc) if str(exc) in declared else type(exc).__name__
        for profile in self._profiles:
            profile.fail_history_projection(reason)

    def _load_projection(self, budget: _PreloadBudget) -> None:
        budget.require_time()
        manifest = self._history.manifest()
        if manifest is None:
            raise RuntimeError("history_snapshot_unavailable")
        for profile in self._profiles:
            profile.bind_outcome_manifest(manifest)
            profile.record_maintenance(
                "loading", stage="reading_active_snapshot", total_units=len(manifest.universe_codes)
            )
        entries = self._scan(manifest, budget)
        confirmed = self._history.manifest()
        if confirmed is None or confirmed.snapshot_hash != manifest.snapshot_hash:
            raise RuntimeError("history_snapshot_changed")
        self._publish(manifest, entries, budget)

    def _publish(
        self,
        manifest: PublishedHistoryManifest,
        entries: tuple[dict[str, PublishedHistoryEntry], ...],
        budget: _PreloadBudget,
    ) -> None:
        shared_bytes = (
            sum(len(CanonicalJsonBytes((code, entry.bars))) for code, entry in entries[0].items()) if entries else 0
        )
        if self._reserve_bytes is not None and not self._reserve_bytes(shared_bytes):
            raise RuntimeError("history_preload_capacity")
        published = False
        try:
            for profile, values in zip(self._profiles, entries, strict=True):
                published = (
                    profile.publish_history_projection(manifest, values, budget.cancelled, budget.deadline) or published
                )
        finally:
            if not published and self._reserve_bytes is not None:
                self._reserve_bytes(0)

    def _scan(
        self, manifest: PublishedHistoryManifest, budget: _PreloadBudget
    ) -> tuple[dict[str, PublishedHistoryEntry], ...]:
        sessions = min(max(profile.history_sessions for profile in self._profiles) + 1, len(manifest.calendar_dates))
        calendars = tuple(profile.projection_calendar() for profile in self._profiles)
        lookbacks = tuple(sorted({profile.history_sessions for profile in self._profiles}))
        results: tuple[dict[str, PublishedHistoryEntry], ...] = tuple({} for _profile in self._profiles)
        lifetimes = {code: (start, end) for code, start, end in manifest.listing_intervals}
        self._scan_count += 1
        for completed, window in enumerate(
            self._history.iter_windows(manifest, sessions=sessions, check=budget.require_time), start=1
        ):
            budget.require_time()
            aligned = self._aligned(window, manifest, lifetimes.get(window.code), sessions, budget)
            self._window_count += 1
            if aligned.bars:
                shared_bars = aligned.bars[-20:]
                contexts = {
                    lookback: BuildHistoryContext(aligned.bars[-lookback:], lookback_sessions=lookback)
                    for lookback in lookbacks
                }
                for profile, values, calendar in zip(self._profiles, results, calendars, strict=True):
                    values[window.code] = profile.project_history_window(
                        manifest, window.code, aligned, calendar, shared_bars, contexts[profile.history_sessions]
                    )
            for profile in self._profiles:
                profile.record_maintenance(
                    "loading",
                    stage="reading_active_snapshot",
                    completed_units=completed,
                    total_units=len(manifest.universe_codes),
                )
        return results

    def _aligned(
        self,
        window: PublishedHistoryWindow,
        manifest: PublishedHistoryManifest,
        lifetime: tuple[date, date | None] | None,
        sessions: int,
        budget: _PreloadBudget,
    ) -> AlignedHistoryBars:
        aligned = BuildAlignedHistoryBars(window, manifest.calendar_dates[-sessions:], lifetime)
        first = manifest.calendar_dates[-sessions]
        if len(aligned.bars) >= sessions or (lifetime is not None and lifetime[0] > first):
            return aligned
        if not all(
            cell.status in {"unknown_missing", "tencent_inferred_suspended", "supplier_marked_suspended"}
            for cell in window.cells
            if not aligned.bars or cell.trade_date < date.fromisoformat(aligned.bars[0].trade_date)
        ):
            return aligned
        maximum = min(2000, len(manifest.calendar_dates))
        extended_sessions = sessions
        while len(aligned.bars) < sessions and extended_sessions < maximum:
            budget.require_time()
            extended_sessions = min(maximum, extended_sessions + 63)
            extended = self._history.read_windows(
                manifest, (window.code,), sessions=extended_sessions, check=budget.require_time
            )
            if not extended:
                break
            aligned = BuildAlignedHistoryBars(extended[0], manifest.calendar_dates[-extended_sessions:], lifetime)
        return aligned


__all__ = ["SharedHistoryProjection"]
