"""Candidate-only recovery over an immutable, download-owned weekly base."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from trader.download.application.fetch_history_tail import FetchHistoryTailUseCase
from trader.download.application.read_published_history import ReadPublishedHistoryUseCase
from trader.download.domain.history_tail import HistoryTailRequest
from trader.download.domain.published_history import PublishedHistoryManifest
from trader.infra.market_data.history.history import DailyBar, PriceAdjustment
from trader.recommendation.application.ports.runtime import TradingCalendarUnavailableError
from trader.recommendation.application.runtime.schedule import SHANGHAI
from trader.recommendation.domain.market.history_tail import (
    HistoryQuality,
    HistoryTailPlan,
    plan_history_tail,
    validate_tail_overlap,
)
from trader.recommendation.infra.market_data.published_history_bars import daily_bar


@dataclass(frozen=True, slots=True)
class HistoryTailStatus:
    requested_count: int = 0
    dispatched_count: int = 0
    cache_hit_count: int = 0
    deferred_count: int = 0
    inflight_count: int = 0
    expected_date: date | None = None
    quality_counts: tuple[tuple[HistoryQuality, int], ...] = ()
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class _RequestIdentity:
    snapshot_hash: str
    expected_date: date
    sessions: int
    code: str


@dataclass(frozen=True, slots=True)
class _Result:
    bars: tuple[DailyBar, ...]
    quality: HistoryQuality
    expires_at: float


@dataclass(frozen=True, slots=True)
class HistoryTailDependencies:
    history: ReadPublishedHistoryUseCase
    supplier: FetchHistoryTailUseCase
    open_dates: Callable[[], tuple[date, ...]]
    wall_clock: Callable[[], datetime]
    cancel_requested: Callable[[], bool]
    monotonic: Callable[[], float] = time.monotonic


@dataclass(frozen=True, slots=True)
class _BatchSelection:
    pending: tuple[str, ...]
    selected: tuple[str, ...]
    cached_qualities: tuple[tuple[str, HistoryQuality], ...]


class CandidateHistoryTailRecovery:
    """Own only bounded ephemeral tails; never publish or write historical facts."""

    def __init__(
        self,
        dependencies: HistoryTailDependencies,
        *,
        batch_timeout_seconds: float = 12.0,
        max_batch_size: int = 120,
    ) -> None:
        if batch_timeout_seconds <= 0 or not 1 <= max_batch_size <= 120:
            raise ValueError("history_tail_limits_invalid")
        self._history = dependencies.history
        self._supplier = dependencies.supplier
        self._open_dates = dependencies.open_dates
        self._wall_clock = dependencies.wall_clock
        self._cancel_requested = dependencies.cancel_requested
        self._monotonic = dependencies.monotonic
        self._budget = batch_timeout_seconds
        self._max_batch_size = max_batch_size
        self._lock = threading.RLock()
        self._batch_lock = threading.Lock()
        self._recent: dict[_RequestIdentity, _Result] = {}
        self._attempt_order: dict[str, int] = {}
        self._sequence = 0
        self._status = HistoryTailStatus()
        self._calendar_dates: tuple[date, ...] = ()

    def plan(self, latest: date, observed_at: datetime) -> HistoryTailPlan:
        with self._lock:
            dates = self._calendar_dates
        return plan_history_tail(dates, latest, observed_at.astimezone(SHANGHAI).date())

    def cached(
        self, codes: Sequence[str], manifest: PublishedHistoryManifest, sessions: int, observed_at: datetime
    ) -> dict[str, tuple[DailyBar, ...]]:
        try:
            expected = self.plan(manifest.data_cutoff, observed_at).expected_date
        except (OSError, RuntimeError, ValueError):
            return {}
        now = self._monotonic()
        with self._lock:
            self._prune(manifest.snapshot_hash, now)
            return {
                code: result.bars
                for code in codes
                if (result := self._recent.get(_RequestIdentity(manifest.snapshot_hash, expected, sessions, code)))
                is not None
                and result.quality is HistoryQuality.FULL_HISTORY_READY
            }

    def recover(
        self,
        codes: Sequence[str],
        manifest: PublishedHistoryManifest,
        *,
        sessions: int,
        observed_at: datetime,
        deadline: datetime | None,
    ) -> Mapping[str, tuple[DailyBar, ...]]:
        requested = tuple(dict.fromkeys(codes))
        if not requested or not self._batch_lock.acquire(blocking=False):
            return self.cached(requested, manifest, sessions, observed_at)
        try:
            return self._recover_batch(requested, manifest, sessions, observed_at, deadline)
        finally:
            self._batch_lock.release()

    def _recover_batch(
        self,
        requested: tuple[str, ...],
        manifest: PublishedHistoryManifest,
        sessions: int,
        observed_at: datetime,
        deadline: datetime | None,
    ) -> Mapping[str, tuple[DailyBar, ...]]:
        started = self._monotonic()
        budget = self._budget
        if deadline is not None:
            budget = min(budget, max(0.0, (deadline - self._wall_clock()).total_seconds()))
        limit = started + budget
        try:
            if not self._cancel_requested() and self._monotonic() < limit:
                dates = self._open_dates()
                with self._lock:
                    self._calendar_dates = dates
            plan = self.plan(manifest.data_cutoff, observed_at)
        except (OSError, TradingCalendarUnavailableError, ValueError):
            with self._lock:
                self._status = HistoryTailStatus(
                    requested_count=len(requested),
                    deferred_count=len(requested),
                    last_error="history_calendar_unverifiable",
                )
            return {}
        results = self.cached(requested, manifest, sessions, observed_at)
        selection = self._select(requested, manifest.snapshot_hash, plan.expected_date, sessions)
        qualities = dict(selection.cached_qualities)
        dispatched = 0
        last_error: str | None = None
        for code in selection.selected:
            if self._cancel_requested() or self._monotonic() >= limit:
                break
            with self._lock:
                self._sequence += 1
                self._attempt_order[code] = self._sequence
                self._status = HistoryTailStatus(inflight_count=1, expected_date=plan.expected_date)
            dispatched += 1
            try:
                result = self._recover_one(code, manifest, sessions, observed_at, limit)
                if self._cancel_requested() or self._monotonic() >= limit:
                    raise TimeoutError("history_tail_deadline")
            except (OSError, RuntimeError, ValueError) as exc:
                last_error = "history_tail_deadline" if self._monotonic() >= limit else type(exc).__name__
                result = _Result((), HistoryQuality.TAIL_PENDING, self._monotonic() + 60.0)
            qualities[code] = result.quality
            with self._lock:
                self._recent[_RequestIdentity(manifest.snapshot_hash, plan.expected_date, sessions, code)] = result
            if result.bars:
                results[code] = result.bars
        for code in requested:
            qualities.setdefault(code, HistoryQuality.TAIL_PENDING)
        with self._lock:
            self._status = HistoryTailStatus(
                len(requested),
                dispatched,
                len(selection.cached_qualities),
                len(selection.pending) - dispatched,
                0,
                plan.expected_date,
                tuple((quality, sum(value is quality for value in qualities.values())) for quality in HistoryQuality),
                last_error,
            )
            self._prune(manifest.snapshot_hash, self._monotonic())
        return results

    def _select(self, requested: tuple[str, ...], snapshot_hash: str, expected: date, sessions: int) -> _BatchSelection:
        with self._lock:
            qualities = tuple(
                (code, result.quality)
                for code in requested
                if (result := self._recent.get(_RequestIdentity(snapshot_hash, expected, sessions, code))) is not None
            )
            cached_codes = {code for code, _ in qualities}
            pending = tuple(code for code in requested if code not in cached_codes)
            selected = tuple(
                sorted(pending, key=lambda code: (self._attempt_order.get(code, 0), code))[: self._max_batch_size]
            )
            return _BatchSelection(pending, selected, qualities)

    def _recover_one(
        self,
        code: str,
        manifest: PublishedHistoryManifest,
        sessions: int,
        observed_at: datetime,
        deadline: float,
    ) -> _Result:
        windows = self._history.read_windows(manifest, (code,), sessions=sessions)
        if not windows or not windows[0].revisions:
            return _Result((), HistoryQuality.HISTORY_UNAVAILABLE, self._monotonic() + 60.0)
        original = tuple(row.cell for row in windows[0].revisions)
        plan = self.plan(original[-1].trade_date, observed_at)
        if plan.quality is not HistoryQuality.TAIL_PENDING:
            return _Result((), plan.quality, self._monotonic() + 60.0)
        overlap = original[-3:]
        request = HistoryTailRequest(code, tuple(cell.trade_date for cell in overlap) + plan.missing_dates)
        if self._cancel_requested() or self._monotonic() >= deadline:
            raise TimeoutError("history_tail_deadline")
        tail = self._supplier.fetch(request, deadline=deadline)
        quality = (
            validate_tail_overlap(overlap, tail.cells)
            if tail.source == "baostock"
            else HistoryQuality.ADJUSTMENT_CONFLICT
        )
        if quality is not HistoryQuality.FULL_HISTORY_READY:
            return _Result((), quality, self._monotonic() + 60.0)
        merged = original + tuple(cell for cell in tail.cells if cell.trade_date > original[-1].trade_date)
        selected = merged[-sessions:]
        with self._lock:
            expected_dates = tuple(day for day in self._calendar_dates if day <= plan.expected_date)[-sessions:]
        bars = tuple(bar for cell in selected if (bar := daily_bar(cell, PriceAdjustment.QFQ)) is not None)
        if len(bars) != sessions or tuple(cell.trade_date for cell in selected) != expected_dates:
            return _Result((), HistoryQuality.HISTORY_UNAVAILABLE, self._monotonic() + 60.0)
        return _Result(bars, HistoryQuality.FULL_HISTORY_READY, self._monotonic() + 900.0)

    def _prune(self, snapshot_hash: str, now: float) -> None:
        self._recent = {
            key: item
            for key, item in self._recent.items()
            if key.snapshot_hash == snapshot_hash and item.expires_at > now
        }
        while len(self._recent) > 480:
            del self._recent[next(iter(self._recent))]
        if len(self._attempt_order) > 6000:
            active = {key.code for key in self._recent}
            self._attempt_order = {code: order for code, order in self._attempt_order.items() if code in active}

    def status(self) -> HistoryTailStatus:
        with self._lock:
            return self._status
