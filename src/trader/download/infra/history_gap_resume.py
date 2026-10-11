"""Batch old gap checkpoints by month without changing published price files."""

from __future__ import annotations

import os
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import dataclass
from datetime import date

from trader.download.domain.baostock_daily import BaoStockDailyCell
from trader.download.domain.history_price_qualification import qualify_history_pairs
from trader.download.domain.history_sync import HistorySyncProgress, HistorySyncProgressPort
from trader.download.domain.published_history import PublishedHistoryWindow, project_history_cell
from trader.download.infra.history_month_partition import SQLiteHistoryMonthPartitionRepository
from trader.download.infra.history_pending_prices import PendingHistoryPartitions, backup_database
from trader.download.infra.history_sqlite_activity import HistorySqliteActivity
from trader.download.infra.history_tencent_stage import HistoryTencentStage


@dataclass(frozen=True)
class HistoryGapResumeContext:
    stage: HistoryTencentStage
    pending: PendingHistoryPartitions
    sequence: int
    cancel_requested: Callable[[], bool]
    progress: HistorySyncProgressPort | None = None


@dataclass
class _ResumeProgress:
    progress: HistorySyncProgressPort | None
    total: int
    current: int = 0
    item: str = "prices"

    def publish(self, completed: bool = False) -> None:
        if self.progress is not None:
            try:
                self.progress.publish(
                    HistorySyncProgress(
                        "history_gap_resume",
                        "completed" if completed else "waiting",
                        self.current,
                        self.total,
                        current_item=self.item,
                    )
                )
            except OSError:
                pass


def reopen_legacy_history_gaps(
    context: HistoryGapResumeContext,
    requests: Iterable[tuple[str, tuple[date, ...]]],
) -> None:
    stage, pending, sequence = context.stage, context.pending, context.sequence
    legacy = stage.legacy_recovery_codes()
    selected = tuple((code, dates) for code, dates in requests if code in legacy)
    if not selected:
        return
    status = _ResumeProgress(context.progress, len(selected))
    activity = HistorySqliteActivity(status.publish, context.cancel_requested)
    month_codes: dict[tuple[int, int], set[str]] = defaultdict(set)
    changed_months: set[tuple[int, int]] = set()
    status.publish()
    for start in range(0, len(selected), 64):
        activity.tick()
        batch = selected[start : start + 64]
        for window in _legacy_windows(stage, pending, batch, sequence, activity):
            activity.tick()
            stage.reopen_legacy_recovery(window)
            for cell in window.cells:
                month = (cell.trade_date.year, cell.trade_date.month)
                month_codes[month].add(window.code)
                if cell.status not in {"complete", "supplier_marked_suspended"}:
                    changed_months.add(month)
            status.current += 1
    for month, codes in sorted(month_codes.items()):
        status.item = f"{month[0]:04d}-{month[1]:02d}"
        activity.tick()
        if month in changed_months:
            stage.discard_sealed_month(*month)
        _clear_unpublished_gaps(context, month, tuple(sorted(codes)), activity)
    status.publish(True)


def _legacy_windows(
    stage: HistoryTencentStage,
    pending: PendingHistoryPartitions,
    requests: tuple[tuple[str, tuple[date, ...]], ...],
    sequence: int,
    activity: HistorySqliteActivity,
) -> tuple[PublishedHistoryWindow, ...]:
    cells_by_code: dict[str, dict[date, BaoStockDailyCell]] = {}
    keys_by_month: dict[tuple[int, int], list[tuple[str, date]]] = defaultdict(list)
    for code, dates in requests:
        activity.tick()
        cells = {cell.trade_date: cell for cell in stage.qualified_cells(code, dates)}
        window, _, _ = stage.read_supplement_context(code)
        for cell in qualify_history_pairs(code, dates, window):
            if cell.obtained:
                cells.setdefault(cell.trade_date, cell)
        cells_by_code[code] = cells
        for day in dates:
            if day not in cells:
                keys_by_month[(day.year, day.month)].append((code, day))
    for month, keys in sorted(keys_by_month.items()):
        activity.tick()
        path = pending.paths[month]
        if path.is_file():
            for revision in SQLiteHistoryMonthPartitionRepository(path, *month, activity=activity).read_price_keys(
                tuple(keys), sequence
            ):
                cells_by_code[revision.code].setdefault(revision.trade_date, revision.cell)
    return tuple(
        PublishedHistoryWindow(
            code,
            tuple(
                project_history_cell(
                    cells_by_code[code].get(day, BaoStockDailyCell(code, day, "unknown_missing", None, None))
                )
                for day in dates
            ),
        )
        for code, dates in requests
    )


def _clear_unpublished_gaps(
    context: HistoryGapResumeContext,
    month: tuple[int, int],
    codes: tuple[str, ...],
    activity: HistorySqliteActivity,
) -> None:
    stage, pending, sequence = context.stage, context.pending, context.sequence
    path = pending.paths[month]
    if not path.is_file():
        return
    rows: list[tuple[str, str, str]] = []
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0)) as connection:
        with activity.watch(connection):
            for start in range(0, len(codes), 200):
                batch = codes[start : start + 200]
                rows.extend(
                    connection.execute(
                        "SELECT o.code,o.trade_date,o.revision_id FROM daily_observations o "
                        "INDEXED BY history_month_observation_code_date_idx CROSS JOIN daily_records r "
                        "WHERE o.code IN (" + ",".join("?" for _ in batch) + ") AND o.sync_sequence=? "
                        "AND r.trade_date=o.trade_date AND r.code=o.code AND r.revision_id=o.revision_id "
                        "AND json_extract(r.payload_json,'$.cell.status') IN "
                        "('unknown_missing','qfq_missing','unadjusted_missing')",
                        (*batch, sequence),
                    )
                )
    if not rows:
        return
    # Invalidate before mutation so a crash cannot leave a reusable stale seal.
    stage.discard_sealed_month(*month)
    if path.stat().st_nlink > 1:
        wal = path.with_name(path.name + "-wal")
        if wal.is_file() and wal.stat().st_size:
            raise RuntimeError("history_legacy_shared_wal_conflict")
        detached = path.with_name(path.name + ".recovery")
        backup_database(path, detached, activity=activity)
        os.replace(detached, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    with closing(sqlite3.connect(path, timeout=5.0)) as connection, connection:
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("BEGIN IMMEDIATE")
        with activity.watch(connection):
            for code, day, revision_id in rows:
                activity.tick()
                connection.execute(
                    "DELETE FROM daily_observations WHERE trade_date=? AND code=? AND sync_sequence=?",
                    (day, code, sequence),
                )
                connection.execute(
                    "DELETE FROM daily_records WHERE trade_date=? AND code=? AND revision_id=? AND NOT EXISTS "
                    "(SELECT 1 FROM daily_observations WHERE trade_date=? AND code=? AND revision_id=?)",
                    (day, code, revision_id, day, code, revision_id),
                )
