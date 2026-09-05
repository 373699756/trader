from __future__ import annotations

from tests.component.market_data_test_support import (
    LONG_POLICY,
    MARKET_REGIME_POLICY,
    NEWS_POLICY,
    NOW,
    TAIL_POLICY,
    BlockingMarketClient,
    BoundedExecutor,
    BoundedLruCache,
    CountingMarketClient,
    FailingMarketClient,
    FeatureBuilder,
    MarketDataDeadlineExceededError,
    MarketDataGateway,
    MarketDataUnavailableError,
    MarketQuote,
    MutableMonotonic,
    Path,
    SequenceMarketClient,
    StaticGateway,
    StaticGatewayWithSeparateQuotes,
    StaticHistoryClient,
    StaticMarketClient,
    StaticTencentClient,
    _quote,
    _service,
    gateway_module,
    load_runtime_settings,
    pytest,
    replace,
    threading,
    time,
    timedelta,
)


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

    assert health.active_source == "sina"
    assert health.sources["eastmoney"].circuit_open is True
    assert health.sources["eastmoney"].planned_count == 1
    assert health.sources["eastmoney"].error_count == 1
    assert health.sources["eastmoney"].p50_latency_ms is not None
    assert health.sources["eastmoney"].p95_latency_ms is not None
    assert health.sources["sina"].planned_count == 1
    assert health.sources["sina"].success_count == 1
    assert health.route is not None
    assert health.route.status == "success"
    assert health.route.degraded is True
    assert health.route.vendor == "sina"
    assert health.route.fallback_reason == "primary_failed"
    assert [item.name for item in health.route.results] == ["eastmoney", "sina"]
    assert health.route.results[0].status == "failed"
    assert health.route.results[1].status == "success"
    waterfall = health.latency_waterfall
    assert waterfall.stages["external_source"].sample_count == 2
    assert waterfall.stages["normalization"].sample_count == 1
    assert waterfall.stages["merge"].sample_count == 1
    assert waterfall.stages["canonical_commit"].sample_count == 1
    assert waterfall.stages["cycle_total:full_market"].sample_count == 1
    assert "eastmoney:source_failed" in gateway.canonical_snapshot().degraded_reasons


def test_gateway_columnar_projection_failure_preserves_scalar_market_and_marks_degraded(monkeypatch) -> None:
    quote = replace(_quote(), source="sina")
    gateway = MarketDataGateway(
        FailingMarketClient(),
        StaticMarketClient((quote,)),
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=1,
        circuit_breaker_seconds=60,
        wall_clock=lambda: NOW,
    )

    def fail_projection(*_args, **_kwargs):
        raise RuntimeError("injected Polars construction failure")

    monkeypatch.setattr(gateway_module.ColumnarQuoteBatch, "from_snapshot", fail_projection)

    fetched = tuple(gateway.fetch_market(observed_at=NOW))

    snapshot = gateway.canonical_snapshot()
    health = gateway.health()
    assert [(item.code, item.price, item.source) for item in fetched] == [(quote.code, quote.price, "sina")]
    assert snapshot is not None
    assert "columnar_projection_failed" in snapshot.degraded_reasons
    assert health.snapshot is not None and health.snapshot.merge_epoch == snapshot.merge_epoch
    assert health.changes.merge_epoch == snapshot.merge_epoch
    assert len(health.changes.inserted_codes) == 1


def test_full_market_commit_preserves_candidate_overlay_published_during_merge(monkeypatch) -> None:
    seed = replace(_quote(), price=12.0, data_version="seed")
    full_refresh = replace(
        _quote(),
        price=12.1,
        source_time=NOW + timedelta(seconds=1),
        received_time=NOW + timedelta(seconds=1),
        data_version="full-refresh",
    )
    candidate = replace(
        _quote(),
        price=12.2,
        source_time=NOW + timedelta(seconds=2),
        received_time=NOW + timedelta(seconds=2),
        data_version="candidate-latest",
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
    assert snapshot.source_versions["eastmoney"] == "full-refresh"
    assert "sina" not in snapshot.source_versions
    assert snapshot.source_versions["tencent"] == "candidate-latest"


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

    assert health.active_source == "sina"
    assert health.route is not None
    assert health.route.status == "success"
    assert health.route.degraded is True
    assert health.route.vendor == "sina"
    assert len(health.route.results) == 2
    assert sum(1 for vendor in health.route.results if vendor.status == "failed") == 0
    assert sum(1 for vendor in health.route.results if vendor.skipped) == 1
    assert health.route.results[0].name == "eastmoney"
    assert health.route.results[0].status == "skipped"
    assert health.route.results[0].skipped is True
    assert health.route.results[0].error == "circuit_open"
    assert health.route.results[1].name == "sina"
    assert health.route.results[1].status == "success"
    assert health.sources["eastmoney"].error_count == 0
    assert health.sources["eastmoney"].physical_failure_count == 0
    assert health.sources["eastmoney"].circuit_skipped_count == 1


def test_gateway_source_health_records_superseded_without_physical_failure() -> None:
    gateway = MarketDataGateway(
        StaticMarketClient((_quote(),)),
        StaticMarketClient((_quote(),)),
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
    )

    gateway.record_superseded("eastmoney")

    health = gateway.health().sources["eastmoney"]
    assert health.superseded_count == 1
    assert health.error_count == 0
    assert health.physical_failure_count == 0


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
    class ProbeSequenceMarketClient(SequenceMarketClient):
        def __init__(self, results) -> None:
            super().__init__(results)
            self.probe_calls = 0

        def probe_market(self) -> None:
            self.probe_calls += 1

    monotonic = MutableMonotonic()
    source = ProbeSequenceMarketClient((RuntimeError("offline"), (_quote(),)))
    gateway = MarketDataGateway(
        source,
        FailingMarketClient(),
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=1,
        circuit_breaker_seconds=60,
        monotonic=monotonic,
    )

    with pytest.raises(MarketDataUnavailableError):
        gateway.fetch_market()
    assert gateway.health().sources["eastmoney"].circuit_open is True

    monotonic.value = 61.0

    fetched = tuple(gateway.fetch_market())
    assert [(item.code, item.price, item.source) for item in fetched] == [("600001", 12.0, "eastmoney")]
    assert source.calls == 2
    assert source.probe_calls == 1
    assert gateway.health().sources["eastmoney"].circuit_open is False
    assert gateway.health().sources["eastmoney"].recovery_probe_count == 1
    assert gateway.health().sources["eastmoney"].recovery_probe_success_count == 1


def test_gateway_reports_recoverable_unavailability_when_all_sources_fail() -> None:
    gateway = MarketDataGateway(
        FailingMarketClient(),
        FailingMarketClient(),
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=1,
        circuit_breaker_seconds=60,
    )

    with pytest.raises(MarketDataUnavailableError, match=r"eastmoney: offline; sina: offline"):
        gateway.fetch_market()

    health = gateway.health()
    assert health.active_source == "unavailable"
    assert health.sources["eastmoney"].circuit_open is True
    assert health.sources["sina"].circuit_open is True
    assert health.sources["eastmoney"].physical_failure_count == 1
    assert health.sources["sina"].physical_failure_count == 1
    assert health.sources["eastmoney"].timeout_count == 0
    assert health.sources["sina"].timeout_count == 0
    assert health.route is not None
    assert health.route.status == "failed"
    assert health.route.fallback_reason == "failed"
    assert health.route.vendor == "sina"
    assert [item.name for item in health.route.results] == ["eastmoney", "sina"]
    assert [item.status for item in health.route.results] == ["failed", "failed"]


def test_gateway_health_records_no_data_route_fallback() -> None:
    gateway = MarketDataGateway(
        FailingMarketClient(),
        StaticMarketClient(()),
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=1,
        circuit_breaker_seconds=60,
    )

    with pytest.raises(MarketDataUnavailableError, match=r"sina: only 0 market rows;.*eastmoney: offline"):
        gateway.fetch_market()

    health = gateway.health()
    assert health.route is not None
    assert health.route.status == "no_data"
    assert health.route.fallback_reason == "no_data"
    assert health.route.vendor == ""
    assert health.route.results[0].status == "failed"
    assert health.route.results[1].status == "no_data"


def test_feature_service_rejects_targeted_quote_older_than_full_market_snapshot() -> None:
    current = replace(
        _quote(),
        price=12.5,
        source_time=NOW + timedelta(seconds=2),
        received_time=NOW + timedelta(seconds=2),
        data_version="market-latest",
    )
    older = replace(_quote(), price=11.5, data_version="target")
    middle = replace(
        _quote(),
        price=12.0,
        source_time=NOW + timedelta(seconds=1),
        received_time=NOW + timedelta(seconds=1),
        data_version="target-middle",
    )
    gateway = StaticGatewayWithSeparateQuotes((current,), (older,))
    service = _service(
        gateway,
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
        wall_clock=lambda: NOW + timedelta(seconds=5),
    )
    service.refresh_candidate_quotes(("600001",), NOW)
    service.fetch_market_features(NOW + timedelta(seconds=2))
    gateway._candidate_quotes = (middle,)

    refreshed = service.refresh_candidate_quotes(("600001",), NOW + timedelta(seconds=3))

    assert refreshed[0].quote.price == 12.5
    assert refreshed[0].quote.data_version == "market-latest"
    assert service.health()["quote_out_of_order_count"] == 1


def test_feature_service_does_not_commit_full_market_result_after_deadline() -> None:
    deadline = NOW + timedelta(seconds=1)
    wall_times = iter((NOW, deadline))
    service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
        wall_clock=lambda: next(wall_times),
    )

    with pytest.raises(MarketDataDeadlineExceededError, match="completed after"):
        service.fetch_market_features(NOW, deadline=deadline)

    assert service.quotes.status().market_features == ()


def test_gateway_full_market_cache_avoids_duplicate_physical_requests_and_reports_hits() -> None:
    runtime = load_runtime_settings(Path(__file__).parents[2] / "config" / "runtime.json")
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
    assert sina.calls == 0
    status = cache.status().datasets["full_market_quotes"]
    assert status["eastmoney"].hit == 1
    assert status["eastmoney"].entries == 1


def test_gateway_negative_refresh_keeps_failure_degradation_with_last_valid_value() -> None:
    runtime = load_runtime_settings(Path(__file__).parents[2] / "config" / "runtime.json")
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


def test_gateway_keeps_last_valid_snapshot_when_both_free_full_market_sources_fail() -> None:
    eastmoney = SequenceMarketClient(
        (
            (replace(_quote(), source="eastmoney"),),
            RuntimeError("eastmoney offline"),
        )
    )
    gateway = MarketDataGateway(
        eastmoney,
        FailingMarketClient(),
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=30,
        wall_clock=lambda: NOW,
    )

    baseline = tuple(gateway.fetch_market(observed_at=NOW))
    degraded = tuple(gateway.fetch_market(observed_at=NOW, force=True))

    assert degraded == baseline
    snapshot = gateway.canonical_snapshot()
    assert snapshot is not None
    assert "all_sources_failed:last_valid_snapshot" in snapshot.degraded_reasons
    assert "eastmoney:source_failed" in snapshot.degraded_reasons
    assert "sina:source_failed" in snapshot.degraded_reasons


def test_gateway_background_refresh_failure_uses_negative_cache_to_suppress_retries() -> None:
    runtime = load_runtime_settings(Path(__file__).parents[2] / "config" / "runtime.json")
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
    eastmoney = replace(_quote(), source="eastmoney", price=12.0, speed=None, data_version="z-east")
    sina = replace(_quote(), source="sina", price=12.01, speed=0.7, data_version="sina")
    tencent = replace(_quote(), source="tencent", price=12.02, speed=None, data_version="a-tencent")
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
    assert snapshot.quotes[0].speed is None
    assert snapshot.field_sources["600001"]["price"] == "tencent"
    assert "speed" not in snapshot.field_sources["600001"]
    assert snapshot.source_versions["tencent"] == "a-tencent"
