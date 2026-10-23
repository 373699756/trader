"""Shared process resources retain independent profile decisions and model inputs."""

import threading
import time
from dataclasses import replace
from datetime import date, timedelta

import pytest

from tests.component.market_data_test_support import (
    AFTERNOON,
    FEATURE_WEIGHT_POLICY,
    LONG_POLICY,
    MARKET_REGIME_POLICY,
    NEWS_POLICY,
    TAIL_POLICY,
    StaticGateway,
    StaticHistoryClient,
    StaticIntradayClient,
    _quote,
    _service,
    _tail_minute_bars,
)
from tests.component.test_company_startup import _AT, _DATES, _Reader, _snapshot
from tests.component.test_published_input_preloader import NOW, _Archive, _cache
from tests.contract.test_cold_start import _empty_config
from trader.bootstrap import BuildProfiledSystem, BuildSystem
from trader.download.application.read_published_history import ReadPublishedHistoryUseCase
from trader.infra.market_data.history.history import BuildHistoryContext, DailyBar, PriceAdjustment
from trader.infra.shutdown import ShutdownDeadline
from trader.infra.workers import BoundedExecutor
from trader.recommendation.application.ports.market_data import MarketDataPendingError
from trader.recommendation.application.runtime.source_lanes import SourceLaneScheduler
from trader.recommendation.infra.market_data.published_company_research import PublishedCompanyResearch
from trader.recommendation.infra.market_data.published_history_cache import PublishedHistoryCache
from trader.recommendation.infra.normalization.features import FeatureBuilder


def test_both_shares_sources_and_budget_but_isolates_models_and_publication(tmp_path):
    system = BuildProfiledSystem(_empty_config(tmp_path), profiles=("v2", "v3"))
    v2, v3 = system.systems
    assert v2.market_cache is v3.market_cache
    assert v2.source_lanes is v3.source_lanes
    assert v2.minute_pool is v3.minute_pool
    assert v2.budget is v3.budget
    assert v2.market_data.quotes.gateway is v3.market_data.quotes.gateway
    assert v2.market_data.research is v3.market_data.research
    assert v2.market_data.static_cache is v3.market_data.static_cache
    assert v2.market_data.references is v3.market_data.references
    assert v2.market_data.intraday is v3.market_data.intraday
    assert v2.market_data.history is not v3.market_data.history
    assert v2.tomorrow_index is not v3.tomorrow_index
    assert v2.records is not v3.records
    assert v2.workers.control_pool is not v3.workers.control_pool
    assert v2.workers.persistence_pool is not v3.workers.persistence_pool
    assert v2.owns_shared_resources and not v3.owns_shared_resources
    for profile in ("v2", "v3"):
        single = BuildSystem(tmp_path / "config/runtime.json", scoring_profile=profile)
        assert single.settings.market_data.cache_policy.total_bytes == 512 * 1024 * 1024
        assert single.settings.performance_budgets.memory.process_peak_rss_bytes == 2 * 1024**3
    assert not (tmp_path / "data").exists()


def test_company_preload_is_single_and_waits_for_the_complete_publication():
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    class Reader(_Reader):
        def visit(self, codes, consume):
            entered.set()
            assert release.wait(2)
            super().visit(codes, consume)

    reader = Reader(_snapshot())
    research = PublishedCompanyResearch(
        reader,
        wall_clock=lambda: _AT,
        open_dates=lambda: _DATES,
        monotonic=time.monotonic,
    )
    first = threading.Thread(target=lambda: research.initialize(("600001",), cancelled=lambda: False))

    def second():
        research.initialize(("600001",), cancelled=lambda: False)
        assert research.load(("600001",), _AT, include_structured=True)
        finished.set()

    other = threading.Thread(target=second)
    first.start()
    assert entered.wait(1)
    other.start()
    assert not finished.wait(0.02)
    release.set()
    first.join(2)
    other.join(2)
    assert not first.is_alive() and not other.is_alive() and finished.is_set()
    assert reader.visits == 1
    reader.visit = lambda *args: (_ for _ in ()).throw(AssertionError("hot path read SQL"))
    assert research.load(("600001",), _AT, include_structured=True)


def test_daily_inputs_bind_references_company_model_and_config_without_reloading():
    source = _Archive(61, 1, ("600001",))
    history = _cache(source)
    history.initialize(cancelled=lambda: False)
    loaded = history.loaded_identity
    versions = {
        "security_master": "securities",
        "trading_calendar": "calendar",
        "model_industry": "industry",
        "company": "company",
        "model": "model",
    }
    history.bind_inputs(NOW, versions=versions, config_version="config")
    first = history.input_identity
    assert first != loaded
    history.bind_inputs(NOW, versions={**versions, "model": "later-model"}, config_version="later")
    assert history.input_identity == first
    source.iter_windows = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("hot path read SQL"))
    bars = history.cached(("600001",))
    assert history.summaries(bars, NOW)
    for key in versions:
        changed = _cache(_Archive(61, 1, ("600001",)))
        changed.initialize(cancelled=lambda: False)
        changed.bind_inputs(NOW, versions={**versions, key: f"other-{key}"}, config_version="config")
        assert changed.input_identity != first


def test_history_binding_capacity_failure_keeps_all_resident_bytes_and_blocks_scoring():
    reservations = []

    def reserve(size):
        reservations.append(size)
        return len(reservations) == 1

    history = PublishedHistoryCache(
        ReadPublishedHistoryUseCase(_Archive(61, 1, ("600001",))),
        lookback_sessions=61,
        monotonic=time.monotonic,
        reserve_bytes=reserve,
        require_input_binding=True,
    )
    history.initialize(cancelled=lambda: False)
    with pytest.raises(MarketDataPendingError):
        history.require_projection()
    history.bind_inputs(NOW, versions={"security_master": "securities"}, config_version="config")
    assert len(reservations) == 2 and reservations[1] > reservations[0]
    with pytest.raises(MarketDataPendingError, match="history_input_capacity"):
        history.require_projection()


def test_equal_base_facts_build_once_across_profiles_and_quote_changes_invalidate(tmp_path, monkeypatch):
    system = BuildSystem(_empty_config(tmp_path))
    cache = system.market_cache
    bars = tuple(
        DailyBar(
            str(day),
            10.0,
            10.0 + index / 1000,
            11.0,
            9.0,
            1000.0,
            10000.0,
            0.1,
            adjustment=PriceAdjustment.QFQ,
            source="fixture",
        )
        for index, day in enumerate(date(2025, 10, 1) + timedelta(days=offset) for offset in range(251))
    )
    quote = _quote()
    calls = []
    original = FeatureBuilder._base_features

    def counted(self, *args):
        calls.append(args[0].code)
        return original(self, *args)

    monkeypatch.setattr(FeatureBuilder, "_base_features", counted)
    builders = tuple(
        FeatureBuilder(
            NEWS_POLICY,
            TAIL_POLICY,
            MARKET_REGIME_POLICY,
            LONG_POLICY,
            FEATURE_WEIGHT_POLICY,
            model_momentum_horizons=horizons,
            cache=cache,
        )
        for horizons in ((20, 60, 250), (20, 40, 60))
    )
    contexts = (BuildHistoryContext(bars, lookback_sessions=251), BuildHistoryContext(bars[-61:], lookback_sessions=61))
    for builder, context in zip(builders, contexts, strict=True):
        got = builder._raw_features(quote, bars[-20:], context)
        assert got["amount_median_20d"] == 10000
    assert calls == [quote.code]
    cached_values = tuple(
        builder._raw_features(quote, bars[-20:], context) for builder, context in zip(builders, contexts, strict=True)
    )
    builders[1]._raw_features(replace(quote, price=quote.price + 0.1), bars[-20:], contexts[1])
    assert calls == [quote.code, quote.code]
    assert cache.status().datasets["quote_factor_inputs"]["local_history"].estimated_bytes > 0
    monkeypatch.setattr(FeatureBuilder, "_base_features", original)
    for horizons, context, cached in zip(((20, 60, 250), (20, 40, 60)), contexts, cached_values, strict=True):
        uncached = FeatureBuilder(
            NEWS_POLICY,
            TAIL_POLICY,
            MARKET_REGIME_POLICY,
            LONG_POLICY,
            FEATURE_WEIGHT_POLICY,
            model_momentum_horizons=horizons,
        )
        assert uncached._raw_features(quote, bars[-20:], context) == cached


def test_minute_pool_runs_five_requests_with_one_source_worker_and_one_deadline():
    source = BoundedExecutor(worker_count=1, queue_capacity=2, thread_name_prefix="source-fixture")
    minutes = BoundedExecutor(worker_count=5, queue_capacity=0, thread_name_prefix="minute-fixture")
    lanes = SourceLaneScheduler(source)
    ready, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    active, peak = 0, 0
    deadlines = []

    class Client:
        def fetch_intraday_minutes(self, code, *, now, deadline, cancelled):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                deadlines.append(deadline)
                if active == 5:
                    ready.set()
            try:
                assert release.wait(2)
                assert not cancelled()
                return _tail_minute_bars()
            finally:
                with lock:
                    active -= 1

    service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        worker_pool=source,
        minute_pool=minutes,
        source_lanes=lanes,
        intraday_client=Client(),
        intraday_workers=5,
        intraday_batch_timeout_seconds=3,
    )
    source.start()
    minutes.start()
    result = []
    worker = threading.Thread(
        target=lambda: result.append(service.intraday.load(tuple(str(600001 + index) for index in range(7)), AFTERNOON))
    )
    try:
        worker.start()
        assert ready.wait(1)
        assert len(deadlines) == 5
        release.set()
        worker.join(2)
        assert not worker.is_alive()
        assert len(result[0]) == len(deadlines) == 7 and peak == 5
        assert len(set(deadlines)) == 1
    finally:
        release.set()
        worker.join(2)
        deadline = ShutdownDeadline.start(3)
        lanes.stop(wait=True, deadline=deadline)
        source.stop(deadline=deadline)
        minutes.stop(deadline=deadline)


def test_minute_waiter_timeout_does_not_cancel_another_profiles_queued_refresh(monkeypatch):
    source = BoundedExecutor(worker_count=1, queue_capacity=2, thread_name_prefix="queued-source")
    minutes = BoundedExecutor(worker_count=1, queue_capacity=1, thread_name_prefix="queued-minutes")
    lanes = SourceLaneScheduler(source)
    entered, release, submitted = threading.Event(), threading.Event(), threading.Event()
    original_submit = lanes.submit

    def track(*args, **kwargs):
        future = original_submit(*args, **kwargs)
        submitted.set()
        return future

    service = _service(
        StaticGateway((_quote(),)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        worker_pool=source,
        minute_pool=minutes,
        source_lanes=lanes,
        intraday_client=StaticIntradayClient(_tail_minute_bars()),
        intraday_batch_timeout_seconds=0.04,
    )
    results = []

    def occupy():
        entered.set()
        assert release.wait(2)

    source.start()
    minutes.start()
    running = lanes.submit("eastmoney_intraday", "occupied", AFTERNOON - timedelta(seconds=1), occupy)
    assert entered.wait(1)
    monkeypatch.setattr(lanes, "submit", track)
    first = threading.Thread(target=lambda: results.append(service.intraday.load(("600001",), AFTERNOON)))
    second = threading.Thread(target=lambda: results.append(service.intraday.load(("600001",), AFTERNOON)))
    try:
        first.start()
        assert submitted.wait(1)
        submitted.clear()
        service.intraday._batch_timeout_seconds = 1
        second.start()
        assert submitted.wait(1)
        first.join(0.5)
        assert not first.is_alive() and results == [{}]
        release.set()
        running.result(timeout=1)
        second.join(1)
        assert not second.is_alive() and set(results[1]) == {"600001"}
    finally:
        release.set()
        first.join(1)
        second.join(1)
        deadline = ShutdownDeadline.start(2)
        lanes.stop(wait=True, deadline=deadline)
        source.stop(deadline=deadline)
        minutes.stop(deadline=deadline)
