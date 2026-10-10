"""Bounded supplier waves with resumable, single-writer qfq publication."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import Future, as_completed
from dataclasses import dataclass
from datetime import date, datetime
from itertools import islice
from time import monotonic
from typing import Protocol

from trader.download.application.qfq_download_progress import QfqDownloadProgress, qfq_message
from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.history_sync import HistorySupplierContext
from trader.download.domain.published_history import PublishedHistoryWindow
from trader.download.domain.qfq_window import (
    QfqUpdateResult,
    QfqWindowIncompleteError,
    completed_daily_cutoff,
    merge_daily_tail,
    paired_window,
)
from trader.infra.workers import WorkerExecutor, submit_or_reject


class QfqWindowPort(Protocol):
    def read_code(self, code: str) -> PublishedHistoryWindow: ...

    def source_identity(self, code: str) -> str | None: ...

    def replace_window(self, window: PublishedHistoryWindow, source_identity: str) -> tuple[tuple[str, ...], int]: ...


class QfqSupplierPort(Protocol):
    def load_qfq_context(self, as_of: date, sessions: int) -> HistorySupplierContext: ...

    def fetch_window(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> PublishedHistoryWindow: ...


class QfqResumePort(Protocol):
    def completed(self, day: date, code: str, source: str) -> bool: ...

    def confirm(self, day: date, code: str, source: str) -> None: ...


@dataclass(frozen=True)
class _DownloadJob:
    security: BaoStockSecurity
    dates: tuple[date, ...]
    previous: PublishedHistoryWindow


@dataclass(frozen=True)
class UpdateQfqWindows:
    v2: QfqWindowPort
    v3: QfqWindowPort
    supplier: QfqSupplierPort
    resume: QfqResumePort
    cancel_requested: Callable[[], bool]
    report: Callable[[str], None]
    workers: int = 8
    worker_pool: WorkerExecutor | None = None
    monotonic: Callable[[], float] = monotonic

    def __post_init__(self) -> None:
        if not 1 <= self.workers <= 12:
            raise ValueError("qfq workers must be within 1..12")

    def seed(self, windows: Iterable[PublishedHistoryWindow], identity: str) -> QfqUpdateResult:
        changed: set[str] = set()
        rows = 0
        count = 0
        for window in windows:
            if self.cancel_requested():
                break
            for cache in (self.v2, self.v3):
                if not cache.read_code(window.code).cells:
                    files, delta = cache.replace_window(window, identity)
                    changed.update(files)
                    rows += delta
            count += 1
            if count % 64 == 0:
                self.report(f"qfq history extraction: codes={count} changed_files={len(changed)}")
        return QfqUpdateResult(None, count, changed_files=tuple(sorted(changed)), changed_rows=rows)

    def execute(self, observed_at: datetime) -> QfqUpdateResult:
        started = self.monotonic()
        self.report(qfq_message(0, "准备", "正在加载沪深股票名单和 Tencent 交易日历"))
        try:
            context = self.supplier.load_qfq_context(completed_daily_cutoff(observed_at), 251)
        except (RuntimeError, OSError, ValueError) as exc:
            stage = "已取消" if self.cancel_requested() else "准备失败"
            self.report(qfq_message(self.monotonic() - started, stage, type(exc).__name__))
            raise
        source = f"{context.source_versions.sdk_version}:{context.source_versions.content_hash}"
        progress = QfqDownloadProgress(
            context.calendar.open_dates[-1],
            source,
            len(context.universe),
            self.report,
            lambda: self.monotonic() - started,
        )
        progress.publish("就绪", f"股票 {progress.total} 只 | 截止 {progress.day} | 并发 {self.workers}")
        jobs = iter(self._jobs(context, progress))
        while wave := tuple(islice(jobs, self.workers)):
            if self.cancel_requested():
                break
            progress.begin_batch(tuple(job.security for job in wave))
            self._run_wave(wave, progress)
            progress.summary("批次完成")
        return progress.finish(self.cancel_requested())

    def _jobs(self, context: HistorySupplierContext, progress: QfqDownloadProgress) -> Iterable[_DownloadJob]:
        checked = 0
        for security in context.universe:
            if self.cancel_requested():
                return
            try:
                job = self._prepare_job(security, context, progress)
            except (RuntimeError, OSError, ValueError) as exc:
                progress.record_pending(security, exc)
                job = None
            if job is not None:
                yield job
            else:
                checked += 1
                if checked % self.workers == 0:
                    progress.summary("检查完成")

    def _prepare_job(
        self, security: BaoStockSecurity, context: HistorySupplierContext, progress: QfqDownloadProgress
    ) -> _DownloadJob | None:
        dates = context.calendar.expected_dates(security)[-251:]
        if not dates:
            progress.not_applicable += 1
            return None
        previous = self.v2.read_code(security.code)
        smaller = self.v3.read_code(security.code)
        same_source = all(cache.source_identity(security.code) == progress.source for cache in (self.v2, self.v3))
        if (
            same_source
            and self.resume.completed(progress.day, security.code, progress.source)
            and paired_window(previous, dates)
            and paired_window(smaller, dates[-61:])
        ):
            progress.completed += 1
            progress.skipped += 1
            return None
        if not same_source:
            previous = PublishedHistoryWindow(security.code, ())
        return _DownloadJob(security, dates, previous)

    def _run_wave(self, wave: tuple[_DownloadJob, ...], progress: QfqDownloadProgress) -> None:
        futures: dict[Future[PublishedHistoryWindow], _DownloadJob] = {}
        for job in wave:
            if self.cancel_requested():
                break
            if self.worker_pool is None:
                future: Future[PublishedHistoryWindow] = Future()
                try:
                    future.set_result(self._download(job))
                except (RuntimeError, OSError, ValueError) as exc:
                    future.set_exception(exc)
            else:
                future = submit_or_reject(self.worker_pool, self._download, job)
            futures[future] = job
        for future in as_completed(futures):
            job = futures[future]
            try:
                if self.cancel_requested():
                    continue
                self._commit_one(future.result(), progress)
                progress.completed += 1
            except (RuntimeError, OSError, ValueError) as exc:
                progress.record_pending(job.security, exc)

    def _commit_one(self, window: PublishedHistoryWindow, progress: QfqDownloadProgress) -> None:
        for cache in (self.v2, self.v3):
            files, delta = cache.replace_window(window, progress.source)
            progress.changed.update(files)
            progress.rows += delta
        if not self.cancel_requested():
            self.resume.confirm(progress.day, window.code, progress.source)

    def _download(self, job: _DownloadJob) -> PublishedHistoryWindow:
        if self.cancel_requested():
            raise RuntimeError("qfq cancelled")
        missing = tuple(day for day in job.dates if day not in {cell.trade_date for cell in job.previous.cells})
        if job.previous.cells and len(missing) <= 5:
            count = max(3, len(missing) + 3)
            tail = self._fetch(job.security, job.dates[-count:])
            merged = merge_daily_tail(job.previous, tail, job.dates)
            if merged is not None and paired_window(merged, job.dates):
                return merged
        return self._fetch(job.security, job.dates)

    def _fetch(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> PublishedHistoryWindow:
        if self.cancel_requested():
            raise RuntimeError("qfq cancelled")
        window = self.supplier.fetch_window(security, dates)
        if window.code != security.code or tuple(cell.trade_date for cell in window.cells) != dates:
            raise RuntimeError("qfq_window_identity_or_calendar_invalid")
        if not paired_window(window, dates):
            raw_missing = tuple(cell.trade_date for cell in window.cells if cell.unadjusted is None)
            qfq_missing = tuple(cell.trade_date for cell in window.cells if cell.qfq is None)
            raise QfqWindowIncompleteError(raw_missing, qfq_missing)
        return window
