"""Bounded batch orchestration for resumable CNInfo issuer announcement sync."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, wait
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from threading import Lock

import requests

from trader.infra.failures import AdapterFailureCode, ClassifyAdapterFailure
from trader.infra.workers import SubmitOrReject, WorkerExecutor
from trader.recommendation.application.ports.data_plane_records import DataPlaneUnavailableError
from trader.recommendation.infra.market_data.announcement_sync import (
    CNINFO_CURSOR_PREFIX,
    CNINFO_SOURCE,
    CninfoAnnouncementIncrementalSync,
    CninfoDataPlane,
    CninfoSyncResult,
)

_RESULT_DRAIN_SECONDS = 1.0
_PROGRESS_INTERVAL_SECONDS = 5.0


@dataclass(frozen=True)
class CninfoBatchSyncReport:
    changed_codes: tuple[str, ...]
    completed_codes: tuple[str, ...]
    attempted_codes: tuple[str, ...]
    deferred_codes: tuple[str, ...]
    failed_codes: tuple[str, ...]
    pages_fetched: int
    pages_checkpointed: int
    saved_announcements: int
    resumed_codes: tuple[str, ...]
    deadline_reached: bool
    request_seconds: float
    parse_seconds: float
    evidence_write_seconds: float
    cursor_write_seconds: float


class _CninfoBatchProgress:
    def __init__(
        self,
        total_codes: int,
        publish: Callable[[tuple[int, int, int, int, int, int]], None] | None,
    ) -> None:
        self._total_codes = total_codes
        self._publish = publish
        self._lock = Lock()
        self._pages_by_code: dict[str, tuple[int, int, int]] = {}
        self._processed = 0
        self._completed = 0
        self._last_publish = 0.0
        self._closed = False

    def checkpoint(self, code: str, counts: tuple[int, int, int]) -> None:
        with self._lock:
            if self._closed:
                return
            self._pages_by_code[code] = counts
            self._emit(force=False)

    def results(self, results: Mapping[str, CninfoSyncResult], failed: set[str]) -> None:
        with self._lock:
            if self._closed:
                return
            for code, result in results.items():
                self._pages_by_code[code] = (
                    result.pages_fetched,
                    result.pages_checkpointed,
                    result.saved_announcements,
                )
            self._processed = min(self._total_codes, len(results) + len(failed))
            self._completed = sum(result.history_complete for result in results.values())
            self._emit(force=True)

    def close(self) -> dict[str, tuple[int, int, int]]:
        with self._lock:
            self._closed = True
            return dict(self._pages_by_code)

    def _emit(self, *, force: bool) -> None:
        if self._publish is None:
            return
        now = time.monotonic()
        if not force and now - self._last_publish < _PROGRESS_INTERVAL_SECONDS:
            return
        self._last_publish = now
        pages = tuple(sum(counts[index] for counts in self._pages_by_code.values()) for index in range(3))
        self._publish((self._processed, self._total_codes, pages[0], pages[1], pages[2], self._completed))


class CninfoAnnouncementBatchSynchronizer:
    """Run bounded CNInfo code syncs and expose resumable aggregate progress."""

    def __init__(
        self,
        syncer: CninfoAnnouncementIncrementalSync,
        data_plane: CninfoDataPlane,
        worker_pool: WorkerExecutor,
        *,
        wall_clock: Callable[[], datetime],
        max_inflight: int = 3,
    ) -> None:
        self._syncer = syncer
        self._data_plane = data_plane
        self._worker_pool = worker_pool
        self._wall_clock = wall_clock
        self._max_inflight = max(1, max_inflight)

    def pending_codes(self, codes: Sequence[str], observed_at: datetime) -> tuple[str, ...]:
        try:
            records = self._data_plane.load_source_cursor_recent_records()
        except DataPlaneUnavailableError:
            return tuple(codes)
        completed = {
            record.cursor_name.removeprefix(CNINFO_CURSOR_PREFIX)
            for record in records
            if record.source == CNINFO_SOURCE
            and record.cursor_name.startswith(CNINFO_CURSOR_PREFIX)
            and record.observed_at.date() >= observed_at.date()
            and isinstance(record.payload, Mapping)
            and bool(record.payload.get("issuer_history_complete"))
        }
        return tuple(code for code in codes if code not in completed)

    def sync_report(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        deadline: datetime,
        progress: Callable[[tuple[int, int, int, int, int, int]], None] | None = None,
    ) -> CninfoBatchSyncReport:
        results, failed, pending, page_counts = self._collect_sync_results(codes, observed_at, deadline, progress)
        completed_codes = tuple(sorted(code for code, result in results.items() if result.history_complete))
        changed_codes = tuple(sorted(code for code, counts in page_counts.items() if counts[2] > 0))
        failed.update(code for code, result in results.items() if result.terminal_failure)
        deferred = tuple(sorted(set(codes) - set(completed_codes) - failed))
        return CninfoBatchSyncReport(
            changed_codes=changed_codes,
            completed_codes=completed_codes,
            attempted_codes=tuple(sorted(set(results) | failed | set(page_counts))),
            deferred_codes=deferred,
            failed_codes=tuple(sorted(failed)),
            pages_fetched=sum(counts[0] for counts in page_counts.values()),
            pages_checkpointed=sum(counts[1] for counts in page_counts.values()),
            saved_announcements=sum(counts[2] for counts in page_counts.values()),
            resumed_codes=tuple(sorted(code for code, result in results.items() if result.resumed)),
            deadline_reached=bool(pending) or self._wall_clock() >= deadline,
            request_seconds=sum(result.request_seconds for result in results.values()),
            parse_seconds=sum(result.parse_seconds for result in results.values()),
            evidence_write_seconds=sum(result.evidence_write_seconds for result in results.values()),
            cursor_write_seconds=sum(result.cursor_write_seconds for result in results.values()),
        )

    def _fill_sync_futures(
        self,
        codes: Sequence[str],
        next_index: int,
        observed_at: datetime,
        deadline: datetime,
        futures: dict[Future[CninfoSyncResult], str],
        progress: _CninfoBatchProgress,
    ) -> int:
        while next_index < len(codes) and len(futures) < self._max_inflight and self._wall_clock() < deadline:
            code = codes[next_index]
            future = SubmitOrReject(
                self._worker_pool,
                self._syncer.sync_code,
                code,
                observed_at,
                deadline=deadline,
                progress=partial(progress.checkpoint, code),
            )
            futures[future] = code
            next_index += 1
        return next_index

    def _collect_sync_results(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        deadline: datetime,
        progress: Callable[[tuple[int, int, int, int, int, int]], None] | None,
    ) -> tuple[dict[str, CninfoSyncResult], set[str], bool, dict[str, tuple[int, int, int]]]:
        futures: dict[Future[CninfoSyncResult], str] = {}
        tracker = _CninfoBatchProgress(len(codes), progress)
        next_index = self._fill_sync_futures(codes, 0, observed_at, deadline, futures, tracker)
        results: dict[str, CninfoSyncResult] = {}
        failed: set[str] = set()
        pending = set(futures)
        rate_limited = False
        while pending and not rate_limited:
            timeout = max(0.0, (deadline - self._wall_clock()).total_seconds())
            if timeout <= 0.0:
                break
            completed, pending = wait(pending, timeout=timeout, return_when=FIRST_COMPLETED)
            if not completed:
                break
            rate_limited = self._consume_sync_results(completed, futures, codes, results, failed)
            for future in completed:
                futures.pop(future, None)
            if not rate_limited:
                next_index = self._fill_sync_futures(codes, next_index, observed_at, deadline, futures, tracker)
                pending.update(futures)
            tracker.results(results, failed)
        if pending:
            completed, pending = wait(pending, timeout=_RESULT_DRAIN_SECONDS)
            self._consume_sync_results(completed, futures, codes, results, failed)
            tracker.results(results, failed)
        for future in pending:
            future.cancel()
        has_pending = bool(pending) or next_index < len(codes)
        page_counts = tracker.close()
        return results, failed, has_pending, page_counts

    @staticmethod
    def _consume_sync_results(
        completed: set[Future[CninfoSyncResult]],
        futures: Mapping[Future[CninfoSyncResult], str],
        codes: Sequence[str],
        results: dict[str, CninfoSyncResult],
        failed: set[str],
    ) -> bool:
        for future in completed:
            code = futures[future]
            try:
                results[code] = future.result()
            except (OSError, RuntimeError, TypeError, ValueError, requests.RequestException) as exc:
                failure = ClassifyAdapterFailure(exc, provider=CNINFO_SOURCE, operation="announcements")
                if failure.code is AdapterFailureCode.RATE_LIMITED:
                    failed.update(codes)
                    return True
                if not failure.retryable:
                    failed.add(code)
        return False


__all__ = ["CninfoAnnouncementBatchSynchronizer", "CninfoBatchSyncReport"]
