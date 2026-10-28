from __future__ import annotations

from datetime import datetime, timezone

from tests.component.market_data_test_support import (
    FEATURE_WEIGHT_POLICY,
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
    LoadRuntimeSettings,
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
    pytest,
    replace,
    threading,
    time,
    timedelta,
)
from trader.recommendation.application.runtime.source_lanes import SourceLaneScheduler


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
    assert health.sources["tencent_market"].circuit_open is True
    assert health.sources["tencent_market"].planned_count == 1
    assert health.sources["tencent_market"].error_count == 1
    assert health.sources["tencent_market"].p50_latency_ms is not None
    assert health.sources["tencent_market"].p95_latency_ms is not None
    assert health.sources["sina"].planned_count == 1
    assert health.sources["sina"].success_count == 1
    assert health.route is not None
    assert health.route.status == "success"
    assert health.route.degraded is True
    assert health.route.vendor == "sina"
    assert health.route.fallback_reason == "primary_failed"
    assert [item.name for item in health.route.results] == ["tencent_market", "sina"]
    assert health.route.results[0].status == "failed"
    assert health.route.results[1].status == "success"
    waterfall = health.latency_waterfall
    assert waterfall.stages["external_source"].sample_count == 2
    assert waterfall.stages["normalization"].sample_count == 1
    assert waterfall.stages["merge"].sample_count == 1
    assert waterfall.stages["canonical_commit"].sample_count == 1
    assert waterfall.stages["cycle_total:full_market"].sample_count == 1
    assert "tencent_market:source_failed" in gateway.canonical_snapshot().degraded_reasons


def test_gateway_uses_composed_full_market_fetcher_for_deadline_and_cancellation() -> None:
    quote = _quote()
    calls: list[tuple[object, threading.Event]] = []

    def fetch_tencent_market(deadline, cancel_event):
        calls.append((deadline, cancel_event))
        return (quote,)

    gateway = MarketDataGateway(
        StaticMarketClient(()),
        StaticMarketClient((quote,)),
        StaticTencentClient((quote,)),
        minimum_market_rows=1,
        circuit_breaker_failures=2,
        circuit_breaker_seconds=60,
        full_market_fetchers={"tencent_market": fetch_tencent_market},
        wall_clock=lambda: NOW,
    )

    deadline = NOW + timedelta(seconds=1)
    fetched = tuple(gateway.fetch_market(observed_at=NOW, deadline=deadline))
    assert [(item.code, item.source) for item in fetched] == [(quote.code, "tencent")]
    assert calls and calls[0][0] == deadline
    assert isinstance(calls[0][1], threading.Event)


def test_candidate_request_reserves_deadline_for_normalization_and_commit() -> None:
    quote = replace(_quote(), source="tencent")

    class RecordingTencentClient(StaticTencentClient):
        def __init__(self) -> None:
            super().__init__((quote,))
            self.timeout_seconds: float | None = None

        def fetch_quotes(self, codes, *, timeout_seconds=None):
            self.timeout_seconds = timeout_seconds
            return super().fetch_quotes(codes, timeout_seconds=timeout_seconds)

    tencent = RecordingTencentClient()
    gateway = MarketDataGateway(
        StaticMarketClient((quote,)),
        StaticMarketClient((quote,)),
        tencent,
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        wall_clock=lambda: NOW,
    )

    fetched = gateway.fetch_candidates(
        (quote.code,),
        observed_at=NOW,
        deadline=NOW + timedelta(seconds=3),
    )

    assert fetched[0].code == quote.code
    assert tencent.timeout_seconds == pytest.approx(2.8)


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

    original_merge = gateway_module.MergeMarketObservations
    merge_started = threading.Event()
    release_merge = threading.Event()

    def coordinated_merge(*args, **kwargs):
        if threading.current_thread().name == "full-market-refresh":
            merge_started.set()
            assert release_merge.wait(10.0)
        return original_merge(*args, **kwargs)

    monkeypatch.setattr(gateway_module, "MergeMarketObservations", coordinated_merge)
    errors: list[BaseException] = []

    def refresh_market() -> None:
        try:
            gateway.fetch_market(observed_at=NOW + timedelta(seconds=1))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=refresh_market, name="full-market-refresh")
    thread.start()
    try:
        assert merge_started.wait(10.0)
        refreshed = tuple(gateway.fetch_candidates(("600001",), observed_at=NOW + timedelta(seconds=2)))
        assert refreshed[0].price == 12.2
    finally:
        release_merge.set()
        thread.join(10.0)

    assert not thread.is_alive()
    assert errors == []
    snapshot = gateway.canonical_snapshot()
    assert snapshot is not None
    assert snapshot.quotes[0].price == 12.2
    assert snapshot.quotes[0].source == "tencent"
    assert "sina" not in snapshot.source_versions
    assert snapshot.source_versions["tencent"] == "candidate-latest"


def test_full_market_projection_finishes_once_under_targeted_publication(monkeypatch) -> None:
    seed = _quote()
    full = replace(seed, price=12.1, source_time=NOW + timedelta(seconds=1), received_time=NOW + timedelta(seconds=1))
    targeted = replace(
        seed, price=12.2, source_time=NOW + timedelta(seconds=2), received_time=NOW + timedelta(seconds=2)
    )
    gateway = MarketDataGateway(
        SequenceMarketClient(((seed,), (full,))),
        StaticMarketClient(()),
        StaticTencentClient((targeted,)),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        wall_clock=lambda: NOW + timedelta(seconds=3),
    )
    gateway.fetch_market(observed_at=NOW)
    projection_entered = threading.Event()
    release_projection = threading.Event()
    target_started = threading.Event()
    target_done = threading.Event()
    project = gateway_module._try_columnar_snapshot
    projections = []
    errors = []

    def blocked_projection(snapshot, **kwargs):
        projections.append(snapshot.merge_epoch)
        projection_entered.set()
        assert release_projection.wait(2.0)
        return project(snapshot, **kwargs)

    def refresh_full():
        try:
            gateway.fetch_market(observed_at=NOW + timedelta(seconds=1))
        except BaseException as exc:
            errors.append(exc)

    def refresh_target():
        target_started.set()
        try:
            gateway.fetch_candidates((seed.code,), observed_at=NOW + timedelta(seconds=2))
        except BaseException as exc:
            errors.append(exc)
        finally:
            target_done.set()

    monkeypatch.setattr(gateway_module, "_try_columnar_snapshot", blocked_projection)
    full_thread = threading.Thread(target=refresh_full)
    target_thread = threading.Thread(target=refresh_target)
    full_thread.start()
    try:
        assert projection_entered.wait(1.0)
        target_thread.start()
        assert target_started.wait(1.0)
        target_done.wait(0.1)
    finally:
        release_projection.set()
        full_thread.join(2.0)
        if target_thread.ident is not None:
            target_thread.join(2.0)
    assert not full_thread.is_alive() and not target_thread.is_alive()
    assert not errors
    assert len(projections) == 1
    assert gateway.current_quotes((seed.code,))[0].price == 12.2


def test_later_full_market_cycle_replaces_older_cross_source_quote() -> None:
    older_sina = replace(
        _quote(),
        source="sina",
        price=12.0,
        source_time=NOW,
        received_time=NOW,
        data_version="sina-older-cycle",
    )
    later_tencent = replace(
        _quote(),
        source="tencent",
        price=12.2,
        source_time=NOW + timedelta(seconds=10),
        received_time=NOW + timedelta(seconds=10),
        data_version="tencent-later-cycle",
    )
    gateway = MarketDataGateway(
        SequenceMarketClient((RuntimeError("tencent offline"), (later_tencent,))),
        StaticMarketClient((older_sina,)),
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        wall_clock=lambda: NOW + timedelta(seconds=10),
    )

    first = tuple(gateway.fetch_market(observed_at=NOW))
    second = tuple(gateway.fetch_market(observed_at=NOW + timedelta(seconds=10), force=True))

    assert first[0].source == "sina"
    assert second[0].source == "tencent"
    assert second[0].price == 12.2
    assert second[0].received_time == NOW + timedelta(seconds=10)


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
    gateway._states["tencent_market"].open_until = gateway._monotonic() + 60.0

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
    assert health.route.results[0].name == "tencent_market"
    assert health.route.results[0].status == "skipped"
    assert health.route.results[0].skipped is True
    assert health.route.results[0].error == "circuit_open"
    assert health.route.results[1].name == "sina"
    assert health.route.results[1].status == "success"
    assert health.sources["tencent_market"].error_count == 0
    assert health.sources["tencent_market"].physical_failure_count == 0
    assert health.sources["tencent_market"].circuit_skipped_count == 1


def test_gateway_source_health_records_superseded_without_physical_failure() -> None:
    gateway = MarketDataGateway(
        StaticMarketClient((_quote(),)),
        StaticMarketClient((_quote(),)),
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
    )

    gateway.record_superseded("tencent_market")

    health = gateway.health().sources["tencent_market"]
    assert health.superseded_count == 1
    assert health.error_count == 0
    assert health.physical_failure_count == 0


@pytest.mark.parametrize("with_lanes", (False, True))
def test_gateway_coalesces_concurrent_full_market_requests_into_one_physical_call(with_lanes) -> None:
    source = BlockingMarketClient((_quote(),))
    pool = BoundedExecutor(worker_count=2, queue_capacity=2, thread_name_prefix="shared-gateway")
    lanes = SourceLaneScheduler(pool) if with_lanes else None
    gateway = MarketDataGateway(
        source,
        StaticMarketClient(()),
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        worker_pool=pool,
        source_lanes=lanes,
    )
    results: list[tuple[MarketQuote, ...]] = []
    threads = [threading.Thread(target=lambda: results.append(tuple(gateway.fetch_market()))) for _index in range(2)]

    try:
        pool.start()
        for thread in threads:
            thread.start()
        assert source.started.wait(1.0)
        with pytest.raises(MarketDataDeadlineExceededError):
            gateway.fetch_market(deadline=datetime.now(timezone.utc) + timedelta(seconds=0.02))
    finally:
        source.release.set()
        for thread in threads:
            thread.join(1.0)
        if lanes is not None:
            lanes.stop(wait=True)
        pool.stop(wait=True, cancel_futures=True)

    assert source.calls == 1
    assert [[(item.code, item.price, item.source) for item in result] for result in results] == [
        [("600001", 12.0, "tencent")],
        [("600001", 12.0, "tencent")],
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
        recovery_probes={"tencent_market": source.probe_market},
    )

    with pytest.raises(MarketDataUnavailableError):
        gateway.fetch_market()
    assert gateway.health().sources["tencent_market"].circuit_open is True

    monotonic.value = 61.0

    fetched = tuple(gateway.fetch_market())
    assert [(item.code, item.price, item.source) for item in fetched] == [("600001", 12.0, "tencent")]
    assert source.calls == 2
    assert source.probe_calls == 1
    assert gateway.health().sources["tencent_market"].circuit_open is False
    assert gateway.health().sources["tencent_market"].recovery_probe_count == 1
    assert gateway.health().sources["tencent_market"].recovery_probe_success_count == 1


def test_gateway_reports_recoverable_unavailability_when_all_sources_fail() -> None:
    gateway = MarketDataGateway(
        FailingMarketClient(),
        FailingMarketClient(),
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=1,
        circuit_breaker_seconds=60,
    )

    with pytest.raises(MarketDataUnavailableError, match=r"tencent_market: offline; sina: offline"):
        gateway.fetch_market()

    health = gateway.health()
    assert health.active_source == "unavailable"
    assert health.sources["tencent_market"].circuit_open is True
    assert health.sources["sina"].circuit_open is True
    assert health.sources["tencent_market"].physical_failure_count == 1
    assert health.sources["sina"].physical_failure_count == 1
    assert health.sources["tencent_market"].timeout_count == 0
    assert health.sources["sina"].timeout_count == 0
    assert health.route is not None
    assert health.route.status == "failed"
    assert health.route.fallback_reason == "failed"
    assert health.route.vendor == "sina"
    assert [item.name for item in health.route.results] == ["tencent_market", "sina"]
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

    with pytest.raises(MarketDataUnavailableError, match=r"sina: only 0 market rows;.*tencent_market: offline"):
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
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
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
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        wall_clock=lambda: next(wall_times),
    )

    with pytest.raises(MarketDataDeadlineExceededError, match="completed after"):
        service.fetch_market_features(NOW, deadline=deadline)

    assert service.quotes.status().market_features == ()


def test_gateway_full_market_cache_avoids_duplicate_physical_requests_and_reports_hits() -> None:
    runtime = LoadRuntimeSettings(Path(__file__).parents[2] / "config" / "runtime.json")
    cache: BoundedLruCache[object] = BoundedLruCache(
        runtime.market_data.cache_policy,
        cadence_seconds=runtime.pipeline.cadence_seconds,
        wall_clock=lambda: NOW,
    )
    tencent_market = CountingMarketClient((replace(_quote(), source="tencent"),))
    sina = CountingMarketClient((replace(_quote(), source="sina", price=12.01),))
    gateway = MarketDataGateway(
        tencent_market,
        sina,
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        cache=cache,
        source_contracts=runtime.market_data.source_contracts,
        config_version=runtime.config_version,
        wall_clock=lambda: NOW,
    )

    first = tuple(gateway.fetch_market(observed_at=NOW))
    second = tuple(gateway.fetch_market(observed_at=NOW))

    assert first == second
    assert tencent_market.calls == 1
    assert sina.calls == 0
    status = cache.status().datasets["full_market_quotes"]
    assert status["tencent_market"].hit == 1
    assert status["tencent_market"].entries == 1


def test_gateway_negative_refresh_keeps_failure_degradation_with_last_valid_value() -> None:
    runtime = LoadRuntimeSettings(Path(__file__).parents[2] / "config" / "runtime.json")
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
            return (replace(_quote(), source="tencent"),)

    tencent_market = ToggleMarketClient()
    gateway = MarketDataGateway(
        tencent_market,
        StaticMarketClient((replace(_quote(), source="sina"),)),
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        cache=cache,
        source_contracts=runtime.market_data.source_contracts,
        config_version=runtime.config_version,
        wall_clock=lambda: NOW,
    )

    gateway.fetch_market(observed_at=NOW)
    tencent_market.fail = True
    gateway.fetch_market(observed_at=NOW, force=True)
    gateway.fetch_market(observed_at=NOW)

    snapshot = gateway.canonical_snapshot()
    assert snapshot is not None
    assert "tencent:source_failed" in snapshot.degraded_reasons


def test_gateway_keeps_last_valid_snapshot_when_both_free_full_market_sources_fail() -> None:
    tencent_market = SequenceMarketClient(
        (
            (replace(_quote(), source="tencent"),),
            RuntimeError("tencent offline"),
        )
    )
    gateway = MarketDataGateway(
        tencent_market,
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
    assert "tencent_market:source_failed" in snapshot.degraded_reasons
    assert "sina:source_failed" in snapshot.degraded_reasons
    assert snapshot.status == "stale"
    assert "failed" in snapshot.failure_categories
    assert "stale" in snapshot.failure_categories


def test_gateway_background_refresh_failure_uses_negative_cache_to_suppress_retries() -> None:
    runtime = LoadRuntimeSettings(Path(__file__).parents[2] / "config" / "runtime.json")
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

    tencent_market = ToggleMarketClient("tencent")
    sina = ToggleMarketClient("sina")
    gateway = MarketDataGateway(
        tencent_market,
        sina,
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        worker_pool=pool,
        cache=cache,
        source_contracts=runtime.market_data.source_contracts,
        config_version=runtime.config_version,
        monotonic=monotonic,
        wall_clock=lambda: NOW,
    )
    pool.start()

    try:
        gateway.fetch_market(observed_at=NOW)
        monotonic.value = 30.001
        tencent_market.fail = True
        gateway.fetch_market(observed_at=NOW)
        assert tencent_market.failed.wait(1.0)
        time.sleep(0.02)

        gateway.fetch_market(observed_at=NOW)
        time.sleep(0.02)
    finally:
        pool.stop(wait=True, cancel_futures=True)
        cache.stop()

    assert tencent_market.calls == 2
    snapshot = gateway.canonical_snapshot()
    assert snapshot is not None
    assert "tencent_market:source_failed" in snapshot.degraded_reasons


def test_targeted_quote_overlay_updates_canonical_value_and_field_attribution() -> None:
    tencent_market = replace(_quote(), source="tencent", price=12.0, speed=None, data_version="z-market")
    sina = replace(_quote(), source="sina", price=12.01, speed=0.7, data_version="sina")
    tencent = replace(
        _quote(),
        source="tencent",
        price=12.02,
        speed=None,
        source_time=NOW + timedelta(seconds=1),
        received_time=NOW + timedelta(seconds=1),
        data_version="a-tencent",
    )
    gateway = MarketDataGateway(
        StaticMarketClient((tencent_market,)),
        StaticMarketClient((sina,)),
        StaticTencentClient((tencent,)),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        wall_clock=lambda: NOW + timedelta(seconds=1),
    )

    gateway.fetch_market(observed_at=NOW)
    gateway.fetch_candidates(("600001",), observed_at=NOW + timedelta(seconds=1))

    snapshot = gateway.canonical_snapshot()
    assert snapshot is not None
    assert snapshot.quotes[0].price == 12.02
    assert snapshot.quotes[0].speed is None
    assert snapshot.field_sources["600001"]["price"] == "tencent"
    assert "speed" not in snapshot.field_sources["600001"]
    assert snapshot.source_versions["tencent"] == "a-tencent"
