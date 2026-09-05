from __future__ import annotations

from tests.component.market_data_test_support import (
    LONG_POLICY,
    MARKET_REGIME_POLICY,
    NEWS_POLICY,
    NOW,
    TAIL_POLICY,
    BoundedExecutor,
    BoundedLruCache,
    FakeTushareFrame,
    FakeTusharePro,
    FeatureBuilder,
    MutableMonotonic,
    Path,
    ReferenceLoadRequest,
    SourceLaneRegistry,
    SourceObservation,
    StaticGateway,
    StaticHistoryClient,
    TushareClient,
    _quote,
    _ReferenceLoadOptions,
    _service,
    date,
    load_runtime_settings,
    pytest,
    replace,
    threading,
    timedelta,
    tushare_records_module,
)


def test_tushare_reference_version_uses_response_time_before_hash_order() -> None:
    service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
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

    service.references.apply_fields("valuation", (newer,))
    service.references.apply_fields("valuation", (older,))

    assert service.references.versions()["valuation"] == "a-newer"


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
    assert client.health().degraded_reason == "missing_token"
    assert client.health().consecutive_failures == 1


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
    assert client.health().degraded_reason == expected_code
    assert client.health().consecutive_failures == 1


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
    assert client.health().circuit_open is True
    assert client.fetch_security_master(NOW)[0].error_code == "circuit_open"
    assert factory_calls == 3

    monotonic.value = 60.001
    should_fail = False
    recovered = client.fetch_security_master(NOW)

    assert recovered[0].status == "success"
    assert factory_calls == 4
    assert client.health().circuit_open is False
    assert client.health().consecutive_failures == 0


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
    assert lanes.status().lanes["tushare"].running is False


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
    assert all(item.fields.get("price_adjustment") == "qfq" for item in observations if item.status == "success")
    assert any(
        item.status == "failed" and item.subject_key == "600002" and item.error_code == "timeout"
        for item in observations
    )


def test_tushare_120_point_profile_uses_bounded_free_daily_and_disables_paid_references() -> None:
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
    assert all(item.fields.get("price_adjustment") == "raw" for item in observations)
    assert [call["ts_code"] for call in pro.calls] == ["600001.SH", "300001.SZ", "688001.SH"]
    assert client.supports("daily_history") is True
    assert client.supports("security_master") is False
    assert client.supports("trading_calendar") is False
    assert client.supports("daily_valuation") is False
    assert client.supports("financial_indicators") is False
    health = client.health()
    assert health.access_points == 120
    assert health.history_mode == "unadjusted_daily"
    assert health.minute_call_limit == 50
    assert health.daily_call_limit == 8000
    assert health.process_api_attempts_last_minute == 3
    assert health.process_api_attempts_today == 3
    assert health.process_remaining_calls_today == 7997
    assert health.local_rate_limit_count == 0


def test_tushare_120_point_profile_stops_before_exceeding_minute_quota() -> None:
    class FreeDailyPro:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def daily(self, **kwargs):
            code = str(kwargs["ts_code"])
            self.calls.append(code)
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

    pro = FreeDailyPro()
    client = TushareClient(
        token="secret-token",
        timeout_seconds=8,
        points=120,
        sdk_factory=lambda _token, _timeout: pro,
        wall_clock=lambda: NOW,
        monotonic=lambda: 10.0,
    )
    codes = tuple(f"{index:06d}" for index in range(1, 52))

    observations = client.fetch_daily_history(codes, date(2026, 7, 1), date(2026, 7, 16), NOW)

    assert len(pro.calls) == 50
    assert len([item for item in observations if item.status == "success"]) == 50
    assert observations[-1].status == "failed"
    assert observations[-1].error_code == "local_rate_limit"
    health = client.health()
    assert health.process_api_attempts_last_minute == 50
    assert health.process_api_attempts_today == 50
    assert health.process_remaining_calls_today == 7950
    assert health.local_rate_limit_count == 1


def test_tushare_daily_empty_code_result_is_an_explicit_failure() -> None:
    class EmptyDailyPro:
        @staticmethod
        def daily(**_kwargs):
            return FakeTushareFrame([])

    client = TushareClient(
        token="secret-token",
        timeout_seconds=8,
        points=120,
        sdk_factory=lambda _token, _timeout: EmptyDailyPro(),
        wall_clock=lambda: NOW,
    )

    observations = client.fetch_daily_history(("600001",), date(2026, 7, 1), date(2026, 7, 16), NOW)

    assert observations[0].subject_key == "600001"
    assert observations[0].status == "failed"
    assert observations[0].error_code == "no_data"


def test_tushare_default_daily_transport_uses_direct_https_without_environment_proxy(monkeypatch) -> None:
    class DirectResponse:
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

    class DirectSession:
        def __init__(self) -> None:
            self.trust_env = True
            self.calls: list[tuple[str, dict[str, object]]] = []

        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return DirectResponse()

    session = DirectSession()
    module = type("FakeTushareModule", (), {"pro_api": staticmethod(lambda _token, timeout: object())})()
    monkeypatch.setattr(tushare_records_module.requests, "Session", lambda: session)
    monkeypatch.setattr(tushare_records_module.importlib, "import_module", lambda _name: module)
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


def test_tushare_daily_transport_preserves_only_numeric_provider_error_code(monkeypatch) -> None:
    class ErrorResponse:
        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json():
            return {"code": "-2002", "msg": "sensitive vendor response must not escape"}

    class DirectSession:
        trust_env = False

        @staticmethod
        def post(_url, **_kwargs):
            return ErrorResponse()

    module = type("FakeTushareModule", (), {"pro_api": staticmethod(lambda _token, timeout: object())})()
    monkeypatch.setattr(tushare_records_module.requests, "Session", lambda: DirectSession())
    monkeypatch.setattr(tushare_records_module.importlib, "import_module", lambda _name: module)
    client = TushareClient(token="secret-token", points=120, timeout_seconds=8, wall_clock=lambda: NOW)

    observations = client.fetch_daily_history(("600001",), date(2026, 7, 1), date(2026, 7, 16), NOW)

    assert observations[0].error_code == "provider_error_-2002"
    assert "sensitive" not in str(observations[0])


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


def test_tushare_negative_refresh_marks_preserved_reference_data_degraded() -> None:
    runtime = load_runtime_settings(Path(__file__).parents[2] / "config" / "runtime.json")
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
    service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
        tushare_client=client,
        cache=cache,
        source_contract_versions=runtime.market_data.source_contract_versions,
        config_version=runtime.config_version,
        wall_clock=lambda: NOW,
    )
    request = {"dataset": "security_master", "market": "ashare"}

    reference_request = ReferenceLoadRequest(
        "security_master_calendar",
        "security_master",
        request,
        _ReferenceLoadOptions(
            observed_at=NOW,
            function=client.fetch_security_master,
            args=(NOW,),
            force=False,
            kwargs={},
        ),
    )
    first = service.references.load(reference_request)
    pro.fail = True
    service.references.load(replace(reference_request, force=True))
    preserved = service.references.load(reference_request)

    assert "reference_data_degraded" not in first[0].fields
    assert preserved[0].fields["board_reliability"] == "degraded"
    assert preserved[0].fields["reference_data_degraded"] is True
