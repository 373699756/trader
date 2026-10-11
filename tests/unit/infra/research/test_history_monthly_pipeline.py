import sqlite3
from dataclasses import replace
from datetime import date

import pytest

from tests.unit.infra.research.test_history_archive_reader import _build_snapshot, _revision
from tests.unit.infra.research.test_history_archive_sync import FakeSupplier
from trader.download.domain.baostock_daily import BaoStockDailyCell
from trader.download.domain.published_history import PublishedHistoryWindow, project_history_cell
from trader.download.infra.history_active_gap_scan import scan_active_price_gaps
from trader.download.infra.history_month_partition import SQLiteHistoryMonthPartitionRepository
from trader.download.infra.history_pending_prices import prepare_pending, write_qualified_month
from trader.download.infra.history_sqlite_activity import HistorySqliteActivity
from trader.download.infra.history_tencent_stage import HistoryTencentStage


def test_gap_index_reuses_identity_and_recovers_bad_digest(tmp_path, monkeypatch):
    day = date(2026, 9, 1)
    row = replace(_revision("600001", day), cell=BaoStockDailyCell("600001", day, "unknown_missing", None, None))
    snapshot = _build_snapshot(tmp_path, (row,))
    context = FakeSupplier((day,)).load_context(day, 1)
    calls = []
    query = SQLiteHistoryMonthPartitionRepository.missing_price_dates

    def tracked(self, sequence):
        calls.append(sequence)
        return query(self, sequence)

    monkeypatch.setattr(SQLiteHistoryMonthPartitionRepository, "missing_price_dates", tracked)

    def scan():
        return scan_active_price_gaps(tmp_path, snapshot, context, lambda: False, lambda *_args: None)

    assert scan() == {"600001": (day,)}
    assert scan() == {"600001": (day,)}
    assert len(calls) == 1
    with sqlite3.connect(tmp_path / "control.sqlite3") as connection:
        connection.execute("UPDATE active_price_gaps SET digest='bad'")
    assert scan() == {"600001": (day,)}
    assert len(calls) == 2
    path = tmp_path / snapshot.partitions[0].relative_path
    path.touch()
    assert scan() == {"600001": (day,)}
    assert len(calls) == 3
    path.touch()

    def unavailable(*_args):
        raise sqlite3.OperationalError("fixture_database_unavailable")

    monkeypatch.setattr(SQLiteHistoryMonthPartitionRepository, "missing_price_dates", unavailable)
    with pytest.raises(RuntimeError, match="history_gap_scan_failed"):
        scan()


def test_qualified_month_replay_preserves_pending_rows_and_uses_one_write_connection(tmp_path, monkeypatch):
    days = (date(2026, 8, 31), date(2026, 9, 1))
    stage = HistoryTencentStage(tmp_path / "stage.sqlite3", "fixture")
    stage.initialize()
    pending = prepare_pending(tmp_path, days, None, False)
    august = SQLiteHistoryMonthPartitionRepository(pending.paths[(2026, 8)], 2026, 8)
    legacy = replace(_revision("600001", days[0]), is_st=None, industry=None, industry_classification=None)
    august.save_revisions((legacy,))
    stage.mark_supplemented((("600001", 0),))
    rows = tuple(
        replace(_revision("600002", day), is_st=None, industry=None, industry_classification=None) for day in days
    )
    stage.save_qualified((("600002", rows, 0),))
    opened = []
    connect = SQLiteHistoryMonthPartitionRepository._write_connection

    def tracked(self):
        opened.append(self._path)
        return connect(self)

    monkeypatch.setattr(SQLiteHistoryMonthPartitionRepository, "_write_connection", tracked)
    for month in ((2026, 8), (2026, 9), (2026, 8), (2026, 9)):
        write_qualified_month(pending, stage, month)
    assert len(opened) == 2
    assert august.read_day(days[0], snapshot_sequence=1) == (legacy, rows[0])
    assert tuple(stage.iter_qualified_month(2026, 9)) == (rows[1],)


def test_long_sql_interrupt_is_cancellation_not_database_failure():
    checks = 0

    def cancelled():
        nonlocal checks
        checks += 1
        return checks > 2

    activity = HistorySqliteActivity(lambda: None, cancelled)
    with sqlite3.connect(":memory:") as connection, pytest.raises(KeyboardInterrupt):
        with activity.watch(connection):
            connection.execute(
                "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<1000000) SELECT sum(x) FROM n"
            ).fetchone()


def test_qualification_transfer_is_atomic_and_month_scan_uses_clustered_key(tmp_path):
    day = date(2026, 9, 1)
    row = replace(_revision("600001", day), is_st=None, industry=None, industry_classification=None)
    path = tmp_path / "stage.sqlite3"
    stage = HistoryTencentStage(path, "fixture")
    stage.initialize()
    stage.save_batch((PublishedHistoryWindow(row.code, (project_history_cell(row.cell),)),), ())
    with pytest.raises(sqlite3.IntegrityError):
        stage.save_qualified(((row.code, (row,), -1),))
    assert stage.read_supplement_context(row.code)[0].cells
    assert tuple(stage.iter_qualified_month(2026, 9)) == ()
    stage.save_qualified(((row.code, (row,), 0),))
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM candidates").fetchone() == (0,)
        sql = connection.execute("SELECT sql FROM sqlite_master WHERE name='qualified_prices'").fetchone()[0]
        assert "PRIMARY KEY (trade_date, code)" in sql and "WITHOUT ROWID" in sql
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT payload_json FROM qualified_prices WHERE trade_date>=? AND trade_date<? ORDER BY trade_date,code",
            ("2026-09-01", "2026-10-01"),
        ).fetchall()
        assert any("PRIMARY KEY" in str(item) for item in plan)
        assert not any("TEMP B-TREE" in str(item) for item in plan)
    assert tuple(stage.iter_qualified_month(2026, 9)) == (row,)


def test_month_writer_bounds_durable_batches_without_changing_revision_semantics(tmp_path):
    day = date(2026, 9, 1)
    row = replace(_revision("600001", day), is_st=None, industry=None, industry_classification=None)
    statements = []
    path = tmp_path / "partitions/2026/09.sqlite3"
    month = SQLiteHistoryMonthPartitionRepository(path, 2026, 9, statement_trace=statements.append)
    month.initialize()
    statements.clear()
    month.save_revision_batches(replace(row, first_seen_sequence=sequence) for sequence in range(1, 4098))
    assert sum(sql.strip() == "BEGIN IMMEDIATE" for sql in statements) == 2
    assert sum(sql.strip() == "COMMIT" for sql in statements) == 2
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM daily_records").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM daily_observations").fetchone() == (4097,)
    assert month.read_day(day, snapshot_sequence=4097) == (row,)
