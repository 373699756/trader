"""Tencent-first bounded fetching with one durable final-price writer."""

from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, wait
from dataclasses import dataclass, replace
from datetime import date
from threading import Event

from trader.download.domain.baostock_daily import BaoStockDailyCell, BaoStockSecurity
from trader.download.domain.history_price_coverage import HistoryPriceCoverage
from trader.download.domain.history_supplier_failure import HistorySupplierPendingError
from trader.download.domain.history_sync import (
    HistoryPipelineProgress,
    HistoryRecoverySummary,
    HistorySupplierContext,
    HistorySyncConfiguration,
    HistorySyncProgress,
    HistorySyncProgressPort,
    HistorySyncProgressState,
    HistoryTencentSummary,
    HistoryTwoStageSupplier,
)
from trader.download.domain.history_trading_status import HistoryTradingFact
from trader.download.domain.published_history import PublishedHistoryWindow
from trader.download.infra.history_direct_prices import HistoryDirectPrices, HistoryPriceContext, HistoryPriceResult
from trader.download.infra.history_download_index import HistoryDownloadIndex, HistoryDownloadTask
from trader.download.infra.history_month_partition import HistoryMonthPartitionError
from trader.infra.workers import WorkerExecutor, submit_or_reject

_TERMINAL = frozenset(
    {"history_tail_raw_price_conflict", "history_recovery_anchor_missing", "supplier_daily_window_conflict"}
)
_FATAL = frozenset({"history_st_reference_conflict", "history_supplier_identity_conflict"})


def download_failure(exc: BaseException) -> str:
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
    cancel: Callable[[], bool]
    progress: HistorySyncProgressPort | None


@dataclass(frozen=True)
class _TencentResult:
    task: HistoryDownloadTask
    dates: tuple[date, ...]
    previous: tuple[BaoStockDailyCell, ...]
    window: PublishedHistoryWindow
    failure: str | None = None
    seconds: float = 0.0


@dataclass(frozen=True)
class _WriteResult:
    results: tuple[HistoryPriceResult, ...]
    seconds: float


class HistoryDownloadFlow:
    def __init__(self, resources: HistoryDownloadResources) -> None:
        self._resources = resources
        self._reused_codes: set[str] = set()
        self._stopped = Event()

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
        return self._resources.prices.prepare(
            security,
            task,
            window,
            context=HistoryPriceContext(
                set(self._resources.context.calendar.expected_dates(security)).issubset(task.dates), result.previous
            ),
        )

    def _commit(self, batch: tuple[_TencentResult, ...]) -> _WriteResult:
        started = time.monotonic()
        self._check()
        results = tuple(self._tencent_result(result) for result in batch)
        self._resources.prices.commit(results)
        for result in results:
            self._check()
            self._resources.index.finish(result.task, result.missing, result.failure)
        return _WriteResult(results, time.monotonic() - started)

    def _acknowledge(self, results: tuple[HistoryPriceResult, ...], completed: int, total: int) -> int:
        for result in results:
            completed += 1
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
        # Old completed requests may still have truncated history. Requalify gaps
        # with Tencent before allowing any BaoStock attempt, including on resume.
        jobs = deque(task for task in tasks if not task.tencent_done or task.code in gap_codes)
        if any(len(task.dates) > 32768 for task in jobs):
            raise ValueError("history_download_task_too_large")
        completed = sum(task.tencent_done and task.code not in gap_codes for task in tasks)
        self._reused_codes.update(task.code for task in tasks if task.tencent_done and task.code not in gap_codes)
        _TencentPipeline(self, jobs, completed, len(tasks)).run()
        finished = resources.index.tasks()
        gap_codes = resources.index.gap_codes()
        failed = sum(task.failure is not None for task in finished)
        partial = sum(task.failure is None and task.code in gap_codes for task in finished)
        pending = sum(task.failure == "tencent_history_pending" for task in finished)
        conflicts = sum(task.failure == "history_tail_raw_price_conflict" for task in finished)
        self._emit(
            HistorySyncProgress(
                "tencent_history",
                "completed",
                len(tasks),
                len(tasks),
                tencent_summary=HistoryTencentSummary(
                    len(tasks),
                    len(tasks) - failed - partial,
                    failed,
                    conflicts,
                    partial,
                    pending,
                    len(self._reused_codes),
                ),
            )
        )

    def _dispatch(
        self,
        jobs: deque[HistoryDownloadTask],
        futures: dict[Future[_TencentResult], HistoryDownloadTask],
        codes: int,
        cells: int,
    ) -> bool:
        resources = self._resources
        while jobs and len(futures) < resources.configuration.history_workers and codes < 64:
            self._check()
            if cells + len(jobs[0].dates) > 32768:
                break
            task = jobs.popleft()
            futures[submit_or_reject(resources.executor, self._fetch_task, task)] = task
            cells += len(task.dates)
            codes += 1
        return not jobs

    def _fetch_task(self, task: HistoryDownloadTask) -> _TencentResult:
        self._check()
        previous = self._resources.prices.read_for_download(task.code, task.dates)
        obtained = {cell.trade_date for cell in previous if cell.obtained}
        missing = set(task.dates) - obtained
        dates = tuple(sorted(missing | set(sorted(obtained)[-5:]))) if missing else ()
        empty = PublishedHistoryWindow(task.code, ())
        if not dates:
            return _TencentResult(task, (), previous, empty)
        started = time.monotonic()
        failure = None
        try:
            window = self._resources.supplier.fetch_tencent_window(self._security(task.code), dates)
        except HistorySupplierPendingError as exc:
            window, failure = exc.window, "tencent_history_pending"
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            failure = download_failure(exc)
            if failure in _FATAL:
                raise
            window = empty
        self._check()
        if window.code != task.code or (
            (window.cells or failure is None) and tuple(cell.trade_date for cell in window.cells) != dates
        ):
            raise RuntimeError("history_supplier_identity_conflict")
        return _TencentResult(task, dates, previous, window, failure, time.monotonic() - started)

    def _tencent_result(self, result: _TencentResult) -> HistoryPriceResult:
        task = result.task
        try:
            prepared = self._prepare(result)
            return replace(prepared, failure=result.failure) if prepared.missing else prepared
        except HistoryMonthPartitionError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            failure = download_failure(exc)
            if failure in _FATAL:
                raise
            previous = result.previous
            obtained = {cell.trade_date for cell in previous if cell.obtained}
            count = sum(cell.status == "complete" for cell in previous)
            suspended = sum(cell.status == "supplier_marked_suspended" for cell in previous)
            return HistoryPriceResult(
                task,
                (),
                tuple(day for day in task.dates if day not in obtained),
                failure,
                HistoryPriceCoverage(len(task.dates), count, count, count, suspended),
            )

    def baostock(self) -> None:
        resources = self._resources
        gap_codes = resources.index.gap_codes()
        tasks = tuple(
            sorted(
                (task for task in resources.index.tasks() if task.code in gap_codes and task.failure is None),
                key=lambda task: task.code,
            )
        )
        succeeded = failed = 0
        try:
            for position, task in enumerate(tasks):
                self._check()
                if self._recover(task, position, len(tasks)):
                    succeeded += 1
                else:
                    failed += 1
        finally:
            remaining = len(tasks) - succeeded - failed
            state: HistorySyncProgressState = (
                "completed" if not remaining else "cancelled" if resources.cancel() else "failed"
            )
            self._emit(
                HistorySyncProgress(
                    "baostock_gap_fill",
                    state,
                    succeeded + failed,
                    len(tasks),
                    recovery_summary=HistoryRecoverySummary(succeeded, failed, remaining),
                )
            )

    def _recover(self, task: HistoryDownloadTask, position: int, total: int) -> bool:
        resources = self._resources
        if task.failure in _FATAL:
            raise RuntimeError(task.failure)
        if task.failure in _TERMINAL:
            return False
        previous = resources.prices.read(task.code, task.dates)
        obtained = {cell.trade_date for cell in previous if cell.obtained}
        actual_missing = tuple(day for day in task.dates if day not in obtained)
        resources.index.finish(task, actual_missing, task.failure if actual_missing else None)
        if not actual_missing:
            return True
        for _ in range(task.attempts, 3):
            self._check()
            previous = resources.prices.read(task.code, task.dates)
            obtained = {cell.trade_date for cell in previous if cell.obtained}
            missing = tuple(day for day in task.dates if day not in obtained)
            if not missing:
                resources.index.finish(task, (), None)
                return True
            anchors = tuple(sorted(cell.trade_date for cell in previous if cell.status == "complete"))[-5:]
            dates = tuple(sorted(set(missing[: 2000 - len(anchors)]) | set(anchors)))
            attempt = resources.index.begin_attempt(task.code)
            self._emit(
                HistorySyncProgress(
                    "baostock_gap_fill",
                    "started",
                    position,
                    total,
                    task.code,
                    attempt=attempt,
                    max_attempts=3,
                    supplier_source="baostock",
                    requested_sessions=len(dates),
                )
            )
            result = self._recovery_result(task, dates, missing)
            self._check()
            resources.prices.commit((result,))
            failure = result.failure or ("history_recovery_incomplete" if result.missing else None)
            resources.index.finish(task, result.missing, failure)
            self._emit(
                HistorySyncProgress(
                    "baostock_gap_fill",
                    "failed" if failure else "completed",
                    position + 1,
                    total,
                    task.code,
                    attempt=attempt,
                    max_attempts=3,
                    supplier_source="baostock",
                    requested_sessions=len(dates),
                    failure_reason=failure,
                    price_coverage=self._coverage(result),
                )
            )
            if failure in _FATAL:
                raise RuntimeError(failure)
            if failure is None or failure in _TERMINAL:
                break
        return not resources.index.gaps(task.code)

    def _recovery_result(
        self,
        task: HistoryDownloadTask,
        dates: tuple[date, ...],
        missing: tuple[date, ...],
    ) -> HistoryPriceResult:
        security = self._security(task.code)
        suspended: tuple[BaoStockDailyCell, ...] = ()
        try:
            requested = tuple(day for day in dates if day in missing)
            facts = self._verified_trading_facts(security, requested)
            unverified = len(facts) != len(requested)
            suspended = tuple(fact.suspended_cell() for fact in facts if fact.status == "suspended")
            trading = {fact.trade_date for fact in facts if fact.status == "trading"}
            if not trading:
                result = self._prepare_recovery(task, suspended)
                return replace(result, failure="history_trading_status_unverified") if unverified else result
            price_dates = tuple(day for day in dates if day in trading or day not in missing)
            cells = self._verified_recovery_prices(security, price_dates)
            result = self._prepare_recovery(task, tuple(sorted((*suspended, *cells), key=lambda cell: cell.trade_date)))
            return replace(result, failure="history_trading_status_unverified") if unverified else result
        except HistoryMonthPartitionError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            failure = download_failure(exc)
            if failure in _FATAL:
                raise
            result = self._prepare_recovery(task, suspended)
            return replace(result, failure=failure) if result.missing else result

    def _verified_trading_facts(
        self, security: BaoStockSecurity, dates: tuple[date, ...]
    ) -> tuple[HistoryTradingFact, ...]:
        facts = self._resources.supplier.fetch_trading_facts(security, dates)
        if any(fact.code != security.code or fact.trade_date not in dates for fact in facts):
            raise RuntimeError("history_supplier_identity_conflict")
        if len({fact.trade_date for fact in facts}) != len(facts):
            raise RuntimeError("supplier_daily_window_conflict")
        if any(fact.is_st for fact in facts):
            raise RuntimeError("history_st_reference_conflict")
        return facts

    def _verified_recovery_prices(
        self, security: BaoStockSecurity, dates: tuple[date, ...]
    ) -> tuple[BaoStockDailyCell, ...]:
        downloaded = self._resources.supplier.fetch_baostock_prices(security, dates)
        batch = downloaded.batch
        if batch.code != security.code or any(fact.code != security.code for fact in downloaded.daily_facts):
            raise RuntimeError("history_supplier_identity_conflict")
        if any(fact.is_st for fact in downloaded.daily_facts):
            raise RuntimeError("history_st_reference_conflict")
        if tuple(cell.trade_date for cell in batch.cells) != dates or any(
            (batch.duplicate_rows, batch.out_of_window_rows, batch.future_rows)
        ):
            raise RuntimeError("supplier_daily_window_conflict")
        return batch.cells

    def _prepare_recovery(self, task: HistoryDownloadTask, cells: tuple[BaoStockDailyCell, ...]) -> HistoryPriceResult:
        resources = self._resources
        security = self._security(task.code)
        return resources.prices.prepare(
            security,
            task,
            PublishedHistoryWindow(task.code, ()),
            cells,
            context=HistoryPriceContext(set(resources.context.calendar.expected_dates(security)).issubset(task.dates)),
        )


class _TencentPipeline:
    """One phase's bounded in-flight payloads and durable write acknowledgements."""

    def __init__(self, flow: HistoryDownloadFlow, jobs: deque[HistoryDownloadTask], completed: int, total: int) -> None:
        self._flow = flow
        self._jobs = jobs
        self._completed = completed
        self._total = total
        self._downloaded = completed
        self._futures: dict[Future[_TencentResult], HistoryDownloadTask] = {}
        self._buffered: list[_TencentResult] = []
        self._writing: Future[_WriteResult] | None = None
        self._writing_cells = self._writing_codes = 0
        self._download_seconds = self._write_seconds = self._backpressure_seconds = 0.0
        self._exhausted = False
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
            self._report()
        finally:
            self._cancel_pending()

    def _acknowledge(self) -> None:
        if self._writing is None or not self._writing.done():
            return
        saved = self._writing.result()
        self._completed = self._flow._acknowledge(saved.results, self._completed, self._total)
        self._write_seconds += saved.seconds
        self._writing = None
        self._writing_cells = self._writing_codes = 0

    def _collect(self) -> None:
        for future in tuple(self._futures):
            if not future.done():
                continue
            result = future.result()
            del self._futures[future]
            self._buffered.append(result)
            self._downloaded += 1
            self._download_seconds += result.seconds
            if not result.dates:
                self._flow._reused_codes.add(result.task.code)

    def _dispatch(self) -> bool:
        reserved = self._writing_cells + sum(len(item.task.dates) for item in self._buffered)
        reserved += sum(len(task.dates) for task in self._futures.values())
        codes = self._writing_codes + len(self._buffered) + len(self._futures)
        next_cells = len(self._jobs[0].dates) if self._jobs else 0
        blocked = bool(self._jobs) and (codes >= 64 or reserved + next_cells > 32768)
        if not self._exhausted and not blocked:
            self._exhausted = self._flow._dispatch(self._jobs, self._futures, codes, reserved)
        return blocked

    def _flush(self) -> None:
        if not self._buffered or self._writing is not None:
            return
        cells = sum(len(item.task.dates) for item in self._buffered)
        if len(self._buffered) < 16 and cells < 8192 and time.monotonic() - self._last_commit < 5 and self._futures:
            return
        batch = tuple(self._buffered)
        self._buffered.clear()
        self._writing_cells, self._writing_codes = cells, len(batch)
        self._writing = submit_or_reject(self._flow._resources.writer, self._flow._commit, batch)
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
                pipeline=HistoryPipelineProgress(
                    self._downloaded,
                    self._writing_codes + len(self._buffered),
                    self._download_seconds,
                    self._write_seconds,
                    self._backpressure_seconds,
                ),
            )
        )
        self._last_report = time.monotonic()

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


__all__ = ["HistoryDownloadFlow", "HistoryDownloadResources", "download_failure"]
