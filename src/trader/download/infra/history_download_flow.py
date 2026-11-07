"""Tencent-first bounded fetching with one durable final-price writer."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy

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
from threading import Event, Lock

from trader.download.domain.baostock_daily import BaoStockDailyCell, BaoStockSecurity
from trader.download.domain.history_price_coverage import HistoryPriceCoverage
from trader.download.domain.history_price_qualification import HistoryQfqBasisError, QualifyReplacementWindow
from trader.download.domain.history_supplier_failure import (
    HISTORY_SUPPLIER_PENDING_REASONS,
    HistorySupplierPendingError,
)
from trader.download.domain.history_sync import (
    HistoryPipelineProgress,
    HistoryStockRequest,
    HistorySupplierContext,
    HistorySyncConfiguration,
    HistorySyncProgress,
    HistorySyncProgressPort,
    HistoryTencentSummary,
    HistoryTwoStageSupplier,
)
from trader.download.domain.published_history import ProjectHistoryCell, PublishedHistoryWindow
from trader.download.domain.price_review import PriceReviewBudget, PriceReviewBudgetExhausted
from trader.download.infra.history_direct_prices import HistoryDirectPrices, HistoryPriceContext, HistoryPriceResult
from trader.download.domain.history_download import HistoryDownloadCompletion, HistoryDownloadTask
from trader.download.infra.history_download_index import HistoryDownloadIndex
from trader.download.infra.history_prices import HistoryPricePartitionError
from trader.download.infra.history_price_repair import HistoryPriceRepair
from trader.download.infra.history_inferred_review import (
    HistoryInferredReviewIndex,
    HistoryInferredReviewTask,
    IsHistoryReviewBusy,
)
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
    review_confirmed: bool = False


@dataclass(frozen=True)
class _WriteResult:
    prepared: tuple[_PreparedResult, ...]
    seconds: float


@dataclass(frozen=True)
class HistoryInferredReviewResult:
    task: HistoryInferredReviewTask
    cells: tuple[BaoStockDailyCell, ...]
    price: HistoryPriceResult | None
    failure: str | None = None


class HistoryDownloadFlow:
    def __init__(
        self, resources: HistoryDownloadResources, *, sqlite_policy: SqliteIoPolicy, batched_price_reads: bool = False,
    ) -> None:
        self._sqlite_policy = sqlite_policy
        self._resources = resources
        self._batched_price_reads = batched_price_reads
        self._reused_codes: set[str] = set()
        self._window_cells: dict[str, int] = {}
        self._stopped = Event()
        self._write_lock = Lock()
        self._write_operation = "空闲"
        self._progress_lock = Lock()
        self._download_progress: HistorySyncProgress | None = None
        self._emit(HistorySyncProgress("initializing", "started", 0, 1, "repair_index"))
        self._baostock_codes = HistoryPriceRepair(
            resources.configuration.history_root, sqlite_policy=self._sqlite_policy,
        ).baostock_codes() & frozenset(item.code for item in resources.context.universe)
        self._recheck_codes = HistoryPriceRepair(resources.configuration.history_root, sqlite_policy=self._sqlite_policy).recheck_due(self._baostock_codes, resources.context.calendar.open_dates)
        self._emit(HistorySyncProgress("initializing", "completed", 1, 1, "repair_index"))

    def _check(self) -> None:
        if self._stopped.is_set() or self._resources.cancel():
            raise KeyboardInterrupt

    def _emit(self, value: HistorySyncProgress) -> None:
        if value.stage == "tencent_history" and value.request is None:
            with self._progress_lock:
                self._download_progress = value
        if self._resources.progress is not None:
            try:
                self._resources.progress.publish(value)
            except OSError:
                pass

    def _request_window(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> PublishedHistoryWindow:
        def report(state: str, failure: BaseException | None = None) -> None:
            with self._progress_lock:
                previous = self._download_progress
            if previous is None:
                return
            request = HistoryStockRequest(security.code, security.name, dates[0], dates[-1], "started" if state == "started" else "finished")
            self._emit(HistorySyncProgress(
                "tencent_history", "failed" if failure is not None else "waiting",
                previous.completed_units, previous.total_units, security.code,
                current_name=security.name, pipeline=previous.pipeline, request=request,
                supplier_source="tencent", requested_sessions=min(2000, len(dates)),
                requested_from=dates[0], requested_to=dates[-1],
                failure_reason=DownloadFailure(failure) if failure is not None else None,
                failure_detail=HistoryFailureDetail(failure)[:512] if failure is not None else None,
            ))

        report("started")
        try:
            result = self._resources.supplier.fetch_tencent_window(security, dates)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            report("finished", exc)
            raise
        except BaseException:
            report("finished")
            raise
        report("finished")
        return result

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
        full_window = set(self._resources.context.calendar.expected_dates(security)).issubset(task.dates)
        previous = {cell.trade_date: cell for cell in result.previous}
        if result.dates and result.previous and not full_window and any(
            cell.status == "complete" for cell in window.cells
        ) and not any(
            cell.qfq is not None and cell.qfq.trading_status == "trading"
            and (before := previous.get(cell.trade_date)) is not None
            and before.qfq is not None and before.qfq.trading_status == "trading"
            for cell in window.cells
        ):
            # Five tail anchors can all be suspended. A newly observed trading
            # price still needs a verified basis for the entire current window.
            raise HistoryQfqBasisError("history_qfq_basis_unverified")
        return self._resources.prices.prepare(
            security,
            task,
            window,
            context=HistoryPriceContext(
                full_window, result.previous
            ),
        )

    def _commit(self, batch: tuple[_PreparedResult, ...]) -> _WriteResult:
        started = time.monotonic()
        self._check()
        results = tuple(item.price for item in batch)
        with self._write_lock:
            self._write_operation = "价格写入"
        self._resources.prices.commit_pipeline(results)
        replacements = tuple(item for item in results if item.replacement is not None)
        if replacements:
            self._resources.prices.close_pipeline()
            for item in replacements:
                assert item.replacement is not None
                self._resources.prices.replace_window(self._security(item.task.code), item.replacement, "tencent")
        self._check()
        try:
            with self._write_lock:
                self._write_operation = "提交下载断点"
            self._resources.index.finish(
                tuple(
                    HistoryDownloadCompletion(
                        item.price.task,
                        item.price.missing if item.price.failure is not None else (),
                        item.price.failure,
                        item.price.unusable_qfq_dates,
                        item.price.inferred_suspended_dates,
                        item.dates if item.review_confirmed else (),
                    )
                    for item in batch
                )
            )
        except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError) as exc:
            raise HistorySyncFailureError(exc, "confirming_download") from exc
        with self._write_lock:
            self._write_operation = "空闲"
        return _WriteResult(batch, time.monotonic() - started)

    def _acknowledge(
        self,
        results: tuple[_PreparedResult, ...],
        completed: int,
        total: int,
        pipeline: HistoryPipelineProgress,
    ) -> int:
        for prepared in results:
            result = prepared.price
            completed += 1
            security = self._security(result.task.code)
            self._emit(
                HistorySyncProgress(
                    "tencent_history",
                    "failed" if result.failure else "completed",
                    completed,
                    total,
                    result.task.code,
                    supplier_source="tencent" if prepared.dates else None,
                    requested_sessions=min(2000, len(prepared.dates)) if prepared.dates else None,
                    failure_reason=result.failure,
                    failure_detail=result.diagnostic[:512] if result.failure and result.diagnostic else None,
                    requested_from=prepared.dates[0] if prepared.dates else None,
                    requested_to=prepared.dates[-1] if prepared.dates else None,
                    stock_download_seconds=prepared.seconds,
                    stock_read_seconds=prepared.read_seconds,
                    stock_validation_seconds=prepared.validation_seconds,
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
        total = len(resources.context.universe)
        self._emit(HistorySyncProgress("initializing", "started", 0, total, "reading_tasks"))
        tasks = resources.index.tasks(
            codes=frozenset(item.code for item in resources.context.universe),
            progress=lambda completed, count: self._emit(
                HistorySyncProgress("initializing", "waiting", completed, count, "reading_tasks")
            ),
        )
        self._emit(HistorySyncProgress("initializing", "completed", total, total, "reading_tasks"))
        self._emit(HistorySyncProgress("initializing", "started", 0, 1, "reading_gaps"))
        gap_codes = resources.index.gap_codes()
        retryable_codes = frozenset(resources.index.gaps_by_code(resources.context.calendar.open_dates))
        # Resume only retryable gaps; confirmed nonpositive qfq dates retain
        # their trading-session deferral even before initial publication.
        jobs = deque(task for task in tasks if not task.tencent_done or task.code in retryable_codes)
        if any(self._task_cells(task) > resources.tuning.max_cells for task in jobs):
            raise ValueError("history_download_task_too_large")
        completed = sum(task.tencent_done and task.code not in retryable_codes for task in tasks)
        self._reused_codes.update(task.code for task in tasks if task.tencent_done and task.code not in gap_codes)
        self._emit(HistorySyncProgress("initializing", "completed", 1, 1, "reading_gaps"))
        self._emit(HistorySyncProgress("tencent_history", "started", completed, len(tasks)))
        pipeline = _TencentPipeline(self, jobs, completed, len(tasks)).run()
        self._report_tencent_summary(len(tasks), pipeline)

    def prepare_inferred_reviews(
        self,
        tasks: tuple[HistoryInferredReviewTask, ...],
        budget: PriceReviewBudget,
        sequence: int,
    ) -> tuple[HistoryInferredReviewResult, ...]:
        """Prepare auxiliary facts without changing ordinary task/gap checkpoints."""
        resources = self._resources
        results: list[HistoryInferredReviewResult] = []
        repair = HistoryPriceRepair(
            resources.configuration.history_root,
            sqlite_policy=replace(
                self._sqlite_policy,
                connection_timeout_seconds=min(self._sqlite_policy.connection_timeout_seconds, 0.001),
            ),
        )
        for task in tasks:
            self._check()
            if not budget.reserve_code(task.code):
                continue
            previous: tuple[BaoStockDailyCell, ...] = ()
            try:
                previous = resources.prices.read_for_review(task.code, task.dates, budget)
                inferred = frozenset(cell.trade_date for cell in previous if cell.status == "tencent_inferred_suspended")
                if not inferred:
                    results.append(HistoryInferredReviewResult(task, previous, None))
                    continue
                budget.check_deadline()
                if repair.source(task.code, sequence) == "baostock":
                    continue
                budget.check_deadline()
                if not any(cell.status == "complete" for cell in previous):
                    history = resources.prices.read_for_review(
                        task.code, resources.context.calendar.expected_dates(self._security(task.code)), budget,
                    )
                    anchors = tuple(cell.trade_date for cell in history if cell.status == "complete")[-5:]
                    task = replace(task, dates=tuple(sorted(set(task.dates) | set(anchors))))
                    previous = tuple(cell for cell in history if cell.trade_date in task.dates)
                window = resources.supplier.fetch_tencent_window(
                    self._security(task.code), task.dates, request_budget=budget,
                )
                budget.check_deadline()
                if window.code != task.code or tuple(cell.trade_date for cell in window.cells) != task.dates:
                    raise RuntimeError("history_supplier_identity_conflict")
                price = resources.prices.prepare(
                    self._security(task.code),
                    HistoryDownloadTask(task.code, task.dates),
                    window,
                    context=HistoryPriceContext(
                        set(resources.context.calendar.expected_dates(self._security(task.code))).issubset(task.dates),
                        previous,
                    ),
                )
                if price.failure is not None:
                    results.append(HistoryInferredReviewResult(task, previous, None, price.failure))
                    continue
                revisions = tuple(
                    revision for revision in price.revisions
                    if revision.trade_date in inferred and revision.cell.status in {"complete", "supplier_marked_suspended"}
                )
                changed = {revision.trade_date: revision.cell for revision in revisions}
                cells = tuple(changed.get(cell.trade_date, cell) for cell in previous)
                results.append(HistoryInferredReviewResult(task, cells, replace(price, revisions=revisions, missing=())))
            except PriceReviewBudgetExhausted:
                break
            except sqlite3.OperationalError as exc:
                if not IsHistoryReviewBusy(exc):
                    raise
                _LOGGER.warning("history_inferred_review_deferred code=%s reason=sqlite_busy", task.code)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                # A failed review establishes no new suspension or missing-price
                # fact and never changes the normal download failure population.
                if previous:
                    results.append(HistoryInferredReviewResult(task, previous, None, DownloadFailure(exc)))
        return tuple(results)

    def commit_inferred_reviews(
        self,
        results: tuple[HistoryInferredReviewResult, ...],
        sequence: int,
        budget: PriceReviewBudget,
    ) -> None:
        """Commit new facts only behind the unpublished sequence fence."""
        resources = self._resources
        prices = tuple(result.price for result in results if result.price is not None and result.price.revisions)
        if prices:
            if any(revision.first_seen_sequence != sequence for price in prices for revision in price.revisions):
                raise RuntimeError("history_download_identity_conflict")
            budget.check_deadline()
            resources.index.require_unpublished(sequence)
            budget.check_deadline()
            resources.prices.commit_for_review(prices, budget)
        review = HistoryInferredReviewIndex(
            resources.configuration.history_root / "catalog.sqlite3", sqlite_policy=self._sqlite_policy,
        )
        for result in results:
            self._check()
            if result.failure is not None:
                _LOGGER.warning("history_inferred_review_failed code=%s reason=%s", result.task.code, result.failure)
                continue
            budget.check_deadline()
            review.checked(result.task.code, result.cells, sequence, resources.context.calendar.open_dates[-1], budget)

    def _report_tencent_summary(self, total: int, pipeline: HistoryPipelineProgress) -> None:
        resources = self._resources
        finished = resources.index.tasks(codes=frozenset(item.code for item in resources.context.universe))
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
                pipeline=pipeline,
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
        selected: list[HistoryDownloadTask] = []
        while jobs and len(futures) + len(selected) < resources.configuration.history_workers and codes < resources.tuning.max_codes:
            self._check()
            if cells + self._task_cells(jobs[0]) > resources.tuning.max_cells:
                break
            task = jobs.popleft()
            if task.dates and task.code in self._baostock_codes and task.code in self._recheck_codes:
                task = replace(task, dates=resources.context.calendar.expected_dates(self._security(task.code)))
                if cells + len(task.dates) > resources.tuning.max_cells:
                    jobs.appendleft(task)
                    break
            selected.append(task)
            cells += self._task_cells(task)
            codes += 1
        read_started = time.monotonic()
        previous = resources.prices.read_for_download_batch(tuple(selected)) if self._batched_price_reads else ()
        read_seconds = (time.monotonic() - read_started) / len(selected) if selected else 0.0
        for position, task in enumerate(selected):
            self._check()
            future = SubmitWithBackpressure(
                resources.executor,
                self._fetch_task,
                task,
                previous[position].cells if self._batched_price_reads else None,
                read_seconds if self._batched_price_reads else 0.0,
            )
            futures[future] = task
            if future.done() and not future.cancelled() and isinstance(future.exception(), WorkerResourceRejectedError):
                jobs.extendleft(reversed(selected[position + 1:]))
                break
        return not jobs

    def _task_cells(self, task: HistoryDownloadTask) -> int:
        # Any incremental fetch can discover a new adjustment basis. Reserve
        # its bounded full-window fallback before handing the task to a worker.
        if not task.dates:
            return 0
        capacity = self._window_cells.get(task.code)
        if capacity is None:
            capacity = len(self._resources.context.calendar.expected_dates(self._security(task.code)))
            self._window_cells[task.code] = capacity
        return max(len(task.dates), capacity)

    def _fetch_task(
        self,
        task: HistoryDownloadTask,
        previous: tuple[BaoStockDailyCell, ...] | None = None,
        read_seconds: float = 0.0,
    ) -> _PreparedResult:
        self._check()
        uses_baostock = bool(task.dates) and task.code in self._baostock_codes
        if uses_baostock and task.code in self._recheck_codes:
            task = replace(task, dates=self._resources.context.calendar.expected_dates(self._security(task.code)))
        if previous is None:
            read_started = time.monotonic()
            previous = self._resources.prices.read_for_download(task.code, task.dates)
            read_seconds = time.monotonic() - read_started
        obtained = {cell.trade_date for cell in previous if cell.obtained}
        missing = set(task.dates) - obtained
        dates = tuple(sorted(missing | set(sorted(obtained)[-5:]))) if missing else ()
        if uses_baostock and missing and task.code in self._recheck_codes:
            dates = task.dates
            HistoryPriceRepair(self._resources.configuration.history_root, sqlite_policy=self._sqlite_policy).record_tencent_check(task.code, dates[-1])
        empty = PublishedHistoryWindow(task.code, ())
        if not dates:
            started = time.monotonic()
            price = self._tencent_result(_TencentResult(task, (), previous, empty))
            return _PreparedResult(price, (), 0.0, read_seconds, time.monotonic() - started)
        started = time.monotonic()
        failure: str | None = None
        diagnostic: str | None = None
        unusable_qfq_dates: tuple[date, ...] = ()
        try:
            window = self._request_window(self._security(task.code), dates)
        except HistorySupplierPendingError as exc:
            window, failure = exc.window, exc.reason
            diagnostic = HistoryFailureDetail(exc)
            unusable_qfq_dates = exc.unusable_qfq_dates
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            failure = DownloadFailure(exc)
            diagnostic = HistoryFailureDetail(exc)
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
        try:
            price = self._tencent_result(_TencentResult(task, dates, previous, window, failure, unusable_qfq_dates))
            if price.failure and diagnostic is not None:
                price = replace(price, diagnostic=diagnostic)
        except HistoryQfqBasisError:
            return self._fetch_basis_replacement(task, download_seconds, read_seconds)
        return _PreparedResult(
            price,
            dates,
            download_seconds,
            read_seconds,
            time.monotonic() - validation_started,
            review_confirmed=failure is None and price.failure is None,
        )

    def _fetch_basis_replacement(
        self, task: HistoryDownloadTask, download_seconds: float, read_seconds: float,
    ) -> _PreparedResult:
        self._check()
        security = self._security(task.code)
        dates = self._resources.context.calendar.expected_dates(security)
        full_task = replace(task, dates=dates)
        read_started = time.monotonic()
        previous = self._resources.prices.read_for_download(task.code, dates)
        read_seconds += time.monotonic() - read_started
        empty = PublishedHistoryWindow(task.code, ())
        validation_seconds = 0.0
        try:
            fetch_started = time.monotonic()
            try:
                window = self._request_window(security, dates)
            finally:
                download_seconds += time.monotonic() - fetch_started
            self._check()
            if window.code != task.code or tuple(cell.trade_date for cell in window.cells) != dates:
                raise RuntimeError("history_supplier_identity_conflict")
            validation_started = time.monotonic()
            qualified = QualifyReplacementWindow(
                dates, PublishedHistoryWindow(task.code, tuple(ProjectHistoryCell(cell) for cell in previous)), window
            )
            price = HistoryPriceResult(full_task, (), (), None, replacement=qualified)
            validation_seconds = time.monotonic() - validation_started
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            failure = DownloadFailure(exc)
            if failure in _FATAL:
                raise
            # Never publish the already-fetched tail after a failed whole-window
            # refresh. Its prices were observed on an incompatible basis.
            price = replace(self._failed_tencent_result(_TencentResult(full_task, dates, previous, empty), failure),
                            diagnostic=HistoryFailureDetail(exc))
            validation_seconds = 0.0
        return _PreparedResult(
            price, dates, download_seconds, read_seconds, validation_seconds,
            review_confirmed=price.failure is None,
        )

    def _tencent_result(self, result: _TencentResult) -> HistoryPriceResult:
        try:
            prepared = self._prepare(result)
            unusable = tuple(day for day in result.unusable_qfq_dates if day in prepared.missing)
            prepared = replace(prepared, unusable_qfq_dates=unusable)
            return (
                replace(prepared, failure=result.failure or "tencent_history_pending") if prepared.missing else prepared
            )
        except (HistoryPricePartitionError, HistoryQfqBasisError):
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            failure = DownloadFailure(exc)
            if failure in _FATAL:
                raise
            return replace(self._failed_tencent_result(result, failure), diagnostic=HistoryFailureDetail(exc))

    def _failed_tencent_result(self, result: _TencentResult, failure: str) -> HistoryPriceResult:
        previous = result.previous
        obtained = {cell.trade_date for cell in previous if cell.obtained}
        count = sum(cell.status == "complete" for cell in previous)
        suspended = sum(
            cell.status in {"supplier_marked_suspended", "tencent_inferred_suspended"} for cell in previous
        )
        return HistoryPriceResult(
            result.task,
            (),
            tuple(day for day in result.task.dates if day not in obtained),
            failure,
            HistoryPriceCoverage(
                len(result.task.dates), count, count, count, suspended,
                inferred_suspended=sum(cell.status == "tencent_inferred_suspended" for cell in previous),
            ),
            inferred_suspended_dates=tuple(cell.trade_date for cell in previous if cell.status == "tencent_inferred_suspended"),
        )


class _TencentPipeline:
    """One phase's bounded in-flight payloads and durable write acknowledgements."""

    def __init__(self, flow: HistoryDownloadFlow, jobs: deque[HistoryDownloadTask], completed: int, total: int) -> None:
        self._flow = flow
        self._jobs = jobs
        self._completed = completed
        self._total = total
        self._downloaded = completed
        self._network_completed = self._reused = 0
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
        self._last_confirmed = self._last_report

    def run(self) -> HistoryPipelineProgress:
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
        return self._pipeline_progress()

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
        self._last_confirmed = time.monotonic()
        self._writing = None
        self._write_started = None
        self._writing_cells = self._writing_codes = 0
        if self._exhausted and not self._jobs and not self._futures and not self._buffered:
            self._close_writer()
        self._completed = self._flow._acknowledge(
            saved.prepared,
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
                self._reused += 1
                self._flow._reused_codes.add(result.price.task.code)
            else:
                self._network_completed += 1

    def _dispatch(self) -> bool:
        reserved = self._writing_cells + sum(len(item.price.task.dates) for item in self._buffered)
        reserved += sum(self._flow._task_cells(task) for task in self._futures.values())
        codes = self._writing_codes + len(self._buffered) + len(self._futures)
        next_cells = self._flow._task_cells(self._jobs[0]) if self._jobs else 0
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
        oldest = min(self._dispatched_at, key=self._dispatched_at.__getitem__, default=None)
        with self._flow._write_lock:
            operation = self._flow._write_operation
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
            network_completed_codes=self._network_completed,
            reused_codes=self._reused,
            writing_codes=self._writing_codes,
            oldest_inflight_code=None if oldest is None else self._futures[oldest].code,
            seconds_since_commit=now - self._last_confirmed,
            write_operation=operation,
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


__all__ = [
    "HistoryDownloadFlow",
    "HistoryDownloadResources",
    "HistoryInferredReviewResult",
    "DownloadFailure",
]
