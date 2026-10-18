from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from trader.download.domain.baostock_daily import BaoStockDailyCell, BaoStockDailySide
from trader.download.domain.history_revision import HistoryRevision
from trader.download.infra import history_month_partition as partition_module
from trader.download.infra.history_month_partition import (
    HistoryMonthPartitionConflictError,
    HistoryMonthPartitionError,
    HistoryMonthPrices,
)
from trader.infra.sqlite_connection import OpenSqliteConnection


def _side(day: date, adjustment: str, close: float) -> BaoStockDailySide:
    return BaoStockDailySide(
        "600001",
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


def _revision(day: date, sequence: int, close: float) -> HistoryRevision:
    raw = _side(day, "unadjusted", close)
    qfq = _side(day, "qfq", close - 1.0)
    return HistoryRevision(
        sequence,
        "main",
        BaoStockDailyCell("600001", day, "complete", raw, qfq),
        None,
        None,
        None,
    )


def test_month_schema_initialization_commits_all_ddl_once(tmp_path: Path) -> None:
    statements: list[str] = []
    month = HistoryMonthPrices(tmp_path / "partitions/2026/09.sqlite3", 2026, 9, statement_trace=statements.append)
    month.initialize()
    assert sum(sql.strip().upper().startswith("BEGIN IMMEDIATE") for sql in statements) == 1
    assert sum(sql.strip().upper() == "COMMIT" for sql in statements) == 1


def test_first_build_insert_rejects_duplicate_without_changing_durable_rows(tmp_path: Path) -> None:
    path = tmp_path / "partitions/2026/09.sqlite3"
    month = HistoryMonthPrices(path, 2026, 9)
    month.initialize()
    revision = _revision(date(2026, 9, 10), 1, 10.0)
    session = month.open_write_session(fresh=True)
    try:
        session.save_revision_batches((revision,))
        with pytest.raises(HistoryMonthPartitionError, match="write failed"):
            session.save_revision_batches((revision,))
    finally:
        session.close()
    assert month.read_day(revision.trade_date, snapshot_sequence=1) == (revision,)
    assert month.seal(1).row_count == 1


def test_only_unpublished_unknown_observation_can_gain_a_price_in_same_sequence(tmp_path: Path) -> None:
    month = HistoryMonthPrices(tmp_path / "partitions/2026/09.sqlite3", 2026, 9)
    month.initialize()
    day = date(2026, 9, 10)
    missing = HistoryRevision(
        1, "main", BaoStockDailyCell("600001", day, "unknown_missing", None, None), None, None, None
    )
    complete = _revision(day, 1, 10.0)
    with closing(month.open_write_session(fresh=True)) as session:
        session.save_revision_batches((missing,))
    with closing(month.open_write_session(pending_sequence=2)) as session:
        with pytest.raises(HistoryMonthPartitionConflictError, match="sequence"):
            session.save_revision_batches((complete,))
    with closing(month.open_write_session(pending_sequence=1)) as session:
        session.save_revision_batches((complete,))
    assert month.read_day(day, snapshot_sequence=1) == (complete,)
    assert month.seal(1).row_count == 1
    with closing(month.open_write_session(pending_sequence=1)) as session:
        with pytest.raises(HistoryMonthPartitionConflictError, match="sequence"):
            session.save_revision_batches((_revision(day, 1, 11.0),))


@pytest.mark.parametrize("status", ("qfq_missing", "unadjusted_missing", "tencent_inferred_suspended"))
def test_pending_incomplete_observation_can_gain_a_complete_pair(tmp_path: Path, status: str) -> None:
    month = HistoryMonthPrices(tmp_path / "partitions/2026/09.sqlite3", 2026, 9)
    month.initialize()
    day = date(2026, 9, 10)
    raw = _side(day, "unadjusted", 10.0) if status == "qfq_missing" else None
    qfq = _side(day, "qfq", 9.0) if status == "unadjusted_missing" else None
    incomplete = HistoryRevision(1, "main", BaoStockDailyCell("600001", day, status, raw, qfq), None, None, None)
    with closing(month.open_write_session(fresh=True)) as session:
        session.save_revision_batches((incomplete,))
    complete = _revision(day, 1, 10.0)
    with closing(month.open_write_session(pending_sequence=1)) as session:
        session.save_revision_batches((complete,))
    assert month.read_day(day, snapshot_sequence=1) == (complete,)


@pytest.mark.parametrize("status", ("qfq_missing", "unadjusted_missing"))
def test_pending_completion_preserves_the_existing_price_side(tmp_path: Path, status: str) -> None:
    month = HistoryMonthPrices(tmp_path / "partitions/2026/09.sqlite3", 2026, 9)
    month.initialize()
    day = date(2026, 9, 10)
    complete = _revision(day, 1, 10.0)
    initial = HistoryRevision(
        1,
        "main",
        BaoStockDailyCell(
            "600001",
            day,
            status,  # type: ignore[arg-type]
            complete.cell.unadjusted if status == "qfq_missing" else None,
            complete.cell.qfq if status == "unadjusted_missing" else None,
        ),
        None,
        None,
        None,
    )
    changed = _revision(day, 1, 11.0)
    candidate = HistoryRevision(
        1,
        "main",
        BaoStockDailyCell(
            "600001",
            day,
            "complete",
            changed.cell.unadjusted if status == "qfq_missing" else complete.cell.unadjusted,
            changed.cell.qfq if status == "unadjusted_missing" else complete.cell.qfq,
        ),
        None,
        None,
        None,
    )
    with closing(month.open_write_session(fresh=True)) as session:
        session.save_revision_batches((initial,))
    with closing(month.open_write_session(pending_sequence=1)) as session:
        with pytest.raises(HistoryMonthPartitionConflictError, match="retained price"):
            session.save_revision_batches((candidate,))
    assert month.read_day(day, snapshot_sequence=1) == (initial,)
    with closing(month.open_write_session(pending_sequence=1)) as session:
        session.save_revision_batches((complete,))
    assert month.read_day(day, snapshot_sequence=1) == (complete,)


def test_month_partition_schema_revision_replay_and_indexes(tmp_path: Path) -> None:
    path = tmp_path / "partitions/2026/09.sqlite3"
    repository = HistoryMonthPrices(path, 2026, 9)
    repository.initialize()
    with closing(OpenSqliteConnection(path)) as connection, connection:
        assert connection.execute("PRAGMA page_size").fetchone() == (8192,)
    original = _revision(date(2026, 9, 10), 1, 10.0)
    revised = _revision(date(2026, 9, 10), 3, 11.0)

    repository.save_revisions((original, original))
    repository.save_revisions((replace(original, first_seen_sequence=2),))
    repository.save_revisions((revised,))

    assert repository.read_day(date(2026, 9, 10), snapshot_sequence=1) == (original,)
    assert repository.read_day(date(2026, 9, 10), snapshot_sequence=2) == (original,)
    assert repository.read_day(date(2026, 9, 10), snapshot_sequence=3) == (revised,)
    assert repository.read_day(date(2026, 9, 10), snapshot_sequence=3, board="star") == ()
    assert repository.read_code("600001", date(2026, 9, 1), date(2026, 9, 30), snapshot_sequence=3) == (revised,)
    with closing(OpenSqliteConnection(path)) as connection, connection:
        indexes = {
            name: tuple(row[2] for row in connection.execute(f"PRAGMA index_info('{name}')"))
            for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")
        }
    assert indexes["history_month_code_date_idx"][:2] == ("code", "trade_date")
    assert indexes["history_month_date_board_code_idx"][:3] == ("trade_date", "board", "code")
    assert indexes["history_month_observation_code_date_idx"][:3] == (
        "code",
        "trade_date",
        "sync_sequence",
    )

    reverted = _revision(date(2026, 9, 10), 4, 10.0)
    repository.save_revisions((reverted,))
    assert repository.read_day(date(2026, 9, 10), snapshot_sequence=4) == (original,)


def test_month_partition_batches_revision_context_and_uses_specialized_latest_sql(tmp_path: Path) -> None:
    path = tmp_path / "partitions/2026/09.sqlite3"
    statements: list[str] = []
    repository = HistoryMonthPrices(path, 2026, 9, statement_trace=statements.append)
    repository.initialize()
    statements.clear()
    revisions = tuple(_revision(date(2026, 9, day), 1, 10.0 + day) for day in range(1, 16))

    repository.save_revisions(revisions)

    selects = tuple(statement for statement in statements if statement.lstrip().upper().startswith("SELECT"))
    assert len(selects) == 4  # metadata plus three set-based conflict/context reads
    assert sum(statement.lstrip().upper().startswith("BEGIN IMMEDIATE") for statement in statements) == 1
    statements.clear()

    assert len(tuple(repository.iter_range(date(2026, 9, 1), date(2026, 9, 30), snapshot_sequence=1))) == 15
    latest_sql = next(statement for statement in statements if statement.lstrip().startswith("WITH latest AS"))
    assert "ROW_NUMBER" in latest_sql
    assert "NOT EXISTS" not in latest_sql
    assert "IS NULL OR" not in latest_sql


def test_month_partition_batch_context_uses_requested_keys_as_the_outer_query(tmp_path: Path) -> None:
    path = tmp_path / "partitions/2026/09.sqlite3"
    repository = HistoryMonthPrices(path, 2026, 9)
    repository.initialize()
    revision = _revision(date(2026, 9, 10), 1, 10.0)
    repository.save_revisions((revision,))

    with closing(OpenSqliteConnection(path)) as connection, connection:
        connection.execute(
            "CREATE TEMP TABLE requested_history_revisions ("
            "trade_date TEXT NOT NULL, code TEXT NOT NULL, sync_sequence INTEGER NOT NULL, "
            "revision_id TEXT NOT NULL, PRIMARY KEY (trade_date, code, sync_sequence, revision_id)"
            ") WITHOUT ROWID"
        )
        connection.execute(
            "INSERT INTO requested_history_revisions VALUES (?, ?, ?, ?)",
            (revision.trade_date.isoformat(), revision.code, revision.first_seen_sequence, revision.revision_id),
        )
        plans = {
            "observations": tuple(
                str(row[3])
                for row in connection.execute("EXPLAIN QUERY PLAN " + partition_module._EXISTING_OBSERVATIONS_SQL)
            ),
            "records": tuple(
                str(row[3])
                for row in connection.execute("EXPLAIN QUERY PLAN " + partition_module._EXISTING_REVISIONS_SQL)
            ),
            "latest": tuple(
                str(row[3])
                for row in connection.execute("EXPLAIN QUERY PLAN " + partition_module._LATEST_SEQUENCES_SQL)
            ),
        }

    assert not any("SCAN observations" in detail for detail in (*plans["observations"], *plans["latest"]))
    assert not any("SCAN records" in detail for detail in plans["records"])
    assert any("SEARCH observations USING PRIMARY KEY" in detail for detail in plans["observations"])
    assert any("SEARCH records USING PRIMARY KEY" in detail for detail in plans["records"])
    assert any("SEARCH observations USING" in detail for detail in plans["latest"])


def test_month_partition_bounds_write_cache_without_evicting_hot_index_pages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "partitions/2026/09.sqlite3"
    statements: list[str] = []
    repository = HistoryMonthPrices(path, 2026, 9, statement_trace=statements.append)
    repository.initialize()
    statements.clear()
    monkeypatch.setattr(
        partition_module,
        "_release_file_cache",
        lambda _path: pytest.fail("committed hot index pages were evicted"),
    )

    repository.save_revisions((_revision(date(2026, 9, 10), 1, 10.0),))

    assert "PRAGMA cache_size=-8192" in statements
    assert "PRAGMA mmap_size=0" in statements


def test_month_partition_seal_scans_rows_without_a_temporary_sort(tmp_path: Path) -> None:
    path = tmp_path / "partitions/2026/09.sqlite3"
    repository = HistoryMonthPrices(path, 2026, 9)
    repository.initialize()
    repository.save_revisions(tuple(_revision(date(2026, 9, day), 1, 10.0 + day) for day in range(1, 16)))

    with closing(OpenSqliteConnection(path)) as connection, connection:
        plan = tuple(
            str(row[3]) for row in connection.execute("EXPLAIN QUERY PLAN " + partition_module._ALL_REVISION_SCAN_SQL)
        )

    assert not any("TEMP B-TREE" in detail for detail in plan)


def test_month_partition_seal_checkpoints_before_bounded_mmap_verification(tmp_path: Path) -> None:
    path = tmp_path / "partitions/2026/09.sqlite3"
    statements: list[str] = []
    repository = HistoryMonthPrices(path, 2026, 9, statement_trace=statements.append)
    repository.initialize()
    repository.save_revisions((_revision(date(2026, 9, 10), 1, 10.0),))
    statements.clear()

    repository.seal()

    assert "PRAGMA wal_checkpoint(TRUNCATE)" not in statements
    assert "PRAGMA quick_check" in statements
    assert "PRAGMA cache_size=-8192" in statements


@pytest.mark.parametrize("sequence", (1, 2, 3, 4))
def test_code_ordered_stream_preserves_point_in_time_revisions_without_sorting(
    tmp_path: Path,
    sequence: int,
) -> None:
    path = tmp_path / "09.sqlite3"
    statements: list[str] = []
    repository = HistoryMonthPrices(path, 2026, 9, statement_trace=statements.append)
    repository.initialize()
    original = _revision(date(2026, 9, 10), 1, 10.0)
    other = replace(
        original,
        cell=replace(
            original.cell,
            code="600002",
            unadjusted=replace(original.cell.unadjusted, code="600002"),
            qfq=replace(original.cell.qfq, code="600002"),
        ),
    )
    repository.save_revisions((original, other, _revision(date(2026, 9, 11), 1, 12.0)))
    repository.save_revisions((_revision(original.trade_date, 2, 11.0),))
    repository.save_revisions((_revision(original.trade_date, 3, 10.0),))
    repository.save_revisions((_revision(date(2026, 9, 9), 4, 9.0),))
    start, end = date(2026, 9, 10), date(2026, 9, 11)
    expected = sorted(
        repository.iter_range(start, end, snapshot_sequence=sequence), key=lambda row: (row.code, row.trade_date)
    )
    statements.clear()

    assert tuple(repository.iter_range_by_code(start, end, snapshot_sequence=sequence)) == tuple(expected)

    query = next(value for value in statements if value.lstrip().startswith("SELECT records.trade_date"))
    with closing(OpenSqliteConnection(path)) as connection, connection:
        plan = tuple(str(row[3]) for row in connection.execute("EXPLAIN QUERY PLAN " + query))
    assert not any("TEMP B-TREE" in detail for detail in plan)


def test_training_stream_projects_latest_points_without_revision_hashes_or_temporary_sort(tmp_path: Path) -> None:
    path = tmp_path / "09.sqlite3"
    statements: list[str] = []
    repository = HistoryMonthPrices(path, 2026, 9, statement_trace=statements.append)
    repository.initialize()
    day = date(2026, 9, 10)
    repository.save_revisions((_revision(day, 1, 10.0), _revision(day, 2, 11.0)))
    statements.clear()

    original = tuple(repository.iter_training_rows(day, day, snapshot_sequence=1))
    revised = tuple(repository.iter_training_rows(day, day, snapshot_sequence=2))

    assert original[0].point is not None
    assert revised[0].point is not None
    assert original[0].point.qfq_close_price == 9.0
    assert revised[0].point.qfq_close_price == 10.0
    query = next(value for value in statements if "$.cell.qfq.close_price" in value)
    assert "content_hash" not in query
    with closing(OpenSqliteConnection(path)) as connection, connection:
        plan = tuple(str(row[3]) for row in connection.execute("EXPLAIN QUERY PLAN " + query))
    assert not any("TEMP B-TREE" in detail for detail in plan)


def test_month_partition_batch_preserves_a_to_b_to_a_snapshot_replay(tmp_path: Path) -> None:
    repository = HistoryMonthPrices(tmp_path / "09.sqlite3", 2026, 9)
    repository.initialize()
    day = date(2026, 9, 10)
    original = _revision(day, 1, 10.0)
    revised = _revision(day, 2, 11.0)
    reverted = _revision(day, 3, 10.0)

    repository.save_revisions((original, revised, reverted))

    assert repository.read_day(day, snapshot_sequence=1) == (original,)
    assert repository.read_day(day, snapshot_sequence=2) == (revised,)
    assert repository.read_day(day, snapshot_sequence=3) == (original,)


def test_existing_month_partition_keeps_its_physical_page_size(tmp_path: Path) -> None:
    path = tmp_path / "partitions/2026/09.sqlite3"
    path.parent.mkdir(parents=True)
    with closing(OpenSqliteConnection(path)) as connection, connection:
        connection.execute("PRAGMA page_size=4096")
        connection.execute("CREATE TABLE legacy(value INTEGER)")

    HistoryMonthPrices(path, 2026, 9).initialize()

    with closing(OpenSqliteConnection(path)) as connection, connection:
        assert connection.execute("PRAGMA page_size").fetchone() == (4096,)


def test_month_partition_rejects_backdating_conflicts_and_wrong_month(tmp_path: Path) -> None:
    repository = HistoryMonthPrices(tmp_path / "09.sqlite3", 2026, 9)
    repository.initialize()
    original = _revision(date(2026, 9, 10), 2, 10.0)
    repository.save_revisions((original,))

    with pytest.raises(HistoryMonthPartitionConflictError, match="backdate"):
        repository.save_revisions((replace(original, first_seen_sequence=1),))
    with pytest.raises(HistoryMonthPartitionConflictError, match="sequence"):
        repository.save_revisions((_revision(date(2026, 9, 10), 2, 11.0),))
    with pytest.raises(HistoryMonthPartitionConflictError, match="backdate"):
        repository.save_revisions(
            (
                _revision(date(2026, 9, 9), 1, 9.0),
                _revision(date(2026, 9, 10), 1, 11.0),
            )
        )
    assert repository.read_day(date(2026, 9, 9), snapshot_sequence=1) == ()
    with pytest.raises(ValueError, match="month"):
        repository.save_revisions((_revision(date(2026, 10, 1), 3, 12.0),))

    empty = HistoryMonthPrices(tmp_path / "ordered.sqlite3", 2026, 9)
    empty.initialize()
    later = _revision(date(2026, 9, 11), 1, 12.0)
    with pytest.raises(ValueError, match="deterministic order"):
        empty.save_revisions((later, _revision(date(2026, 9, 10), 1, 10.0)))
    assert empty.read_day(date(2026, 9, 11), snapshot_sequence=1) == ()


def test_sealed_month_partition_has_stable_hash_and_fails_closed_on_tamper(tmp_path: Path) -> None:
    path = tmp_path / "partitions/2026/09.sqlite3"
    repository = HistoryMonthPrices(path, 2026, 9)
    repository.initialize()
    repository.save_revisions((_revision(date(2026, 9, 10), 1, 10.0),))

    reference = repository.seal()

    assert reference.relative_path == "partitions/2026/09.sqlite3"
    assert reference.row_count == 1
    sealed_path = tmp_path / reference.relative_path
    HistoryMonthPrices.verify(sealed_path, reference)
    with closing(OpenSqliteConnection(sealed_path)) as connection, connection:
        connection.execute("UPDATE daily_records SET content_hash=?", ("0" * 64,))
    with pytest.raises(HistoryMonthPartitionError, match="logical"):
        HistoryMonthPrices.verify(sealed_path, reference)


def test_month_partition_seal_does_not_repeat_full_verification_after_atomic_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "partitions/2026/09.sqlite3"
    repository = HistoryMonthPrices(path, 2026, 9)
    repository.initialize()
    repository.save_revisions((_revision(date(2026, 9, 10), 1, 10.0),))

    monkeypatch.setattr(
        HistoryMonthPrices,
        "verify",
        classmethod(lambda _cls, _path, _reference, _progress=None: pytest.fail("duplicate verification")),
    )

    reference = repository.seal()

    assert reference.row_count == 1


def test_partition_verification_bounds_sqlite_and_file_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "partitions/2026/09.sqlite3"
    repository = HistoryMonthPrices(path, 2026, 9)
    repository.initialize()
    repository.save_revisions((_revision(date(2026, 9, 10), 1, 10.0),))
    reference = repository.seal()
    calls: list[int] = []
    monkeypatch.setattr(
        partition_module.os,
        "posix_fadvise",
        lambda _descriptor, _offset, _length, advice: calls.append(advice),
    )

    HistoryMonthPrices.verify(tmp_path / reference.relative_path, reference)

    assert not calls  # Verification reads logical SQLite rows, not the entire file.
    with repository._read_connection() as connection:
        assert connection.execute("PRAGMA cache_size").fetchone() == (-8192,)
        assert connection.execute("PRAGMA mmap_size").fetchone() == (0,)
        assert connection.execute("PRAGMA temp_store").fetchone() == (1,)
    with repository._verification_connection() as connection:
        assert connection.execute("PRAGMA query_only").fetchone() == (1,)
        assert connection.execute("PRAGMA cache_size").fetchone() == (-8192,)
        assert connection.execute("PRAGMA mmap_size").fetchone() == (partition_module._VERIFICATION_MMAP_BYTES,)
        assert connection.execute("PRAGMA temp_store").fetchone() == (1,)


def test_month_partition_rejects_invalid_payload_and_changed_observation(tmp_path: Path) -> None:
    path = tmp_path / "partitions/2026/09.sqlite3"
    repository = HistoryMonthPrices(path, 2026, 9)
    repository.initialize()
    repository.save_revisions((_revision(date(2026, 9, 10), 1, 10.0),))
    reference = repository.seal()
    with closing(OpenSqliteConnection(path)) as connection, connection:
        connection.execute("UPDATE daily_records SET payload_json='{}'")
    with pytest.raises(HistoryMonthPartitionError):
        HistoryMonthPrices.verify(path, reference)
