"""Tencent-first bounded fetching with one durable final-price writer."""

from __future__ import annotations

import logging
import re
import sqlite3
import sys
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, wait
from dataclasses import dataclass, replace
from datetime import date
from threading import Event

from trader.download.domain.baostock_daily import BaoStockDailyCell, BaoStockSecurity
from trader.download.domain.history_price_coverage import HistoryPriceCoverage
from trader.download.domain.history_price_qualification import QualifyReplacementWindow
from trader.download.domain.history_supplier_failure import (
    HISTORY_SUPPLIER_PENDING_REASONS,
    HistorySupplierPendingError,
)
from trader.download.domain.history_sync import (
    HistoryPipelineProgress,
    HistorySupplierContext,
    HistorySyncConfiguration,
    HistorySyncProgress,
    HistorySyncProgressPort,
    HistoryTencentSummary,
    HistoryTwoStageSupplier,
)
from trader.download.domain.published_history import ProjectHistoryCell, PublishedHistoryWindow
from trader.download.infra.history_direct_prices import HistoryDirectPrices, HistoryPriceContext, HistoryPriceResult
from trader.download.infra.history_download_index import (
    HistoryDownloadCompletion,
    HistoryDownloadIndex,
    HistoryDownloadTask,
)
from trader.download.infra.history_prices import HistoryPricePartitionError
from trader.download.infra.history_price_repair import HistoryPriceRepair
from trader.download.infra.history_sync_failure import HistoryFailureDetail, HistorySyncFailureError
from trader.download.infra.history_write_tuning import HistoryWriteTuning
from trader.infra.workers import SubmitWithBackpressure, WorkerExecutor, WorkerResourceRejectedError

_FATAL = frozenset({"history_st_reference_conflict", "history_supplier_identity_conflict"})
_LOGGER = logging.getLogger(__name__)


def DownloadFailure(exc: BaseException) -> str:
    reason = str(exc).strip()
    return reason if re.fullmatch(r"[a-z0-9_]{1,64}", reason) else "history_supplier_failed"


@dataclass(frozen=True)
class HistoryDownloadResources:
    configuration: HistorySyncConfiguration
    context: HistorySupplierContext
    supplier: HistoryTwoStageSupplier
    prices: HistoryDirectPrices
    index: HistoryDownloadIndex
    executor: WorkerExecutor
    writer: WorkerExecutor
    tuning: HistoryWriteTuning
    cancel: Callable[[], bool]
    progress: HistorySyncProgressPort | None


@dataclass(frozen=True)
class _TencentResult:
    task: HistoryDownloadTask
    dates: tuple[date, ...]
    previous: tuple[BaoStockDailyCell, ...]
    window: PublishedHistoryWindow
    failure: str | None = None
    unusable_qfq_dates: tuple[date, ...] = ()


@dataclass(frozen=True)
class _PreparedResult:
    price: HistoryPriceResult
    dates: tuple[date, ...]
    seconds: float
    read_seconds: float
    validation_seconds: float


@dataclass(frozen=True)
class _WriteResult:
    results: tuple[HistoryPriceResult, ...]
    seconds: float


class HistoryDownloadFlow:
    def __init__(self, resources: HistoryDownloadResources) -> None:
        self._resources = resources
        self._reused_codes: set[str] = set()
        self._stopped = Event()
        self._baostock_codes = HistoryPriceRepair(resources.configuration.history_root).baostock_codes()
        self._recheck_codes = HistoryPriceRepair(resources.configuration.history_root).recheck_due(self._baostock_codes, resources.context.calendar.open_dates)

    def _check(self) -> None:
        if self._stopped.is_set() or self._resources.cancel():
            raise KeyboardInterrupt

    def _emit(self, value: HistorySyncProgress) -> None:
        if self._resources.progress is not None:
            try:
                self._resources.progress.publish(value)
            except OSError:
                pass

    def _security(self, code: str) -> BaoStockSecurity:
        return next(item for item in self._resources.context.universe if item.code == code)

    def _prepare(self, result: _TencentResult) -> HistoryPriceResult:
        task, window = result.task, result.window
        security = self._security(task.code)
        if result.dates and task.code in self._baostock_codes:
            if task.code not in self._recheck_codes:
                raise RuntimeError("price_source_basis_conflict")
            qualified = QualifyReplacementWindow(task.dates, PublishedHistoryWindow(task.code, tuple(ProjectHistoryCell(cell) for cell in result.previous)), window)
            return HistoryPriceResult(task, (), (), None, replacement=qualified)
        return self._resources.prices.prepare(
            security,
            task,
            window,
            context=HistoryPriceContext(
                set(self._resources.context.calendar.expected_dates(security)).issubset(task.dates), result.previous
            ),
        )

    def _commit(self, batch: tuple[_PreparedResult, ...]) -> _WriteResult:
        started = time.monotonic()
        self._check()
        results = tuple(item.price for item in batch)
        self._resources.prices.commit_pipeline(results)
        replacements = tuple(item for item in results if item.replacement is not None)
        if replacements:
            self._resources.prices.close_pipeline()
            for item in replacements:
                assert item.replacement is not None
                self._resources.prices.replace_window(self._security(item.task.code), item.replacement, "tencent")
        self._check()
        try:
            self._resources.index.finish(
                tuple(
                    HistoryDownloadCompletion(
                        result.task,
                        result.missing if result.failure is not None else (),
                        result.failure,
                        result.unusable_qfq_dates,
                    )
                    for result in results
                )
            )
        except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError) as exc:
            raise HistorySyncFailureError(exc, "confirming_download") from exc
        return _WriteResult(results, time.monotonic() - started)

    def _acknowledge(
        self,
        results: tuple[HistoryPriceResult, ...],
        completed: int,
        total: int,
        pipeline: HistoryPipelineProgress,
    ) -> int:
        for result in results:
            completed += 1
            security = self._security(result.task.code)
            self._emit(
                HistorySyncProgress(
                    "tencent_history",
                    "failed" if result.failure else "completed",
                    completed,
                    total,
                    result.task.code,
                    supplier_source="tencent" if result.task.dates else None,
                    requested_sessions=min(2000, len(result.task.dates)) if result.task.dates else None,
                    failure_reason=result.failure,
                    price_coverage=self._coverage(result),
                    pipeline=pipeline,
                    current_name=security.name,
                )
            )
        return completed

    def _coverage(self, result: HistoryPriceResult) -> HistoryPriceCoverage | None:
        if result.coverage is None:
            return None
        calendar = self._resources.context.calendar
        return replace(
            result.coverage,
            calendar_sessions=len(calendar.open_dates),
            not_applicable=len(calendar.open_dates) - len(calendar.expected_dates(self._security(result.task.code))),
        )

    def tencent(self) -> None:
        resources = self._resources
        tasks = resources.index.tasks()
        gap_codes = resources.index.gap_codes()
        retryable_codes = frozenset(resources.index.gaps_by_code(resources.context.calendar.open_dates))
        # Resume only retryable gaps; confirmed nonpositive qfq dates retain
        # their trading-session deferral even before initial publication.
        jobs = deque(task for task in tasks if not task.tencent_done or task.code in retryable_codes)
        if any(len(task.dates) > resources.tuning.max_cells for task in jobs):
            raise ValueError("history_download_task_too_large")
        completed = sum(task.tencent_done and task.code not in retryable_codes for task in tasks)
        self._reused_codes.update(task.code for task in tasks if task.tencent_done and task.code not in gap_codes)
        self._emit(HistorySyncProgress("tencent_history", "started", completed, len(tasks)))
        _TencentPipeline(self, jobs, completed, len(tasks)).run()
        self._report_tencent_summary(len(tasks))
        all_gaps = resources.index.gaps_by_code()
        retryable_gaps = resources.index.gaps_by_code(resources.context.calendar.open_dates)
        retry_jobs: deque[HistoryDownloadTask] = deque()
        for task in resources.index.tasks():
            if task.failure is None:
                continue
            allowed = frozenset(retryable_gaps.get(task.code, ()))
            if not any(day in allowed for day in task.dates):
                continue
            deferred = frozenset(all_gaps.get(task.code, ())) - allowed
            retry_jobs.append(replace(task, dates=tuple(day for day in task.dates if day not in deferred)))
        if retry_jobs:
            self._check()
            resources.prices.prepare_retry_pass()
            self._emit(HistorySyncProgress("tencent_history", "retrying", len(tasks) - len(retry_jobs), len(tasks)))
            _TencentPipeline(self, retry_jobs, len(tasks) - len(retry_jobs), len(tasks)).run()
            self._report_tencent_summary(len(tasks))

    def _report_tencent_summary(self, total: int) -> None:
        resources = self._resources
        finished = resources.index.tasks()
        gap_codes = resources.index.gap_codes()
        failed = sum(task.failure is not None for task in finished)
        partial = sum(task.failure is None and task.code in gap_codes for task in finished)
        pending = sum(task.failure in HISTORY_SUPPLIER_PENDING_REASONS for task in finished)
        conflicts = sum(task.failure == "history_tail_raw_price_conflict" for task in finished)
        # No request may also mean a deferred qfq gap, which remains partial.
        reused = sum(
            task.code in self._reused_codes and task.failure is None and task.code not in gap_codes for task in finished
        )
        self._emit(
            HistorySyncProgress(
                "tencent_history",
                "completed",
                total,
                total,
                tencent_summary=HistoryTencentSummary(
                    total,
                    total - failed - partial,
                    failed,
                    conflicts,
                    partial,
                    pending,
                    reused,
                ),
            )
        )

    def _dispatch(
        self,
        jobs: deque[HistoryDownloadTask],
        futures: dict[Future[_PreparedResult], HistoryDownloadTask],
        codes: int,
        cells: int,
    ) -> bool:
        resources = self._resources
        while jobs and len(futures) < resources.configuration.history_workers and codes < resources.tuning.max_codes:
            self._check()
            if cells + len(jobs[0].dates) > resources.tuning.max_cells:
                break
            task = jobs.popleft()
            future = SubmitWithBackpressure(resources.executor, self._fetch_task, task)
            futures[future] = task
            cells += len(task.dates)
            codes += 1
            if future.done() and not future.cancelled() and isinstance(future.exception(), WorkerResourceRejectedError):
                break
        return not jobs

    def _fetch_task(self, task: HistoryDownloadTask) -> _PreparedResult:
        self._check()
        uses_baostock = bool(task.dates) and task.code in self._baostock_codes
        if uses_baostock and task.code in self._recheck_codes:
            task = replace(task, dates=self._resources.context.calendar.expected_dates(self._security(task.code)))
        read_started = time.monotonic()
        previous = self._resources.prices.read_for_download(task.code, task.dates)
        read_seconds = time.monotonic() - read_started
        obtained = {cell.trade_date for cell in previous if cell.obtained}
        missing = set(task.dates) - obtained
        dates = tuple(sorted(missing | set(sorted(obtained)[-5:]))) if missing else ()
        if uses_baostock and missing and task.code in self._recheck_codes:
            dates = task.dates
            HistoryPriceRepair(self._resources.configuration.history_root).record_tencent_check(task.code, dates[-1])
        empty = PublishedHistoryWindow(task.code, ())
        if not dates:
            started = time.monotonic()
            price = self._tencent_result(_TencentResult(task, (), previous, empty))
            return _PreparedResult(price, (), 0.0, read_seconds, time.monotonic() - started)
        started = time.monotonic()
        failure: str | None = None
        unusable_qfq_dates: tuple[date, ...] = ()
        try:
            window = self._resources.supplier.fetch_tencent_window(self._security(task.code), dates)
        except HistorySupplierPendingError as exc:
            window, failure = exc.window, exc.reason
            unusable_qfq_dates = exc.unusable_qfq_dates
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            failure = DownloadFailure(exc)
            if failure in _FATAL:
                raise
            window = empty
        self._check()
        if window.code != task.code or (
            (window.cells or failure is None) and tuple(cell.trade_date for cell in window.cells) != dates
        ):
            raise RuntimeError("history_supplier_identity_conflict")
        download_seconds = time.monotonic() - started
        validation_started = time.monotonic()
        price = self._tencent_result(_TencentResult(task, dates, previous, window, failure, unusable_qfq_dates))
        return _PreparedResult(price, dates, download_seconds, read_seconds, time.monotonic() - validation_started)

    def _tencent_result(self, result: _TencentResult) -> HistoryPriceResult:
        task = result.task
        try:
            prepared = self._prepare(result)
            unusable = tuple(day for day in result.unusable_qfq_dates if day in prepared.missing)
            prepared = replace(prepared, unusable_qfq_dates=unusable)
            return (
                replace(prepared, failure=result.failure or "tencent_history_pending") if prepared.missing else prepared
            )
        except HistoryPricePartitionError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            failure = DownloadFailure(exc)
            if failure in _FATAL:
                raise
            previous = result.previous
            obtained = {cell.trade_date for cell in previous if cell.obtained}
            count = sum(cell.status == "complete" for cell in previous)
            suspended = sum(
                cell.status in {"supplier_marked_suspended", "tencent_inferred_suspended"} for cell in previous
            )
            return HistoryPriceResult(
                task,
                (),
                tuple(day for day in task.dates if day not in obtained),
                failure,
                HistoryPriceCoverage(
                    len(task.dates),
                    count,
                    count,
                    count,
                    suspended,
                    inferred_suspended=sum(cell.status == "tencent_inferred_suspended" for cell in previous),
                ),
            )


class _TencentPipeline:
    """One phase's bounded in-flight payloads and durable write acknowledgements."""

    def __init__(self, flow: HistoryDownloadFlow, jobs: deque[HistoryDownloadTask], completed: int, total: int) -> None:
        self._flow = flow
        self._jobs = jobs
        self._completed = completed
        self._total = total
        self._downloaded = completed
        self._futures: dict[Future[_PreparedResult], HistoryDownloadTask] = {}
        self._dispatched_at: dict[Future[_PreparedResult], float] = {}
        self._buffered: list[_PreparedResult] = []
        self._writing: Future[_WriteResult] | None = None
        self._writing_cells = self._writing_codes = 0
        self._download_seconds = self._write_seconds = self._backpressure_seconds = 0.0
        self._read_seconds = self._validation_seconds = 0.0
        self._write_started: float | None = None
        self._exhausted = False
        self._writer_closed = False
        self._last_commit = self._last_report = time.monotonic()

    def run(self) -> None:
        try:
            while not self._exhausted or self._futures or self._buffered or self._writing is not None:
                self._flow._check()
                self._acknowledge()
                self._collect()
                blocked = self._dispatch()
                self._flush()
                self._wait(blocked)
                if time.monotonic() - self._last_report >= 5:
                    self._report()
            self._close_writer()
        finally:
            original_failure = sys.exc_info()[1]
            self._cancel_pending()
            try:
                self._close_writer()
            except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError) as exc:
                if original_failure is None:
                    raise
                _LOGGER.error("history_writer_cleanup_failed diagnostic=%s", HistoryFailureDetail(exc))

    def _acknowledge(self) -> None:
        if self._writing is None or not self._writing.done():
            return
        try:
            saved = self._writing.result()
        except HistorySyncFailureError:
            raise
        except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError) as exc:
            raise HistorySyncFailureError(exc, "persisting_partitions") from exc
        self._write_seconds += saved.seconds
        self._writing = None
        self._write_started = None
        self._writing_cells = self._writing_codes = 0
        if self._exhausted and not self._jobs and not self._futures and not self._buffered:
            self._close_writer()
        self._completed = self._flow._acknowledge(
            saved.results,
            self._completed,
            self._total,
            self._pipeline_progress(),
        )
        self._last_report = time.monotonic()

    def _collect(self) -> None:
        for future in tuple(self._futures):
            if not future.done():
                continue
            try:
                result = future.result()
            except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError) as exc:
                raise HistorySyncFailureError(exc, "tencent_history", self._futures[future].code) from exc
            del self._futures[future]
            del self._dispatched_at[future]
            self._buffered.append(result)
            self._downloaded += 1
            self._download_seconds += result.seconds
            self._read_seconds += result.read_seconds
            self._validation_seconds += result.validation_seconds
            if not result.dates:
                self._flow._reused_codes.add(result.price.task.code)

    def _dispatch(self) -> bool:
        reserved = self._writing_cells + sum(len(item.price.task.dates) for item in self._buffered)
        reserved += sum(len(task.dates) for task in self._futures.values())
        codes = self._writing_codes + len(self._buffered) + len(self._futures)
        next_cells = len(self._jobs[0].dates) if self._jobs else 0
        tuning = self._flow._resources.tuning
        blocked = bool(self._jobs) and (codes >= tuning.max_codes or reserved + next_cells > tuning.max_cells)
        if not self._exhausted and not blocked:
            self._exhausted = self._flow._dispatch(self._jobs, self._futures, codes, reserved)
            now = time.monotonic()
            for future in self._futures:
                self._dispatched_at.setdefault(future, now)
        return blocked

    def _flush(self) -> None:
        if not self._buffered or self._writing is not None:
            return
        cells = sum(len(item.price.task.dates) for item in self._buffered)
        tuning = self._flow._resources.tuning
        if (
            len(self._buffered) < tuning.flush_codes
            and cells < tuning.flush_cells
            and time.monotonic() - self._last_commit < 5
            and self._futures
        ):
            return
        batch = tuple(self._buffered)
        self._buffered.clear()
        self._writing_cells, self._writing_codes = cells, len(batch)
        self._write_started = time.monotonic()
        self._writing = SubmitWithBackpressure(self._flow._resources.writer, self._flow._commit, batch)
        self._last_commit = time.monotonic()

    def _wait(self, blocked: bool) -> None:
        pending = (*self._futures, *((self._writing,) if self._writing is not None else ()))
        if not pending:
            return
        started = time.monotonic()
        wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
        if blocked:
            self._backpressure_seconds += time.monotonic() - started

    def _report(self) -> None:
        self._flow._emit(
            HistorySyncProgress(
                "tencent_history",
                "waiting",
                self._completed,
                self._total,
                pipeline=self._pipeline_progress(),
            )
        )
        self._last_report = time.monotonic()

    def _pipeline_progress(self) -> HistoryPipelineProgress:
        now = time.monotonic()
        return HistoryPipelineProgress(
            self._downloaded,
            self._writing_codes + len(self._buffered),
            self._download_seconds,
            self._write_seconds,
            self._backpressure_seconds,
            inflight_codes=len(self._futures),
            pending_codes=len(self._jobs),
            oldest_inflight_seconds=max((now - started for started in self._dispatched_at.values()), default=0.0),
            read_seconds=self._read_seconds,
            validation_seconds=self._validation_seconds,
            unconfirmed_write_seconds=0.0 if self._write_started is None else now - self._write_started,
        )

    def _cancel_pending(self) -> None:
        if self._futures or self._writing is not None:
            self._flow._stopped.set()
            self._flow._resources.prices.cancel_pending()
        for future in self._futures:
            future.cancel()
        if self._writing is not None:
            self._writing.cancel()
            # Keep the maintenance lock until the sole writer exits, even on error.
            try:
                self._writing.result()
            except BaseException:
                pass  # Preserve the original failure while joining cleanup.

    def _close_writer(self) -> None:
        if self._writer_closed:
            return
        started = time.monotonic()
        future = SubmitWithBackpressure(self._flow._resources.writer, self._flow._resources.prices.close_pipeline)
        future.result()
        self._write_seconds += time.monotonic() - started
        self._writer_closed = True


__all__ = ["HistoryDownloadFlow", "HistoryDownloadResources", "DownloadFailure"]
