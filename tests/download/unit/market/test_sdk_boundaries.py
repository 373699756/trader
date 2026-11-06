"""Supplier parsing and cleanup stay behind the injected SDK operation boundary."""

import sys
from concurrent.futures import CancelledError
from datetime import date
from types import SimpleNamespace

import pytest

from trader.download.infra import baostock_industry
from trader.download.infra.akshare_calendar import AkshareCalendarTimeoutError, FetchAkshareCalendar


def test_akshare_column_normalization_and_timeout_are_owned_by_adapter(monkeypatch):
    frame = {"trade_date": ["2026-10-16", "2026-10-15", "2026-10-16"]}

    class Frame:
        columns = tuple(frame)

        def __getitem__(self, key):
            return frame[key]

    monkeypatch.setitem(sys.modules, "akshare", SimpleNamespace(tool_trade_date_hist_sina=Frame))
    timeouts = []

    def execute(operation, timeout):
        timeouts.append(timeout)
        return operation()

    assert FetchAkshareCalendar(3, execute=execute) == (date(2026, 10, 15), date(2026, 10, 16))
    assert timeouts == [3]


def test_calendar_timeout_keeps_cause_and_limit_without_exposing_sdk_message():
    cause = TimeoutError("private supplier details")

    def execute(_operation, _timeout):
        raise cause

    with pytest.raises(AkshareCalendarTimeoutError) as caught:
        FetchAkshareCalendar(30, execute=execute)

    assert caught.value.timeout_seconds == 30
    assert caught.value.__cause__ is cause
    assert str(caught.value) == "akshare_sina_calendar_timeout"


def test_calendar_cancellation_is_not_reported_as_timeout():
    cause = CancelledError("sdk_cancelled")

    def execute(_operation, _timeout):
        raise cause

    with pytest.raises(CancelledError) as caught:
        FetchAkshareCalendar(30, execute=execute)

    assert caught.value is cause


@pytest.mark.parametrize("pagination_error", [False, True])
def test_industry_query_uses_shared_session_and_always_logs_out(monkeypatch, pagination_error):
    events = []

    class Rows:
        fields = ("code", "industry", "industryClassification", "updateDate")
        error_code = "0"
        count = 0

        def next(self):
            self.count += 1
            if self.count == 2 and pagination_error:
                self.error_code = "-1"
            return self.count == 1

        def get_row_data(self):
            return ("sh600001", "制造业", "证监会行业分类", "2026-10-16")

    class Limited:
        def __init__(self, sdk):
            assert sdk is client

        def protect_after_login(self):
            events.append("pace_after_login")

        def query_stock_industry(self, *, code, date):
            assert code == "" and date == "2026-10-16"
            events.append("query")
            return Rows()

    client = object()
    monkeypatch.setattr(baostock_industry, "LoadBaostockSdk", lambda: client)
    monkeypatch.setattr(baostock_industry, "LoginBaostock", lambda sdk: events.append("login"))
    monkeypatch.setattr(baostock_industry, "LogoutBaostock", lambda sdk: events.append("logout"))
    monkeypatch.setattr(baostock_industry, "RateLimitedBaoStockSdk", Limited)

    def execute(operation, timeout):
        assert timeout == 3
        return operation()

    if pagination_error:
        with pytest.raises(RuntimeError, match="pagination_rejected"):
            baostock_industry.FetchBaoStockIndustries("2026-10-16", 3, execute=execute)
    else:
        result = baostock_industry.FetchBaoStockIndustries("2026-10-16", 3, execute=execute)
        assert result[0].source_code == "sh600001" and result[0].industry == "制造业"
    assert events == ["login", "pace_after_login", "query", "logout"]
