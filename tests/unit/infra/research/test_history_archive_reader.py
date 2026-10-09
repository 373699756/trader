from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

import trader.download.infra.history_month_partition as partition_module
from trader.download.domain.baostock_daily import BaoStockDailyCell, BaoStockDailySide
from trader.download.domain.history_control import HistoryActiveSnapshot, HistorySnapshotPartition
from trader.download.domain.history_revision import HistoryRevision
from trader.download.domain.published_history import project_history_cell
from trader.download.infra.history_archive_reader import (
    HistoryArchiveReadError,
    HistoryPartitionRevisionComparison,
    SQLiteHistoryArchiveReader,
    route_history_months,
)
from trader.download.infra.history_month_partition import SQLiteHistoryMonthPartitionRepository


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


def _revision(code: str, day: date, sequence: int = 1) -> HistoryRevision:
    close = 10.0 + day.toordinal() % 100
    raw = _side(code, day, "unadjusted", close)
    qfq = _side(code, day, "qfq", close - 1.0)
    return HistoryRevision(
        sequence,
        "main",
        BaoStockDailyCell(code, day, "complete", raw, qfq),
        False,
        "bank",
        "sw",
    )


def _build_snapshot(root: Path, rows: tuple[HistoryRevision, ...]) -> HistoryActiveSnapshot:
    grouped: dict[tuple[int, int], list[HistoryRevision]] = defaultdict(list)
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


def test_archive_routes_single_day_code_window_and_cross_month_training_windows(tmp_path: Path) -> None:
    root = tmp_path / "history"
    dates = tuple(date(2026, 8, 1) + timedelta(days=offset) for offset in range(62))
    rows = tuple(_revision(code, day) for day in dates for code in ("600001", "600002"))
    snapshot = _build_snapshot(root, rows)
    archive = SQLiteHistoryArchiveReader(root)

    assert route_history_months(dates[0], dates[-1]) == ((2026, 8), (2026, 9), (2026, 10))
    assert tuple(row.code for row in archive.read_day(dates[31], snapshot)) == ("600001", "600002")
    code_rows = archive.read_code_window("600001", dates[-61:], snapshot)
    assert tuple(row.trade_date for row in code_rows) == dates[-61:]
    assert tuple(archive.iter_snapshot_revisions(snapshot)) == rows

    windows = tuple(archive.iter_training_windows(snapshot, dates))
    assert len(windows) == 4
    assert all(len(window.points) == 61 for window in windows)

    with pytest.raises(ValueError, match="cutoff"):
        archive.read_day(dates[-1] + timedelta(days=1), snapshot)
    with pytest.raises(HistoryArchiveReadError, match="cover"):
        tuple(archive.iter_training_windows(replace(snapshot, partitions=snapshot.partitions[:-1]), dates))


@pytest.mark.slow
def test_archive_reads_2000_sessions_without_directory_scan_or_unbounded_windows(tmp_path: Path) -> None:
    root = tmp_path / "history"
    dates = tuple(date(2020, 1, 1) + timedelta(days=offset) for offset in range(2000))
    rows = tuple(_revision("600001", day) for day in dates)
    snapshot = _build_snapshot(root, rows)
    archive = SQLiteHistoryArchiveReader(root)

    descriptor_root = Path("/proc/self/fd")
    baseline_descriptors = len(tuple(descriptor_root.iterdir())) if descriptor_root.is_dir() else None
    peak_descriptors = baseline_descriptors
    replayed_rows: list[HistoryRevision] = []
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
    assert len(final_window.points) == 61
    assert final_window.trade_date == dates[-1]
    long_windows = tuple(archive.iter_training_windows(snapshot, dates, window_sessions=251))
    assert len(long_windows) == 1750
    assert len(long_windows[-1].points) == 251
    assert long_windows[-1].trade_date == dates[-1]
    assert len(archive.read_code_window("600001", dates[-251:], snapshot)) == 251
    if baseline_descriptors is not None and peak_descriptors is not None:
        assert peak_descriptors <= baseline_descriptors + 4


def test_verified_snapshot_reuses_each_partition_check_and_counts_latest_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "history"
    dates = tuple(date(2026, 8, 30) + timedelta(days=offset) for offset in range(4))
    rows = tuple(_revision(code, day) for day in dates for code in ("600001", "600002"))
    snapshot = _build_snapshot(root, rows)
    archive = SQLiteHistoryArchiveReader(root)
    original = SQLiteHistoryMonthPartitionRepository.verify.__func__
    verified: list[Path] = []

    def verify(cls, path: Path, reference: HistorySnapshotPartition, progress=None) -> None:
        verified.append(path)
        original(cls, path, reference, progress)

    monkeypatch.setattr(SQLiteHistoryMonthPartitionRepository, "verify", classmethod(verify))

    observed: list[tuple[int, int, int, int, int, str]] = []
    archive.verify_snapshot(snapshot, lambda *values: observed.append(values))
    assert archive.count_range(dates[0], dates[-1], snapshot, codes=("600001",)) == len(dates)
    assert archive.count_range(dates[0], dates[-1], snapshot, codes=("600001", "600002")) == len(rows)
    assert len(tuple(archive.iter_code("600001", dates[0], dates[-1], snapshot))) == len(dates)
    assert len(tuple(archive.iter_code("600002", dates[0], dates[-1], snapshot))) == len(dates)

    assert observed[-1][:3] == (len(snapshot.partitions), len(snapshot.partitions), len(snapshot.partitions))
    assert any(completed == 0 and current == 1 and phase == "hash" for completed, _, current, _, _, phase in observed)
    assert len(verified) == len(snapshot.partitions)


def test_revision_comparison_uses_one_current_physical_partition_for_both_sequences(tmp_path: Path) -> None:
    root = tmp_path / "history"
    first = date(2026, 9, 9)
    second = date(2026, 9, 10)
    original = _revision("600001", first)
    revised = replace(original, first_seen_sequence=2, is_st=True)
    unchanged = _revision("600001", second)
    snapshot = _build_snapshot(root, (original, revised, unchanged))
    comparison = HistoryPartitionRevisionComparison(snapshot.partitions[0], 1, 2)

    changed = SQLiteHistoryArchiveReader(root).revised_dates(first, second, comparison)

    assert changed == (first,)


def test_published_daily_projection_preserves_facts_without_revision_decode(tmp_path, monkeypatch):
    root = tmp_path / "history"
    dates = tuple(date(2026, 8, 1) + timedelta(days=offset) for offset in range(62))
    original = _revision("600001", dates[0])
    changed = replace(original, first_seen_sequence=2, is_st=True)
    rows = tuple(_revision(code, day) for day in dates for code in ("600001", "600002")) + (changed,)
    ordered = tuple(sorted(rows, key=lambda row: (row.trade_date, row.code, row.first_seen_sequence, row.revision_id)))
    snapshot = replace(_build_snapshot(root, ordered), sequence=2)
    reader = SQLiteHistoryArchiveReader(root)
    expected = tuple(project_history_cell(row.cell) for row in reader.read_code_window("600001", dates, snapshot))

    def unexpected_revision_decode(_row):
        pytest.fail("published daily projection must not decode or hash complete revisions")

    monkeypatch.setattr(partition_module, "_decode_row", unexpected_revision_decode)
    assert reader.read_published_code_window("600001", dates, snapshot) == expected
    streamed = tuple(reader.iter_published_range_by_code(dates[0], dates[-1], snapshot))
    assert tuple(cell for cell in streamed if cell.code == "600001") == expected
    assert len(streamed) == len(dates) * 2


@pytest.mark.parametrize("change", ("same_size_corruption", "atomic_replace", "pending_wal"))
def test_cached_partition_trust_detects_physical_changes(tmp_path, change, monkeypatch):
    root = tmp_path / "history"
    day = date(2026, 9, 1)
    snapshot = _build_snapshot(root, (_revision("600001", day),))
    verified = []
    verify = SQLiteHistoryMonthPartitionRepository.verify.__func__

    def observed(cls, path, reference, progress=None):
        verified.append(reference)
        return verify(cls, path, reference, progress)

    monkeypatch.setattr(SQLiteHistoryMonthPartitionRepository, "verify", classmethod(observed))
    reader = SQLiteHistoryArchiveReader(root)
    reader.read_published_code_window("600001", (day,), snapshot)
    path = root / snapshot.partitions[0].relative_path
    previous = path.stat()
    if change == "same_size_corruption":
        content = bytearray(path.read_bytes())
        content[-1] ^= 1
        path.write_bytes(content)
        os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    elif change == "atomic_replace":
        replacement = path.with_suffix(".replacement")
        replacement.write_bytes(b"x" * previous.st_size)
        os.utime(replacement, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        replacement.replace(path)
    else:
        Path(f"{path}-wal").write_bytes(b"pending")
    with pytest.raises(HistoryArchiveReadError):
        reader.read_published_code_window("600001", (day,), snapshot)
    if change == "pending_wal":
        assert len(verified) == 1
        Path(f"{path}-wal").unlink()
        assert reader.read_published_code_window("600001", (day,), snapshot)
        assert len(verified) == 2


def test_published_snapshot_reuses_unchanged_partitions_and_checks_revisions(tmp_path, monkeypatch):
    root = tmp_path / "history"
    day = date(2026, 9, 1)
    original = _revision("600001", day)
    revised = replace(
        original,
        first_seen_sequence=2,
        cell=replace(original.cell, qfq=replace(original.cell.qfq, close_price=original.cell.qfq.close_price + 0.1)),
    )
    snapshot = _build_snapshot(root, (original, revised))
    calls = []
    verify = SQLiteHistoryMonthPartitionRepository.verify.__func__

    def observe(cls, path, reference, progress=None):
        calls.append(reference)
        return verify(cls, path, reference, progress)

    monkeypatch.setattr(SQLiteHistoryMonthPartitionRepository, "verify", classmethod(observe))
    reader = SQLiteHistoryArchiveReader(root)
    assert reader.read_published_code_window("600001", (day,), snapshot) == (project_history_cell(original.cell),)
    current = replace(snapshot, sequence=2)
    assert reader.read_published_code_window("600001", (day,), current) == (project_history_cell(revised.cell),)
    assert len(calls) == 1
    invalid = replace(current, partitions=(replace(current.partitions[0], sha256="f" * 64),))
    with pytest.raises(HistoryArchiveReadError):
        reader.read_published_code_window("600001", (day,), invalid)
    assert len(calls) == 2
