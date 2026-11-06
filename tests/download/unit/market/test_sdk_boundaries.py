"""Supplier parsing and cleanup stay behind the injected SDK operation boundary."""

import sys
from concurrent.futures import CancelledError
from datetime import date
from types import SimpleNamespace

import pytest

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
