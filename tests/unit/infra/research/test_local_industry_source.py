"""Current recommendation industry reads the validated dated-query database."""

from __future__ import annotations

import json
from contextlib import closing
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from trader.download.infra.baostock_industry import BaoStockIndustryClient, BaoStockIndustryRow
from trader.infra.dated_query_cache import SQLiteDatedQueryCache
from trader.infra.sqlite_connection import OpenSqliteConnection
from trader.recommendation.infra.market_data.local_industry_source import LocalIndustrySource

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_AT = datetime(2026, 10, 16, 9, 30, tzinfo=_SHANGHAI)


def test_current_industry_reuses_local_query_then_refreshes_after_seven_days(tmp_path) -> None:
    calls: list[str] = []

    def fetch_rows(day: str, timeout: float) -> tuple[BaoStockIndustryRow, ...]:
        calls.append(day)
        return (BaoStockIndustryRow("sh.600001", "J66货币金融服务", "证监会行业分类", day),)

    path = tmp_path / "industry_queries.sqlite3"
    client = BaoStockIndustryClient(fetch_rows=fetch_rows, minimum_rows=1)
    source = LocalIndustrySource(path, client, minimum_rows=1)
    assert source.load_cached(_AT) == ()
    assert source.fetch(_AT)[0].fields["model_industry"] == "J66货币金融服务"
    assert calls == [_AT.date().isoformat()]
    assert source.fetch(_AT + timedelta(days=6)) == source.load_cached(_AT + timedelta(days=6))
    assert calls == [_AT.date().isoformat()]
    assert len(source.fetch(_AT + timedelta(days=7))) == 1
    assert calls == [_AT.date().isoformat(), (_AT + timedelta(days=7)).date().isoformat()]
    assert (
        SQLiteDatedQueryCache(path, "baostock_dated_industry_queries", "current_industry_queries").latest(
            (_AT + timedelta(days=7)).date()
        )
        is not None
    )


def test_current_industry_rejects_corrupt_local_evidence(tmp_path) -> None:
    path = tmp_path / "industry_queries.sqlite3"
    source = LocalIndustrySource(
        path,
        BaoStockIndustryClient(
            fetch_rows=lambda day, timeout: (
                BaoStockIndustryRow("sh.600001", "J66货币金融服务", "证监会行业分类", day),
            ),
            minimum_rows=1,
        ),
        minimum_rows=1,
    )
    source.fetch(_AT)
    with closing(OpenSqliteConnection(path)) as connection, connection:
        connection.execute("UPDATE current_industry_queries SET payload='[]'")
    assert source.load_cached(_AT) == ()


def test_current_industry_keeps_previous_valid_query_when_newest_is_corrupt(tmp_path) -> None:
    path = tmp_path / "industry_queries.sqlite3"
    source = LocalIndustrySource(
        path,
        BaoStockIndustryClient(
            fetch_rows=lambda day, timeout: (
                BaoStockIndustryRow("sh.600001", "J66货币金融服务", "证监会行业分类", day),
            ),
            minimum_rows=1,
        ),
        minimum_rows=1,
    )
    source.fetch(_AT)
    source.fetch(_AT + timedelta(days=7))
    with closing(OpenSqliteConnection(path)) as connection, connection:
        connection.execute(
            "UPDATE current_industry_queries SET payload='[]' WHERE day=?",
            ((_AT + timedelta(days=7)).date().isoformat(),),
        )
    cached = source.load_cached(_AT + timedelta(days=7))
    assert len(cached) == 1
    assert cached[0].source_time.date() == _AT.date()


def test_online_refresh_never_overwrites_same_day_history_query(tmp_path) -> None:
    path = tmp_path / "industry_queries.sqlite3"
    source = LocalIndustrySource(
        path,
        BaoStockIndustryClient(
            fetch_rows=lambda day, timeout: (
                BaoStockIndustryRow("sh.600001", "J66货币金融服务", "证监会行业分类", day),
            ),
            minimum_rows=1,
        ),
        minimum_rows=1,
    )
    source.fetch(_AT)
    historical = SQLiteDatedQueryCache(path, "baostock_dated_industry_queries")
    historical.save(
        "baostock_history_industry",
        _AT.date(),
        _AT.date(),
        json.dumps([["600001", _AT.date().isoformat(), "I65软件", "证监会行业分类"]], ensure_ascii=False),
    )
    assert historical.read(_AT.date(), _AT.date(), source="baostock_history_industry") is not None
    current = SQLiteDatedQueryCache(path, "baostock_dated_industry_queries", "current_industry_queries")
    assert current.read(_AT.date(), _AT.date(), source="baostock_current_industry") is not None
    assert source.load_cached(_AT)[0].fields["model_industry"] == "J66货币金融服务"


def test_historical_query_is_available_before_online_table_exists(tmp_path) -> None:
    path = tmp_path / "industry_queries.sqlite3"
    SQLiteDatedQueryCache(path, "baostock_dated_industry_queries").save(
        "baostock_history_industry",
        _AT.date(),
        _AT.date(),
        json.dumps([["600001", _AT.date().isoformat(), "I65软件", "证监会行业分类"]], ensure_ascii=False),
    )
    source = LocalIndustrySource(
        path,
        BaoStockIndustryClient(fetch_rows=lambda day, timeout: (), minimum_rows=1),
        minimum_rows=1,
    )
    assert source.load_cached(_AT)[0].fields["model_industry"] == "I65软件"
    assert source.fetch(_AT)[0].fields["model_industry"] == "I65软件"
