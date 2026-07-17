from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

from trader.application.ports import MarketDataUnavailable
from trader.domain.models import Evidence, MarketQuote, Strategy
from trader.domain.news import NewsSignalPolicy
from trader.domain.research import ResearchObservation
from trader.domain.strategies import score_strategy
from trader.domain.tail import MinuteBar, TailSignalPolicy
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
AFTERNOON = datetime.fromisoformat("2026-07-16T14:50:00+08:00")
NEWS_POLICY = NewsSignalPolicy(
    lookback_hours=72.0,
    freshness_full_score_hours=1.0,
    positive_score=75.0,
    neutral_score=50.0,
    negative_score=25.0,
    positive_keywords=("回购", "增持", "中标"),
    negative_keywords=("减持", "立案", "亏损"),
)
TAIL_POLICY = TailSignalPolicy(
    lookback_minutes=30,
    minimum_baseline_minutes=30,
    return_score_points_per_pct=25.0,
    volume_score_points_per_ratio=50.0,
)
_STRATEGY_SETTINGS = load_strategy_settings(Path(__file__).parents[2] / "config" / "v2" / "strategy.json")
D25_POLICY = _STRATEGY_SETTINGS.d25_signal
LONG_POLICY = _STRATEGY_SETTINGS.long_research


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


def test_eastmoney_normalizes_unadjusted_intraday_minutes() -> None:
    payload = {
        "data": {
            "trends": [
                "2026-07-16 14:49,10.00,10.10,10.20,9.90,100,1010,10.05",
                "2026-07-16 14:50,10.10,10.20,10.30,10.00,150,1530,10.10",
                "invalid,row",
            ]
        }
    }
    session = FakeSession([payload])
    client = EastmoneyClient(timeout_seconds=2, session_factory=lambda: session)

    bars = client.fetch_intraday_minutes("600001", now=AFTERNOON)

    assert [bar.close for bar in bars] == [10.1, 10.2]
    assert [bar.volume for bar in bars] == [100.0, 150.0]
    assert bars[-1].source_time.isoformat() == "2026-07-16T14:50:00+08:00"
    assert bars[-1].received_time == AFTERNOON
    assert bars[-1].data_version == f"eastmoney-intraday:{int(AFTERNOON.timestamp())}"
    assert bars[-1].source == "eastmoney_intraday"
    assert session.calls[0][0][0].endswith("/api/qt/stock/trends2/get")
    assert session.calls[0][1]["params"]["ndays"] == "1"
    assert "fqt" not in session.calls[0][1]["params"]
    assert session.calls[0][1]["timeout"] == 2
    assert session.calls[0][1]["proxies"] == {"http": "", "https": "", "all": ""}


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

    with_history, without_history = FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY).build(
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
    builder = FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY)
    low = replace(_quote(code="600001"), speed=0.1)
    middle = replace(_quote(code="600002"), speed=0.2)
    high = replace(_quote(code="600003"), speed=0.3)
    market = builder.build((low, middle, high), {}, NOW)
    reference = {item.quote.code: item.values for item in market}

    targeted = builder.build((high,), {}, NOW, cross_section_reference=reference)

    assert market[-1].values["speed_percentile"] == 100.0
    assert targeted[0].values["speed_percentile"] == 100.0


def test_feature_builder_partitions_cross_sections_and_excludes_missing_breadth() -> None:
    builder = FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY)
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
    assert features[0].market_regime == "neutral"
    assert features[0].values["market_regime_factor"] == pytest.approx(1.0)
    assert features[2].market_regime == "risk_on"
    assert features[2].values["market_regime_factor"] == pytest.approx(1.03)
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
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
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
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
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
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
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


def test_feature_builder_derives_auditable_tail_inputs_without_fabricating_missing_values() -> None:
    builder = FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY)
    available, missing = builder.build(
        (_quote(), _quote(code="600002")),
        {},
        AFTERNOON,
        intraday_minutes={"600001": _tail_minute_bars()},
    )

    assert available.values["tail_return_30m_pct"] == pytest.approx(2.0)
    assert available.values["tail_return_30m"] == pytest.approx(100.0)
    assert available.values["tail_volume_ratio_raw"] == pytest.approx(1.5)
    assert available.values["tail_volume_ratio"] == pytest.approx(75.0)
    tail_evidence = next(item for item in available.evidence if item.evidence_type == "intraday_tail")
    assert tail_evidence.source == "eastmoney_intraday"
    assert tail_evidence.published_at == AFTERNOON
    assert tail_evidence.received_at == AFTERNOON
    assert tail_evidence.data_version == "intraday-v1"
    assert "30分钟收益=2.000000%" in tail_evidence.title
    assert "量比=1.500000" in tail_evidence.title
    assert missing.values["tail_return_30m_pct"] is None
    assert missing.values["tail_return_30m"] is None
    assert missing.values["tail_volume_ratio_raw"] is None
    assert missing.values["tail_volume_ratio"] is None
    assert "tail_return_30m" in missing.missing_fields


def test_feature_builder_populates_every_tomorrow_component_from_point_in_time_inputs() -> None:
    feature = FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY).build(
        (_quote(),),
        {"600001": _history_bars()},
        AFTERNOON,
        intraday_minutes={"600001": _tail_minute_bars()},
    )[0]

    required = {
        "amount_percentile_20d",
        "relative_strength_5d",
        "relative_strength_20d",
        "price_volume_confirmation",
        "moderate_daily_return",
        "ma20_60_position",
        "ma_slope",
        "breakout_20d",
        "industry_trend",
        "risk_adjusted_return_20d",
        "low_drawdown_score",
        "upward_consistency",
        "capacity_score",
        "moderate_amplitude",
        "limit_distance_safety",
        "tail_return_30m",
        "tail_volume_ratio",
        "close_location",
    }
    assert all(feature.optional_value(name) is not None for name in required)
    assert required.isdisjoint(feature.missing_fields)


@pytest.mark.parametrize(
    ("price", "high", "low", "expected"),
    (
        (10.0, 12.0, 10.0, 0.0),
        (11.0, 12.0, 10.0, 50.0),
        (12.0, 12.0, 10.0, 100.0),
        (11.0, 10.0, 10.0, None),
        (float("nan"), 12.0, 10.0, None),
    ),
)
def test_close_location_has_exact_boundaries_and_preserves_missing(
    price: float,
    high: float,
    low: float,
    expected: float | None,
) -> None:
    quote = replace(_quote(), price=price, high=high, low=low)

    feature = FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY).build((quote,), {}, NOW)[0]

    if expected is None:
        assert feature.optional_value("close_location") is None
    else:
        assert feature.optional_value("close_location") == pytest.approx(expected)


def test_zero_historical_return_has_neutral_price_volume_confirmation() -> None:
    bars = tuple(
        DailyBar(
            trade_date=f"2026-06-{index + 1:02d}",
            open_price=10.0,
            close=10.0,
            high=10.1,
            low=9.9,
            volume=1_000_000.0,
            amount=100_000_000.0,
            pct_change=0.0,
        )
        for index in range(21)
    )
    quote = replace(_quote(), price=10.0, amount=100_000_000.0)

    feature = FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY).build(
        (quote,), {quote.code: bars}, NOW
    )[0]

    assert feature.values["return_5d"] == pytest.approx(0.0)
    assert feature.values["price_volume_confirmation"] == pytest.approx(50.0)


def test_market_service_fetches_intraday_minutes_only_for_requested_candidate_mode() -> None:
    intraday = StaticIntradayClient(_tail_minute_bars())
    service = MarketFeatureService(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
        intraday_client=intraday,
        intraday_workers=1,
    )

    service.fetch_market_features(AFTERNOON)
    without_tail = service.fetch_candidate_features(("600001",), AFTERNOON)
    with_tail = service.fetch_candidate_features(
        ("600001",),
        AFTERNOON,
        include_intraday_tail=True,
    )

    assert intraday.calls == ["600001"]
    assert "tail_return_30m" not in without_tail[0].values
    assert "tail_return_30m" not in without_tail[0].missing_fields
    assert with_tail[0].values["tail_return_30m"] == pytest.approx(100.0)
    assert service.health()["intraday_tail_success_count"] == 1
    assert service.health()["intraday_tail_covered_rows"] == 1
    assert service.health()["intraday_tail_latest_source_time"] == AFTERNOON.isoformat()
    assert service.health()["intraday_tail_sources"] == ("eastmoney_intraday",)
    assert service.health()["intraday_tail_data_versions"] == ("intraday-v1",)


def test_intraday_cache_has_a_hard_entry_limit() -> None:
    intraday = StaticIntradayClient(_tail_minute_bars())
    service = MarketFeatureService(
        StaticGateway((_quote(), _quote(code="600002"))),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
        intraday_client=intraday,
        intraday_workers=1,
        intraday_cache_limit=1,
    )

    service.fetch_candidate_features(("600001",), AFTERNOON, include_intraday_tail=True)
    service.fetch_candidate_features(("600002",), AFTERNOON, include_intraday_tail=True)

    assert intraday.calls == ["600001", "600002"]
    assert service.health()["intraday_tail_cache_entries"] == 1


def test_intraday_failure_keeps_tomorrow_features_available_and_marks_missing() -> None:
    service = MarketFeatureService(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
        intraday_client=FailingIntradayClient(),
        intraday_workers=1,
    )

    result = service.fetch_candidate_features(
        ("600001",),
        AFTERNOON,
        include_intraday_tail=True,
    )

    assert len(result) == 1
    assert result[0].values["tail_return_30m"] is None
    assert result[0].values["tail_volume_ratio"] is None
    assert "tail_return_30m" in result[0].missing_fields
    assert service.health()["intraday_tail_error_count"] == 1
    assert service.health()["intraday_tail_last_error"] == "offline"


def test_intraday_health_requires_complete_tail_signals_for_coverage() -> None:
    intraday = StaticIntradayClient(_tail_minute_bars()[-10:])
    service = MarketFeatureService(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
        intraday_client=intraday,
        intraday_workers=1,
    )

    result = service.fetch_candidate_features(
        ("600001",),
        AFTERNOON,
        include_intraday_tail=True,
    )

    assert result[0].values["tail_return_30m"] is None
    assert result[0].values["tail_volume_ratio"] is None
    assert service.health()["intraday_tail_covered_rows"] == 0
    assert service.health()["intraday_tail_coverage_ratio"] == 0.0
    assert service.health()["intraday_tail_last_error"] == "intraday_series_incomplete"


def test_intraday_batch_deadline_does_not_wait_for_every_candidate_request() -> None:
    intraday = BlockingIntradayClient()
    service = MarketFeatureService(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
        intraday_client=intraday,
        intraday_workers=1,
        intraday_batch_timeout_seconds=0.01,
    )

    started = time.monotonic()
    try:
        result = service.fetch_candidate_features(
            ("600001",),
            AFTERNOON,
            include_intraday_tail=True,
        )
    finally:
        intraday.release.set()

    assert time.monotonic() - started < 0.5
    assert result[0].values["tail_return_30m"] is None
    assert service.health()["intraday_tail_last_error"] == "intraday_batch_deadline"


def test_akshare_news_is_bounded_and_normalized() -> None:
    callback = "jQuery35101792940631092459_1764599530165"
    payload = {
        "result": {
            "cmsArticleWebOld": [
                {"title": "时间未知", "date": "invalid", "mediaName": "交易所"},
                {"title": "未来新闻", "date": "2026-07-16 11:00:00", "mediaName": "交易所"},
                {
                    "title": "<em>测试股份</em>发布公告",
                    "date": "2026-07-16 09:00:00",
                    "mediaName": "交易所",
                },
            ]
        }
    }
    calls = []

    def get(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeResponse(f"{callback}({json.dumps(payload, ensure_ascii=False)});")

    evidence = AkshareResearchClient(timeout_seconds=8, get=get).fetch_news("600001", observed_at=NOW, limit=1)

    assert evidence[0].evidence_type == "news"
    assert evidence[0].title == "测试股份发布公告"
    assert evidence[0].published_at.isoformat() == "2026-07-16T09:00:00+08:00"
    assert calls[0][1]["timeout"] == 8
    assert calls[0][1]["proxies"] == {"http": "", "https": "", "all": ""}


def test_akshare_structured_research_is_point_in_time_and_builds_real_long_inputs() -> None:
    financial_payload = {
        "version": "financial-v1",
        "result": {
            "data": [
                {
                    "REPORT_DATE": "2026-06-30 00:00:00",
                    "NOTICE_DATE": "2026-07-17 00:00:00",
                    "EPSJB": 9.0,
                    "BPS": 9.0,
                },
                {
                    "REPORT_DATE": "2026-03-31 00:00:00",
                    "NOTICE_DATE": "2026-04-30 00:00:00",
                    "EPSJB": 1.0,
                    "BPS": 10.0,
                    "TOTALOPERATEREVETZ": 20.0,
                    "PARENTNETPROFITTZ": 10.0,
                    "KCFJCXSYJLRTZ": 0.0,
                    "ROEJQ": 3.0,
                    "PARENTNETPROFIT": 100.0,
                    "KCFJCXSYJLR": 80.0,
                },
            ]
        },
        "success": True,
    }
    announcement_payload = {
        "data": {
            "list": [
                {
                    "art_code": "future",
                    "display_time": "2026-07-17 09:00:00:000",
                    "notice_date": "2026-07-17 00:00:00",
                    "title": "未来公告",
                    "columns": [{"column_name": "重大事项"}],
                },
                {
                    "art_code": "a-1",
                    "display_time": "2026-07-15 10:00:00:000",
                    "notice_date": "2026-07-15 00:00:00",
                    "title": "控股股东减持并收到监管函",
                    "columns": [{"column_name": "持股变动"}],
                },
                {
                    "art_code": "a-2",
                    "display_time": "2026-07-14 10:00:00:000",
                    "notice_date": "2026-07-14 00:00:00",
                    "title": "公司获得政策支持并获批新项目",
                    "columns": [{"column_name": "重大事项"}],
                },
                *(
                    {
                        "art_code": f"normal-{index}",
                        "display_time": "2026-07-13 10:00:00:000",
                        "notice_date": "2026-07-13 00:00:00",
                        "title": f"公司日常经营公告{index}",
                        "columns": [{"column_name": "其他"}],
                    }
                    for index in range(20)
                ),
            ],
            "total_hits": 23,
        },
        "success": 1,
    }
    pledge_payload = {
        "version": "pledge-v1",
        "result": {"data": [{"NOTICE_DATE": "2026-07-01", "ACCUM_PLEDGE_TSR": 15.0}]},
        "success": True,
    }
    unlock_payload = {
        "version": "unlock-v1",
        "result": {
            "data": [
                {"FREE_DATE": "2026-08-01", "TOTAL_RATIO": 0.06},
                {"FREE_DATE": "2027-01-01", "TOTAL_RATIO": 0.50},
            ]
        },
        "success": True,
    }
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        if "securities/api/data/get" in url:
            return FakeResponse(financial_payload)
        if "api/security/ann" in url:
            return FakeResponse(announcement_payload)
        report = kwargs["params"].get("reportName")
        if report == "RPTA_APP_ACCUMDETAILS":
            return FakeResponse(pledge_payload)
        if report == "RPT_LIFT_STAGE":
            return FakeResponse(unlock_payload)
        raise AssertionError(f"unexpected research URL: {url}")

    observation = AkshareResearchClient(
        timeout_seconds=8,
        get=get,
        long_research_policy=LONG_POLICY,
    ).fetch_snapshot("600001", observed_at=AFTERNOON)
    feature = FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY).build(
        (replace(_quote(), price=20.0),),
        {"600001": _history_bars()},
        AFTERNOON,
        research_observations={"600001": observation},
    )[0]

    assert observation.financial is not None
    assert observation.financial.report_date == date(2026, 3, 31)
    assert len(observation.announcements) == 22
    assert observation.pledge_ratio_pct == pytest.approx(15.0)
    assert observation.unlock_ratio_pct == pytest.approx(6.0)
    assert feature.values["value_score"] == pytest.approx(92.8571428571)
    assert feature.values["growth_score"] == pytest.approx(70.0)
    assert feature.values["quality_score"] == pytest.approx(67.5)
    assert feature.values["pledge_risk"] == 1.0
    assert feature.values["reduction_or_unlock"] == 3.0
    assert feature.values["negative_announcement_level"] == 2.0
    assert {item.evidence_type for item in feature.evidence} >= {
        "financial_snapshot",
        "announcement",
        "ownership_filing",
        "research_summary",
    }
    financial_evidence = next(item for item in feature.evidence if item.evidence_type == "financial_snapshot")
    pledge_evidence = next(item for item in feature.evidence if item.source == "eastmoney_pledge")
    assert "EPS=1" in financial_evidence.title
    assert "core_profit=80" in financial_evidence.title
    assert pledge_evidence.published_at.isoformat() == "2026-07-01T23:59:59+08:00"
    assert all(call[1]["timeout"] == 8 for call in calls)
    assert all(call[1]["proxies"] == {"http": "", "https": "", "all": ""} for call in calls)
    assert all("search-api-web" not in call[0] for call in calls)


def test_structured_research_source_failure_preserves_null_and_other_sources() -> None:
    observation = ResearchObservation(
        announcements_available=True,
        pledge_ratio_pct=None,
        unlock_ratio_pct=0.0,
        source_errors=("pledge:timeout",),
    )

    feature = FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY).build(
        (_quote(),),
        {"600001": _history_bars()},
        AFTERNOON,
        research_observations={"600001": observation},
    )[0]

    assert feature.values["pledge_risk"] is None
    assert feature.values["reduction_or_unlock"] == 0.0
    assert "pledge_risk" in feature.missing_fields
    assert feature.values["risk_protection_score"] is not None


@pytest.mark.parametrize(
    "unlock_row",
    (
        {"FREE_DATE": "invalid", "TOTAL_RATIO": 0.01},
        {"FREE_DATE": "2026-08-01", "TOTAL_RATIO": 1.01},
    ),
)
def test_akshare_structured_research_contains_malformed_source_failure(unlock_row) -> None:
    def get(url, **kwargs):
        if "securities/api/data/get" in url:
            return FakeResponse({"version": "financial-empty", "result": {"data": []}, "success": True})
        if "api/security/ann" in url:
            return FakeResponse({"data": {"list": [], "total_hits": 0}, "success": 1})
        if kwargs["params"].get("reportName") == "RPTA_APP_ACCUMDETAILS":
            return FakeResponse(
                {
                    "version": "pledge-invalid",
                    "result": {"data": [{"NOTICE_DATE": "2026-07-01", "ACCUM_PLEDGE_TSR": "invalid"}]},
                    "success": True,
                }
            )
        return FakeResponse({"version": "unlock-invalid", "result": {"data": [unlock_row]}, "success": True})

    observation = AkshareResearchClient(
        timeout_seconds=8,
        get=get,
        long_research_policy=LONG_POLICY,
    ).fetch_snapshot("600001", observed_at=AFTERNOON)

    assert observation.announcements_available is True
    assert observation.unlock_ratio_pct is None
    assert observation.pledge_ratio_pct is None
    assert observation.source_errors == ("pledge:ValueError", "unlock:ValueError")


def test_akshare_research_rejects_unvalidated_stock_code() -> None:
    client = AkshareResearchClient(get=lambda *_args, **_kwargs: FakeResponse(""))

    with pytest.raises(ValueError, match="six digits"):
        client.fetch_news('600001") OR ("1"="1', observed_at=AFTERNOON)


def test_candidate_news_is_cached_and_failure_does_not_block() -> None:
    news = Evidence("news-1", "news", "公司拟回购股份", "fixture", NOW - timedelta(hours=1))
    research = StaticResearchClient((news,))
    service = MarketFeatureService(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
        research_client=research,
        research_workers=1,
    )

    first = service.fetch_candidate_features(("600001",), NOW)
    second = service.fetch_candidate_features(("600001",), NOW)

    assert research.calls == 1
    assert [item.evidence_id for item in first[0].evidence] == [first[0].evidence[0].evidence_id, "news-1"]
    assert second[0].evidence[-1].evidence_id == "news-1"
    assert first[0].values["news_sentiment"] == 75.0
    assert first[0].values["evidence_freshness"] == 100.0
    assert "news_sentiment" not in first[0].missing_fields
    assert score_strategy(Strategy.TODAY, first[0]).components["sentiment"] == 87.5

    degraded = MarketFeatureService(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
        research_client=FailingResearchClient(),
        research_workers=1,
    )
    result = degraded.fetch_candidate_features(("600001",), NOW)
    assert len(result) == 1
    assert len(result[0].evidence) == 1
    assert result[0].values["news_sentiment"] is None
    assert result[0].values["evidence_freshness"] is None
    assert "news_sentiment" in result[0].missing_fields
    assert score_strategy(Strategy.TODAY, result[0]).components["sentiment"] == 60.0
    assert degraded.health()["research_error_count"] == 1
    assert degraded.health()["research_last_error"] == "offline"


def test_structured_research_upgrades_news_only_cache_and_is_reused() -> None:
    news = Evidence("news-1", "news", "公司拟回购股份", "fixture", NOW - timedelta(hours=1))
    research = StaticStructuredResearchClient(
        news,
        ResearchObservation(
            evidence=(news,),
            announcements_available=True,
            pledge_ratio_pct=15.0,
            unlock_ratio_pct=0.0,
        ),
    )
    service = MarketFeatureService(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
        research_client=research,
        research_workers=1,
    )

    news_only = service.fetch_candidate_features(("600001",), NOW)
    first_full = service.fetch_candidate_features(
        ("600001",),
        NOW,
        include_structured_research=True,
    )
    second_full = service.fetch_candidate_features(
        ("600001",),
        NOW,
        include_structured_research=True,
    )

    assert research.news_calls == 1
    assert research.snapshot_calls == 1
    assert news_only[0].values["pledge_risk"] is None
    assert first_full[0].values["pledge_risk"] == 1.0
    assert second_full[0].values["pledge_risk"] == 1.0


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


class StaticStructuredResearchClient:
    def __init__(self, news, observation) -> None:
        self._news = (news,)
        self._observation = observation
        self.news_calls = 0
        self.snapshot_calls = 0

    def fetch_news(self, _code, *, observed_at):
        self.news_calls += 1
        return self._news

    def fetch_snapshot(self, _code, *, observed_at):
        self.snapshot_calls += 1
        return self._observation


class StaticIntradayClient:
    def __init__(self, bars) -> None:
        self._bars = bars
        self.calls = []

    def fetch_intraday_minutes(self, code, *, now):
        self.calls.append(code)
        return self._bars


class FailingIntradayClient:
    @staticmethod
    def fetch_intraday_minutes(_code, *, now):
        raise RuntimeError("offline")


class BlockingIntradayClient:
    def __init__(self) -> None:
        self.release = threading.Event()

    def fetch_intraday_minutes(self, _code, *, now):
        self.release.wait(2.0)
        return _tail_minute_bars()


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


def _tail_minute_bars() -> tuple[MinuteBar, ...]:
    start = AFTERNOON - timedelta(minutes=60)
    return tuple(
        MinuteBar(
            source_time=start + timedelta(minutes=index),
            close=10.2 if index == 60 else 10.0,
            volume=150.0 if index >= 31 else 100.0,
            source="eastmoney_intraday",
            received_time=AFTERNOON,
            data_version="intraday-v1",
        )
        for index in range(61)
    )
