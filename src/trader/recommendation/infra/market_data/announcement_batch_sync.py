"""Bounded batch orchestration for resumable CNInfo issuer announcement sync."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, wait
from dataclasses import dataclass
from datetime import datetime

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
        progress: Callable[[tuple[int, int, int, int, int]], None] | None = None,
    ) -> CninfoBatchSyncReport:
        results, failed, pending = self._collect_sync_results(codes, observed_at, deadline, progress)
        completed_codes = tuple(sorted(code for code, result in results.items() if result.history_complete))
        changed_codes = tuple(sorted(code for code, result in results.items() if result.saved_announcements > 0))
        failed.update(code for code, result in results.items() if result.terminal_failure)
        deferred = tuple(sorted(set(codes) - set(completed_codes) - failed))
        return CninfoBatchSyncReport(
            changed_codes=changed_codes,
            completed_codes=completed_codes,
            attempted_codes=tuple(sorted(set(results) | failed)),
            deferred_codes=deferred,
            failed_codes=tuple(sorted(failed)),
            pages_fetched=sum(result.pages_fetched for result in results.values()),
            pages_checkpointed=sum(result.pages_checkpointed for result in results.values()),
            saved_announcements=sum(result.saved_announcements for result in results.values()),
            resumed_codes=tuple(sorted(code for code, result in results.items() if result.resumed)),
            deadline_reached=bool(pending) or self._wall_clock() >= deadline,
        )

    def _fill_sync_futures(
        self,
        codes: Sequence[str],
        next_index: int,
        observed_at: datetime,
        deadline: datetime,
        futures: dict[Future[CninfoSyncResult], str],
    ) -> int:
        while next_index < len(codes) and len(futures) < self._max_inflight and self._wall_clock() < deadline:
            code = codes[next_index]
            future = SubmitOrReject(
                self._worker_pool,
                self._syncer.sync_code,
                code,
                observed_at,
                deadline=deadline,
            )
            futures[future] = code
            next_index += 1
        return next_index

    def _collect_sync_results(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        deadline: datetime,
        progress: Callable[[tuple[int, int, int, int, int]], None] | None,
    ) -> tuple[dict[str, CninfoSyncResult], set[str], bool]:
        futures: dict[Future[CninfoSyncResult], str] = {}
        next_index = self._fill_sync_futures(codes, 0, observed_at, deadline, futures)
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
                next_index = self._fill_sync_futures(codes, next_index, observed_at, deadline, futures)
                pending.update(futures)
            _publish_batch_progress(progress, codes, results, failed)
        if pending:
            completed, pending = wait(pending, timeout=_RESULT_DRAIN_SECONDS)
            self._consume_sync_results(completed, futures, codes, results, failed)
            _publish_batch_progress(progress, codes, results, failed)
        for future in pending:
            future.cancel()
        has_pending = bool(pending) or next_index < len(codes)
        return results, failed, has_pending

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


def _publish_batch_progress(
    progress: Callable[[tuple[int, int, int, int, int]], None] | None,
    codes: Sequence[str],
    results: Mapping[str, CninfoSyncResult],
    failed: set[str],
) -> None:
    if progress is None:
        return
    progress(
        (
            min(len(codes), len(results) + len(failed)),
            len(codes),
            sum(result.pages_fetched for result in results.values()),
            sum(result.pages_checkpointed for result in results.values()),
            sum(result.saved_announcements for result in results.values()),
        )
    )


__all__ = ["CninfoAnnouncementBatchSynchronizer", "CninfoBatchSyncReport"]
