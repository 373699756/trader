"""One bounded history scan, immediately projected to every registered profile."""

from __future__ import annotations

import logging
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
from trader.infra.settings.models import HistoryPreloadSettings
from trader.recommendation.infra.market_data.aligned_history_bars import AlignedHistoryBars, BuildAlignedHistoryBars

if TYPE_CHECKING:
    from trader.recommendation.infra.market_data.published_history_cache import (
        PublishedHistoryCache,
        PublishedHistoryEntry,
    )


_LOGGER = logging.getLogger(__name__)


@dataclass
class _PreloadProgress:
    cancelled: Callable[[], bool]
    monotonic: Callable[[], float]
    started: float
    warn_after_seconds: float
    warned: bool = False

    def check(self) -> None:
        if self.cancelled():
            raise RuntimeError("history_preload_cancelled")
        elapsed = self.monotonic() - self.started
        if not self.warned and elapsed >= self.warn_after_seconds:
            self.warned = True
            _LOGGER.warning("历史预读已耗时 %.3f 秒，超过提示阈值 %.3f 秒，继续读取", elapsed, self.warn_after_seconds)


@dataclass(frozen=True)
class HistoryProjectionTimings:
    read_seconds: float
    alignment_seconds: float
    derivation_seconds: float
    publication_seconds: float
    extension_requests: int
    extension_rows: int


class SharedHistoryProjection:
    def __init__(
        self,
        history: ReadPublishedHistoryUseCase,
        *,
        monotonic: Callable[[], float],
        settings: HistoryPreloadSettings,
        reserve_bytes: Callable[[int], bool] | None = None,
    ) -> None:
        self._history = history
        self._monotonic = monotonic
        self._settings = settings
        self._reserve_bytes = reserve_bytes
        self._lock = threading.Lock()
        self._profiles: list[PublishedHistoryCache] = []
        self._initialized = False
        self._scan_count = 0
        self._window_count = 0
        self._read_seconds = self._alignment_seconds = self._derivation_seconds = self._publication_seconds = 0.0
        self._extension_requests = self._extension_rows = 0

    @property
    def timings(self) -> HistoryProjectionTimings:
        return HistoryProjectionTimings(
            self._read_seconds,
            self._alignment_seconds,
            self._derivation_seconds,
            self._publication_seconds,
            self._extension_requests,
            self._extension_rows,
        )

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
            progress = _PreloadProgress(cancelled, self._monotonic, self._monotonic(), self._settings.warn_after_seconds)
            if not cancelled():
                for profile in self._profiles:
                    profile.initialize_outcome_projection()
            try:
                self._load_projection(progress)
            except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
                self._record_failure(exc)
            finally:
                _LOGGER.info("历史预读结束，耗时 %.3f 秒", self._monotonic() - progress.started)
            for profile in self._profiles:
                profile.record_projection_observation()

    def _record_failure(self, exc: Exception) -> None:
        declared = {
            "history_snapshot_unavailable",
            "history_snapshot_changed",
            "history_preload_capacity",
            "history_preload_cancelled",
        }
        reason = str(exc) if str(exc) in declared else type(exc).__name__
        for profile in self._profiles:
            profile.fail_history_projection(reason)

    def _load_projection(self, progress: _PreloadProgress) -> None:
        progress.check()
        manifest = self._history.manifest()
        if manifest is None:
            raise RuntimeError("history_snapshot_unavailable")
        for profile in self._profiles:
            profile.bind_outcome_manifest(manifest)
            profile.record_maintenance(
                "loading", stage="reading_published_snapshot", total_units=len(manifest.universe_codes)
            )
        entries = self._scan(manifest, progress)
        confirmed = self._history.manifest()
        if confirmed is None or confirmed.snapshot_hash != manifest.snapshot_hash:
            raise RuntimeError("history_snapshot_changed")
        for profile in self._profiles:
            profile.record_maintenance("loading", stage="publishing_history_projection")
        started = self._monotonic()
        try:
            self._publish(manifest, entries, progress)
        finally:
            self._publication_seconds += self._monotonic() - started

    def _publish(
        self,
        manifest: PublishedHistoryManifest,
        entries: tuple[dict[str, PublishedHistoryEntry], ...],
        progress: _PreloadProgress,
    ) -> None:
        shared_bytes = 0
        if entries:
            for index, (code, entry) in enumerate(entries[0].items()):
                if index % 64 == 0:
                    progress.check()
                shared_bytes += len(CanonicalJsonBytes((code, entry.bars)))
        if self._reserve_bytes is not None and not self._reserve_bytes(shared_bytes):
            raise RuntimeError("history_preload_capacity")
        published = False
        try:
            for profile, values in zip(self._profiles, entries, strict=True):
                published = (
                    profile.publish_history_projection(manifest, values, progress.check) or published
                )
        finally:
            if not published and self._reserve_bytes is not None:
                self._reserve_bytes(0)

    def _scan(
        self, manifest: PublishedHistoryManifest, progress: _PreloadProgress
    ) -> tuple[dict[str, PublishedHistoryEntry], ...]:
        sessions = min(max(profile.history_sessions for profile in self._profiles) + 1, len(manifest.calendar_dates))
        calendars = tuple(profile.projection_calendar() for profile in self._profiles)
        lookbacks = tuple(sorted({profile.history_sessions for profile in self._profiles}))
        results: tuple[dict[str, PublishedHistoryEntry], ...] = tuple({} for _profile in self._profiles)
        lifetimes = {code: (start, end) for code, start, end in manifest.listing_intervals}
        self._scan_count += 1
        windows = iter(self._history.iter_windows(manifest, sessions=sessions, check=progress.check))
        completed = 0
        while True:
            started = self._monotonic()
            try:
                window = next(windows)
            except StopIteration:
                break
            finally:
                self._read_seconds += self._monotonic() - started
            progress.check()
            started = self._monotonic()
            try:
                aligned = self._aligned(window, manifest, lifetimes.get(window.code), sessions, progress)
            finally:
                self._alignment_seconds += self._monotonic() - started
            self._window_count += 1
            started = self._monotonic()
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
            self._derivation_seconds += self._monotonic() - started
            completed += 1
            for profile in self._profiles:
                profile.record_maintenance(
                    "loading",
                    stage="reading_published_snapshot",
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
        progress: _PreloadProgress,
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
        retained = window
        while len(aligned.bars) < sessions and extended_sessions < maximum:
            progress.check()
            next_sessions = min(maximum, extended_sessions + 63)
            earlier_dates = manifest.calendar_dates[-next_sessions:-extended_sessions]
            self._extension_requests += 1
            extended = self._history.read_range(
                manifest, (window.code,), earlier_dates[0], earlier_dates[-1], check=progress.check
            )
            extended_sessions = next_sessions
            if extended:
                self._extension_rows += len(extended[0].cells)
                retained = PublishedHistoryWindow(window.code, (*extended[0].cells, *retained.cells))
            aligned = BuildAlignedHistoryBars(retained, manifest.calendar_dates[-extended_sessions:], lifetime)
        return aligned


__all__ = ["HistoryProjectionTimings", "SharedHistoryProjection"]
