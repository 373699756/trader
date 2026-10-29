"""Bounded supplier waves with resumable, single-writer qfq publication."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, wait
from dataclasses import dataclass
from datetime import date, datetime
from itertools import islice
from time import monotonic
from typing import Literal, Protocol
from uuid import uuid4

from trader.download.application.progress_message import ProgressLevel, ProgressMessage
from trader.download.application.qfq_download_progress import (
    QfqDownloadProgress,
    QfqFailureCode,
    QfqMessage,
    QfqStockFeedback,
)
from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.history_sync import HistorySupplierContext
from trader.download.domain.history_supplier_failure import HistorySupplierPendingError
from trader.download.domain.history_price_qualification import QualifyReplacementWindow
from trader.download.domain.price_repair import PriceIssue
from trader.download.domain.published_history import PublishedHistoryWindow
from trader.download.domain.qfq_window import (
    CompletedDailyCutoff,
    MergeDailyTail,
    QfqUpdateResult,
    QfqWindowSnapshot,
    QfqPendingReason,
)
from trader.infra.workers import SubmitWithBackpressure, WorkerExecutor, WorkerResourceRejectedError


class QfqPairPublicationError(RuntimeError):
    """A later board failed after earlier paired transactions committed."""

    def __init__(
        self,
        codes: tuple[str, ...],
        files: tuple[str, ...],
        rows: int,
        *,
        reason: str = "qfq_pair_publication_failed",
    ) -> None:
        super().__init__(reason)
        self.codes = codes
        self.files = files
        self.rows = rows


class QfqWindowPort(Protocol):
    def repair_cutoff(self) -> date | None: ...

    def price_issues(self) -> tuple[PriceIssue, ...]: ...

    def record_price_issue(self, issue: PriceIssue, observed: PublishedHistoryWindow | None = None) -> None: ...

    def repair_reference(self, code: str) -> PublishedHistoryWindow: ...

    def covered_dates(self, code: str) -> tuple[date, ...]: ...

    def tencent_recheck_due(self, code: str, calendar: tuple[date, ...]) -> bool: ...

    def reserve_repair(self, issue: PriceIssue, batch: str, *, explicit: bool) -> bool: ...

    def fail_repair(self, code: str, batch: str, reason: str) -> None: ...

    def source_identity(self, code: str) -> str | None: ...

    def replace_paired_windows(
        self,
        peer: QfqWindowPort,
        windows: tuple[PublishedHistoryWindow, ...],
        source_identity: str,
        *,
        calendar: tuple[date, ...],
        completed_day: date | None,
    ) -> tuple[tuple[str, ...], int]: ...

    def completed(self, day: date, code: str, source: str) -> bool: ...

    def read_code(self, code: str) -> PublishedHistoryWindow: ...

    def read_codes(self, codes: tuple[str, ...]) -> tuple[PublishedHistoryWindow, ...]: ...

    def inspect_windows(self, allowed_codes: frozenset[str]) -> QfqWindowSnapshot: ...

    def retain_codes(self, allowed_codes: frozenset[str]) -> tuple[tuple[str, ...], int]: ...


class QfqSupplierPort(Protocol):
    def load_qfq_context(self, as_of: date, sessions: int) -> HistorySupplierContext: ...

    def fetch_window(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> PublishedHistoryWindow: ...


class QfqRepairSupplierPort(Protocol):
    def fetch_baostock_window(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> PublishedHistoryWindow: ...


@dataclass(frozen=True)
class _DownloadPlan:
    security: BaoStockSecurity
    dates: tuple[date, ...]
    reuse_previous: bool


@dataclass(frozen=True)
class _DownloadJob:
    security: BaoStockSecurity
    dates: tuple[date, ...]
    previous: PublishedHistoryWindow
    baostock_basis: bool = False
    recheck_due: bool = False


@dataclass(frozen=True)
class _WriteReceipt:
    codes: tuple[str, ...]
    files: tuple[str, ...]
    rows: int
    failure: RuntimeError | OSError | ValueError | None = None
    write_seconds: float = 0.0


@dataclass(frozen=True)
class UpdateQfqWindows:
    v2: QfqWindowPort
    v3: QfqWindowPort
    supplier: QfqSupplierPort
    cancel_requested: Callable[[], bool]
    report: Callable[[ProgressMessage], None]
    workers: int = 8
    worker_pool: WorkerExecutor | None = None
    writer_pool: WorkerExecutor | None = None
    monotonic: Callable[[], float] = monotonic
    repair_supplier: QfqRepairSupplierPort | None = None
    repair_only: bool = False

    def __post_init__(self) -> None:
        if not 1 <= self.workers <= 12:
            raise ValueError("qfq workers must be within 1..12")

    def seed(
        self, windows: Iterable[PublishedHistoryWindow], identity: str, *, calendar: tuple[date, ...]
    ) -> QfqUpdateResult:
        started = self.monotonic()
        changed: set[str] = set()
        rows = 0
        count = 0
        for window in windows:
            if self.cancel_requested():
                break
            bounded = PublishedHistoryWindow(
                window.code, tuple(cell for cell in window.cells if cell.trade_date in calendar[-251:])
            )
            files, delta = self.v2.replace_paired_windows(
                self.v3, (bounded,), identity, calendar=calendar[-251:], completed_day=None
            )
            changed.update(files)
            rows += delta
            count += 1
            if count % 64 == 0:
                self.report(
                    QfqMessage(
                        self.monotonic() - started,
                        "历史提取",
                        f"已处理 {count} 只 | 变更文件 {len(changed)}",
                    )
                )
        return QfqUpdateResult(None, count, changed_files=tuple(sorted(changed)), changed_rows=rows)

    def execute(self, observed_at: datetime) -> QfqUpdateResult:
        started = self.monotonic()
        self.report(QfqMessage(0, "准备", "正在加载沪深股票名单和 Tencent 交易日历"))
        try:
            cutoff = self.v2.repair_cutoff() if self.repair_only else None
            context = self.supplier.load_qfq_context(cutoff or CompletedDailyCutoff(observed_at), 251)
        except (RuntimeError, OSError, ValueError) as exc:
            cancelled = self.cancel_requested()
            stage = "已取消" if cancelled else "准备失败"
            self.report(
                QfqMessage(
                    self.monotonic() - started,
                    stage,
                    QfqFailureCode(exc),
                    level=ProgressLevel.WARNING if cancelled else ProgressLevel.ERROR,
                )
            )
            raise
        source = f"{context.source_versions.sdk_version}:{context.source_versions.content_hash}"
        progress = QfqDownloadProgress(
            context.calendar.open_dates[-1],
            source,
            len(context.universe),
            self.report,
            lambda: self.monotonic() - started,
        )
        allowed_codes = frozenset(security.code for security in context.universe)
        for cache in (self.v2, self.v3):
            files, deleted_rows = cache.retain_codes(allowed_codes)
            progress.changed.update(files)
            progress.rows += deleted_rows
        inspection_started = self.monotonic()
        progress.publish("本地窗口检查", "开始 | 逐板块校验两档内容")
        v2_snapshot = self.v2.inspect_windows(allowed_codes)
        v3_snapshot = self.v3.inspect_windows(allowed_codes)
        progress.publish(
            "本地窗口检查",
            f"完成 | V2 {len(v2_snapshot.states)} 只 | V3 {len(v3_snapshot.states)} 只 "
            f"| 阶段耗时 {self.monotonic() - inspection_started:.2f} 秒",
        )
        progress.publish("就绪", f"股票 {progress.total} 只 | 截止 {progress.day} | 并发 {self.workers}")
        if self.repair_only:
            # Older files had no durable issue inventory. Recover absent dates
            # only for locally published stocks; never schedule the whole universe.
            for security in context.universe:
                state = v2_snapshot.state_for(security.code)
                if state is not None and state.verified:
                    covered = self.v2.covered_dates(security.code)
                    absent = tuple(day for day in context.calendar.expected_dates(security) if day <= max(covered) and day not in covered) if covered else ()
                    if absent:
                        self.v2.record_price_issue(PriceIssue(security.code, absent, "legacy_window_missing"))
            failure_reason = None
        else:
            plans = iter(self._plans(context, progress, v2_snapshot, v3_snapshot))
            failure_reason = self._download_rolling(plans, progress, context.calendar.open_dates[-251:])
        if self.repair_supplier is not None and failure_reason is None and not self.cancel_requested():
            self._repair(context, progress)
        return progress.finish(self.cancel_requested(), failure_reason=failure_reason)

    def _repair(self, context: HistorySupplierContext, progress: QfqDownloadProgress) -> None:
        assert self.repair_supplier is not None
        batch = uuid4().hex
        securities = {item.code: item for item in context.universe}
        issues = tuple(item for item in self.v2.price_issues() if item.code in securities and any(day in context.calendar.open_dates for day in item.dates))
        if self.repair_only:
            progress.total = len(issues)
        progress.publish("BaoStock补缺", f"问题股票 {len(issues)} 只 | 串行请求 | 最小间隔 1.5 秒")
        for issue in issues:
            if self.cancel_requested():
                break
            security = securities.get(issue.code)
            if security is None:
                continue
            dates = context.calendar.expected_dates(security)[-251:]
            issue = PriceIssue(issue.code, tuple(day for day in issue.dates if day in dates), issue.reason)
            if not issue.dates or not self.v2.reserve_repair(issue, batch, explicit=self.repair_only):
                continue
            try:
                previous = self.v2.read_code(issue.code)
                candidate = None
                if (self.v2.source_identity(issue.code) or "").startswith("baostock:") and previous.cells:
                    missing = tuple(day for day in dates if day not in {cell.trade_date for cell in previous.cells})
                    if len(missing) <= 5:
                        tail = self.repair_supplier.fetch_baostock_window(security, dates[-max(5, len(missing) + 3):])
                        candidate = MergeDailyTail(previous, tail, dates)
                if candidate is None:
                    candidate = self.repair_supplier.fetch_baostock_window(security, dates)
                candidate = QualifyReplacementWindow(dates, self.v2.repair_reference(issue.code), candidate)
                files, rows = self.v2.replace_paired_windows(self.v3, (candidate,), f"baostock:{progress.source}", calendar=context.calendar.open_dates[-251:], completed_day=progress.day)
                progress.changed.update(files)
                progress.rows += rows
                progress.publish("BaoStock补缺", f"{issue.code} | 整窗发布成功 | {len(dates)} 股日")
            except (RuntimeError, OSError, ValueError) as exc:
                self.v2.fail_repair(issue.code, batch, QfqFailureCode(exc))
                progress.publish("BaoStock补缺", f"{issue.code} | 未修复 | {QfqFailureCode(exc)}", level=ProgressLevel.WARNING)
        remaining = tuple(item for item in self.v2.price_issues() if item.code in securities and any(day in context.calendar.open_dates for day in item.dates))
        # Durable state is authoritative after the second phase; Tencent pending
        # counters must not continue to report a stock that has been repaired.
        resolved = len(issues) - len(remaining)
        progress.completed += resolved
        progress.pending = len(remaining)
        reasons: dict[str, int] = {}
        for issue in remaining:
            reasons[issue.reason] = reasons.get(issue.reason, 0) + 1
        progress.pending_reasons = tuple(QfqPendingReason(reason, count) for reason, count in sorted(reasons.items()))

    def _plans(
        self,
        context: HistorySupplierContext,
        progress: QfqDownloadProgress,
        v2_snapshot: QfqWindowSnapshot,
        v3_snapshot: QfqWindowSnapshot,
    ) -> Iterable[_DownloadPlan]:
        for security in context.universe:
            if self.cancel_requested():
                return
            dates = context.calendar.expected_dates(security)[-251:]
            try:
                plan = self._prepare_plan(security, dates, progress, v2_snapshot, v3_snapshot)
            except (RuntimeError, OSError, ValueError) as exc:
                if self.repair_supplier is not None:
                    self.v2.record_price_issue(PriceIssue(security.code, dates, QfqFailureCode(exc)))
                progress.record_pending(security, exc, dates, action="本地检查")
                plan = None
            if plan is not None:
                yield plan

    def _prepare_plan(
        self,
        security: BaoStockSecurity,
        dates: tuple[date, ...],
        progress: QfqDownloadProgress,
        v2_snapshot: QfqWindowSnapshot,
        v3_snapshot: QfqWindowSnapshot,
    ) -> _DownloadPlan | None:
        if not dates:
            progress.not_applicable += 1
            progress.record_stock(
                security,
                QfqStockFeedback("无需下载", "无需下载", 0, 0, 0, 0),
            )
            return None
        v2_state = v2_snapshot.state_for(security.code)
        v3_state = v3_snapshot.state_for(security.code)
        same_source = all(
            state is not None and state.verified and state.source == progress.source for state in (v2_state, v3_state)
        )
        baostock_source = all(state is not None and state.verified and state.source == f"baostock:{progress.source}" for state in (v2_state, v3_state))
        if (
            (same_source or baostock_source)
            and self.v2.completed(progress.day, security.code, f"baostock:{progress.source}" if baostock_source else progress.source)
            and bool(dates)
            and v2_state is not None
            and bool(v2_state.dates)
            and v2_state.dates[-1] == dates[-1]
            and v3_state is not None
            and v3_state.dates == tuple(day for day in v2_state.dates if day >= dates[-min(61, len(dates))])
        ):
            progress.completed += 1
            progress.skipped += 1
            progress.record_stock(
                security,
                QfqStockFeedback(
                    "本地复用",
                    "复用",
                    len(v2_state.dates),
                    len(v2_state.dates),
                    len(v2_state.dates),
                    len(v2_state.dates),
                ),
            )
            return None
        return _DownloadPlan(security, dates, same_source or baostock_source)

    def _hydrate_wave(self, plans: tuple[_DownloadPlan, ...]) -> tuple[_DownloadJob, ...]:
        reusable_codes = tuple(plan.security.code for plan in plans if plan.reuse_previous)
        previous = self.v2.read_codes(reusable_codes) if reusable_codes else ()
        previous_index = 0
        jobs: list[_DownloadJob] = []
        for plan in plans:
            window = PublishedHistoryWindow(plan.security.code, ())
            if plan.reuse_previous:
                window = previous[previous_index]
                previous_index += 1
                if window.code != plan.security.code:
                    raise RuntimeError("qfq batch read order mismatch")
            basis = self.repair_supplier is not None and (self.v2.source_identity(plan.security.code) or "").startswith("baostock:")
            if basis and not window.cells:
                window = self.v2.read_code(plan.security.code)
            due = basis and self.v2.tencent_recheck_due(plan.security.code, plan.dates)
            jobs.append(_DownloadJob(plan.security, plan.dates, window, basis, due))
        return tuple(jobs)

    def _download_rolling(
        self, plans: Iterable[_DownloadPlan], progress: QfqDownloadProgress, calendar: tuple[date, ...]
    ) -> Literal["qfq_worker_unavailable"] | None:
        remaining = iter(plans)
        futures: dict[Future[PublishedHistoryWindow], _DownloadJob] = {}
        downloaded: list[tuple[_DownloadJob, PublishedHistoryWindow]] = []
        writing: Future[_WriteReceipt] | None = None
        write_batch: tuple[tuple[_DownloadJob, PublishedHistoryWindow], ...] = ()
        exhausted = False
        failure_reason: Literal["qfq_worker_unavailable"] | None = None
        last_feedback = last_flush = self.monotonic()
        try:
            while not self.cancel_requested():
                if writing is not None and writing.done():
                    self._accept_write(write_batch, writing.result(), progress)
                    writing, write_batch = None, ()
                buffered = len(downloaded) + len(write_batch) + len(futures)
                cells = sum(len(window.cells) for _, window in (*downloaded, *write_batch))
                cells += sum(len(job.dates) for job in futures.values())
                capacity = min(self.workers - len(futures), max(0, 32 - buffered), max(0, (8192 - cells) // 251))
                plan_batch = tuple(islice(remaining, capacity)) if not exhausted else ()
                if capacity and len(plan_batch) < capacity:
                    exhausted = True
                if plan_batch:
                    failure_reason = self._fill_slots(plan_batch, futures, progress)
                    exhausted = exhausted or failure_reason is not None
                if (
                    downloaded
                    and writing is None
                    and (
                        len(downloaded) >= 16
                        or sum(len(window.cells) for _, window in downloaded) >= 4096
                        or self.monotonic() - last_flush >= 1
                        or not futures
                    )
                ):
                    count, batch_cells = 0, 0
                    for _, window in downloaded[:16]:
                        if count and batch_cells + len(window.cells) > 4096:
                            break
                        count, batch_cells = count + 1, batch_cells + len(window.cells)
                    write_batch = tuple(downloaded[:count])
                    del downloaded[:count]
                    writing = self._submit_write(write_batch, progress.source, calendar)
                    last_flush = self.monotonic()
                if not futures and exhausted and not downloaded and writing is None:
                    break
                if self.monotonic() - last_feedback >= 5:
                    progress.publish(
                        "请求/落库中",
                        f"网络在途 {len(futures)} | 待写 {len(downloaded)} | 写入 {len(write_batch)} | 已处理 {progress.processed}/{progress.total}",
                    )
                    last_feedback = self.monotonic()
                if futures or writing is not None:
                    wait(
                        (*futures, *((writing,) if writing is not None else ())), timeout=1, return_when=FIRST_COMPLETED
                    )
                if self.cancel_requested():
                    break
                downloaded.extend(
                    self._collect_completed({future for future in futures if future.done()}, futures, progress)
                )
        finally:
            for future in futures:
                future.cancel()
            if writing is not None:
                # Never release the maintenance lock while a paired transaction is still running.
                writing.result()
        return failure_reason

    def _submit_write(
        self, batch: tuple[tuple[_DownloadJob, PublishedHistoryWindow], ...], source: str, calendar: tuple[date, ...]
    ) -> Future[_WriteReceipt]:
        if self.writer_pool is not None:
            return SubmitWithBackpressure(self.writer_pool, self._write_downloaded, batch, source, calendar)
        result: Future[_WriteReceipt] = Future()
        result.set_result(self._write_downloaded(batch, source, calendar))
        return result

    def _write_downloaded(
        self, batch: tuple[tuple[_DownloadJob, PublishedHistoryWindow], ...], source: str, calendar: tuple[date, ...]
    ) -> _WriteReceipt:
        if self.cancel_requested():
            return _WriteReceipt((), (), 0, RuntimeError("cancelled"))
        started = self.monotonic()
        try:
            files, rows = self.v2.replace_paired_windows(
                self.v3, tuple(window for _, window in batch), source, calendar=calendar, completed_day=calendar[-1]
            )
            return _WriteReceipt(
                tuple(window.code for _, window in batch), files, rows, write_seconds=self.monotonic() - started
            )
        except QfqPairPublicationError as exc:
            return _WriteReceipt(exc.codes, exc.files, exc.rows, exc, self.monotonic() - started)
        except (RuntimeError, OSError, ValueError) as exc:
            return _WriteReceipt((), (), 0, exc, self.monotonic() - started)

    def _fill_slots(
        self,
        plans: tuple[_DownloadPlan, ...],
        futures: dict[Future[PublishedHistoryWindow], _DownloadJob],
        progress: QfqDownloadProgress,
    ) -> Literal["qfq_worker_unavailable"] | None:
        try:
            jobs = self._hydrate_wave(plans)
        except (RuntimeError, OSError, ValueError) as exc:
            for plan in plans:
                if self.repair_supplier is not None:
                    self.v2.record_price_issue(PriceIssue(plan.security.code, plan.dates, QfqFailureCode(exc)))
                progress.record_pending(plan.security, exc, plan.dates, action="本地检查")
            return None
        for job in jobs:
            if self.cancel_requested():
                break
            future = self._submit(job)
            futures[future] = job
            if future.done() and not future.cancelled() and isinstance(future.exception(), WorkerResourceRejectedError):
                return "qfq_worker_unavailable"
        return None

    def _collect_completed(
        self,
        done: set[Future[PublishedHistoryWindow]],
        futures: dict[Future[PublishedHistoryWindow], _DownloadJob],
        progress: QfqDownloadProgress,
    ) -> list[tuple[_DownloadJob, PublishedHistoryWindow]]:
        ready: list[tuple[_DownloadJob, PublishedHistoryWindow]] = []
        for future in done:
            job = futures.pop(future)
            try:
                window = future.result()
            except WorkerResourceRejectedError:
                progress.record_pending(
                    job.security, RuntimeError("qfq_worker_unavailable"), job.dates, action="任务派发"
                )
                continue
            except (RuntimeError, OSError, ValueError) as exc:
                if self.repair_supplier is not None:
                    dates = job.dates
                    if isinstance(exc, HistorySupplierPendingError):
                        dates = tuple(cell.trade_date for cell in exc.window.cells if cell.status not in ("complete", "supplier_marked_suspended", "tencent_inferred_suspended")) or dates
                    if job.baostock_basis:
                        previous_dates = {cell.trade_date for cell in self.v2.read_code(job.security.code).cells}
                        dates = tuple(sorted(set(dates) | (set(job.dates) - previous_dates)))
                    self.v2.record_price_issue(PriceIssue(job.security.code, dates, QfqFailureCode(exc)), exc.window if isinstance(exc, HistorySupplierPendingError) else None)
                progress.record_pending(job.security, exc, job.dates, action="Tencent下载")
                continue
            if all(cell.status == "tencent_inferred_suspended" for cell in window.cells):
                progress.record_no_trade_data(job.security, window)
            else:
                ready.append((job, window))
        return ready

    def _submit(self, job: _DownloadJob) -> Future[PublishedHistoryWindow]:
        if self.worker_pool is not None:
            return SubmitWithBackpressure(self.worker_pool, self._download, job)
        future: Future[PublishedHistoryWindow] = Future()
        try:
            future.set_result(self._download(job))
        except (RuntimeError, OSError, ValueError) as exc:
            future.set_exception(exc)
        return future

    def _accept_write(
        self,
        batch: tuple[tuple[_DownloadJob, PublishedHistoryWindow], ...],
        receipt: _WriteReceipt,
        progress: QfqDownloadProgress,
    ) -> tuple[str, ...]:
        progress.changed.update(receipt.files)
        progress.rows += receipt.rows
        progress.write_seconds += receipt.write_seconds
        for job, window in batch:
            if window.code in receipt.codes:
                progress.completed += 1
                self._record_completed(progress, job, window)
            else:
                if self.repair_supplier is not None:
                    self.v2.record_price_issue(PriceIssue(job.security.code, job.dates, QfqFailureCode(receipt.failure or RuntimeError("qfq_pair_publication_failed"))))
                progress.record_pending(
                    job.security,
                    receipt.failure or RuntimeError("qfq_pair_publication_failed"),
                    job.dates,
                    action="配对落库",
                    failure_level=ProgressLevel.ERROR,
                )
        return receipt.codes

    @staticmethod
    def _record_completed(progress: QfqDownloadProgress, job: _DownloadJob, window: PublishedHistoryWindow) -> None:
        paired = sum(cell.status == "complete" for cell in window.cells)
        if window.cells[-1].status != "complete":
            progress.latest_missing += 1
        progress.record_stock(
            job.security,
            QfqStockFeedback(
                "Tencent下载",
                "合格",
                paired,
                paired,
                paired,
                paired,
                inferred_suspended=sum(cell.status == "tencent_inferred_suspended" for cell in window.cells),
            ),
        )

    def _download(self, job: _DownloadJob) -> PublishedHistoryWindow:
        if self.cancel_requested():
            raise RuntimeError("qfq cancelled")
        previous_dates = {cell.trade_date for cell in job.previous.cells}
        missing = tuple(day for day in job.dates if day not in previous_dates)
        if job.baostock_basis and not job.recheck_due:
            self._fetch(job.security, job.dates[-max(5, min(len(missing) + 3, len(job.dates))):])
            raise RuntimeError("price_source_basis_conflict")
        window: PublishedHistoryWindow | None = None
        if job.previous.cells and len(missing) <= 5 and not job.baostock_basis:
            count = max(3, len(missing) + 3)
            tail = self._fetch(job.security, job.dates[-count:])
            merged = MergeDailyTail(job.previous, tail, job.dates)
            if merged is not None and tuple(cell.trade_date for cell in merged.cells) == job.dates:
                window = merged
        if window is None:
            window = self._fetch(job.security, job.dates)
        if job.baostock_basis:
            window = QualifyReplacementWindow(job.dates, self.v2.read_code(job.security.code), window)
        if any(cell.status not in ("complete", "supplier_marked_suspended", "tencent_inferred_suspended") for cell in window.cells):
            raise HistorySupplierPendingError(window)
        if not any(cell.status == "complete" for cell in window.cells) and not all(
            cell.status == "tencent_inferred_suspended" for cell in window.cells
        ):
            raise RuntimeError("qfq_no_trade_bars")
        return window

    def _fetch(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> PublishedHistoryWindow:
        if self.cancel_requested():
            raise RuntimeError("qfq cancelled")
        window = self.supplier.fetch_window(security, dates)
        self._validate_window(security, dates, window)
        return window

    @staticmethod
    def _validate_window(security: BaoStockSecurity, dates: tuple[date, ...], window: PublishedHistoryWindow) -> None:
        if window.code != security.code or tuple(cell.trade_date for cell in window.cells) != dates:
            raise RuntimeError("qfq_window_identity_or_calendar_invalid")
