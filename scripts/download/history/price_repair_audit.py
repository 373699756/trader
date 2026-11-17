"""Profile an existing automatic repair journal on an isolated annual SQLite copy."""

from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path
from time import monotonic
from typing import cast
from unittest.mock import patch

from scripts.core.paths import PROJECT_ROOT as TOOL_ROOT
from scripts.core.reporting import emit_report
from trader.download.domain.baostock_daily import BaoStockBoard
from trader.download.domain.history_price_qualification import QualifyHistoryPairs, QualifyReplacementWindow
from trader.download.domain.history_revision import HistoryRevision
from trader.download.domain.published_history import PublishedHistoryWindow
from trader.download.infra.history_prices import HistoryPricePartition
from trader.download.infra.published_history_codec import DecodePublishedHistoryCellPayload
from trader.infra.sqlite_codec import DecodeDate
from trader.infra.sqlite_connection import SqliteConnection, SqliteConnectionOptions, SqliteSession, SqliteTransaction
from trader.infra.sqlite_policy import SqliteIoPolicy








def _source_stats(paths: tuple[Path, ...]) -> tuple[tuple[int, int, int, int] | None, ...]:
    result: list[tuple[int, int, int, int] | None] = []
    for path in paths:
        if path.exists():
            stat = path.stat()
            result.append((stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns))
        else:
            result.append(None)
    return tuple(result)


def _snapshot_options(path: Path) -> SqliteConnectionOptions:
    wal = Path(str(path) + "-wal")
    # A stopped writer's empty/absent WAL needs no shared-memory sidecars.
    # A nonempty WAL must remain visible to SQLite's read-only backup.
    return SqliteConnectionOptions(read_only=True, immutable=not wal.exists() or wal.stat().st_size == 0)


def _profile_repair_year(
    root: Path,
    work: Path,
    year: int,
    code: str,
    *,
    sqlite_policy: SqliteIoPolicy,
) -> dict[str, object]:
    from scripts.download.history.history_publication_measurement import HistoryPublicationMeasurement
    from scripts.infra.database.database_measurement import DatabaseMeasurement

    root = root.resolve()
    if (
        not work.is_absolute() or work.exists() or work.is_symlink()
        or work.resolve().is_relative_to(TOOL_ROOT) or work.resolve().is_relative_to(root)
        or root.is_relative_to(work.resolve()) or not 1990 <= year <= 2100
        or re.fullmatch(r"[0-9]{6}", code) is None
    ):
        raise ValueError("repair_profile_requires_new_external_work_root_and_valid_year_code")
    source = root / f"prices_{year:04d}.sqlite3"
    catalog = root / "catalog.sqlite3"
    protected = (catalog, Path(str(catalog) + "-wal"), source, Path(str(source) + "-wal"))
    before = _source_stats(protected)
    with SqliteSession(catalog, _snapshot_options(catalog), sqlite_policy=sqlite_policy) as connection:
        published = connection.run("SELECT MAX(sync_sequence) FROM published_snapshot").require_one()[0]
        rows = connection.run(
            "SELECT trade_date,sync_sequence,board,source,payload FROM price_replacement WHERE code=? ORDER BY trade_date LIMIT 2001",
            (code,),
        ).fetchall()
    if not rows or len(rows) > 2000 or any(tuple(row[1:4]) != tuple(rows[0][1:4]) for row in rows):
        raise ValueError("repair_profile_requires_one_complete_journal_window")
    sequence = rows[0][1]
    if (published is not None and published >= sequence) or rows[0][3] not in ("tencent", "baostock"):
        raise ValueError("repair_profile_requires_unpublished_sequence")
    dates = tuple(DecodeDate(row[0]) for row in rows)
    window = PublishedHistoryWindow(code, tuple(
        DecodePublishedHistoryCellPayload(code, day.isoformat(), row[4])
        for day, row in zip(dates, rows, strict=True)
    ))
    QualifyReplacementWindow(dates, PublishedHistoryWindow(code, ()), window)
    revisions = tuple(
        HistoryRevision(sequence, cast(BaoStockBoard, rows[0][2]), cell, None, None, None)
        for cell in QualifyHistoryPairs(code, dates, window) if cell.trade_date.year == year
    )
    if not revisions:
        return {"status": "empty", "summary": {"reason": "candidate_year_empty", "network_requests": 0}}
    if not source.is_file() or source.is_symlink() or source.stat().st_size > 700 * 1024**2:
        raise ValueError("repair_profile_requires_annual_file_below_700_mib")
    if shutil.disk_usage(work.parent).free < source.stat().st_size * 2 + 64 * 1024**2:
        raise ValueError("repair_profile_disk_space_insufficient")
    work.mkdir()
    target = work / source.name
    measurement = HistoryPublicationMeasurement(profiler=False)
    changed = 0
    changed_operation = HistoryPricePartition.changed_revisions

    def changed_revisions(partition: HistoryPricePartition, values: tuple[HistoryRevision, ...]) -> tuple[HistoryRevision, ...]:
        nonlocal changed
        with measurement.phase("repair_changed_revisions", source.name):
            result = tuple(changed_operation(partition, values))
            changed = len(result)
            return result

    try:
        with measurement.phase("source_backup", source.name):
            with (
                SqliteSession(source, _snapshot_options(source), sqlite_policy=sqlite_policy) as original,
                SqliteSession(target, sqlite_policy=sqlite_policy) as duplicate,
            ):
                original.backup_to(duplicate, deadline=monotonic() + 180.0, monotonic=monotonic)
        with DatabaseMeasurement(work, sample_wal=False) as database:
            with (
                patch.object(HistoryPricePartition, "changed_revisions", changed_revisions),
                patch.object(HistoryPricePartition, "_save_batch", measurement._timed("repair_write", HistoryPricePartition._save_batch)),
                patch.object(SqliteTransaction, "commit", measurement._timed("repair_commit", SqliteTransaction.commit)),
                patch.object(SqliteConnection, "close", measurement._timed("repair_close", SqliteConnection.close)),
                measurement.phase("repair_replace_total", source.name),
            ):
                HistoryPricePartition(target, year, 1, sqlite_policy=sqlite_policy).replace_unpublished_code(revisions, sequence)
    finally:
        if _source_stats(protected) != before:
            raise RuntimeError("repair_profile_source_changed")
    return {
        "status": "passed" if changed else "empty",
        "summary": {
            "code": code, "year": year, "sequence": sequence, "candidate_window_rows": len(dates),
            "candidate_year_rows": len(revisions), "changed_rows": changed,
            "source_database_bytes": source.stat().st_size, "network_requests": 0,
            "active_data_written": False, "source_stat_unchanged": True, "performance_acceptance": False,
            "database_measurement": database.result, "publication_measurement": measurement.report(),
        },
    }


def Main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-root", type=Path, required=True)
    parser.add_argument("--runtime-config", type=Path, default=TOOL_ROOT / "config/runtime.json")
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--repair-profile-year", type=int, required=True)
    parser.add_argument("--repair-profile-code", required=True)
    args = parser.parse_args()
    from trader.bootstrap import BuildSqlitePolicy
    from trader.infra.settings import LoadRuntimeSettings
    sqlite_policy = BuildSqlitePolicy(LoadRuntimeSettings(args.runtime_config))
    emit_report({ **_profile_repair_year(
        args.history_root, args.work_root, args.repair_profile_year, args.repair_profile_code,
        sqlite_policy=sqlite_policy,
    )})
    return 0
