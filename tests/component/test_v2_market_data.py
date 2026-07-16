from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from trader.domain.models import MarketQuote
from trader.infrastructure.market_data.calendar import ChinaTradingCalendar, TradingCalendarUnavailable
from trader.infrastructure.market_data.eastmoney import EastmoneyClient
from trader.infrastructure.market_data.features import FeatureBuilder
from trader.infrastructure.market_data.gateway import MarketDataGateway
from trader.infrastructure.market_data.history import DailyBar
from trader.infrastructure.market_data.tencent import TencentClient

NOW = datetime(2026, 7, 16, 2, 0, tzinfo=timezone.utc)


def test_eastmoney_normalizes_quote_and_history() -> None:
    quote_payload = {
        "data": {
            "total": 1,
            "diff": [
                {
                    "f2": 12.0,
                    "f3": 3.0,
                    "f6": 300000000,
                    "f7": 4.0,
                    "f8": 3.0,
                    "f10": 2.0,
                    "f11": 1.0,
                    "f12": "600001",
                    "f14": "测试股份",
                    "f15": 12.2,
                    "f16": 11.7,
                    "f17": 11.8,
                    "f18": 11.65,
                    "f20": 30000000000,
                    "f22": 0.8,
                    "f100": "工业",
                    "f124": int(NOW.timestamp()),
                }
            ],
        }
    }
    history_payload = {"data": {"klines": ["2026-07-15,10,11,12,9,100,100000000,3,1"]}}
    session = FakeSession([quote_payload, history_payload])
    client = EastmoneyClient(timeout_seconds=2, session_factory=lambda: session)

    quotes = client.fetch_market(NOW)
    history = client.fetch_history("600001", days=90, now=NOW)

    assert len(quotes) == 1
    assert quotes[0].code == "600001"
    assert quotes[0].industry == "工业"
    assert quotes[0].source_time == NOW
    assert history[0].amount == 100000000


def test_tencent_normalizes_targeted_quote() -> None:
    fields = [""] * 50
    fields[1] = "测试股份"
    fields[2] = "600001"
    fields[3] = "12.00"
    fields[4] = "11.65"
    fields[5] = "11.80"
    fields[30] = "20260716100000"
    fields[32] = "3.00"
    fields[33] = "12.20"
    fields[34] = "11.70"
    fields[35] = "0/0/300000000"
    fields[38] = "3.0"
    fields[43] = "4.0"
    fields[49] = "2.0"
    body = f'v_sh600001="{"~".join(fields)}";'.encode("gb18030")
    client = TencentClient(timeout_seconds=2, session_factory=lambda: FakeSession([body]))

    quotes = client.fetch_quotes(["600001"], NOW)

    assert quotes[0].price == 12.0
    assert quotes[0].amount == 300000000.0
    assert quotes[0].source_time.isoformat() == "2026-07-16T10:00:00+08:00"


def test_gateway_falls_back_and_tracks_health() -> None:
    quote = _quote()
    gateway = MarketDataGateway(
        FailingMarketClient(),
        StaticMarketClient((quote,)),
        StaticTencentClient((quote,)),
        minimum_market_rows=1,
        circuit_breaker_failures=1,
        circuit_breaker_seconds=60,
    )

    assert gateway.fetch_market() == (quote,)
    health = gateway.health()

    assert health["active_source"] == "sina"
    assert health["sources"]["eastmoney"]["circuit_open"] is True


def test_feature_builder_marks_history_missing_and_builds_cross_section() -> None:
    quote = _quote()
    bars = tuple(
        DailyBar(
            trade_date=f"2026-06-{index:02d}",
            open_price=10 + index / 100,
            close=10 + index / 100,
            high=10.2 + index / 100,
            low=9.8 + index / 100,
            volume=1_000_000,
            amount=100_000_000 + index,
            pct_change=0.1,
        )
        for index in range(1, 61)
    )

    with_history, without_history = FeatureBuilder().build(
        (quote, _quote(code="600002", industry="银行")),
        {"600001": bars},
        NOW,
    )

    assert with_history.history_days == 60
    assert with_history.optional_value("return_20d") is not None
    assert without_history.history_days == 0
    assert without_history.optional_value("return_20d") is None
    assert "return_20d" in without_history.missing_fields


def test_targeted_feature_build_preserves_full_market_cross_section() -> None:
    builder = FeatureBuilder()
    low = replace(_quote(code="600001"), speed=0.1)
    middle = replace(_quote(code="600002"), speed=0.2)
    high = replace(_quote(code="600003"), speed=0.3)
    market = builder.build((low, middle, high), {}, NOW)
    reference = {item.quote.code: item.values for item in market}

    targeted = builder.build((high,), {}, NOW, cross_section_reference=reference)

    assert market[-1].values["speed_percentile"] == 100.0
    assert targeted[0].values["speed_percentile"] == 100.0


def test_calendar_uses_cache_and_fails_closed(tmp_path) -> None:
    cache = tmp_path / "calendar.json"
    cache.write_text(
        json.dumps({"fetched_at": NOW.isoformat(), "dates": ["2026-07-16"]}),
        encoding="utf-8",
    )
    calendar = ChinaTradingCalendar(cache, now=lambda: NOW)

    assert calendar.is_trading_day(date(2026, 7, 16)) is True
    assert calendar.is_trading_day(date(2026, 7, 18)) is False

    unavailable = ChinaTradingCalendar(
        tmp_path / "missing.json",
        fetcher=lambda: (_ for _ in ()).throw(RuntimeError("offline")),
        now=lambda: NOW,
    )
    with pytest.raises(TradingCalendarUnavailable):
        unavailable.is_trading_day(date(2026, 7, 16))

    stale_cache = tmp_path / "stale-calendar.json"
    stale_cache.write_text(
        json.dumps({"fetched_at": (NOW - timedelta(days=31)).isoformat(), "dates": ["2026-07-16"]}),
        encoding="utf-8",
    )
    stale = ChinaTradingCalendar(
        stale_cache,
        fetcher=lambda: (_ for _ in ()).throw(RuntimeError("offline")),
        now=lambda: NOW,
    )
    with pytest.raises(TradingCalendarUnavailable, match="cannot refresh"):
        stale.is_trading_day(date(2026, 7, 16))


class FakeResponse:
    def __init__(self, payload) -> None:
        self._payload = payload
        self.content = payload if isinstance(payload, bytes) else b""
        self.text = payload.decode("gb18030") if isinstance(payload, bytes) else ""

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, payloads) -> None:
        self._payloads = iter(payloads)

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def get(self, *_args, **_kwargs):
        return FakeResponse(next(self._payloads))


class FailingMarketClient:
    @staticmethod
    def fetch_market():
        raise RuntimeError("offline")


class StaticMarketClient:
    def __init__(self, quotes) -> None:
        self._quotes = quotes

    def fetch_market(self):
        return self._quotes


class StaticTencentClient:
    def __init__(self, quotes) -> None:
        self._quotes = quotes

    def fetch_quotes(self, _codes):
        return self._quotes


def _quote(code: str = "600001", industry: str = "工业") -> MarketQuote:
    return MarketQuote(
        code=code,
        name="测试股份",
        price=12.0,
        previous_close=11.65,
        open_price=11.8,
        high=12.2,
        low=11.7,
        pct_change=3.0,
        change_5m=1.0,
        speed=0.8,
        volume_ratio=2.0,
        turnover_rate=3.0,
        amount=300_000_000.0,
        amplitude=4.0,
        market_cap=30_000_000_000.0,
        industry=industry,
        source="fixture",
        source_time=NOW,
        received_time=NOW,
        data_version="fixture-v1",
    )
