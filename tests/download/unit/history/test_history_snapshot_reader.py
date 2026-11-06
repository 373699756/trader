from __future__ import annotations

from tests.helpers.shared.configuration import SqlitePolicyFixture

import os
import sqlite3
from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import trader.download.infra.history_prices as partition_module
from tests.helpers.shared.reference_fixtures import PublishHistoryReferenceFixture
from trader.download.domain.baostock_daily import BaoStockDailyCell, BaoStockDailySide, BaoStockIndustryInterval
from trader.download.domain.history_control import (
    HistorySnapshot,
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
from trader.download.infra.history_sqlite_read_connection import PrefetchHistoryPricePages
from trader.infra.sqlite_codec import EncodeDate
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction
from trader.infra.sqlite_publication import PublishSqliteFile


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


@pytest.mark.parametrize(
    ("dates", "expected_events"),
    (
        ((date(2026, 8, 31), date(2026, 9, 1)), (("consume", "08"), ("consume", "09"), ("release", "09"))),
        (
            (date(2026, 12, 31), date(2027, 1, 1)),
            (("consume", "12"), ("release", "12"), ("consume", "01"), ("release", "01")),
        ),
    ),
)
def test_training_stream_releases_pages_after_each_annual_file(
    tmp_path: Path, monkeypatch, dates, expected_events
) -> None:
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
    rows = tuple(HistorySnapshotReader(tmp_path, sqlite_policy=SqlitePolicyFixture()).iter_training_rows(snapshot, dates, ("600001",)))
    assert len(rows) == 2
    assert events == list(expected_events)


def test_training_stream_early_close_releases_open_annual_file(tmp_path: Path, monkeypatch) -> None:
    dates = (date(2026, 8, 31), date(2026, 9, 1))
    snapshot = _build_snapshot(tmp_path, tuple(_revision("600001", day) for day in dates))
    released = []
    release = HistoryPricePartition.release_read_pages

    def releasing(self):
        released.append(self._calendar_month)
        release(self)

    monkeypatch.setattr(HistoryPricePartition, "release_read_pages", releasing)
    stream = HistorySnapshotReader(tmp_path, sqlite_policy=SqlitePolicyFixture()).iter_training_rows(snapshot, dates, ("600001",))
    assert next(stream).trade_date == dates[0]
    stream.close()
    assert released == [8]


def test_training_stream_reports_invalid_projected_field_as_snapshot_error(tmp_path: Path) -> None:
    day = date(2026, 9, 1)
    snapshot = _build_snapshot(tmp_path, (_revision("600001", day),))
    path = tmp_path / snapshot.partitions[0].relative_path
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        connection.run("UPDATE daily_price_revision SET qfq_trading_status='invalid'")
    # This fixture tests typed projection errors after a valid file publication.
    PublishSqliteFile(path, sqlite_policy=SqlitePolicyFixture())

    with pytest.raises(HistorySnapshotReadError, match="history training partition read failed"):
        tuple(HistorySnapshotReader(tmp_path, sqlite_policy=SqlitePolicyFixture()).iter_training_rows(snapshot, (day,), ("600001",)))


def _build_snapshot(root: Path, rows: tuple[HistoryRevision, ...]) -> HistorySnapshot:
    grouped: dict[tuple[int, int], list[HistoryRevision]] = defaultdict(list)
    for row in rows:
        grouped[(row.trade_date.year, row.trade_date.month)].append(row)
    references: list[HistorySnapshotPartition] = []
    for (year, month), month_rows in sorted(grouped.items()):
        path = root / f"prices_{year:04d}.sqlite3"
        index = HistoryPricePartition(path, year, month, sqlite_policy=SqlitePolicyFixture())
        index.initialize()
        index.save_revisions(tuple(month_rows))
        references.append(index.seal())
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
    reference_path = root / "company_st.sqlite3"
    publication_id = PublishHistoryReferenceFixture(reference_path, reference)
    control = HistoryControlIndex(root / "catalog.sqlite3", reference_path=reference_path, sqlite_policy=SqlitePolicyFixture())
    control.initialize()
    control.save_reference(reference, publication_id)
    control.save_source(source)
    return HistorySnapshot(
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
    history = HistorySnapshotReader(root, sqlite_policy=SqlitePolicyFixture())

    assert RouteHistoryMonths(dates[0], dates[-1]) == ((2026, 8), (2026, 9), (2026, 10))
    assert tuple(row.code for row in history.read_day(dates[31], snapshot)) == ("600001", "600002")
    code_rows = history.read_code_window("600001", dates[-61:], snapshot)
    assert tuple(row.trade_date for row in code_rows) == dates[-61:]
    assert tuple(history.iter_snapshot_revisions(snapshot)) == rows

    training_rows = tuple(history.iter_training_rows(snapshot, dates, {"600001", "600002"}))
    assert len(training_rows) == 124
    assert tuple((row.trade_date, row.code) for row in training_rows) == tuple(
        (day, code) for day in dates for code in ("600001", "600002")
    )
    current_rows = tuple(history.iter_training_rows(snapshot, dates, {"600001"}))
    assert len(current_rows) == 62
    assert {row.code for row in current_rows} == {"600001"}

    with pytest.raises(ValueError, match="cutoff"):
        history.read_day(dates[-1] + timedelta(days=1), snapshot)
    with pytest.raises(HistorySnapshotReadError, match="cover"):
        tuple(
            history.iter_training_rows(
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
    history = HistorySnapshotReader(root, sqlite_policy=SqlitePolicyFixture())

    descriptor_root = Path("/proc/self/fd")
    baseline_descriptors = len(tuple(descriptor_root.iterdir())) if descriptor_root.is_dir() else None
    peak_descriptors = baseline_descriptors
    replayed_rows: list[HistoryRevision] = []
    for row in history.iter_snapshot_revisions(snapshot):
        replayed_rows.append(row)
        if peak_descriptors is not None:
            peak_descriptors = max(peak_descriptors, len(tuple(descriptor_root.iterdir())))
    training_rows = tuple(history.iter_training_rows(snapshot, dates, {"600001"}))

    assert tuple(replayed_rows) == rows
    assert len(snapshot.partitions) == len(RouteHistoryMonths(dates[0], dates[-1]))
    assert len(training_rows) == 2000
    assert training_rows[-1].trade_date == dates[-1]
    assert all(row.point is not None for row in training_rows)
    assert len(history.read_code_window("600001", dates[-251:], snapshot)) == 251
    if baseline_descriptors is not None and peak_descriptors is not None:
        assert peak_descriptors <= baseline_descriptors + 4


def test_verified_snapshot_reuses_each_file_checksum_and_counts_latest_rows(tmp_path, monkeypatch):
    from trader.infra import sqlite_publication
    root = tmp_path / "history"
    dates = tuple(date(2026, 8, 30) + timedelta(days=offset) for offset in range(4))
    rows = tuple(_revision(code, day) for day in dates for code in ("600001", "600002"))
    snapshot = _build_snapshot(root, rows)
    original = sqlite_publication._digest
    verified = []

    def digest(path, size, check):
        verified.append(path)
        return original(path, size, check)

    monkeypatch.setattr(sqlite_publication, "_digest", digest)
    history = HistorySnapshotReader(root, sqlite_policy=SqlitePolicyFixture())
    observed = []
    history.verify_snapshot(snapshot, lambda *values: observed.append(values))
    assert history.count_range(dates[0], dates[-1], snapshot, codes=("600001",)) == len(dates)
    assert len(tuple(history.iter_code("600002", dates[0], dates[-1], snapshot))) == len(dates)
    assert verified == [root / "prices_2026.sqlite3"]
    assert observed[-1][:3] == (1, 1, 1)
    assert {item[-1] for item in observed} == {"hash"}


def test_snapshot_loading_never_selects_business_rows_for_verification(tmp_path, monkeypatch):
    root = tmp_path / "history"
    dates = tuple(date(2026, 8, 30) + timedelta(days=offset) for offset in range(4))
    snapshot = _build_snapshot(root, tuple(_revision("600001", day) for day in dates))
    reader = HistorySnapshotReader(root, sqlite_policy=SqlitePolicyFixture())
    from trader.infra.sqlite_connection import SqliteConnection
    original = SqliteConnection.run
    statements = []

    def execute(connection, sql, parameters=()):
        statements.append(sql)
        assert "daily_price" not in sql.lower()
        assert "quick_check" not in sql.lower() and "integrity_check" not in sql.lower()
        return original(connection, sql, parameters)

    with monkeypatch.context() as isolated:
        isolated.setattr(SqliteConnection, "run", execute)
        reader.verify_snapshot(snapshot)
    assert statements
    assert reader.count_range(dates[0], dates[-1], snapshot, codes=("600001",)) == len(dates)


def test_published_snapshot_hash_trust_rejects_changed_bytes(tmp_path: Path) -> None:
    root = tmp_path / "history"
    day = date(2026, 9, 1)
    snapshot = _build_snapshot(root, (_revision("600001", day),))
    path = root / snapshot.partitions[0].relative_path
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        connection.run("UPDATE daily_price_revision SET content_hash=?", (bytes(32),))

    with pytest.raises(HistorySnapshotReadError, match="verification failed"):
        HistorySnapshotReader(root, sqlite_policy=SqlitePolicyFixture()).verify_snapshot(snapshot)


def test_repeated_snapshot_load_reuses_verified_file_fingerprint(tmp_path, monkeypatch):
    from trader.infra import sqlite_publication
    root = tmp_path / "history"
    snapshot = _build_snapshot(root, (_revision("600001", date(2026, 9, 1)),))
    original = sqlite_publication._digest
    calls = []

    def digest(path, size, check):
        calls.append(path)
        return original(path, size, check)

    monkeypatch.setattr(sqlite_publication, "_digest", digest)
    reader = HistorySnapshotReader(root, sqlite_policy=SqlitePolicyFixture())
    reader.verify_snapshot(snapshot)
    reader.verify_snapshot(snapshot)
    assert calls == [root / "prices_2026.sqlite3"]


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

    changed = HistorySnapshotReader(root, sqlite_policy=SqlitePolicyFixture()).revised_dates(first, second, comparison)

    assert changed == (first,)


def test_published_daily_projection_preserves_facts_without_revision_decode(tmp_path, monkeypatch):
    root = tmp_path / "history"
    dates = tuple(date(2026, 8, 1) + timedelta(days=offset) for offset in range(62))
    original = _revision("600001", dates[0])
    changed = replace(original, first_seen_sequence=2, is_st=True)
    rows = tuple(_revision(code, day) for day in dates for code in ("600001", "600002")) + (changed,)
    ordered = tuple(sorted(rows, key=lambda row: (row.trade_date, row.code, row.first_seen_sequence, row.revision_id)))
    snapshot = replace(_build_snapshot(root, ordered), sequence=2)
    expected = tuple(
        ProjectHistoryCell(row.cell) for row in HistorySnapshotReader(root, sqlite_policy=SqlitePolicyFixture()).read_code_window("600001", dates, snapshot)
    )
    reader = HistorySnapshotReader(root, sqlite_policy=SqlitePolicyFixture())

    def unexpected_revision_decode(_row):
        pytest.fail("published daily projection must not decode or hash complete revisions")

    monkeypatch.setattr(partition_module, "_decode_row", unexpected_revision_decode)
    prefetched = []

    def prefetch(path, check=None):
        prefetched.append(path.name)
        PrefetchHistoryPricePages(path, check)

    monkeypatch.setattr("trader.download.infra.history_sqlite_read_connection.PrefetchHistoryPricePages", prefetch)
    assert reader.read_published_code_window("600001", dates, snapshot) == expected
    streamed = tuple(reader.iter_published_range_by_code(dates[0], dates[-1], snapshot))
    assert tuple(cell for cell in streamed if cell.code == "600001") == expected
    assert len(streamed) == len(dates) * 2
    assert prefetched == []


@pytest.mark.parametrize("selected_count", (0, 1, 501))
def test_selected_published_stream_filters_before_decode_and_opens_each_year_once(tmp_path, monkeypatch, selected_count):
    root = tmp_path / "history"
    days = (date(2026, 12, 31), date(2027, 1, 1))
    codes = tuple(f"600{number:03d}" for number in range(503))
    snapshot = _build_snapshot(root, tuple(_revision(code, day) for day in days for code in codes))
    reader = HistorySnapshotReader(root, sqlite_policy=SqlitePolicyFixture())
    selected = frozenset(codes[:selected_count])
    connections, decoded = [], []
    open_connection = HistoryPricePartition._read_connection
    decode = partition_module.PublishedCellFromPriceColumns

    def opened(partition):
        connections.append(partition._calendar_year)
        return open_connection(partition)

    def decode_selected(row):
        assert row[1] in selected
        decoded.append((row[1], row[0]))
        return decode(row)

    monkeypatch.setattr(HistoryPricePartition, "_read_connection", opened)
    monkeypatch.setattr(partition_module, "PublishedCellFromPriceColumns", decode_selected)
    result = tuple(reader.iter_published_range_by_code(days[0], days[-1], snapshot, codes=selected))
    assert tuple((row.code, row.trade_date) for row in result) == tuple((code, day) for code in sorted(selected) for day in days)
    assert len(decoded) == selected_count * len(days)
    assert connections == ([2026, 2027] if selected else [])


def test_selected_published_stream_cancellation_closes_open_connections(tmp_path, monkeypatch):
    root = tmp_path / "history"
    days = (date(2026, 12, 31), date(2027, 1, 1))
    snapshot = _build_snapshot(root, tuple(_revision("600001", day) for day in days))
    reader = HistorySnapshotReader(root, sqlite_policy=SqlitePolicyFixture())
    connections = []
    cancelled = False
    original = HistoryPricePartition._read_connection

    def opened(partition):
        connection = original(partition)
        connections.append(connection)
        return connection

    def check():
        if cancelled:
            raise RuntimeError("history_preload_cancelled")

    monkeypatch.setattr(HistoryPricePartition, "_read_connection", opened)
    stream = reader.iter_published_range_by_code(days[0], days[-1], snapshot, codes=frozenset({"600001"}), check=check)
    assert next(stream).trade_date == days[0]
    cancelled = True
    with pytest.raises(RuntimeError, match="history_preload_cancelled"):
        tuple(stream)
    assert len(connections) == 2
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.run("SELECT 1")


def test_published_read_skips_payload_revalidation_but_explicit_audit_rejects_corruption(tmp_path):
    root = tmp_path / "history"
    dates = tuple(date(2025, 1, 1) + timedelta(days=offset) for offset in range(300))
    rows = tuple(_revision("600001", day) for day in dates)
    snapshot = _build_snapshot(root, rows)
    reader = HistorySnapshotReader(root, sqlite_policy=SqlitePolicyFixture())
    assert len(reader.read_published_code_window("600001", dates, snapshot)) == 300
    reference = snapshot.partitions[-1]
    with SqliteSession(root / reference.relative_path) as connection, SqliteTransaction(connection):
        connection.run(
            "UPDATE daily_price_revision SET qfq_amount=qfq_amount+1 WHERE trade_date=?", (EncodeDate(dates[-1]),)
        )
    with pytest.raises(HistorySnapshotReadError):
        reader.read_published_code_window("600001", dates, snapshot)
    with pytest.raises(HistorySnapshotReadError):
        reader.verify_snapshot(snapshot)


@pytest.mark.parametrize("change", ("same_size_corruption", "atomic_replace"))
def test_cached_partition_trust_detects_physical_changes(tmp_path, change, monkeypatch):
    root = tmp_path / "history"
    day = date(2026, 9, 1)
    snapshot = _build_snapshot(root, (_revision("600001", day),))
    verified = []
    verify = HistoryPricePartition.verify.__func__

    def observed(cls, path, reference, progress=None, *, sqlite_policy):
        verified.append(reference)
        return verify(cls, path, reference, progress, sqlite_policy=sqlite_policy)

    monkeypatch.setattr(HistoryPricePartition, "verify", classmethod(observed))
    reader = HistorySnapshotReader(root, sqlite_policy=SqlitePolicyFixture())
    reader.verify_snapshot(snapshot)
    path = root / snapshot.partitions[0].relative_path
    previous = path.stat()
    if change == "same_size_corruption":
        with SqliteSession(path) as connection, SqliteTransaction(connection):
            connection.run("UPDATE daily_price_revision SET content_hash=?", (bytes(32),))
        os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    elif change == "atomic_replace":
        replacement = path.with_suffix(".replacement")
        replacement.write_bytes(b"x" * previous.st_size)
        os.utime(replacement, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        replacement.replace(path)
    else:
        Path(f"{path}-wal").write_bytes(b"pending")
    with pytest.raises(HistorySnapshotReadError):
        reader.verify_snapshot(snapshot)
    if change == "pending_wal":
        assert len(verified) == 1
        Path(f"{path}-wal").unlink()
        assert reader.read_published_code_window("600001", (day,), snapshot)
        assert len(verified) == 2


def test_file_verification_preserves_sequence_reads_and_detects_later_changes(tmp_path, monkeypatch):
    root = tmp_path / "history"
    day = date(2026, 9, 1)
    original = _revision("600001", day)
    revised = replace(
        original,
        first_seen_sequence=2,
        cell=replace(original.cell, qfq=replace(original.cell.qfq, close_price=original.cell.qfq.close_price + 0.1)),
    )
    snapshot = _build_snapshot(root, (original, revised))
    path = root / snapshot.partitions[0].relative_path
    old_reference = HistoryPricePartition(path, 2026, 9, sqlite_policy=SqlitePolicyFixture()).seal(1)
    reader = HistorySnapshotReader(root, sqlite_policy=SqlitePolicyFixture())
    old_snapshot = replace(snapshot, sequence=1, partitions=(old_reference,))
    assert reader.read_published_code_window("600001", (day,), old_snapshot) == (ProjectHistoryCell(original.cell),)
    assert reader.read_published_code_window("600001", (day,), snapshot) == (ProjectHistoryCell(revised.cell),)
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        connection.run("UPDATE daily_price_revision SET qfq_amount=qfq_amount+1")
    with pytest.raises(HistorySnapshotReadError):
        reader.verify_snapshot(snapshot)


def test_annual_published_stream_keeps_each_months_sequence_and_ignores_unpublished_revisions(tmp_path):
    first, second, third = date(2026, 7, 1), date(2026, 8, 1), date(2026, 9, 1)
    rows = tuple(_revision("600001", day) for day in (first, second, third))
    snapshot = _build_snapshot(tmp_path, rows)
    # The last month has its own sequence; it must not change the earlier group's visibility.
    revisions = tuple(
        replace(
            row,
            first_seen_sequence=2,
            cell=replace(row.cell, qfq=replace(row.cell.qfq, close_price=row.cell.qfq.close_price + 0.1)),
        )
        for row in (rows[0], rows[2])
    )
    partition = HistoryPricePartition(tmp_path / snapshot.partitions[0].relative_path, 2026, 9, sqlite_policy=SqlitePolicyFixture())
    partition.save_revisions(revisions)
    latest = partition.seal(2)
    snapshot = replace(snapshot, sequence=2, partitions=(*snapshot.partitions[:2], latest))
    reader = HistorySnapshotReader(tmp_path, sqlite_policy=SqlitePolicyFixture())
    expected = (
        ProjectHistoryCell(rows[0].cell),
        ProjectHistoryCell(rows[1].cell),
        ProjectHistoryCell(revisions[1].cell),
    )
    assert tuple(reader.iter_published_range_by_code(first, third, snapshot)) == expected
