"""Sina source time requires the supplier's own date and clock fields."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.recommendation.market_data import FakeSession
from trader.recommendation.infra.market_data.providers.sina import SinaClient

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_RECEIVED = datetime(2026, 7, 16, 10, 5, tzinfo=_SHANGHAI)


def _row(**changes):
    return {
        "symbol": "sh600001",
        "name": "测试股份",
        "trade": "12.00",
        "settlement": "11.65",
        "open": "11.80",
        "high": "12.20",
        "low": "11.70",
        "changepercent": "3.00",
        "turnoverratio": "3.0",
        "amount": "300000000",
        "volume": "25000000",
        "mktcap": "3000000",
        **changes,
    }


def _fetch(rows, received_at=_RECEIVED):
    session = FakeSession([str(len(rows)).encode(), rows])
    client = SinaClient(worker_pool=None, timeout_seconds=2, session_factory=lambda: session)
    quotes = client.fetch_market(received_at)
    assert len(session.calls) == 2
    return quotes


@pytest.mark.parametrize("date_field", ("day", "date"))
def test_sina_uses_the_explicit_supplier_date_and_ticktime(date_field):
    row = _row(**{date_field: "2026-07-16", "ticktime": "10:00:00"})

    quote = _fetch([row])[0]

    assert quote.source_time == datetime(2026, 7, 16, 10, tzinfo=_SHANGHAI)
    assert str(quote.source_time.tzinfo) == "Asia/Shanghai"
    assert quote.received_time == _RECEIVED
    assert quote.volume == 25_000_000.0
    assert quote.amount == 300_000_000.0
    assert quote.price == 12.0


@pytest.mark.parametrize("stamp", (
    "2026-07-16 10:00:00",
    "2026-07-16T10:00:00+08:00",
    "2026-07-16T02:00:00Z",
))
def test_sina_full_supplier_timestamp_preserves_source_in_shanghai(stamp):
    quote = _fetch([_row(ticktime=stamp)])[0]

    assert quote.source_time == datetime(2026, 7, 16, 10, tzinfo=_SHANGHAI)
    assert str(quote.source_time.tzinfo) == "Asia/Shanghai"
    assert quote.received_time == _RECEIVED


def test_sina_replayed_yesterday_payload_remains_yesterday_after_receiving_today():
    row = _row(day="2026-07-15", ticktime="15:00:00")
    first = _fetch([row], datetime(2026, 7, 15, 15, 1, tzinfo=_SHANGHAI))[0]
    repeated = _fetch([row], _RECEIVED)[0]

    assert first.source_time == repeated.source_time == datetime(2026, 7, 15, 15, tzinfo=_SHANGHAI)
    assert repeated.received_time == _RECEIVED
    assert repeated.received_time > first.received_time
    assert repeated.age_seconds(_RECEIVED) > 60 * 60


@pytest.mark.parametrize("fields", (
    {},
    {"ticktime": "10:00:00"},
    {"ticktime": "10:00:00", "received_at": "2026-07-16T10:05:00+08:00"},
    {"day": "2026-07-16"},
    {"day": "", "ticktime": "10:00:00"},
    {"day": "2026-02-30", "ticktime": "10:00:00"},
    {"day": "2026-07-16", "ticktime": "25:00:00"},
    {"day": "2026-07-16", "ticktime": "10:00"},
    {"day": True, "ticktime": "10:00:00"},
    {"date": 20260716, "ticktime": "10:00:00"},
    {"day": "2026-07-16", "date": "2026-07-15", "ticktime": "10:00:00"},
    {"day": "2026-07-15", "ticktime": "2026-07-16 10:00:00"},
    {"ticktime": "2026-07-16"},
    {"ticktime": "2026-02-30T10:00:00"},
    {"ticktime": "2026-07-16T10:00:00+24:00"},
    {"ticktime": "2026-07-17T10:00:00+08:00"},
))
def test_sina_missing_invalid_or_inconsistent_source_time_cannot_be_received_time(fields):
    with pytest.raises(RuntimeError, match="sina_source_time_unknown"):
        _fetch([_row(**fields)])


def test_sina_same_row_date_aliases_must_agree():
    quote = _fetch([_row(day="2026-07-16", date="2026-07-16", ticktime="10:00:00")])[0]
    assert quote.source_time == datetime(2026, 7, 16, 10, tzinfo=_SHANGHAI)


def test_sina_utc_received_clock_does_not_supply_the_supplier_date():
    received = _RECEIVED.astimezone(timezone.utc)
    quote = _fetch([_row(day="2026-07-16", ticktime="10:00:00")], received)[0]
    assert quote.source_time == datetime(2026, 7, 16, 10, tzinfo=_SHANGHAI)
    assert quote.received_time == received


def test_sina_incomplete_timestamp_population_is_rejected_instead_of_returning_partial_quotes():
    valid = _row(day="2026-07-16", ticktime="10:00:00")
    unknown = _row(symbol="sh600002", ticktime="10:00:00")
    with pytest.raises(RuntimeError, match="sina_source_time_unknown"):
        _fetch([valid, unknown])


def test_sina_fractional_source_seconds_are_preserved():
    quote = _fetch([_row(ticktime="2026-07-16T10:00:00.125000+08:00")])[0]
    assert quote.source_time == datetime(2026, 7, 16, 10, tzinfo=_SHANGHAI) + timedelta(microseconds=125000)
