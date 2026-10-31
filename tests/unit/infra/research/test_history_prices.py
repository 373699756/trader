from __future__ import annotations

from tests.helpers.configuration import SqlitePolicyFixture

import math
from contextlib import closing
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from trader.download.domain.baostock_daily import BaoStockDailyCell, BaoStockDailySide
from trader.download.domain.history_revision import HistoryRevision
from trader.download.domain.published_history import ProjectHistoryCell
from trader.download.infra.price_columns import PriceCellFromColumns, PriceColumnValues
from trader.download.infra import history_prices as partition_module
from trader.download.infra.history_prices import (
    HistoryPricePartition,
    HistoryPricePartitionConflictError,
    HistoryPricePartitionError,
)
from trader.infra.sqlite_codec import EncodeDate
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction


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


def test_real_price_columns_preserve_mixed_numeric_types_and_revision_hash(tmp_path: Path) -> None:
    path = tmp_path / "prices_2026.sqlite3"
    month = HistoryPricePartition(path, 2026, 9, sqlite_policy=SqlitePolicyFixture())
    month.initialize()
    day = date(2026, 9, 10)
    revision = _revision(day, 1, 10.0)
    raw = replace(revision.cell.unadjusted, open_price=10, volume=100, amount=1000.0, pct_change=-0.0)
    mixed = replace(revision, cell=replace(revision.cell, unadjusted=raw))
    month.save_revisions((mixed,))
    decoded = month.read_day(day, snapshot_sequence=1)[0]
    assert type(decoded.cell.unadjusted.open_price) is int
    assert type(decoded.cell.unadjusted.amount) is float
    assert math.copysign(1, decoded.cell.unadjusted.pct_change) == -1
    assert decoded.content_hash == mixed.content_hash
    published = tuple(month.iter_published_cells(day, day, snapshot_sequence=1))[0]
    assert published == ProjectHistoryCell(mixed.cell)
    assert type(published.unadjusted.open_price) is int
    assert type(published.unadjusted.amount) is float
    assert math.copysign(1, published.unadjusted.pct_change) == -1
    with SqliteSession(path) as connection:
        assert tuple(
            connection.run(
                "SELECT typeof(raw_open),typeof(raw_volume),typeof(content_hash) FROM daily_price_revision"
            ).require_one()
        ) == ("real", "real", "blob")


@pytest.mark.parametrize("position", (2, 6, 8, 9, 10, 13, 17))
@pytest.mark.parametrize("invalid", (float("inf"), float("-inf"), float("nan"), True, "10"))
def test_price_read_boundary_rejects_invalid_numbers(position, invalid):
    day = date(2026, 9, 10)
    values = list(PriceColumnValues(ProjectHistoryCell(_revision(day, 1, 10.0).cell)))
    values[position] = invalid
    with pytest.raises(ValueError):
        PriceCellFromColumns("600001", EncodeDate(day), tuple(values))


def test_month_schema_initialization_commits_all_ddl_once(tmp_path: Path) -> None:
    statements: list[str] = []
    month = HistoryPricePartition(tmp_path / "prices_2026.sqlite3", 2026, 9, statement_trace=statements.append, sqlite_policy=SqlitePolicyFixture())
    month.initialize()
    assert sum(sql.strip().upper().startswith("BEGIN IMMEDIATE") for sql in statements) == 1
    assert sum(sql.strip().upper() == "COMMIT" for sql in statements) == 1


@pytest.mark.parametrize("consumer", ("published", "training"))
def test_published_month_query_seeks_dates_in_annual_file_and_preserves_revision(tmp_path: Path, consumer: str) -> None:
    path = tmp_path / "prices_2026.sqlite3"
    index = HistoryPricePartition(path, 2026, 9, sqlite_policy=SqlitePolicyFixture())
    index.initialize()
    day = date(2026, 9, 10)
    index.save_revisions((_revision(date(2026, 1, 10), 1, 8.0), _revision(day, 1, 10.0)))
    index.save_revisions((_revision(day, 2, 12.0),))
    query = (
        partition_module._PUBLISHED_RANGE_BY_CODE_SQL
        if consumer == "published"
        else partition_module._TRAINING_RANGE_SQL
    )
    with closing(index._read_connection()) as connection:
        plan = tuple(
            str(row[3])
            for row in connection.run(
                "EXPLAIN QUERY PLAN " + query,
                ("2026-09-01", "2026-09-30", 1),
            )
        )
    assert any("SEARCH records" in item and "trade_date>? AND trade_date<?" in item for item in plan)
    assert not any("SCAN records" in item for item in plan)
    assert any("SEARCH observed USING PRIMARY KEY (trade_date=? AND code=?" in item for item in plan)
    read = index.iter_published_cells if consumer == "published" else index.iter_training_rows
    before = tuple(read(day, day, snapshot_sequence=1))
    after = tuple(read(day, day, snapshot_sequence=2))
    assert len(before) == len(after) == 1
    if consumer == "published":
        assert before[0].qfq.close_price == 9.0 and after[0].qfq.close_price == 11.0
    else:
        assert before[0].point.qfq_close_price == 9.0 and after[0].point.qfq_close_price == 11.0


@pytest.mark.parametrize("operation", ("proof", "cells"))
def test_published_history_read_checks_budget_inside_sqlite(tmp_path: Path, operation: str) -> None:
    path = tmp_path / "prices_2026.sqlite3"
    index = HistoryPricePartition(path, 2026, 9, sqlite_policy=SqlitePolicyFixture())
    index.initialize()
    revisions = []
    for number in range(500):
        original = _revision(date(2026, 9, 10), 1, 10.0)
        code = f"{600000 + number:06d}"
        revisions.append(
            replace(
                original,
                cell=replace(
                    original.cell,
                    code=code,
                    unadjusted=replace(original.cell.unadjusted, code=code),
                    qfq=replace(original.cell.qfq, code=code),
                ),
            )
        )
    index.save_revisions(revisions)
    reference = index.seal(1)
    calls = 0

    def deadline():
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise RuntimeError("history_preload_deadline")

    with pytest.raises(RuntimeError, match="^history_preload_deadline$"):
        if operation == "proof":
            HistoryPricePartition.verify_logical_content(path, reference, check=deadline, sqlite_policy=SqlitePolicyFixture())
        else:
            tuple(index.iter_published_cells(date(2026, 9, 1), date(2026, 9, 30), snapshot_sequence=1, check=deadline))
    HistoryPricePartition.verify_logical_content(path, reference, sqlite_policy=SqlitePolicyFixture())
    assert len(tuple(index.iter_published_cells(date(2026, 9, 1), date(2026, 9, 30), snapshot_sequence=1))) == 500


def test_resume_read_skips_absent_stocks_and_preserves_partial_and_sequence_views(tmp_path: Path) -> None:
    statements: list[str] = []
    month = HistoryPricePartition(tmp_path / "prices_2026.sqlite3", 2026, 9, statement_trace=statements.append, sqlite_policy=SqlitePolicyFixture())
    month.initialize()
    first = _revision(date(2026, 9, 10), 1, 10.0)
    later = _revision(date(2026, 9, 11), 2, 11.0)
    month.save_revisions((first, later))
    absent = (("600002", first.trade_date), ("600002", later.trade_date))
    statements.clear()
    assert month.read_price_keys(absent, 2) == ()
    assert not any("WITH requested(code,day)" in sql for sql in statements)
    mixed = ((first.code, first.trade_date), *absent, (later.code, later.trade_date))
    assert month.read_price_keys(mixed, 1) == (first,)
    assert month.read_price_keys(mixed, 2) == (first, later)


def test_first_build_insert_rejects_duplicate_without_changing_durable_rows(tmp_path: Path) -> None:
    path = tmp_path / "prices_2026.sqlite3"
    month = HistoryPricePartition(path, 2026, 9, sqlite_policy=SqlitePolicyFixture())
    month.initialize()
    revision = _revision(date(2026, 9, 10), 1, 10.0)
    session = month.open_write_session(fresh=True)
    try:
        session.save_revision_batches((revision,))
        with pytest.raises(HistoryPricePartitionError, match="write failed"):
            session.save_revision_batches((revision,))
    finally:
        session.close()
    assert month.read_day(revision.trade_date, snapshot_sequence=1) == (revision,)
    assert month.seal(1).row_count == 1


def test_resumed_writer_inserts_absent_stocks_without_per_date_probes(tmp_path: Path) -> None:
    statements: list[str] = []
    month = HistoryPricePartition(tmp_path / "prices_2026.sqlite3", 2026, 9, statement_trace=statements.append, sqlite_policy=SqlitePolicyFixture())
    month.initialize()
    original = _revision(date(2026, 9, 10), 1, 10.0)
    month.save_revisions((original,))
    assert original.cell.unadjusted is not None and original.cell.qfq is not None
    incoming = replace(
        original,
        cell=replace(
            original.cell,
            code="600002",
            unadjusted=replace(original.cell.unadjusted, code="600002"),
            qfq=replace(original.cell.qfq, code="600002"),
        ),
    )
    statements.clear()
    with closing(month.open_write_session(pending_sequence=1)) as session:
        session.save_revision_batches((incoming,))
    assert not any("requested_history_revisions" in sql for sql in statements)
    assert month.read_day(original.trade_date, snapshot_sequence=1) == (original, incoming)
    assert month.seal(1).row_count == 2
    # Replaying a stock already on disk must retain conflict/idempotency validation.
    statements.clear()
    with closing(month.open_write_session(pending_sequence=1)) as session:
        session.save_revision_batches((incoming, incoming))
    assert any("CREATE TEMP TABLE requested_history_revisions" in sql for sql in statements)
    assert month.read_day(original.trade_date, snapshot_sequence=1) == (original, incoming)


def test_resumed_writer_does_not_assume_orphaned_records_are_absent(tmp_path: Path) -> None:
    month = HistoryPricePartition(tmp_path / "prices_2026.sqlite3", 2026, 9, sqlite_policy=SqlitePolicyFixture())
    month.initialize()
    original = _revision(date(2026, 9, 10), 1, 10.0)
    month.save_revisions((original,))
    with SqliteSession(month._path) as connection, SqliteTransaction(connection):
        connection.run("DELETE FROM daily_price_observation")
    with closing(month.open_write_session(pending_sequence=1)) as session:
        session.save_revision_batches((original,))
    assert month.read_day(original.trade_date, snapshot_sequence=1) == (original,)


def test_resumed_writer_reuses_new_revision_content_across_observation_sequences(tmp_path: Path) -> None:
    month = HistoryPricePartition(tmp_path / "prices_2026.sqlite3", 2026, 9, sqlite_policy=SqlitePolicyFixture())
    month.initialize()
    original = _revision(date(2026, 9, 10), 1, 10.0)
    with closing(month.open_write_session(pending_sequence=2)) as session:
        session.save_revision_batches((original, replace(original, first_seen_sequence=2)))
    assert month.read_day(original.trade_date, snapshot_sequence=1) == (original,)
    assert month.read_day(original.trade_date, snapshot_sequence=2) == (original,)
    assert month.seal(2).row_count == 1


def test_only_unpublished_unknown_observation_can_gain_a_price_in_same_sequence(tmp_path: Path) -> None:
    month = HistoryPricePartition(tmp_path / "prices_2026.sqlite3", 2026, 9, sqlite_policy=SqlitePolicyFixture())
    month.initialize()
    day = date(2026, 9, 10)
    missing = HistoryRevision(
        1, "main", BaoStockDailyCell("600001", day, "unknown_missing", None, None), None, None, None
    )
    complete = _revision(day, 1, 10.0)
    with closing(month.open_write_session(fresh=True)) as session:
        session.save_revision_batches((missing,))
    with closing(month.open_write_session(pending_sequence=2)) as session:
        with pytest.raises(HistoryPricePartitionConflictError, match="sequence"):
            session.save_revision_batches((complete,))
    with closing(month.open_write_session(pending_sequence=1)) as session:
        session.save_revision_batches((complete,))
    assert month.read_day(day, snapshot_sequence=1) == (complete,)
    assert month.seal(1).row_count == 1
    with closing(month.open_write_session(pending_sequence=1)) as session:
        with pytest.raises(HistoryPricePartitionConflictError, match="sequence"):
            session.save_revision_batches((_revision(day, 1, 11.0),))


@pytest.mark.parametrize("status", ("qfq_missing", "unadjusted_missing", "tencent_inferred_suspended"))
def test_pending_incomplete_observation_can_gain_a_complete_pair(tmp_path: Path, status: str) -> None:
    month = HistoryPricePartition(tmp_path / "prices_2026.sqlite3", 2026, 9, sqlite_policy=SqlitePolicyFixture())
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
    month = HistoryPricePartition(tmp_path / "prices_2026.sqlite3", 2026, 9, sqlite_policy=SqlitePolicyFixture())
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
        with pytest.raises(HistoryPricePartitionConflictError, match="retained price"):
            session.save_revision_batches((candidate,))
    assert month.read_day(day, snapshot_sequence=1) == (initial,)
    with closing(month.open_write_session(pending_sequence=1)) as session:
        session.save_revision_batches((complete,))
    assert month.read_day(day, snapshot_sequence=1) == (complete,)


def test_month_partition_schema_revision_replay_and_indexes(tmp_path: Path) -> None:
    path = tmp_path / "prices_2026.sqlite3"
    index = HistoryPricePartition(path, 2026, 9, sqlite_policy=SqlitePolicyFixture())
    index.initialize()
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        assert tuple(connection.run("PRAGMA page_size").fetchone()) == (8192,)
    original = _revision(date(2026, 9, 10), 1, 10.0)
    revised = _revision(date(2026, 9, 10), 3, 11.0)

    index.save_revisions((original, original))
    index.save_revisions((replace(original, first_seen_sequence=2),))
    index.save_revisions((revised,))

    assert index.read_day(date(2026, 9, 10), snapshot_sequence=1) == (original,)
    assert index.read_day(date(2026, 9, 10), snapshot_sequence=2) == (original,)
    assert index.read_day(date(2026, 9, 10), snapshot_sequence=3) == (revised,)
    assert index.read_day(date(2026, 9, 10), snapshot_sequence=3, board="star") == ()
    assert index.read_code("600001", date(2026, 9, 1), date(2026, 9, 30), snapshot_sequence=3) == (revised,)
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        indexes = {
            name: tuple(row[2] for row in connection.run(f"PRAGMA index_info('{name}')"))
            for (name,) in connection.run("SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")
        }
    assert indexes["history_price_code_date_idx"][:2] == ("code", "trade_date")
    assert indexes["history_price_date_board_code_idx"][:3] == ("trade_date", "board", "code")
    assert indexes["history_price_observation_code_date_idx"][:3] == (
        "code",
        "trade_date",
        "sync_sequence",
    )

    reverted = _revision(date(2026, 9, 10), 4, 10.0)
    index.save_revisions((reverted,))
    assert index.read_day(date(2026, 9, 10), snapshot_sequence=4) == (original,)


def test_month_partition_batches_revision_context_and_uses_specialized_latest_sql(tmp_path: Path) -> None:
    path = tmp_path / "prices_2026.sqlite3"
    statements: list[str] = []
    index = HistoryPricePartition(path, 2026, 9, statement_trace=statements.append, sqlite_policy=SqlitePolicyFixture())
    index.initialize()
    statements.clear()
    revisions = tuple(_revision(date(2026, 9, day), 1, 10.0 + day) for day in range(1, 16))

    index.save_revisions(revisions)

    selects = tuple(statement for statement in statements if statement.lstrip().upper().startswith("SELECT"))
    assert len(selects) == 4  # metadata plus three set-based conflict/context reads
    assert sum(statement.lstrip().upper().startswith("BEGIN IMMEDIATE") for statement in statements) == 1
    statements.clear()

    assert len(tuple(index.iter_range(date(2026, 9, 1), date(2026, 9, 30), snapshot_sequence=1))) == 15
    latest_sql = next(statement for statement in statements if statement.lstrip().startswith("WITH latest AS"))
    assert "ROW_NUMBER" in latest_sql
    assert "NOT EXISTS" not in latest_sql
    assert "IS NULL OR" not in latest_sql


def test_month_partition_batch_context_uses_requested_keys_as_the_outer_query(tmp_path: Path) -> None:
    path = tmp_path / "prices_2026.sqlite3"
    index = HistoryPricePartition(path, 2026, 9, sqlite_policy=SqlitePolicyFixture())
    index.initialize()
    revision = _revision(date(2026, 9, 10), 1, 10.0)
    index.save_revisions((revision,))

    with SqliteSession(path) as connection, SqliteTransaction(connection):
        connection.run(
            "CREATE TEMP TABLE requested_history_revisions ("
            "trade_date INTEGER NOT NULL, code TEXT NOT NULL, sync_sequence INTEGER NOT NULL, "
            "revision_id TEXT NOT NULL, PRIMARY KEY (trade_date, code, sync_sequence, revision_id)"
            ") WITHOUT ROWID"
        )
        connection.run(
            "INSERT INTO requested_history_revisions VALUES (?, ?, ?, ?)",
            (EncodeDate(revision.trade_date), revision.code, revision.first_seen_sequence, revision.revision_id),
        )
        plans = {
            "observations": tuple(
                str(row[3])
                for row in connection.run("EXPLAIN QUERY PLAN " + partition_module._EXISTING_OBSERVATIONS_SQL)
            ),
            "records": tuple(
                str(row[3]) for row in connection.run("EXPLAIN QUERY PLAN " + partition_module._EXISTING_REVISIONS_SQL)
            ),
            "latest": tuple(
                str(row[3]) for row in connection.run("EXPLAIN QUERY PLAN " + partition_module._LATEST_SEQUENCES_SQL)
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
    path = tmp_path / "prices_2026.sqlite3"
    statements: list[str] = []
    index = HistoryPricePartition(path, 2026, 9, statement_trace=statements.append, sqlite_policy=SqlitePolicyFixture())
    index.initialize()
    statements.clear()
    monkeypatch.setattr(
        partition_module,
        "_release_file_cache",
        lambda _path: pytest.fail("committed hot index pages were evicted"),
    )

    index.save_revisions((_revision(date(2026, 9, 10), 1, 10.0),))

    assert "PRAGMA cache_size=-8192" in statements
    assert "PRAGMA mmap_size=0" in statements


def test_month_partition_seal_scans_rows_without_a_temporary_sort(tmp_path: Path) -> None:
    path = tmp_path / "prices_2026.sqlite3"
    index = HistoryPricePartition(path, 2026, 9, sqlite_policy=SqlitePolicyFixture())
    index.initialize()
    index.save_revisions(tuple(_revision(date(2026, 9, day), 1, 10.0 + day) for day in range(1, 16)))

    with SqliteSession(path) as connection, SqliteTransaction(connection):
        plan = tuple(
            str(row[3]) for row in connection.run("EXPLAIN QUERY PLAN " + partition_module._ALL_REVISION_SCAN_SQL)
        )

    assert not any("TEMP B-TREE" in detail for detail in plan)


def test_month_partition_seal_checkpoints_before_bounded_mmap_verification(tmp_path: Path) -> None:
    path = tmp_path / "prices_2026.sqlite3"
    statements: list[str] = []
    index = HistoryPricePartition(path, 2026, 9, statement_trace=statements.append, sqlite_policy=SqlitePolicyFixture())
    index.initialize()
    index.save_revisions((_revision(date(2026, 9, 10), 1, 10.0),))
    statements.clear()

    index.seal()

    assert "PRAGMA wal_checkpoint(TRUNCATE)" not in statements
    assert "PRAGMA quick_check" not in statements  # Incremental month proof avoids an annual full scan.
    assert "PRAGMA cache_size=-8192" in statements


@pytest.mark.parametrize("sequence", (1, 2, 3, 4))
def test_code_ordered_stream_preserves_point_in_time_revisions_without_sorting(
    tmp_path: Path,
    sequence: int,
) -> None:
    path = tmp_path / "09.sqlite3"
    statements: list[str] = []
    index = HistoryPricePartition(path, 2026, 9, statement_trace=statements.append, sqlite_policy=SqlitePolicyFixture())
    index.initialize()
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
    index.save_revisions((original, other, _revision(date(2026, 9, 11), 1, 12.0)))
    index.save_revisions((_revision(original.trade_date, 2, 11.0),))
    index.save_revisions((_revision(original.trade_date, 3, 10.0),))
    index.save_revisions((_revision(date(2026, 9, 9), 4, 9.0),))
    start, end = date(2026, 9, 10), date(2026, 9, 11)
    expected = sorted(
        index.iter_range(start, end, snapshot_sequence=sequence), key=lambda row: (row.code, row.trade_date)
    )
    statements.clear()

    assert tuple(index.iter_range_by_code(start, end, snapshot_sequence=sequence)) == tuple(expected)

    query = next(value for value in statements if value.lstrip().startswith("SELECT records.trade_date"))
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        plan = tuple(str(row[3]) for row in connection.run("EXPLAIN QUERY PLAN " + query))
    assert not any("TEMP B-TREE" in detail for detail in plan)


def test_training_stream_projects_latest_points_without_revision_hashes_or_temporary_sort(tmp_path: Path) -> None:
    path = tmp_path / "09.sqlite3"
    statements: list[str] = []
    index = HistoryPricePartition(path, 2026, 9, statement_trace=statements.append, sqlite_policy=SqlitePolicyFixture())
    index.initialize()
    day = date(2026, 9, 10)
    index.save_revisions((_revision(day, 1, 10.0), _revision(day, 2, 11.0)))
    statements.clear()

    original = tuple(index.iter_training_rows(day, day, snapshot_sequence=1))
    revised = tuple(index.iter_training_rows(day, day, snapshot_sequence=2))

    assert original[0].point is not None
    assert revised[0].point is not None
    assert original[0].point.qfq_close_price == 9.0
    assert revised[0].point.qfq_close_price == 10.0
    query = next(value for value in statements if "qfq_close" in value)
    assert "content_hash" not in query
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        plan = tuple(str(row[3]) for row in connection.run("EXPLAIN QUERY PLAN " + query))
    assert not any("TEMP B-TREE" in detail for detail in plan)


def test_month_partition_batch_preserves_a_to_b_to_a_snapshot_replay(tmp_path: Path) -> None:
    index = HistoryPricePartition(tmp_path / "09.sqlite3", 2026, 9, sqlite_policy=SqlitePolicyFixture())
    index.initialize()
    day = date(2026, 9, 10)
    original = _revision(day, 1, 10.0)
    revised = _revision(day, 2, 11.0)
    reverted = _revision(day, 3, 10.0)

    index.save_revisions((original, revised, reverted))

    assert index.read_day(day, snapshot_sequence=1) == (original,)
    assert index.read_day(day, snapshot_sequence=2) == (revised,)
    assert index.read_day(day, snapshot_sequence=3) == (original,)


def test_existing_month_partition_keeps_its_physical_page_size(tmp_path: Path) -> None:
    path = tmp_path / "prices_2026.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    HistoryPricePartition(path, 2026, 9, sqlite_policy=SqlitePolicyFixture()).initialize()
    with SqliteSession(path) as connection:
        connection.run("PRAGMA journal_mode=DELETE")
        connection.run("PRAGMA page_size=4096")
        connection.run("VACUUM")

    HistoryPricePartition(path, 2026, 9, sqlite_policy=SqlitePolicyFixture()).initialize()

    with SqliteSession(path) as connection, SqliteTransaction(connection):
        assert tuple(connection.run("PRAGMA page_size").fetchone()) == (4096,)


def test_month_partition_rejects_backdating_conflicts_and_wrong_month(tmp_path: Path) -> None:
    index = HistoryPricePartition(tmp_path / "09.sqlite3", 2026, 9, sqlite_policy=SqlitePolicyFixture())
    index.initialize()
    original = _revision(date(2026, 9, 10), 2, 10.0)
    index.save_revisions((original,))

    with pytest.raises(HistoryPricePartitionConflictError, match="backdate"):
        index.save_revisions((replace(original, first_seen_sequence=1),))
    with pytest.raises(HistoryPricePartitionConflictError, match="sequence"):
        index.save_revisions((_revision(date(2026, 9, 10), 2, 11.0),))
    with pytest.raises(HistoryPricePartitionConflictError, match="backdate"):
        index.save_revisions(
            (
                _revision(date(2026, 9, 9), 1, 9.0),
                _revision(date(2026, 9, 10), 1, 11.0),
            )
        )
    assert index.read_day(date(2026, 9, 9), snapshot_sequence=1) == ()
    with pytest.raises(ValueError, match="year"):
        index.save_revisions((_revision(date(2027, 1, 1), 3, 12.0),))

    empty = HistoryPricePartition(tmp_path / "ordered.sqlite3", 2026, 9, sqlite_policy=SqlitePolicyFixture())
    empty.initialize()
    later = _revision(date(2026, 9, 11), 1, 12.0)
    with pytest.raises(ValueError, match="deterministic order"):
        empty.save_revisions((later, _revision(date(2026, 9, 10), 1, 10.0)))
    assert empty.read_day(date(2026, 9, 11), snapshot_sequence=1) == ()


def test_sealed_month_partition_has_stable_hash_and_fails_closed_on_tamper(tmp_path: Path) -> None:
    path = tmp_path / "prices_2026.sqlite3"
    index = HistoryPricePartition(path, 2026, 9, sqlite_policy=SqlitePolicyFixture())
    index.initialize()
    index.save_revisions((_revision(date(2026, 9, 10), 1, 10.0),))

    reference = index.seal()

    assert reference.relative_path == "prices_2026.sqlite3"
    assert reference.row_count == 1
    sealed_path = tmp_path / reference.relative_path
    HistoryPricePartition.verify(sealed_path, reference, sqlite_policy=SqlitePolicyFixture())
    with SqliteSession(sealed_path) as connection, SqliteTransaction(connection):
        connection.run("UPDATE daily_price_revision SET content_hash=?", (bytes(32),))
    with pytest.raises(HistoryPricePartitionError, match="logical"):
        HistoryPricePartition.verify(sealed_path, reference, sqlite_policy=SqlitePolicyFixture())


def test_month_partition_seal_does_not_repeat_full_verification_after_atomic_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "prices_2026.sqlite3"
    index = HistoryPricePartition(path, 2026, 9, sqlite_policy=SqlitePolicyFixture())
    index.initialize()
    index.save_revisions((_revision(date(2026, 9, 10), 1, 10.0),))

    monkeypatch.setattr(
        HistoryPricePartition,
        "verify",
        classmethod(lambda _cls, _path, _reference, _progress=None: pytest.fail("duplicate verification")),
    )

    reference = index.seal()

    assert reference.row_count == 1


def test_partition_verification_bounds_sqlite_and_file_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "prices_2026.sqlite3"
    index = HistoryPricePartition(path, 2026, 9, sqlite_policy=SqlitePolicyFixture())
    index.initialize()
    index.save_revisions((_revision(date(2026, 9, 10), 1, 10.0),))
    reference = index.seal()
    calls: list[int] = []
    monkeypatch.setattr(
        partition_module.os,
        "posix_fadvise",
        lambda _descriptor, _offset, _length, advice: calls.append(advice),
    )

    HistoryPricePartition.verify(tmp_path / reference.relative_path, reference, sqlite_policy=SqlitePolicyFixture())

    assert not calls  # Verification reads logical SQLite rows, not the entire file.
    with closing(index._read_connection()) as connection:
        assert tuple(connection.run("PRAGMA cache_size").fetchone()) == (-8192,)
        assert tuple(connection.run("PRAGMA mmap_size").fetchone()) == (0,)
        assert tuple(connection.run("PRAGMA temp_store").fetchone()) == (1,)


def test_month_partition_rejects_invalid_payload_and_changed_observation(tmp_path: Path) -> None:
    path = tmp_path / "prices_2026.sqlite3"
    index = HistoryPricePartition(path, 2026, 9, sqlite_policy=SqlitePolicyFixture())
    index.initialize()
    index.save_revisions((_revision(date(2026, 9, 10), 1, 10.0),))
    reference = index.seal()
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        connection.run("UPDATE daily_price_revision SET qfq_close=999")
    with pytest.raises(HistoryPricePartitionError):
        HistoryPricePartition.verify(path, reference, sqlite_policy=SqlitePolicyFixture())
