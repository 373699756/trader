"""Tencent-first bounded fetching with one durable final-price writer."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, wait
from dataclasses import dataclass
from datetime import date

from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.history_sync import (
    HistoryRecoverySummary,
    HistorySupplierContext,
    HistorySyncConfiguration,
    HistorySyncProgress,
    HistorySyncProgressPort,
    HistorySyncProgressState,
    HistoryTencentSummary,
    HistoryTwoStageSupplier,
)
from trader.download.domain.published_history import PublishedHistoryWindow
from trader.download.infra.history_direct_prices import HistoryDirectPrices, HistoryPriceResult
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
    cancel: Callable[[], bool]
    progress: HistorySyncProgressPort | None


@dataclass(frozen=True)
class _TencentJob:
    task: HistoryDownloadTask
    dates: tuple[date, ...]


class HistoryDownloadFlow:
    def __init__(self, resources: HistoryDownloadResources) -> None:
        self._resources = resources

    def _check(self) -> None:
        if self._resources.cancel():
            raise KeyboardInterrupt

    def _emit(self, value: HistorySyncProgress) -> None:
        if self._resources.progress is not None:
            try:
                self._resources.progress.publish(value)
            except OSError:
                pass

    def _security(self, code: str) -> BaoStockSecurity:
        return next(item for item in self._resources.context.universe if item.code == code)

    def _prepare(self, task: HistoryDownloadTask, window: PublishedHistoryWindow) -> HistoryPriceResult:
        security = self._security(task.code)
        return self._resources.prices.prepare(
            security,
            task,
            window,
            full_window=set(self._resources.context.calendar.expected_dates(security)).issubset(task.dates),
        )

    def _commit(self, results: list[HistoryPriceResult], completed: int, total: int) -> int:
        self._check()
        self._resources.prices.commit(tuple(results))
        for result in results:
            self._resources.index.finish(result.task, result.missing, result.failure)
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
                )
            )
        results.clear()
        return completed

    def tencent(self) -> None:
        resources = self._resources
        tasks = resources.index.tasks()
        jobs = iter(task for task in tasks if not task.tencent_done)
        completed = sum(task.tencent_done for task in tasks)
        futures: dict[Future[PublishedHistoryWindow], _TencentJob] = {}
        buffered: list[HistoryPriceResult] = []
        exhausted = False
        last_commit = time.monotonic()
        try:
            while not exhausted or futures:
                self._check()
                if not exhausted:
                    exhausted = self._dispatch(jobs, futures, buffered, completed, len(tasks))
                if futures:
                    done, _ = wait(futures, timeout=5, return_when=FIRST_COMPLETED)
                    self._check()
                    if not done:
                        self._emit(HistorySyncProgress("tencent_history", "waiting", completed, len(tasks)))
                    for future in done:
                        job = futures.pop(future)
                        result = self._tencent_result(future, job)
                        buffered.append(result)
                if buffered and (
                    len(buffered) >= 64
                    or sum(len(item.revisions) for item in buffered) >= 32768
                    or time.monotonic() - last_commit >= 5
                    or not futures
                ):
                    completed = self._commit(buffered, completed, len(tasks))
                    last_commit = time.monotonic()
            if buffered:
                self._commit(buffered, completed, len(tasks))
        finally:
            for future in futures:
                future.cancel()
        finished = resources.index.tasks()
        failed = sum(task.failure is not None for task in finished)
        conflicts = sum(task.failure == "history_tail_raw_price_conflict" for task in finished)
        self._emit(
            HistorySyncProgress(
                "tencent_history",
                "completed",
                len(tasks),
                len(tasks),
                tencent_summary=HistoryTencentSummary(len(tasks), len(tasks) - failed, failed, conflicts),
            )
        )

    def _dispatch(
        self,
        jobs: Iterator[HistoryDownloadTask],
        futures: dict[Future[PublishedHistoryWindow], _TencentJob],
        buffered: list[HistoryPriceResult],
        completed: int,
        total: int,
    ) -> bool:
        resources = self._resources
        while len(futures) < resources.configuration.history_workers and len(buffered) < 64:
            task = next(jobs, None)
            if task is None:
                return True
            previous = resources.prices.read(task.code, task.dates)
            if not task.dates or sum(cell.obtained for cell in previous) == len(task.dates):
                buffered.append(HistoryPriceResult(task, (), (), None))
                continue
            obtained = {cell.trade_date for cell in previous if cell.obtained}
            missing = set(task.dates) - obtained
            dates = tuple(sorted(missing | set(sorted(obtained)[-5:])))
            self._emit(
                HistorySyncProgress(
                    "tencent_history",
                    "started",
                    completed,
                    total,
                    task.code,
                    supplier_source="tencent",
                    requested_sessions=min(2000, len(dates)),
                )
            )
            futures[
                submit_or_reject(
                    resources.executor, resources.supplier.fetch_tencent_window, self._security(task.code), dates
                )
            ] = _TencentJob(task, dates)
        return False

    def _tencent_result(self, future: Future[PublishedHistoryWindow], job: _TencentJob) -> HistoryPriceResult:
        task = job.task
        try:
            window = future.result()
            if window.code != task.code or tuple(cell.trade_date for cell in window.cells) != job.dates:
                raise RuntimeError("history_supplier_identity_conflict")
            return self._prepare(task, window)
        except HistoryMonthPartitionError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            failure = download_failure(exc)
            if failure in _FATAL:
                raise
            previous = self._resources.prices.read(task.code, task.dates)
            obtained = {cell.trade_date for cell in previous if cell.obtained}
            return HistoryPriceResult(task, (), tuple(day for day in task.dates if day not in obtained), failure)

    def baostock(self) -> None:
        resources = self._resources
        tasks = tuple(
            sorted(
                (task for task in resources.index.tasks() if resources.index.gaps(task.code)),
                key=lambda task: (task.failure is None, task.code),
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
            anchors = tuple(sorted(obtained))[-5:]
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
        resources = self._resources
        security = self._security(task.code)
        try:
            downloaded = resources.supplier.fetch_baostock_prices(security, dates)
            batch = downloaded.batch
            if batch.code != task.code or any(fact.code != task.code for fact in downloaded.daily_facts):
                raise RuntimeError("history_supplier_identity_conflict")
            if any(fact.is_st for fact in downloaded.daily_facts):
                raise RuntimeError("history_st_reference_conflict")
            if tuple(cell.trade_date for cell in batch.cells) != dates or any(
                (batch.duplicate_rows, batch.out_of_window_rows, batch.future_rows)
            ):
                raise RuntimeError("supplier_daily_window_conflict")
            return resources.prices.prepare(
                security,
                task,
                PublishedHistoryWindow(task.code, ()),
                batch.cells,
                full_window=set(resources.context.calendar.expected_dates(security)).issubset(task.dates),
            )
        except HistoryMonthPartitionError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return HistoryPriceResult(task, (), missing, download_failure(exc))


__all__ = ["HistoryDownloadFlow", "HistoryDownloadResources", "download_failure"]
