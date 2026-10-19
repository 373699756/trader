from __future__ import annotations

import os
from collections import defaultdict
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import trader.download.infra.history_prices as partition_module
from trader.download.domain.baostock_daily import BaoStockDailyCell, BaoStockDailySide, BaoStockIndustryInterval
from trader.download.domain.history_control import (
    HistoryActiveSnapshot,
    HistorySnapshotPartition,
    HistorySourceIdentity,
)
from trader.download.domain.history_reference import HistoryReferenceSnapshot, HistoryStEvidence
from trader.download.domain.history_revision import HistoryRevision
from trader.download.domain.published_history import ProjectHistoryCell
from trader.download.infra.history_control_index import HistoryControlIndex
from trader.download.infra.history_prices import HistoryPricePartition
from trader.download.infra.history_snapshot_reader import (
    HistoryPartitionRevisionComparison,
    HistorySnapshotReader,
    HistorySnapshotReadError,
    RouteHistoryMonths,
)
from trader.infra.sqlite_connection import OpenSqliteConnection


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


def test_training_stream_consumes_narrow_rows_and_releases_each_month(tmp_path: Path, monkeypatch) -> None:
    dates = (date(2026, 8, 31), date(2026, 9, 1))
    snapshot = _build_snapshot(tmp_path, tuple(_revision("600001", day) for day in dates))
    events = []
    consume = HistoryPricePartition.iter_training_rows
    release = HistoryPricePartition.release_read_pages

    def consuming(self, *args, **kwargs):
        events.append(("consume", f"{self._calendar_month:02d}"))
        yield from consume(self, *args, **kwargs)

    def releasing(self):
        events.append(("release", f"{self._calendar_month:02d}"))
        release(self)

    monkeypatch.setattr(HistoryPricePartition, "iter_training_rows", consuming)
    monkeypatch.setattr(HistoryPricePartition, "release_read_pages", releasing)
    rows = tuple(HistorySnapshotReader(tmp_path).iter_training_rows(snapshot, dates, ("600001",)))
    assert len(rows) == 2
    assert events == [(phase, month) for month in ("08", "09") for phase in ("consume", "release")]


def test_training_stream_reports_invalid_projected_field_as_snapshot_error(tmp_path: Path) -> None:
    day = date(2026, 9, 1)
    snapshot = _build_snapshot(tmp_path, (_revision("600001", day),))
    path = tmp_path / snapshot.partitions[0].relative_path
    with closing(OpenSqliteConnection(path)) as connection, connection:
        connection.run("UPDATE daily_records SET qfq_trading_status='invalid'")

    with pytest.raises(HistorySnapshotReadError, match="history training partition read failed"):
        tuple(HistorySnapshotReader(tmp_path).iter_training_rows(snapshot, (day,), ("600001",)))


def _build_snapshot(root: Path, rows: tuple[HistoryRevision, ...]) -> HistoryActiveSnapshot:
    grouped: dict[tuple[int, int], list[HistoryRevision]] = defaultdict(list)
    for row in rows:
        grouped[(row.trade_date.year, row.trade_date.month)].append(row)
    references: list[HistorySnapshotPartition] = []
    for (year, month), month_rows in sorted(grouped.items()):
        path = root / f"prices_{year:04d}.sqlite3"
        repository = HistoryPricePartition(path, year, month)
        repository.initialize()
        repository.save_revisions(tuple(month_rows))
        references.append(repository.seal())
    codes = tuple(sorted({row.code for row in rows}))
    reference = HistoryReferenceSnapshot(
        codes,
        tuple(BaoStockIndustryInterval(code, date(2000, 1, 1), None, "bank", "sw") for code in codes),
        tuple(HistoryStEvidence(code, max(row.trade_date for row in rows), "clear") for code in codes),
    )
    source = HistorySourceIdentity(
        "tencent",
        "history_daily",
        f"history_ref_{reference.content_hash}_fixture",
        datetime(2026, 10, 10, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    control = HistoryControlIndex(root / "catalog.sqlite3")
    control.initialize()
    control.save_reference(reference)
    control.save_source(source)
    return HistoryActiveSnapshot(
        max(row.first_seen_sequence for row in rows),
        max(row.trade_date for row in rows),
        max(row.trade_date for row in rows),
        "a" * 64,
        "b" * 64,
        source.content_hash,
        tuple(references),
    )


def test_history_reader_routes_single_day_code_window_and_cross_month_training_rows(tmp_path: Path) -> None:
    root = tmp_path / "history"
    dates = tuple(date(2026, 8, 1) + timedelta(days=offset) for offset in range(62))
    rows = tuple(_revision(code, day) for day in dates for code in ("600001", "600002"))
    snapshot = _build_snapshot(root, rows)
    archive = HistorySnapshotReader(root)

    assert RouteHistoryMonths(dates[0], dates[-1]) == ((2026, 8), (2026, 9), (2026, 10))
    assert tuple(row.code for row in archive.read_day(dates[31], snapshot)) == ("600001", "600002")
    code_rows = archive.read_code_window("600001", dates[-61:], snapshot)
    assert tuple(row.trade_date for row in code_rows) == dates[-61:]
    assert tuple(archive.iter_snapshot_revisions(snapshot)) == rows

    training_rows = tuple(archive.iter_training_rows(snapshot, dates, {"600001", "600002"}))
    assert len(training_rows) == 124
    assert tuple((row.trade_date, row.code) for row in training_rows) == tuple(
        (day, code) for day in dates for code in ("600001", "600002")
    )
    current_rows = tuple(archive.iter_training_rows(snapshot, dates, {"600001"}))
    assert len(current_rows) == 62
    assert {row.code for row in current_rows} == {"600001"}

    with pytest.raises(ValueError, match="cutoff"):
        archive.read_day(dates[-1] + timedelta(days=1), snapshot)
    with pytest.raises(HistorySnapshotReadError, match="cover"):
        tuple(
            archive.iter_training_rows(
                replace(snapshot, partitions=snapshot.partitions[:-1]),
                dates,
                {"600001", "600002"},
            )
        )


@pytest.mark.slow
def test_history_reader_streams_2000_sessions_without_directory_scan_or_unbounded_state(tmp_path: Path) -> None:
    root = tmp_path / "history"
    dates = tuple(date(2020, 1, 1) + timedelta(days=offset) for offset in range(2000))
    rows = tuple(_revision("600001", day) for day in dates)
    snapshot = _build_snapshot(root, rows)
    archive = HistorySnapshotReader(root)

    descriptor_root = Path("/proc/self/fd")
    baseline_descriptors = len(tuple(descriptor_root.iterdir())) if descriptor_root.is_dir() else None
    peak_descriptors = baseline_descriptors
    replayed_rows: list[HistoryRevision] = []
    for row in archive.iter_snapshot_revisions(snapshot):
        replayed_rows.append(row)
        if peak_descriptors is not None:
            peak_descriptors = max(peak_descriptors, len(tuple(descriptor_root.iterdir())))
    training_rows = tuple(archive.iter_training_rows(snapshot, dates, {"600001"}))

    assert tuple(replayed_rows) == rows
    assert len(snapshot.partitions) == len(RouteHistoryMonths(dates[0], dates[-1]))
    assert len(training_rows) == 2000
    assert training_rows[-1].trade_date == dates[-1]
    assert all(row.point is not None for row in training_rows)
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
    archive = HistorySnapshotReader(root)
    original = HistoryPricePartition.verify.__func__
    verified: list[Path] = []

    def verify(cls, path: Path, reference: HistorySnapshotPartition, progress=None) -> None:
        verified.append(path)
        original(cls, path, reference, progress)

    monkeypatch.setattr(HistoryPricePartition, "verify", classmethod(verify))

    observed: list[tuple[int, int, int, int, int, str]] = []
    archive.verify_snapshot(snapshot, lambda *values: observed.append(values))
    assert archive.count_range(dates[0], dates[-1], snapshot, codes=("600001",)) == len(dates)
    assert archive.count_range(dates[0], dates[-1], snapshot, codes=("600001", "600002")) == len(rows)
    assert len(tuple(archive.iter_code("600001", dates[0], dates[-1], snapshot))) == len(dates)
    assert len(tuple(archive.iter_code("600002", dates[0], dates[-1], snapshot))) == len(dates)

    assert observed[-1][:3] == (len(snapshot.partitions), len(snapshot.partitions), len(snapshot.partitions))
    assert any(
        completed == 0 and current == 1 and phase == "logical" for completed, _, current, _, _, phase in observed
    )
    assert len(verified) == len(snapshot.partitions)


def test_published_snapshot_reestablishes_hash_trust_without_sqlite_rescan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "history"
    dates = tuple(date(2026, 8, 30) + timedelta(days=offset) for offset in range(4))
    snapshot = _build_snapshot(root, tuple(_revision("600001", day) for day in dates))
    published = HistoryPricePartition.verify_logical_content.__func__
    published_calls: list[HistorySnapshotPartition] = []

    def verify_published(cls, path, reference, progress=None, **kwargs):
        published_calls.append(reference)
        return published(cls, path, reference, progress, **kwargs)

    def unexpected_full_verify(*_args, **_kwargs):
        pytest.fail("training trust must not repeat SQLite quick_check and row-count scans")

    monkeypatch.setattr(
        HistoryPricePartition,
        "verify_logical_content",
        classmethod(verify_published),
    )
    monkeypatch.setattr(HistoryPricePartition, "verify", classmethod(unexpected_full_verify))
    reader = HistorySnapshotReader(root)
    progress: list[tuple[int, int, int, int, int, str]] = []

    reader.verify_published_snapshot(snapshot, lambda *values: progress.append(values))

    assert reader.count_range(dates[0], dates[-1], snapshot, codes=("600001",)) == len(dates)
    assert published_calls == list(snapshot.partitions)
    assert progress[-1][:3] == (len(snapshot.partitions), len(snapshot.partitions), len(snapshot.partitions))
    assert {item[-1] for item in progress} == {"logical"}


def test_published_snapshot_hash_trust_rejects_changed_bytes(tmp_path: Path) -> None:
    root = tmp_path / "history"
    day = date(2026, 9, 1)
    snapshot = _build_snapshot(root, (_revision("600001", day),))
    path = root / snapshot.partitions[0].relative_path
    with closing(OpenSqliteConnection(path)) as connection, connection:
        connection.run("UPDATE daily_records SET content_hash=?", ("0" * 64,))

    with pytest.raises(HistorySnapshotReadError, match="verification failed"):
        HistorySnapshotReader(root).verify_published_snapshot(snapshot)


def test_explicit_full_verification_upgrades_published_byte_trust(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "history"
    day = date(2026, 9, 1)
    snapshot = _build_snapshot(root, (_revision("600001", day),))
    published = HistoryPricePartition.verify_logical_content.__func__
    full = HistoryPricePartition.verify.__func__
    calls: list[str] = []

    def verify_published(cls, path, reference, progress=None, **kwargs):
        calls.append("logical")
        return published(cls, path, reference, progress, **kwargs)

    def verify_full(cls, path, reference, progress=None):
        calls.append("full")
        return full(cls, path, reference, progress)

    monkeypatch.setattr(
        HistoryPricePartition,
        "verify_logical_content",
        classmethod(verify_published),
    )
    monkeypatch.setattr(HistoryPricePartition, "verify", classmethod(verify_full))
    reader = HistorySnapshotReader(root)

    reader.verify_published_snapshot(snapshot)
    reader.verify_snapshot(snapshot)

    assert calls == ["logical", "full", "logical"]


def test_revision_comparison_uses_one_current_physical_partition_for_both_sequences(tmp_path: Path) -> None:
    root = tmp_path / "history"
    first = date(2026, 9, 9)
    second = date(2026, 9, 10)
    original = _revision("600001", first)
    revised = replace(
        original,
        first_seen_sequence=2,
        cell=replace(original.cell, qfq=replace(original.cell.qfq, close_price=original.cell.qfq.close_price + 0.1)),
    )
    unchanged = _revision("600001", second)
    snapshot = _build_snapshot(root, (original, revised, unchanged))
    comparison = HistoryPartitionRevisionComparison(snapshot.partitions[0], 1, 2)

    changed = HistorySnapshotReader(root).revised_dates(first, second, comparison)

    assert changed == (first,)


def test_published_daily_projection_preserves_facts_without_revision_decode(tmp_path, monkeypatch):
    root = tmp_path / "history"
    dates = tuple(date(2026, 8, 1) + timedelta(days=offset) for offset in range(62))
    original = _revision("600001", dates[0])
    changed = replace(original, first_seen_sequence=2, is_st=True)
    rows = tuple(_revision(code, day) for day in dates for code in ("600001", "600002")) + (changed,)
    ordered = tuple(sorted(rows, key=lambda row: (row.trade_date, row.code, row.first_seen_sequence, row.revision_id)))
    snapshot = replace(_build_snapshot(root, ordered), sequence=2)
    reader = HistorySnapshotReader(root)
    expected = tuple(ProjectHistoryCell(row.cell) for row in reader.read_code_window("600001", dates, snapshot))

    def unexpected_revision_decode(_row):
        pytest.fail("published daily projection must not decode or hash complete revisions")

    monkeypatch.setattr(partition_module, "_decode_row", unexpected_revision_decode)
    assert reader.read_published_code_window("600001", dates, snapshot) == expected
    streamed = tuple(reader.iter_published_range_by_code(dates[0], dates[-1], snapshot))
    assert tuple(cell for cell in streamed if cell.code == "600001") == expected
    assert len(streamed) == len(dates) * 2


@pytest.mark.parametrize("change", ("same_size_corruption", "atomic_replace"))
def test_cached_partition_trust_detects_physical_changes(tmp_path, change, monkeypatch):
    root = tmp_path / "history"
    day = date(2026, 9, 1)
    snapshot = _build_snapshot(root, (_revision("600001", day),))
    verified = []
    verify = HistoryPricePartition.verify.__func__

    def observed(cls, path, reference, progress=None):
        verified.append(reference)
        return verify(cls, path, reference, progress)

    monkeypatch.setattr(HistoryPricePartition, "verify", classmethod(observed))
    reader = HistorySnapshotReader(root)
    reader.read_published_code_window("600001", (day,), snapshot)
    path = root / snapshot.partitions[0].relative_path
    previous = path.stat()
    if change == "same_size_corruption":
        with closing(OpenSqliteConnection(path)) as connection, connection:
            connection.run("UPDATE daily_records SET content_hash=?", ("0" * 64,))
        os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    elif change == "atomic_replace":
        replacement = path.with_suffix(".replacement")
        replacement.write_bytes(b"x" * previous.st_size)
        os.utime(replacement, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        replacement.replace(path)
    else:
        Path(f"{path}-wal").write_bytes(b"pending")
    with pytest.raises(HistorySnapshotReadError):
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
    verify = HistoryPricePartition.verify.__func__

    def observe(cls, path, reference, progress=None):
        calls.append(reference)
        return verify(cls, path, reference, progress)

    monkeypatch.setattr(HistoryPricePartition, "verify", classmethod(observe))
    reader = HistorySnapshotReader(root)
    old_reference = HistoryPricePartition(root / snapshot.partitions[0].relative_path, 2026, 9).seal(1)
    old_snapshot = replace(snapshot, sequence=1, partitions=(old_reference,))
    assert reader.read_published_code_window("600001", (day,), old_snapshot) == (ProjectHistoryCell(original.cell),)
    current = replace(snapshot, sequence=2)
    assert reader.read_published_code_window("600001", (day,), current) == (ProjectHistoryCell(revised.cell),)
    assert len(calls) == 2
    invalid = replace(current, partitions=(replace(current.partitions[0], logical_hash="f" * 64),))
    with pytest.raises(HistorySnapshotReadError):
        reader.read_published_code_window("600001", (day,), invalid)
    assert len(calls) == 3
