"""Bounded supplier waves with resumable, single-writer qfq publication."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import Future, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime
from itertools import islice
from typing import Protocol

from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.history_sync import HistorySupplierContext
from trader.download.domain.published_history import PublishedHistoryWindow
from trader.download.domain.qfq_window import QfqUpdateResult, completed_daily_cutoff, merge_daily_tail, paired_window
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
    position: int
    security: BaoStockSecurity
    dates: tuple[date, ...]
    previous: PublishedHistoryWindow


@dataclass
class _UpdateProgress:
    day: date
    source: str
    completed: int = 0
    pending: int = 0
    skipped: int = 0
    rows: int = 0
    changed: set[str] = field(default_factory=set)

    def result(self, cancelled: bool) -> QfqUpdateResult:
        return QfqUpdateResult(
            self.day,
            self.completed,
            self.pending,
            tuple(sorted(self.changed)),
            self.rows,
            self.skipped,
            "cancelled" if cancelled else None,
        )


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
        self.report("qfq context: loading current exchange universe and Tencent trading calendar")
        context = self.supplier.load_qfq_context(completed_daily_cutoff(observed_at), 251)
        source = f"{context.source_versions.sdk_version}:{context.source_versions.content_hash}"
        progress = _UpdateProgress(context.calendar.open_dates[-1], source)
        jobs = iter(self._jobs(context, progress))
        while wave := tuple(islice(jobs, self.workers)):
            if self.cancel_requested():
                progress.pending += len(wave)
                continue
            self.report(f"qfq downloading: wave={len(wave)} cutoff={progress.day}")
            self._run_wave(wave, progress)
            self.report(
                f"qfq progress: completed={progress.completed} pending={progress.pending} "
                f"changed_files={len(progress.changed)}"
            )
        return progress.result(self.cancel_requested())

    def _jobs(self, context: HistorySupplierContext, progress: _UpdateProgress) -> Iterable[_DownloadJob]:
        for position, security in enumerate(context.universe, 1):
            if self.cancel_requested():
                progress.pending += len(context.universe) - position + 1
                return
            try:
                job = self._prepare_job(position, security, context, progress)
            except (RuntimeError, OSError, ValueError) as exc:
                self._pending(position, exc, progress)
                continue
            if job is not None:
                yield job

    def _prepare_job(
        self, position: int, security: BaoStockSecurity, context: HistorySupplierContext, progress: _UpdateProgress
    ) -> _DownloadJob | None:
        dates = context.calendar.expected_dates(security)[-251:]
        if not dates:
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
        return _DownloadJob(position, security, dates, previous)

    def _run_wave(self, wave: tuple[_DownloadJob, ...], progress: _UpdateProgress) -> None:
        futures: dict[Future[PublishedHistoryWindow], _DownloadJob] = {}
        for job in wave:
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
                    progress.pending += 1
                    continue
                self._commit_one(future.result(), progress)
                progress.completed += 1
            except (RuntimeError, OSError, ValueError) as exc:
                self._pending(job.position, exc, progress)

    def _pending(self, position: int, exc: Exception, progress: _UpdateProgress) -> None:
        progress.pending += 1
        message = str(exc)
        reason = message if len(message) <= 64 and message.replace("_", "").isalnum() else type(exc).__name__
        self.report(f"qfq code pending: position={position} reason={reason}")

    def _commit_one(self, window: PublishedHistoryWindow, progress: _UpdateProgress) -> None:
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
            raw_missing = sum(cell.unadjusted is None for cell in window.cells)
            qfq_missing = sum(cell.qfq is None for cell in window.cells)
            raise RuntimeError(f"qfq_incomplete_raw_{raw_missing}_qfq_{qfq_missing}")
        return window
