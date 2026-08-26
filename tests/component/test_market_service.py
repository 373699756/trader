from __future__ import annotations

from tests.component.market_data_test_support import (
    AFTERNOON,
    LONG_POLICY,
    MARKET_REGIME_POLICY,
    NEWS_POLICY,
    NOW,
    TAIL_POLICY,
    BlockingMarketClient,
    Board,
    BoundedExecutor,
    BoundedLruCache,
    CountingMarketClient,
    DataPlaneRepository,
    Evidence,
    FailFirstTencentClient,
    FailingMarketClient,
    FeatureBuilder,
    MarketDataGateway,
    MarketQuote,
    MutableMonotonic,
    Path,
    ResearchObservation,
    SourceLaneRegistry,
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
    date,
    datetime,
    load_runtime_settings,
    replace,
    threading,
    time,
    timedelta,
    timezone,
)


def test_market_service_components_own_distinct_locks_and_facade_has_no_shared_lock() -> None:
    service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
    )

    component_locks = (
        service.quotes._lock,
        service.history._lock,
        service.warmup._lock,
        service.research._lock,
        service.intraday._lock,
        service.references._lock,
    )

    assert len({id(lock) for lock in component_locks}) == len(component_locks)
    assert not hasattr(service, "_lock")


def test_long_quote_circuit_does_not_open_candidate_quote_circuit() -> None:
    quote = replace(_quote("600001"), source="tencent", data_version="tencent-targeted-v1")
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
    runtime = load_runtime_settings(Path(__file__).parents[2] / "config" / "v2" / "runtime.json")
    cache: BoundedLruCache[object] = BoundedLruCache(
        runtime.market_data.cache_policy,
        cadence_seconds=runtime.pipeline.cadence_seconds,
        wall_clock=lambda: NOW,
    )
    quote = replace(_quote("600001"), source="tencent", data_version="tencent-targeted-v1")
    gateway = MarketDataGateway(
        FailingMarketClient(),
        StaticMarketClient((quote,)),
        StaticTencentClient((quote,)),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        cache=cache,
        source_contract_versions=runtime.market_data.source_contract_versions,
        config_version=runtime.config_version,
        wall_clock=lambda: NOW,
    )

    first = gateway.fetch_long_quotes(("600001",), observed_at=NOW)
    second = gateway.fetch_long_quotes(("600001",), observed_at=NOW)

    assert first[0].code == "600001"
    assert second[0].code == "600001"
    assert "long_quotes" not in cache.status().datasets


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
    assert sina.calls == 0


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
    service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
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
            service.research.load,
            ("600001", "600002", "600003", "600004"),
            NOW,
            include_structured=False,
            force=True,
        ).result()
        lanes.submit(
            "akshare",
            "research-cycle-2",
            NOW,
            service.research.load,
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
            service.research.load,
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


def test_auxiliary_cache_action_age_marks_new_features_observe_only() -> None:
    runtime = load_runtime_settings(Path(__file__).parents[2] / "config" / "v2" / "runtime.json")
    measured_at = AFTERNOON + timedelta(seconds=91)
    cache: BoundedLruCache[object] = BoundedLruCache(
        runtime.market_data.cache_policy,
        cadence_seconds=runtime.pipeline.cadence_seconds,
        wall_clock=lambda: measured_at,
    )
    quote = replace(_quote(), source_time=measured_at, received_time=measured_at)
    service = _service(
        StaticGateway((quote,)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
        cache=cache,
        source_contract_versions=runtime.market_data.source_contract_versions,
        config_version=runtime.config_version,
        wall_clock=lambda: measured_at,
    )
    service.quotes.update_candidate_quotes((quote,))
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
        service.runner.cache_identity(
            "daily_history",
            "eastmoney",
            quote.code,
            {"code": quote.code, "days": 61, "retained_days": 20, "adjust": "qfq"},
            measured_at,
        ),
        history,
        data_version="history-old-v1",
        source_time=measured_at - timedelta(seconds=86401),
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
        data_version="intraday-old-v1",
        source_time=measured_at - timedelta(seconds=91),
    )
    cache.put(
        service.runner.cache_identity(
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


def test_feature_service_health_reports_bounded_quote_age_summaries() -> None:
    measured_at = NOW + timedelta(seconds=31)
    service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
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
    service = _service(
        StaticGatewayWithSeparateQuotes((market_quote,), (targeted_quote,)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
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
    service = _service(
        StaticGateway((canonical_quote,)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
    )

    quotes = service.current_quotes(("600001",))

    assert quotes["600001"].price == 14.0
    assert quotes["600001"].pct_change == 6.0
    assert quotes["600001"].data_version == "canonical-v2"


def test_market_service_uses_injected_lifecycle_data_pool() -> None:
    pool = BoundedExecutor(worker_count=1, queue_capacity=8, thread_name_prefix="shared-data")
    history = ThreadRecordingHistoryClient(_history_bars())
    gateway = ThreadRecordingGateway((_quote(), _quote(code="600002")))
    service = _service(
        gateway,
        history,
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
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


def test_late_free_identity_is_persisted_without_waiting_for_next_score_cycle(tmp_path: Path) -> None:
    completed = threading.Event()
    observed_at = datetime.now(timezone.utc)

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
    data_plane = DataPlaneRepository(tmp_path)
    service = _service(
        gateway,
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
        data_plane=data_plane,
        worker_pool=pool,
        source_lanes=lanes,
    )
    gateway.set_security_reference_persistence_sink(service.references.schedule_security_master_persistence)
    pool.start()
    deadline = observed_at + timedelta(milliseconds=80)

    try:
        result = tuple(gateway.fetch_market(observed_at=observed_at, deadline=deadline))
        time.sleep(0.1)
        eastmoney.release.set()
        assert completed.wait(1.0)
        timeout_at = time.monotonic() + 2.0
        persisted = None
        while persisted is None and time.monotonic() < timeout_at:
            persisted = data_plane.load_security_master_recent(eastmoney_quote.code)
            time.sleep(0.01)
    finally:
        eastmoney.release.set()
        lanes.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)

    assert result[0].source == "sina"
    assert persisted is not None
    assert persisted.source == "eastmoney_security_master"
    assert persisted.payload["listing_date"] == "1999-11-10"
    assert lanes.status().lanes["reference"].completed_count >= 1
