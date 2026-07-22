from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

from trader.application.ports import (
    MarketDataDeadlineExceeded,
    MarketDataFailed,
    MarketDataNoData,
    MarketDataUnavailable,
)
from trader.application.source_lanes import (
    LatestRequestLane,
    SourceLaneRegistry,
    SourceRequestSuperseded,
)
from trader.application.workers import BoundedExecutor
from trader.domain.models import Evidence, MarketQuote, Strategy
from trader.domain.news import NewsSignalPolicy
from trader.domain.research import ResearchObservation
from trader.domain.strategies import score_strategy
from trader.domain.tail import MinuteBar, TailSignalPolicy
from trader.infrastructure.cache import BoundedLruCache
from trader.infrastructure.market_data import gateway as gateway_module
from trader.infrastructure.market_data import tushare_support as tushare_support_module
from trader.infrastructure.market_data.akshare import AkshareResearchClient
from trader.infrastructure.market_data.calendar import ChinaTradingCalendar, TradingCalendarUnavailable
from trader.infrastructure.market_data.eastmoney import EastmoneyClient
from trader.infrastructure.market_data.features import FeatureBuilder
from trader.infrastructure.market_data.gateway import MarketDataGateway
from trader.infrastructure.market_data.history import DailyBar
from trader.infrastructure.market_data.history_seed import FallbackHistoryClient, LocalHistorySeedClient
from trader.infrastructure.market_data.observations import SourceObservation
from trader.infrastructure.market_data.router import VendorRoute, VendorSeverity, route
from trader.infrastructure.market_data.service import MarketFeatureService
from trader.infrastructure.market_data.sina import SinaClient
from trader.infrastructure.market_data.tencent import TencentClient
from trader.infrastructure.market_data.tushare import TushareClient
from trader.infrastructure.settings import ConfigurationError, load_runtime_settings, load_strategy_settings

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


def test_local_history_seed_serves_last_valid_qfq_rows_without_calling_remote(tmp_path) -> None:
    database = tmp_path / "market_data.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE daily_bars (
                trade_date TEXT, code TEXT, volume REAL, turnover REAL,
                qfq_open REAL, qfq_close REAL, qfq_high REAL, qfq_low REAL, pct_chg REAL
            )
            """
        )
        connection.executemany(
            "INSERT INTO daily_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    (date(2026, 7, 15) - timedelta(days=index)).strftime("%Y%m%d"),
                    "600001",
                    1000.0,
                    10_500_000.0,
                    10.0,
                    10.5,
                    10.8,
                    9.9,
                    5.0,
                )
                for index in range(25)
            ],
        )

    class FailingRemote:
        calls = 0

        def fetch_history(self, _code, *, days):
            self.calls += 1
            raise RuntimeError("remote unavailable")

    remote = FailingRemote()
    rows = LocalHistorySeedClient(database, remote).fetch_history("600001", days=90)

    assert LocalHistorySeedClient(database, remote).available_codes(("600002", "600001")) == ("600001",)
    assert len(rows) == 25
    assert rows[-1].trade_date == "2026-07-15"
    assert rows[-1].volume == 100_000.0
    assert rows[-1].amount == 10_500_000.0
    assert remote.calls == 0


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


def test_tencent_history_preserves_volume_amount_and_turnover_fields() -> None:
    rows = [
        [
            (date(2026, 6, 1) + timedelta(days=index)).isoformat(),
            "10.00",
            f"{10.0 + index / 10:.2f}",
            "13.00",
            "9.90",
            "1000.00",
            {},
            "0.33",
            "6000.00",
            "",
        ]
        for index in range(21)
    ]
    body = "kline_dayqfq2026=" + json.dumps({"data": {"sh600001": {"qfqday": rows}}})
    session = FakeSession([body])
    client = TencentClient(
        timeout_seconds=8,
        session_factory=lambda: session,
        wall_clock=lambda: datetime(2026, 7, 15, 16, 30, tzinfo=timezone.utc),
    )

    bars = client.fetch_history("600001", days=20)

    assert len(bars) == 20
    assert bars[-1].volume == 100_000.0
    assert bars[-1].amount == 60_000_000.0
    assert bars[-1].turnover_rate == 0.33
    assert bars[-1].pct_change == pytest.approx((12.0 / 11.9 - 1.0) * 100.0)
    assert session.calls[0][0][0].startswith("https://proxy.finance.qq.com/")
    assert ",2026-07-16,640,qfq" in session.calls[0][1]["params"]["param"]
    assert session.calls[0][1]["params"]["param"].endswith(",640,qfq")
    assert session.calls[0][1]["proxies"] == {"http": "", "https": "", "all": ""}


def test_history_fallback_uses_eastmoney_only_when_tencent_is_insufficient() -> None:
    primary = CountingHistoryClient(())
    fallback = CountingHistoryClient(_history_bars())

    bars = FallbackHistoryClient(primary, fallback).fetch_history("600001", days=90)

    assert len(bars) == 60
    assert primary.calls == ["600001"]
    assert fallback.calls == ["600001"]


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


def test_sina_full_market_pages_are_fetched_with_bounded_parallelism() -> None:
    class ConcurrentSinaSession:
        def __init__(self, state) -> None:
            self._state = state

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def get(self, url, **kwargs):
            if "getHQNodeStockCount" in url:
                return FakeResponse(b"201")
            page = int(kwargs["params"]["page"])
            with self._state["lock"]:
                self._state["active"] += 1
                self._state["maximum"] = max(self._state["maximum"], self._state["active"])
            time.sleep(0.02)
            with self._state["lock"]:
                self._state["active"] -= 1
            return FakeResponse(
                [
                    {
                        "symbol": f"sh{600000 + (page - 1) * 100 + index:06d}",
                        "name": "测试股份",
                        "trade": "12.00",
                    }
                    for index in range(100 if page < 3 else 1)
                ]
            )

    state = {"active": 0, "maximum": 0, "lock": threading.Lock()}
    client = SinaClient(
        timeout_seconds=2,
        workers=3,
        session_factory=lambda: ConcurrentSinaSession(state),
    )

    quotes = client.fetch_market(NOW)

    assert len(quotes) == 201
    assert state["maximum"] >= 2


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

    fetched = tuple(gateway.fetch_market())
    assert [(item.code, item.price, item.source) for item in fetched] == [(quote.code, quote.price, "sina")]
    health = gateway.health()

    assert health["active_source"] == "sina"
    assert health["sources"]["eastmoney"]["circuit_open"] is True
    assert health["sources"]["eastmoney"]["planned_count"] == 1
    assert health["sources"]["eastmoney"]["error_count"] == 1
    assert health["sources"]["eastmoney"]["p50_latency_ms"] is not None
    assert health["sources"]["eastmoney"]["p95_latency_ms"] is not None
    assert health["sources"]["sina"]["planned_count"] == 1
    assert health["sources"]["sina"]["success_count"] == 1
    assert health["route"]["status"] == "success"
    assert health["route"]["degraded"] is True
    assert health["route"]["used_vendor"] == "sina"
    assert health["route"]["fallback_reason"] is None
    assert [item["name"] for item in health["route"]["attempted_vendors"]] == ["eastmoney", "sina"]
    assert health["route"]["attempted_vendors"][0]["status"] == "failed"
    assert health["route"]["attempted_vendors"][1]["status"] == "success"
    assert "eastmoney:source_failed" in gateway.canonical_snapshot().degraded_reasons


def test_source_lane_coalesces_running_identity_and_keeps_only_latest_pending_request() -> None:
    pool = BoundedExecutor(worker_count=5, queue_capacity=5, thread_name_prefix="source-data")
    pool.start()
    lane = LatestRequestLane("eastmoney", pool)
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def load(value: str) -> str:
        calls.append(value)
        if value == "running":
            started.set()
            assert release.wait(1.0)
        return value

    running = lane.submit("market", NOW, load, "running")
    coalesced = lane.submit("market", NOW, load, "duplicate")
    assert running is coalesced
    assert started.wait(1.0)
    superseded = lane.submit("history", NOW + timedelta(seconds=1), load, "superseded")
    latest = lane.submit("intraday", NOW + timedelta(seconds=2), load, "latest")

    try:
        with pytest.raises(SourceRequestSuperseded):
            superseded.result(timeout=1.0)
        release.set()
        assert running.result(timeout=1.0) == "running"
        assert latest.result(timeout=1.0) == "latest"
        assert calls == ["running", "latest"]
    finally:
        release.set()
        lane.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)

    assert lane.status()["running"] is False
    assert lane.status()["pending"] is False


def test_source_lane_stop_cancels_pending_request_and_waits_for_running_cleanup() -> None:
    pool = BoundedExecutor(worker_count=5, queue_capacity=5, thread_name_prefix="source-data")
    pool.start()
    lane = LatestRequestLane("tushare", pool)
    started = threading.Event()
    release = threading.Event()

    def blocked() -> str:
        started.set()
        assert release.wait(1.0)
        return "finished"

    running = lane.submit("master", NOW, blocked)
    assert started.wait(1.0)
    pending = lane.submit("calendar", NOW + timedelta(seconds=1), lambda: "must-not-run")

    lane.stop(wait=False)
    with pytest.raises(RuntimeError, match="source lane stopped"):
        pending.result(timeout=1.0)
    release.set()
    assert running.result(timeout=1.0) == "finished"
    lane.stop(wait=True, timeout_seconds=1.0)
    pool.stop(wait=True, cancel_futures=True)

    assert lane.status()["running"] is False
    assert lane.status()["pending"] is False


def test_source_lane_stop_cancels_runner_that_has_not_started() -> None:
    pool = BoundedExecutor(worker_count=1, queue_capacity=1, thread_name_prefix="source-data")
    pool.start()
    occupied = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def occupy_worker() -> None:
        occupied.set()
        assert release.wait(5.0)

    blocker = pool.submit(occupy_worker)
    assert blocker is not None
    assert occupied.wait(1.0)
    lane = LatestRequestLane("tushare", pool)
    queued = lane.submit("master", NOW, lambda: calls.append("unexpected"))

    try:
        lane.stop(wait=True, timeout_seconds=0.1)
        with pytest.raises(RuntimeError, match="runner stopped before execution"):
            queued.result(timeout=1.0)
    finally:
        release.set()
        blocker.result(timeout=1.0)
        pool.stop(wait=True, cancel_futures=True)

    assert calls == []
    assert lane.status()["running"] is False
    assert lane.status()["pending"] is False


def test_source_lane_marks_running_future_and_skips_cancelled_pending_io() -> None:
    pool = BoundedExecutor(worker_count=5, queue_capacity=5, thread_name_prefix="source-data")
    pool.start()
    lane = LatestRequestLane("akshare", pool)
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def load(value: str) -> str:
        calls.append(value)
        if value == "running":
            started.set()
            assert release.wait(1.0)
        return value

    running = lane.submit("research-running", NOW, load, "running")
    assert started.wait(1.0)
    pending = lane.submit("research-pending", NOW + timedelta(seconds=1), load, "cancelled-pending")

    try:
        assert running.cancel() is False
        assert pending.cancel() is True
        release.set()
        assert running.result(timeout=1.0) == "running"
        lane.stop(wait=True, timeout_seconds=1.0)
    finally:
        release.set()
        lane.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)

    assert calls == ["running"]
    assert lane.status()["running"] is False
    assert lane.status()["pending"] is False


def test_source_lane_replaces_cancelled_pending_identity_with_newest_request() -> None:
    pool = BoundedExecutor(worker_count=1, queue_capacity=1, thread_name_prefix="source-data")
    lane = LatestRequestLane("eastmoney", pool)
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def load(value: str) -> str:
        calls.append(value)
        if value == "running":
            started.set()
            assert release.wait(1.0)
        return value

    pool.start()
    running = lane.submit("running", NOW, load, "running")
    assert started.wait(1.0)
    cancelled = lane.submit("same-request", NOW + timedelta(seconds=1), load, "cancelled")
    assert cancelled.cancel() is True
    newest = lane.submit("same-request", NOW + timedelta(seconds=2), load, "newest")

    try:
        release.set()
        assert running.result(timeout=1.0) == "running"
        assert newest.result(timeout=1.0) == "newest"
    finally:
        release.set()
        lane.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)

    assert calls == ["running", "newest"]


def test_scheduled_tushare_reference_refresh_does_not_block_fast_source_lane() -> None:
    pool = BoundedExecutor(worker_count=5, queue_capacity=5, thread_name_prefix="source-data")
    lanes = SourceLaneRegistry(pool)

    class ReferenceGateway(StaticGateway):
        @staticmethod
        def update_reference_observations(_observations):
            return None

    class BlockingTushareClient:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()

        def fetch_security_master(self, _observed_at):
            self.started.set()
            assert self.release.wait(1.0)
            return ()

        @staticmethod
        def supports(_dataset):
            return True

    tushare = BlockingTushareClient()
    service = MarketFeatureService(
        ReferenceGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
        tushare_client=tushare,
        worker_pool=pool,
        source_lanes=lanes,
        wall_clock=lambda: NOW,
    )
    pool.start()

    try:
        started_at = time.perf_counter()
        service.schedule_reference_data((), NOW)
        scheduling_seconds = time.perf_counter() - started_at
        assert tushare.started.wait(1.0)
        fast = lanes.submit("eastmoney", "fast-market", NOW, lambda: "fast")
        assert fast.result(timeout=1.0) == "fast"
        assert scheduling_seconds < 0.1
    finally:
        tushare.release.set()
        lanes.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)


def test_scheduled_reference_refresh_starts_tushare_and_eastmoney_history_independently() -> None:
    pool = BoundedExecutor(worker_count=5, queue_capacity=5, thread_name_prefix="source-data")
    lanes = SourceLaneRegistry(pool)

    class ReferenceGateway(StaticGateway):
        @staticmethod
        def update_reference_observations(_observations):
            return None

    class BlockingTushareClient:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()

        def fetch_security_master(self, _observed_at):
            self.started.set()
            assert self.release.wait(1.0)
            return ()

        @staticmethod
        def supports(_dataset):
            return True

        @staticmethod
        def fetch_forward_adjusted_daily(*_args):
            return ()

        @staticmethod
        def fetch_daily_valuations(*_args):
            return ()

        @staticmethod
        def fetch_financial_indicators(*_args):
            return ()

    class RecordingHistoryClient:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.thread_name = ""

        def fetch_history(self, _code, *, days):
            assert days == 90
            self.thread_name = threading.current_thread().name
            self.started.set()
            return ()

    tushare = BlockingTushareClient()
    history = RecordingHistoryClient()
    service = MarketFeatureService(
        ReferenceGateway((_quote(),)),
        history,
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
        tushare_client=tushare,
        worker_pool=pool,
        source_lanes=lanes,
        wall_clock=lambda: NOW,
    )
    pool.start()

    try:
        service.schedule_reference_data(("600001",), NOW)
        assert tushare.started.wait(1.0)
        assert history.started.wait(0.2)
        assert history.thread_name.startswith("source-data")
    finally:
        tushare.release.set()
        lanes.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)


def test_history_activity_does_not_block_realtime_eastmoney_lane() -> None:
    pool = BoundedExecutor(worker_count=5, queue_capacity=5, thread_name_prefix="source-data")
    lanes = SourceLaneRegistry(pool)
    history_started = threading.Event()
    release_history = threading.Event()

    def block_history() -> None:
        history_started.set()
        assert release_history.wait(1.0)

    pool.start()
    try:
        history = lanes.submit("history", "history", NOW, block_history)
        assert history_started.wait(1.0)
        realtime = lanes.submit("eastmoney", "realtime", NOW, lambda: "fresh")
        assert realtime.result(timeout=1.0) == "fresh"
        release_history.set()
        history.result(timeout=1.0)
    finally:
        release_history.set()
        lanes.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)


def test_dedicated_history_workers_do_not_consume_realtime_source_workers() -> None:
    source_pool = BoundedExecutor(worker_count=2, queue_capacity=2, thread_name_prefix="source-data")
    history_pool = BoundedExecutor(worker_count=2, queue_capacity=2, thread_name_prefix="history-data")
    lanes = SourceLaneRegistry(source_pool)
    history_started = threading.Event()
    release_history = threading.Event()

    class BlockingRemoteHistory:
        @staticmethod
        def fetch_history(_code, *, days):
            assert days == 90
            history_started.set()
            assert release_history.wait(1.0)
            return _history_bars()

    service = MarketFeatureService(
        StaticGateway((_quote(),)),
        BlockingRemoteHistory(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
        worker_pool=source_pool,
        history_worker_pool=history_pool,
        source_lanes=lanes,
        history_warmup_batch_size=1,
        wall_clock=lambda: NOW,
    )
    source_pool.start()
    history_pool.start()
    try:
        service.fetch_market_features(NOW, deadline=NOW + timedelta(seconds=1))
        assert history_started.wait(1.0)
        realtime = lanes.submit("eastmoney", "realtime-during-history", NOW, lambda: "fresh")
        assert realtime.result(timeout=0.2) == "fresh"
    finally:
        release_history.set()
        lanes.stop(wait=True, timeout_seconds=1.0)
        source_pool.stop(wait=True, cancel_futures=True)
        history_pool.stop(wait=True, cancel_futures=True)


def test_gateway_starts_eastmoney_and_sina_together_on_shared_source_pool() -> None:
    both_started = threading.Barrier(3)
    release = threading.Event()
    eastmoney = CoordinatedMarketClient((replace(_quote(), source="eastmoney"),), both_started, release)
    sina = CoordinatedMarketClient((replace(_quote(), source="sina", price=12.01),), both_started, release)
    pool = BoundedExecutor(worker_count=5, queue_capacity=5, thread_name_prefix="source-data")
    pool.start()
    lanes = SourceLaneRegistry(pool)
    gateway = MarketDataGateway(
        eastmoney,
        sina,
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        worker_pool=pool,
        source_lanes=lanes,
        wall_clock=lambda: NOW,
    )
    result: list[tuple[MarketQuote, ...]] = []
    caller = threading.Thread(target=lambda: result.append(tuple(gateway.fetch_market(observed_at=NOW))))

    try:
        caller.start()
        both_started.wait(1.0)
        assert eastmoney.thread_name.startswith("source-data")
        assert sina.thread_name.startswith("source-data")
    finally:
        release.set()
        caller.join(1.0)
        lanes.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)

    assert result[0][0].price == 12.0
    assert eastmoney.calls == 1
    assert sina.calls == 1
    assert gateway.health()["merge_count"] == 1


def test_full_market_source_lane_deadline_returns_before_blocked_source_io() -> None:
    pool = BoundedExecutor(worker_count=5, queue_capacity=5, thread_name_prefix="source-data")
    lanes = SourceLaneRegistry(pool)
    eastmoney = BlockingMarketClient((replace(_quote(), source="eastmoney"),))
    gateway = MarketDataGateway(
        eastmoney,
        StaticMarketClient((replace(_quote(), source="sina"),)),
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        worker_pool=pool,
        source_lanes=lanes,
    )
    pool.start()
    observed_at = datetime.now(timezone.utc)
    deadline = observed_at + timedelta(seconds=0.01)
    release_timer = threading.Timer(0.6, eastmoney.release.set)
    release_timer.start()

    started = time.monotonic()
    try:
        with pytest.raises(MarketDataDeadlineExceeded, match="deadline"):
            gateway.fetch_market(observed_at=observed_at, deadline=deadline)
        elapsed = time.monotonic() - started
    finally:
        eastmoney.release.set()
        release_timer.cancel()
        lanes.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)

    assert elapsed < 0.5
    assert gateway.canonical_snapshot() is None
    source_health = gateway.health()["sources"]["eastmoney"]
    assert source_health["success_count"] == 0
    assert source_health["error_count"] == 1
    assert source_health["timeout_count"] == 1


def test_candidate_source_lane_deadline_returns_baseline_and_discards_late_quote() -> None:
    pool = BoundedExecutor(worker_count=5, queue_capacity=5, thread_name_prefix="source-data")
    lanes = SourceLaneRegistry(pool)
    tencent = BlockingTencentClient((replace(_quote(), source="tencent", price=12.5),))
    gateway = MarketDataGateway(
        StaticMarketClient((replace(_quote(), source="eastmoney"),)),
        StaticMarketClient((replace(_quote(), source="sina"),)),
        tencent,
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        worker_pool=pool,
        source_lanes=lanes,
    )
    pool.start()
    observed_at = datetime.now(timezone.utc)
    gateway.fetch_market(observed_at=observed_at)
    baseline_observed_at = gateway.canonical_snapshot().observed_at
    deadline = datetime.now(timezone.utc) + timedelta(seconds=0.01)
    release_timer = threading.Timer(0.6, tencent.release.set)
    release_timer.start()

    started = time.monotonic()
    try:
        result = gateway.fetch_candidates(("600001",), observed_at=observed_at, deadline=deadline)
        elapsed = time.monotonic() - started
    finally:
        tencent.release.set()
        release_timer.cancel()
        lanes.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)

    assert elapsed < 0.5
    assert result[0].price == 12.0
    assert gateway.canonical_snapshot().quotes[0].price == 12.0
    assert gateway.canonical_snapshot().observed_at == baseline_observed_at


def test_gateway_full_market_cache_avoids_duplicate_physical_requests_and_reports_hits() -> None:
    runtime = load_runtime_settings(Path(__file__).parents[2] / "config" / "v2" / "runtime.json")
    cache: BoundedLruCache[object] = BoundedLruCache(
        runtime.market_data.cache_policy,
        cadence_seconds=runtime.pipeline.cadence_seconds,
        wall_clock=lambda: NOW,
    )
    eastmoney = CountingMarketClient((replace(_quote(), source="eastmoney"),))
    sina = CountingMarketClient((replace(_quote(), source="sina", price=12.01),))
    gateway = MarketDataGateway(
        eastmoney,
        sina,
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        cache=cache,
        source_contract_versions=runtime.market_data.source_contract_versions,
        config_version=runtime.config_version,
        wall_clock=lambda: NOW,
    )

    first = tuple(gateway.fetch_market(observed_at=NOW))
    second = tuple(gateway.fetch_market(observed_at=NOW))

    assert first == second
    assert eastmoney.calls == 1
    assert sina.calls == 1
    status = cache.status()["full_market_quotes"]
    assert status["eastmoney"]["hit"] == 1
    assert status["sina"]["hit"] == 1
    assert status["eastmoney"]["entries"] == 1


def test_source_lane_returns_due_market_cache_before_background_refresh_completes() -> None:
    runtime = load_runtime_settings(Path(__file__).parents[2] / "config" / "v2" / "runtime.json")
    monotonic = MutableMonotonic()
    cache: BoundedLruCache[object] = BoundedLruCache(
        runtime.market_data.cache_policy,
        cadence_seconds=runtime.pipeline.cadence_seconds,
        monotonic=monotonic,
        wall_clock=lambda: NOW,
    )
    pool = BoundedExecutor(worker_count=5, queue_capacity=5, thread_name_prefix="source-data")
    lanes = SourceLaneRegistry(pool)

    class BlockingRefreshClient:
        def __init__(self, source: str) -> None:
            self.source = source
            self.calls = 0
            self.refresh_started = threading.Event()
            self.release_refresh = threading.Event()

        def fetch_market(self):
            self.calls += 1
            if self.calls > 1:
                self.refresh_started.set()
                assert self.release_refresh.wait(1.0)
            return (replace(_quote(), source=self.source),)

    eastmoney = BlockingRefreshClient("eastmoney")
    sina = BlockingRefreshClient("sina")
    gateway = MarketDataGateway(
        eastmoney,
        sina,
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        worker_pool=pool,
        source_lanes=lanes,
        cache=cache,
        source_contract_versions=runtime.market_data.source_contract_versions,
        config_version=runtime.config_version,
        monotonic=monotonic,
        wall_clock=lambda: NOW,
    )
    pool.start()
    completed = threading.Event()
    results: list[tuple[MarketQuote, ...]] = []
    errors: list[BaseException] = []

    def fetch_again() -> None:
        try:
            results.append(tuple(gateway.fetch_market(observed_at=NOW)))
        except BaseException as exc:
            errors.append(exc)
        finally:
            completed.set()

    try:
        gateway.fetch_market(observed_at=NOW)
        monotonic.value = 30.001
        caller = threading.Thread(target=fetch_again)
        caller.start()
        assert eastmoney.refresh_started.wait(1.0)
        assert sina.refresh_started.wait(1.0)
        assert completed.wait(0.2)
    finally:
        eastmoney.release_refresh.set()
        sina.release_refresh.set()
        if "caller" in locals():
            caller.join(1.0)
        lanes.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)
        cache.stop()

    assert errors == []
    assert results[0][0].price == 12.0
    assert eastmoney.calls == 2
    assert sina.calls == 2
    assert lanes.status()["eastmoney"]["superseded_count"] == 0
    assert lanes.status()["sina"]["superseded_count"] == 0


def test_equal_quote_version_can_gain_new_tushare_board_metadata_from_cache_hit() -> None:
    runtime = load_runtime_settings(Path(__file__).parents[2] / "config" / "v2" / "runtime.json")
    cache: BoundedLruCache[object] = BoundedLruCache(
        runtime.market_data.cache_policy,
        cadence_seconds=runtime.pipeline.cadence_seconds,
        wall_clock=lambda: NOW,
    )
    quote = replace(_quote(), source="eastmoney")
    gateway = MarketDataGateway(
        StaticMarketClient((quote,)),
        StaticMarketClient((replace(quote, source="sina"),)),
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        cache=cache,
        source_contract_versions=runtime.market_data.source_contract_versions,
        config_version=runtime.config_version,
        wall_clock=lambda: NOW,
    )
    first = gateway.fetch_market(observed_at=NOW)
    master = SourceObservation(
        source="tushare",
        subject_key="600001",
        observed_at=NOW,
        source_time=NOW,
        received_at=NOW,
        effective_at=NOW - timedelta(days=1),
        data_version="master-v1",
        fields={
            "board": "main",
            "exchange": "SSE",
            "listing_date": "2020-01-02",
            "listing_age_sessions": 1000.0,
            "has_price_limit": True,
            "exchange_limit_pct": 10.0,
        },
        missing_reasons={},
        payload_hash="master-v1",
        status="success",
        error_code=None,
    )
    gateway.update_reference_observations((master,))

    second = gateway.fetch_market(observed_at=NOW)

    assert first[0].board_source == "code_prefix_fallback"
    assert second[0].board_source == "tushare"
    assert second[0].board_reliability == "verified"
    assert second[0].execution_restrictions == ()


def test_degraded_candidate_cache_is_observe_only_without_rewriting_source_time() -> None:
    runtime = load_runtime_settings(Path(__file__).parents[2] / "config" / "v2" / "runtime.json")
    monotonic = MutableMonotonic()

    def wall_clock() -> datetime:
        return NOW + timedelta(seconds=monotonic.value)

    cache: BoundedLruCache[object] = BoundedLruCache(
        runtime.market_data.cache_policy,
        cadence_seconds=runtime.pipeline.cadence_seconds,
        monotonic=monotonic,
        wall_clock=wall_clock,
    )
    quote = replace(_quote(), source="eastmoney")
    gateway = MarketDataGateway(
        StaticMarketClient((quote,)),
        StaticMarketClient((replace(quote, source="sina"),)),
        StaticTencentClient((replace(quote, source="tencent"),)),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        cache=cache,
        source_contract_versions=runtime.market_data.source_contract_versions,
        config_version=runtime.config_version,
        monotonic=monotonic,
        wall_clock=wall_clock,
    )
    gateway.fetch_market(observed_at=NOW)
    gateway.fetch_candidates(("600001",), observed_at=NOW)
    monotonic.value = 15.001

    result = gateway.fetch_candidates(("600001",), observed_at=wall_clock())

    snapshot = gateway.canonical_snapshot()
    assert result[0].source_time == NOW
    assert "market_data_degraded" in result[0].execution_restrictions
    assert snapshot is not None
    assert "tencent:cache_degraded" in snapshot.degraded_reasons


def test_gateway_negative_refresh_keeps_failure_degradation_with_last_valid_value() -> None:
    runtime = load_runtime_settings(Path(__file__).parents[2] / "config" / "v2" / "runtime.json")
    cache: BoundedLruCache[object] = BoundedLruCache(
        runtime.market_data.cache_policy,
        cadence_seconds=runtime.pipeline.cadence_seconds,
        wall_clock=lambda: NOW,
    )

    class ToggleMarketClient:
        fail = False

        def fetch_market(self):
            if self.fail:
                raise RuntimeError("offline")
            return (replace(_quote(), source="eastmoney"),)

    eastmoney = ToggleMarketClient()
    gateway = MarketDataGateway(
        eastmoney,
        StaticMarketClient((replace(_quote(), source="sina"),)),
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        cache=cache,
        source_contract_versions=runtime.market_data.source_contract_versions,
        config_version=runtime.config_version,
        wall_clock=lambda: NOW,
    )

    gateway.fetch_market(observed_at=NOW)
    eastmoney.fail = True
    gateway.fetch_market(observed_at=NOW, force=True)
    gateway.fetch_market(observed_at=NOW)

    snapshot = gateway.canonical_snapshot()
    assert snapshot is not None
    assert "eastmoney:source_failed" in snapshot.degraded_reasons


def test_gateway_background_refresh_failure_uses_negative_cache_to_suppress_retries() -> None:
    runtime = load_runtime_settings(Path(__file__).parents[2] / "config" / "v2" / "runtime.json")
    monotonic = MutableMonotonic()
    cache: BoundedLruCache[object] = BoundedLruCache(
        runtime.market_data.cache_policy,
        cadence_seconds=runtime.pipeline.cadence_seconds,
        monotonic=monotonic,
        wall_clock=lambda: NOW,
    )
    pool = BoundedExecutor(worker_count=5, queue_capacity=5, thread_name_prefix="source-data")

    class ToggleMarketClient:
        fail = False

        def __init__(self, source: str) -> None:
            self.source = source
            self.calls = 0
            self.failed = threading.Event()

        def fetch_market(self):
            self.calls += 1
            if self.fail:
                self.failed.set()
                raise RuntimeError("offline")
            return (replace(_quote(), source=self.source),)

    eastmoney = ToggleMarketClient("eastmoney")
    sina = ToggleMarketClient("sina")
    gateway = MarketDataGateway(
        eastmoney,
        sina,
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        worker_pool=pool,
        cache=cache,
        source_contract_versions=runtime.market_data.source_contract_versions,
        config_version=runtime.config_version,
        monotonic=monotonic,
        wall_clock=lambda: NOW,
    )
    pool.start()

    try:
        gateway.fetch_market(observed_at=NOW)
        monotonic.value = 30.001
        eastmoney.fail = True
        gateway.fetch_market(observed_at=NOW)
        assert eastmoney.failed.wait(1.0)
        time.sleep(0.02)

        gateway.fetch_market(observed_at=NOW)
        time.sleep(0.02)
    finally:
        pool.stop(wait=True, cancel_futures=True)
        cache.stop()

    assert eastmoney.calls == 2
    snapshot = gateway.canonical_snapshot()
    assert snapshot is not None
    assert "eastmoney:source_failed" in snapshot.degraded_reasons


def test_targeted_quote_overlay_updates_canonical_value_and_field_attribution() -> None:
    eastmoney = replace(_quote(), source="eastmoney", price=12.0, speed=None, data_version="z-east-v1")
    sina = replace(_quote(), source="sina", price=12.01, speed=0.7, data_version="sina-v1")
    tencent = replace(_quote(), source="tencent", price=12.02, speed=None, data_version="a-tencent-v1")
    gateway = MarketDataGateway(
        StaticMarketClient((eastmoney,)),
        StaticMarketClient((sina,)),
        StaticTencentClient((tencent,)),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        wall_clock=lambda: NOW,
    )

    gateway.fetch_market(observed_at=NOW)
    gateway.fetch_candidates(("600001",), observed_at=NOW)

    snapshot = gateway.canonical_snapshot()
    assert snapshot is not None
    assert snapshot.quotes[0].price == 12.02
    assert snapshot.quotes[0].speed == 0.7
    assert snapshot.field_sources["600001"]["price"] == "tencent"
    assert snapshot.field_sources["600001"]["speed"] == "sina"
    assert snapshot.source_versions["tencent"] == "a-tencent-v1"


def test_candidate_feature_service_keeps_tencent_priority_before_cross_vendor_version_text() -> None:
    eastmoney = replace(_quote(), source="eastmoney", price=12.0, data_version="z-east-v1")
    sina = replace(_quote(), source="sina", price=12.01, data_version="z-sina-v1")
    tencent = replace(_quote(), source="tencent", price=12.02, data_version="a-tencent-v1")
    gateway = MarketDataGateway(
        StaticMarketClient((eastmoney,)),
        StaticMarketClient((sina,)),
        StaticTencentClient((tencent,)),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        wall_clock=lambda: NOW,
    )
    service = MarketFeatureService(
        gateway,
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
        wall_clock=lambda: NOW,
    )
    service.fetch_market_features(NOW)

    refreshed = tuple(service.refresh_candidate_quotes(("600001",), NOW))

    assert refreshed[0].quote.source == "tencent"
    assert refreshed[0].quote.price == 12.02
    assert refreshed[0].quote.data_version == "a-tencent-v1"


def test_full_market_commit_preserves_candidate_overlay_published_during_merge(monkeypatch) -> None:
    seed = replace(_quote(), price=12.0, data_version="seed-v1")
    full_refresh = replace(
        _quote(),
        price=12.1,
        source_time=NOW + timedelta(seconds=1),
        received_time=NOW + timedelta(seconds=1),
        data_version="full-v2",
    )
    candidate = replace(
        _quote(),
        price=12.2,
        source_time=NOW + timedelta(seconds=2),
        received_time=NOW + timedelta(seconds=2),
        data_version="candidate-v2",
    )
    gateway = MarketDataGateway(
        SequenceMarketClient(((seed,), (full_refresh,))),
        SequenceMarketClient(((seed,), (full_refresh,))),
        StaticTencentClient((candidate,)),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        wall_clock=lambda: NOW + timedelta(seconds=3),
    )
    gateway.fetch_market(observed_at=NOW)

    original_merge = gateway_module.merge_market_observations
    merge_started = threading.Event()
    release_merge = threading.Event()

    def coordinated_merge(*args, **kwargs):
        if threading.current_thread().name == "full-market-refresh":
            merge_started.set()
            assert release_merge.wait(1.0)
        return original_merge(*args, **kwargs)

    monkeypatch.setattr(gateway_module, "merge_market_observations", coordinated_merge)
    errors: list[BaseException] = []

    def refresh_market() -> None:
        try:
            gateway.fetch_market(observed_at=NOW + timedelta(seconds=1))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=refresh_market, name="full-market-refresh")
    thread.start()
    try:
        assert merge_started.wait(1.0)
        refreshed = tuple(gateway.fetch_candidates(("600001",), observed_at=NOW + timedelta(seconds=2)))
        assert refreshed[0].price == 12.2
    finally:
        release_merge.set()
        thread.join(1.0)

    assert not thread.is_alive()
    assert errors == []
    snapshot = gateway.canonical_snapshot()
    assert snapshot is not None
    assert snapshot.quotes[0].price == 12.2
    assert snapshot.quotes[0].source == "tencent"
    assert snapshot.source_versions["eastmoney"] == "full-v2"
    assert snapshot.source_versions["sina"] == "full-v2"
    assert snapshot.source_versions["tencent"] == "candidate-v2"


def test_newer_reference_refresh_can_correct_an_older_effective_listing_date() -> None:
    gateway = MarketDataGateway(
        StaticMarketClient((replace(_quote(), source="eastmoney"),)),
        StaticMarketClient((replace(_quote(), source="sina"),)),
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        wall_clock=lambda: NOW,
    )

    def master(listing_date: str, source_time: datetime, version: str) -> SourceObservation:
        return SourceObservation(
            source="tushare",
            subject_key="600001",
            observed_at=source_time,
            source_time=source_time,
            received_at=source_time,
            effective_at=datetime.fromisoformat(f"{listing_date}T00:00:00+08:00"),
            data_version=version,
            fields={"board": "main", "listing_date": listing_date},
            missing_reasons={},
            payload_hash=version,
            status="success",
            error_code=None,
        )

    gateway.update_reference_observations((master("2020-01-02", NOW - timedelta(seconds=1), "master-v1"),))
    gateway.update_reference_observations((master("2019-01-02", NOW, "master-v2"),))
    gateway.fetch_market(observed_at=NOW)

    snapshot = gateway.canonical_snapshot()
    assert snapshot is not None
    assert snapshot.quotes[0].listing_date == date(2019, 1, 2)


def test_listing_session_projection_reuses_a_sorted_calendar_index() -> None:
    gateway = MarketDataGateway(
        StaticMarketClient((replace(_quote(), source="eastmoney"),)),
        StaticMarketClient((replace(_quote(), source="sina"),)),
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        wall_clock=lambda: NOW,
    )

    def calendar(day: date) -> SourceObservation:
        effective_at = datetime.combine(day, datetime.min.time(), tzinfo=NOW.tzinfo)
        return SourceObservation(
            source="tushare",
            subject_key=day.isoformat(),
            observed_at=NOW,
            source_time=NOW,
            received_at=NOW,
            effective_at=effective_at,
            data_version="calendar-v1",
            fields={"calendar_date": day.isoformat(), "is_open": True},
            missing_reasons={},
            payload_hash=day.isoformat(),
            status="success",
            error_code=None,
        )

    gateway.update_reference_observations((calendar(date(2020, 1, 2)), calendar(date(2026, 7, 16))))

    class NoIterationOpenDates(set[date]):
        def __iter__(self):
            raise AssertionError("listing-age projection must not rescan every calendar date per security")

    gateway._calendar_open_dates = NoIterationOpenDates((date(2020, 1, 2), date(2026, 7, 16)))
    master = SourceObservation(
        source="tushare",
        subject_key="600001",
        observed_at=NOW,
        source_time=NOW,
        received_at=NOW,
        effective_at=datetime.fromisoformat("2020-01-02T00:00:00+08:00"),
        data_version="master-v1",
        fields={"board": "main", "listing_date": "2020-01-02"},
        missing_reasons={},
        payload_hash="master-v1",
        status="success",
        error_code=None,
    )

    gateway.update_reference_observations((master,))
    quote = gateway.fetch_market(observed_at=NOW)[0]

    assert quote.listing_age_sessions == 2


def test_tushare_reference_version_uses_response_time_before_hash_order() -> None:
    service = MarketFeatureService(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
    )
    newer = SourceObservation(
        source="tushare",
        subject_key="600001",
        observed_at=NOW,
        source_time=NOW,
        received_at=NOW,
        effective_at=NOW,
        data_version="a-newer",
        fields={"pe": 12.0},
        missing_reasons={},
        payload_hash="newer",
        status="success",
        error_code=None,
    )
    older = replace(
        newer,
        observed_at=NOW - timedelta(seconds=1),
        source_time=NOW - timedelta(seconds=1),
        received_at=NOW - timedelta(seconds=1),
        effective_at=NOW - timedelta(seconds=1),
        data_version="z-older",
        payload_hash="older",
    )

    service._apply_tushare_fields("valuation", (newer,))
    service._apply_tushare_fields("valuation", (older,))

    assert service._tushare_reference_versions["valuation"] == "a-newer"


def test_snapshot_metadata_copies_tushare_versions_under_service_lock() -> None:
    quote = _quote()
    gateway = MarketDataGateway(
        StaticMarketClient((quote,)),
        StaticMarketClient((replace(quote, source="sina"),)),
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        wall_clock=lambda: NOW,
    )
    gateway.fetch_market(observed_at=NOW)
    service = MarketFeatureService(
        gateway,
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
        wall_clock=lambda: NOW,
    )

    class TrackingLock:
        held = False

        def __enter__(self):
            self.held = True
            return self

        def __exit__(self, _exc_type, _exc, _traceback):
            self.held = False

    class LockCheckedVersions(Mapping[str, str]):
        def __init__(self, lock: TrackingLock) -> None:
            self._lock = lock
            self._values = {"valuation": "valuation-v1"}

        def __getitem__(self, key: str) -> str:
            return self._values[key]

        def __iter__(self) -> Iterator[str]:
            assert self._lock.held
            return iter(self._values)

        def __len__(self) -> int:
            return len(self._values)

    tracking_lock = TrackingLock()
    service._lock = tracking_lock
    service._tushare_reference_versions = LockCheckedVersions(tracking_lock)

    metadata = service.snapshot_metadata()

    assert metadata["tushare_reference_versions"] == {"valuation": "valuation-v1"}


def test_final_refresh_bypasses_fresh_cache_but_remains_single_flight() -> None:
    runtime = load_runtime_settings(Path(__file__).parents[2] / "config" / "v2" / "runtime.json")
    final_at = AFTERNOON - timedelta(milliseconds=100)
    cache: BoundedLruCache[object] = BoundedLruCache(
        runtime.market_data.cache_policy,
        cadence_seconds=runtime.pipeline.cadence_seconds,
        wall_clock=lambda: final_at,
    )
    eastmoney = BlockingMarketClient(
        (replace(_quote(), source="eastmoney", source_time=final_at, received_time=final_at),)
    )
    sina = CountingMarketClient((replace(_quote(), source="sina", source_time=final_at, received_time=final_at),))
    gateway = MarketDataGateway(
        eastmoney,
        sina,
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        cache=cache,
        source_contract_versions=runtime.market_data.source_contract_versions,
        config_version=runtime.config_version,
        wall_clock=lambda: final_at,
    )
    eastmoney.release.set()
    assert gateway.fetch_market(observed_at=final_at)
    eastmoney.release.clear()
    eastmoney.started.clear()
    results: list[tuple[MarketQuote, ...]] = []
    threads = [
        threading.Thread(
            target=lambda: results.append(
                tuple(
                    gateway.fetch_market(
                        observed_at=final_at,
                        force=True,
                        deadline=AFTERNOON,
                    )
                )
            )
        )
        for _ in range(2)
    ]

    try:
        for thread in threads:
            thread.start()
        assert eastmoney.started.wait(1.0)
        time.sleep(0.02)
    finally:
        eastmoney.release.set()
        for thread in threads:
            thread.join(1.0)

    assert len(results) == 2
    assert eastmoney.calls == 2
    assert sina.calls == 2


def test_tushare_missing_token_is_explicit_degradation_without_sdk_import() -> None:
    imported = False

    def sdk_factory(_token: str, _timeout: float):
        nonlocal imported
        imported = True
        raise AssertionError("SDK must not be created without a token")

    client = TushareClient(token="", timeout_seconds=8, sdk_factory=sdk_factory)

    observations = client.fetch_security_master(NOW)

    assert imported is False
    assert len(observations) == 1
    assert observations[0].status == "failed"
    assert observations[0].error_code == "missing_token"
    assert client.health()["degraded_reason"] == "missing_token"
    assert client.health()["consecutive_failures"] == 1


def test_tushare_security_master_is_structured_and_uses_eight_second_transport_timeout() -> None:
    factory_args: list[tuple[str, float]] = []
    pro = FakeTusharePro(
        [
            {
                "ts_code": "600001.SH",
                "symbol": "600001",
                "name": "测试股份",
                "area": "上海",
                "industry": "工业",
                "market": "主板",
                "exchange": "SSE",
                "list_status": "L",
                "list_date": "20200102",
            }
        ]
    )

    def sdk_factory(token: str, timeout: float):
        factory_args.append((token, timeout))
        return pro

    client = TushareClient(token="secret-token", timeout_seconds=8, sdk_factory=sdk_factory)

    observations = client.fetch_security_master(NOW)

    assert factory_args == [("secret-token", 8.0)]
    assert pro.calls[0][0] == "stock_basic"
    assert observations[0].status == "success"
    assert observations[0].subject_key == "600001"
    assert observations[0].fields["board"] == "main"
    assert observations[0].fields["exchange"] == "SSE"
    assert observations[0].fields["listing_date"] == "2020-01-02"
    assert observations[0].fields.get("is_relisted_first_session") is None
    assert observations[0].missing_reasons["is_relisted_first_session"] == "source_field_unavailable"
    assert "secret-token" not in repr(observations)


def test_tushare_security_master_keeps_out_of_scope_exchange_unsupported() -> None:
    pro = FakeTusharePro(
        [
            {
                "ts_code": "830001.BJ",
                "symbol": "830001",
                "name": "范围外证券",
                "market": "北交所",
                "exchange": "BSE",
                "list_status": "L",
                "list_date": "20200102",
            }
        ]
    )
    client = TushareClient(
        token="secret-token",
        timeout_seconds=8,
        sdk_factory=lambda _token, _timeout: pro,
    )

    observations = client.fetch_security_master(NOW)

    assert observations[0].fields["board"] == "unsupported"


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (ModuleNotFoundError("No module named 'tushare'"), "sdk_not_installed"),
        (PermissionError("permission denied"), "permission_denied"),
        (RuntimeError("429 quota exceeded"), "quota_or_rate_limit"),
        (TimeoutError("transport timed out"), "timeout"),
        (RuntimeError("SDK protocol failed"), "sdk_error"),
    ],
)
def test_tushare_sdk_failures_are_structured_degradations(error, expected_code) -> None:
    def sdk_factory(_token: str, _timeout: float):
        raise error

    client = TushareClient(token="secret-token", timeout_seconds=8, sdk_factory=sdk_factory)

    observations = client.fetch_security_master(NOW)

    assert observations[0].status == "failed"
    assert observations[0].error_code == expected_code
    assert client.health()["degraded_reason"] == expected_code
    assert client.health()["consecutive_failures"] == 1


def test_tushare_circuit_opens_after_three_failures_and_recovers_with_one_probe() -> None:
    monotonic = MutableMonotonic()
    factory_calls = 0
    should_fail = True

    def sdk_factory(_token: str, _timeout: float):
        nonlocal factory_calls
        factory_calls += 1
        if should_fail:
            raise TimeoutError("transport timed out")
        return FakeTusharePro(
            [
                {
                    "ts_code": "600001.SH",
                    "symbol": "600001",
                    "name": "测试股份",
                    "market": "主板",
                    "exchange": "SSE",
                    "list_status": "L",
                    "list_date": "20200102",
                }
            ]
        )

    client = TushareClient(
        token="secret-token",
        timeout_seconds=8,
        sdk_factory=sdk_factory,
        monotonic=monotonic,
        wall_clock=lambda: NOW,
    )

    for _index in range(3):
        assert client.fetch_security_master(NOW)[0].error_code == "timeout"
    assert client.health()["circuit_open"] is True
    assert client.fetch_security_master(NOW)[0].error_code == "circuit_open"
    assert factory_calls == 3

    monotonic.value = 60.001
    should_fail = False
    recovered = client.fetch_security_master(NOW)

    assert recovered[0].status == "success"
    assert factory_calls == 4
    assert client.health()["circuit_open"] is False
    assert client.health()["consecutive_failures"] == 0


def test_tushare_per_code_batch_stops_before_next_sdk_call_during_lane_shutdown() -> None:
    pool = BoundedExecutor(worker_count=5, queue_capacity=5, thread_name_prefix="source-data")
    lanes = SourceLaneRegistry(pool)

    class BlockingValuationPro:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.started = threading.Event()
            self.release = threading.Event()

        def daily_basic(self, **kwargs):
            self.calls.append(str(kwargs["ts_code"]))
            self.started.set()
            assert self.release.wait(1.0)
            return FakeTushareFrame([{"ts_code": kwargs["ts_code"], "trade_date": "20260715", "pe": 12.0}])

    pro = BlockingValuationPro()
    client = TushareClient(
        token="secret-token",
        timeout_seconds=8,
        sdk_factory=lambda _token, _timeout: pro,
        cancel_requested=lambda: lanes.is_stopped("tushare"),
        wall_clock=lambda: NOW,
    )
    pool.start()
    future = lanes.submit(
        "tushare",
        "valuation-batch",
        NOW,
        client.fetch_daily_valuations,
        ("600001", "600002"),
        date(2026, 7, 15),
        NOW,
    )

    try:
        assert pro.started.wait(1.0)
        lanes.stop(wait=False)
        pro.release.set()
        observations = future.result(timeout=1.0)
    finally:
        pro.release.set()
        lanes.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)

    assert pro.calls == ["600001.SH"]
    assert observations[0].status == "failed"
    assert observations[0].error_code == "stopped"
    assert lanes.status()["tushare"]["running"] is False


def test_tushare_per_code_batch_keeps_successes_when_one_code_fails() -> None:
    class PartiallyFailingPro:
        def pro_bar(self, **kwargs):
            code = str(kwargs["ts_code"])
            if code == "600002.SH":
                raise TimeoutError("one code timed out")
            return FakeTushareFrame(
                [
                    {
                        "ts_code": code,
                        "trade_date": "20260715",
                        "open": 10.0,
                        "close": 10.5,
                        "high": 10.8,
                        "low": 9.9,
                        "vol": 1000.0,
                        "amount": 10500.0,
                        "pct_chg": 5.0,
                    }
                ]
            )

    client = TushareClient(
        token="secret-token",
        timeout_seconds=8,
        sdk_factory=lambda _token, _timeout: PartiallyFailingPro(),
        wall_clock=lambda: NOW,
    )

    observations = client.fetch_forward_adjusted_daily(
        ("600001", "600002", "600003"),
        date(2026, 7, 1),
        date(2026, 7, 16),
        NOW,
    )

    assert {item.subject_key for item in observations if item.status == "success"} == {"600001", "600003"}
    assert any(
        item.status == "failed" and item.subject_key == "600002" and item.error_code == "timeout"
        for item in observations
    )


def test_tushare_120_point_profile_batches_free_daily_and_disables_paid_references() -> None:
    class FreeDailyPro:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def daily(self, **kwargs):
            self.calls.append(kwargs)
            return FakeTushareFrame(
                [
                    {
                        "ts_code": code,
                        "trade_date": "20260715",
                        "open": 10.0,
                        "close": 10.5,
                        "high": 10.8,
                        "low": 9.9,
                        "vol": 1000.0,
                        "amount": 10500.0,
                        "pct_chg": 5.0,
                    }
                    for code in str(kwargs["ts_code"]).split(",")
                ]
            )

    pro = FreeDailyPro()
    client = TushareClient(
        token="secret-token",
        timeout_seconds=8,
        points=120,
        sdk_factory=lambda _token, _timeout: pro,
        wall_clock=lambda: NOW,
    )

    observations = client.fetch_daily_history(
        ("600001", "300001", "688001"),
        date(2026, 7, 1),
        date(2026, 7, 16),
        NOW,
    )

    assert {item.subject_key for item in observations} == {"600001", "300001", "688001"}
    assert len(pro.calls) == 1
    assert pro.calls[0]["ts_code"] == "600001.SH,300001.SZ,688001.SH"
    assert client.supports("daily_history") is True
    assert client.supports("security_master") is False
    assert client.supports("trading_calendar") is False
    assert client.supports("daily_valuation") is False
    assert client.supports("financial_indicators") is False
    assert client.health()["access_points"] == 120
    assert client.health()["history_mode"] == "unadjusted_daily"


def test_tushare_default_daily_transport_uses_direct_https_without_environment_proxy(monkeypatch) -> None:
    class FakeResponse:
        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json():
            return {
                "code": 0,
                "data": {
                    "fields": ["ts_code", "trade_date", "open", "high", "low", "close", "pct_chg", "vol", "amount"],
                    "items": [["600001.SH", "20260715", 10.0, 10.8, 9.9, 10.5, 5.0, 1000.0, 10500.0]],
                },
            }

    class FakeSession:
        def __init__(self) -> None:
            self.trust_env = True
            self.calls: list[tuple[str, dict[str, object]]] = []

        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return FakeResponse()

    session = FakeSession()
    module = type("FakeTushareModule", (), {"pro_api": staticmethod(lambda _token, timeout: object())})()
    monkeypatch.setattr(tushare_support_module.requests, "Session", lambda: session)
    monkeypatch.setattr(tushare_support_module.importlib, "import_module", lambda _name: module)
    client = TushareClient(token="secret-token", points=120, timeout_seconds=8, wall_clock=lambda: NOW)

    observations = client.fetch_daily_history(
        ("600001",),
        date(2026, 7, 1),
        date(2026, 7, 16),
        NOW,
    )

    assert [item.subject_key for item in observations] == ["600001"]
    assert session.trust_env is False
    assert session.calls[0][0] == "https://api.tushare.pro"
    assert session.calls[0][1]["json"]["api_name"] == "daily"
    assert session.calls[0][1]["timeout"] == 8


def test_tushare_date_only_financial_records_become_effective_at_shanghai_day_end() -> None:
    class FinancialPro:
        def fina_indicator(self, **_kwargs):
            return FakeTushareFrame(
                [
                    {
                        "ts_code": "600001.SH",
                        "ann_date": "20260716",
                        "end_date": "20260630",
                        "eps": 1.0,
                    }
                ]
            )

    client = TushareClient(
        token="secret-token",
        timeout_seconds=8,
        sdk_factory=lambda _token, _timeout: FinancialPro(),
        wall_clock=lambda: NOW,
    )

    observations = client.fetch_financial_indicators(("600001",), NOW)

    assert len(observations) == 1
    assert observations[0].effective_at.isoformat() == "2026-07-16T23:59:59+08:00"


def test_reference_refresh_reuses_cache_and_refreshes_due_entries_inside_tushare_lane() -> None:
    runtime = load_runtime_settings(Path(__file__).parents[2] / "config" / "v2" / "runtime.json")
    monotonic = MutableMonotonic()
    cache: BoundedLruCache[object] = BoundedLruCache(
        runtime.market_data.cache_policy,
        cadence_seconds=runtime.pipeline.cadence_seconds,
        monotonic=monotonic,
        wall_clock=lambda: NOW,
    )
    pool = BoundedExecutor(worker_count=5, queue_capacity=5, thread_name_prefix="source-data")
    lanes = SourceLaneRegistry(pool)

    class ReferencePro(FakeTusharePro):
        def trade_cal(self, **kwargs):
            self.calls.append(("trade_cal", kwargs))
            return FakeTushareFrame(
                [{"exchange": "SSE", "cal_date": "20260716", "is_open": 1, "pretrade_date": "20260715"}]
            )

    pro = ReferencePro(
        [
            {
                "ts_code": "600001.SH",
                "symbol": "600001",
                "name": "测试股份",
                "industry": "工业",
                "market": "主板",
                "exchange": "SSE",
                "list_status": "L",
                "list_date": "20200102",
            }
        ]
    )
    quote = _quote()
    gateway = MarketDataGateway(
        StaticMarketClient((quote,)),
        StaticMarketClient((quote,)),
        StaticTencentClient((quote,)),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        worker_pool=pool,
        source_lanes=lanes,
        cache=cache,
        source_contract_versions=runtime.market_data.source_contract_versions,
        config_version=runtime.config_version,
        monotonic=monotonic,
        wall_clock=lambda: NOW,
    )
    service = MarketFeatureService(
        gateway,
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
        tushare_client=TushareClient(
            token="secret-token",
            timeout_seconds=8,
            sdk_factory=lambda _token, _timeout: pro,
            wall_clock=lambda: NOW,
        ),
        worker_pool=pool,
        source_lanes=lanes,
        cache=cache,
        source_contract_versions=runtime.market_data.source_contract_versions,
        config_version=runtime.config_version,
        wall_clock=lambda: NOW,
    )
    pool.start()

    try:
        lanes.submit("tushare", "reference-cycle-1", NOW, service.refresh_reference_data, (), NOW).result()
        lanes.submit("tushare", "reference-cycle-2", NOW, service.refresh_reference_data, (), NOW).result()
        monotonic.value = 86400.001
        lanes.submit("tushare", "reference-cycle-3", NOW, service.refresh_reference_data, (), NOW).result()
    finally:
        lanes.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)
        cache.stop()

    assert [name for name, _arguments in pro.calls] == ["stock_basic", "trade_cal", "stock_basic", "trade_cal"]
    status = cache.status()["security_master_calendar"]["tushare"]
    assert status["entries"] == 2
    assert status["hit"] == 4
    assert lanes.status()["tushare"]["superseded_count"] == 0


def test_akshare_circuit_skips_excess_requests_and_recovers_with_one_probe() -> None:
    monotonic = MutableMonotonic()
    pool = BoundedExecutor(worker_count=5, queue_capacity=5, thread_name_prefix="source-data")
    lanes = SourceLaneRegistry(pool)

    class ToggleResearchClient:
        calls = 0
        should_fail = True

        def fetch_news(self, code: str, *, observed_at: datetime):
            self.calls += 1
            if self.should_fail:
                raise RuntimeError("offline")
            return (Evidence(f"news:{code}", "news", "恢复", "fixture", observed_at),)

    research = ToggleResearchClient()
    service = MarketFeatureService(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
        research_client=research,
        research_workers=1,
        worker_pool=pool,
        source_lanes=lanes,
        monotonic=monotonic,
        wall_clock=lambda: NOW,
    )
    pool.start()

    try:
        lanes.submit(
            "akshare",
            "research-cycle-1",
            NOW,
            service._load_research,
            ("600001", "600002", "600003", "600004"),
            NOW,
            include_structured=False,
            force=True,
        ).result()
        lanes.submit(
            "akshare",
            "research-cycle-2",
            NOW,
            service._load_research,
            ("600005",),
            NOW,
            include_structured=False,
            force=True,
        ).result()
        assert research.calls == 3
        assert service.health()["sources"]["akshare"]["circuit_open"] is True

        monotonic.value = 60.001
        research.should_fail = False
        lanes.submit(
            "akshare",
            "research-cycle-3",
            NOW,
            service._load_research,
            ("600006",),
            NOW,
            include_structured=False,
            force=True,
        ).result()
    finally:
        lanes.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)

    assert research.calls == 4
    assert service.health()["sources"]["akshare"]["circuit_open"] is False
    assert service.health()["sources"]["akshare"]["consecutive_failures"] == 0


def test_tushare_negative_refresh_marks_preserved_reference_data_degraded() -> None:
    runtime = load_runtime_settings(Path(__file__).parents[2] / "config" / "v2" / "runtime.json")
    cache: BoundedLruCache[object] = BoundedLruCache(
        runtime.market_data.cache_policy,
        cadence_seconds=runtime.pipeline.cadence_seconds,
        wall_clock=lambda: NOW,
    )

    class ToggleReferencePro(FakeTusharePro):
        fail = False

        def stock_basic(self, **kwargs):
            if self.fail:
                raise TimeoutError("timed out")
            return super().stock_basic(**kwargs)

    pro = ToggleReferencePro(
        [
            {
                "ts_code": "600001.SH",
                "symbol": "600001",
                "name": "测试股份",
                "industry": "工业",
                "market": "主板",
                "exchange": "SSE",
                "list_status": "L",
                "list_date": "20200102",
            }
        ]
    )
    client = TushareClient(
        token="secret-token",
        timeout_seconds=8,
        sdk_factory=lambda _token, _timeout: pro,
        wall_clock=lambda: NOW,
    )
    service = MarketFeatureService(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
        tushare_client=client,
        cache=cache,
        source_contract_versions=runtime.market_data.source_contract_versions,
        config_version=runtime.config_version,
        wall_clock=lambda: NOW,
    )
    request = {"dataset": "security_master", "market": "ashare"}

    first = service._load_tushare_reference(
        "security_master_calendar",
        "security_master",
        request,
        NOW,
        client.fetch_security_master,
        NOW,
        force=False,
    )
    pro.fail = True
    service._load_tushare_reference(
        "security_master_calendar",
        "security_master",
        request,
        NOW,
        client.fetch_security_master,
        NOW,
        force=True,
    )
    preserved = service._load_tushare_reference(
        "security_master_calendar",
        "security_master",
        request,
        NOW,
        client.fetch_security_master,
        NOW,
        force=False,
    )

    assert "reference_data_degraded" not in first[0].fields
    assert preserved[0].fields["board_reliability"] == "degraded"
    assert preserved[0].fields["reference_data_degraded"] is True


def test_reference_degradation_replaces_same_version_verified_identity_conservatively() -> None:
    quote = replace(_quote(), source="eastmoney")
    gateway = MarketDataGateway(
        StaticMarketClient((quote,)),
        StaticMarketClient((replace(quote, source="sina"),)),
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        wall_clock=lambda: NOW,
    )
    fields = {
        "board": "main",
        "exchange": "SSE",
        "listing_date": "2020-01-02",
        "listing_age_sessions": 1000.0,
        "has_price_limit": True,
        "exchange_limit_pct": 10.0,
    }
    verified = SourceObservation(
        source="tushare",
        subject_key="600001",
        observed_at=NOW,
        source_time=NOW,
        received_at=NOW,
        effective_at=NOW,
        data_version="master-v1",
        fields=fields,
        missing_reasons={},
        payload_hash="z-verified",
        status="success",
        error_code=None,
    )
    degraded = replace(
        verified,
        fields={**fields, "board_reliability": "degraded", "reference_data_degraded": True},
        missing_reasons={"cache_refresh": "timeout"},
        payload_hash="a-degraded",
    )

    gateway.update_reference_observations((verified,))
    gateway.fetch_market(observed_at=NOW)
    gateway.update_reference_observations((degraded,))
    refreshed = gateway.fetch_market(observed_at=NOW)

    assert refreshed[0].board_reliability == "degraded"
    assert "board_identity_degraded" in refreshed[0].execution_restrictions


def test_auxiliary_cache_action_age_marks_new_features_observe_only() -> None:
    runtime = load_runtime_settings(Path(__file__).parents[2] / "config" / "v2" / "runtime.json")
    measured_at = AFTERNOON + timedelta(seconds=91)
    cache: BoundedLruCache[object] = BoundedLruCache(
        runtime.market_data.cache_policy,
        cadence_seconds=runtime.pipeline.cadence_seconds,
        wall_clock=lambda: measured_at,
    )
    quote = replace(_quote(), source_time=measured_at, received_time=measured_at)
    service = MarketFeatureService(
        StaticGateway((quote,)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
        cache=cache,
        source_contract_versions=runtime.market_data.source_contract_versions,
        config_version=runtime.config_version,
        wall_clock=lambda: measured_at,
    )
    service._candidate_quotes[quote.code] = quote
    history = _history_bars()
    intraday = tuple(
        replace(
            bar,
            source_time=bar.source_time - timedelta(seconds=91),
            received_time=bar.received_time - timedelta(seconds=91),
        )
        for bar in _tail_minute_bars()
    )
    research = ResearchObservation(
        evidence=(
            Evidence(
                "news-old",
                "news",
                "中标",
                "fixture",
                measured_at - timedelta(seconds=1201),
                received_at=measured_at - timedelta(seconds=1201),
                data_version="news-old-v1",
            ),
        )
    )
    cache.put(
        service._data_cache_identity(
            "daily_history",
            "eastmoney",
            quote.code,
            {"code": quote.code, "days": 90, "adjust": "qfq"},
            measured_at,
        ),
        history,
        data_version="history-old-v1",
        source_time=measured_at - timedelta(seconds=86401),
    )
    cache.put(
        service._data_cache_identity(
            "intraday_minutes",
            "eastmoney",
            quote.code,
            {"code": quote.code, "scale_minutes": 1, "adjust": "none"},
            measured_at,
        ),
        intraday,
        data_version="intraday-old-v1",
        source_time=measured_at - timedelta(seconds=91),
    )
    cache.put(
        service._data_cache_identity(
            "research_success",
            "akshare",
            quote.code,
            {"code": quote.code, "include_structured": False},
            measured_at,
        ),
        research,
        data_version="research-old-v1",
        source_time=measured_at - timedelta(seconds=1201),
    )

    features = service.read_candidate_features(
        (quote.code,),
        measured_at,
        include_intraday_tail=True,
        include_structured_research=False,
    )

    assert features[0].quote.execution_restrictions == (
        "history_data_degraded",
        "intraday_data_degraded",
        "research_data_degraded",
    )
    assert features[0].history_days == 60
    assert features[0].evidence[-1].evidence_id == "news-old"


def test_history_intraday_and_research_share_the_bounded_market_cache() -> None:
    runtime = load_runtime_settings(Path(__file__).parents[2] / "config" / "v2" / "runtime.json")
    cache: BoundedLruCache[object] = BoundedLruCache(
        runtime.market_data.cache_policy,
        cadence_seconds=runtime.pipeline.cadence_seconds,
        wall_clock=lambda: NOW,
    )
    history = CountingHistoryClient(_history_bars())
    intraday = StaticIntradayClient(_tail_minute_bars())
    evidence = Evidence(
        evidence_id="news-1",
        evidence_type="news",
        title="中标",
        source="fixture",
        published_at=NOW - timedelta(hours=1),
        received_at=NOW - timedelta(minutes=59),
        data_version="news-v1",
    )
    research = StaticResearchClient((evidence,))
    service = MarketFeatureService(
        StaticGateway((_quote(),)),
        history,
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
        intraday_client=intraday,
        research_client=research,
        cache=cache,
        source_contract_versions=runtime.market_data.source_contract_versions,
        config_version=runtime.config_version,
        wall_clock=lambda: NOW,
    )

    service._load_histories(("600001",))
    service._load_intraday(("600001",), NOW)
    service._load_research(("600001",), NOW, include_structured=False)
    service._load_histories(("600001",))
    service._load_intraday(("600001",), NOW)
    service._load_research(("600001",), NOW, include_structured=False)

    status = cache.status()
    assert status["daily_history"]["eastmoney"]["entries"] == 1
    assert status["daily_history"]["eastmoney"]["hit"] == 1
    assert status["intraday_minutes"]["eastmoney"]["entries"] == 1
    assert status["intraday_minutes"]["eastmoney"]["hit"] == 1
    assert status["research_success"]["akshare"]["entries"] == 1
    assert status["research_success"]["akshare"]["hit"] == 1
    assert history.calls == ["600001"]
    assert intraday.calls == ["600001"]
    assert research.calls == 1


def test_expired_unified_intraday_cache_triggers_a_new_physical_load() -> None:
    runtime = load_runtime_settings(Path(__file__).parents[2] / "config" / "v2" / "runtime.json")
    monotonic = MutableMonotonic()
    cache: BoundedLruCache[object] = BoundedLruCache(
        runtime.market_data.cache_policy,
        cadence_seconds=runtime.pipeline.cadence_seconds,
        monotonic=monotonic,
        wall_clock=lambda: NOW,
    )
    intraday = StaticIntradayClient(_tail_minute_bars())
    service = MarketFeatureService(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
        intraday_client=intraday,
        cache=cache,
        source_contract_versions=runtime.market_data.source_contract_versions,
        config_version=runtime.config_version,
        intraday_ttl_seconds=45,
        monotonic=monotonic,
        wall_clock=lambda: NOW,
    )

    service._load_intraday(("600001",), NOW)
    monotonic.value = 45.001
    service._load_intraday(("600001",), NOW)

    assert intraday.calls == ["600001", "600001"]


def test_reference_refresh_structures_tushare_history_valuation_and_financial_data() -> None:
    runtime = load_runtime_settings(Path(__file__).parents[2] / "config" / "v2" / "runtime.json")
    cache: BoundedLruCache[object] = BoundedLruCache(
        runtime.market_data.cache_policy,
        cadence_seconds=runtime.pipeline.cadence_seconds,
        wall_clock=lambda: AFTERNOON,
    )
    pool = BoundedExecutor(worker_count=5, queue_capacity=5, thread_name_prefix="source-data")
    lanes = SourceLaneRegistry(pool)

    class SlowDataPro(FakeTusharePro):
        def trade_cal(self, **kwargs):
            self.calls.append(("trade_cal", kwargs))
            return FakeTushareFrame(
                [{"exchange": "SSE", "cal_date": "20260715", "is_open": 1, "pretrade_date": "20260714"}]
            )

        def pro_bar(self, **kwargs):
            self.calls.append(("pro_bar", kwargs))
            return FakeTushareFrame(
                [
                    {
                        "ts_code": "600001.SH",
                        "trade_date": "20260715",
                        "open": 10.0,
                        "close": 10.2,
                        "high": 10.3,
                        "low": 9.9,
                        "vol": 1000.0,
                        "amount": 1020000.0,
                        "pct_chg": 2.0,
                    }
                ]
            )

        def daily_basic(self, **kwargs):
            self.calls.append(("daily_basic", kwargs))
            return FakeTushareFrame([{"ts_code": "600001.SH", "trade_date": "20260715", "pe": 12.0, "pb": 1.5}])

        def fina_indicator(self, **kwargs):
            self.calls.append(("fina_indicator", kwargs))
            return FakeTushareFrame(
                [{"ts_code": "600001.SH", "ann_date": "20260715", "end_date": "20260630", "eps": 1.0}]
            )

    pro = SlowDataPro(
        [
            {
                "ts_code": "600001.SH",
                "symbol": "600001",
                "name": "测试股份",
                "industry": "工业",
                "market": "主板",
                "exchange": "SSE",
                "list_status": "L",
                "list_date": "20200102",
            }
        ]
    )
    gateway = MarketDataGateway(
        StaticMarketClient((_quote(),)),
        StaticMarketClient((_quote(),)),
        StaticTencentClient((_quote(),)),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        worker_pool=pool,
        source_lanes=lanes,
        cache=cache,
        source_contract_versions=runtime.market_data.source_contract_versions,
        config_version=runtime.config_version,
        wall_clock=lambda: AFTERNOON,
    )
    service = MarketFeatureService(
        gateway,
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
        tushare_client=TushareClient(
            token="secret-token",
            timeout_seconds=8,
            sdk_factory=lambda _token, _timeout: pro,
            wall_clock=lambda: AFTERNOON,
        ),
        worker_pool=pool,
        source_lanes=lanes,
        cache=cache,
        source_contract_versions=runtime.market_data.source_contract_versions,
        config_version=runtime.config_version,
        wall_clock=lambda: AFTERNOON,
    )
    pool.start()

    try:
        service.refresh_reference_data(("600001",), AFTERNOON)
        service.refresh_reference_data(("600001",), AFTERNOON)
    finally:
        lanes.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)
        cache.stop()

    assert [name for name, _arguments in pro.calls] == [
        "stock_basic",
        "trade_cal",
        "pro_bar",
        "daily_basic",
        "fina_indicator",
    ]
    daily_basic_arguments = next(arguments for name, arguments in pro.calls if name == "daily_basic")
    assert daily_basic_arguments["trade_date"] == "20260715"
    assert service._history["600001"].bars[-1].trade_date == "2026-07-15"
    assert service._history["600001"].bars[-1].volume == 100_000.0
    assert service._history["600001"].bars[-1].amount == 1_020_000_000.0
    assert service._tushare_reference_fields["600001"]["tushare_valuation_pe"] == 12.0
    assert service._tushare_reference_fields["600001"]["tushare_financial_eps"] == 1.0


def test_eastmoney_history_completion_cannot_overwrite_newer_tushare_history() -> None:
    eastmoney_bar = DailyBar("2026-07-14", 10.0, 10.1, 10.2, 9.9, 1000.0, 10000.0, 1.0)

    class BlockingHistory:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()

        def fetch_history(self, _code, *, days):
            assert days == 90
            self.started.set()
            assert self.release.wait(1.0)
            return (eastmoney_bar,)

    history = BlockingHistory()
    service = MarketFeatureService(
        StaticGateway((_quote(),)),
        history,
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
        wall_clock=lambda: NOW,
    )
    result: dict[str, tuple[DailyBar, ...]] = {}
    errors: list[BaseException] = []

    def load_eastmoney() -> None:
        try:
            result.update(service._load_histories(("600001",)))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=load_eastmoney)
    thread.start()
    assert history.started.wait(1.0)
    tushare = SourceObservation(
        source="tushare",
        subject_key="600001",
        observed_at=NOW,
        source_time=NOW,
        received_at=NOW,
        effective_at=NOW,
        data_version="tushare-history-v2",
        fields={
            "trade_date": "2026-07-15",
            "open": 10.1,
            "close": 10.2,
            "high": 10.3,
            "low": 10.0,
            "vol": 11.0,
            "amount": 12.0,
            "pct_chg": 1.0,
        },
        missing_reasons={},
        payload_hash="tushare-history-v2",
        status="success",
        error_code=None,
    )
    service._apply_tushare_history((tushare,))
    history.release.set()
    thread.join(1.0)

    assert not thread.is_alive()
    assert errors == []
    assert result["600001"][-1].trade_date == "2026-07-15"
    assert service._history["600001"].bars[-1].trade_date == "2026-07-15"


def test_gateway_marks_circuit_open_vendor_as_skipped_in_route_health() -> None:
    quote = _quote()
    gateway = MarketDataGateway(
        StaticMarketClient((quote,)),
        StaticMarketClient((quote,)),
        StaticTencentClient((quote,)),
        minimum_market_rows=1,
        circuit_breaker_failures=1,
        circuit_breaker_seconds=60,
    )
    gateway._states["eastmoney"].open_until = gateway._monotonic() + 60.0

    fetched = tuple(gateway.fetch_market())
    assert [(item.code, item.price, item.source) for item in fetched] == [(quote.code, quote.price, "sina")]
    health = gateway.health()

    assert health["active_source"] == "sina"
    assert health["route"]["status"] == "success"
    assert health["route"]["degraded"] is True
    assert health["route"]["used_vendor"] == "sina"
    assert health["route"]["attempted_count"] == 2
    assert health["route"]["failure_count"] == 0
    assert health["route"]["skipped_count"] == 1
    assert health["route"]["attempted_vendors"][0]["name"] == "eastmoney"
    assert health["route"]["attempted_vendors"][0]["status"] == "skipped"
    assert health["route"]["attempted_vendors"][0]["skipped"] is True
    assert health["route"]["attempted_vendors"][0]["error"] == "circuit_open"
    assert health["route"]["attempted_vendors"][1]["name"] == "sina"
    assert health["route"]["attempted_vendors"][1]["status"] == "success"


def test_gateway_coalesces_concurrent_full_market_requests_into_one_physical_call() -> None:
    source = BlockingMarketClient((_quote(),))
    gateway = MarketDataGateway(
        source,
        StaticMarketClient(()),
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
    )
    results: list[tuple[MarketQuote, ...]] = []
    threads = [threading.Thread(target=lambda: results.append(tuple(gateway.fetch_market()))) for _index in range(2)]

    try:
        for thread in threads:
            thread.start()
        assert source.started.wait(1.0)
        time.sleep(0.02)
    finally:
        source.release.set()
        for thread in threads:
            thread.join(1.0)

    assert source.calls == 1
    assert [[(item.code, item.price, item.source) for item in result] for result in results] == [
        [("600001", 12.0, "eastmoney")],
        [("600001", 12.0, "eastmoney")],
    ]


def test_gateway_allows_one_recovery_probe_after_circuit_timeout() -> None:
    monotonic = MutableMonotonic()
    source = SequenceMarketClient((RuntimeError("offline"), (_quote(),)))
    gateway = MarketDataGateway(
        source,
        FailingMarketClient(),
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=1,
        circuit_breaker_seconds=60,
        monotonic=monotonic,
    )

    with pytest.raises(MarketDataUnavailable):
        gateway.fetch_market()
    assert gateway.health()["sources"]["eastmoney"]["circuit_open"] is True

    monotonic.value = 61.0

    fetched = tuple(gateway.fetch_market())
    assert [(item.code, item.price, item.source) for item in fetched] == [("600001", 12.0, "eastmoney")]
    assert source.calls == 2
    assert gateway.health()["sources"]["eastmoney"]["circuit_open"] is False


def test_gateway_reports_recoverable_unavailability_when_all_sources_fail() -> None:
    gateway = MarketDataGateway(
        FailingMarketClient(),
        FailingMarketClient(),
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=1,
        circuit_breaker_seconds=60,
    )

    with pytest.raises(MarketDataUnavailable, match=r"eastmoney: offline; sina: offline"):
        gateway.fetch_market()

    health = gateway.health()
    assert health["active_source"] == "unavailable"
    assert health["sources"]["eastmoney"]["circuit_open"] is True
    assert health["sources"]["sina"]["circuit_open"] is True
    assert health["route"]["status"] == "failed"
    assert health["route"]["fallback_reason"] == "failed"
    assert health["route"]["used_vendor"] == "sina"
    assert [item["name"] for item in health["route"]["attempted_vendors"]] == ["eastmoney", "sina"]
    assert [item["status"] for item in health["route"]["attempted_vendors"]] == ["failed", "failed"]


def test_gateway_health_records_no_data_route_fallback() -> None:
    gateway = MarketDataGateway(
        FailingMarketClient(),
        StaticMarketClient(()),
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=1,
        circuit_breaker_seconds=60,
    )

    with pytest.raises(MarketDataUnavailable, match=r"sina: only 0 market rows;.*eastmoney: offline"):
        gateway.fetch_market()

    health = gateway.health()
    assert health["route"]["status"] == "no_data"
    assert health["route"]["fallback_reason"] == "no_data"
    assert health["route"]["used_vendor"] is None
    assert health["route"]["attempted_vendors"][0]["status"] == "failed"
    assert health["route"]["attempted_vendors"][1]["status"] == "no_data"


def test_market_data_router_prefers_no_data_over_failures() -> None:
    def empty_payload() -> tuple[MarketQuote, ...]:
        return ()

    with pytest.raises(MarketDataNoData, match="insufficient rows") as exc_info:
        route(
            (
                VendorRoute(
                    "eastmoney",
                    lambda: (_ for _ in ()).throw(RuntimeError("offline")),
                    VendorSeverity.REQUIRED,
                ),
                VendorRoute("sina", empty_payload, VendorSeverity.REQUIRED),
            ),
            on_no_data="insufficient rows",
        )
    message = str(exc_info.value)
    assert "eastmoney: offline" in message
    assert "sina: insufficient rows" in message


def test_market_data_router_aggregates_required_failures() -> None:
    def failing() -> tuple[MarketQuote, ...]:
        raise RuntimeError("offline")

    with pytest.raises(MarketDataFailed, match=r"eastmoney: offline; sina: offline") as exc_info:
        route(
            (
                VendorRoute("eastmoney", failing, VendorSeverity.REQUIRED),
                VendorRoute("sina", failing, VendorSeverity.REQUIRED),
            )
        )
    assert str(exc_info.value).startswith("sina: ")


def test_feature_service_rejects_targeted_quote_older_than_full_market_snapshot() -> None:
    current = replace(
        _quote(),
        price=12.5,
        source_time=NOW + timedelta(seconds=2),
        received_time=NOW + timedelta(seconds=2),
        data_version="market-v2",
    )
    older = replace(_quote(), price=11.5, data_version="target-v1")
    middle = replace(
        _quote(),
        price=12.0,
        source_time=NOW + timedelta(seconds=1),
        received_time=NOW + timedelta(seconds=1),
        data_version="target-middle",
    )
    gateway = StaticGatewayWithSeparateQuotes((current,), (older,))
    service = MarketFeatureService(
        gateway,
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
        wall_clock=lambda: NOW + timedelta(seconds=5),
    )
    service.refresh_candidate_quotes(("600001",), NOW)
    service.fetch_market_features(NOW + timedelta(seconds=2))
    gateway._candidate_quotes = (middle,)

    refreshed = service.refresh_candidate_quotes(("600001",), NOW + timedelta(seconds=3))

    assert refreshed[0].quote.price == 12.5
    assert refreshed[0].quote.data_version == "market-v2"
    assert service.health()["quote_out_of_order_count"] == 1


def test_feature_service_does_not_commit_full_market_result_after_deadline() -> None:
    deadline = NOW + timedelta(seconds=1)
    wall_times = iter((NOW, deadline))
    service = MarketFeatureService(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
        wall_clock=lambda: next(wall_times),
    )

    with pytest.raises(MarketDataDeadlineExceeded, match="completed after"):
        service.fetch_market_features(NOW, deadline=deadline)

    assert service._market_features == ()


def test_feature_service_does_not_commit_history_cache_after_deadline() -> None:
    deadline = NOW + timedelta(seconds=1)
    wall_times = iter((NOW, NOW, NOW, deadline))
    service = MarketFeatureService(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
        wall_clock=lambda: next(wall_times, deadline),
    )

    with pytest.raises(MarketDataDeadlineExceeded, match="completed after"):
        service.fetch_market_features(NOW, deadline=deadline)

    assert service._history == {}
    assert service._market_features == ()


def test_full_market_deadline_does_not_wait_for_blocked_history_warmup() -> None:
    runtime = load_runtime_settings(Path(__file__).parents[2] / "config" / "v2" / "runtime.json")
    cache: BoundedLruCache[object] = BoundedLruCache(
        runtime.market_data.cache_policy,
        cadence_seconds=runtime.pipeline.cadence_seconds,
    )
    pool = BoundedExecutor(worker_count=5, queue_capacity=5, thread_name_prefix="source-data")
    lanes = SourceLaneRegistry(pool)
    history = BlockingHistoryClient(_history_bars())
    service = MarketFeatureService(
        StaticGateway((_quote(),)),
        history,
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
        worker_pool=pool,
        source_lanes=lanes,
        cache=cache,
        source_contract_versions=runtime.market_data.source_contract_versions,
        config_version=runtime.config_version,
    )
    pool.start()
    deadline = datetime.now(timezone.utc) + timedelta(seconds=0.2)
    release_timer = threading.Timer(0.6, history.release.set)
    release_timer.start()

    started = time.monotonic()
    try:
        features = service.fetch_market_features(NOW, deadline=deadline)
        elapsed = time.monotonic() - started
    finally:
        history.release.set()
        release_timer.cancel()
        lanes.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)
        cache.stop()

    assert elapsed < 0.2
    assert [feature.quote.code for feature in features] == ["600001"]
    assert features[0].history_days == 0
    assert cache.status()["daily_history"]["eastmoney"]["entries"] == 0


def test_source_lane_research_deadline_discards_late_memory_and_disk_cache(tmp_path) -> None:
    runtime = load_runtime_settings(Path(__file__).parents[2] / "config" / "v2" / "runtime.json")
    cache: BoundedLruCache[object] = BoundedLruCache(
        runtime.market_data.cache_policy,
        cadence_seconds=runtime.pipeline.cadence_seconds,
    )
    pool = BoundedExecutor(worker_count=5, queue_capacity=5, thread_name_prefix="source-data")
    lanes = SourceLaneRegistry(pool)
    research = BlockingResearchClient((Evidence("news-late", "news", "迟到新闻", "fixture", NOW - timedelta(hours=1)),))
    service = MarketFeatureService(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
        research_client=research,
        research_cache_dir=tmp_path,
        worker_pool=pool,
        source_lanes=lanes,
        cache=cache,
        source_contract_versions=runtime.market_data.source_contract_versions,
        config_version=runtime.config_version,
    )
    pool.start()
    deadline = datetime.now(timezone.utc) + timedelta(seconds=0.01)
    release_timer = threading.Timer(0.6, research.release.set)
    release_timer.start()

    started = time.monotonic()
    try:
        with pytest.raises(MarketDataDeadlineExceeded):
            service.refresh_market_news(("600001",), NOW, deadline=deadline)
        elapsed = time.monotonic() - started
    finally:
        research.release.set()
        release_timer.cancel()
        lanes.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)
        cache.stop()

    assert elapsed < 0.5
    assert service._research == {}
    research_cache_status = cache.status().get("research_success", {}).get("akshare", {})
    assert research_cache_status.get("entries", 0) == 0
    assert not (tmp_path / "observations").exists()


def test_feature_service_health_reports_bounded_quote_age_summaries() -> None:
    measured_at = NOW + timedelta(seconds=31)
    service = MarketFeatureService(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
        wall_clock=lambda: measured_at,
    )
    service.fetch_market_features(NOW)
    service.refresh_candidate_quotes(("600001",), NOW)

    health = service.health()

    assert health["market_quote_age"] == {
        "sample_count": 1,
        "p50_seconds": 31.0,
        "p95_seconds": 31.0,
        "maximum_seconds": 31.0,
        "latest_source_time": NOW.isoformat(),
    }
    assert health["candidate_quote_age"]["maximum_seconds"] == 31.0


def test_feature_service_current_quote_index_prefers_latest_targeted_quote() -> None:
    market_quote = _quote()
    targeted_quote = replace(
        market_quote,
        price=15.0,
        pct_change=8.0,
        source="tencent",
        source_time=NOW + timedelta(seconds=5),
        received_time=NOW + timedelta(seconds=5),
        data_version="targeted-v2",
    )
    service = MarketFeatureService(
        StaticGatewayWithSeparateQuotes((market_quote,), (targeted_quote,)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
    )
    service.fetch_market_features(NOW)
    service.refresh_candidate_quotes(("600001",), NOW + timedelta(seconds=5))

    quotes = service.current_quotes(("600001", "600999"))

    assert tuple(quotes) == ("600001",)
    assert quotes["600001"].price == 15.0
    assert quotes["600001"].pct_change == 8.0
    assert quotes["600001"].source == "tencent"
    assert quotes["600001"].data_version == "targeted-v2"


def test_feature_service_current_quote_index_reads_canonical_quote_before_feature_commit() -> None:
    canonical_quote = replace(
        _quote(),
        price=14.0,
        pct_change=6.0,
        data_version="canonical-v2",
    )
    service = MarketFeatureService(
        StaticGateway((canonical_quote,)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
    )

    quotes = service.current_quotes(("600001",))

    assert quotes["600001"].price == 14.0
    assert quotes["600001"].pct_change == 6.0
    assert quotes["600001"].data_version == "canonical-v2"


def test_feature_builder_does_not_compute_limit_proximity_when_limit_is_inapplicable() -> None:
    quote = replace(
        _quote(),
        has_price_limit=False,
        exchange_limit_pct=None,
        listing_age_sessions=1,
    )

    feature = FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY).build((quote,), {}, NOW)[0]

    assert feature.values["limit_proximity"] is None
    assert feature.values["limit_distance_safety"] is None


def test_out_of_order_intraday_refresh_keeps_last_valid_tail_input() -> None:
    monotonic = MutableMonotonic()
    current = _tail_minute_bars()
    older = tuple(
        replace(
            bar,
            source_time=bar.source_time - timedelta(days=1),
            received_time=bar.received_time - timedelta(days=1),
            data_version="intraday-old",
        )
        for bar in current
    )
    service = MarketFeatureService(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
        intraday_client=SequenceIntradayClient((current, older)),
        intraday_ttl_seconds=1,
        monotonic=monotonic,
    )

    first = service.fetch_candidate_features(("600001",), AFTERNOON, include_intraday_tail=True)
    monotonic.value = 2.0
    second = service.fetch_candidate_features(("600001",), AFTERNOON, include_intraday_tail=True)

    assert second[0].values["tail_return_30m"] == first[0].values["tail_return_30m"]
    assert second[0].values["tail_volume_ratio"] == first[0].values["tail_volume_ratio"]
    assert service.health()["intraday_out_of_order_count"] == 1


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


def test_market_service_uses_injected_lifecycle_data_pool() -> None:
    pool = BoundedExecutor(worker_count=1, queue_capacity=8, thread_name_prefix="shared-data")
    history = ThreadRecordingHistoryClient(_history_bars())
    gateway = ThreadRecordingGateway((_quote(), _quote(code="600002")))
    service = MarketFeatureService(
        gateway,
        history,
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
        worker_pool=pool,
        history_workers=2,
    )
    pool.start()
    try:
        features = service.fetch_market_features(NOW)
    finally:
        pool.stop()

    assert len(features) == 2
    assert len(history.thread_names) == 2
    assert all(name.startswith("shared-data") for name in history.thread_names)
    assert gateway.thread_names and all(name.startswith("shared-data") for name in gateway.thread_names)
    assert not any(thread.name.startswith("shared-data") for thread in threading.enumerate())


def test_candidate_quote_refresh_uses_reserved_urgent_worker() -> None:
    pool = BoundedExecutor(
        worker_count=2,
        urgent_worker_count=1,
        queue_capacity=8,
        thread_name_prefix="shared-priority-data",
    )
    lanes = SourceLaneRegistry(pool)
    entered = threading.Event()
    release = threading.Event()

    def blocking_task() -> None:
        entered.set()
        release.wait(timeout=2.0)

    gateway = MarketDataGateway(
        StaticMarketClient((_quote(),)),
        StaticMarketClient((replace(_quote(), source="sina"),)),
        StaticTencentClient((replace(_quote(), source="tencent"),)),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        worker_pool=pool,
        source_lanes=lanes,
        wall_clock=lambda: NOW,
    )
    service = MarketFeatureService(
        gateway,
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
        worker_pool=pool,
        source_lanes=lanes,
        wall_clock=lambda: NOW,
    )
    pool.start()
    try:
        normal = pool.submit(blocking_task)
        assert normal is not None
        assert entered.wait(timeout=1.0)
        refreshed = service.refresh_candidate_quotes(
            ("600001",),
            NOW,
            deadline=NOW + timedelta(seconds=1),
        )
    finally:
        release.set()
        lanes.stop(wait=True, timeout_seconds=1.0)
        pool.stop()

    assert [feature.quote.code for feature in refreshed] == ["600001"]


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


def test_production_source_lanes_warm_tushare_history_after_realtime_commit() -> None:
    quotes = (_quote(), _quote(code="300001"), _quote(code="688001"))
    completed = threading.Event()

    class HistoryPro:
        def __init__(self) -> None:
            self.calls = 0

        def daily(self, **kwargs):
            self.calls += 1
            rows = [
                {
                    "ts_code": code,
                    "trade_date": (date(2026, 7, 15) - timedelta(days=index)).strftime("%Y%m%d"),
                    "open": 10.0,
                    "close": 10.5,
                    "high": 10.8,
                    "low": 9.9,
                    "vol": 1000.0,
                    "amount": 10500.0,
                    "pct_chg": 1.0,
                }
                for code in str(kwargs["ts_code"]).split(",")
                for index in range(60)
            ]
            completed.set()
            return FakeTushareFrame(rows)

    pro = HistoryPro()
    pool = BoundedExecutor(worker_count=5, queue_capacity=5, thread_name_prefix="source-data")
    lanes = SourceLaneRegistry(pool)
    service = MarketFeatureService(
        StaticGateway(quotes),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
        tushare_client=TushareClient(
            token="secret-token",
            timeout_seconds=8,
            points=120,
            sdk_factory=lambda _token, _timeout: pro,
            wall_clock=lambda: NOW,
        ),
        worker_pool=pool,
        source_lanes=lanes,
        history_warmup_batch_size=3,
        market_ttl_seconds=1,
        wall_clock=lambda: NOW,
    )
    pool.start()

    try:
        first = service.fetch_market_features(NOW, deadline=NOW + timedelta(seconds=1))
        assert all(feature.history_days == 0 for feature in first)
        assert completed.wait(1.0)
        lanes.submit("tushare", "history-warmup-barrier", NOW + timedelta(seconds=1), lambda: None).result(timeout=1.0)
        second = service.fetch_market_features(NOW + timedelta(seconds=2), force=True)
    finally:
        lanes.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)

    assert all(feature.history_days == 60 for feature in second)
    assert service.health()["history_coverage_ratio"] == 1.0
    assert service.health()["history_warmup_completed_count"] == 3
    assert pro.calls == 1


def test_permission_denied_tushare_falls_back_to_batched_history_lane() -> None:
    codes = ("600001", "600002", "300001", "300002", "688001", "688002")
    quotes = tuple(_quote(code=code) for code in codes)

    class PermissionDeniedTushare:
        @staticmethod
        def health():
            return {
                "enabled": True,
                "circuit_open": False,
                "degraded_reason": "permission_denied",
            }

    history = CountingHistoryClient(_history_bars())
    pool = BoundedExecutor(worker_count=5, queue_capacity=5, thread_name_prefix="source-data")
    lanes = SourceLaneRegistry(pool)
    service = MarketFeatureService(
        StaticGateway(quotes),
        history,
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
        tushare_client=PermissionDeniedTushare(),
        worker_pool=pool,
        source_lanes=lanes,
        history_warmup_batch_size=3,
        market_ttl_seconds=1,
        wall_clock=lambda: NOW,
    )
    pool.start()

    try:
        service.fetch_market_features(NOW, deadline=NOW + timedelta(seconds=1))
        timeout_at = time.monotonic() + 1.0
        while service.health()["history_warmup_completed_count"] < len(codes) and time.monotonic() < timeout_at:
            time.sleep(0.01)
    finally:
        lanes.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)

    assert sorted(history.calls) == sorted(codes)
    assert service.health()["history_warmup_completed_count"] == len(codes)
    assert service.health()["history_warmup_last_source"] == "tencent"


def test_repeated_refresh_does_not_queue_multiple_history_warmup_batches() -> None:
    codes = ("600001", "600002", "300001", "300002", "688001", "688002")
    started = threading.Event()
    release = threading.Event()

    class BlockingHistory:
        @staticmethod
        def fetch_history(_code, *, days):
            assert days == 90
            started.set()
            assert release.wait(1.0)
            return _history_bars()

    pool = BoundedExecutor(worker_count=2, queue_capacity=2, thread_name_prefix="source-data")
    lanes = SourceLaneRegistry(pool)
    service = MarketFeatureService(
        StaticGateway(tuple(_quote(code=code) for code in codes)),
        BlockingHistory(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
        worker_pool=pool,
        source_lanes=lanes,
        history_warmup_batch_size=3,
        wall_clock=lambda: NOW,
    )
    pool.start()

    try:
        service.schedule_history_warmup(codes, NOW)
        assert started.wait(1.0)
        service.schedule_history_warmup(codes, NOW + timedelta(seconds=1))
        service.schedule_history_warmup(codes, NOW + timedelta(seconds=2))
        health = service.health()
        assert health["history_warmup_planned_count"] == 3
        assert health["history_warmup_inflight_count"] == 3
    finally:
        release.set()
        lanes.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)


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


def test_source_lane_intraday_batch_timeout_returns_without_waiting_for_blocked_io() -> None:
    pool = BoundedExecutor(worker_count=5, queue_capacity=5, thread_name_prefix="source-data")
    lanes = SourceLaneRegistry(pool)
    intraday = BlockingIntradayClient()
    service = MarketFeatureService(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
        intraday_client=intraday,
        intraday_workers=1,
        intraday_batch_timeout_seconds=0.01,
        worker_pool=pool,
        source_lanes=lanes,
    )
    pool.start()

    started = time.monotonic()
    try:
        result = service.fetch_candidate_features(
            ("600001",),
            AFTERNOON,
            include_intraday_tail=True,
        )
        elapsed = time.monotonic() - started
    finally:
        intraday.release.set()
        lanes.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)

    assert elapsed < 0.5
    assert result[0].values["tail_return_30m"] is None
    assert service.health()["intraday_tail_last_error"] == "intraday_batch_deadline"


def test_timed_out_intraday_lane_cannot_mutate_caller_restrictions_after_return() -> None:
    runtime = load_runtime_settings(Path(__file__).parents[2] / "config" / "v2" / "runtime.json")
    measured_at = AFTERNOON + timedelta(seconds=91)
    cache: BoundedLruCache[object] = BoundedLruCache(
        runtime.market_data.cache_policy,
        cadence_seconds=runtime.pipeline.cadence_seconds,
        wall_clock=lambda: measured_at,
    )
    pool = BoundedExecutor(worker_count=5, queue_capacity=5, thread_name_prefix="source-data")
    lanes = SourceLaneRegistry(pool)
    service = MarketFeatureService(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
        intraday_client=StaticIntradayClient(_tail_minute_bars()),
        intraday_workers=1,
        intraday_batch_timeout_seconds=0.01,
        worker_pool=pool,
        source_lanes=lanes,
        cache=cache,
        source_contract_versions=runtime.market_data.source_contract_versions,
        config_version=runtime.config_version,
        wall_clock=lambda: measured_at,
    )
    pool.start()
    service._load_intraday(("600001",), AFTERNOON)
    service._intraday["600001"] = replace(service._intraday["600001"], expires_at=-1.0)
    blocking = BlockingIntradayClient()
    service._intraday_client = blocking

    class TrackingRestrictions(dict[str, set[str]]):
        def __init__(self) -> None:
            super().__init__()
            self.mutation_threads: list[str] = []

        def setdefault(self, key: str, default: set[str] | None = None) -> set[str]:
            self.mutation_threads.append(threading.current_thread().name)
            return super().setdefault(key, default or set())

    restrictions = TrackingRestrictions()
    try:
        result = service._load_intraday(
            ("600001",),
            AFTERNOON,
            action_restrictions=restrictions,
        )
        blocking.release.set()
        lanes.stop(wait=True, timeout_seconds=1.0)
    finally:
        blocking.release.set()
        lanes.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)
        cache.stop()

    assert result == {}
    assert restrictions == {}
    assert restrictions.mutation_threads == []


def test_source_lane_cancels_queued_intraday_io_after_batch_timeout() -> None:
    pool = BoundedExecutor(worker_count=5, queue_capacity=5, thread_name_prefix="source-data")
    lanes = SourceLaneRegistry(pool)
    intraday = StaticIntradayClient(())
    service = MarketFeatureService(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
        intraday_client=intraday,
        intraday_workers=1,
        intraday_batch_timeout_seconds=0.01,
        worker_pool=pool,
        source_lanes=lanes,
    )
    occupied = threading.Event()
    release = threading.Event()

    def occupy_eastmoney_lane() -> None:
        occupied.set()
        assert release.wait(1.0)

    pool.start()
    running = lanes.submit(
        "eastmoney",
        "occupied",
        AFTERNOON - timedelta(seconds=1),
        occupy_eastmoney_lane,
    )
    assert occupied.wait(1.0)

    try:
        result = service._load_intraday(("600001",), AFTERNOON)
        release.set()
        running.result(timeout=1.0)
    finally:
        release.set()
        lanes.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)

    assert result == {}
    assert intraday.calls == []
    assert lanes.status()["eastmoney"]["pending"] is False


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


def test_akshare_news_response_is_cached_with_atomic_writer(tmp_path) -> None:
    callback = "jQuery35101792940631092459_1764599530165"
    payload = {
        "result": {
            "cmsArticleWebOld": [
                {
                    "title": "<em>测试股份</em>发布公告",
                    "date": "2026-07-16 09:00:00",
                    "mediaName": "交易所",
                },
            ]
        }
    }

    def get(*args, **kwargs):
        return FakeResponse(f"{callback}({json.dumps(payload, ensure_ascii=False)});")

    AkshareResearchClient(
        timeout_seconds=8,
        get=get,
        evidence_cache_dir=tmp_path,
    ).fetch_news("600001", observed_at=NOW)

    cached = json.loads((tmp_path / "raw" / "news" / "600001.json").read_text(encoding="utf-8"))

    assert cached["source"] == "news"
    assert cached["code"] == "600001"
    assert "payload" in cached


def test_research_cache_is_used_after_restart_before_source_request(tmp_path) -> None:
    cache_dir = tmp_path / "evidence_cache"
    cache_file = cache_dir / "observations" / "news" / "600001.json"
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(
        json.dumps(
            {
                "include_structured": False,
                "expires_at": (NOW + timedelta(minutes=10)).isoformat(),
                "observation": {
                    "financial": None,
                    "announcements": (),
                    "announcements_available": False,
                    "pledge_ratio_pct": None,
                    "unlock_ratio_pct": None,
                    "evidence": [
                        {
                            "evidence_id": "cached-news:1",
                            "evidence_type": "news",
                            "title": "缓存新闻",
                            "source": "eastmoney_news",
                            "published_at": "2026-07-16T09:00:00+08:00",
                            "received_at": "2026-07-16T09:05:00+08:00",
                            "data_version": "cached-v1",
                        }
                    ],
                    "source_errors": [],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    service = MarketFeatureService(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
        research_client=FailingResearchClient(),
        research_cache_dir=cache_dir,
        research_workers=1,
        wall_clock=lambda: NOW,
    )
    result = service.fetch_candidate_features(("600001",), NOW)

    assert any(item.evidence_id == "cached-news:1" for item in result[0].evidence)


def test_research_cache_expired_calls_research_client(tmp_path) -> None:
    cache_dir = tmp_path / "evidence_cache"
    cache_file = cache_dir / "observations" / "news" / "600001.json"
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(
        json.dumps(
            {
                "include_structured": False,
                "expires_at": (NOW - timedelta(minutes=1)).isoformat(),
                "observation": {
                    "financial": None,
                    "announcements": (),
                    "announcements_available": False,
                    "pledge_ratio_pct": None,
                    "unlock_ratio_pct": None,
                    "evidence": [
                        {
                            "evidence_id": "stale-news:1",
                            "evidence_type": "news",
                            "title": "过期缓存",
                            "source": "eastmoney_news",
                            "published_at": "2026-07-16T09:00:00+08:00",
                            "received_at": "2026-07-16T09:05:00+08:00",
                            "data_version": "cached-v1",
                        }
                    ],
                    "source_errors": [],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    research = StaticResearchClient(
        (Evidence("fresh-news", "news", "实时抓取", "eastmoney_news", NOW - timedelta(hours=1)),)
    )
    service = MarketFeatureService(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
        research_client=research,
        research_cache_dir=cache_dir,
        research_workers=1,
        wall_clock=lambda: NOW,
    )
    result = service.fetch_candidate_features(("600001",), NOW)

    assert research.calls == 1
    assert any(item.evidence_id == "fresh-news" for item in result[0].evidence)


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


def test_stock_risk_refresh_reuses_successful_ten_minute_cache() -> None:
    observation = ResearchObservation(
        announcements_available=True,
        pledge_ratio_pct=0.0,
        unlock_ratio_pct=0.0,
    )
    research = StaticStructuredResearchClient((), observation)
    service = MarketFeatureService(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
        research_client=research,
        research_workers=1,
    )

    service.refresh_stock_risk(("600001",), AFTERNOON)
    service.refresh_stock_risk(("600001",), AFTERNOON + timedelta(minutes=3))

    assert research.snapshot_calls == 1


def test_stock_risk_batch_deadline_is_controlled_and_discards_late_result() -> None:
    research = BlockingStructuredResearchClient()
    observed_at = datetime.now(timezone.utc)
    service = MarketFeatureService(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, D25_POLICY, LONG_POLICY),
        research_client=research,
        research_workers=1,
        wall_clock=lambda: datetime.now(timezone.utc),
    )
    release_timer = threading.Timer(0.6, research.release.set)
    release_timer.start()

    try:
        with pytest.raises(MarketDataDeadlineExceeded, match="deadline"):
            service.refresh_stock_risk(
                ("600001",),
                observed_at,
                deadline=observed_at + timedelta(seconds=0.01),
            )
    finally:
        research.release.set()
        release_timer.cancel()

    assert service.health()["research_last_error"] == "research_batch_deadline"
    assert service._research == {}


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

    def fetch_quotes(self, _codes):
        return self._quotes


class BlockingTencentClient(StaticTencentClient):
    def __init__(self, quotes) -> None:
        super().__init__(quotes)
        self.release = threading.Event()

    def fetch_quotes(self, codes):
        self.release.wait(1.0)
        return super().fetch_quotes(codes)


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
    def health():
        return {}


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
