"""Locked disk seeding and resumable online window refresh."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from trader.download.application.progress_message import ProgressLevel
from trader.download.application.qfq_download_progress import QfqFailureCode, QfqMessage
from trader.download.application.progress_message import ProgressMessage
from trader.download.application.read_published_history import ReadPublishedHistoryUseCase
from trader.download.application.update_qfq import UpdateQfqWindows
from trader.download.domain.baostock_daily import BaoStockCalendar
from trader.download.domain.qfq_window import QfqUpdateResult
from trader.download.domain.prepared_reference import PreparedReference
from trader.download.domain.published_history import PublishedHistoryManifest, PublishedHistoryWindow
from trader.download.infra.history_control_index import HistoryMaintenanceLock
from trader.download.infra.qfq_progress import QfqLocalProgress
from trader.download.infra.qfq_sqlite import SQLiteQfqWindowCache
from trader.download.application.download_failure import DownloadFailurePort
from trader.download.domain.download_failure import DownloadFailureContext
from trader.infra.shutdown import ShutdownDeadline
from trader.infra.workers import ManagedWorkerExecutor

_SHANGHAI = ZoneInfo("Asia/Shanghai")


class _SeedCancelledError(RuntimeError):
    """Stop a local history read without discarding earlier seed receipts."""


@dataclass(frozen=True)
class QfqShutdown:
    writer: ManagedWorkerExecutor
    downloads: ManagedWorkerExecutor
    report: Callable[[ProgressMessage], None]
    failures: DownloadFailurePort


def StopQfqWorkers(
    resources: QfqShutdown,
    timeout_seconds: float,
    started_at: float,
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Drain explicitly owned paired workers under one deadline and report failure."""
    deadline = ShutdownDeadline.start(timeout_seconds, monotonic=monotonic)
    writer_stopped = resources.writer.stop(wait=True, cancel_futures=True, deadline=deadline)
    stopped = resources.downloads.stop(wait=True, cancel_futures=True, deadline=deadline)
    if not stopped.completed or not writer_stopped.completed:
        resources.failures(
            DownloadFailureContext("shutdown", elapsed_seconds=monotonic() - started_at),
            RuntimeError("qfq_worker_deadline_exceeded"),
        )
        resources.report(QfqMessage(
            "关闭失败", "worker_deadline_exceeded", level=ProgressLevel.ERROR,
        ))


@dataclass(frozen=True)
class QfqUpdateRunner:
    history: ReadPublishedHistoryUseCase
    updater: UpdateQfqWindows
    v2: SQLiteQfqWindowCache
    v3: SQLiteQfqWindowCache
    lock_path: Path
    now: Callable[[], datetime]
    failures: DownloadFailurePort | None = None

    def execute(self, *, seed_only: bool = False, reference: PreparedReference | None = None) -> QfqUpdateResult:
        started = self.updater.monotonic()
        changed: set[str] = set()
        rows = 0
        stage = "maintenance_lock"
        result: QfqUpdateResult | None = None
        try:
            with HistoryMaintenanceLock(self.lock_path):
                stage = "initial_integrity"
                if Path(f"{self.v2.path}.sha256").is_file():
                    self.v2.verify_integrity()
                if not seed_only:
                    stage = "update"
                    result = self.updater.execute(self.now())
                    changed.update(result.changed_files)
                    rows += result.changed_rows
                    return result
                stage = "current_eligibility"
                observed_at = self.now()
                if observed_at.tzinfo is None or observed_at.utcoffset() is None:
                    raise ValueError("qfq_clock_timezone_required")
                observed_at = observed_at.astimezone(_SHANGHAI)
                if (
                    reference is None or reference.checked_at.date() != observed_at.date()
                    or reference.checked_at > observed_at
                ):
                    raise RuntimeError("qfq_current_reference_required")
                allowed_codes = frozenset(item.code for item in reference.eligible_universe)
                if not allowed_codes:
                    raise RuntimeError("qfq_eligible_population_empty")
                if self.updater.cancel_requested():
                    return QfqUpdateResult(None, failure_reason="cancelled")
                stage = "history_manifest"
                manifest = self.history.manifest()
                if manifest is None:
                    raise RuntimeError("qfq_history_unavailable")
                selected = allowed_codes & frozenset(manifest.universe_codes)
                stage = "select_population"
                if any(
                    state.end_date is not None and state.end_date > manifest.data_cutoff
                    for cache in (self.v2, self.v3) for state in cache.inspect_windows(selected).states
                ):
                    raise RuntimeError("qfq_seed_target_regressed")
                files, changed_rows = self.v2.select_published_codes(self.v3, selected)
                changed.update(files)
                rows += changed_rows
                needed = selected - self._reusable_seed_codes(manifest, selected, reference)
                if needed:
                    stage = "local_seed"
                    self.updater.report(
                        QfqMessage(
                            "历史提取",
                            f"待提取 {len(needed)} 只",
                        )
                    )
                    with QfqLocalProgress(self.updater.report):
                        seeded = self.updater.seed(
                            self._seed_windows(manifest, needed),
                            f"history:{manifest.snapshot_hash}",
                            calendar=manifest.calendar_dates,
                            nontrading_observed_on=observed_at.date(),
                        )
                    changed.update(seeded.changed_files)
                    rows += seeded.changed_rows
                completed_codes = self._reusable_seed_codes(manifest, selected, reference)
                return QfqUpdateResult(
                    manifest.data_cutoff,
                    len(completed_codes & allowed_codes),
                    pending_codes=len(allowed_codes - completed_codes),
                    changed_files=tuple(sorted(changed)),
                    changed_rows=rows,
                    failure_reason="cancelled" if self.updater.cancel_requested() else None,
                )
        except (RuntimeError, OSError, ValueError, sqlite3.Error) as exc:
            if self.failures is not None and not self.updater.cancel_requested():
                self.failures(DownloadFailureContext(stage, elapsed_seconds=self.updater.monotonic() - started), exc)
            reason = QfqFailureCode(exc)
            self.updater.report(
                QfqMessage("更新失败", reason, level=ProgressLevel.ERROR)
            )
            if result is not None:
                return replace(
                    result, changed_files=tuple(sorted(changed)), changed_rows=rows, failure_reason=reason
                )
            return QfqUpdateResult(None, changed_files=tuple(sorted(changed)), changed_rows=rows, failure_reason=reason)

    def _reusable_seed_codes(
        self, manifest: PublishedHistoryManifest, selected: frozenset[str], reference: PreparedReference
    ) -> frozenset[str]:
        calendar = BaoStockCalendar(manifest.calendar_dates[-self.updater.configuration.maximum_sessions:])
        target_dates = frozenset(calendar.open_dates)
        securities = {security.code: security for security in reference.eligible_universe}
        snapshots = []
        for cache in (self.v2, self.v3):
            expected = manifest.calendar_dates[-self.updater.configuration.sessions_for(cache.profile):]
            current = cache.manifest()
            if current is None or current.calendar_dates != expected:
                return frozenset()
            snapshots.append(cache.inspect_windows(selected))
        unresolved = frozenset(
            issue.code for issue in self.v2.price_issues()
            if any(day in target_dates for day in issue.dates)
        )
        reusable: set[str] = set()
        for code in selected - unresolved:
            first, second = (snapshot.state_for(code) for snapshot in snapshots)
            if first is None or second is None or first.source != second.source:
                continue
            if all(
                state.verified
                and state.start_date == manifest.calendar_dates[-self.updater.configuration.sessions_for(cache.profile):][0]
                and state.end_date == manifest.data_cutoff
                for cache, state in ((self.v2, first), (self.v3, second))
            ):
                expected = calendar.expected_dates(securities[code])
                # Bounds do not prove daily coverage: an absent row is not
                # suspension evidence. The shared price view also includes
                # explicitly recorded nontrading dates, within listing bounds.
                if expected and frozenset(expected).issubset(self.v2.covered_dates(code)):
                    reusable.add(code)
        return frozenset(reusable)

    def _check_seed_cancelled(self) -> None:
        if self.updater.cancel_requested():
            raise _SeedCancelledError("cancelled")

    def _seed_windows(
        self, manifest: PublishedHistoryManifest, needed: frozenset[str]
    ) -> Iterator[PublishedHistoryWindow]:
        ordered = tuple(sorted(needed))
        for start in range(0, len(ordered), 32):
            if self.updater.cancel_requested():
                return
            try:
                windows = self.history.read_windows(
                    manifest, ordered[start:start + 32], sessions=min(2000, len(manifest.calendar_dates)),
                    check=self._check_seed_cancelled,
                )
            except _SeedCancelledError:
                return
            yield from windows
