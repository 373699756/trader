"""Reject incomplete repair metadata without rebuilding source or replay state."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.download.domain.published_history import PublishedHistoryCell, PublishedHistorySide
from trader.download.infra.history_control_index import HistoryControlError, HistoryControlIndex
from trader.download.infra.history_price_repair import HistoryPriceRepair
from trader.download.infra.published_history_codec import DecodePublishedHistoryCellPayload, EncodePublishedHistoryCell
from trader.infra.sqlite_codec import EncodeDate

_REPAIR_TABLES = ("price_repair_attempt", "price_tencent_check", "price_source", "price_replacement")
_SOURCE_AND_REPLAY_TABLES = ("price_tencent_check", "price_source", "price_replacement")
_DAY = date(2026, 11, 5)


def _control(path: Path) -> HistoryControlIndex:
    return HistoryControlIndex(path, sqlite_policy=SqlitePolicyFixture())


def _candidate() -> PublishedHistoryCell:
    raw = PublishedHistorySide(
        code="600001",
        trade_date=_DAY,
        adjustment="unadjusted",
        open_price=10.0,
        high_price=11.0,
        low_price=9.0,
        close_price=10.0,
        volume=100.0,
        amount=1000.0,
        preclose=10.0,
        pct_change=0.0,
        turnover=1.0,
        trading_status="trading",
    )
    qfq = replace(raw, adjustment="qfq", preclose=None, pct_change=None, turnover=None)
    return PublishedHistoryCell("600001", _DAY, "complete", raw, qfq)


def _snapshot(path: Path) -> tuple[str, ...]:
    with closing(sqlite3.connect(path)) as connection:
        return tuple(connection.iterdump())


@pytest.fixture
def catalog(tmp_path: Path) -> Path:
    path = tmp_path / "catalog.sqlite3"
    _control(path).initialize()
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("INSERT INTO price_source VALUES('600001',1,'baostock')")
        connection.execute("INSERT INTO price_tencent_check VALUES('600001',?)", (EncodeDate(_DAY),))
        connection.execute(
            "INSERT INTO price_repair_attempt VALUES('600001',?,'fixture','requested','supplier_result_unknown',NULL)",
            (EncodeDate(_DAY),),
        )
        connection.execute(
            "INSERT INTO price_replacement VALUES('600001',?,2,'main','baostock',?)",
            (EncodeDate(_DAY), EncodePublishedHistoryCell(_candidate())),
        )
    return path


def test_new_history_catalog_creates_complete_repair_metadata(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    _control(path).initialize()
    with closing(sqlite3.connect(path)) as connection:
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert set(_REPAIR_TABLES) <= names
        assert all(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0 for table in _REPAIR_TABLES)


@pytest.mark.parametrize("table", _SOURCE_AND_REPLAY_TABLES)
def test_missing_history_repair_table_is_rejected_without_rebuilding(catalog: Path, table: str) -> None:
    with closing(sqlite3.connect(catalog)) as connection, connection:
        connection.execute(f"DROP TABLE {table}")
    before = _snapshot(catalog)

    with pytest.raises(HistoryControlError) as captured:
        _control(catalog).initialize()

    assert isinstance(captured.value.__cause__, sqlite3.DatabaseError)
    assert str(captured.value.__cause__) == "sqlite_schema_unsupported"
    assert _snapshot(catalog) == before


@pytest.mark.parametrize("table", _SOURCE_AND_REPLAY_TABLES)
def test_incompatible_history_repair_columns_are_rejected_without_changes(catalog: Path, table: str) -> None:
    with closing(sqlite3.connect(catalog)) as connection, connection:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN unexpected TEXT")
    before = _snapshot(catalog)

    with pytest.raises(HistoryControlError) as captured:
        _control(catalog).initialize()

    assert isinstance(captured.value.__cause__, sqlite3.DatabaseError)
    assert str(captured.value.__cause__) == "sqlite_schema_unsupported"
    assert _snapshot(catalog) == before


def test_reopening_current_history_catalog_preserves_sources_and_replay_candidates(catalog: Path) -> None:
    before = _snapshot(catalog)
    _control(catalog).initialize()

    repair = HistoryPriceRepair(catalog.parent, sqlite_policy=SqlitePolicyFixture())
    assert repair.source("600001", 1) == "baostock"
    assert repair.baostock_codes() == frozenset({"600001"})
    assert repair.recheck_due(frozenset({"600001"}), (_DAY,)) == frozenset()
    with closing(sqlite3.connect(catalog)) as connection:
        row = connection.execute("SELECT trade_date,sync_sequence,board,source,payload FROM price_replacement").fetchone()
    assert row is not None
    assert row[:4] == (EncodeDate(_DAY), 2, "main", "baostock")
    assert DecodePublishedHistoryCellPayload("600001", _DAY.isoformat(), row[4]) == _candidate()
    assert _snapshot(catalog) == before
