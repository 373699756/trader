from __future__ import annotations

from tests.component.market_data_test_support import (
    LONG_POLICY,
    MARKET_REGIME_POLICY,
    NEWS_POLICY,
    NOW,
    TAIL_POLICY,
    BlockingMarketClient,
    Board,
    BoundedExecutor,
    CountingHistoryClient,
    CountingMarketClient,
    DailyBar,
    DataPlaneRepository,
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
    Path,
    PriceAdjustment,
    SinaClient,
    SourceLaneRegistry,
    SourceObservation,
    StaticGateway,
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
    requests,
    route,
    threading,
    time,
    timedelta,
    timezone,
)


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
                    "f13": 1,
                    "f14": "测试股份",
                    "f15": 12.2,
                    "f16": 11.7,
                    "f17": 11.8,
                    "f18": 11.65,
                    "f20": 30000000000,
                    "f22": 0.8,
                    "f26": 19991110,
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
    assert quotes[0].board is Board.MAIN
    assert quotes[0].board_source == "eastmoney"
    assert quotes[0].board_reliability == "reported"
    assert quotes[0].exchange == "SSE"
    assert quotes[0].listing_date == date(1999, 11, 10)
    assert quotes[0].source_time == NOW - timedelta(minutes=1)
    assert quotes[0].data_version == f"eastmoney:{int(NOW.timestamp())}"
    assert history[0].amount == 100000000
    assert all(call[1]["proxies"] == {"http": "", "https": "", "all": ""} for call in session.calls)


def test_eastmoney_history_fallback_attempts_each_host_once() -> None:
    session = FakeSession([requests.Timeout("slow host")] * 3)
    client = EastmoneyClient(timeout_seconds=2, session_factory=lambda: session)

    with pytest.raises(RuntimeError, match="eastmoney request failed"):
        client.fetch_history("600001", days=90, now=NOW)

    assert len(session.calls) == 3
    assert len({call[0][0].split("/", 3)[2] for call in session.calls}) == 3


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
    fields[45] = "120.5"
    fields[49] = "2.0"
    body = f'v_sh600001="{"~".join(fields)}";'.encode("gb18030")
    session = FakeSession([body])
    client = TencentClient(timeout_seconds=2, session_factory=lambda: session)

    quotes = client.fetch_quotes(["600001"], NOW, timeout_seconds=0.75)

    assert quotes[0].price == 12.0
    assert quotes[0].amount == 300000000.0
    assert quotes[0].market_cap == 12_050_000_000.0
    assert quotes[0].source_time.isoformat() == "2026-07-16T10:00:00+08:00"
    assert session.calls[0][1]["timeout"] == 0.75
    assert session.calls[0][1]["proxies"] == {"http": "", "https": "", "all": ""}


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


def test_tencent_history_rejects_unadjusted_day_payload_when_qfq_is_missing() -> None:
    rows = [["2026-07-15", "10", "11", "12", "9", "1000", {}, "0.3", "6000"]]
    body = "kline_dayqfq2026=" + json.dumps({"data": {"sh600001": {"day": rows}}})
    client = TencentClient(timeout_seconds=2, session_factory=lambda: FakeSession([body]))

    assert client.fetch_history("600001", days=20) == ()


def test_tencent_history_supports_a_separate_direct_probe_host() -> None:
    session = FakeSession(['kline_dayqfq2026={"data": {"sh600001": {"qfqday": []}}}'])
    client = TencentClient(timeout_seconds=2, session_factory=lambda: session)

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
    client = TencentClient(timeout_seconds=2, session_factory=lambda: FakeSession([body]))

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
    client = TencentClient(timeout_seconds=2, session_factory=lambda: FakeSession([body]))

    assert client.fetch_history("600001", days=20) == ()


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

    state = {"active": 0, "maximum": 0, "factory_calls": 0, "lock": threading.Lock()}

    def session_factory():
        state["factory_calls"] += 1
        return ConcurrentSinaSession(state)

    client = SinaClient(
        timeout_seconds=2,
        workers=3,
        session_factory=session_factory,
    )

    quotes = client.fetch_market(NOW)

    assert len(quotes) == 201
    assert state["maximum"] >= 2
    assert state["factory_calls"] == 1


def test_eastmoney_full_market_reuses_one_session_and_stops_before_expired_deadline() -> None:
    session = FakeSession(
        [
            {
                "data": {
                    "total": 501,
                    "diff": [
                        {"f12": f"{600000 + index:06d}", "f14": "测试股份", "f124": int(NOW.timestamp())}
                        for index in range(500)
                    ],
                }
            },
            {
                "data": {
                    "total": 501,
                    "diff": [{"f12": "600500", "f14": "测试股份", "f124": int(NOW.timestamp())}],
                }
            },
        ]
    )
    factory_calls = 0

    def session_factory():
        nonlocal factory_calls
        factory_calls += 1
        return session

    client = EastmoneyClient(
        timeout_seconds=2,
        session_factory=session_factory,
        wall_clock=lambda: NOW,
    )

    assert len(client.fetch_market(NOW, deadline=NOW + timedelta(seconds=1))) == 501
    assert factory_calls == 1

    with pytest.raises(RuntimeError, match="deadline"):
        client.fetch_market(NOW, deadline=NOW)
    assert factory_calls == 1


def test_eastmoney_pages_remain_parallel_when_called_from_source_worker() -> None:
    class ConcurrentEastmoneySession:
        def __init__(self) -> None:
            self.active = 0
            self.maximum = 0
            self.lock = threading.Lock()

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def get(self, _url, **kwargs):
            page = int(kwargs["params"]["pn"])
            if page > 1:
                with self.lock:
                    self.active += 1
                    self.maximum = max(self.maximum, self.active)
                time.sleep(0.02)
                with self.lock:
                    self.active -= 1
            row_count = 500 if page < 3 else 1
            start = (page - 1) * 500
            return FakeResponse(
                {
                    "data": {
                        "total": 1001,
                        "diff": [
                            {
                                "f12": f"{600000 + start + index:06d}",
                                "f14": "测试股份",
                                "f124": int(NOW.timestamp()),
                            }
                            for index in range(row_count)
                        ],
                    }
                }
            )

    session = ConcurrentEastmoneySession()
    pool = BoundedExecutor(worker_count=3, queue_capacity=3, thread_name_prefix="source-data")
    client = EastmoneyClient(
        timeout_seconds=2,
        workers=2,
        worker_pool=pool,
        session_factory=lambda: session,
    )
    pool.start()
    try:
        future = pool.submit(client.fetch_market, NOW)
        assert future is not None
        quotes = future.result(timeout=1.0)
    finally:
        pool.stop(wait=True, cancel_futures=True)

    assert len(quotes) == 1001
    assert session.maximum == 2


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


def test_targeted_partial_result_keeps_sina_full_market_quote_for_missing_code() -> None:
    sina_first = replace(_quote("600001"), source="sina", data_version="sina-full-v1")
    sina_second = replace(_quote("600002"), source="sina", data_version="sina-full-v1")
    tencent_first = replace(_quote("600001"), source="tencent", data_version="tencent-targeted-v1")
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


def test_eastmoney_history_completion_cannot_overwrite_newer_tushare_history() -> None:
    eastmoney_bar = DailyBar(
        "2026-07-14",
        10.0,
        10.1,
        10.2,
        9.9,
        1000.0,
        10000.0,
        1.0,
        adjustment=PriceAdjustment.QFQ,
        source="eastmoney",
    )

    class BlockingHistory:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()

        def fetch_history(self, _code, *, days):
            assert days == 61
            self.started.set()
            assert self.release.wait(1.0)
            return (eastmoney_bar,)

    history = BlockingHistory()
    service = _service(
        StaticGateway((_quote(),)),
        history,
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
        wall_clock=lambda: NOW,
    )
    result: dict[str, tuple[DailyBar, ...]] = {}
    errors: list[BaseException] = []

    def load_eastmoney() -> None:
        try:
            result.update(service.history.load(("600001",)))
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
            "price_adjustment": "qfq",
        },
        missing_reasons={},
        payload_hash="tushare-history-v2",
        status="success",
        error_code=None,
    )
    service.references.apply_history((tushare,))
    history.release.set()
    thread.join(1.0)

    assert not thread.is_alive()
    assert errors == []
    assert result["600001"][-1].trade_date == "2026-07-15"
    assert service.history.entries()["600001"].bars[-1].trade_date == "2026-07-15"


def test_market_data_router_prefers_no_data_over_failures() -> None:
    def empty_payload() -> tuple[MarketQuote, ...]:
        return ()

    with pytest.raises(MarketDataNoDataError, match="insufficient rows") as exc_info:
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

    with pytest.raises(MarketDataFailedError, match=r"eastmoney: offline; sina: offline") as exc_info:
        route(
            (
                VendorRoute("eastmoney", failing, VendorSeverity.REQUIRED),
                VendorRoute("sina", failing, VendorSeverity.REQUIRED),
            )
        )
    assert str(exc_info.value).startswith("sina: ")


def test_gateway_primary_success_does_not_start_sina_hedge() -> None:
    eastmoney = CountingMarketClient((replace(_quote(), source="eastmoney"),))
    sina = CountingMarketClient((replace(_quote(), source="sina", price=12.01),))
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
        full_market_hedge_delay_seconds=0.05,
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
    assert gateway.health().route.results[1].error == "hedge_not_needed"


def test_gateway_starts_sina_after_hedge_delay_and_returns_without_waiting_for_slow_primary() -> None:
    eastmoney = BlockingMarketClient((replace(_quote(), source="eastmoney"),))
    sina = CountingMarketClient((replace(_quote(), source="sina", price=12.01),))
    pool = BoundedExecutor(worker_count=5, queue_capacity=5, thread_name_prefix="source-data")
    pool.start()
    lanes = SourceLaneRegistry(pool)
    gateway = MarketDataGateway(
        eastmoney,
        sina,
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=30,
        worker_pool=pool,
        source_lanes=lanes,
        full_market_hedge_delay_seconds=0.01,
    )

    started = time.monotonic()
    try:
        result = tuple(
            gateway.fetch_market(
                observed_at=datetime.now(timezone.utc),
                deadline=datetime.now(timezone.utc) + timedelta(seconds=0.5),
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
    assert gateway.health().route.fallback_reason == "hedge_delay"
    assert "eastmoney:hedge_delay" in gateway.canonical_snapshot().degraded_reasons
    assert "eastmoney:source_failed" not in gateway.canonical_snapshot().degraded_reasons


def test_late_eastmoney_hedge_preserves_security_identity_without_overwriting_sina_quote(
    tmp_path: Path,
) -> None:
    completed = threading.Event()

    class LateIdentityClient(BlockingMarketClient):
        def fetch_market(self):
            result = super().fetch_market()
            completed.set()
            return result

    eastmoney_quote = replace(
        _quote(),
        source="eastmoney",
        board=Board.MAIN,
        board_source="eastmoney",
        board_reliability="reported",
        exchange="SSE",
        listing_date=date(1999, 11, 10),
    )
    eastmoney = LateIdentityClient((eastmoney_quote,))
    sina = CountingMarketClient((replace(_quote(), source="sina", price=12.01),))
    pool = BoundedExecutor(worker_count=5, queue_capacity=5, thread_name_prefix="source-data")
    pool.start()
    lanes = SourceLaneRegistry(pool)
    gateway = MarketDataGateway(
        eastmoney,
        sina,
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=30,
        worker_pool=pool,
        source_lanes=lanes,
        full_market_hedge_delay_seconds=0.01,
    )
    observed_at = datetime.now(timezone.utc)
    deadline = observed_at + timedelta(seconds=0.08)

    try:
        result = tuple(gateway.fetch_market(observed_at=observed_at, deadline=deadline))
        time.sleep(0.1)
        eastmoney.release.set()
        assert completed.wait(1.0)
        timeout_at = time.monotonic() + 1.0
        while not gateway.reference_observations((eastmoney_quote.code,)) and time.monotonic() < timeout_at:
            time.sleep(0.01)
        references = gateway.reference_observations((eastmoney_quote.code,))
    finally:
        eastmoney.release.set()
        lanes.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)

    assert result[0].source == "sina"
    assert gateway.current_quotes((eastmoney_quote.code,))[0].source == "sina"
    assert len(references) == 1
    assert references[0].source == "eastmoney_security_master"
    assert references[0].fields["listing_date"] == "1999-11-10"
    assert eastmoney.calls == 1
    data_plane = DataPlaneRepository(tmp_path)
    service = _service(
        gateway,
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
        data_plane=data_plane,
        wall_clock=lambda: observed_at,
    )
    service.schedule_reference_data(
        (),
        observed_at,
        security_master_codes=(eastmoney_quote.code,),
    )
    persisted = data_plane.load_security_master_recent(eastmoney_quote.code)
    assert persisted is not None
    assert persisted.source == "eastmoney_security_master"
    assert persisted.payload["listing_date"] == "1999-11-10"


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
    service = _service(
        gateway,
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
        wall_clock=lambda: NOW,
    )
    service.fetch_market_features(NOW)

    refreshed = tuple(service.refresh_candidate_quotes(("600001",), NOW))

    assert refreshed[0].quote.source == "tencent"
    assert refreshed[0].quote.price == 12.02
    assert refreshed[0].quote.data_version == "a-tencent-v1"
