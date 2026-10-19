from contextlib import closing
from dataclasses import replace
from datetime import date, timedelta

import pytest

from tests.unit.infra.research.test_baostock_gateway import _Result, _Sdk
from trader.download.domain.baostock_daily import BaoStockCalendar, BaoStockDailySpec, BaoStockSecurity
from trader.download.infra.baostock_gateway import BaoStockRowGateway
from trader.download.infra.history_industry_cache import HistoryIndustryQueryCache
from trader.infra.sqlite_connection import OpenSqliteConnection

DATES = (date(2020, 2, 3), date(2020, 7, 1), date(2021, 1, 4), date(2026, 10, 9))
UNIVERSE = (BaoStockSecurity("600001", "A", "main", date(2000, 1, 1), None, "test"),)


class _IndustrySdk(_Sdk):
    def __init__(self):
        self.calls = []
        self.fail_on = None
        self.empty = False
        self.future = False

    def query_stock_industry(self, *, code="", date=""):
        self.calls.append(date)
        if date == self.fail_on:
            raise RuntimeError("supplier_industry_timeout")
        return _Result(
            ("updateDate", "code", "industry", "industryClassification"),
            ()
            if self.empty
            else tuple(
                ("2099-01-01" if self.future else date, code, "bank", "test") for code in ("sh.600001", "sh.600002")
            ),
        )


def _fetch(sdk, cache, dates=DATES, universe=UNIVERSE, progress=None):
    return BaoStockRowGateway(sdk).fetch_industry_intervals(
        BaoStockDailySpec(sessions=len(dates)), BaoStockCalendar(dates), universe, cache=cache, progress=progress
    )


def test_same_day_reuses_complete_dates_and_new_universe_is_filtered_at_read(tmp_path):
    cache = HistoryIndustryQueryCache(tmp_path / "industry_queries.sqlite3")
    sdk = _IndustrySdk()
    original = _fetch(sdk, cache)
    assert len(sdk.calls) == 4
    sdk.calls.clear()
    events = []
    assert _fetch(sdk, cache, progress=events.append) == original
    assert sdk.calls == []
    assert [event.completed_units for event in events if event.state == "completed"] == [1, 2, 3, 4]
    assert all(event.cache_hit for event in events if event.state == "completed")
    added = replace(UNIVERSE[0], code="600002")
    result = _fetch(sdk, cache, universe=(added,))
    assert {item.code for item in result} == {"600002"}
    assert sdk.calls == []


def test_failed_date_resumes_without_requerying_successful_dates(tmp_path):
    cache = HistoryIndustryQueryCache(tmp_path / "industry_queries.sqlite3")
    sdk = _IndustrySdk()
    sdk.fail_on = DATES[2].isoformat()
    with pytest.raises(RuntimeError, match="supplier_industry_timeout"):
        _fetch(sdk, cache)
    sdk.fail_on = None
    sdk.calls.clear()
    _fetch(sdk, cache)
    assert sdk.calls == [str(day) for day in DATES[2:]]


def test_calendar_roll_keeps_historical_queries_and_only_fetches_new_dates(tmp_path):
    cache = HistoryIndustryQueryCache(tmp_path / "industry_queries.sqlite3")
    sdk = _IndustrySdk()
    _fetch(sdk, cache)
    sdk.calls.clear()
    dates = (date(2020, 2, 4), *DATES[1:-1], date(2026, 10, 12))
    _fetch(sdk, cache, dates)
    assert sdk.calls == ["2020-02-04", "2026-10-12"]
    sdk.calls.clear()
    _fetch(sdk, cache, (*dates[:-1], date(2026, 11, 12)))
    assert sdk.calls == ["2026-11-12"]
    sdk.calls.clear()
    assert _fetch(sdk, cache)  # The old rolling-window dates remain available.
    assert sdk.calls == []


@pytest.mark.parametrize("kind", ("empty", "future"))
def test_empty_or_future_only_responses_are_not_cached(tmp_path, kind):
    cache = HistoryIndustryQueryCache(tmp_path / "industry_queries.sqlite3")
    sdk = _IndustrySdk()
    setattr(sdk, kind, True)
    assert _fetch(sdk, cache) == ()
    sdk.calls.clear()
    assert _fetch(sdk, cache) == ()
    assert len(sdk.calls) == 4


def test_corrupt_cached_payload_requeries_only_that_date_and_source_change_invalidates(tmp_path):
    cache = HistoryIndustryQueryCache(tmp_path / "industry_queries.sqlite3")
    sdk = _IndustrySdk()
    original = _fetch(sdk, cache)
    with closing(OpenSqliteConnection(cache.path)) as connection, connection:
        connection.run("UPDATE queries SET payload='[]' WHERE day=?", (str(DATES[1]),))
    sdk.calls.clear()
    assert _fetch(sdk, cache) == original
    assert sdk.calls == [str(DATES[1])]
    sdk.__version__ = "changed"
    sdk.calls.clear()
    _fetch(sdk, cache)
    assert len(sdk.calls) == 4


def test_pagination_failure_never_creates_a_complete_query(tmp_path):
    cache = HistoryIndustryQueryCache(tmp_path / "industry_queries.sqlite3")

    class BrokenPage(_IndustrySdk):
        def query_stock_industry(self, **kwargs):
            result = super().query_stock_industry(**kwargs)

            def fail_next():
                raise RuntimeError("page_failed")

            result.next = fail_next
            return result

    with pytest.raises(RuntimeError, match="page_failed"):
        _fetch(BrokenPage(), cache)
    sdk = _IndustrySdk()
    _fetch(sdk, cache)
    assert len(sdk.calls) == 4


def test_recent_fixed_anchor_is_reused_when_cutoff_advances(tmp_path):
    cache = HistoryIndustryQueryCache(tmp_path / "industry_queries.sqlite3")
    sdk = _IndustrySdk()
    dates = (date(2026, 7, 1), date(2026, 10, 9))
    _fetch(sdk, cache, dates)
    sdk.calls.clear()
    _fetch(sdk, cache, (dates[0], dates[-1] + timedelta(days=3)))
    assert sdk.calls == ["2026-10-12"]
    sdk.calls.clear()
    _fetch(sdk, cache, (dates[0], dates[-1] + timedelta(days=3)))
    assert sdk.calls == []


def test_unreadable_sqlite_cache_has_explicit_failure_and_is_not_deleted(tmp_path):
    cache = HistoryIndustryQueryCache(tmp_path / "industry_queries.sqlite3")
    cache.path.write_bytes(b"not a database")
    with pytest.raises(RuntimeError, match="industry_query_cache_unavailable"):
        _fetch(_IndustrySdk(), cache)
    assert cache.path.read_bytes() == b"not a database"


def test_missing_listed_security_reuses_full_market_query_without_inventing_industry(tmp_path):
    cache = HistoryIndustryQueryCache(tmp_path / "industry_queries.sqlite3")
    sdk = _IndustrySdk()
    _fetch(sdk, cache)
    sdk.calls.clear()
    missing = replace(UNIVERSE[0], code="600003", listed_on=DATES[-1])
    for _ in range(3):
        events = []
        result = _fetch(sdk, cache, universe=(*UNIVERSE, missing), progress=events.append)
        assert sdk.calls == []
        assert {item.code for item in result} == {UNIVERSE[0].code}
        assert events[-1].industry_missing_codes == 1
        assert events[-1].cache_hit is True
