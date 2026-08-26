from __future__ import annotations

from tests.component.market_data_test_support import (
    _SHANGHAI,
    LONG_POLICY,
    MARKET_REGIME_POLICY,
    NEWS_POLICY,
    NOW,
    TAIL_POLICY,
    BlockingHistoryClient,
    BoundedExecutor,
    BoundedLruCache,
    CountingHistoryClient,
    DailyBar,
    DataPlaneRepository,
    DataPlaneUnavailableError,
    FeatureBuilder,
    FeatureSnapshot,
    HistoricalFeatureRecord,
    HistoryCache,
    Mapping,
    MarketDataDeadlineExceededError,
    MarketTaskRunner,
    MutableMonotonic,
    Path,
    PriceAdjustment,
    SelectiveHistoryClient,
    Sequence,
    SourceLaneRegistry,
    StaticGateway,
    StaticHistoryClient,
    _history_bars,
    _history_preload_codes,
    _quote,
    _service,
    build_history_context,
    date,
    datetime,
    load_runtime_settings,
    pytest,
    replace,
    threading,
    time,
    timedelta,
    timezone,
)


def test_history_cache_fetches_sixty_one_bars_but_retains_only_twenty_raw_rows() -> None:
    bars = (
        DailyBar(
            trade_date="2026-04-30",
            open_price=9.9,
            close=9.9,
            high=10.0,
            low=9.8,
            volume=1_000_000,
            amount=100_000_000,
            pct_change=0.1,
            adjustment=PriceAdjustment.QFQ,
            source="fixture",
        ),
        *_history_bars(),
    )

    class RecordingHistory:
        days: list[int] = []

        def fetch_history(self, _code, *, days):
            self.days.append(days)
            return bars

    history = RecordingHistory()
    service = _service(
        StaticGateway((_quote(),)),
        history,
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
        wall_clock=lambda: NOW,
    )

    loaded = service.history.load(("600001",))
    entry = service.history.entries()["600001"]
    status = service.history.status()

    assert history.days == [61]
    assert len(loaded["600001"]) == 20
    assert len(entry.bars) == 20
    assert entry.context is not None
    assert entry.context.sample_count == 61
    assert entry.context.profile.moving_average_60d is not None
    assert status.raw_rows == 20
    assert status.profile_entries == 1


def test_history_cache_reuses_actionable_refresh_due_value_with_degradation() -> None:
    runtime = load_runtime_settings(Path(__file__).parents[2] / "config" / "v2" / "runtime.json")
    monotonic = MutableMonotonic()
    cache: BoundedLruCache[object] = BoundedLruCache(
        runtime.market_data.cache_policy,
        cadence_seconds=runtime.pipeline.cadence_seconds,
        monotonic=monotonic,
        wall_clock=lambda: NOW,
    )
    runner = MarketTaskRunner(
        worker_pool=None,
        source_lanes=None,
        cache=cache,
        source_contract_versions=runtime.market_data.source_contract_versions,
        config_version=runtime.config_version,
        schema_version="market-v15",
        wall_clock=lambda: NOW,
    )
    history = HistoryCache(
        CountingHistoryClient(()),
        runner,
        history_worker_pool=None,
        workers=1,
        ttl_seconds=21_600,
        capacity=360,
        history_data_plane=None,
        monotonic=monotonic,
    )
    bars = tuple(
        replace(
            bar,
            trade_date=(date(2026, 7, 15) - timedelta(days=19 - index)).isoformat(),
        )
        for index, bar in enumerate(_history_bars()[-20:])
    )
    identity = runner.cache_identity(
        "daily_history",
        "eastmoney",
        "600001",
        {"code": "600001", "days": 61, "retained_days": 20, "adjust": "qfq"},
        NOW,
    )
    cache.put(
        identity,
        bars,
        data_version="2026-07-15",
        source_time=NOW - timedelta(hours=7),
    )
    monotonic.value = 21_601.0
    restrictions: dict[str, set[str]] = {}

    result = history.cached(
        ("600001",),
        fresh_only=False,
        action_restrictions=restrictions,
    )

    assert result == {"600001": bars}
    assert restrictions == {"600001": {"history_data_degraded"}}
    assert history.cached(("600001",), fresh_only=True) == {}


def test_feature_service_does_not_commit_history_cache_after_deadline() -> None:
    deadline = NOW + timedelta(seconds=1)
    wall_times = iter((NOW, NOW, NOW, deadline))
    service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
        wall_clock=lambda: next(wall_times, deadline),
    )

    with pytest.raises(MarketDataDeadlineExceededError, match="completed after"):
        service.fetch_market_features(NOW, deadline=deadline)

    assert service.history.entries() == {}
    assert service.quotes.status().market_features == ()


def test_full_market_deadline_does_not_wait_for_blocked_history_warmup() -> None:
    runtime = load_runtime_settings(Path(__file__).parents[2] / "config" / "v2" / "runtime.json")
    cache: BoundedLruCache[object] = BoundedLruCache(
        runtime.market_data.cache_policy,
        cadence_seconds=runtime.pipeline.cadence_seconds,
    )
    pool = BoundedExecutor(worker_count=5, queue_capacity=5, thread_name_prefix="source-data")
    lanes = SourceLaneRegistry(pool)
    history = BlockingHistoryClient(_history_bars())
    service = _service(
        StaticGateway((_quote(),)),
        history,
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
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
    assert cache.status().datasets["daily_history"]["eastmoney"].entries == 0


def test_market_service_bounds_history_preload_to_stratified_candidate_universe() -> None:
    history = CountingHistoryClient(_history_bars())
    quotes = tuple(_quote(code=f"60000{index}", industry="工业" if index % 2 else "银行") for index in range(1, 6))
    service = _service(
        StaticGateway(quotes),
        history,
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
        history_workers=2,
        history_preload_limit=2,
    )

    features = service.fetch_market_features(NOW)

    assert len(history.calls) == 2
    assert sum(item.history_days >= 20 for item in features) == 2
    assert service.health()["history_universe_rows"] == 2


def test_history_preload_reserves_120_slots_for_each_supported_board() -> None:
    quotes = tuple(
        [
            *(_quote(code=f"600{index:03d}", industry=f"主板{index % 12}") for index in range(120)),
            *(_quote(code=f"300{index:03d}", industry=f"创业板{index % 12}") for index in range(120)),
            *(_quote(code=f"688{index:03d}", industry=f"科创板{index % 12}") for index in range(120)),
            *(_quote(code=f"830{index:03d}", industry=f"不支持{index % 12}") for index in range(120)),
        ]
    )

    selected = _history_preload_codes(quotes, 360)

    assert len(selected) == 360
    assert sum(code.startswith("600") for code in selected) == 120
    assert sum(code.startswith("300") for code in selected) == 120
    assert sum(code.startswith("688") for code in selected) == 120
    assert not any(code.startswith("830") for code in selected)

    imbalanced = _history_preload_codes(
        tuple(
            [
                *(_quote(code=f"600{index:03d}") for index in range(121)),
                _quote(code="300001"),
                _quote(code="688001"),
            ]
        ),
        360,
    )

    assert sum(code.startswith("600") for code in imbalanced) == 120


def test_repeated_refresh_does_not_queue_multiple_history_warmup_batches() -> None:
    codes = ("600001", "600002", "300001", "300002", "688001", "688002")
    started = threading.Event()
    release = threading.Event()

    class BlockingHistory:
        @staticmethod
        def fetch_history(_code, *, days):
            assert days == 61
            started.set()
            assert release.wait(1.0)
            return _history_bars()

    pool = BoundedExecutor(worker_count=2, queue_capacity=2, thread_name_prefix="source-data")
    lanes = SourceLaneRegistry(pool)
    service = _service(
        StaticGateway(tuple(_quote(code=code) for code in codes)),
        BlockingHistory(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
        worker_pool=pool,
        source_lanes=lanes,
        history_warmup_batch_size=3,
        wall_clock=lambda: NOW,
    )
    pool.start()

    try:
        service.warmup.schedule_history_warmup(codes, NOW)
        assert started.wait(1.0)
        service.warmup.schedule_history_warmup(codes, NOW + timedelta(seconds=1))
        service.warmup.schedule_history_warmup(codes, NOW + timedelta(seconds=2))
        health = service.health()
        assert health["history_warmup_planned_count"] == 3
        assert health["history_warmup_inflight_count"] == 3
    finally:
        release.set()
        lanes.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)


def test_history_warmup_does_not_supersede_pending_candidate_history() -> None:
    codes = ("600001", "600002")
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    class BlockingFirstHistory:
        @staticmethod
        def fetch_history(code, *, days):
            assert days == 61
            calls.append(code)
            if len(calls) == 1:
                started.set()
                assert release.wait(1.0)
            return _history_bars()

    pool = BoundedExecutor(worker_count=2, queue_capacity=2, thread_name_prefix="source-data")
    lanes = SourceLaneRegistry(pool)
    service = _service(
        StaticGateway(tuple(_quote(code=code) for code in codes)),
        BlockingFirstHistory(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
        worker_pool=pool,
        source_lanes=lanes,
        history_warmup_batch_size=1,
        wall_clock=lambda: NOW,
    )
    candidate_result: list[Sequence[FeatureSnapshot]] = []
    candidate_errors: list[BaseException] = []

    def load_candidates() -> None:
        try:
            candidate_result.append(service.fetch_candidate_features(codes, NOW + timedelta(seconds=1)))
        except BaseException as exc:
            candidate_errors.append(exc)

    pool.start()
    candidate_thread = threading.Thread(target=load_candidates)
    try:
        service.fetch_market_features(NOW)
        assert started.wait(1.0)
        candidate_thread.start()
        timeout_at = time.monotonic() + 1.0
        while not lanes.status().lanes["history"].pending and time.monotonic() < timeout_at:
            time.sleep(0.01)
        assert lanes.status().lanes["history"].pending is True
        release.set()
        candidate_thread.join(2.0)
    finally:
        release.set()
        candidate_thread.join(1.0)
        lanes.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)

    assert candidate_errors == []
    assert len(candidate_result) == 1
    assert {feature.quote.code for feature in candidate_result[0]} == set(codes)
    assert lanes.status().lanes["history"].superseded_count == 0


def test_history_warmup_deadline_releases_blocked_batch_identity() -> None:
    started = threading.Event()
    release = threading.Event()

    class BlockingHistory:
        @staticmethod
        def fetch_history(_code, *, days):
            assert days == 61
            started.set()
            assert release.wait(1.0)
            return _history_bars()

    source_pool = BoundedExecutor(worker_count=2, queue_capacity=2, thread_name_prefix="source-data")
    history_pool = BoundedExecutor(worker_count=1, queue_capacity=1, thread_name_prefix="history-data")
    lanes = SourceLaneRegistry(source_pool)
    service = _service(
        StaticGateway((_quote(),)),
        BlockingHistory(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
        worker_pool=source_pool,
        history_worker_pool=history_pool,
        source_lanes=lanes,
        history_warmup_batch_size=1,
        history_warmup_batch_timeout_seconds=0.02,
        wall_clock=lambda: datetime.now(timezone.utc),
    )
    source_pool.start()
    history_pool.start()

    try:
        observed_at = datetime.now(timezone.utc)
        service.warmup.schedule_history_warmup(("600001",), observed_at)
        assert started.wait(1.0)
        timeout_at = time.monotonic() + 1.0
        while service.health()["history_warmup_timeout_count"] < 1 and time.monotonic() < timeout_at:
            time.sleep(0.01)

        health = service.health()
        assert health["history_warmup_timeout_count"] == 1
        assert health["history_warmup_inflight_count"] == 0
        assert health["history_warmup_unique_failure_count"] == 1
        assert health["history_warmup_batch_timeout_seconds"] == 0.02
    finally:
        release.set()
        lanes.stop(wait=True, timeout_seconds=1.0)
        source_pool.stop(wait=True, cancel_futures=True)
        history_pool.stop(wait=True, cancel_futures=True)


def test_history_warmup_deadline_keeps_completed_stock_and_retries_only_slow_tail() -> None:
    slow_started = threading.Event()
    release = threading.Event()

    class PartialHistory:
        @staticmethod
        def fetch_history(code, *, days):
            assert days == 61
            if code == "600002":
                slow_started.set()
                assert release.wait(1.0)
            return _history_bars()

    source_pool = BoundedExecutor(worker_count=2, queue_capacity=2, thread_name_prefix="source-data")
    history_pool = BoundedExecutor(worker_count=2, queue_capacity=2, thread_name_prefix="history-data")
    lanes = SourceLaneRegistry(source_pool)
    service = _service(
        StaticGateway((_quote(),)),
        PartialHistory(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
        worker_pool=source_pool,
        history_worker_pool=history_pool,
        source_lanes=lanes,
        history_warmup_batch_size=2,
        history_warmup_batch_timeout_seconds=0.5,
        wall_clock=lambda: datetime.now(timezone.utc),
    )
    source_pool.start()
    history_pool.start()

    try:
        service.warmup.schedule_history_warmup(("600001", "600002"), datetime.now(timezone.utc))
        assert slow_started.wait(1.0)
        visible_at = time.monotonic() + 0.2
        while "600001" not in service.history.entries() and time.monotonic() < visible_at:
            time.sleep(0.005)
        assert len(service.history.entries()["600001"].bars) == 20
        timeout_at = time.monotonic() + 1.0
        while service.health()["history_warmup_timeout_count"] < 1 and time.monotonic() < timeout_at:
            time.sleep(0.01)

        entries = service.history.entries()
        status = service.warmup.status()
        assert len(entries["600001"].bars) == 20
        assert "600002" not in entries
        assert status.completed_count == 1
        assert status.failure_count == 1
        assert status.unique_failure_count == 1
    finally:
        release.set()
        lanes.stop(wait=True, timeout_seconds=1.0)
        source_pool.stop(wait=True, cancel_futures=True)
        history_pool.stop(wait=True, cancel_futures=True)


def test_market_service_reloads_expired_history_and_reports_failed_coverage() -> None:
    clock = MutableMonotonic()
    history = SelectiveHistoryClient(_history_bars(), failing_codes={"600002"})
    service = _service(
        StaticGateway((_quote(), _quote(code="600002"))),
        history,
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
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


def test_history_cache_recover_from_data_plane_restores_context_and_window(tmp_path: Path) -> None:
    observed_at = datetime(2026, 7, 16, 15, 0, tzinfo=_SHANGHAI)
    source_time = observed_at - timedelta(minutes=1)
    data_plane = DataPlaneRepository(tmp_path)
    bars = _history_bars()
    for bar in bars:
        data_plane.save_historical_feature_recent(
            HistoricalFeatureRecord(
                code="600001",
                trade_date=bar.trade_date,
                observed_at=observed_at,
                source_time=source_time,
                source="fixture",
                data_version="fixture-v1",
                payload={
                    "trade_date": bar.trade_date,
                    "open_price": bar.open_price,
                    "close": bar.close,
                    "high": bar.high,
                    "low": bar.low,
                    "volume": bar.volume,
                    "amount": bar.amount,
                    "pct_change": bar.pct_change,
                    "turnover_rate": bar.turnover_rate,
                    "adjustment": bar.adjustment.value,
                    "source": bar.source,
                },
                schema_version="v2_data_plane_v1",
                payload_hash="",
            )
        )

    service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
        data_plane=data_plane,
        wall_clock=lambda: NOW,
    )
    service.history.recover_from_data_plane()
    entry = service.history.entries()["600001"]

    assert len(entry.bars) == 20
    assert entry.context is not None
    assert entry.context.sample_count == len(bars)
    assert entry.context.sample_count == 60
    assert entry.context.latest_trade_date == bars[-1].trade_date

    restored = service.history.load(("600001",))
    assert restored["600001"] == entry.bars


def test_history_cache_persists_latest_compact_summary_with_raw_window(tmp_path: Path) -> None:
    data_plane = DataPlaneRepository(tmp_path)
    bars = _history_bars()
    context = build_history_context(bars)
    service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
        data_plane=data_plane,
        wall_clock=lambda: datetime(2026, 7, 16, 15, 0, tzinfo=_SHANGHAI),
    )

    service.history._persist_history_bars("600001", bars, context, 0.0, "fixture")

    records = data_plane.load_historical_feature_recent_records(codes=("600001",))
    latest = next(record for record in records if record.trade_date == context.latest_trade_date)
    summary = latest.payload["history_summary"]
    assert isinstance(summary, Mapping)
    assert summary["latest_trade_date"] == context.latest_trade_date
    assert summary["sample_count"] == context.sample_count
    assert isinstance(summary["profile"], Mapping)
    assert summary["profile"]["median_amount_20d"] == context.profile.median_amount_20d

    trimmed_plane = DataPlaneRepository(tmp_path / "trimmed")
    for record in records[-20:]:
        trimmed_plane.save_historical_feature_recent(record)
    restored_service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
        data_plane=trimmed_plane,
        wall_clock=lambda: datetime(2026, 7, 16, 15, 1, tzinfo=_SHANGHAI),
    )
    restored_service.history.recover_from_data_plane()
    restored_context = restored_service.history.entries()["600001"].context
    assert restored_context == context


def test_history_cache_persistence_unavailable_does_not_block_history_load() -> None:
    class UnavailableHistoryDataPlane:
        def __init__(self) -> None:
            self.calls = 0

        def save_historical_feature_recent_records(self, records) -> None:
            self.calls += 1
            assert len(records) == 20
            raise DataPlaneUnavailableError("unavailable")

    data_plane = UnavailableHistoryDataPlane()
    bars = _history_bars()[-20:]
    service = _service(
        StaticGateway((_quote(),)),
        CountingHistoryClient(bars),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
        data_plane=data_plane,
        wall_clock=lambda: NOW,
    )
    loaded = service.history.load(("600001",))

    assert list(loaded["600001"])[-1].trade_date == bars[-1].trade_date
    assert len(loaded["600001"]) == 20
    assert data_plane.calls == 1
