from __future__ import annotations

import logging
from dataclasses import replace

import pytest

from tests.component.market_data_test_support import (
    AFTERNOON,
    FEATURE_WEIGHT_POLICY,
    LONG_POLICY,
    MARKET_REGIME_POLICY,
    NEWS_POLICY,
    NOW,
    TAIL_POLICY,
    BlockingMarketClient,
    BoundedExecutor,
    BoundedLruCache,
    CountingHistoryClient,
    CountingMarketClient,
    FailFirstTencentClient,
    FailingMarketClient,
    FeatureBuilder,
    LoadRuntimeSettings,
    MarketDataGateway,
    MarketQuote,
    MutableMonotonic,
    Path,
    StaticGateway,
    StaticGatewayWithSeparateQuotes,
    StaticHistoryClient,
    StaticMarketClient,
    StaticTencentClient,
    ThreadRecordingGateway,
    ThreadRecordingHistoryClient,
    _history_bars,
    _quote,
    _service,
    _tail_minute_bars,
    datetime,
    threading,
    time,
    timedelta,
)
from trader.download.application.read_published_history import ReadPublishedHistoryUseCase
from trader.download.infra.published_history_snapshot import SQLitePublishedHistorySnapshot
from trader.infra.market_data.observations import SourceObservation
from trader.recommendation.application.ports.data_plane_records import (
    DataPlaneConflictError,
    DataPlaneUnavailableError,
)
from trader.recommendation.application.ports.market_data import MarketDataPendingError
from trader.recommendation.infra.market_data.published_history_cache import PublishedHistoryCache


def test_market_service_components_own_distinct_locks_and_facade_has_no_shared_lock() -> None:
    service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
    )

    component_locks = (
        service.quotes._lock,
        service.history._lock,
        service.research._lock,
        service.intraday._lock,
        service.references._lock,
    )

    assert len({id(lock) for lock in component_locks}) == len(component_locks)
    assert not hasattr(service, "_lock")


def test_initial_history_projection_defers_market_io(tmp_path, monkeypatch) -> None:
    gateway = StaticGateway((_quote(),))

    def fetch_market(**kwargs):
        raise AssertionError("market I/O must wait for the initial local projection")

    monkeypatch.setattr(gateway, "fetch_market", fetch_market)
    history = PublishedHistoryCache(
        ReadPublishedHistoryUseCase(SQLitePublishedHistorySnapshot(tmp_path / "history")), lookback_sessions=61
    )
    history.record_maintenance("loading", stage="reading_active_snapshot")
    service = _service(
        gateway,
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        published_history=history,
    )

    with pytest.raises(MarketDataPendingError, match="history_projection_loading"):
        service.fetch_market_features(NOW, force=True, deadline=NOW + timedelta(seconds=20))


@pytest.mark.parametrize("source_delay_seconds", (5, 20))
def test_market_stage_health_uses_completion_time_without_accepting_future_quotes(source_delay_seconds) -> None:
    received_at = NOW + timedelta(seconds=5)
    quote = replace(
        _quote(),
        source_time=NOW + timedelta(seconds=source_delay_seconds),
        received_time=received_at,
    )
    service = _service(
        StaticGateway((quote,)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        wall_clock=lambda: received_at,
    )

    batch = service.fetch_market_feature_batch(NOW, force=True)

    assert batch.dynamic_stage.snapshot.as_of == received_at
    expected = "ready" if source_delay_seconds == 5 else "unavailable"
    assert batch.dynamic_stage.snapshot.source_health.state.value == expected


def test_unknown_source_contract_fallback_uses_stable_cache_identity() -> None:
    service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
    )

    identity = service.runner.cache_identity(
        "daily_history",
        "unconfigured-source",
        "600001",
        {"code": "600001"},
        NOW,
    )

    assert identity.source_contract_version == "unconfigured-source-component"


def _security_master_observation() -> SourceObservation:
    return SourceObservation(
        source="eastmoney_security_master",
        subject_key="600001",
        observed_at=NOW,
        source_time=NOW,
        received_at=NOW,
        effective_at=NOW,
        data_version="fixture",
        fields={"name": "fixture"},
        missing_reasons={},
        payload_hash="",
        status="success",
        error_code=None,
    )


def test_security_master_persistence_conflict_is_debug_only(caplog) -> None:
    class ConflictingDataPlane:
        def save_security_master_recent_records(self, records) -> None:
            assert records
            raise DataPlaneConflictError("recent save conflicts at the same observation time")

    caplog.set_level(logging.DEBUG)
    service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        data_plane=ConflictingDataPlane(),
    )
    service.references._persist_security_masters(NOW, (_security_master_observation(),))

    assert "security master persistence already committed" in caplog.text
    assert "security master persistence failed" not in caplog.text


def test_security_master_persistence_unavailable_remains_a_warning(caplog) -> None:
    class UnavailableDataPlane:
        def save_security_master_recent_records(self, _records) -> None:
            raise DataPlaneUnavailableError("controlled unavailable")

    caplog.set_level(logging.WARNING)
    service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        data_plane=UnavailableDataPlane(),
    )

    service.references._persist_security_masters(NOW, (_security_master_observation(),))

    assert "security master persistence unavailable" in caplog.text
    assert "security master persistence already committed" not in caplog.text


def test_long_quote_circuit_does_not_open_candidate_quote_circuit() -> None:
    quote = replace(_quote("600001"), source="tencent", data_version="tencent-targeted")
    tencent = FailFirstTencentClient((quote,))
    gateway = MarketDataGateway(
        FailingMarketClient(),
        StaticMarketClient((quote,)),
        tencent,
        minimum_market_rows=1,
        circuit_breaker_failures=1,
        circuit_breaker_seconds=60,
        wall_clock=lambda: NOW,
    )

    assert gateway.fetch_long_quotes(("600001",), observed_at=NOW) == ()
    fetched = gateway.fetch_candidates(("600001",), observed_at=NOW)

    assert fetched[0].code == "600001"
    health = gateway.health().sources
    assert health["tencent_long"].circuit_open is True
    assert health["tencent"].circuit_open is False


def test_long_quotes_bypass_shared_cache_for_each_realtime_refresh() -> None:
    runtime = LoadRuntimeSettings(Path(__file__).parents[2] / "config" / "runtime.json")
    cache: BoundedLruCache[object] = BoundedLruCache(
        runtime.market_data.cache_policy,
        cadence_seconds=runtime.pipeline.cadence_seconds,
        wall_clock=lambda: NOW,
    )
    quote = replace(_quote("600001"), source="tencent", data_version="tencent-targeted")
    gateway = MarketDataGateway(
        FailingMarketClient(),
        StaticMarketClient((quote,)),
        StaticTencentClient((quote,)),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        cache=cache,
        source_contracts=runtime.market_data.source_contracts,
        config_version=runtime.config_version,
        wall_clock=lambda: NOW,
    )

    first = gateway.fetch_long_quotes(("600001",), observed_at=NOW)
    second = gateway.fetch_long_quotes(("600001",), observed_at=NOW)

    assert first[0].code == "600001"
    assert second[0].code == "600001"
    assert "long_quotes" not in cache.status().datasets


def test_final_refresh_bypasses_fresh_cache_but_remains_single_flight() -> None:
    runtime = LoadRuntimeSettings(Path(__file__).parents[2] / "config" / "runtime.json")
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
        source_contracts=runtime.market_data.source_contracts,
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
    assert sina.calls == 0


def test_stale_intraday_and_missing_published_research_remain_observe_only() -> None:
    runtime = LoadRuntimeSettings(Path(__file__).parents[2] / "config" / "runtime.json")
    measured_at = AFTERNOON + timedelta(seconds=91)
    cache: BoundedLruCache[object] = BoundedLruCache(
        runtime.market_data.cache_policy,
        cadence_seconds=runtime.pipeline.cadence_seconds,
        wall_clock=lambda: measured_at,
    )
    quote = replace(_quote(), source_time=measured_at, received_time=measured_at)
    service = _service(
        StaticGateway((quote,)),
        CountingHistoryClient(_history_bars()),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        cache=cache,
        source_contracts=runtime.market_data.source_contracts,
        config_version=runtime.config_version,
        wall_clock=lambda: measured_at,
    )
    service.quotes.update_candidate_quotes((quote,))
    service.history.load((quote.code,))
    intraday = tuple(
        replace(
            bar,
            source_time=bar.source_time - timedelta(seconds=91),
            received_time=bar.received_time - timedelta(seconds=91),
        )
        for bar in _tail_minute_bars()
    )
    cache.put(
        service.runner.cache_identity(
            "intraday_minutes",
            "eastmoney",
            quote.code,
            {"code": quote.code, "scale_minutes": 1, "adjust": "none"},
            measured_at,
        ),
        intraday,
        data_version="intraday-old",
        source_time=measured_at - timedelta(seconds=91),
    )

    features = service.read_candidate_features(
        (quote.code,),
        measured_at,
        include_intraday_tail=True,
        include_structured_research=False,
    )

    assert features[0].quote.execution_restrictions == (
        "intraday_data_degraded",
        "research_data_degraded",
    )
    assert features[0].history_days == 60
    assert all(item.evidence_id != "news-old" for item in features[0].evidence)


def test_feature_market_data_health_reports_bounded_quote_age_summaries() -> None:
    measured_at = NOW + timedelta(seconds=31)
    service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
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
    assert health["candidate_quote_latest_source"] == "fixture"


def test_candidate_quote_cache_retains_disjoint_three_strategy_union() -> None:
    service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
    )
    quotes = tuple(
        replace(
            _quote(),
            code=f"{600000 + offset:06d}",
            data_version=f"candidate-{offset:04d}",
        )
        for offset in range(1080)
    )

    service.quotes.update_candidate_quotes(quotes)

    codes = tuple(quote.code for quote in quotes)
    assert tuple(quote.code for quote in service.quotes.candidate_snapshot(codes)) == codes


def test_candidate_quote_health_age_only_covers_the_latest_candidate_cycle() -> None:
    fresh = _quote()
    measured_at = NOW + timedelta(seconds=1)
    service = _service(
        StaticGateway((fresh,)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        wall_clock=lambda: measured_at,
    )
    old_quotes = tuple(
        replace(
            fresh,
            code=f"{601000 + offset:06d}",
            source_time=NOW - timedelta(minutes=10),
            received_time=NOW - timedelta(minutes=10),
            data_version=f"old-candidate-{offset:04d}",
        )
        for offset in range(1080)
    )
    service.quotes.update_candidate_quotes(old_quotes)

    service.refresh_candidate_quotes((fresh.code,), NOW)

    health = service.health()
    assert health["candidate_quote_cache_entries"] == 1080
    assert health["candidate_quote_age"] == {
        "sample_count": 1,
        "p50_seconds": 1.0,
        "p95_seconds": 1.0,
        "maximum_seconds": 1.0,
        "latest_source_time": NOW.isoformat(),
    }


def test_feature_service_current_quote_index_prefers_latest_targeted_quote() -> None:
    market_quote = _quote()
    targeted_quote = replace(
        market_quote,
        price=15.0,
        pct_change=8.0,
        source="tencent",
        source_time=NOW + timedelta(seconds=5),
        received_time=NOW + timedelta(seconds=5),
        data_version="targeted-refresh",
    )
    service = _service(
        StaticGatewayWithSeparateQuotes((market_quote,), (targeted_quote,)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
    )
    service.fetch_market_features(NOW)
    service.refresh_candidate_quotes(("600001",), NOW + timedelta(seconds=5))

    quotes = service.current_quotes(("600001", "600999"))

    assert tuple(quotes) == ("600001",)
    assert quotes["600001"].price == 15.0
    assert quotes["600001"].pct_change == 8.0
    assert quotes["600001"].source == "tencent"
    assert quotes["600001"].data_version == "targeted-refresh"


def test_feature_service_current_quote_index_reads_canonical_quote_before_feature_commit() -> None:
    canonical_quote = replace(
        _quote(),
        price=14.0,
        pct_change=6.0,
        data_version="canonical-snapshot",
    )
    service = _service(
        StaticGateway((canonical_quote,)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
    )

    quotes = service.current_quotes(("600001",))

    assert quotes["600001"].price == 14.0
    assert quotes["600001"].pct_change == 6.0
    assert quotes["600001"].data_version == "canonical-snapshot"


def test_market_service_uses_injected_runtime_data_pool_for_market_gateway() -> None:
    pool = BoundedExecutor(worker_count=1, queue_capacity=8, thread_name_prefix="shared-data")
    history = ThreadRecordingHistoryClient(_history_bars())
    gateway = ThreadRecordingGateway((_quote(), _quote(code="600002")))
    service = _service(
        gateway,
        history,
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        worker_pool=pool,
        history_workers=2,
    )
    pool.start()
    try:
        features = service.fetch_market_features(NOW)
    finally:
        pool.stop()

    assert len(features) == 2
    assert gateway.thread_names and all(name.startswith("shared-data") for name in gateway.thread_names)
    assert not any(thread.name.startswith("shared-data") for thread in threading.enumerate())


def test_degraded_candidate_cache_is_observe_only_without_rewriting_source_time() -> None:
    runtime = LoadRuntimeSettings(Path(__file__).parents[2] / "config" / "runtime.json")
    monotonic = MutableMonotonic()

    def wall_clock() -> datetime:
        return NOW + timedelta(seconds=monotonic.value)

    cache: BoundedLruCache[object] = BoundedLruCache(
        runtime.market_data.cache_policy,
        cadence_seconds=runtime.pipeline.cadence_seconds,
        monotonic=monotonic,
        wall_clock=wall_clock,
    )
    quote = replace(_quote(), source="tencent_market")
    gateway = MarketDataGateway(
        StaticMarketClient((quote,)),
        StaticMarketClient((replace(quote, source="sina"),)),
        StaticTencentClient((replace(quote, source="tencent"),)),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        cache=cache,
        source_contracts=runtime.market_data.source_contracts,
        config_version=runtime.config_version,
        monotonic=monotonic,
        wall_clock=wall_clock,
    )
    gateway.fetch_market(observed_at=NOW)
    gateway.fetch_candidates(("600001",), observed_at=NOW)
    monotonic.value = 30.001

    result = gateway.fetch_candidates(("600001",), observed_at=wall_clock())

    snapshot = gateway.canonical_snapshot()
    assert result[0].source_time == NOW
    assert "market_data_degraded" in result[0].execution_restrictions
    assert snapshot is not None
    assert "tencent:cache_degraded" in snapshot.degraded_reasons
