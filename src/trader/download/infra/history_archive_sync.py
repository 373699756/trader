"""Crash-safe orchestration for the zero-argument monthly history archive."""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import time as monotonic_time
from collections import defaultdict
from collections.abc import Callable, Iterator
from concurrent.futures import Future, as_completed
from contextlib import closing
from dataclasses import dataclass, field
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
from trader.download.infra.history_month_partition import (
    HistoryMonthPartitionError,
    SQLiteHistoryMonthPartitionRepository,
)
from trader.download.infra.history_reference_files import (
    history_industry_mapping_path,
    read_bound_history_reference,
    write_history_reference,
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


@dataclass(frozen=True)
class _CodeDownloadContext:
    configuration: HistorySyncConfiguration
    supplier_context: HistorySupplierContext
    active: HistoryActiveSnapshot | None
    previous_codes: frozenset[str]
    previous_gaps: dict[str, tuple[date, ...]]


@dataclass(frozen=True)
class _PendingPartitions:
    root: Path
    paths: dict[tuple[int, int], Path]
    active_by_month: dict[tuple[int, int], HistorySnapshotPartition]
    first_date: date


@dataclass(frozen=True)
class _PartitionReplacement:
    destination: Path
    rollback: Path


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
        previous_gaps = _previous_price_gaps(root, active, context)
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
    sync_identity = _sync_identity(calendar, universe, active)
    checkpoints = _matching_checkpoints(control.load_state().checkpoints, sync_identity)
    completed = max((item.completed_units for item in checkpoints), default=0)
    ordinal = max((item.ordinal for item in checkpoints), default=0) + 1
    total = len(context.universe)
    try:
        _publish_progress(progress, "preparing_partitions", "started")
        pending = _prepare_pending(configuration.archive_root, calendar.open_dates, active, completed > 0)
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
        unresolved = _pending_price_gap_count(pending, sequence, context)
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
        )
        _publish_progress(progress, "publishing_snapshot", "completed", (1, 1))
        try:
            _remove_pending(pending)
        except OSError:
            pass
        try:
            stage.clear()
        except OSError:
            pass
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
    pending: _PendingPartitions
    control: SQLiteHistoryControlRepository
    executor: WorkerExecutor
    observed_at: datetime
    sync_identity: str
    sequence: int
    ordinal: int
    completed: int
    progress: HistorySyncProgressPort | None
    cancel_requested: Cancellation
    recovery_started_at: float = field(default_factory=monotonic_time.monotonic)
    recovery_codes: int = 0
    recovery_selection: frozenset[str] = frozenset()

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
        wave_size = self.context.configuration.history_workers
        for start in range(0, len(universe), wave_size):
            self.check_cancel()
            end = min(start + wave_size, len(universe))
            self._tencent_wave(start, end)
            self.checkpoint(end)

    def _tencent_wave(self, start: int, end: int) -> None:
        futures: dict[Future[PublishedHistoryWindow], tuple[BaoStockSecurity, tuple[date, ...]]] = {}
        try:
            for index, security in enumerate(self.context.supplier_context.universe[start:end], start=start):
                self.check_cancel()
                if self.stage.contains(security.code):
                    continue
                dates = _requested_dates(self.context, security)
                self._price_progress("tencent_history", "started", index, security.code, dates)
                if not dates:
                    self.stage.save(PublishedHistoryWindow(security.code, ()))
                    self._price_progress("tencent_history", "completed", index + 1, security.code, dates)
                    continue
                future = submit_or_reject(self.executor, self.supplier.fetch_tencent_window, security, dates)
                futures[future] = (security, dates)
            completed = end - len(futures)
            for future in as_completed(futures):
                self.check_cancel()
                security, dates = futures[future]
                state = self._save_tencent_result(future, security, dates)
                completed += 1
                self._price_progress("tencent_history", state, completed, security.code, dates)
        finally:
            for future in futures:
                future.cancel()

    def _save_tencent_result(
        self, future: Future[PublishedHistoryWindow], security: BaoStockSecurity, dates: tuple[date, ...]
    ) -> Literal["completed", "failed"]:
        try:
            window = future.result()
            self.check_cancel()
            if window.code != security.code or tuple(cell.trade_date for cell in window.cells) != dates:
                raise RuntimeError("history_tencent_coverage_incomplete")
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self.check_cancel()
            self.stage.fail(security.code, _failure_code(exc))
            return "failed"
        self.stage.save(window)
        return "completed"

    def supplement_baostock(self) -> None:
        self.recovery_started_at = monotonic_time.monotonic()
        gaps = self.stage.price_gap_codes()
        cap = self.context.configuration.baostock_max_codes
        offset = ((self.sequence - 1) * cap) % len(gaps) if gaps else 0
        self.recovery_selection = frozenset((gaps[offset:] + gaps[:offset])[:cap])
        universe = self.context.supplier_context.universe
        supplemented = self.stage.supplemented_codes()
        batch_size = self.context.configuration.download_batch_size
        for start in range(0, len(universe), batch_size):
            self.check_cancel()
            end = min(start + batch_size, len(universe))
            revisions: list[HistoryRevision] = []
            for index, security in enumerate(universe[start:end], start=start):
                if security.code not in supplemented:
                    revisions.extend(self._supplement_security(index, security))
            _write_revision_batch(self.pending, tuple(revisions))
            self.stage.mark_supplemented(tuple(item.code for item in universe[start:end]))
            self.checkpoint(end)

    def build_gap_inventory(self) -> None:
        total = len(self.context.supplier_context.universe)
        _publish_progress(self.progress, "history_gap_inventory", "started", (0, total))
        self.check_cancel()
        self.stage.save_gap_inventory(_history_gap_rows(self.context, self.stage, self.cancel_requested))
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

    def _supplement_security(self, index: int, security: BaoStockSecurity) -> tuple[HistoryRevision, ...]:
        self.check_cancel()
        dates = _requested_dates(self.context, security)
        self._price_progress("persisting_prices", "started", index, security.code, dates)
        if not dates:
            self._price_progress("persisting_prices", "completed", index + 1, security.code, dates)
            return ()
        tencent = self.stage.read(security.code)
        missing_dates = tuple(
            date.fromisoformat(day) for day in self.stage.gap_dates(security.code, "baostock_price_pair")
        )
        anchors = tuple(date.fromisoformat(day) for day in self.stage.gap_dates(security.code, "baostock_basis_anchor"))
        price_dates = tuple(sorted(set(missing_dates) | set(anchors)))
        recovery: tuple[BaoStockDailyCell, ...] = ()
        settings = self.context.configuration
        if (
            missing_dates
            and security.code in self.recovery_selection
            and self.recovery_codes < settings.baostock_max_codes
            and monotonic_time.monotonic() - self.recovery_started_at < settings.baostock_budget_seconds
        ):
            self.recovery_codes += 1
            self._price_progress("baostock_gap_fill", "started", index, security.code, price_dates)
            try:
                price_gaps = self.supplier.fetch_baostock_prices(security, price_dates)
                batch = price_gaps.batch
                if (
                    batch.code != security.code
                    or batch.failure_reasons
                    or tuple(cell.trade_date for cell in batch.cells) != price_dates
                    or any((batch.duplicate_rows, batch.null_rows, batch.out_of_window_rows, batch.future_rows))
                ):
                    raise ValueError("history_recovery_invalid")
                if any(fact.is_st for fact in price_gaps.daily_facts):
                    raise RuntimeError("history_st_reference_conflict")
                recovery = price_gaps.batch.cells
                qualify_history_pairs(security.code, dates, tencent, recovery)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                self.check_cancel()
                if str(exc) == "history_st_reference_conflict":
                    raise
                recovery = ()
            self._price_progress("baostock_gap_fill", "completed", index + 1, security.code, price_dates)
        self.check_cancel()
        cells = qualify_history_pairs(security.code, dates, tencent, recovery)
        context = self.context
        if (
            context.active is not None
            and security.code in context.previous_codes
            and HISTORY_TAIL_CONTRACT in context.supplier_context.source_versions.dependency_versions
        ):
            cells = _validate_tail_overlap(context, cells, security.code)
        self._price_progress("persisting_prices", "completed", index + 1, security.code, dates)
        return tuple(HistoryRevision(self.sequence, security.board, cell, None, None, None) for cell in cells)

    def _price_progress(
        self,
        phase: Literal["tencent_history", "baostock_gap_fill", "persisting_prices"],
        state: Literal["started", "completed", "failed"],
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
                    state,
                    completed,
                    len(self.context.supplier_context.universe),
                    code,
                    supplier_source=("tencent" if phase == "tencent_history" else "baostock")
                    if dates and phase != "persisting_prices"
                    else None,
                    requested_sessions=len(dates) if dates and phase != "persisting_prices" else None,
                )
            )
        except OSError:
            pass


def _history_gap_rows(
    context: _CodeDownloadContext,
    stage: HistoryTencentStage,
    cancel_requested: Cancellation,
) -> Iterator[tuple[str, str, str]]:
    for security in context.supplier_context.universe:
        if cancel_requested():
            raise _HistoryDownloadCancelledError("cancelled")
        dates = _requested_dates(context, security)
        if not dates:
            continue
        window = stage.read(security.code)
        failed = stage.failure_reason(security.code) is not None
        by_date = {cell.trade_date: cell for cell in window.cells}
        missing = False
        for day in dates:
            cell = by_date.get(day)
            if failed or cell is None or cell.unadjusted is None or cell.qfq is None:
                missing = True
                yield security.code, day.isoformat(), "baostock_price_pair"
        if missing:
            anchors = tuple(
                cell.trade_date for cell in window.cells if cell.unadjusted is not None and cell.qfq is not None
            )
            for day in anchors[-min(5, context.configuration.reread_sessions) :]:
                yield security.code, day.isoformat(), "baostock_basis_anchor"


def _seal_and_publish(  # noqa: PLR0913
    root: Path,
    pending: _PendingPartitions,
    control: SQLiteHistoryControlRepository,
    identities: tuple[HistorySourceIdentity, HistoryCalendarIdentity, HistoryUniverseIdentity],
    sequence: int,
    sync_identity: str,
    ordinal: int,
    total: int,
    observed_at: datetime,
    progress: HistorySyncProgressPort | None,
    reference: HistoryReferenceSnapshot,
) -> HistoryActiveSnapshot:
    source, calendar, universe = identities
    sealed = _seal_pending(root, pending, progress)
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
    mapping_path = history_industry_mapping_path(root)
    mapping_rollback = mapping_path.with_name(".industry_mapping.rollback.json")
    mapping_rollback.unlink(missing_ok=True)
    if mapping_path.is_file():
        mapping_rollback.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(mapping_path, mapping_rollback)
        _fsync_file(mapping_rollback)
    try:
        write_history_reference(mapping_path, reference)
        _publish_snapshot(
            control,
            identities,
            snapshot,
            HistorySyncCheckpoint(sync_identity, ordinal, "completed", observed_at, total, total, None),
        )
    except BaseException:
        try:
            active_hash = control.load_state().active_snapshot_hash
        except HistoryControlError:
            raise
        if active_hash == snapshot.content_hash:
            mapping_rollback.unlink(missing_ok=True)
            _discard_partition_replacements(sealed.replacements)
            return snapshot
        if mapping_rollback.is_file():
            os.replace(mapping_rollback, mapping_path)
            _fsync_directory(mapping_path.parent)
        else:
            mapping_path.unlink(missing_ok=True)
        _restore_partition_replacements(sealed.replacements)
        raise
    mapping_rollback.unlink(missing_ok=True)
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
        reference = next((item for item in active.partitions if _partition_month(item) == (year, month)), None)
        if reference is None:
            raise RuntimeError("history_tail_qfq_overlap_missing")
        # The writer trusts published partition identities exactly as the existing
        # incremental path does; read only this stock's bounded overlap via its index.
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


def _prepare_pending(
    root: Path,
    dates: tuple[date, ...],
    active: HistoryActiveSnapshot | None,
    resume: bool,
) -> _PendingPartitions:
    active_by_month = {_partition_month(item): item for item in active.partitions} if active is not None else {}
    paths: dict[tuple[int, int], Path] = {}
    for year, month in route_history_months(dates[0], dates[-1]):
        path = root / "partitions" / f"{year:04d}" / f".{month:02d}.pending.sqlite3"
        paths[(year, month)] = path
        if not resume:
            _remove_sqlite(path)
    pending = _PendingPartitions(root, paths, active_by_month, dates[0])
    if resume:
        required = {
            (dates[0].year, dates[0].month),
            (dates[-1].year, dates[-1].month),
            *(paths.keys() - active_by_month.keys()),
        }
        if any(not paths[month].is_file() for month in required):
            raise RuntimeError("sync_checkpoint_incomplete")
    if active is None:
        for month_key in paths:
            _ensure_pending(pending, month_key)
    else:
        _ensure_pending(pending, (dates[0].year, dates[0].month))
        _ensure_pending(pending, (dates[-1].year, dates[-1].month))
        for month_key in paths.keys() - active_by_month.keys():
            _ensure_pending(pending, month_key)
    return pending


def _ensure_pending(pending: _PendingPartitions, month: tuple[int, int]) -> Path:
    year, calendar_month = month
    path = pending.paths[month]
    if path.is_file():
        return path
    reference = pending.active_by_month.get(month)
    if reference is not None:
        _backup_database(pending.root / reference.relative_path, path)
    repository = SQLiteHistoryMonthPartitionRepository(path, year, calendar_month)
    repository.initialize()
    if month == (pending.first_date.year, pending.first_date.month):
        repository.prune_before(pending.first_date)
    return path


def _write_revision_batch(pending: _PendingPartitions, revisions: tuple[HistoryRevision, ...]) -> None:
    grouped: dict[tuple[int, int], list[HistoryRevision]] = defaultdict(list)
    for value in revisions:
        grouped[(value.trade_date.year, value.trade_date.month)].append(value)
    for (year, month), values in sorted(grouped.items()):
        path = _ensure_pending(pending, (year, month))
        repository = SQLiteHistoryMonthPartitionRepository(path, year, month)
        repository.save_revisions(sorted(values, key=lambda item: (item.trade_date, item.code, item.revision_id)))


def _seal_pending(
    root: Path,
    pending: _PendingPartitions,
    progress: HistorySyncProgressPort | None = None,
) -> _SealedPartitions:
    references = []
    replacements: list[_PartitionReplacement] = []
    total = len(pending.paths)
    try:
        for index, ((year, month), path) in enumerate(sorted(pending.paths.items())):
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
                reference = SQLiteHistoryMonthPartitionRepository(candidate, year, month).seal()
            else:
                existing_reference = pending.active_by_month.get((year, month))
                if existing_reference is None:
                    raise RuntimeError("history_snapshot_month_missing")
                reference = existing_reference
            references.append(reference)
            _publish_progress(progress, "sealing_partitions", "completed", (index + 1, total), current_item)
    except BaseException:
        _restore_partition_replacements(tuple(replacements))
        raise
    return _SealedPartitions(tuple(references), tuple(replacements))


def _stage_partition_for_seal(source: Path, candidate: Path) -> None:
    _remove_sqlite(candidate)
    wal = Path(f"{source}-wal")
    if not wal.exists() or wal.stat().st_size == 0:
        try:
            os.link(source, candidate)
            _fsync_directory(candidate.parent)
            return
        except OSError:
            candidate.unlink(missing_ok=True)
    _backup_database(source, candidate)


def _create_partition_rollback(destination: Path) -> _PartitionReplacement | None:
    if not destination.is_file():
        return None
    rollback = destination.with_name(f".{destination.stem}.rollback.sqlite3")
    pending = rollback.with_suffix(f"{rollback.suffix}.pending")
    pending.unlink(missing_ok=True)
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
            pass


def _publish_snapshot(
    control: SQLiteHistoryControlRepository,
    identities: tuple[HistorySourceIdentity, HistoryCalendarIdentity, HistoryUniverseIdentity],
    snapshot: HistoryActiveSnapshot,
    checkpoint: HistorySyncCheckpoint,
) -> None:
    source, calendar, universe = identities
    control.save_source(source)
    control.save_calendar(calendar)
    control.save_universe(universe)
    control.save_checkpoint(checkpoint)
    control.publish_snapshot(snapshot)


def _backup_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with (
        closing(sqlite3.connect(source)) as input_connection,
        closing(sqlite3.connect(destination)) as output_connection,
    ):
        input_connection.backup(output_connection)


def _remove_pending(pending: _PendingPartitions) -> None:
    for path in pending.paths.values():
        _remove_sqlite(path)


def _remove_sqlite(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        candidate.unlink(missing_ok=True)


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


def _previous_price_gaps(
    root: Path, active: HistoryActiveSnapshot | None, context: HistorySupplierContext
) -> dict[str, tuple[date, ...]]:
    if active is None:
        return {}
    gaps: dict[str, list[date]] = defaultdict(list)
    allowed_codes = {item.code for item in context.universe}
    allowed_dates = frozenset(context.calendar.open_dates)
    for reference in active.partitions:
        year, month = _partition_month(reference)
        partition = SQLiteHistoryMonthPartitionRepository(root / reference.relative_path, year, month)
        for code, day in partition.missing_price_dates(active.sequence):
            if code in allowed_codes and day in allowed_dates:
                gaps[code].append(day)
    return {code: tuple(sorted(days)) for code, days in gaps.items()}


def _pending_price_gap_count(pending: _PendingPartitions, sequence: int, context: HistorySupplierContext) -> int:
    allowed_codes = {item.code for item in context.universe}
    allowed_dates = frozenset(context.calendar.open_dates)
    count = 0
    for month, candidate in pending.paths.items():
        previous = pending.active_by_month.get(month)
        path = candidate if candidate.is_file() else pending.root / previous.relative_path if previous else None
        if path is None:
            continue
        partition = SQLiteHistoryMonthPartitionRepository(path, *month)
        count += sum(
            code in allowed_codes and day in allowed_dates for code, day in partition.missing_price_dates(sequence)
        )
    return count


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


def _partition_month(reference: HistorySnapshotPartition) -> tuple[int, int]:
    path = Path(reference.relative_path)
    return int(path.parts[1]), int(path.stem)


def _sync_identity(
    calendar: HistoryCalendarIdentity,
    universe: HistoryUniverseIdentity,
    active: HistoryActiveSnapshot | None,
) -> str:
    digest = canonical_artifact_hash(
        (calendar.content_hash, universe.content_hash, active.content_hash if active else None)
    )
    return f"sync-{calendar.open_dates[-1]:%Y%m%d}-{digest[:20]}"


def _matching_checkpoints(
    checkpoints: tuple[HistorySyncCheckpoint, ...],
    sync_identity: str,
) -> tuple[HistorySyncCheckpoint, ...]:
    """Only identical calendar, universe, source contract and parent may resume."""
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
