from __future__ import annotations

from tests.component.market_data_test_support import (
    LONG_POLICY,
    MARKET_REGIME_POLICY,
    NEWS_POLICY,
    NOW,
    TAIL_POLICY,
    BlockingMarketClient,
    BlockingTencentClient,
    BoundedExecutor,
    BoundedLruCache,
    FeatureBuilder,
    LatencyWaterfall,
    LatestRequestLane,
    MarketDataDeadlineExceededError,
    MarketDataGateway,
    MarketQuote,
    MutableMonotonic,
    Path,
    SourceLaneRegistry,
    SourceRequestSupersededError,
    StaticGateway,
    StaticHistoryClient,
    StaticMarketClient,
    StaticTencentClient,
    _history_bars,
    _quote,
    _service,
    datetime,
    load_runtime_settings,
    pytest,
    replace,
    threading,
    time,
    timedelta,
    timezone,
)


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
        with pytest.raises(SourceRequestSupersededError):
            superseded.result(timeout=1.0)
        release.set()
        assert running.result(timeout=1.0) == "running"
        assert latest.result(timeout=1.0) == "latest"
        assert calls == ["running", "latest"]
    finally:
        release.set()
        lane.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)

    assert lane.status().running is False
    assert lane.status().pending is False


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

    assert lane.status().running is False
    assert lane.status().pending is False


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
    assert lane.status().running is False
    assert lane.status().pending is False


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
    assert lane.status().running is False
    assert lane.status().pending is False


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
    service = _service(
        ReferenceGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
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


def test_source_lane_records_bounded_queue_wait_telemetry() -> None:
    pool = BoundedExecutor(worker_count=1, queue_capacity=1, thread_name_prefix="source-latency")
    latency = LatencyWaterfall()
    lanes = SourceLaneRegistry(pool, latency=latency)
    pool.start()

    try:
        assert lanes.submit("eastmoney", "market", NOW, lambda: "done").result(timeout=1.0) == "done"
    finally:
        lanes.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)

    summary = latency.status().stages["source_queue_wait"]
    assert summary.sample_count == 1
    assert summary.maximum_ms is not None and summary.maximum_ms >= 0.0


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


def test_topk_quote_lane_uses_reserved_urgent_worker_while_candidate_quotes_are_blocked() -> None:
    pool = BoundedExecutor(
        worker_count=2,
        urgent_worker_count=1,
        queue_capacity=2,
        thread_name_prefix="test-source-priority",
    )
    lanes = SourceLaneRegistry(pool)
    candidate_started = threading.Event()
    release_candidate = threading.Event()

    def blocked_candidate() -> str:
        candidate_started.set()
        assert release_candidate.wait(2.0)
        return "candidate"

    pool.start()
    try:
        candidate = lanes.submit("tencent", "candidate", NOW, blocked_candidate)
        assert candidate_started.wait(1.0)
        topk = lanes.submit_urgent("tencent_topk", "topk", NOW, lambda: "topk")

        assert topk.result(timeout=0.5) == "topk"
        assert not candidate.done()
        release_candidate.set()
        assert candidate.result(timeout=1.0) == "candidate"
    finally:
        release_candidate.set()
        lanes.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)


def test_topk_quote_refresh_uses_reserved_urgent_worker() -> None:
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
    service = _service(
        gateway,
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
        worker_pool=pool,
        source_lanes=lanes,
        wall_clock=lambda: NOW,
    )
    pool.start()
    try:
        normal = pool.submit(blocking_task)
        assert normal is not None
        assert entered.wait(timeout=1.0)
        refreshed = service.refresh_topk_quotes(
            ("600001",),
            NOW,
            deadline=NOW + timedelta(seconds=1),
        )
    finally:
        release.set()
        lanes.stop(wait=True, timeout_seconds=1.0)
        pool.stop()

    assert [feature.quote.code for feature in refreshed] == ["600001"]


def test_dedicated_history_workers_do_not_consume_realtime_source_workers() -> None:
    source_pool = BoundedExecutor(worker_count=2, queue_capacity=2, thread_name_prefix="source-data")
    history_pool = BoundedExecutor(worker_count=2, queue_capacity=2, thread_name_prefix="history-data")
    lanes = SourceLaneRegistry(source_pool)
    history_started = threading.Event()
    release_history = threading.Event()

    class BlockingRemoteHistory:
        @staticmethod
        def fetch_history(_code, *, days):
            assert days == 61
            history_started.set()
            assert release_history.wait(1.0)
            return _history_bars()

    service = _service(
        StaticGateway((_quote(),)),
        BlockingRemoteHistory(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
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
        with pytest.raises(MarketDataDeadlineExceededError, match="deadline"):
            gateway.fetch_market(observed_at=observed_at, deadline=deadline)
        elapsed = time.monotonic() - started
    finally:
        eastmoney.release.set()
        release_timer.cancel()
        lanes.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)

    assert elapsed < 0.5
    assert gateway.canonical_snapshot() is None
    source_health = gateway.health().sources["eastmoney"]
    assert source_health.success_count == 0
    assert source_health.error_count == 1
    assert source_health.timeout_count == 1


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


def test_source_lane_waits_for_hedged_physical_refresh_when_market_cache_is_due() -> None:
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
            self.block_after_calls = 1 if source == "eastmoney" else 0
            self.refresh_started = threading.Event()
            self.release_refresh = threading.Event()

        def fetch_market(self):
            self.calls += 1
            if self.calls > self.block_after_calls:
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
        full_market_hedge_delay_seconds=0.01,
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
        assert not completed.wait(0.05)
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
    assert sina.calls == 1
    assert lanes.status().lanes["eastmoney"].superseded_count == 0
    assert lanes.status().lanes["sina"].superseded_count == 0
