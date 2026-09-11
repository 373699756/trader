"""Crash-safe orchestration for the zero-argument monthly history archive."""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
from collections import defaultdict
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Literal, TypeAlias
from zoneinfo import ZoneInfo

from trader.application.research.history_maintenance import HistoryMaintenanceState, HistoryMaintenanceStatus
from trader.application.research.history_sync import (
    HistorySupplierContext,
    HistorySyncConfiguration,
    HistorySyncProgress,
    HistorySyncProgressPort,
    HistorySyncProgressStage,
    HistorySyncSupplier,
)
from trader.domain.research.baostock_daily import (
    BaoStockCodeBatch,
    BaoStockCodeDownload,
    BaoStockIndustryInterval,
    BaoStockSecurity,
)
from trader.domain.research.h1_point_in_time import canonical_hash
from trader.domain.research.history_control import (
    HistoryActiveSnapshot,
    HistoryCalendarIdentity,
    HistoryDiskRequirement,
    HistorySecurityIdentity,
    HistorySnapshotPartition,
    HistorySourceIdentity,
    HistorySyncCheckpoint,
    HistoryUniverseIdentity,
)
from trader.domain.research.history_monthly import HistoryMonthlyRevision
from trader.infra.research.history_control_repository import (
    HistoryControlError,
    HistoryMaintenanceAlreadyRunningError,
    HistoryMaintenanceLock,
    SQLiteHistoryControlRepository,
    inspect_history_disk,
)
from trader.infra.research.history_month_archive import route_history_months
from trader.infra.research.history_month_partition import (
    HistoryMonthPartitionError,
    SQLiteHistoryMonthPartitionRepository,
)
from trader.infra.research.history_training_due import evaluate_history_training_due

Clock: TypeAlias = Callable[[], datetime]
Cancellation: TypeAlias = Callable[[], bool]
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_BAOSTOCK_DAILY_READY = time(20, 30)
_ERROR_CODE = re.compile(r"[a-z0-9_]{1,64}")


@dataclass(frozen=True)
class _CodeDownloadContext:
    configuration: HistorySyncConfiguration
    supplier_context: HistorySupplierContext
    active: HistoryActiveSnapshot | None
    previous_codes: frozenset[str]
    full_refresh_codes: frozenset[str]
    existing_qfq: dict[tuple[str, date], str | None]


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


def run_history_sync(
    configuration: HistorySyncConfiguration,
    supplier: HistorySyncSupplier,
    *,
    clock: Clock | None = None,
    cancel_requested: Cancellation | None = None,
    progress: HistorySyncProgressPort | None = None,
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
            return _run_locked(configuration, supplier, observed_at, cancel, progress)
    except KeyboardInterrupt:
        active = _safe_active(SQLiteHistoryControlRepository(root / "control.sqlite3"))
        return _status("cancelled", "cancelled", configuration, active, observed_at)
    except HistoryMaintenanceAlreadyRunningError:
        _publish_progress(progress, "initializing", "failed")
        active = _safe_active(SQLiteHistoryControlRepository(root / "control.sqlite3"))
        return _status("already_running", "history_maintenance_running", configuration, active, observed_at)


def _run_locked(
    configuration: HistorySyncConfiguration,
    supplier: HistorySyncSupplier,
    observed_at: datetime,
    cancel_requested: Cancellation,
    progress: HistorySyncProgressPort | None,
) -> HistoryMaintenanceStatus:
    root = configuration.archive_root
    control = SQLiteHistoryControlRepository(root / "control.sqlite3")
    try:
        control.initialize()
        state = control.load_state()
        _publish_progress(progress, "initializing", "completed", (1, 1))
        active = state.active_snapshot
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
        _verify_snapshot(root, active)
        previous_codes = _active_universe(control, active)
        if not previous_codes.issubset(item.code for item in universe.securities):
            raise RuntimeError("supplier_universe_regressed")
        if _is_current(active, calendar, universe):
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
        )
    except (HistoryControlError, OSError, RuntimeError, TypeError, ValueError) as exc:
        active = _safe_active(control)
        return _status("failed", _failure_code(exc), configuration, active, observed_at)


def _has_sufficient_disk(root: Path, minimum_free_bytes: int) -> bool:
    return inspect_history_disk(root, HistoryDiskRequirement(minimum_free_bytes, 0, 0, 0)).sufficient


def _synchronize(  # noqa: PLR0913
    configuration: HistorySyncConfiguration,
    supplier: HistorySyncSupplier,
    observed_at: datetime,
    cancel_requested: Cancellation,
    control: SQLiteHistoryControlRepository,
    active: HistoryActiveSnapshot | None,
    context: HistorySupplierContext,
    source: HistorySourceIdentity,
    calendar: HistoryCalendarIdentity,
    universe: HistoryUniverseIdentity,
    progress: HistorySyncProgressPort | None,
) -> HistoryMaintenanceStatus:
    sequence = 1 if active is None else active.sequence + 1
    sync_identity = _sync_identity(calendar, universe, active)
    checkpoints = tuple(item for item in control.load_state().checkpoints if item.sync_identity == sync_identity)
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
        full_refresh_codes = _full_refresh_codes(control, active, source, universe)
        existing_qfq = _existing_qfq(
            configuration.archive_root,
            active,
            calendar.open_dates[-configuration.reread_sessions :],
        )
        download_context = _CodeDownloadContext(
            configuration,
            context,
            active,
            old_universe,
            full_refresh_codes,
            existing_qfq,
        )
        for index, security in enumerate(context.universe[completed:], start=completed):
            _publish_progress(progress, "downloading_codes", "started", (index, total), security.code)
            if cancel_requested():
                _publish_progress(progress, "downloading_codes", "cancelled", (index, total), security.code)
                control.save_checkpoint(
                    HistorySyncCheckpoint(sync_identity, ordinal, "cancelled", observed_at, index, total, "cancelled")
                )
                return _status("cancelled", "cancelled", configuration, active, observed_at)
            download = _download_for_security(supplier, download_context, security)
            revisions = _revisions(download, security, context.industry_intervals, sequence)
            _write_revisions(pending, revisions)
            completed = index + 1
            control.save_checkpoint(
                HistorySyncCheckpoint(sync_identity, ordinal, "running", observed_at, completed, total, None)
            )
            _publish_progress(progress, "downloading_codes", "completed", (completed, total), security.code)
            ordinal += 1
        if cancel_requested():
            _publish_progress(progress, "downloading_codes", "cancelled", (completed, total))
            control.save_checkpoint(
                HistorySyncCheckpoint(sync_identity, ordinal, "cancelled", observed_at, completed, total, "cancelled")
            )
            return _status("cancelled", "cancelled", configuration, active, observed_at)
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
        )
        _publish_progress(progress, "publishing_snapshot", "completed", (1, 1))
        try:
            _remove_pending(pending)
        except OSError:
            pass
        return _status("completed", None, configuration, snapshot, observed_at)
    except KeyboardInterrupt:
        _publish_progress(progress, "downloading_codes", "cancelled", (completed, total))
        control.save_checkpoint(
            HistorySyncCheckpoint(sync_identity, ordinal, "cancelled", observed_at, completed, total, "cancelled")
        )
        return _status("cancelled", "cancelled", configuration, active, observed_at)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        reason = _failure_code(exc)
        control.save_checkpoint(
            HistorySyncCheckpoint(sync_identity, ordinal, "failed", observed_at, completed, total, reason)
        )
        return _status("failed", reason, configuration, active, observed_at)


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
    try:
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
            _discard_partition_replacements(sealed.replacements)
            return snapshot
        _restore_partition_replacements(sealed.replacements)
        raise
    _discard_partition_replacements(sealed.replacements)
    return snapshot


def _download_for_security(
    supplier: HistorySyncSupplier,
    context: _CodeDownloadContext,
    security: BaoStockSecurity,
) -> BaoStockCodeDownload:
    expected = context.supplier_context.calendar.expected_dates(security)
    if not expected:
        return BaoStockCodeDownload(BaoStockCodeBatch(security.code, ()), ())
    if (
        context.active is None
        or security.code not in context.previous_codes
        or security.code in context.full_refresh_codes
    ):
        requested = expected
    else:
        requested = tuple(
            sorted(
                set(expected[-context.configuration.reread_sessions :])
                | {day for day in expected if day > context.active.data_cutoff}
            )
        )
    download = supplier.fetch_code(security, requested)
    _validate_download(download, security.code, requested)
    if context.active is not None and _qfq_revised(context.active, download, context.existing_qfq):
        download = supplier.fetch_code(security, expected)
        _validate_download(download, security.code, expected)
    return download


def _qfq_revised(
    active: HistoryActiveSnapshot,
    download: BaoStockCodeDownload,
    existing_qfq: dict[tuple[str, date], str | None],
) -> bool:
    return any(
        (cell.code, cell.trade_date) in existing_qfq
        and existing_qfq[(cell.code, cell.trade_date)] != (cell.qfq.content_hash if cell.qfq is not None else None)
        for cell in download.batch.cells
        if cell.trade_date <= active.data_cutoff
    )


def _existing_qfq(
    root: Path,
    active: HistoryActiveSnapshot | None,
    recent_dates: tuple[date, ...],
) -> dict[tuple[str, date], str | None]:
    if active is None:
        return {}
    values: dict[tuple[str, date], str | None] = {}
    dates_by_month: dict[tuple[int, int], list[date]] = defaultdict(list)
    for day in recent_dates:
        if day <= active.data_cutoff:
            dates_by_month[(day.year, day.month)].append(day)
    references = {_partition_month(item): item for item in active.partitions}
    for (year, month), dates in dates_by_month.items():
        reference = references.get((year, month))
        if reference is None:
            raise RuntimeError("history_snapshot_month_missing")
        path = root / reference.relative_path
        SQLiteHistoryMonthPartitionRepository.verify(path, reference)
        repository = SQLiteHistoryMonthPartitionRepository(path, year, month)
        for day in dates:
            for revision in repository.read_day(day, snapshot_sequence=active.sequence):
                values[(revision.code, day)] = revision.cell.qfq.content_hash if revision.cell.qfq is not None else None
    return values


def _validate_download(download: BaoStockCodeDownload, code: str, expected: tuple[date, ...]) -> None:
    batch = download.batch
    dates = tuple(item.trade_date for item in batch.cells)
    if (
        batch.code != code
        or dates != expected
        or not all(item.obtained for item in batch.cells)
        or batch.duplicate_rows
        or batch.null_rows
        or batch.out_of_window_rows
        or batch.future_rows
        or batch.failure_reasons
    ):
        raise RuntimeError("supplier_data_incomplete")


def _revisions(
    download: BaoStockCodeDownload,
    security: BaoStockSecurity,
    intervals: tuple[BaoStockIndustryInterval, ...],
    sequence: int,
) -> tuple[HistoryMonthlyRevision, ...]:
    facts = {item.trade_date: item.is_st for item in download.daily_facts}
    applicable = tuple(item for item in intervals if item.code == security.code)
    values = []
    for cell in download.batch.cells:
        industry = next(
            (
                item
                for item in reversed(applicable)
                if item.effective_from <= cell.trade_date
                and (item.effective_to is None or cell.trade_date < item.effective_to)
            ),
            None,
        )
        values.append(
            HistoryMonthlyRevision(
                sequence,
                security.board,
                cell,
                facts.get(cell.trade_date),
                industry.industry if industry is not None else None,
                industry.classification if industry is not None else None,
            )
        )
    return tuple(values)


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


def _write_revisions(pending: _PendingPartitions, revisions: tuple[HistoryMonthlyRevision, ...]) -> None:
    grouped: dict[tuple[int, int], list[HistoryMonthlyRevision]] = defaultdict(list)
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
                _remove_sqlite(candidate)
                _backup_database(path, candidate)
                destination = root / "partitions" / f"{year:04d}" / f"{month:02d}.sqlite3"
                replacement = _create_partition_rollback(destination)
                if replacement is not None:
                    replacements.append(replacement)
                reference = SQLiteHistoryMonthPartitionRepository(candidate, year, month).seal()
            else:
                existing_reference = pending.active_by_month.get((year, month))
                if existing_reference is None:
                    raise RuntimeError("history_snapshot_month_missing")
                reference = existing_reference
            SQLiteHistoryMonthPartitionRepository.verify(root / reference.relative_path, reference)
            references.append(reference)
            _publish_progress(progress, "sealing_partitions", "completed", (index + 1, total), current_item)
    except BaseException:
        _restore_partition_replacements(tuple(replacements))
        raise
    return _SealedPartitions(tuple(references), tuple(replacements))


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


def _control_identities(
    context: HistorySupplierContext,
) -> tuple[HistorySourceIdentity, HistoryCalendarIdentity, HistoryUniverseIdentity]:
    cutoff = context.calendar.open_dates[-1]
    observed = datetime.combine(cutoff, time(20, 30), _SHANGHAI)
    supplier_contract = f"python_sdk_{canonical_hash((context.source_versions, context.industry_intervals))}"
    source = HistorySourceIdentity("baostock", "baostock_daily", supplier_contract[:128], observed)
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


def _verify_snapshot(root: Path, active: HistoryActiveSnapshot | None) -> None:
    if active is None:
        return
    for reference in active.partitions:
        SQLiteHistoryMonthPartitionRepository.verify(root / reference.relative_path, reference)


def _active_universe(control: SQLiteHistoryControlRepository, active: HistoryActiveSnapshot | None) -> frozenset[str]:
    if active is None:
        return frozenset()
    state = control.load_state()
    universe = next(item for item in state.universes if item.content_hash == active.universe_hash)
    return frozenset(item.code for item in universe.securities)


def _full_refresh_codes(
    control: SQLiteHistoryControlRepository,
    active: HistoryActiveSnapshot | None,
    source: HistorySourceIdentity,
    universe: HistoryUniverseIdentity,
) -> frozenset[str]:
    if active is None:
        return frozenset()
    state = control.load_state()
    previous_source = next(item for item in state.sources if item.content_hash == active.source_identity_hash)
    previous_universe = next(item for item in state.universes if item.content_hash == active.universe_hash)
    previous_by_code = {item.code: item for item in previous_universe.securities}
    if previous_source.supplier_contract != source.supplier_contract:
        return frozenset(previous_by_code)
    return frozenset(
        item.code
        for item in universe.securities
        if item.code in previous_by_code and item != previous_by_code[item.code]
    )


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
    digest = canonical_hash((calendar.content_hash, universe.content_hash, active.content_hash if active else None))
    return f"sync-{calendar.open_dates[-1]:%Y%m%d}-{digest[:20]}"


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


def _status(
    state: HistoryMaintenanceState,
    reason: str | None,
    configuration: HistorySyncConfiguration,
    snapshot: HistoryActiveSnapshot | None,
    observed_at: datetime,
) -> HistoryMaintenanceStatus:
    root = configuration.archive_root
    due = None
    if snapshot is not None:
        due = evaluate_history_training_due(
            root,
            configuration.training_root,
            observed_at,
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
        selected_baseline_source="baostock",
        efficient_daily_source=None,
        active_snapshot_hash=snapshot.content_hash if snapshot is not None else None,
        data_cutoff=snapshot.data_cutoff if snapshot is not None else None,
        label_cutoff=snapshot.label_cutoff if snapshot is not None else None,
        matured_label_days_since_training=(due_state.matured_label_days_since_training if due_state else 0),
        training_due=(due_state.training_due if due_state else False),
        training_due_reason=(due_state.reason if due_state else "data_incomplete"),
        automatic_training=False,
    )


__all__ = ["run_history_sync"]
