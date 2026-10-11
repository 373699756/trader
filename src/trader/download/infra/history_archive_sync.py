"""Crash-safe orchestration for the zero-argument monthly history archive."""

from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
import time as monotonic_time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, wait
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Literal, TypeAlias
from zoneinfo import ZoneInfo

from trader.download.domain.baostock_daily import (
    BaoStockDailyCell,
    BaoStockSecurity,
)
from trader.download.domain.history_control import (
    HistoryActiveSnapshot,
    HistoryCalendarIdentity,
    HistoryControlState,
    HistoryDiskRequirement,
    HistorySecurityIdentity,
    HistorySnapshotPartition,
    HistorySourceIdentity,
    HistorySyncCheckpoint,
    HistoryUniverseIdentity,
)
from trader.download.domain.history_maintenance import HistoryMaintenanceState, HistoryMaintenanceStatus
from trader.download.domain.history_price_qualification import (
    HISTORY_TAIL_CONTRACT,
    HISTORY_UNIVERSE_CONTRACT,
    qualify_history_pairs,
    require_history_qfq_overlap,
)
from trader.download.domain.history_reference import HistoryReferenceSnapshot
from trader.download.domain.history_revision import HistoryRevision
from trader.download.domain.history_sync import (
    HistorySupplierContext,
    HistorySyncConfiguration,
    HistorySyncProgress,
    HistorySyncProgressPort,
    HistorySyncProgressStage,
    HistoryTwoStageSupplier,
)
from trader.download.domain.published_history import PublishedHistoryWindow
from trader.download.infra.history_active_gap_scan import scan_active_price_gaps
from trader.download.infra.history_archive_reader import route_history_months
from trader.download.infra.history_archive_repack import (
    HistoryArchiveRepackFenceError,
    require_history_archive_repack_inactive,
)
from trader.download.infra.history_control_repository import (
    HistoryControlError,
    HistoryMaintenanceAlreadyRunningError,
    HistoryMaintenanceLock,
    SQLiteHistoryControlRepository,
    inspect_history_disk,
)
from trader.download.infra.history_gap_recovery import (
    HistoryGapRecovery,
    HistoryGapRecoveryContext,
    recover_selected_history_gaps,
)
from trader.download.infra.history_month_partition import (
    HistoryMonthPartitionError,
    SQLiteHistoryMonthPartitionRepository,
)
from trader.download.infra.history_pending_prices import (
    PendingHistoryPartitions,
    backup_database,
    partition_month,
    prepare_pending,
    write_revision_batch,
)
from trader.download.infra.history_reference_files import (
    history_industry_mapping_path,
    read_bound_history_reference,
    write_history_reference,
)
from trader.download.infra.history_seal_resume import (
    promote_completed_candidate,
    record_sealed_month,
    recover_sealed_months,
    remove_sqlite_files,
)
from trader.download.infra.history_snapshot_publication import (
    HistoryPublication,
    is_reference_only_change,
    publish_history_snapshot,
)
from trader.download.infra.history_tencent_stage import HistoryTencentStage
from trader.infra.workers import WorkerExecutor, injected_executor, submit_or_reject
from trader.training.domain.evaluation.artifact_identity import canonical_artifact_hash
from trader.training.infra.history.history_training_due import HistoryTrainingDueQuery, evaluate_history_training_due
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE

Clock: TypeAlias = Callable[[], datetime]
Cancellation: TypeAlias = Callable[[], bool]
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_BAOSTOCK_DAILY_READY = time(15, 10)
_ERROR_CODE = re.compile(r"[a-z0-9_]{1,64}")
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _CodeDownloadContext:
    configuration: HistorySyncConfiguration
    supplier_context: HistorySupplierContext
    active: HistoryActiveSnapshot | None
    previous_codes: frozenset[str]
    previous_gaps: dict[str, tuple[date, ...]]


@dataclass(frozen=True)
class _PartitionReplacement:
    destination: Path
    rollback: Path


@dataclass(frozen=True)
class _TencentDownloadResult:
    security: BaoStockSecurity
    dates: tuple[date, ...]
    window: PublishedHistoryWindow | None
    failure_reason: str | None


@dataclass(frozen=True)
class _ProgressOutcome:
    state: Literal["started", "completed", "failed"]
    failure_reason: str | None = None


@dataclass(frozen=True)
class _SupplementResult:
    index: int
    code: str
    dates: tuple[date, ...]
    revisions: tuple[HistoryRevision, ...]
    unresolved_price_cells: int


@dataclass(frozen=True)
class _SealedPartitions:
    references: tuple[HistorySnapshotPartition, ...]
    replacements: tuple[_PartitionReplacement, ...]


def run_history_sync(  # noqa: PLR0913 - explicit external resource injection
    configuration: HistorySyncConfiguration,
    supplier: HistoryTwoStageSupplier,
    *,
    clock: Clock | None = None,
    cancel_requested: Cancellation | None = None,
    progress: HistorySyncProgressPort | None = None,
    worker_pool: WorkerExecutor | None = None,
) -> HistoryMaintenanceStatus:
    """Synchronize and publish one complete immutable snapshot."""
    observed_at = (clock or (lambda: datetime.now(_SHANGHAI)))()
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("history synchronization clock must be timezone-aware")
    observed_at = observed_at.astimezone(_SHANGHAI)
    cancel = cancel_requested or (lambda: False)
    root = configuration.archive_root
    _publish_progress(progress, "initializing", "started")
    try:
        with HistoryMaintenanceLock(root / ".maintenance.lock"):
            require_history_archive_repack_inactive(root)
            return _run_locked(configuration, supplier, observed_at, cancel, progress, worker_pool)
    except KeyboardInterrupt:
        active = _safe_active(SQLiteHistoryControlRepository(root / "control.sqlite3"))
        return _status("cancelled", "cancelled", configuration, active, observed_at)
    except HistoryMaintenanceAlreadyRunningError:
        _publish_progress(progress, "initializing", "failed")
        active = _safe_active(SQLiteHistoryControlRepository(root / "control.sqlite3"))
        return _status("already_running", "history_maintenance_running", configuration, active, observed_at)
    except HistoryArchiveRepackFenceError:
        _publish_progress(progress, "initializing", "failed")
        active = _safe_active(SQLiteHistoryControlRepository(root / "control.sqlite3"))
        return _status("blocked", "history_archive_repack_activation_pending", configuration, active, observed_at)


def _run_locked(  # noqa: PLR0913 - explicit external resource injection
    configuration: HistorySyncConfiguration,
    supplier: HistoryTwoStageSupplier,
    observed_at: datetime,
    cancel_requested: Cancellation,
    progress: HistorySyncProgressPort | None,
    worker_pool: WorkerExecutor | None,
) -> HistoryMaintenanceStatus:
    root = configuration.archive_root
    control = SQLiteHistoryControlRepository(root / "control.sqlite3")
    try:
        control.initialize()
        state = control.load_state()
        _publish_progress(progress, "initializing", "completed", (1, 1))
        active = state.active_snapshot
        _recover_reference_mapping(root, active, control)
        _recover_partition_replacements(root, active)
        if active is None and not _has_sufficient_disk(root, configuration.minimum_free_bytes):
            return _status("blocked", "disk_space_insufficient", configuration, None, observed_at)
        _publish_progress(progress, "loading_context", "started")
        try:
            context = supplier.load_context(_latest_completed_daily_date(observed_at), configuration.sessions)
        except KeyboardInterrupt:
            _publish_progress(progress, "loading_context", "cancelled")
            raise
        except (OSError, RuntimeError, TypeError, ValueError):
            _publish_progress(progress, "loading_context", "failed")
            raise
        _publish_progress(progress, "loading_context", "completed", (1, 1))
        _validate_context(context, configuration.sessions)
        source, calendar, universe = _control_identities(context)
        reference = HistoryReferenceSnapshot(
            tuple(item.code for item in context.universe), context.industry_intervals, context.st_evidence
        )
        previous_codes = _active_universe(control, active)
        if (
            not previous_codes.issubset(item.code for item in universe.securities)
            and HISTORY_UNIVERSE_CONTRACT not in context.source_versions.dependency_versions
        ):
            raise RuntimeError("supplier_universe_regressed")
        previous_gaps = scan_active_price_gaps(
            root,
            active,
            context,
            cancel_requested,
            lambda state, completed, total, item: _publish_progress(
                progress, "history_active_gaps", state, (completed, total), item
            ),
        )
        if (
            active is not None
            and not previous_gaps
            and is_reference_only_change(state, active, source, calendar, universe)
        ):
            sync_identity = _sync_identity(calendar, universe, active, source, state)
            checkpoints = _matching_checkpoints(state.checkpoints, sync_identity)
            ordinal = max((item.ordinal for item in checkpoints), default=0) + 1
            total = len(context.universe)
            snapshot = HistoryActiveSnapshot(
                active.sequence + 1,
                calendar.open_dates[-1],
                calendar.open_dates[-2] if len(calendar.open_dates) > 1 else calendar.open_dates[-1],
                calendar.content_hash,
                universe.content_hash,
                source.content_hash,
                active.partitions,
            )
            _publish_progress(progress, "publishing_snapshot", "started")
            publish_history_snapshot(
                control,
                HistoryPublication(
                    root,
                    (source, calendar, universe),
                    snapshot,
                    HistorySyncCheckpoint(sync_identity, ordinal, "completed", observed_at, total, total, None),
                    reference,
                ),
            )
            _publish_progress(progress, "publishing_snapshot", "completed", (1, 1))
            return _status("completed", None, configuration, snapshot, observed_at)
        if _is_current(active, calendar, universe) and not previous_gaps:
            write_history_reference(history_industry_mapping_path(root), reference)
            return _status("already_current", None, configuration, active, observed_at)
        if not _has_sufficient_disk(root, configuration.minimum_free_bytes):
            return _status("blocked", "disk_space_insufficient", configuration, active, observed_at)
        return _synchronize(
            configuration,
            supplier,
            observed_at,
            cancel_requested,
            control,
            active,
            context,
            source,
            calendar,
            universe,
            progress,
            worker_pool,
            previous_gaps,
            reference,
        )
    except (HistoryControlError, OSError, RuntimeError, TypeError, ValueError) as exc:
        active = _safe_active(control)
        return _status("failed", _failure_code(exc), configuration, active, observed_at)


def _has_sufficient_disk(root: Path, minimum_free_bytes: int) -> bool:
    return inspect_history_disk(root, HistoryDiskRequirement(minimum_free_bytes, 0, 0, 0)).sufficient


def _synchronize(  # noqa: PLR0913
    configuration: HistorySyncConfiguration,
    supplier: HistoryTwoStageSupplier,
    observed_at: datetime,
    cancel_requested: Cancellation,
    control: SQLiteHistoryControlRepository,
    active: HistoryActiveSnapshot | None,
    context: HistorySupplierContext,
    source: HistorySourceIdentity,
    calendar: HistoryCalendarIdentity,
    universe: HistoryUniverseIdentity,
    progress: HistorySyncProgressPort | None,
    worker_pool: WorkerExecutor | None,
    previous_gaps: dict[str, tuple[date, ...]],
    reference: HistoryReferenceSnapshot,
) -> HistoryMaintenanceStatus:
    sequence = 1 if active is None else active.sequence + 1
    state = control.load_state()
    sync_identity = _sync_identity(calendar, universe, active, source, state)
    checkpoints = _matching_checkpoints(state.checkpoints, sync_identity)
    completed = max((item.completed_units for item in checkpoints), default=0)
    ordinal = max((item.ordinal for item in checkpoints), default=0) + 1
    total = len(context.universe)
    try:
        control.save_source(source)
        control.save_calendar(calendar)
        control.save_universe(universe)
        _publish_progress(progress, "preparing_partitions", "started")
        pending = prepare_pending(configuration.archive_root, calendar.open_dates, active, completed > 0)
        _publish_progress(progress, "preparing_partitions", "completed", (1, 1))
        if not checkpoints:
            control.save_checkpoint(
                HistorySyncCheckpoint(sync_identity, ordinal, "running", observed_at, 0, total, None)
            )
            ordinal += 1
        old_universe = _active_universe(control, active)
        download_context = _CodeDownloadContext(
            configuration,
            context,
            active,
            old_universe,
            previous_gaps,
        )
        stage = HistoryTencentStage(
            configuration.archive_root / ".tencent-stage" / f"{sync_identity.rsplit('-', 1)[-1]}.sqlite3",
            sync_identity,
        )
        stage.initialize()
        executor = worker_pool if worker_pool is not None else injected_executor(None)
        phases = _HistoryDownloadPhases(
            download_context,
            supplier,
            stage,
            pending,
            control,
            executor,
            observed_at,
            sync_identity,
            sequence,
            ordinal,
            completed,
            progress,
            cancel_requested,
        )
        phases.download_tencent()
        completed, ordinal = phases.completed, phases.ordinal
        phases.build_gap_inventory()
        phases.supplement_baostock()
        completed, ordinal = phases.completed, phases.ordinal
        phases.check_cancel()
        unresolved = phases.unresolved_price_cells()
        snapshot = _seal_and_publish(
            configuration.archive_root,
            pending,
            control,
            (source, calendar, universe),
            sequence,
            sync_identity,
            ordinal,
            total,
            observed_at,
            progress,
            reference,
            stage,
        )
        _publish_progress(progress, "publishing_snapshot", "completed", (1, 1))
        try:
            _remove_pending(pending)
        except OSError:
            _LOGGER.warning("history_pending_cleanup_failed")
        try:
            stage.clear()
        except OSError:
            _LOGGER.warning("history_stage_cleanup_failed")
        return _status(
            "completed", None, configuration, snapshot, observed_at, unresolved_price_cells=max(0, unresolved)
        )
    except (KeyboardInterrupt, _HistoryDownloadCancelledError):
        _publish_progress(progress, "downloading_codes", "cancelled", (completed, total))
        # A completed wave/batch may have advanced its durable checkpoint before cancellation.
        ordinal = max((item.ordinal for item in control.load_state().checkpoints), default=ordinal) + 1
        control.save_checkpoint(
            HistorySyncCheckpoint(sync_identity, ordinal, "cancelled", observed_at, completed, total, "cancelled")
        )
        return _status("cancelled", "cancelled", configuration, active, observed_at)
    except (OSError, RuntimeError, TypeError, ValueError, sqlite3.Error) as exc:
        reason = "cancelled" if cancel_requested() else _failure_code(exc)
        ordinal = max((item.ordinal for item in control.load_state().checkpoints), default=ordinal) + 1
        control.save_checkpoint(
            HistorySyncCheckpoint(
                sync_identity,
                ordinal,
                "cancelled" if reason == "cancelled" else "failed",
                observed_at,
                completed,
                total,
                reason,
            )
        )
        return _status("cancelled" if reason == "cancelled" else "failed", reason, configuration, active, observed_at)


class _HistoryDownloadCancelledError(RuntimeError):
    """Stop orchestration while retaining only previously durable results."""


@dataclass
class _HistoryDownloadPhases:
    context: _CodeDownloadContext
    supplier: HistoryTwoStageSupplier
    stage: HistoryTencentStage
    pending: PendingHistoryPartitions
    control: SQLiteHistoryControlRepository
    executor: WorkerExecutor
    observed_at: datetime
    sync_identity: str
    sequence: int
    ordinal: int
    completed: int
    progress: HistorySyncProgressPort | None
    cancel_requested: Cancellation
    recovery_selection: frozenset[str] = frozenset()
    recoveries: tuple[HistoryGapRecovery, ...] = ()

    def check_cancel(self) -> None:
        if self.cancel_requested():
            raise _HistoryDownloadCancelledError("cancelled")

    def checkpoint(self, completed: int) -> None:
        self.completed = completed
        self.control.save_checkpoint(
            HistorySyncCheckpoint(
                self.sync_identity,
                self.ordinal,
                "running",
                self.observed_at,
                completed,
                len(self.context.supplier_context.universe),
                None,
            )
        )
        self.ordinal += 1

    def download_tencent(self) -> None:
        universe = self.context.supplier_context.universe
        batch_size = self.context.configuration.history_workers
        completed_codes = self.stage.completed_codes()
        self.completed = sum(security.code in completed_codes for security in universe)
        jobs = tuple(
            (index, security) for index, security in enumerate(universe) if security.code not in completed_codes
        )
        futures: dict[Future[PublishedHistoryWindow], tuple[int, BaoStockSecurity, tuple[date, ...]]] = {}
        durable_batch: list[_TencentDownloadResult] = []
        next_job = 0
        try:
            while futures or next_job < len(jobs):
                self.check_cancel()
                next_job = self._fill_tencent_workers(jobs, next_job, futures, durable_batch, batch_size)
                if len(durable_batch) >= batch_size:
                    self._save_tencent_batch(durable_batch[:batch_size])
                    del durable_batch[:batch_size]
                    continue
                if not futures:
                    break
                done, _pending = wait(tuple(futures), return_when=FIRST_COMPLETED)
                self.check_cancel()
                for future in done:
                    _index, security, dates = futures.pop(future)
                    durable_batch.append(self._tencent_result(future, security, dates))
                if len(durable_batch) >= batch_size:
                    self._save_tencent_batch(durable_batch[:batch_size])
                    del durable_batch[:batch_size]
            if durable_batch:
                self._save_tencent_batch(durable_batch)
        finally:
            for future in futures:
                future.cancel()
        summary = self.stage.tencent_summary()
        if self.progress is not None:
            try:
                self.progress.publish(
                    HistorySyncProgress(
                        "tencent_history", "completed", summary.processed_codes, len(universe), tencent_summary=summary
                    )
                )
            except OSError:
                pass

    def _fill_tencent_workers(
        self,
        jobs: tuple[tuple[int, BaoStockSecurity], ...],
        next_job: int,
        futures: dict[Future[PublishedHistoryWindow], tuple[int, BaoStockSecurity, tuple[date, ...]]],
        durable_batch: list[_TencentDownloadResult],
        batch_size: int,
    ) -> int:
        while len(futures) < batch_size and next_job < len(jobs):
            index, security = jobs[next_job]
            next_job += 1
            dates = _requested_dates(self.context, security)
            self._price_progress("tencent_history", _ProgressOutcome("started"), index, security.code, dates)
            if dates:
                future = submit_or_reject(self.executor, self.supplier.fetch_tencent_window, security, dates)
                futures[future] = (index, security, dates)
            else:
                durable_batch.append(
                    _TencentDownloadResult(security, dates, PublishedHistoryWindow(security.code, ()), None)
                )
                if len(durable_batch) >= batch_size:
                    break
        return next_job

    def _tencent_result(
        self, future: Future[PublishedHistoryWindow], security: BaoStockSecurity, dates: tuple[date, ...]
    ) -> _TencentDownloadResult:
        try:
            window = future.result()
            self.check_cancel()
            if window.code != security.code or tuple(cell.trade_date for cell in window.cells) != dates:
                raise RuntimeError("history_tencent_coverage_incomplete")
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self.check_cancel()
            return _TencentDownloadResult(security, dates, None, _failure_code(exc))
        return _TencentDownloadResult(security, dates, window, None)

    def _save_tencent_batch(self, results: list[_TencentDownloadResult]) -> None:
        self.check_cancel()
        self.stage.save_batch(
            tuple(result.window for result in results if result.window is not None),
            tuple(
                (result.security.code, result.failure_reason) for result in results if result.failure_reason is not None
            ),
        )
        self.completed += len(results)
        self.checkpoint(self.completed)
        first_completed = self.completed - len(results) + 1
        for offset, result in enumerate(results):
            self._price_progress(
                "tencent_history",
                _ProgressOutcome("failed" if result.failure_reason is not None else "completed", result.failure_reason),
                first_completed + offset,
                result.security.code,
                result.dates,
            )

    def supplement_baostock(self) -> None:
        cap = self.context.configuration.baostock_max_codes
        recovery_order = self.stage.prioritized_recovery_codes(self.sequence, cap)
        self.recovery_selection = frozenset(recovery_order)
        universe = self.context.supplier_context.universe
        supplemented = self.stage.supplemented_codes()
        if all(security.code in supplemented for security in universe):
            return
        self.recoveries = recover_selected_history_gaps(
            HistoryGapRecoveryContext(
                self.stage,
                self.supplier,
                universe,
                tuple(code for code in recovery_order if code not in supplemented),
                self.context.configuration,
                self.progress,
                self.cancel_requested,
                monotonic_time.monotonic,
            )
        )
        batch_size = self.context.configuration.download_batch_size
        for start in range(0, len(universe), batch_size):
            self.check_cancel()
            end = min(start + batch_size, len(universe))
            results: list[_SupplementResult] = []
            for index, security in enumerate(universe[start:end], start=start):
                if security.code not in supplemented:
                    results.append(self._supplement_security(index, security))
            if not results:
                continue
            write_revision_batch(
                self.pending,
                tuple(revision for result in results for revision in result.revisions),
                self.progress,
            )
            self.stage.mark_supplemented(tuple((result.code, result.unresolved_price_cells) for result in results))
            self.checkpoint(end)
            for result in results:
                self._price_progress(
                    "persisting_prices", _ProgressOutcome("completed"), result.index + 1, result.code, result.dates
                )

    def build_gap_inventory(self) -> None:
        total = len(self.context.supplier_context.universe)
        _publish_progress(self.progress, "history_gap_inventory", "started", (0, total))
        self.check_cancel()
        if not self.stage.has_gap_inventory():
            requested = (
                (security.code, _requested_dates(self.context, security))
                for security in self.context.supplier_context.universe
            )
            self.stage.save_gap_inventory(
                self.stage.iter_gap_inventory(
                    requested,
                    self.context.configuration.reread_sessions,
                    self.cancel_requested,
                )
            )
        if self.progress is not None:
            try:
                self.progress.publish(
                    HistorySyncProgress(
                        "history_gap_inventory",
                        "completed",
                        total,
                        total,
                        gap_summary=self.stage.gap_summary(),
                    )
                )
            except OSError:
                pass

    def _supplement_security(self, index: int, security: BaoStockSecurity) -> _SupplementResult:
        self.check_cancel()
        dates = _requested_dates(self.context, security)
        self._price_progress("persisting_prices", _ProgressOutcome("started"), index, security.code, dates)
        if not dates:
            return _SupplementResult(index, security.code, dates, (), 0)
        tencent, _, _ = self.stage.read_supplement_context(security.code)
        recovery = next((item.cells for item in self.recoveries if item.code == security.code), ())
        self.check_cancel()
        cells = qualify_history_pairs(security.code, dates, tencent, recovery)
        context = self.context
        if (
            context.active is not None
            and security.code in context.previous_codes
            and HISTORY_TAIL_CONTRACT in context.supplier_context.source_versions.dependency_versions
        ):
            cells = _validate_tail_overlap(context, cells, security.code)
        return _SupplementResult(
            index,
            security.code,
            dates,
            tuple(HistoryRevision(self.sequence, security.board, cell, None, None, None) for cell in cells),
            sum(not cell.obtained for cell in cells),
        )

    def unresolved_price_cells(self) -> int:
        known, unknown = self.stage.unresolved_price_counts()
        if unknown:
            before_recovery = self.stage.pre_recovery_price_gap_counts()
            recovery_codes = unknown & self.recovery_selection
            inferred = {code: before_recovery.get(code, 0) for code in unknown - recovery_codes}
            if recovery_codes:
                recovered = _pending_price_gap_counts_for_codes(self.pending, self.sequence, recovery_codes)
                inferred.update({code: recovered.get(code, 0) for code in recovery_codes})
            self.stage.mark_supplemented(tuple(sorted(inferred.items())))
            known.update(inferred)
        return sum(known.values())

    def _price_progress(
        self,
        phase: Literal["tencent_history", "persisting_prices"],
        outcome: _ProgressOutcome,
        completed: int,
        code: str,
        dates: tuple[date, ...],
    ) -> None:
        if self.progress is None:
            return
        try:
            self.progress.publish(
                HistorySyncProgress(
                    phase,
                    outcome.state,
                    completed,
                    len(self.context.supplier_context.universe),
                    code,
                    supplier_source="tencent" if dates and phase == "tencent_history" else None,
                    requested_sessions=len(dates) if dates and phase != "persisting_prices" else None,
                    failure_reason=outcome.failure_reason,
                )
            )
        except OSError:
            pass


def _seal_and_publish(  # noqa: PLR0913
    root: Path,
    pending: PendingHistoryPartitions,
    control: SQLiteHistoryControlRepository,
    identities: tuple[HistorySourceIdentity, HistoryCalendarIdentity, HistoryUniverseIdentity],
    sequence: int,
    sync_identity: str,
    ordinal: int,
    total: int,
    observed_at: datetime,
    progress: HistorySyncProgressPort | None,
    reference: HistoryReferenceSnapshot,
    stage: HistoryTencentStage,
) -> HistoryActiveSnapshot:
    source, calendar, universe = identities
    sealed = _seal_pending(root, pending, stage, progress)
    _publish_progress(progress, "publishing_snapshot", "started")
    label_cutoff = calendar.open_dates[-2] if len(calendar.open_dates) > 1 else calendar.open_dates[-1]
    snapshot = HistoryActiveSnapshot(
        sequence,
        calendar.open_dates[-1],
        label_cutoff,
        calendar.content_hash,
        universe.content_hash,
        source.content_hash,
        sealed.references,
    )
    try:
        publish_history_snapshot(
            control,
            HistoryPublication(
                root,
                identities,
                snapshot,
                HistorySyncCheckpoint(sync_identity, ordinal, "completed", observed_at, total, total, None),
                reference,
            ),
        )
    except BaseException:
        published = control.load_published_state()
        if published is None or published.snapshot.content_hash != snapshot.content_hash:
            _restore_partition_replacements(sealed.replacements)
        raise
    _discard_partition_replacements(sealed.replacements)
    return snapshot


def _recover_reference_mapping(
    root: Path,
    active: HistoryActiveSnapshot | None,
    control: SQLiteHistoryControlRepository,
) -> None:
    mapping_path = history_industry_mapping_path(root)
    rollback = mapping_path.with_name(".industry_mapping.rollback.json")
    if not rollback.is_file():
        return
    current_matches = False
    if active is not None:
        try:
            source = control.read_source(active.source_identity_hash)
            read_bound_history_reference(root, source.supplier_contract)
            current_matches = True
        except (HistoryControlError, OSError, ValueError):
            pass
    if current_matches:
        rollback.unlink(missing_ok=True)
        return
    os.replace(rollback, mapping_path)
    _fsync_directory(mapping_path.parent)


def _requested_dates(context: _CodeDownloadContext, security: BaoStockSecurity) -> tuple[date, ...]:
    expected = context.supplier_context.calendar.expected_dates(security)
    if not expected:
        return ()
    if (
        context.active is not None
        and security.delisted_on is not None
        and security.delisted_on <= context.supplier_context.calendar.open_dates[-1]
    ):
        return ()
    if context.active is None or security.code not in context.previous_codes:
        requested = expected
    else:
        requested = tuple(
            sorted(
                set(expected[-context.configuration.reread_sessions :])
                | {day for day in expected if day > context.active.data_cutoff}
                | (set(context.previous_gaps.get(security.code, ())) & set(expected))
            )
        )
        if HISTORY_TAIL_CONTRACT in context.supplier_context.source_versions.dependency_versions:
            anchor = tuple(day for day in expected if day <= context.active.data_cutoff)
            requested = tuple(sorted(set(requested) | set(anchor[-context.configuration.reread_sessions :])))
    return requested


def _validate_tail_overlap(
    context: _CodeDownloadContext, cells: tuple[BaoStockDailyCell, ...], code: str
) -> tuple[BaoStockDailyCell, ...]:
    active = context.active
    assert active is not None
    overlap = tuple(cell for cell in cells if cell.trade_date <= active.data_cutoff)
    if not overlap:
        return tuple(BaoStockDailyCell(code, cell.trade_date, "unknown_missing", None, None) for cell in cells)
    prior: dict[date, BaoStockDailyCell] = {}
    anchored = False
    for year, month in route_history_months(overlap[0].trade_date, overlap[-1].trade_date):
        reference = next((item for item in active.partitions if partition_month(item) == (year, month)), None)
        if reference is None:
            raise RuntimeError("history_tail_qfq_overlap_missing")
        # Trust the published identity and read only this stock's bounded overlap via its index.
        partition = SQLiteHistoryMonthPartitionRepository(
            context.configuration.archive_root / reference.relative_path,
            year,
            month,
        )
        rows = partition.read_code(
            code,
            overlap[0].trade_date,
            overlap[-1].trade_date,
            snapshot_sequence=active.sequence,
        )
        prior.update((row.trade_date, row.cell) for row in rows)
        for cell in (item for item in overlap if (item.trade_date.year, item.trade_date.month) == (year, month)):
            previous = next((row.cell for row in rows if row.trade_date == cell.trade_date), None)
            if previous is not None and previous.obtained and cell.obtained:
                require_history_qfq_overlap(previous, cell)
                anchored = True
    security = next(item for item in context.supplier_context.universe if item.code == code)
    if tuple(cell.trade_date for cell in cells) == context.supplier_context.calendar.expected_dates(security):
        anchored = True
    return tuple(
        prior[cell.trade_date]
        if not cell.obtained and cell.trade_date in prior and prior[cell.trade_date].obtained
        else cell
        if anchored or not cell.obtained
        else BaoStockDailyCell(code, cell.trade_date, "unknown_missing", None, None)
        for cell in cells
    )


def _seal_pending(
    root: Path,
    pending: PendingHistoryPartitions,
    stage: HistoryTencentStage,
    progress: HistorySyncProgressPort | None = None,
) -> _SealedPartitions:
    references = recover_sealed_months(root, pending.paths, stage, progress)
    replacements: list[_PartitionReplacement] = []
    total = len(pending.paths)
    try:
        remaining = sorted((month, path) for month, path in pending.paths.items() if month not in references)
        for index, ((year, month), path) in enumerate(remaining, start=len(references)):
            current_item = f"{year:04d}-{month:02d}"
            _publish_progress(progress, "sealing_partitions", "started", (index, total), current_item)
            if path.is_file():
                candidate = path.with_name(f".{month:02d}.seal.sqlite3")
                _stage_partition_for_seal(path, candidate)
                destination = root / "partitions" / f"{year:04d}" / f"{month:02d}.sqlite3"
                replacement = (
                    _create_partition_rollback(destination) if (year, month) in pending.active_by_month else None
                )
                if replacement is not None:
                    replacements.append(replacement)
                reference = promote_completed_candidate(stage, (year, month), candidate, destination)
                if reference is None:
                    reference = SQLiteHistoryMonthPartitionRepository(candidate, year, month).seal()
                record_sealed_month(stage, (year, month), destination, candidate, reference)
            else:
                existing_reference = pending.active_by_month.get((year, month))
                if existing_reference is None:
                    raise RuntimeError("history_snapshot_month_missing")
                reference = existing_reference
            references[(year, month)] = reference
            _publish_progress(progress, "sealing_partitions", "completed", (index + 1, total), current_item)
    except BaseException:
        _restore_partition_replacements(tuple(replacements))
        raise
    return _SealedPartitions(tuple(references[month] for month in sorted(references)), tuple(replacements))


def _stage_partition_for_seal(source: Path, candidate: Path) -> None:
    remove_sqlite_files(candidate)
    wal = Path(f"{source}-wal")
    if not wal.exists() or wal.stat().st_size == 0:
        try:
            os.link(source, candidate)
            _fsync_directory(candidate.parent)
            return
        except OSError:
            candidate.unlink(missing_ok=True)
    backup_database(source, candidate)


def _create_partition_rollback(destination: Path) -> _PartitionReplacement | None:
    if not destination.is_file():
        return None
    rollback = destination.with_name(f".{destination.stem}.rollback.sqlite3")
    pending = rollback.with_suffix(f"{rollback.suffix}.pending")
    pending.unlink(missing_ok=True)
    try:
        os.link(destination, pending)
    except OSError:
        shutil.copyfile(destination, pending)
    _fsync_file(pending)
    os.replace(pending, rollback)
    _fsync_directory(destination.parent)
    return _PartitionReplacement(destination, rollback)


def _recover_partition_replacements(root: Path, active: HistoryActiveSnapshot | None) -> None:
    partition_root = root / "partitions"
    for pending in partition_root.glob("*/*.rollback.sqlite3.pending"):
        pending.unlink(missing_ok=True)
    replacements = tuple(
        _PartitionReplacement(path.with_name(f"{path.name[1:3]}.sqlite3"), path)
        for path in sorted(partition_root.glob("*/.??.rollback.sqlite3"))
    )
    if not replacements:
        return
    if active is None:
        raise RuntimeError("history_partition_recovery_failed")
    references = {root / item.relative_path: item for item in active.partitions}
    for replacement in replacements:
        reference = references.get(replacement.destination)
        if reference is None:
            raise RuntimeError("history_partition_recovery_failed")
        if _partition_matches(replacement.destination, reference):
            replacement.rollback.unlink(missing_ok=True)
            continue
        if not _partition_matches(replacement.rollback, reference):
            raise RuntimeError("history_partition_recovery_failed")
        os.replace(replacement.rollback, replacement.destination)
        _fsync_directory(replacement.destination.parent)


def _partition_matches(path: Path, reference: HistorySnapshotPartition) -> bool:
    try:
        SQLiteHistoryMonthPartitionRepository.verify(path, reference)
    except HistoryMonthPartitionError:
        return False
    return True


def _restore_partition_replacements(replacements: tuple[_PartitionReplacement, ...]) -> None:
    for replacement in reversed(replacements):
        if not replacement.rollback.is_file():
            raise RuntimeError("history_partition_rollback_missing")
        os.replace(replacement.rollback, replacement.destination)
        _fsync_directory(replacement.destination.parent)


def _discard_partition_replacements(replacements: tuple[_PartitionReplacement, ...]) -> None:
    for replacement in replacements:
        try:
            replacement.rollback.unlink(missing_ok=True)
            _fsync_directory(replacement.destination.parent)
        except OSError:
            _LOGGER.warning("history_partition_cleanup_failed")


def _remove_pending(pending: PendingHistoryPartitions) -> None:
    for path in pending.paths.values():
        remove_sqlite_files(path)
        remove_sqlite_files(path.with_name(path.name.replace(".pending.sqlite3", ".seal.sqlite3")))


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _pending_price_gap_counts_for_codes(
    pending: PendingHistoryPartitions,
    sequence: int,
    codes: frozenset[str],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for month, candidate in pending.paths.items():
        previous = pending.active_by_month.get(month)
        path = candidate if candidate.is_file() else pending.root / previous.relative_path if previous else None
        if path is None:
            continue
        monthly = SQLiteHistoryMonthPartitionRepository(path, *month).missing_price_counts(sequence, codes)
        for code, count in monthly.items():
            counts[code] = counts.get(code, 0) + count
    return counts


def _control_identities(
    context: HistorySupplierContext,
) -> tuple[HistorySourceIdentity, HistoryCalendarIdentity, HistoryUniverseIdentity]:
    cutoff = context.calendar.open_dates[-1]
    observed = datetime.combine(cutoff, _BAOSTOCK_DAILY_READY, _SHANGHAI)
    reference = HistoryReferenceSnapshot(
        tuple(item.code for item in context.universe), context.industry_intervals, context.st_evidence
    )
    supplier_contract = f"history_ref_{reference.content_hash}_{canonical_artifact_hash(context.source_versions)[:16]}"
    source = HistorySourceIdentity("tencent_baostock", "history_daily", supplier_contract[:128], observed)
    calendar = HistoryCalendarIdentity(context.calendar.open_dates, source.content_hash)
    universe = HistoryUniverseIdentity(
        tuple(
            HistorySecurityIdentity(item.code, item.name, item.board, item.listed_on, item.delisted_on)
            for item in context.universe
        ),
        source.content_hash,
    )
    return source, calendar, universe


def _validate_context(context: HistorySupplierContext, sessions: int) -> None:
    if len(context.calendar.open_dates) != sessions:
        raise RuntimeError("supplier_calendar_incomplete")


def _is_current(
    active: HistoryActiveSnapshot | None,
    calendar: HistoryCalendarIdentity,
    universe: HistoryUniverseIdentity,
) -> bool:
    return bool(
        active is not None
        and active.data_cutoff == calendar.open_dates[-1]
        and active.calendar_hash == calendar.content_hash
        and active.universe_hash == universe.content_hash
    )


def _active_universe(control: SQLiteHistoryControlRepository, active: HistoryActiveSnapshot | None) -> frozenset[str]:
    if active is None:
        return frozenset()
    state = control.load_state()
    universe = next(item for item in state.universes if item.content_hash == active.universe_hash)
    return frozenset(item.code for item in universe.securities)


def _latest_completed_daily_date(observed_at: datetime) -> date:
    if observed_at.time() >= _BAOSTOCK_DAILY_READY:
        return observed_at.date()
    return observed_at.date() - timedelta(days=1)


def _sync_identity(
    calendar: HistoryCalendarIdentity,
    universe: HistoryUniverseIdentity,
    active: HistoryActiveSnapshot | None,
    source: HistorySourceIdentity,
    state: HistoryControlState,
) -> str:
    # Rebind an existing price-equivalent checkpoint explicitly, including pre-upgrade
    # identities that included the reference hash. Never discard valid download work
    # just because the ST check timestamp or industry intervals changed.
    price_contract = source.supplier_contract.rsplit("_", 1)[-1]
    compatible_sources = {
        item.content_hash for item in state.sources if item.supplier_contract.rsplit("_", 1)[-1] == price_contract
    }
    compatible_sources.add(source.content_hash)
    for previous_calendar in (*state.calendars, calendar):
        if (
            previous_calendar.open_dates != calendar.open_dates
            or previous_calendar.source_identity_hash not in compatible_sources
        ):
            continue
        for previous_universe in (*state.universes, universe):
            if (
                previous_universe.securities != universe.securities
                or previous_universe.source_identity_hash != previous_calendar.source_identity_hash
            ):
                continue
            previous_digest = canonical_artifact_hash(
                (
                    previous_calendar.content_hash,
                    previous_universe.content_hash,
                    active.content_hash if active else None,
                )
            )
            previous_identity = f"sync-{calendar.open_dates[-1]:%Y%m%d}-{previous_digest[:20]}"
            matches = _matching_checkpoints(state.checkpoints, previous_identity)
            if matches and max(matches, key=lambda item: item.ordinal).state != "completed":
                return previous_identity
    digest = canonical_artifact_hash(
        (calendar.open_dates, universe.securities, price_contract, active.content_hash if active else None)
    )
    return f"sync-{calendar.open_dates[-1]:%Y%m%d}-{digest[:20]}"


def _matching_checkpoints(
    checkpoints: tuple[HistorySyncCheckpoint, ...],
    sync_identity: str,
) -> tuple[HistorySyncCheckpoint, ...]:
    """Only identical prices contract, dates, securities and parent may resume."""
    return tuple(item for item in checkpoints if item.sync_identity == sync_identity)


def _safe_active(control: SQLiteHistoryControlRepository) -> HistoryActiveSnapshot | None:
    try:
        return control.load_state().active_snapshot
    except HistoryControlError:
        return None


def _failure_code(exc: BaseException) -> str:
    value = str(exc).strip()
    return value if _ERROR_CODE.fullmatch(value) else "history_sync_failed"


def _publish_progress(
    progress: HistorySyncProgressPort | None,
    stage: HistorySyncProgressStage,
    state: Literal["started", "waiting", "retrying", "completed", "failed", "cancelled"],
    counts: tuple[int, int] = (0, 1),
    current_item: str | None = None,
) -> None:
    if progress is None:
        return
    try:
        progress.publish(HistorySyncProgress(stage, state, *counts, current_item))
    except OSError:
        pass


def _status(  # noqa: PLR0913 - immutable status projection keeps the public fields explicit
    state: HistoryMaintenanceState,
    reason: str | None,
    configuration: HistorySyncConfiguration,
    snapshot: HistoryActiveSnapshot | None,
    observed_at: datetime,
    *,
    unresolved_price_cells: int = 0,
) -> HistoryMaintenanceStatus:
    root = configuration.archive_root
    due = None
    if snapshot is not None:
        due = evaluate_history_training_due(
            HistoryTrainingDueQuery(root, configuration.training_root / "v3", observed_at, V3_TRAINING_PROFILE)
        )
    due_state = (
        due.state
        if due is not None and snapshot is not None and due.active_snapshot.content_hash == snapshot.content_hash
        else None
    )
    return HistoryMaintenanceStatus(
        state=state,
        reason=reason,
        archive_root=root,
        selected_baseline_source="tencent_then_baostock_gap",
        efficient_daily_source="tencent",
        active_snapshot_hash=snapshot.content_hash if snapshot is not None else None,
        data_cutoff=snapshot.data_cutoff if snapshot is not None else None,
        label_cutoff=snapshot.label_cutoff if snapshot is not None else None,
        matured_label_days_since_training=(due_state.matured_label_days_since_training if due_state else 0),
        training_due=(due_state.training_due if due_state else False),
        training_due_reason=(due_state.reason if due_state else "data_incomplete"),
        automatic_training=False,
        unresolved_price_cells=unresolved_price_cells,
    )


__all__ = ["run_history_sync"]
