"""History sealing candidates preserve month proofs and failed-publication boundaries."""

from __future__ import annotations

import math
import sqlite3
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.download.domain.baostock_daily import BaoStockDailyCell, BaoStockDailySide
from trader.download.domain.history_revision import HistoryRevision
from trader.download.infra.history_prices import HistoryPricePartition, HistoryPricePartitionError
from trader.download.infra.history_sqlite_activity import HistorySqliteActivity
from trader.infra.sqlite_codec import EncodeDate
from trader.infra.sqlite_connection import SqliteConnection, SqliteParameters, SqliteResult


def _revision(day: date, sequence: int, close: float, code: str = "600001") -> HistoryRevision:
    raw = BaoStockDailySide(
        code, day, "unadjusted", int(close), close + 1.0, close - 1.0, close,
        100, 1000.0, close - 0.1, -0.0, 0.02, "trading",
    )
    qfq = replace(raw, adjustment="qfq", preclose=None, pct_change=None, turnover=None)
    return HistoryRevision(sequence, "main", BaoStockDailyCell(code, day, "complete", raw, qfq), None, None, None)


def _prices(path: Path) -> HistoryPricePartition:
    writer = HistoryPricePartition(path, 2026, 1, sqlite_policy=SqlitePolicyFixture())
    writer.initialize()
    writer.save_revisions(tuple(_revision(date(2026, month, 10), 1, 10.0 + month) for month in (1, 2, 7, 12)))
    writer.save_revisions((
        _revision(date(2026, 1, 10), 2, 21.0),
        _revision(date(2026, 12, 10), 2, 22.0, "600002"),
    ))
    writer.save_revisions((
        _revision(date(2026, 1, 10), 3, 23.0, "600002"),
        _revision(date(2026, 12, 10), 3, 24.0),
    ))
    return writer


@pytest.mark.parametrize("sequence", [1, 2, 3, None])
@pytest.mark.parametrize("exact_ranges,early_fingerprint", [(False, False), (True, False), (False, True), (True, True)])
def test_sparse_month_candidates_keep_revision_visibility_numeric_types_and_identical_proofs(
    tmp_path: Path, sequence: int | None, exact_ranges: bool, early_fingerprint: bool
) -> None:
    path = tmp_path / "prices_2026.sqlite3"
    writer = _prices(path)
    baseline = writer.seal_months((1, 2, 12), sequence)
    previous_receipt = Path(f"{path}.sha256").read_bytes()
    previous_database = path.read_bytes()
    actual = writer.seal_months(
        (1, 2, 12), sequence, exact_month_ranges=exact_ranges, fingerprint_before_statistics=early_fingerprint
    )
    resolved_sequence = sequence or 3
    assert actual == baseline
    assert tuple(partition.row_count for partition in actual) == {
        1: (1, 1, 1), 2: (1, 1, 2), 3: (2, 1, 2)
    }[resolved_sequence]
    assert Path(f"{path}.sha256").read_bytes() == previous_receipt
    assert path.read_bytes() == previous_database
    assert all(partition.sequence == resolved_sequence for partition in actual)
    decoded = writer.read_day(date(2026, 1, 10), snapshot_sequence=resolved_sequence)[0]
    assert decoded.cell.unadjusted is not None
    assert type(decoded.cell.unadjusted.open_price) is int
    assert type(decoded.cell.unadjusted.close_price) is float
    assert type(decoded.cell.unadjusted.volume) is int
    assert type(decoded.cell.unadjusted.amount) is float
    assert math.copysign(1, decoded.cell.unadjusted.pct_change) == -1


def test_default_statistics_remain_single_range_and_candidate_omits_intervening_months(tmp_path: Path) -> None:
    path = tmp_path / "prices_2026.sqlite3"
    _prices(path)
    statements: list[str] = []
    reader = HistoryPricePartition(path, 2026, 1, statement_trace=statements.append, sqlite_policy=SqlitePolicyFixture())
    reader.seal_months((1, 2, 12), 3)
    assert sum("COUNT(DISTINCT code)" in statement for statement in statements) == 1
    statements.clear()
    reader.seal_months((1, 2, 12), 3, exact_month_ranges=True)
    statistics = tuple(statement for statement in statements if "COUNT(DISTINCT code)" in statement)
    assert len(statistics) == 2
    assert f"trade_date<{EncodeDate(date(2026, 3, 1))}" in statistics[0]
    assert f"trade_date>={EncodeDate(date(2026, 12, 1))}" in statistics[1]


@pytest.mark.parametrize("existing_receipt", [False, True])
def test_statistics_failure_after_preparation_never_publishes_new_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing_receipt: bool
) -> None:
    path = tmp_path / "prices_2026.sqlite3"
    writer = _prices(path)
    receipt = Path(f"{path}.sha256")
    if existing_receipt:
        writer.seal_months((1, 12), 3)
    previous = receipt.read_bytes() if existing_receipt else None
    run = SqliteConnection.run

    def fail_statistics(connection: SqliteConnection, sql: str, parameters: SqliteParameters = ()) -> SqliteResult:
        if "COUNT(DISTINCT code)" in sql:
            raise sqlite3.OperationalError("injected statistics failure")
        return run(connection, sql, parameters)

    monkeypatch.setattr(SqliteConnection, "run", fail_statistics)
    with pytest.raises(HistoryPricePartitionError, match="publication failed"):
        writer.seal_months((1, 12), 3, exact_month_ranges=True, fingerprint_before_statistics=True)
    assert (receipt.read_bytes() if receipt.exists() else None) == previous


@pytest.mark.parametrize("existing_receipt", [False, True])
def test_cancellation_after_statistics_cannot_publish_prepared_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing_receipt: bool
) -> None:
    path = tmp_path / "prices_2026.sqlite3"
    writer = _prices(path)
    receipt = Path(f"{path}.sha256")
    if existing_receipt:
        writer.seal_months((1, 12), 3)
    previous = receipt.read_bytes() if existing_receipt else None
    cancelled = False
    activity = HistorySqliteActivity(lambda: None, lambda: cancelled)
    reader = HistoryPricePartition(path, 2026, 1, activity=activity, sqlite_policy=SqlitePolicyFixture())
    run = SqliteConnection.run

    def cancel_statistics(connection: SqliteConnection, sql: str, parameters: SqliteParameters = ()) -> SqliteResult:
        nonlocal cancelled
        result = run(connection, sql, parameters)
        if "COUNT(DISTINCT code)" in sql:
            cancelled = True
        return result

    monkeypatch.setattr(SqliteConnection, "run", cancel_statistics)
    with pytest.raises(KeyboardInterrupt):
        reader.seal_months((1, 12), 3, fingerprint_before_statistics=True)
    assert (receipt.read_bytes() if receipt.exists() else None) == previous
