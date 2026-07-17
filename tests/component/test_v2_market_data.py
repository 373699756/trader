from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

from trader.application.ports import MarketDataUnavailable
from trader.domain.models import Evidence, MarketQuote
from trader.infrastructure.market_data.akshare import AkshareResearchClient
from trader.infrastructure.market_data.calendar import ChinaTradingCalendar, TradingCalendarUnavailable
from trader.infrastructure.market_data.eastmoney import EastmoneyClient
from trader.infrastructure.market_data.features import FeatureBuilder
from trader.infrastructure.market_data.gateway import MarketDataGateway
from trader.infrastructure.market_data.history import DailyBar
from trader.infrastructure.market_data.service import MarketFeatureService
from trader.infrastructure.market_data.sina import SinaClient
from trader.infrastructure.market_data.tencent import TencentClient
from trader.infrastructure.settings import ConfigurationError, load_strategy_settings

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
                    "f124": int((NOW - timedelta(minutes=1)).timestamp()),
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
    assert quotes[0].source_time == NOW - timedelta(minutes=1)
    assert quotes[0].data_version == f"eastmoney:{int(NOW.timestamp())}"
    assert history[0].amount == 100000000
    assert all(call[1]["proxies"] == {"http": "", "https": "", "all": ""} for call in session.calls)


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
    session = FakeSession([body])
    client = TencentClient(timeout_seconds=2, session_factory=lambda: session)

    quotes = client.fetch_quotes(["600001"], NOW)

    assert quotes[0].price == 12.0
    assert quotes[0].amount == 300000000.0
    assert quotes[0].source_time.isoformat() == "2026-07-16T10:00:00+08:00"
    assert session.calls[0][1]["proxies"] == {"http": "", "https": "", "all": ""}


def test_sina_market_request_bypasses_environment_proxy() -> None:
    session = FakeSession(
        [
            b"1",
            [
                {
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
                    "mktcap": "3000000",
                }
            ],
        ]
    )
    client = SinaClient(timeout_seconds=2, session_factory=lambda: session)

    quotes = client.fetch_market(NOW)

    assert quotes[0].code == "600001"
    assert all(call[1]["proxies"] == {"http": "", "https": "", "all": ""} for call in session.calls)


def test_market_sources_retry_transient_disconnect_and_page_504() -> None:
    eastmoney_payload = {
        "data": {
            "total": 1,
            "diff": [{"f12": "600001", "f14": "测试股份", "f124": int(NOW.timestamp())}],
        }
    }
    eastmoney_session = FakeSession(
        [
            requests.ConnectionError("remote closed"),
            requests.ConnectionError("remote closed"),
            requests.ConnectionError("remote closed"),
            eastmoney_payload,
        ]
    )
    eastmoney = EastmoneyClient(timeout_seconds=2, session_factory=lambda: eastmoney_session)

    assert eastmoney.fetch_market(NOW)[0].code == "600001"
    assert len(eastmoney_session.calls) == 4

    sina_session = FakeSession(
        [
            b"1",
            requests.HTTPError("504 Server Error: Gateway Time-out"),
            [{"symbol": "sh600001", "name": "测试股份", "trade": "12.00"}],
        ]
    )
    sina = SinaClient(timeout_seconds=2, session_factory=lambda: sina_session)

    assert sina.fetch_market(NOW)[0].code == "600001"
    assert len(sina_session.calls) == 3


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


def test_gateway_reports_recoverable_unavailability_when_all_sources_fail() -> None:
    gateway = MarketDataGateway(
        FailingMarketClient(),
        FailingMarketClient(),
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=1,
        circuit_breaker_seconds=60,
    )

    with pytest.raises(MarketDataUnavailable, match="eastmoney:offline; sina:offline"):
        gateway.fetch_market()

    health = gateway.health()
    assert health["active_source"] == "unavailable"
    assert health["sources"]["eastmoney"]["circuit_open"] is True
    assert health["sources"]["sina"]["circuit_open"] is True


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


def test_feature_builder_partitions_cross_sections_and_excludes_missing_breadth() -> None:
    builder = FeatureBuilder()
    quotes = (
        replace(_quote(code="600001"), speed=0.1, pct_change=1.0, data_version="v1"),
        replace(_quote(code="600002"), speed=0.2, pct_change=-1.0, data_version="v1"),
        replace(_quote(code="600003"), speed=0.1, pct_change=None, data_version="v2"),
        replace(_quote(code="600004"), speed=0.2, pct_change=2.0, data_version="v2"),
    )

    features = builder.build(quotes, {}, NOW)

    assert [item.values["speed_percentile"] for item in features] == [0.0, 100.0, 0.0, 100.0]
    assert features[0].values["market_breadth"] == 50.0
    assert features[2].values["market_breadth"] == 100.0
    assert features[0].normalization["speed_percentile"].sample_size == 2
    assert features[2].normalization["market_breadth"].missing_count == 1
    assert features[2].values["limit_proximity"] is None
    assert "pct_change" in features[2].missing_fields
    assert "limit_proximity" in features[2].missing_fields


def test_market_service_bounds_history_preload_to_stratified_candidate_universe() -> None:
    history = CountingHistoryClient(_history_bars())
    quotes = tuple(_quote(code=f"60000{index}", industry="工业" if index % 2 else "银行") for index in range(1, 6))
    service = MarketFeatureService(
        StaticGateway(quotes),
        history,
        FeatureBuilder(),
        history_workers=2,
        history_preload_limit=2,
    )

    features = service.fetch_market_features(NOW)

    assert len(history.calls) == 2
    assert sum(item.history_days >= 20 for item in features) == 2
    assert service.health()["history_universe_rows"] == 2


def test_market_service_loads_history_before_cold_start_candidate_cross_section() -> None:
    history = CountingHistoryClient(_history_bars())
    service = MarketFeatureService(
        StaticGateway((_quote(), _quote(code="600002"))),
        history,
        FeatureBuilder(),
        history_workers=2,
    )

    features = service.fetch_market_features(NOW)

    assert sorted(history.calls) == ["600001", "600002"]
    assert all(item.history_days == 60 for item in features)
    assert service.health()["history_coverage_ratio"] == 1.0
    assert service.health()["history_universe_rows"] == 2


def test_market_service_reloads_expired_history_and_reports_failed_coverage() -> None:
    clock = MutableMonotonic()
    history = SelectiveHistoryClient(_history_bars(), failing_codes={"600002"})
    service = MarketFeatureService(
        StaticGateway((_quote(), _quote(code="600002"))),
        history,
        FeatureBuilder(),
        history_workers=2,
        history_ttl_seconds=60,
        market_ttl_seconds=1,
        monotonic=clock,
    )

    first = service.fetch_market_features(NOW)
    clock.value = 61.0
    second = service.fetch_market_features(NOW + timedelta(minutes=1))

    assert history.calls.count("600001") == 2
    assert history.calls.count("600002") == 2
    assert first[1].optional_value("return_20d") is None
    assert second[1].optional_value("return_20d") is None
    assert service.health()["history_coverage_ratio"] == 0.5
    assert service.health()["history_error_count"] == 2


def test_strategy_factor_registry_is_complete_and_required() -> None:
    path = Path(__file__).parents[2] / "config" / "v2" / "strategy.json"
    settings = load_strategy_settings(path)

    assert settings.factor_registry["speed_percentile"].factor_id == "speed_percentile"
    assert settings.strategy_version.startswith("strategy_sha256_")


def test_strategy_loader_rejects_missing_factor_registration(tmp_path) -> None:
    source = Path(__file__).parents[2] / "config" / "v2" / "strategy.json"
    raw = json.loads(source.read_text(encoding="utf-8"))
    del raw["factor_registry"]["speed_percentile"]
    target = tmp_path / "strategy.json"
    target.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="factor_registry mismatch"):
        load_strategy_settings(target)


def test_akshare_news_is_bounded_and_normalized() -> None:
    callback = "jQuery35101792940631092459_1764599530165"
    payload = {
        "result": {
            "cmsArticleWebOld": [
                {
                    "title": "<em>测试股份</em>发布公告",
                    "date": "2026-07-16 09:00:00",
                    "mediaName": "交易所",
                }
            ]
        }
    }
    calls = []

    def get(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeResponse(f"{callback}({json.dumps(payload, ensure_ascii=False)});")

    evidence = AkshareResearchClient(timeout_seconds=8, get=get).fetch_news("600001", observed_at=NOW)

    assert evidence[0].evidence_type == "news"
    assert evidence[0].title == "测试股份发布公告"
    assert evidence[0].published_at.isoformat() == "2026-07-16T09:00:00+08:00"
    assert calls[0][1]["timeout"] == 8
    assert calls[0][1]["proxies"] == {"http": "", "https": "", "all": ""}


def test_candidate_news_is_cached_and_failure_does_not_block() -> None:
    news = Evidence("news-1", "news", "候选新闻", "fixture", NOW - timedelta(hours=1))
    research = StaticResearchClient((news,))
    service = MarketFeatureService(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(),
        research_client=research,
        research_workers=1,
    )

    first = service.fetch_candidate_features(("600001",), NOW)
    second = service.fetch_candidate_features(("600001",), NOW)

    assert research.calls == 1
    assert [item.evidence_id for item in first[0].evidence] == [first[0].evidence[0].evidence_id, "news-1"]
    assert second[0].evidence[-1].evidence_id == "news-1"

    degraded = MarketFeatureService(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(),
        research_client=FailingResearchClient(),
        research_workers=1,
    )
    result = degraded.fetch_candidate_features(("600001",), NOW)
    assert len(result) == 1
    assert len(result[0].evidence) == 1
    assert degraded.health()["research_error_count"] == 1
    assert degraded.health()["research_last_error"] == "offline"


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
        self.text = (
            payload.decode("gb18030") if isinstance(payload, bytes) else payload if isinstance(payload, str) else ""
        )

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, payloads) -> None:
        self._payloads = iter(payloads)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def get(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        payload = next(self._payloads)
        if isinstance(payload, Exception):
            raise payload
        return FakeResponse(payload)


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


class StaticGateway:
    def __init__(self, quotes) -> None:
        self._quotes = quotes

    def fetch_candidates(self, _codes):
        return self._quotes

    def fetch_market(self):
        return self._quotes

    @staticmethod
    def health():
        return {}


class StaticHistoryClient:
    @staticmethod
    def fetch_history(_code, *, days):
        return ()


class CountingHistoryClient:
    def __init__(self, bars) -> None:
        self._bars = bars
        self.calls = []

    def fetch_history(self, code, *, days):
        self.calls.append(code)
        return self._bars


class SelectiveHistoryClient(CountingHistoryClient):
    def __init__(self, bars, *, failing_codes) -> None:
        super().__init__(bars)
        self._failing_codes = set(failing_codes)

    def fetch_history(self, code, *, days):
        self.calls.append(code)
        if code in self._failing_codes:
            raise RuntimeError("offline")
        return self._bars


class MutableMonotonic:
    value = 0.0

    def __call__(self):
        return self.value


class StaticResearchClient:
    def __init__(self, evidence) -> None:
        self._evidence = evidence
        self.calls = 0

    def fetch_news(self, _code, *, observed_at):
        self.calls += 1
        return self._evidence


class FailingResearchClient:
    @staticmethod
    def fetch_news(_code, *, observed_at):
        raise RuntimeError("offline")


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


def _history_bars() -> tuple[DailyBar, ...]:
    return tuple(
        DailyBar(
            trade_date=f"2026-{5 + index // 30:02d}-{index % 30 + 1:02d}",
            open_price=10.0 + index / 100,
            close=10.0 + index / 100,
            high=10.2 + index / 100,
            low=9.8 + index / 100,
            volume=1_000_000,
            amount=100_000_000 + index,
            pct_change=0.1,
        )
        for index in range(60)
    )
