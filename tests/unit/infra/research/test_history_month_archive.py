from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

from trader.domain.research.baostock_daily import BaoStockDailyCell, BaoStockDailySide
from trader.domain.research.history_control import HistoryActiveSnapshot, HistorySnapshotPartition
from trader.domain.research.history_monthly import HistoryMonthlyRevision
from trader.infra.research.history_month_archive import (
    HistoryMonthlyArchiveError,
    SQLiteHistoryMonthlyArchive,
    route_history_months,
)
from trader.infra.research.history_month_partition import SQLiteHistoryMonthPartitionRepository
from trader.infra.research.history_training_cache import HistoryTrainingCacheError, SQLiteHistoryTrainingCache


def _side(code: str, day: date, adjustment: str, close: float) -> BaoStockDailySide:
    return BaoStockDailySide(
        code,
        day,
        adjustment,  # type: ignore[arg-type]
        close,
        close + 0.5,
        close - 0.5,
        close,
        100.0,
        1000.0,
        close - 0.1 if adjustment == "unadjusted" else None,
        0.01 if adjustment == "unadjusted" else None,
        0.02 if adjustment == "unadjusted" else None,
        "trading",
    )


def _revision(code: str, day: date, sequence: int = 1) -> HistoryMonthlyRevision:
    close = 10.0 + day.toordinal() % 100
    raw = _side(code, day, "unadjusted", close)
    qfq = _side(code, day, "qfq", close - 1.0)
    return HistoryMonthlyRevision(
        sequence,
        "main",
        BaoStockDailyCell(code, day, "complete", raw, qfq),
        False,
        "bank",
        "sw",
    )


def _build_snapshot(root: Path, rows: tuple[HistoryMonthlyRevision, ...]) -> HistoryActiveSnapshot:
    grouped: dict[tuple[int, int], list[HistoryMonthlyRevision]] = defaultdict(list)
    for row in rows:
        grouped[(row.trade_date.year, row.trade_date.month)].append(row)
    references: list[HistorySnapshotPartition] = []
    for (year, month), month_rows in sorted(grouped.items()):
        path = root / f"partitions/{year:04d}/{month:02d}.sqlite3"
        repository = SQLiteHistoryMonthPartitionRepository(path, year, month)
        repository.initialize()
        repository.save_revisions(tuple(month_rows))
        references.append(repository.seal())
    return HistoryActiveSnapshot(
        1,
        max(row.trade_date for row in rows),
        max(row.trade_date for row in rows),
        "a" * 64,
        "b" * 64,
        "c" * 64,
        tuple(references),
    )


def test_archive_routes_single_day_code_window_cross_month_and_training_cache(tmp_path: Path) -> None:
    root = tmp_path / "history"
    dates = tuple(date(2026, 8, 1) + timedelta(days=offset) for offset in range(62))
    rows = tuple(_revision(code, day) for day in dates for code in ("600001", "600002"))
    snapshot = _build_snapshot(root, rows)
    archive = SQLiteHistoryMonthlyArchive(root)

    assert route_history_months(dates[0], dates[-1]) == ((2026, 8), (2026, 9), (2026, 10))
    assert tuple(row.code for row in archive.read_day(dates[31], snapshot)) == ("600001", "600002")
    code_rows = archive.read_code_window("600001", dates[-61:], snapshot)
    assert tuple(row.trade_date for row in code_rows) == dates[-61:]
    assert tuple(archive.iter_snapshot_revisions(snapshot)) == rows

    windows = tuple(archive.iter_training_windows(snapshot, dates))
    assert len(windows) == 4
    assert all(len(window.rows) == 61 for window in windows)

    with pytest.raises(ValueError, match="cutoff"):
        archive.read_day(dates[-1] + timedelta(days=1), snapshot)
    with pytest.raises(HistoryMonthlyArchiveError, match="cover"):
        tuple(archive.iter_training_windows(replace(snapshot, partitions=snapshot.partitions[:-1]), dates))

    cache = SQLiteHistoryTrainingCache(tmp_path / "training.sqlite3", snapshot.content_hash)
    cache.initialize()
    cache.write_revisions(rows)
    cache.write_revisions(rows)
    assert len(cache.read_date(dates[0])) == 2
    assert len(tuple(cache.iter_code("600001"))) == 62
    with pytest.raises(HistoryTrainingCacheError, match="row conflicts"):
        cache.write_revisions((replace(rows[0], first_seen_sequence=2, is_st=True),))
    with pytest.raises(HistoryTrainingCacheError, match="identity conflicts"):
        SQLiteHistoryTrainingCache(tmp_path / "training.sqlite3", "d" * 64).initialize()
    advanced = cache.advance_snapshot("d" * 64, (dates[0], dates[1]))
    assert advanced.read_date(dates[0]) == ()
    assert len(tuple(advanced.iter_code("600001"))) == 60
    with pytest.raises(HistoryTrainingCacheError, match="identity conflicts"):
        cache.read_date(dates[2])


def test_archive_reads_2000_sessions_without_directory_scan_or_unbounded_windows(tmp_path: Path) -> None:
    root = tmp_path / "history"
    dates = tuple(date(2020, 1, 1) + timedelta(days=offset) for offset in range(2000))
    rows = tuple(_revision("600001", day) for day in dates)
    snapshot = _build_snapshot(root, rows)
    archive = SQLiteHistoryMonthlyArchive(root)

    descriptor_root = Path("/proc/self/fd")
    baseline_descriptors = len(tuple(descriptor_root.iterdir())) if descriptor_root.is_dir() else None
    peak_descriptors = baseline_descriptors
    replayed_rows: list[HistoryMonthlyRevision] = []
    for row in archive.iter_snapshot_revisions(snapshot):
        replayed_rows.append(row)
        if peak_descriptors is not None:
            peak_descriptors = max(peak_descriptors, len(tuple(descriptor_root.iterdir())))
    windows = archive.iter_training_windows(snapshot, dates)
    window_count = 0
    final_window = None
    for current_window in windows:
        window_count += 1
        final_window = current_window

    assert tuple(replayed_rows) == rows
    assert len(snapshot.partitions) == len(route_history_months(dates[0], dates[-1]))
    assert window_count == 1940
    assert final_window is not None
    assert len(final_window.rows) == 61
    assert final_window.trade_date == dates[-1]
    if baseline_descriptors is not None and peak_descriptors is not None:
        assert peak_descriptors <= baseline_descriptors + 4
