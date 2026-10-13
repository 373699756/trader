"""Bounded supplier waves with resumable, single-writer qfq publication."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, wait
from dataclasses import dataclass
from datetime import date, datetime
from itertools import islice
from time import monotonic
from typing import Protocol

from trader.download.application.progress_message import ProgressLevel, ProgressMessage
from trader.download.application.qfq_download_progress import (
    QfqDownloadProgress,
    QfqStockFeedback,
    qfq_failure_code,
    qfq_message,
)
from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.history_sync import HistorySupplierContext
from trader.download.domain.published_history import PublishedHistoryWindow
from trader.download.domain.qfq_window import (
    QfqUpdateResult,
    QfqWindowSnapshot,
    completed_daily_cutoff,
    merge_daily_tail,
)
from trader.infra.workers import WorkerExecutor, submit_with_backpressure


class QfqPairPublicationError(RuntimeError):
    """A later board failed after earlier paired transactions committed."""

    def __init__(self, codes: tuple[str, ...], files: tuple[str, ...], rows: int) -> None:
        super().__init__("qfq_pair_publication_failed")
        self.codes = codes
        self.files = files
        self.rows = rows


class QfqWindowPort(Protocol):
    def replace_paired_windows(
        self, peer: QfqWindowPort, windows: tuple[PublishedHistoryWindow, ...], source_identity: str
    ) -> tuple[tuple[str, ...], int]: ...

    def read_code(self, code: str) -> PublishedHistoryWindow: ...

    def read_codes(self, codes: tuple[str, ...]) -> tuple[PublishedHistoryWindow, ...]: ...

    def inspect_windows(self, allowed_codes: frozenset[str]) -> QfqWindowSnapshot: ...

    def retain_codes(self, allowed_codes: frozenset[str]) -> tuple[tuple[str, ...], int]: ...

    def replace_window(self, window: PublishedHistoryWindow, source_identity: str) -> tuple[tuple[str, ...], int]: ...

    def replace_windows(
        self, windows: tuple[PublishedHistoryWindow, ...], source_identity: str
    ) -> tuple[tuple[str, ...], int]: ...


class QfqSupplierPort(Protocol):
    def load_qfq_context(self, as_of: date, sessions: int) -> HistorySupplierContext: ...

    def fetch_window(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> PublishedHistoryWindow: ...


class QfqResumePort(Protocol):
    def completed(self, day: date, code: str, source: str) -> bool: ...

    def confirm_many(self, day: date, codes: tuple[str, ...], source: str) -> None: ...


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


@dataclass(frozen=True)
class UpdateQfqWindows:
    v2: QfqWindowPort
    v3: QfqWindowPort
    supplier: QfqSupplierPort
    resume: QfqResumePort
    cancel_requested: Callable[[], bool]
    report: Callable[[ProgressMessage], None]
    workers: int = 8
    worker_pool: WorkerExecutor | None = None
    monotonic: Callable[[], float] = monotonic

    def __post_init__(self) -> None:
        if not 1 <= self.workers <= 12:
            raise ValueError("qfq workers must be within 1..12")

    def seed(self, windows: Iterable[PublishedHistoryWindow], identity: str) -> QfqUpdateResult:
        started = self.monotonic()
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
                self.report(
                    qfq_message(
                        self.monotonic() - started,
                        "历史提取",
                        f"已处理 {count} 只 | 变更文件 {len(changed)}",
                    )
                )
        return QfqUpdateResult(None, count, changed_files=tuple(sorted(changed)), changed_rows=rows)

    def execute(self, observed_at: datetime) -> QfqUpdateResult:
        started = self.monotonic()
        self.report(qfq_message(0, "准备", "正在加载沪深股票名单和 Tencent 交易日历"))
        try:
            context = self.supplier.load_qfq_context(completed_daily_cutoff(observed_at), 251)
        except (RuntimeError, OSError, ValueError) as exc:
            cancelled = self.cancel_requested()
            stage = "已取消" if cancelled else "准备失败"
            self.report(
                qfq_message(
                    self.monotonic() - started,
                    stage,
                    qfq_failure_code(exc),
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
        plans = iter(self._plans(context, progress, v2_snapshot, v3_snapshot))
        checkpoint_codes: list[str] = []
        self._download_rolling(plans, progress, checkpoint_codes)
        if not self.cancel_requested():
            self._flush_checkpoint(checkpoint_codes, progress, threshold=1)
        return progress.finish(self.cancel_requested())

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
        if (
            same_source
            and self.resume.completed(progress.day, security.code, progress.source)
            and bool(dates)
            and v2_state is not None
            and bool(v2_state.dates)
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
        return _DownloadPlan(security, dates, same_source)

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
            jobs.append(_DownloadJob(plan.security, plan.dates, window))
        return tuple(jobs)

    def _download_rolling(
        self, plans: Iterable[_DownloadPlan], progress: QfqDownloadProgress, checkpoint_codes: list[str]
    ) -> None:
        remaining = iter(plans)
        futures: dict[Future[PublishedHistoryWindow], _DownloadJob] = {}
        downloaded: list[tuple[_DownloadJob, PublishedHistoryWindow]] = []
        exhausted = False
        last_feedback = self.monotonic()
        try:
            while not self.cancel_requested():
                capacity = self.workers - len(futures)
                plan_batch = tuple(islice(remaining, capacity)) if not exhausted else ()
                if len(plan_batch) < capacity:
                    exhausted = True
                if plan_batch:
                    self._fill_slots(plan_batch, futures, progress)
                if downloaded and (len(downloaded) >= self.workers or not futures):
                    checkpoint_codes.extend(self._commit_downloaded(downloaded[: self.workers], progress))
                    del downloaded[: self.workers]
                    self._flush_checkpoint(checkpoint_codes, progress, threshold=256)
                if not futures:
                    if exhausted and not downloaded:
                        break
                    continue
                done, _pending = wait(tuple(futures), timeout=1.0, return_when=FIRST_COMPLETED)
                if self.monotonic() - last_feedback >= 10.0:
                    progress.publish("请求中", f"在途 {len(futures)} 只 | 已处理 {progress.processed}/{progress.total}")
                    last_feedback = self.monotonic()
                if self.cancel_requested():
                    break
                downloaded.extend(self._collect_completed(done, futures, progress))
        finally:
            for future in futures:
                future.cancel()

    def _fill_slots(
        self,
        plans: tuple[_DownloadPlan, ...],
        futures: dict[Future[PublishedHistoryWindow], _DownloadJob],
        progress: QfqDownloadProgress,
    ) -> None:
        try:
            jobs = self._hydrate_wave(plans)
        except (RuntimeError, OSError, ValueError) as exc:
            for plan in plans:
                progress.record_pending(plan.security, exc, plan.dates, action="本地检查")
            return
        for job in jobs:
            if self.cancel_requested():
                break
            futures[self._submit(job)] = job

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
            except (RuntimeError, OSError, ValueError) as exc:
                progress.record_pending(job.security, exc, job.dates, action="Tencent下载")
                continue
            if all(cell.status == "tencent_inferred_suspended" for cell in window.cells):
                progress.record_no_trade_data(job.security, window)
            else:
                ready.append((job, window))
        return ready

    def _submit(self, job: _DownloadJob) -> Future[PublishedHistoryWindow]:
        if self.worker_pool is not None:
            return submit_with_backpressure(self.worker_pool, self._download, job)
        future: Future[PublishedHistoryWindow] = Future()
        try:
            future.set_result(self._download(job))
        except (RuntimeError, OSError, ValueError) as exc:
            future.set_exception(exc)
        return future

    def _commit_downloaded(
        self, downloaded: list[tuple[_DownloadJob, PublishedHistoryWindow]], progress: QfqDownloadProgress
    ) -> tuple[str, ...]:
        if self.cancel_requested():
            return ()
        try:
            self._commit_many(tuple(window for _job, window in downloaded), progress, progress.source)
        except QfqPairPublicationError as exc:
            progress.changed.update(exc.files)
            progress.rows += exc.rows
            for job, window in downloaded:
                if window.code in exc.codes:
                    progress.completed += 1
                    self._record_completed(progress, job, window)
                else:
                    progress.record_pending(
                        job.security, exc, job.dates, action="Tencent下载", failure_level=ProgressLevel.ERROR
                    )
            return exc.codes
        except (RuntimeError, OSError, ValueError) as exc:
            for job, _window in downloaded:
                progress.record_pending(
                    job.security, exc, job.dates, action="Tencent下载", failure_level=ProgressLevel.ERROR
                )
            return ()
        for job, window in downloaded:
            progress.completed += 1
            self._record_completed(progress, job, window)
        return tuple(window.code for _job, window in downloaded)

    @staticmethod
    def _record_completed(progress: QfqDownloadProgress, job: _DownloadJob, window: PublishedHistoryWindow) -> None:
        paired = sum(cell.status == "complete" for cell in window.cells)
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

    def _commit_many(
        self, windows: tuple[PublishedHistoryWindow, ...], progress: QfqDownloadProgress, source: str
    ) -> None:
        started = self.monotonic()
        files, delta = self.v2.replace_paired_windows(self.v3, windows, source)
        progress.changed.update(files)
        progress.rows += delta
        elapsed = self.monotonic() - started
        if elapsed >= 5.0:
            progress.publish(
                "配对落库",
                f"完成 | 股票 {len(windows)} | 变更行 {delta} | 阶段耗时 {elapsed:.2f} 秒",
                level=ProgressLevel.SUCCESS,
            )

    def _flush_checkpoint(self, checkpoint_codes: list[str], progress: QfqDownloadProgress, *, threshold: int) -> None:
        if self.cancel_requested() or len(checkpoint_codes) < threshold:
            return
        self.resume.confirm_many(progress.day, tuple(checkpoint_codes), progress.source)
        checkpoint_codes.clear()

    def _download(self, job: _DownloadJob) -> PublishedHistoryWindow:
        if self.cancel_requested():
            raise RuntimeError("qfq cancelled")
        previous_dates = {cell.trade_date for cell in job.previous.cells}
        missing = tuple(day for day in job.dates if day not in previous_dates)
        window: PublishedHistoryWindow | None = None
        if job.previous.cells and len(missing) <= 5:
            count = max(3, len(missing) + 3)
            tail = self._fetch(job.security, job.dates[-count:])
            merged = merge_daily_tail(job.previous, tail, job.dates)
            if merged is not None and tuple(cell.trade_date for cell in merged.cells) == job.dates:
                window = merged
        if window is None:
            window = self._fetch(job.security, job.dates)
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
