from __future__ import annotations

from tests.component.market_data_test_support import (
    _SHANGHAI,
    AFTERNOON,
    LONG_POLICY,
    MARKET_REGIME_POLICY,
    NEWS_POLICY,
    NOW,
    TAIL_POLICY,
    BlockingIntradayClient,
    Board,
    BoundedExecutor,
    BoundedLruCache,
    DailyBar,
    DataPlaneRepository,
    EastmoneyClient,
    FailingIntradayClient,
    FakeSession,
    FeatureBuilder,
    MutableMonotonic,
    Path,
    PriceAdjustment,
    SequenceIntradayClient,
    SourceLaneRegistry,
    StaticGateway,
    StaticHistoryClient,
    StaticIntradayClient,
    _history_bars,
    _quote,
    _service,
    _tail_minute_bars,
    datetime,
    load_runtime_settings,
    pytest,
    replace,
    threading,
    time,
    timedelta,
)


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


def test_intraday_tail_has_a_lane_independent_from_full_market_refresh() -> None:
    pool = BoundedExecutor(worker_count=2, queue_capacity=2, thread_name_prefix="test-source-tail")
    lanes = SourceLaneRegistry(pool)
    full_started = threading.Event()
    release_full = threading.Event()

    def blocked_full_market() -> str:
        full_started.set()
        assert release_full.wait(2.0)
        return "market"

    pool.start()
    try:
        full = lanes.submit("eastmoney", "market", NOW, blocked_full_market)
        assert full_started.wait(1.0)
        tail = lanes.submit("eastmoney_intraday", "tail", NOW, lambda: "tail")

        assert tail.result(timeout=0.5) == "tail"
        assert not full.done()
        release_full.set()
        assert full.result(timeout=1.0) == "market"
    finally:
        release_full.set()
        lanes.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)


def test_expired_unified_intraday_cache_triggers_a_new_physical_load() -> None:
    runtime = load_runtime_settings(Path(__file__).parents[2] / "config" / "runtime.json")
    monotonic = MutableMonotonic()
    cache: BoundedLruCache[object] = BoundedLruCache(
        runtime.market_data.cache_policy,
        cadence_seconds=runtime.pipeline.cadence_seconds,
        monotonic=monotonic,
        wall_clock=lambda: NOW,
    )
    intraday = StaticIntradayClient(_tail_minute_bars())
    service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
        intraday_client=intraday,
        cache=cache,
        source_contract_versions=runtime.market_data.source_contract_versions,
        config_version=runtime.config_version,
        intraday_ttl_seconds=45,
        monotonic=monotonic,
        wall_clock=lambda: NOW,
    )

    service.intraday.load(("600001",), NOW)
    monotonic.value = 45.001
    service.intraday.load(("600001",), NOW)

    assert intraday.calls == ["600001", "600001"]


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
    service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
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


def test_feature_builder_derives_auditable_tail_inputs_without_fabricating_missing_values() -> None:
    builder = FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY)
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
    assert tail_evidence.data_version == "intraday"
    assert "30分钟收益=2.000000%" in tail_evidence.title
    assert "量比=1.500000" in tail_evidence.title
    assert missing.values["tail_return_30m_pct"] is None
    assert missing.values["tail_return_30m"] is None
    assert missing.values["tail_volume_ratio_raw"] is None
    assert missing.values["tail_volume_ratio"] is None
    assert "tail_return_30m" in missing.missing_fields


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

    feature = FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY).build((quote,), {}, NOW)[0]

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
            adjustment=PriceAdjustment.QFQ,
            source="fixture",
        )
        for index in range(21)
    )
    quote = replace(_quote(), price=10.0, amount=100_000_000.0)

    feature = FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY).build(
        (quote,), {quote.code: bars}, NOW
    )[0]

    assert feature.values["return_5d"] == pytest.approx(0.0)
    assert feature.values["price_volume_confirmation"] == pytest.approx(50.0)


def test_market_service_fetches_intraday_minutes_only_for_requested_candidate_mode() -> None:
    intraday = StaticIntradayClient(_tail_minute_bars())
    service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
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
    assert service.health()["intraday_tail_data_versions"] == ("intraday",)


def test_market_service_schedules_intraday_io_round_robin_across_boards() -> None:
    intraday = StaticIntradayClient(_tail_minute_bars())
    quotes = (
        replace(_quote(code="600001"), board=Board.MAIN),
        replace(_quote(code="600002"), board=Board.MAIN),
        replace(_quote(code="300001"), board=Board.CHINEXT),
        replace(_quote(code="688001"), board=Board.STAR),
    )
    service = _service(
        StaticGateway(quotes),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
        intraday_client=intraday,
        intraday_workers=1,
    )

    service.fetch_candidate_features(
        ("600001", "600002", "300001", "688001"),
        AFTERNOON,
        include_intraday_tail=True,
    )

    assert intraday.calls == ["600001", "300001", "688001", "600002"]


def test_intraday_cache_has_a_hard_entry_limit() -> None:
    intraday = StaticIntradayClient(_tail_minute_bars())
    service = _service(
        StaticGateway((_quote(), _quote(code="600002"))),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
        intraday_client=intraday,
        intraday_workers=1,
        intraday_cache_limit=1,
    )

    service.fetch_candidate_features(("600001",), AFTERNOON, include_intraday_tail=True)
    service.fetch_candidate_features(("600002",), AFTERNOON, include_intraday_tail=True)

    assert intraday.calls == ["600001", "600002"]
    assert service.health()["intraday_tail_cache_entries"] == 1


def test_intraday_failure_keeps_tomorrow_features_available_and_marks_missing() -> None:
    service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
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
    service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
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
    service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
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


def test_cancelled_before_start_intraday_request_is_retried_on_next_refresh() -> None:
    intraday = BlockingIntradayClient()
    service = _service(
        StaticGateway((_quote(), _quote(code="600002"))),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
        intraday_client=intraday,
        intraday_workers=1,
        intraday_batch_timeout_seconds=0.1,
    )

    first_refresh = threading.Thread(
        target=service.refresh_intraday_tail,
        args=(("600001", "600002"), AFTERNOON),
    )
    try:
        first_refresh.start()
        assert intraday.started.wait(1.0)
        first_refresh.join(1.0)
        assert not first_refresh.is_alive()
        intraday.release.set()
        assert intraday.finished.wait(1.0)
        service.refresh_intraday_tail(("600001", "600002"), AFTERNOON)
    finally:
        intraday.release.set()

    assert intraday.calls == ["600001", "600002"]


def test_source_lane_intraday_batch_timeout_returns_without_waiting_for_blocked_io() -> None:
    pool = BoundedExecutor(worker_count=5, queue_capacity=5, thread_name_prefix="source-data")
    lanes = SourceLaneRegistry(pool)
    intraday = BlockingIntradayClient()
    service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
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
    runtime = load_runtime_settings(Path(__file__).parents[2] / "config" / "runtime.json")
    measured_at = AFTERNOON + timedelta(seconds=91)
    cache: BoundedLruCache[object] = BoundedLruCache(
        runtime.market_data.cache_policy,
        cadence_seconds=runtime.pipeline.cadence_seconds,
        wall_clock=lambda: measured_at,
    )
    pool = BoundedExecutor(worker_count=5, queue_capacity=5, thread_name_prefix="source-data")
    lanes = SourceLaneRegistry(pool)
    service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
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
    service.intraday.load(("600001",), AFTERNOON)
    service.intraday._entries["600001"] = replace(service.intraday._entries["600001"], expires_at=-1.0)
    blocking = BlockingIntradayClient()
    service.intraday._client = blocking

    class TrackingRestrictions(dict[str, set[str]]):
        def __init__(self) -> None:
            super().__init__()
            self.mutation_threads: list[str] = []

        def setdefault(self, key: str, default: set[str] | None = None) -> set[str]:
            self.mutation_threads.append(threading.current_thread().name)
            return super().setdefault(key, default or set())

    restrictions = TrackingRestrictions()
    try:
        result = service.intraday.load(
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
    service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
        intraday_client=intraday,
        intraday_workers=1,
        intraday_batch_timeout_seconds=0.01,
        worker_pool=pool,
        source_lanes=lanes,
    )
    occupied = threading.Event()
    release = threading.Event()

    def occupy_intraday_lane() -> None:
        occupied.set()
        assert release.wait(1.0)

    pool.start()
    running = lanes.submit(
        "eastmoney_intraday",
        "occupied",
        AFTERNOON - timedelta(seconds=1),
        occupy_intraday_lane,
    )
    assert occupied.wait(1.0)

    try:
        result = service.intraday.load(("600001",), AFTERNOON)
        release.set()
        running.result(timeout=1.0)
    finally:
        release.set()
        lanes.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)

    assert result == {}
    assert intraday.calls == []
    assert lanes.status().lanes["eastmoney"].pending is False


def test_history_cache_persists_intraday_current_day_bar_without_future_source_time(tmp_path: Path) -> None:
    observed_at = datetime(2026, 7, 16, 14, 30, tzinfo=_SHANGHAI)
    previous_bar = replace(_history_bars()[-2], trade_date="2026-07-15")
    current_bar = replace(_history_bars()[-1], trade_date=observed_at.date().isoformat())
    data_plane = DataPlaneRepository(tmp_path)
    service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
        data_plane=data_plane,
        wall_clock=lambda: observed_at,
    )

    service.history._persist_history_bars("301717", (previous_bar, current_bar), None, 0.0, "tencent")

    records = data_plane.load_historical_feature_recent_records(codes=("301717",))
    records_by_date = {record.trade_date: record for record in records}
    assert records_by_date["2026-07-15"].source_time == datetime(2026, 7, 15, 15, 0, tzinfo=_SHANGHAI)
    assert records_by_date["2026-07-16"].observed_at == observed_at
    assert records_by_date["2026-07-16"].source_time == observed_at
