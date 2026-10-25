from __future__ import annotations

from zoneinfo import ZoneInfo

from tests.component.market_data_test_support import (
    FEATURE_WEIGHT_POLICY,
    LONG_POLICY,
    MARKET_REGIME_POLICY,
    NEWS_POLICY,
    NOW,
    TAIL_POLICY,
    BlockingMarketClient,
    BoundedExecutor,
    CountingHistoryClient,
    CountingMarketClient,
    EastmoneyClient,
    FailingMarketClient,
    FakeResponse,
    FakeSession,
    FallbackHistoryClient,
    FeatureBuilder,
    MarketDataFailedError,
    MarketDataGateway,
    MarketDataNoDataError,
    MarketQuote,
    PriceAdjustment,
    Route,
    SinaClient,
    SourceLaneScheduler,
    StaticHistoryClient,
    StaticMarketClient,
    StaticTencentClient,
    TencentClient,
    VendorRoute,
    VendorSeverity,
    _history_bars,
    _quote,
    _service,
    date,
    datetime,
    json,
    pytest,
    replace,
    threading,
    time,
    timedelta,
    timezone,
)
from trader.infra.http_contracts import HttpStatusError, HttpTimeout
from trader.recommendation.infra.market_data.providers.tencent import TencentFullMarketSource
from trader.training.domain.evaluation.models import OutcomeBar, OutcomePrice, OutcomeTradingStatus


def test_eastmoney_normalizes_history() -> None:
    history_payload = {"data": {"klines": ["2026-07-15,10,11,12,9,100,100000000,3,1"]}}
    session = FakeSession([history_payload])
    client = EastmoneyClient(worker_pool=None, timeout_seconds=2, session_factory=lambda: session)

    history = client.fetch_history("600001", days=90, now=NOW)

    assert history[0].amount == 100000000
    assert history[0].volume == 10_000.0


def test_eastmoney_history_fallback_attempts_each_host_once() -> None:
    session = FakeSession([HttpTimeout("slow host")] * 3)
    client = EastmoneyClient(worker_pool=None, timeout_seconds=2, session_factory=lambda: session)

    with pytest.raises(RuntimeError, match="eastmoney request failed"):
        client.fetch_history("600001", days=90, now=NOW)

    assert len(session.calls) == 3
    assert len({call[0][0].split("/", 3)[2] for call in session.calls}) == 3


def test_eastmoney_outcome_history_requests_explicit_qfq_and_raw_pairs() -> None:
    qfq_payload = {
        "data": {
            "klines": [
                "2026-07-14,5,5,5.1,4.9,100,1000000,4,0",
                "2026-07-15,4.5,4.5,4.5,4.5,100,900000,0,-10",
            ]
        }
    }
    raw_payload = {
        "data": {
            "klines": [
                "2026-07-14,10,10,10.2,9.8,100,1000000,4,0",
                "2026-07-15,9,9,9,9,100,900000,0,-10",
            ]
        }
    }
    session = FakeSession([qfq_payload, raw_payload])
    client = EastmoneyClient(worker_pool=None, timeout_seconds=2, session_factory=lambda: session)

    bars = client.fetch_outcome_history("600001", days=20, now=NOW)

    assert [call[1]["params"]["fqt"] for call in session.calls] == ["1", "0"]
    assert bars[0].qfq.close == 5.0
    assert bars[0].raw.close == 10.0
    assert bars[1].trading_status is OutcomeTradingStatus.ONE_PRICE_LIMIT_DOWN
    assert {bar.source for bar in bars} == {"eastmoney"}


@pytest.mark.parametrize(
    ("wire_volume", "shares"),
    [("250000", 25_000_000.0), ("0", 0.0), ("", None), ("-1", None), ("nan", None), ("inf", None)],
)
def test_tencent_normalizes_targeted_quote(wire_volume: str, shares: float | None) -> None:
    fields = [""] * 50
    fields[1] = "测试股份"
    fields[2] = "600001"
    fields[3] = "12.00"
    fields[4] = "11.65"
    fields[5] = "11.80"
    fields[6] = wire_volume
    fields[30] = "20260716100000"
    fields[32] = "3.00"
    fields[33] = "12.20"
    fields[34] = "11.70"
    fields[35] = "0/0/300000000"
    fields[38] = "3.0"
    fields[43] = "4.0"
    fields[45] = "120.5"
    fields[49] = "2.0"
    body = f'v_sh600001="{"~".join(fields)}";'.encode("gb18030")
    session = FakeSession([body])
    client = TencentClient(worker_pool=None, timeout_seconds=2, session_factory=lambda: session)

    quotes = client.fetch_quotes(["600001"], NOW, timeout_seconds=0.75)

    assert quotes[0].price == 12.0
    assert quotes[0].amount == 300000000.0
    assert quotes[0].volume == shares
    assert quotes[0].market_cap == 12_050_000_000.0
    assert quotes[0].source_time.isoformat() == "2026-07-16T10:00:00+08:00"
    assert session.calls[0][1]["timeout"] == 0.75


def test_tencent_quote_without_supplier_timestamp_uses_shanghai_fallback() -> None:
    fields = [""] * 50
    fields[1:5] = ["测试股份", "600001", "12.00", "11.65"]
    body = f'v_sh600001="{"~".join(fields)}";'.encode("gb18030")
    client = TencentClient(
        worker_pool=None,
        timeout_seconds=2,
        session_factory=lambda: FakeSession([body]),
        wall_clock=lambda: NOW.astimezone(timezone.utc),
    )

    quote = client.fetch_quotes(("600001",))[0]

    assert quote.source_time == NOW
    assert quote.source_time.tzinfo == ZoneInfo("Asia/Shanghai")


def test_tencent_full_market_rejects_partial_coverage_and_preserves_targeted_partial_results() -> None:
    fields = [""] * 50
    fields[1:5] = ["测试股份", "600001", "12.00", "11.65"]
    fields[30] = "20260716100000"
    body = f'v_sh600001="{"~".join(fields)}";'.encode("gb18030")
    session = FakeSession([body, body])
    client = TencentClient(worker_pool=None, timeout_seconds=2, session_factory=lambda: session)

    assert len(client.fetch_quotes(("600001", "600002"))) == 1
    with pytest.raises(RuntimeError, match="coverage is incomplete"):
        client.fetch_market(("600001", "600002"))


@pytest.mark.parametrize("receipt_delay", (timedelta(seconds=2), timedelta(days=2)))
def test_tencent_full_market_preserves_supplier_time_and_uses_completion_only_for_batch_identity(receipt_delay) -> None:
    fields = [""] * 50
    fields[1:5] = ["测试股份", "600001", "12.00", "11.65"]
    fields[30] = "20260716100000"
    body = f'v_sh600001="{"~".join(fields)}";'.encode("gb18030")
    session = FakeSession([body, body])
    received_at = NOW + receipt_delay
    client = TencentClient(
        worker_pool=None,
        timeout_seconds=2,
        session_factory=lambda: session,
        wall_clock=lambda: received_at,
    )

    targeted = client.fetch_quotes(("600001",))[0]
    market = client.fetch_market(("600001",))[0]

    assert targeted.source_time == NOW
    assert market.source_time == targeted.source_time
    assert market.received_time == received_at
    assert market.data_version == f"tencent-market:{int(received_at.timestamp())}"


def test_tencent_full_market_requires_official_universe_and_stops_on_cancel() -> None:
    session = FakeSession([])
    client = TencentClient(worker_pool=None, timeout_seconds=2, session_factory=lambda: session)
    unavailable = TencentFullMarketSource(client, lambda: ())
    with pytest.raises(RuntimeError, match="official market universe is unavailable"):
        unavailable.fetch_market()

    cancelled = threading.Event()
    cancelled.set()
    source = TencentFullMarketSource(client, lambda: ("600001",))
    with pytest.raises(RuntimeError, match="hedge cancelled"):
        source.fetch_market(cancel_event=cancelled)
    assert not session.calls


def test_tencent_full_market_deadline_uses_monotonic_time() -> None:
    fields = [""] * 50
    fields[1:5] = ["测试股份", "600001", "12.00", "11.65"]
    fields[30] = "20260716100000"
    body = f'v_sh600001="{"~".join(fields)}";'.encode("gb18030")
    elapsed = [0.0]

    class SlowSession(FakeSession):
        def get(self, url, **kwargs):
            response = super().get(url, **kwargs)
            elapsed[0] = 2.0
            return response

    session = SlowSession([body])
    client = TencentClient(
        worker_pool=None,
        timeout_seconds=2,
        session_factory=lambda: session,
        wall_clock=lambda: NOW,
        monotonic=lambda: elapsed[0],
    )

    with pytest.raises(RuntimeError, match="deadline exceeded"):
        client.fetch_market(("600001",), deadline=NOW + timedelta(seconds=1))
    assert len(session.calls) == 1


def test_tencent_receipt_time_is_captured_after_the_response() -> None:
    clock = [NOW]
    fields = [""] * 50
    fields[1:5] = ["测试股份", "600001", "12.00", "11.65"]
    fields[30] = "20260716100001"
    body = f'v_sh600001="{"~".join(fields)}";'.encode("gb18030")

    class DelayedSession(FakeSession):
        def get(self, url, **kwargs):
            clock[0] = NOW + timedelta(seconds=2)
            return super().get(url, **kwargs)

    session = DelayedSession([body])
    client = TencentClient(
        worker_pool=None,
        timeout_seconds=3,
        session_factory=lambda: session,
        wall_clock=lambda: clock[0],
    )
    quote = client.fetch_quotes(("600001",))[0]
    assert quote.received_time == NOW + timedelta(seconds=2)
    assert quote.source_time == NOW + timedelta(seconds=1)


def test_tencent_targeted_quotes_use_three_bounded_shards() -> None:
    state = {"active": 0, "maximum": 0, "urls": []}
    lock = threading.Lock()

    class ConcurrentSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def get(self, url, **_kwargs):
            with lock:
                state["active"] += 1
                state["maximum"] = max(state["maximum"], state["active"])
                state["urls"].append(url)
            try:
                time.sleep(0.02)
                records = []
                for symbol in url.split("q=", 1)[1].split(","):
                    fields = [""] * 50
                    fields[1] = symbol
                    fields[2] = symbol[2:]
                    fields[3] = "12.00"
                    fields[4] = "11.65"
                    fields[30] = "20260716100000"
                    records.append(f'v_{symbol}="{"~".join(fields)}";')
                return FakeResponse("".join(records).encode("gb18030"))
            finally:
                with lock:
                    state["active"] -= 1

    pool = BoundedExecutor(worker_count=3, queue_capacity=3, thread_name_prefix="tencent-test")
    pool.start()
    try:
        codes = tuple(f"{600000 + index:06d}" for index in range(241))
        quotes = TencentClient(
            timeout_seconds=2,
            session_factory=ConcurrentSession,
            worker_pool=pool,
        ).fetch_quotes(codes, NOW)
    finally:
        pool.stop()

    assert len(quotes) == 241
    assert len(state["urls"]) == 3
    assert state["maximum"] == 3
    assert all(url.count(",") <= 119 for url in state["urls"])


def test_tencent_targeted_quotes_keep_successful_shards_when_one_shard_fails() -> None:
    requested_codes = tuple(f"{600000 + index:06d}" for index in range(241))

    class PartialSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def get(self, url, **_kwargs):
            symbols = url.split("q=", 1)[1].split(",")
            if any(symbol.endswith("600120") for symbol in symbols):
                raise HttpTimeout("one shard timed out")
            records = []
            for symbol in symbols:
                fields = [""] * 50
                fields[1] = symbol
                fields[2] = symbol[2:]
                fields[3] = "12.00"
                fields[4] = "11.65"
                fields[30] = "20260716100000"
                records.append(f'v_{symbol}="{"~".join(fields)}";')
            return FakeResponse("".join(records).encode("gb18030"))

    quotes = TencentClient(
        worker_pool=None,
        timeout_seconds=2,
        session_factory=PartialSession,
    ).fetch_quotes(requested_codes, NOW)

    assert len(quotes) == 121
    assert quotes[0].code == "600000"
    assert quotes[-1].code == "600240"
    assert {quote.code for quote in quotes}.isdisjoint({f"{600120 + index:06d}" for index in range(120)})


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
        worker_pool=None,
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
    assert ",2026-07-16,20,qfq" in session.calls[0][1]["params"]["param"]
    assert session.calls[0][1]["params"]["param"].endswith(",20,qfq")


def test_tencent_outcome_history_requests_raw_and_qfq_from_one_source() -> None:
    qfq_rows = [
        ["2026-07-14", "5.00", "5.00", "5.10", "4.90", "1000", {}, "1.0", "6000"],
        ["2026-07-15", "4.50", "4.50", "4.50", "4.50", "1000", {}, "1.0", "6000"],
    ]
    raw_rows = [
        ["2026-07-14", "10.00", "10.00", "10.20", "9.80", "1000", {}, "1.0", "6000"],
        ["2026-07-15", "9.00", "9.00", "9.00", "9.00", "1000", {}, "1.0", "6000"],
    ]
    qfq_body = "kline_dayqfq2026=" + json.dumps({"data": {"sh600001": {"qfqday": qfq_rows}}})
    raw_body = "kline_day2026=" + json.dumps({"data": {"sh600001": {"day": raw_rows}}})
    session = FakeSession([qfq_body, raw_body])
    client = TencentClient(worker_pool=None, timeout_seconds=2, session_factory=lambda: session)

    bars = client.fetch_outcome_history("600001", days=20)

    assert [call[1]["params"]["param"].rsplit(",", 1)[-1] for call in session.calls] == ["qfq", "bfq"]
    assert all(",20," in call[1]["params"]["param"] for call in session.calls)
    assert bars[0].qfq.close == 5.0
    assert bars[0].raw.close == 10.0
    assert bars[1].trading_status is OutcomeTradingStatus.ONE_PRICE_LIMIT_DOWN
    assert {bar.source for bar in bars} == {"tencent"}


def test_tencent_outcome_history_keeps_only_complete_same_date_pairs() -> None:
    qfq_rows = [
        ["2026-07-14", "5.00", "5.00", "5.10", "4.90", "1000", {}, "1.0", "6000"],
        ["2026-07-15", "5.00", "5.10", "5.20", "4.90", "1000", {}, "1.0", "6000"],
    ]
    raw_rows = [["2026-07-14", "10.00", "10.00", "10.20", "9.80", "1000", {}, "1.0", "6000"]]
    qfq_body = "kline_dayqfq2026=" + json.dumps({"data": {"sh600001": {"qfqday": qfq_rows}}})
    raw_body = "kline_day2026=" + json.dumps({"data": {"sh600001": {"day": raw_rows}}})
    session = FakeSession([qfq_body, raw_body])
    client = TencentClient(worker_pool=None, timeout_seconds=2, session_factory=lambda: session)

    bars = client.fetch_outcome_history("600001", days=20)

    assert tuple(bar.trade_date for bar in bars) == ("2026-07-14",)


def test_tencent_history_rejects_unadjusted_day_payload_when_qfq_is_missing() -> None:
    rows = [["2026-07-15", "10", "11", "12", "9", "1000", {}, "0.3", "6000"]]
    body = "kline_dayqfq2026=" + json.dumps({"data": {"sh600001": {"day": rows}}})
    client = TencentClient(worker_pool=None, timeout_seconds=2, session_factory=lambda: FakeSession([body]))

    assert client.fetch_history("600001", days=20) == ()


def test_tencent_history_supports_a_separate_direct_probe_host() -> None:
    session = FakeSession(['kline_dayqfq2026={"data": {"sh600001": {"qfqday": []}}}'])
    client = TencentClient(worker_pool=None, timeout_seconds=2, session_factory=lambda: session)

    assert client.fetch_history("600001", days=20, history_host="direct") == ()
    assert session.calls[0][0][0].startswith("https://web.ifzq.gtimg.cn/")

    with pytest.raises(ValueError, match="host"):
        client.fetch_history("600001", days=20, history_host="unknown")


def test_tencent_history_accepts_day_payload_proven_equivalent_to_requested_qfq() -> None:
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
            "0.00",
            "0.00",
        ]
        for index in range(21)
    ]
    body = "kline_dayqfq2026=" + json.dumps({"data": {"sh600001": {"day": rows}}})
    client = TencentClient(worker_pool=None, timeout_seconds=2, session_factory=lambda: FakeSession([body]))

    bars = client.fetch_history("600001", days=20)

    assert len(bars) == 20
    assert all(bar.adjustment is PriceAdjustment.QFQ for bar in bars)


@pytest.mark.parametrize(
    "invalid_tail",
    (
        ({"djr": "2026-06-15"}, "0.00", "0.00"),
        ({}, "1.00", "0.00"),
        ({}, "0.00", "1.00"),
    ),
)
def test_tencent_history_rejects_day_payload_without_zero_adjustment_proof(
    invalid_tail: tuple[object, str, str],
) -> None:
    corporate_action, first_adjustment, second_adjustment = invalid_tail
    rows = [
        [
            "2026-07-15",
            "10",
            "11",
            "12",
            "9",
            "1000",
            corporate_action,
            "0.3",
            "6000",
            first_adjustment,
            second_adjustment,
        ]
    ]
    body = "kline_dayqfq2026=" + json.dumps({"data": {"sh600001": {"day": rows}}})
    client = TencentClient(worker_pool=None, timeout_seconds=2, session_factory=lambda: FakeSession([body]))

    assert client.fetch_history("600001", days=20) == ()


def test_history_fallback_uses_eastmoney_only_when_tencent_is_insufficient() -> None:
    primary = CountingHistoryClient(())
    fallback = CountingHistoryClient(_history_bars())

    bars = FallbackHistoryClient(primary, fallback).fetch_history("600001", days=90)

    assert len(bars) == 60
    assert primary.calls == ["600001"]
    assert fallback.calls == ["600001"]


def test_outcome_history_fallback_keeps_raw_and_qfq_from_one_provider() -> None:
    prices = OutcomePrice(10.0, 10.2, 9.8, 10.0)
    expected = (OutcomeBar("2026-07-15", prices, prices, OutcomeTradingStatus.TRADABLE, "fallback"),)

    class OutcomeClient(CountingHistoryClient):
        def __init__(self, outcome_bars) -> None:
            super().__init__(())
            self.outcome_bars = outcome_bars
            self.outcome_calls = []

        def fetch_outcome_history(self, code, *, days):
            self.outcome_calls.append((code, days))
            return self.outcome_bars

    primary = OutcomeClient(())
    fallback = OutcomeClient(expected)

    bars = FallbackHistoryClient(primary, fallback, minimum_rows=1).fetch_outcome_history("600001", days=20)

    assert bars == expected
    assert primary.outcome_calls == [("600001", 20)]
    assert fallback.outcome_calls == [("600001", 20)]


def test_sina_market_request_uses_injected_transport() -> None:
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
                    "volume": "25000000",
                    "mktcap": "3000000",
                }
            ],
        ]
    )
    client = SinaClient(worker_pool=None, timeout_seconds=2, session_factory=lambda: session)

    quotes = client.fetch_market(NOW)

    assert quotes[0].code == "600001"
    assert quotes[0].volume == 25_000_000.0


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

    state = {"active": 0, "maximum": 0, "factory_calls": 0, "lock": threading.Lock()}

    def session_factory():
        state["factory_calls"] += 1
        return ConcurrentSinaSession(state)

    pool = BoundedExecutor(worker_count=3, queue_capacity=3, thread_name_prefix="sina-pages-test")
    client = SinaClient(
        worker_pool=pool,
        timeout_seconds=2,
        workers=3,
        session_factory=session_factory,
    )

    pool.start()
    try:
        quotes = client.fetch_market(NOW)
    finally:
        pool.stop()

    assert len(quotes) == 201
    assert state["maximum"] >= 2
    assert state["factory_calls"] == 1


def test_sina_refills_a_free_worker_before_the_slowest_page_finishes() -> None:
    third_started = threading.Event()

    class UnevenPages:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, url, **kwargs):
            if "getHQNodeStockCount" in url:
                return FakeResponse(b"201")
            page = int(kwargs["params"]["page"])
            if page == 1:
                assert third_started.wait(2), "an idle worker must not wait for the slow first page"
            if page == 3:
                third_started.set()
            return FakeResponse(
                [
                    {"symbol": f"sh{600000 + (page - 1) * 100 + index:06d}", "trade": "12.00"}
                    for index in range(100 if page < 3 else 1)
                ]
            )

    pool = BoundedExecutor(worker_count=2, queue_capacity=2, thread_name_prefix="sina-refill-test")
    client = SinaClient(worker_pool=pool, timeout_seconds=2, workers=2, page_size=100, session_factory=UnevenPages)
    pool.start()
    try:
        quotes = client.fetch_market(NOW)
    finally:
        pool.stop()
    assert len(quotes) == 201
    assert len({quote.code for quote in quotes}) == 201


def test_market_clients_reject_stopped_injected_pool_before_creating_http_session() -> None:
    pool = BoundedExecutor(worker_count=1, queue_capacity=1, thread_name_prefix="stopped-vendor-test")
    session_calls = []

    def session_factory():
        session_calls.append(True)
        raise AssertionError("stopped clients must not create a session")

    clients = (
        SinaClient(timeout_seconds=2, session_factory=session_factory, worker_pool=pool),
        TencentClient(timeout_seconds=2, session_factory=session_factory, worker_pool=pool),
    )
    for client in clients:
        with pytest.raises(RuntimeError, match="executor stopped"):
            if isinstance(client, TencentClient):
                client.fetch_quotes(("600001",), NOW)
            else:
                client.fetch_market(NOW)
    assert session_calls == []


def test_page_submission_failure_drains_workers_before_closing_session(monkeypatch) -> None:
    release = threading.Event()
    finished = threading.Event()

    class RejectingPool(BoundedExecutor):
        submissions = 0

        def submit(self, function, /, *args, **kwargs):
            self.submissions += 1
            if self.submissions == 2:
                release.set()
                raise RuntimeError("controlled_submission_failure")
            return super().submit(function, *args, **kwargs)

    class Session:
        closed = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            assert finished.is_set()
            self.closed = True

    session = Session()
    pool = RejectingPool(worker_count=2, queue_capacity=2, thread_name_prefix="page-drain-test")
    client = SinaClient(timeout_seconds=2, workers=2, worker_pool=pool, session_factory=lambda: session)

    def fetch_page(page, **_kwargs):
        assert release.wait(2)
        assert not session.closed
        finished.set()
        return {}

    monkeypatch.setattr(client, "_fetch_page", fetch_page)
    monkeypatch.setattr(client, "_get_text", lambda *_args, **_kwargs: "201")
    pool.start()
    try:
        with pytest.raises(RuntimeError, match="controlled_submission_failure"):
            client.fetch_market(NOW)
        assert session.closed
        assert pool.status().inflight == 0
    finally:
        release.set()
        pool.stop()


def test_sina_market_source_retries_transient_page_504() -> None:
    sina_session = FakeSession(
        [
            b"1",
            HttpStatusError(504),
            [{"symbol": "sh600001", "name": "测试股份", "trade": "12.00"}],
        ]
    )
    sina = SinaClient(worker_pool=None, timeout_seconds=2, session_factory=lambda: sina_session)

    assert sina.fetch_market(NOW)[0].code == "600001"
    assert len(sina_session.calls) == 3


def test_targeted_partial_result_keeps_sina_full_market_quote_for_missing_code() -> None:
    sina_first = replace(_quote("600001"), source="sina", data_version="sina-full")
    sina_second = replace(_quote("600002"), source="sina", data_version="sina-full")
    tencent_first = replace(_quote("600001"), source="tencent", data_version="tencent-targeted")
    gateway = MarketDataGateway(
        FailingMarketClient(),
        StaticMarketClient((sina_first, sina_second)),
        StaticTencentClient((tencent_first,)),
        minimum_market_rows=1,
        circuit_breaker_failures=1,
        circuit_breaker_seconds=60,
        wall_clock=lambda: NOW,
    )

    gateway.fetch_market(observed_at=NOW)
    fetched = gateway.fetch_candidates(("600001", "600002"), observed_at=NOW)

    assert {item.code: item.source for item in fetched} == {
        "600001": "tencent",
        "600002": "sina",
    }
    assert gateway.health().route is not None
    assert gateway.health().route.vendor == "sina"


def test_market_data_router_prefers_no_data_over_failures() -> None:
    def empty_payload() -> tuple[MarketQuote, ...]:
        return ()

    with pytest.raises(MarketDataNoDataError, match="insufficient rows") as exc_info:
        Route(
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

    with pytest.raises(MarketDataFailedError, match=r"eastmoney: offline; sina: offline") as exc_info:
        Route(
            (
                VendorRoute("eastmoney", failing, VendorSeverity.REQUIRED),
                VendorRoute("sina", failing, VendorSeverity.REQUIRED),
            )
        )
    assert str(exc_info.value).startswith("sina: ")


def test_gateway_primary_success_does_not_start_sina_backup() -> None:
    eastmoney = CountingMarketClient((replace(_quote(), source="eastmoney"),))
    sina = CountingMarketClient((replace(_quote(), source="sina", price=12.01),))
    pool = BoundedExecutor(worker_count=5, queue_capacity=5, thread_name_prefix="source-data")
    pool.start()
    lanes = SourceLaneScheduler(pool)
    gateway = MarketDataGateway(
        eastmoney,
        sina,
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        worker_pool=pool,
        source_lanes=lanes,
        full_market_primary_timeout_seconds=0.05,
        wall_clock=lambda: NOW,
    )

    try:
        result = tuple(gateway.fetch_market(observed_at=NOW))
    finally:
        lanes.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)

    assert result[0].price == 12.0
    assert eastmoney.calls == 1
    assert sina.calls == 0
    assert gateway.health().merge_count == 1
    assert gateway.health().route is not None
    assert gateway.health().route.results[1].error == "backup_not_needed"


def test_gateway_starts_sina_after_primary_timeout_without_waiting_for_slow_primary() -> None:
    eastmoney = BlockingMarketClient((replace(_quote(), source="eastmoney"),))
    sina = CountingMarketClient((replace(_quote(), source="sina", price=12.01),))
    pool = BoundedExecutor(worker_count=5, queue_capacity=5, thread_name_prefix="source-data")
    pool.start()
    lanes = SourceLaneScheduler(pool)
    gateway = MarketDataGateway(
        eastmoney,
        sina,
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=30,
        worker_pool=pool,
        source_lanes=lanes,
        full_market_primary_timeout_seconds=0.02,
        wall_clock=lambda: NOW,
    )

    started = time.monotonic()
    try:
        result = tuple(
            gateway.fetch_market(
                observed_at=NOW,
                deadline=NOW + timedelta(seconds=0.5),
            )
        )
        elapsed = time.monotonic() - started
    finally:
        eastmoney.release.set()
        lanes.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)

    assert result[0].price == 12.01
    assert elapsed < 0.3
    assert eastmoney.calls == 1
    assert sina.calls == 1
    assert gateway.health().route is not None
    assert gateway.health().route.vendor == "sina"
    assert gateway.health().route.fallback_reason == "primary_failed"
    assert "tencent_market:late" in gateway.canonical_snapshot().degraded_reasons
    assert "eastmoney:source_failed" not in gateway.canonical_snapshot().degraded_reasons


def test_late_primary_does_not_overwrite_sina_backup_quote() -> None:
    completed = threading.Event()

    class LateMarketClient(BlockingMarketClient):
        def fetch_market(self):
            result = super().fetch_market()
            completed.set()
            return result

    market_quote = replace(_quote(), source="tencent")
    tencent_market = LateMarketClient((market_quote,))
    sina = CountingMarketClient((replace(_quote(), source="sina", price=12.01),))
    pool = BoundedExecutor(worker_count=5, queue_capacity=5, thread_name_prefix="source-data")
    pool.start()
    lanes = SourceLaneScheduler(pool)
    gateway = MarketDataGateway(
        tencent_market,
        sina,
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=30,
        worker_pool=pool,
        source_lanes=lanes,
        full_market_primary_timeout_seconds=0.02,
        wall_clock=lambda: NOW,
    )
    observed_at = NOW
    deadline = observed_at + timedelta(seconds=0.5)

    try:
        result = tuple(gateway.fetch_market(observed_at=observed_at, deadline=deadline))
        time.sleep(0.1)
        tencent_market.release.set()
        assert completed.wait(1.0)
    finally:
        tencent_market.release.set()
        lanes.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)

    assert result[0].source == "sina"
    assert gateway.current_quotes((market_quote.code,))[0].source == "sina"
    assert gateway.reference_observations((market_quote.code,)) == ()
    assert tencent_market.calls == 1


def test_candidate_feature_service_prefers_newer_tencent_quote_over_market_quote() -> None:
    market_quote = replace(_quote(), source="tencent", price=12.0, data_version="z-market")
    sina = replace(_quote(), source="sina", price=12.01, data_version="z-sina")
    tencent = replace(
        _quote(),
        source="tencent",
        price=12.02,
        source_time=NOW + timedelta(seconds=1),
        received_time=NOW + timedelta(seconds=1),
        data_version="a-tencent",
    )
    gateway = MarketDataGateway(
        StaticMarketClient((market_quote,)),
        StaticMarketClient((sina,)),
        StaticTencentClient((tencent,)),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        wall_clock=lambda: NOW + timedelta(seconds=1),
    )
    service = _service(
        gateway,
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        wall_clock=lambda: NOW,
    )
    service.fetch_market_features(NOW)

    refreshed = tuple(service.refresh_candidate_quotes(("600001",), NOW + timedelta(seconds=1)))

    assert refreshed[0].quote.source == "tencent"
    assert refreshed[0].quote.price == 12.02
    assert refreshed[0].quote.data_version == "a-tencent"
