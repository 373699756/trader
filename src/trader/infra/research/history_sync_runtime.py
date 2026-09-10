"""Crash-safe orchestration for the zero-argument monthly history archive."""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import TypeAlias
from zoneinfo import ZoneInfo

from trader.application.research.history_maintenance import HistoryMaintenanceState, HistoryMaintenanceStatus
from trader.application.research.history_sync import (
    HistorySupplierContext,
    HistorySyncConfiguration,
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
from trader.infra.research.history_month_partition import SQLiteHistoryMonthPartitionRepository

Clock: TypeAlias = Callable[[], datetime]
Cancellation: TypeAlias = Callable[[], bool]
_SHANGHAI = ZoneInfo("Asia/Shanghai")
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


def run_history_sync(
    configuration: HistorySyncConfiguration,
    supplier: HistorySyncSupplier,
    *,
    clock: Clock | None = None,
    cancel_requested: Cancellation | None = None,
) -> HistoryMaintenanceStatus:
    """Synchronize and publish one complete immutable snapshot."""
    observed_at = (clock or (lambda: datetime.now(_SHANGHAI)))()
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("history synchronization clock must be timezone-aware")
    observed_at = observed_at.astimezone(_SHANGHAI)
    cancel = cancel_requested or (lambda: False)
    root = configuration.archive_root
    try:
        with HistoryMaintenanceLock(root / ".maintenance.lock"):
            return _run_locked(configuration, supplier, observed_at, cancel)
    except HistoryMaintenanceAlreadyRunningError:
        active = _safe_active(SQLiteHistoryControlRepository(root / "control.sqlite3"))
        return _status("already_running", "history_maintenance_running", root, active)


def _run_locked(
    configuration: HistorySyncConfiguration,
    supplier: HistorySyncSupplier,
    observed_at: datetime,
    cancel_requested: Cancellation,
) -> HistoryMaintenanceStatus:
    root = configuration.archive_root
    control = SQLiteHistoryControlRepository(root / "control.sqlite3")
    try:
        control.initialize()
        state = control.load_state()
        context = supplier.load_context(observed_at.date(), configuration.sessions)
        _validate_context(context, configuration.sessions)
        source, calendar, universe = _control_identities(context)
        active = state.active_snapshot
        _verify_snapshot(root, active)
        previous_codes = _active_universe(control, active)
        if not previous_codes.issubset(item.code for item in universe.securities):
            raise RuntimeError("supplier_universe_regressed")
        if _is_current(active, calendar, universe):
            return _status("already_current", None, root, active)
        disk = inspect_history_disk(
            root,
            HistoryDiskRequirement(configuration.minimum_free_bytes, 0, 0, 0),
        )
        if not disk.sufficient:
            return _status("blocked", "disk_space_insufficient", root, active)
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
        )
    except (HistoryControlError, OSError, RuntimeError, TypeError, ValueError) as exc:
        active = _safe_active(control)
        return _status("failed", _failure_code(exc), root, active)


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
) -> HistoryMaintenanceStatus:
    sequence = 1 if active is None else active.sequence + 1
    sync_identity = _sync_identity(calendar, universe, active)
    checkpoints = tuple(item for item in control.load_state().checkpoints if item.sync_identity == sync_identity)
    completed = max((item.completed_units for item in checkpoints), default=0)
    ordinal = max((item.ordinal for item in checkpoints), default=0) + 1
    total = len(context.universe)
    try:
        pending = _prepare_pending(configuration.archive_root, calendar.open_dates, active, completed > 0)
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
            if cancel_requested():
                control.save_checkpoint(
                    HistorySyncCheckpoint(sync_identity, ordinal, "cancelled", observed_at, index, total, "cancelled")
                )
                return _status("cancelled", "cancelled", configuration.archive_root, active)
            download = _download_for_security(supplier, download_context, security)
            revisions = _revisions(download, security, context.industry_intervals, sequence)
            _write_revisions(pending, revisions)
            completed = index + 1
            control.save_checkpoint(
                HistorySyncCheckpoint(sync_identity, ordinal, "running", observed_at, completed, total, None)
            )
            ordinal += 1
        if cancel_requested():
            control.save_checkpoint(
                HistorySyncCheckpoint(sync_identity, ordinal, "cancelled", observed_at, completed, total, "cancelled")
            )
            return _status("cancelled", "cancelled", configuration.archive_root, active)
        references = _seal_pending(configuration.archive_root, pending)
        control.save_source(source)
        control.save_calendar(calendar)
        control.save_universe(universe)
        label_cutoff = calendar.open_dates[-2] if len(calendar.open_dates) > 1 else calendar.open_dates[-1]
        snapshot = HistoryActiveSnapshot(
            sequence,
            calendar.open_dates[-1],
            label_cutoff,
            calendar.content_hash,
            universe.content_hash,
            source.content_hash,
            references,
        )
        control.save_checkpoint(
            HistorySyncCheckpoint(sync_identity, ordinal, "completed", observed_at, total, total, None)
        )
        ordinal += 1
        control.publish_snapshot(snapshot)
        try:
            _remove_pending(pending)
        except OSError:
            pass
        return _status("completed", None, configuration.archive_root, snapshot)
    except KeyboardInterrupt:
        control.save_checkpoint(
            HistorySyncCheckpoint(sync_identity, ordinal, "cancelled", observed_at, completed, total, "cancelled")
        )
        return _status("cancelled", "cancelled", configuration.archive_root, active)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        reason = _failure_code(exc)
        control.save_checkpoint(
            HistorySyncCheckpoint(sync_identity, ordinal, "failed", observed_at, completed, total, reason)
        )
        return _status("failed", reason, configuration.archive_root, active)


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
    references = {
        (int(Path(item.relative_path).parts[1]), int(Path(item.relative_path).parts[2])): item
        for item in active.partitions
    }
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
    active_by_month = (
        {
            (int(Path(item.relative_path).parts[1]), int(Path(item.relative_path).parts[2])): item
            for item in active.partitions
        }
        if active is not None
        else {}
    )
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


def _seal_pending(root: Path, pending: _PendingPartitions) -> tuple[HistorySnapshotPartition, ...]:
    references = []
    for (year, month), path in sorted(pending.paths.items()):
        if path.is_file():
            candidate = path.with_name(f".{month:02d}.seal.sqlite3")
            _remove_sqlite(candidate)
            _backup_database(path, candidate)
            references.append(SQLiteHistoryMonthPartitionRepository(candidate, year, month).seal())
            continue
        reference = pending.active_by_month.get((year, month))
        if reference is None:
            raise RuntimeError("history_snapshot_month_missing")
        references.append(reference)
    for reference in references:
        SQLiteHistoryMonthPartitionRepository.verify(root / reference.relative_path, reference)
    return tuple(references)


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


def _status(
    state: HistoryMaintenanceState,
    reason: str | None,
    root: Path,
    snapshot: HistoryActiveSnapshot | None,
) -> HistoryMaintenanceStatus:
    return HistoryMaintenanceStatus(
        state=state,
        reason=reason,
        archive_root=root,
        selected_baseline_source="baostock",
        efficient_daily_source=None,
        active_snapshot_hash=snapshot.content_hash if snapshot is not None else None,
        data_cutoff=snapshot.data_cutoff if snapshot is not None else None,
        label_cutoff=snapshot.label_cutoff if snapshot is not None else None,
        matured_label_days_since_training=0,
        training_due=False,
        training_due_reason="data_incomplete",
        automatic_training=False,
    )


__all__ = ["run_history_sync"]
