from __future__ import annotations

# This module intentionally re-exports shared component fixtures to the split suites.
# ruff: noqa: F401
import json
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import requests

from trader.application.latency import LatencyWaterfall
from trader.application.ports.data_plane import (
    DataPlaneRecoverySummary,
    DataPlaneUnavailableError,
    HistoricalFeatureRecord,
    RiskEvidenceRecord,
    SecurityMasterRecord,
    SourceCursorRecord,
)
from trader.application.ports.market import (
    MarketDataDeadlineExceededError,
    MarketDataFailedError,
    MarketDataNoDataError,
    MarketDataUnavailableError,
)
from trader.application.source_lanes import (
    LatestRequestLane,
    SourceLaneRegistry,
    SourceRequestSupersededError,
)
from trader.application.workers import BoundedExecutor
from trader.domain.market.models import (
    Board,
    Evidence,
    FeatureSnapshot,
    MarketQuote,
)
from trader.domain.market.news import NewsSignalPolicy
from trader.domain.market.research import FinancialReport, ResearchObservation
from trader.domain.market.tail import MinuteBar, TailSignalPolicy
from trader.infra.cache import BoundedLruCache
from trader.infra.market_data.history.history import (
    DailyBar,
    HistoryAdjustmentError,
    PriceAdjustment,
    build_history_context,
)
from trader.infra.market_data.history.history_seed import FallbackHistoryClient
from trader.infra.market_data.history.service_history import HistoryCache
from trader.infra.market_data.history.service_history_warmup import HistoryWarmup
from trader.infra.market_data.normalization.columnar import MarketChangeSet
from trader.infra.market_data.normalization.features import FeatureBuilder
from trader.infra.market_data.providers import tushare_records as tushare_records_module
from trader.infra.market_data.providers.akshare import AkshareResearchClient
from trader.infra.market_data.providers.eastmoney import EastmoneyClient
from trader.infra.market_data.providers.exchange_security_master import ExchangeSecurityMasterClient
from trader.infra.market_data.providers.sina import SinaClient
from trader.infra.market_data.providers.tencent import TencentClient
from trader.infra.market_data.providers.tushare import TushareClient, TushareHealthStatus
from trader.infra.market_data.references.calendar import ChinaTradingCalendar, TradingCalendarUnavailableError
from trader.infra.market_data.service import gateway as gateway_module
from trader.infra.market_data.service.facade import MarketFeatureDependencies, MarketFeatureService
from trader.infra.market_data.service.gateway import MarketDataGateway
from trader.infra.market_data.service.gateway_health import MarketGatewayHealthStatus, SecurityMasterHealthStatus
from trader.infra.market_data.service.market_cache_identity import _history_preload_codes
from trader.infra.market_data.service.observations import SourceObservation
from trader.infra.market_data.service.router import VendorRoute, VendorSeverity, route
from trader.infra.market_data.service.service_candidates import QuoteCache, QuoteCacheDependencies
from trader.infra.market_data.service.service_execution import MarketTaskRunner
from trader.infra.market_data.service.service_health import MarketDataHealth, MarketDataHealthDependencies
from trader.infra.market_data.service.service_intraday import IntradayLoader
from trader.infra.market_data.service.service_research import ResearchLoader
from trader.infra.market_data.service.service_research_data_plane import persist_research_component_statuses
from trader.infra.market_data.service.service_research_models import RESEARCH_COMPONENT_IDS
from trader.infra.market_data.service.service_tushare import (
    ReferenceLoader,
    ReferenceLoadRequest,
    _ReferenceLoadOptions,
)
from trader.infra.persistence.data_plane import DataPlaneRepository
from trader.infra.settings import ConfigurationError, load_runtime_settings, load_strategy_settings

NOW = datetime(2026, 7, 16, 2, 0, tzinfo=timezone.utc)
_SHANGHAI = ZoneInfo("Asia/Shanghai")
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
MARKET_REGIME_POLICY = _STRATEGY_SETTINGS.market_regime
LONG_POLICY = _STRATEGY_SETTINGS.long_research


def _service(
    gateway: Any,
    history_client: Any,
    feature_builder: Any,
    data_plane: DataPlaneRepository | None = None,
    **kwargs: Any,
) -> MarketFeatureService:
    monotonic = kwargs.pop("monotonic", time.monotonic)
    wall_clock = kwargs.pop("wall_clock", lambda: datetime.now(timezone.utc))
    worker_pool = kwargs.pop("worker_pool", None)
    source_lanes = kwargs.pop("source_lanes", None)
    cache = kwargs.pop("cache", None)
    runner = MarketTaskRunner(
        worker_pool=worker_pool,
        source_lanes=source_lanes,
        cache=cache,
        source_contract_versions=kwargs.pop("source_contract_versions", {"tushare": "tushare-component-v1"}),
        config_version=kwargs.pop("config_version", "component-default"),
        schema_version=kwargs.pop("schema_version", "market-v15"),
        wall_clock=wall_clock,
    )
    history = HistoryCache(
        history_client,
        runner,
        history_worker_pool=kwargs.pop("history_worker_pool", None),
        workers=kwargs.pop("history_workers", 6),
        ttl_seconds=kwargs.pop("history_ttl_seconds", 21_600),
        capacity=kwargs.pop("history_cache_limit", 360),
        history_data_plane=data_plane,
        monotonic=monotonic,
    )
    references = ReferenceLoader(
        gateway,
        history,
        runner,
        kwargs.pop("tushare_client", None),
        security_master_client=kwargs.pop("exchange_security_master_client", None),
        data_plane=data_plane,
        monotonic=monotonic,
    )
    warmup = HistoryWarmup(
        history,
        references,
        runner,
        batch_size=kwargs.pop("history_warmup_batch_size", 30),
        batch_timeout_seconds=kwargs.pop("history_warmup_batch_timeout_seconds", 20.0),
        monotonic=monotonic,
    )
    research = ResearchLoader(
        kwargs.pop("research_client", None),
        runner,
        data_plane=data_plane,
        workers=kwargs.pop("research_workers", 4),
        ttl_seconds=kwargs.pop("research_ttl_seconds", 600),
        circuit_breaker_failures=kwargs.pop("research_circuit_breaker_failures", 3),
        circuit_breaker_seconds=kwargs.pop("research_circuit_breaker_seconds", 60),
        capacity=kwargs.pop("research_cache_limit", 360),
        cache_dir=kwargs.pop("research_cache_dir", None),
        json_writer=kwargs.pop("json_writer", None),
        monotonic=monotonic,
    )
    intraday_capacity = kwargs.pop("intraday_cache_limit", 360)
    intraday = IntradayLoader(
        kwargs.pop("intraday_client", None),
        runner,
        workers=kwargs.pop("intraday_workers", 6),
        ttl_seconds=kwargs.pop("intraday_ttl_seconds", 45),
        batch_timeout_seconds=kwargs.pop("intraday_batch_timeout_seconds", 3),
        capacity=intraday_capacity,
        monotonic=monotonic,
    )
    quotes = QuoteCache(
        QuoteCacheDependencies(gateway, feature_builder, history, references),
        market_ttl_seconds=kwargs.pop("market_ttl_seconds", 30),
        candidate_capacity=intraday_capacity,
        monotonic=monotonic,
    )
    health = MarketDataHealth(
        MarketDataHealthDependencies(quotes, history, warmup, research, intraday, references),
        wall_clock=wall_clock,
    )
    history_preload_limit = kwargs.pop("history_preload_limit", 360)
    assert kwargs == {}
    return MarketFeatureService(
        MarketFeatureDependencies(quotes, history, warmup, research, intraday, references, runner, health),
        history_preload_limit=history_preload_limit,
    )


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


class CountingMarketClient(StaticMarketClient):
    def __init__(self, quotes) -> None:
        super().__init__(quotes)
        self.calls = 0

    def fetch_market(self):
        self.calls += 1
        return super().fetch_market()


class SequenceMarketClient:
    def __init__(self, results) -> None:
        self._results = iter(results)
        self.calls = 0

    def fetch_market(self):
        self.calls += 1
        result = next(self._results)
        if isinstance(result, Exception):
            raise result
        return result


class BlockingMarketClient(StaticMarketClient):
    def __init__(self, quotes) -> None:
        super().__init__(quotes)
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()

    def fetch_market(self):
        self.calls += 1
        self.started.set()
        self.release.wait(1.0)
        return super().fetch_market()


class CoordinatedMarketClient(StaticMarketClient):
    def __init__(self, quotes, started: threading.Barrier, release: threading.Event) -> None:
        super().__init__(quotes)
        self._started = started
        self._release = release
        self.calls = 0
        self.thread_name = ""

    def fetch_market(self):
        self.calls += 1
        self.thread_name = threading.current_thread().name
        self._started.wait(1.0)
        assert self._release.wait(1.0)
        return super().fetch_market()


class FakeTushareFrame:
    def __init__(self, rows) -> None:
        self._rows = rows

    def to_dict(self, orient: str):
        assert orient == "records"
        return list(self._rows)


class FakeTusharePro:
    def __init__(self, rows) -> None:
        self._rows = rows
        self.calls: list[tuple[str, dict[str, object]]] = []

    def stock_basic(self, **kwargs):
        self.calls.append(("stock_basic", kwargs))
        return FakeTushareFrame(self._rows)


class StaticTencentClient:
    def __init__(self, quotes) -> None:
        self._quotes = quotes

    def fetch_quotes(self, _codes, *, timeout_seconds=None):
        del timeout_seconds
        return self._quotes


class BlockingTencentClient(StaticTencentClient):
    def __init__(self, quotes) -> None:
        super().__init__(quotes)
        self.release = threading.Event()

    def fetch_quotes(self, codes, *, timeout_seconds=None):
        self.release.wait(1.0)
        return super().fetch_quotes(codes, timeout_seconds=timeout_seconds)


class FailFirstTencentClient(StaticTencentClient):
    def __init__(self, quotes) -> None:
        super().__init__(quotes)
        self.calls = 0

    def fetch_quotes(self, codes, *, timeout_seconds=None):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("long quote failure")
        return super().fetch_quotes(codes, timeout_seconds=timeout_seconds)


def _empty_gateway_health() -> MarketGatewayHealthStatus:
    return MarketGatewayHealthStatus(
        active_source="unavailable",
        cached_rows=0,
        merge_count=0,
        conflict_count=0,
        snapshot=None,
        changes=MarketChangeSet("", (), (), ()),
        route=None,
        source_lanes=None,
        security_master=SecurityMasterHealthStatus(0, 0, 0, 0, "free_market+production_calendar", False, 0),
        sources={},
        cache=None,
        latency_waterfall=LatencyWaterfall().status(),
    )


def _tushare_health(
    *,
    enabled: bool,
    history_mode: str,
    degraded_reason: str | None = None,
) -> TushareHealthStatus:
    return TushareHealthStatus(
        enabled=enabled,
        access_points=0,
        history_mode=history_mode,
        minute_call_limit=0,
        daily_call_limit=0,
        process_api_attempts_last_minute=0,
        process_api_attempts_today=0,
        process_remaining_calls_today=0,
        local_rate_limit_count=0,
        planned_count=0,
        success_count=0,
        error_count=0,
        consecutive_failures=0,
        circuit_open=False,
        timeout_count=0,
        last_latency_ms=0.0,
        p50_latency_ms=None,
        p95_latency_ms=None,
        degraded_reason=degraded_reason,
        timeout_seconds=8.0,
        data_age_seconds=None,
    )


class StaticGateway:
    def __init__(self, quotes) -> None:
        self._quotes = quotes

    def fetch_candidates(self, _codes, **_kwargs):
        return self._quotes

    def fetch_market(self, **_kwargs):
        return self._quotes

    def current_quotes(self, codes):
        requested = set(codes)
        return tuple(quote for quote in self._quotes if quote.code in requested)

    @staticmethod
    def reference_observations(_codes):
        return ()

    @staticmethod
    def health():
        return _empty_gateway_health()


class StaticGatewayWithSeparateQuotes(StaticGateway):
    def __init__(self, market_quotes, candidate_quotes) -> None:
        super().__init__(market_quotes)
        self._candidate_quotes = candidate_quotes

    def fetch_candidates(self, _codes, **_kwargs):
        return self._candidate_quotes


class ThreadRecordingGateway(StaticGateway):
    def __init__(self, quotes) -> None:
        super().__init__(quotes)
        self.thread_names = []

    def fetch_market(self, **kwargs):
        self.thread_names.append(threading.current_thread().name)
        return super().fetch_market(**kwargs)

    def fetch_candidates(self, codes, **kwargs):
        self.thread_names.append(threading.current_thread().name)
        return super().fetch_candidates(codes, **kwargs)


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


class BlockingHistoryClient(CountingHistoryClient):
    def __init__(self, bars) -> None:
        super().__init__(bars)
        self.release = threading.Event()

    def fetch_history(self, code, *, days):
        self.release.wait(1.0)
        return super().fetch_history(code, days=days)


class ThreadRecordingHistoryClient(CountingHistoryClient):
    def __init__(self, bars) -> None:
        super().__init__(bars)
        self.thread_names = []

    def fetch_history(self, code, *, days):
        self.thread_names.append(threading.current_thread().name)
        return super().fetch_history(code, days=days)


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


class BlockingResearchClient(StaticResearchClient):
    def __init__(self, evidence) -> None:
        super().__init__(evidence)
        self.release = threading.Event()

    def fetch_news(self, code, *, observed_at):
        self.release.wait(1.0)
        return super().fetch_news(code, observed_at=observed_at)


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


class BlockingStructuredResearchClient:
    def __init__(self) -> None:
        self.release = threading.Event()

    def fetch_snapshot(self, _code, *, observed_at):
        self.release.wait(2.0)
        return ResearchObservation(announcements_available=True)


class PartiallyBlockingStructuredResearchClient:
    def __init__(self) -> None:
        self.release = threading.Event()

    def fetch_snapshot(self, code, *, observed_at):
        if code == "600002":
            self.release.wait(2.0)
        return ResearchObservation(
            announcements_available=True,
            corporate_risk_history_complete=True,
            corporate_risk_registry_version=f"registry:{code}",
            pledge_ratio_pct=0.0,
            unlock_ratio_pct=0.0,
        )


class StaticIntradayClient:
    def __init__(self, bars) -> None:
        self._bars = bars
        self.calls = []

    def fetch_intraday_minutes(self, code, *, now):
        self.calls.append(code)
        return self._bars


class SequenceIntradayClient:
    def __init__(self, batches) -> None:
        self._batches = iter(batches)

    def fetch_intraday_minutes(self, _code, *, now):
        return next(self._batches)


class FailingIntradayClient:
    @staticmethod
    def fetch_intraday_minutes(_code, *, now):
        raise RuntimeError("offline")


class BlockingIntradayClient:
    def __init__(self) -> None:
        self.release = threading.Event()
        self.started = threading.Event()
        self.finished = threading.Event()
        self.calls = []

    def fetch_intraday_minutes(self, code, *, now):
        self.calls.append(code)
        self.started.set()
        self.release.wait(2.0)
        self.finished.set()
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
            adjustment=PriceAdjustment.QFQ,
            source="fixture",
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
