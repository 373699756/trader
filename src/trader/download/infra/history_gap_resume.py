"""Reopen incomplete pre-attempt checkpoints without changing published price files."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Iterable
from contextlib import closing
from datetime import date

from trader.download.domain.baostock_daily import BaoStockDailyCell
from trader.download.domain.history_price_qualification import qualify_history_pairs
from trader.download.domain.published_history import PublishedHistoryWindow, project_history_cell
from trader.download.infra.history_month_partition import SQLiteHistoryMonthPartitionRepository
from trader.download.infra.history_pending_prices import PendingHistoryPartitions, backup_database
from trader.download.infra.history_tencent_stage import HistoryTencentStage


def reopen_legacy_history_gaps(
    stage: HistoryTencentStage,
    pending: PendingHistoryPartitions,
    requests: Iterable[tuple[str, tuple[date, ...]]],
    sequence: int,
    cancel_requested: Callable[[], bool],
) -> None:
    legacy = stage.legacy_recovery_codes()
    if not legacy:
        return
    for code, dates in requests:
        if code not in legacy:
            continue
        if cancel_requested():
            raise RuntimeError("cancelled")
        stage.reopen_legacy_recovery(_legacy_window(stage, pending, code, dates, sequence))
        months = sorted({(day.year, day.month) for day in dates})
        # Repeated after a crash: only unpublished missing observations are removed.
        # Successful prices and every earlier snapshot observation remain immutable.
        for month in months:
            if cancel_requested():
                raise RuntimeError("cancelled")
            stage.discard_sealed_month(*month)
            _clear_unpublished_gaps(pending, month, code, sequence)


def _legacy_window(
    stage: HistoryTencentStage, pending: PendingHistoryPartitions, code: str, dates: tuple[date, ...], sequence: int
) -> PublishedHistoryWindow:
    cells = {cell.trade_date: cell for cell in stage.qualified_cells(code, dates)}
    window, _, _ = stage.read_supplement_context(code)
    for cell in qualify_history_pairs(code, dates, window):
        if cell.obtained:
            cells.setdefault(cell.trade_date, cell)
    for month in sorted({(day.year, day.month) for day in dates}):
        path = pending.paths[month]
        if path.is_file():
            keys = tuple((code, day) for day in dates if (day.year, day.month) == month)
            for revision in SQLiteHistoryMonthPartitionRepository(path, *month).read_price_keys(keys, sequence):
                cells.setdefault(revision.trade_date, revision.cell)
    return PublishedHistoryWindow(
        code,
        tuple(
            project_history_cell(cells.get(day, BaoStockDailyCell(code, day, "unknown_missing", None, None)))
            for day in dates
        ),
    )


def _clear_unpublished_gaps(
    pending: PendingHistoryPartitions, month: tuple[int, int], code: str, sequence: int
) -> None:
    path = pending.paths[month]
    if not path.is_file():
        return
    if path.stat().st_nlink > 1:
        wal = path.with_name(path.name + "-wal")
        if wal.is_file() and wal.stat().st_size:
            raise RuntimeError("history_legacy_shared_wal_conflict")
        detached = path.with_name(path.name + ".recovery")
        backup_database(path, detached)
        os.replace(detached, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    with closing(sqlite3.connect(path, timeout=5.0)) as connection, connection:
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            "SELECT o.trade_date,o.revision_id FROM daily_observations o "
            "INDEXED BY history_month_observation_code_date_idx CROSS JOIN daily_records r "
            "WHERE o.code=? AND o.sync_sequence=? "
            "AND r.trade_date=o.trade_date AND r.code=o.code AND r.revision_id=o.revision_id "
            "AND json_extract(r.payload_json,'$.cell.status') IN ('unknown_missing','qfq_missing','unadjusted_missing')",
            (code, sequence),
        ).fetchall()
        for day, revision_id in rows:
            connection.execute(
                "DELETE FROM daily_observations WHERE trade_date=? AND code=? AND sync_sequence=?",
                (day, code, sequence),
            )
            connection.execute(
                "DELETE FROM daily_records WHERE trade_date=? AND code=? AND revision_id=? AND NOT EXISTS "
                "(SELECT 1 FROM daily_observations WHERE trade_date=? AND code=? AND revision_id=?)",
                (day, code, revision_id, day, code, revision_id),
            )
