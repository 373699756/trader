"""Bounded supplier waves with resumable, single-writer qfq publication."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, wait
from dataclasses import dataclass
from datetime import date, datetime
from itertools import islice
from time import monotonic
from typing import Protocol

from trader.download.application.qfq_download_progress import QfqDownloadProgress, qfq_failure_code, qfq_message
from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.history_supplier_failure import HistorySupplierPendingError
from trader.download.domain.history_sync import HistorySupplierContext
from trader.download.domain.published_history import PublishedHistoryWindow
from trader.download.domain.qfq_window import (
    QfqUpdateResult,
    QfqWindowIncompleteError,
    QfqWindowSnapshot,
    completed_daily_cutoff,
    merge_daily_tail,
    paired_window,
)
from trader.infra.workers import WorkerExecutor, submit_or_reject


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


class QfqGapRecoveryPort(Protocol):
    @property
    def source_identity(self) -> str: ...

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
class _GapJob:
    security: BaoStockSecurity
    dates: tuple[date, ...]
    missing_dates: tuple[date, ...] | None


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
    gap_supplier: QfqGapRecoveryPort | None = None

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
            self.report(qfq_message(self.monotonic() - started, stage, qfq_failure_code(exc)))
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
        gaps = self._download_rolling(plans, progress, checkpoint_codes)
        if not self.cancel_requested():
            self._recover_gaps(tuple(sorted(gaps, key=lambda gap: gap.security.code)), progress, checkpoint_codes)
            self._flush_checkpoint(checkpoint_codes, progress, threshold=1)
        return progress.finish(self.cancel_requested())

    def _plans(
        self,
        context: HistorySupplierContext,
        progress: QfqDownloadProgress,
        v2_snapshot: QfqWindowSnapshot,
        v3_snapshot: QfqWindowSnapshot,
    ) -> Iterable[_DownloadPlan]:
        checked = 0
        for security in context.universe:
            if self.cancel_requested():
                return
            try:
                plan = self._prepare_plan(security, context, progress, v2_snapshot, v3_snapshot)
            except (RuntimeError, OSError, ValueError) as exc:
                progress.record_pending(security, exc)
                plan = None
            if plan is not None:
                yield plan
            else:
                checked += 1
                if checked % self.workers == 0:
                    progress.summary("检查完成")

    def _prepare_plan(
        self,
        security: BaoStockSecurity,
        context: HistorySupplierContext,
        progress: QfqDownloadProgress,
        v2_snapshot: QfqWindowSnapshot,
        v3_snapshot: QfqWindowSnapshot,
    ) -> _DownloadPlan | None:
        dates = context.calendar.expected_dates(security)[-251:]
        if not dates:
            progress.not_applicable += 1
            return None
        v2_state = v2_snapshot.state_for(security.code)
        v3_state = v3_snapshot.state_for(security.code)
        same_source = all(
            state is not None and state.verified and state.source == progress.source for state in (v2_state, v3_state)
        )
        recovery_source = self.gap_supplier.source_identity if self.gap_supplier is not None else None
        recovered_source = recovery_source is not None and all(
            state is not None and state.verified and state.source == recovery_source for state in (v2_state, v3_state)
        )
        if (
            (same_source or recovered_source)
            and self.resume.completed(progress.day, security.code, progress.source)
            and v2_state is not None
            and v2_state.dates == dates
            and v3_state is not None
            and v3_state.dates == dates[-61:]
        ):
            progress.completed += 1
            progress.skipped += 1
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
    ) -> list[_GapJob]:
        remaining = iter(plans)
        futures: dict[Future[PublishedHistoryWindow], _DownloadJob] = {}
        downloaded: list[tuple[_DownloadJob, PublishedHistoryWindow]] = []
        gaps: list[_GapJob] = []
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
                    progress.summary("批次完成")
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
                failed, ready = self._collect_completed(done, futures, progress)
                gaps.extend(failed)
                downloaded.extend(ready)
        finally:
            for future in futures:
                future.cancel()
        return gaps

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
                progress.record_pending(plan.security, exc)
            return
        progress.begin_batch(tuple(job.security for job in jobs))
        for job in jobs:
            if self.cancel_requested():
                break
            futures[self._submit(job)] = job

    def _collect_completed(
        self,
        done: set[Future[PublishedHistoryWindow]],
        futures: dict[Future[PublishedHistoryWindow], _DownloadJob],
        progress: QfqDownloadProgress,
    ) -> tuple[list[_GapJob], list[tuple[_DownloadJob, PublishedHistoryWindow]]]:
        gaps: list[_GapJob] = []
        ready: list[tuple[_DownloadJob, PublishedHistoryWindow]] = []
        for future in done:
            job = futures.pop(future)
            try:
                ready.append((job, future.result()))
            except (RuntimeError, OSError, ValueError) as exc:
                progress.record_pending(job.security, exc)
                missing = (
                    tuple(sorted(set(exc.raw_missing) | set(exc.qfq_missing)))
                    if isinstance(exc, QfqWindowIncompleteError)
                    else None
                )
                if isinstance(exc, QfqWindowIncompleteError):
                    progress.publish(
                        "Tencent覆盖",
                        f"{job.security.code} | 应有 {len(job.dates)} 日 "
                        f"| raw {len(job.dates) - len(exc.raw_missing)} 日 "
                        f"| qfq {len(job.dates) - len(exc.qfq_missing)} 日 "
                        f"| 剩余缺日 {len(set(exc.raw_missing) | set(exc.qfq_missing))} 日",
                    )
                    gaps.append(_GapJob(job.security, job.dates, missing))
                elif isinstance(exc, HistorySupplierPendingError):
                    progress.publish("Tencent待重试", f"{job.security.code} | 暂时失败，不转BaoStock")
        return gaps, ready

    def _submit(self, job: _DownloadJob) -> Future[PublishedHistoryWindow]:
        if self.worker_pool is not None:
            return submit_or_reject(self.worker_pool, self._download, job)
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
            progress.completed += len(exc.codes)
            for job, window in downloaded:
                if window.code not in exc.codes:
                    progress.record_pending(job.security, exc)
            return exc.codes
        except (RuntimeError, OSError, ValueError) as exc:
            for job, _window in downloaded:
                progress.record_pending(job.security, exc)
            return ()
        progress.completed += len(downloaded)
        for job, window in downloaded:
            progress.publish(
                "Tencent覆盖",
                f"{job.security.code} | 应有 {len(job.dates)} 日 "
                f"| raw {len(window.cells)} 日 | qfq {len(window.cells)} 日 "
                f"| 完整配对 {len(window.cells)} 日 | 剩余缺日 0 日",
            )
        return tuple(window.code for _job, window in downloaded)

    def _recover_gaps(
        self, gaps: tuple[_GapJob, ...], progress: QfqDownloadProgress, checkpoint_codes: list[str]
    ) -> None:
        recovery_started = self.monotonic()
        missing_days = sum(len(gap.missing_dates or ()) for gap in gaps)
        unknown = sum(gap.missing_dates is None for gap in gaps)
        progress.publish(
            "Tencent阶段完成",
            f"合格 {progress.completed} | 待补股票 {len(gaps)} | 已知缺失股票交易日 {missing_days} "
            f"| 缺日数未确定 {unknown} 只 | 其它待重试或检查失败 {progress.pending - len(gaps)} 只",
        )
        if self.gap_supplier is None or not gaps:
            return
        recovered_codes: list[str] = []
        for index, job in enumerate(gaps, 1):
            if self.cancel_requested():
                break
            progress.publish(
                "BaoStock补缺",
                f"{index}/{len(gaps)}（{index / len(gaps):.2%}）| {job.security.code} {job.security.name} "
                f"| 同源配对窗口 {len(job.dates)} 日",
            )
            try:
                window = self.gap_supplier.fetch_window(job.security, job.dates)
                if self.cancel_requested():
                    break
                self._validate_window(job.security, job.dates, window)
                self._commit_one(window, progress, self.gap_supplier.source_identity)
                progress.pending -= 1
                progress.completed += 1
            except (RuntimeError, OSError, ValueError) as exc:
                # Replace the existing pending detail without counting the stock twice.
                progress.pending -= 1
                progress.record_pending(job.security, exc)
            else:
                recovered_codes.append(window.code)
                checkpoint_codes.append(window.code)
                self._flush_checkpoint(checkpoint_codes, progress, threshold=16)
            progress.summary("补缺进度")
        progress.publish(
            "BaoStock补缺完成",
            f"处理 {len(gaps)} 只 | 补齐 {len(recovered_codes)} 只 "
            f"| 阶段耗时 {self.monotonic() - recovery_started:.2f} 秒",
        )

    def _commit_one(self, window: PublishedHistoryWindow, progress: QfqDownloadProgress, source: str) -> None:
        self._commit_many((window,), progress, source)

    def _commit_many(
        self, windows: tuple[PublishedHistoryWindow, ...], progress: QfqDownloadProgress, source: str
    ) -> None:
        started = self.monotonic()
        files, delta = self.v2.replace_paired_windows(self.v3, windows, source)
        progress.changed.update(files)
        progress.rows += delta
        elapsed = self.monotonic() - started
        if elapsed >= 5.0:
            progress.publish("配对落库", f"完成 | 股票 {len(windows)} | 变更行 {delta} | 阶段耗时 {elapsed:.2f} 秒")

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
        self._validate_window(security, dates, window)
        return window

    @staticmethod
    def _validate_window(security: BaoStockSecurity, dates: tuple[date, ...], window: PublishedHistoryWindow) -> None:
        if window.code != security.code or tuple(cell.trade_date for cell in window.cells) != dates:
            raise RuntimeError("qfq_window_identity_or_calendar_invalid")
        if not paired_window(window, dates):
            raise QfqWindowIncompleteError(
                tuple(cell.trade_date for cell in window.cells if cell.unadjusted is None),
                tuple(cell.trade_date for cell in window.cells if cell.qfq is None),
            )
