from __future__ import annotations

import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tests.pipeline_factory import build_pipeline
from trader.application.cadence import CadencePolicy
from trader.application.events import (
    EventAuditRecord,
    EventPriority,
    EventSpec,
    EventStatus,
    InMemoryEventLedger,
    PipelineEvent,
)
from trader.application.events import (
    new_event as create_event,
)
from trader.application.pipeline import RecommendationPipeline
from trader.application.ports.market import MarketDataUnavailableError
from trader.application.ports.snapshots import RecoverySummary
from trader.application.published_snapshots import PublishedSnapshotIndex
from trader.application.publisher import SnapshotPublisher
from trader.application.queries import RecommendationQueries
from trader.application.recommendations import RecommendationEngine
from trader.application.schedule import MarketPhase
from trader.application.snapshot_workflow import freeze_available_snapshots, refresh_candidates
from trader.application.source_lanes import SourceRequestSupersededError
from trader.application.status import RuntimeState
from trader.bootstrap import _recommendation_policy
from trader.domain.market.models import FeatureSnapshot
from trader.domain.recommendation.models import (
    LiveOverlay,
    RecommendationSnapshot,
    Strategy,
)
from trader.infra.persistence.snapshots import snapshot_from_dict, snapshot_to_dict
from trader.infra.settings import load_strategy_settings
from trader.web.schemas import snapshot_envelope


def new_event(event_type: str, **values) -> PipelineEvent:
    return create_event(EventSpec(event_type=event_type, **values))


def test_virtual_trading_day_publishes_and_freezes_expected_strategies(
    recommendation_policy,
    application_feature_factory,
) -> None:
    clock = MutableClock(datetime.fromisoformat("2026-07-16T10:00:00+08:00"))
    features = tuple(
        application_feature_factory(f"60000{index}", clock.now(), industry="工业" if index < 4 else "银行")
        for index in range(1, 7)
    )
    repository = MemoryRepository()
    state = RuntimeState()
    market_data = StaticMarketData(features)
    pipeline = build_pipeline(
        market_data,
        TradingDayCalendar(),
        None,
        repository,
        InMemoryEventLedger(),
        SnapshotPublisher(history_size=32, client_queue_size=4),
        RecommendationEngine(recommendation_policy),
        state,
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=32,
        priority_queue_size=4,
        now=clock.now,
        long_codes=("600001", "600002"),
    )
    pipeline.initialize()

    today = pipeline.run_once(clock.now())
    assert [snapshot.strategy for snapshot in today] == [Strategy.TODAY]
    assert today[0].fusion_mode.value == "local_degraded"
    latency = pipeline.status()["dependencies"]["latency_waterfall"]
    assert latency["planned_count"] == 1
    assert latency["completed_count"] == 1
    assert latency["active_trace_count"] == 0
    assert latency["stages"]["cycle_total:run_once"]["sample_count"] == 1
    assert "deepseek_review" not in latency["stages"]

    clock.set(datetime.fromisoformat("2026-07-16T11:19:50+08:00"))
    pipeline.run_once(clock.now())
    clock.set(datetime.fromisoformat("2026-07-16T11:20:00+08:00"))
    morning_freeze = pipeline.run_once(clock.now())
    assert morning_freeze[-1].strategy is Strategy.TODAY
    assert morning_freeze[-1].frozen is True
    assert morning_freeze[-1].config_version == "config-v2"
    assert morning_freeze[-1].metadata["freeze_anchor"]["600001"]["age_seconds"] == 10.0

    clock.set(datetime.fromisoformat("2026-07-16T14:30:00+08:00"))
    afternoon = pipeline.run_once(clock.now())
    assert {snapshot.strategy for snapshot in afternoon} == {Strategy.TOMORROW, Strategy.D25, Strategy.LONG}
    assert market_data.candidate_tail_requests[-3:] == [True, False, False]
    assert market_data.tail_refreshes[-1] == tuple(feature.quote.code for feature in features)

    clock.set(datetime.fromisoformat("2026-07-16T14:49:50+08:00"))
    pipeline.run_once(clock.now())
    clock.set(datetime.fromisoformat("2026-07-16T14:50:00+08:00"))
    afternoon_freeze = pipeline.run_once(clock.now())
    assert {snapshot.strategy for snapshot in afternoon_freeze} == {Strategy.TOMORROW, Strategy.D25}
    assert all(snapshot.frozen for snapshot in afternoon_freeze)
    assert repository.frozen.keys() == {
        (Strategy.TODAY, "2026-07-16"),
        (Strategy.TOMORROW, "2026-07-16"),
        (Strategy.D25, "2026-07-16"),
    }

    clock.set(datetime.fromisoformat("2026-07-16T14:50:30+08:00"))
    assert pipeline.run_once(clock.now()) == ()
    assert len(repository.frozen) == 3
    assert state.latest(Strategy.LONG).frozen is False


@pytest.mark.parametrize("use_worker", [False, True], ids=("sync", "worker"))
def test_p6_rejection_preserves_previous_publication_state(
    recommendation_policy,
    application_feature_factory,
    *,
    use_worker: bool,
) -> None:
    now = datetime.fromisoformat("2026-07-16T11:19:50+08:00")
    repository = MemoryRepository()
    state = RuntimeState()
    publisher = SnapshotPublisher(history_size=8, client_queue_size=2)
    published = PublishedSnapshotIndex(repository, maximum_view_bytes=1)
    pipeline = build_pipeline(
        StaticMarketData((application_feature_factory("600001", now),)),
        TradingDayCalendar(),
        None,
        repository,
        repository,
        publisher,
        RecommendationEngine(recommendation_policy),
        state,
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=8,
        priority_queue_size=2,
        now=lambda: now,
        published_snapshots=published,
    )
    pipeline.initialize()

    if use_worker:
        pipeline.start()
        try:
            assert pipeline.submit_tick(now) is True
            _wait_until(lambda: pipeline.status()["counters"]["events_completed"] == 1)
        finally:
            pipeline.stop(timeout_seconds=2.0)
    else:
        assert pipeline.run_once(now) == ()

    status = state.snapshot()
    assert state.latest(Strategy.TODAY) is None
    assert published.latest(Strategy.TODAY) is None
    assert publisher.last_sequence() == 0
    assert repository.checkpoints == {}
    assert pipeline._session_snapshot_ids == set()
    assert status["counters"]["p6_snapshot_rejections"] == 1
    assert status["strategy_degraded_reasons"]["today"] == ("p6_snapshot_rejected",)


def test_restart_does_not_restore_a_frozen_snapshot_rejected_by_p6(
    recommendation_policy,
    application_feature_factory,
) -> None:
    now = datetime.fromisoformat("2026-07-16T10:00:00+08:00")
    repository = MemoryRepository()
    first = build_pipeline(
        StaticMarketData((application_feature_factory("600001", now),)),
        TradingDayCalendar(),
        None,
        repository,
        repository,
        SnapshotPublisher(history_size=8, client_queue_size=2),
        RecommendationEngine(recommendation_policy),
        RuntimeState(),
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=8,
        priority_queue_size=2,
        now=lambda: now,
    )
    first.initialize()
    draft = first.run_once(now)[0]
    repository.freeze(replace(draft, frozen=True))

    state = RuntimeState()
    publisher = SnapshotPublisher(history_size=8, client_queue_size=2)
    published = PublishedSnapshotIndex(repository, maximum_view_bytes=1)
    restarted = build_pipeline(
        StaticMarketData((application_feature_factory("600001", now),)),
        TradingDayCalendar(),
        None,
        repository,
        repository,
        publisher,
        RecommendationEngine(recommendation_policy),
        state,
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=8,
        priority_queue_size=2,
        now=lambda: now,
        published_snapshots=published,
    )

    restarted.initialize()

    assert state.latest(Strategy.TODAY) is None
    assert state.is_frozen(Strategy.TODAY, draft.trade_date) is False
    assert published.latest(Strategy.TODAY) is None
    assert publisher.last_sequence() == 0
    assert state.snapshot()["counters"]["p6_snapshot_rejections"] == 1


def test_restart_does_not_restore_a_frozen_replacement_rejected_by_p6(
    recommendation_policy,
    application_feature_factory,
) -> None:
    now = datetime.fromisoformat("2026-07-16T10:00:00+08:00")
    repository = MemoryRepository()
    first = build_pipeline(
        StaticMarketData((application_feature_factory("600001", now),)),
        TradingDayCalendar(),
        None,
        repository,
        repository,
        SnapshotPublisher(history_size=8, client_queue_size=2),
        RecommendationEngine(recommendation_policy),
        RuntimeState(),
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=8,
        priority_queue_size=2,
        now=lambda: now,
    )
    first.initialize()
    stored = replace(first.run_once(now)[0], frozen=True)
    repository.freeze(stored)
    published = PublishedSnapshotIndex(repository)
    assert published.publish(replace(stored, snapshot_id="already-pinned")) is True
    state = RuntimeState()
    restarted = build_pipeline(
        StaticMarketData((application_feature_factory("600001", now),)),
        TradingDayCalendar(),
        None,
        repository,
        repository,
        SnapshotPublisher(history_size=8, client_queue_size=2),
        RecommendationEngine(recommendation_policy),
        state,
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=8,
        priority_queue_size=2,
        now=lambda: now,
        published_snapshots=published,
    )

    restarted.initialize()

    assert state.latest(Strategy.TODAY) is None
    assert state.is_frozen(Strategy.TODAY, stored.trade_date) is False
    assert published.latest(Strategy.TODAY).snapshot_id == "already-pinned"
    assert state.snapshot()["counters"]["p6_snapshot_rejections"] == 1


def test_after_close_persists_current_run_p6_with_closing_prices(
    recommendation_policy,
    application_feature_factory,
) -> None:
    clock = MutableClock(datetime.fromisoformat("2026-07-16T10:30:00+08:00"))
    features = _three_board_features(application_feature_factory, clock.now())
    repository = MemoryRepository()
    state = RuntimeState()
    market_data = ClosingPriceMarketData(features)
    engine = RecommendationEngine(recommendation_policy)
    pipeline = build_pipeline(
        market_data,
        TradingDayCalendar(),
        None,
        repository,
        repository,
        SnapshotPublisher(history_size=32, client_queue_size=4),
        engine,
        state,
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=32,
        priority_queue_size=4,
        now=clock.now,
    )
    pipeline.initialize()

    assert pipeline.run_once(clock.now())[0].strategy is Strategy.TODAY
    clock.set(datetime.fromisoformat("2026-07-16T14:30:00+08:00"))
    pipeline.run_once(clock.now())
    before = {strategy: state.latest(strategy) for strategy in (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25)}
    assert all(snapshot is not None for snapshot in before.values())
    assert repository.frozen == {}

    clock.set(datetime.fromisoformat("2026-07-16T15:00:00+08:00"))
    recovered = pipeline.run_once(clock.now())

    assert {snapshot.strategy for snapshot in recovered} == {
        Strategy.TODAY,
        Strategy.TOMORROW,
        Strategy.D25,
    }
    for strategy, source in before.items():
        assert source is not None
        frozen = repository.load_frozen(strategy, "2026-07-16")
        assert frozen is not None
        assert frozen.frozen is True
        assert frozen.phase == "close_fallback"
        assert frozen.metadata["recovery_path"] == "p6"
        assert frozen.metadata["price_basis"] == "official_close"
        assert tuple((item.features.quote.code, item.score, item.rank) for item in frozen.recommendations) == tuple(
            (item.features.quote.code, item.score, item.rank) for item in source.recommendations
        )
        assert all(item.features.quote.price == 20.0 for item in frozen.recommendations)
        assert engine.verify_frozen(frozen)["status"] == "verified"
        restored = snapshot_from_dict(snapshot_to_dict(frozen))
        assert restored == frozen
        assert engine.verify_frozen(restored)["status"] == "verified"
        assert snapshot_envelope(restored, top_n=18)["phase"] == "close_fallback"


def test_after_close_cold_start_rebuilds_missing_strategies_locally(
    recommendation_policy,
    application_feature_factory,
) -> None:
    clock = MutableClock(datetime.fromisoformat("2026-07-16T15:05:00+08:00"))
    features = _three_board_features(application_feature_factory, clock.now())
    repository = MemoryRepository()
    market_data = ClosingPriceMarketData(features)
    state = RuntimeState()
    pipeline = build_pipeline(
        market_data,
        TradingDayCalendar(),
        FailingReviewer(),
        repository,
        repository,
        SnapshotPublisher(history_size=32, client_queue_size=4),
        RecommendationEngine(recommendation_policy),
        state,
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=32,
        priority_queue_size=4,
        now=clock.now,
    )
    pipeline.initialize()

    recovered = pipeline.run_once(clock.now())

    assert {snapshot.strategy for snapshot in recovered} == {
        Strategy.TODAY,
        Strategy.TOMORROW,
        Strategy.D25,
    }
    assert market_data.market_force_requests == [True]
    for snapshot in recovered:
        assert snapshot.frozen is True
        assert snapshot.phase == "close_fallback"
        assert snapshot.fusion_mode.value == "local_degraded"
        assert snapshot.metadata["recovery_path"] == "full_rebuild"
        assert snapshot.metadata["deepseek_mode"] == "local_only"
        assert repository.load_frozen(snapshot.strategy, "2026-07-16") == snapshot
        assert pipeline._engine.verify_frozen(snapshot)["status"] == "verified"

    queries = RecommendationQueries(
        state,
        now=clock.now,
    )
    lookup = queries.recommendation(Strategy.TOMORROW)
    assert lookup.status == "ready"
    assert lookup.snapshot is not None
    assert lookup.snapshot.phase == "close_fallback"


def test_after_close_cold_start_builds_long_current_snapshot(
    recommendation_policy,
    application_feature_factory,
) -> None:
    clock = MutableClock(datetime.fromisoformat("2026-07-16T15:05:00+08:00"))
    features = _three_board_features(application_feature_factory, clock.now())
    repository = MemoryRepository()
    state = RuntimeState()
    pipeline = build_pipeline(
        ClosingPriceMarketData(features),
        TradingDayCalendar(),
        None,
        repository,
        repository,
        SnapshotPublisher(history_size=32, client_queue_size=4),
        RecommendationEngine(recommendation_policy),
        state,
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=32,
        priority_queue_size=4,
        now=clock.now,
        long_codes=("600001", "300001", "688001"),
    )
    pipeline.initialize()

    recovered = pipeline.run_once(clock.now())

    assert Strategy.LONG in {snapshot.strategy for snapshot in recovered}
    long_snapshot = state.latest(Strategy.LONG)
    assert long_snapshot is not None
    assert long_snapshot.frozen is False
    assert long_snapshot.phase == "close_fallback"
    assert long_snapshot.metadata["recovery_path"] == "after_close_current"
    assert long_snapshot.metadata["price_basis"] == "official_close"
    assert {item.features.quote.code for item in long_snapshot.recommendations} == {
        "600001",
        "300001",
        "688001",
    }
    assert all(item.features.quote.price == 20.0 for item in long_snapshot.recommendations)
    assert repository.load_frozen(Strategy.LONG, "2026-07-16") is None
    assert state.snapshot()["counters"]["after_close_long_recovered"] == 1


def test_after_close_p6_rejection_keeps_formal_records_without_publication(
    recommendation_policy,
    application_feature_factory,
) -> None:
    clock = MutableClock(datetime.fromisoformat("2026-07-16T15:05:00+08:00"))
    repository = MemoryRepository()
    state = RuntimeState()
    publisher = SnapshotPublisher(history_size=32, client_queue_size=4)
    pipeline = build_pipeline(
        ClosingPriceMarketData(_three_board_features(application_feature_factory, clock.now())),
        TradingDayCalendar(),
        None,
        repository,
        repository,
        publisher,
        RecommendationEngine(recommendation_policy),
        state,
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=32,
        priority_queue_size=4,
        now=clock.now,
        published_snapshots=PublishedSnapshotIndex(repository, maximum_view_bytes=1),
    )
    pipeline.initialize()

    assert pipeline.run_once(clock.now()) == ()

    for strategy in (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25):
        formal = repository.load_frozen(strategy, "2026-07-16")
        assert formal is not None
        assert formal.phase == "close_fallback"
        assert state.latest(strategy) is None
        assert state.is_frozen(strategy, "2026-07-16") is False
    assert publisher.last_sequence() == 0
    assert state.snapshot()["counters"]["p6_snapshot_rejections"] >= 3


def test_after_close_cold_start_prefers_existing_database_records(
    recommendation_policy,
    application_feature_factory,
) -> None:
    clock = MutableClock(datetime.fromisoformat("2026-07-16T15:05:00+08:00"))
    features = _three_board_features(application_feature_factory, clock.now())
    repository = MemoryRepository()
    first_market = ClosingPriceMarketData(features)
    first = build_pipeline(
        first_market,
        TradingDayCalendar(),
        None,
        repository,
        repository,
        SnapshotPublisher(history_size=32, client_queue_size=4),
        RecommendationEngine(recommendation_policy),
        RuntimeState(),
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=32,
        priority_queue_size=4,
        now=clock.now,
    )
    first.initialize()
    first.run_once(clock.now())
    existing = dict(repository.frozen)

    second_market = ClosingPriceMarketData(features)
    second_state = RuntimeState()
    second = build_pipeline(
        second_market,
        TradingDayCalendar(),
        None,
        repository,
        repository,
        SnapshotPublisher(history_size=32, client_queue_size=4),
        RecommendationEngine(recommendation_policy),
        second_state,
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=32,
        priority_queue_size=4,
        now=clock.now,
    )
    second.initialize()

    assert second.run_once(clock.now()) == ()
    assert repository.frozen == existing
    assert second_market.market_force_requests == []
    assert all(
        second_state.latest(strategy) == repository.load_frozen(strategy, "2026-07-16")
        for strategy in (
            Strategy.TODAY,
            Strategy.TOMORROW,
            Strategy.D25,
        )
    )


def test_after_close_cold_start_rebuilds_only_missing_strategy(
    recommendation_policy,
    application_feature_factory,
) -> None:
    clock = MutableClock(datetime.fromisoformat("2026-07-16T15:05:00+08:00"))
    features = _three_board_features(application_feature_factory, clock.now())
    repository = MemoryRepository()
    first = build_pipeline(
        ClosingPriceMarketData(features),
        TradingDayCalendar(),
        None,
        repository,
        repository,
        SnapshotPublisher(history_size=32, client_queue_size=4),
        RecommendationEngine(recommendation_policy),
        RuntimeState(),
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=32,
        priority_queue_size=4,
        now=clock.now,
    )
    first.initialize()
    first.run_once(clock.now())
    preserved = {
        strategy: repository.load_frozen(strategy, "2026-07-16") for strategy in (Strategy.TODAY, Strategy.TOMORROW)
    }
    repository.frozen.pop((Strategy.D25, "2026-07-16"))

    second_market = ClosingPriceMarketData(features)
    second = build_pipeline(
        second_market,
        TradingDayCalendar(),
        None,
        repository,
        repository,
        SnapshotPublisher(history_size=32, client_queue_size=4),
        RecommendationEngine(recommendation_policy),
        RuntimeState(),
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=32,
        priority_queue_size=4,
        now=clock.now,
    )
    second.initialize()

    recovered = second.run_once(clock.now())

    assert [snapshot.strategy for snapshot in recovered] == [Strategy.D25]
    assert second_market.market_force_requests == [True]
    assert repository.load_frozen(Strategy.D25, "2026-07-16") is not None
    assert {
        strategy: repository.load_frozen(strategy, "2026-07-16") for strategy in (Strategy.TODAY, Strategy.TOMORROW)
    } == preserved


def test_after_close_does_not_persist_partial_market_rebuild(
    recommendation_policy,
    application_feature_factory,
) -> None:
    clock = MutableClock(datetime.fromisoformat("2026-07-16T15:05:00+08:00"))
    features = _three_board_features(application_feature_factory, clock.now())
    repository = MemoryRepository()
    market_data = IncompleteClosingMarketData(features)
    pipeline = build_pipeline(
        market_data,
        TradingDayCalendar(),
        None,
        repository,
        repository,
        SnapshotPublisher(history_size=32, client_queue_size=4),
        RecommendationEngine(recommendation_policy),
        RuntimeState(),
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=32,
        priority_queue_size=4,
        now=clock.now,
    )
    pipeline.initialize()

    assert pipeline.run_once(clock.now()) == ()
    assert repository.frozen == {}

    market_data.complete = True
    clock.set(datetime.fromisoformat("2026-07-16T15:05:03+08:00"))
    recovered = pipeline.run_once(clock.now())
    assert {snapshot.strategy for snapshot in recovered} == {
        Strategy.TODAY,
        Strategy.TOMORROW,
        Strategy.D25,
    }


def test_after_close_waits_for_complete_historical_board_population(
    recommendation_policy,
    application_feature_factory,
) -> None:
    clock = MutableClock(datetime.fromisoformat("2026-07-16T15:05:00+08:00"))
    features = _three_board_features(application_feature_factory, clock.now())
    repository = MemoryRepository()
    market_data = WarmingClosingMarketData(features)
    state = RuntimeState()
    pipeline = build_pipeline(
        market_data,
        TradingDayCalendar(),
        None,
        repository,
        repository,
        SnapshotPublisher(history_size=32, client_queue_size=4),
        RecommendationEngine(recommendation_policy),
        state,
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=32,
        priority_queue_size=4,
        now=clock.now,
    )
    pipeline.initialize()

    assert pipeline.run_once(clock.now()) == ()
    assert repository.frozen == {}
    assert market_data.market_force_requests == [True]
    assert state.snapshot()["last_error"] == (
        "after-close market recovery waiting for complete three-board close quotes and history: "
        "close_quotes=main:100,chinext:100,star:100; "
        "history=main:0,chinext:0,star:0"
    )

    clock.set(datetime.fromisoformat("2026-07-16T15:05:03+08:00"))
    market_data.warmed = True

    recovered = pipeline.run_once(clock.now())

    assert {snapshot.strategy for snapshot in recovered} == {
        Strategy.TODAY,
        Strategy.TOMORROW,
        Strategy.D25,
    }
    assert all(snapshot.recommendations for snapshot in recovered)
    assert all(
        "board_population_insufficient" not in reason and "board_data_reliability_below_threshold" not in reason
        for snapshot in recovered
        for reason in snapshot.degraded_reasons
    )
    assert market_data.market_force_requests == [True, True]
    assert all(
        repository.load_frozen(strategy, "2026-07-16") is not None
        for strategy in (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25)
    )
    current = RecommendationQueries(state, now=clock.now).current_recommendation(Strategy.TODAY)
    assert current.status == "ready"
    assert current.snapshot is not None
    assert current.snapshot.phase == "close_fallback"


def test_after_close_accepts_quotes_received_after_request_started(
    recommendation_policy,
    application_feature_factory,
) -> None:
    clock = MutableClock(datetime.fromisoformat("2026-07-16T15:05:00+08:00"))
    features = _three_board_features(application_feature_factory, clock.now())
    repository = MemoryRepository()
    market_data = DelayedClosingMarketData(features, clock)
    pipeline = build_pipeline(
        market_data,
        TradingDayCalendar(),
        None,
        repository,
        repository,
        SnapshotPublisher(history_size=32, client_queue_size=4),
        RecommendationEngine(recommendation_policy),
        RuntimeState(),
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=32,
        priority_queue_size=4,
        now=clock.now,
    )
    pipeline.initialize()

    recovered = pipeline.run_once(datetime.fromisoformat("2026-07-16T15:05:00+08:00"))

    assert {snapshot.strategy for snapshot in recovered} == {
        Strategy.TODAY,
        Strategy.TOMORROW,
        Strategy.D25,
    }
    assert all(snapshot.recommendations for snapshot in recovered)


def test_after_close_rebuild_reads_cached_candidate_features(
    recommendation_policy,
    application_feature_factory,
) -> None:
    clock = MutableClock(datetime.fromisoformat("2026-07-16T15:05:00+08:00"))
    features = _three_board_features(application_feature_factory, clock.now())
    repository = MemoryRepository()
    market_data = CachedOnlyClosingMarketData(features)
    pipeline = build_pipeline(
        market_data,
        TradingDayCalendar(),
        None,
        repository,
        repository,
        SnapshotPublisher(history_size=32, client_queue_size=4),
        RecommendationEngine(recommendation_policy),
        RuntimeState(),
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=32,
        priority_queue_size=4,
        now=clock.now,
    )
    pipeline.initialize()

    recovered = pipeline.run_once(clock.now())

    assert {snapshot.strategy for snapshot in recovered} == {
        Strategy.TODAY,
        Strategy.TOMORROW,
        Strategy.D25,
    }
    assert market_data.cached_candidate_reads == 3


def test_after_close_retry_reuses_complete_cached_close_market(
    recommendation_policy,
    application_feature_factory,
) -> None:
    clock = MutableClock(datetime.fromisoformat("2026-07-16T15:05:00+08:00"))
    features = _three_board_features(application_feature_factory, clock.now())
    cached_close = ClosingPriceMarketData._closing(features, clock.now())
    repository = MemoryRepository()
    market_data = CachedCloseMarketOnlyMarketData(features)
    pipeline = build_pipeline(
        market_data,
        TradingDayCalendar(),
        None,
        repository,
        repository,
        SnapshotPublisher(history_size=32, client_queue_size=4),
        RecommendationEngine(recommendation_policy),
        RuntimeState(),
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=32,
        priority_queue_size=4,
        now=clock.now,
    )
    pipeline.initialize()
    pipeline._market_features = cached_close
    pipeline._after_close_retry_attempt = 1

    recovered = pipeline.run_once(clock.now())

    assert {snapshot.strategy for snapshot in recovered} == {
        Strategy.TODAY,
        Strategy.TOMORROW,
        Strategy.D25,
    }
    assert market_data.market_fetch_attempts == 0
    assert market_data.cached_candidate_reads == 3


def test_after_close_commits_ready_strategies_when_d25_research_is_missing(
    application_feature_factory,
) -> None:
    clock = MutableClock(datetime.fromisoformat("2026-07-16T15:05:00+08:00"))
    features = _three_board_features(application_feature_factory, clock.now())
    repository = MemoryRepository()
    state = RuntimeState()
    market_data = MissingD25ResearchClosingMarketData(features)
    pipeline = build_pipeline(
        market_data,
        TradingDayCalendar(),
        None,
        repository,
        repository,
        SnapshotPublisher(history_size=32, client_queue_size=4),
        RecommendationEngine(
            _recommendation_policy(
                load_strategy_settings(Path(__file__).parents[2] / "config" / "v2" / "strategy.json")
            )
        ),
        state,
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=32,
        priority_queue_size=4,
        now=clock.now,
    )
    pipeline.initialize()

    recovered = pipeline.run_once(clock.now())

    assert {snapshot.strategy for snapshot in recovered} == {
        Strategy.TODAY,
        Strategy.TOMORROW,
        Strategy.D25,
    }
    assert repository.load_frozen(Strategy.TODAY, "2026-07-16") is not None
    assert repository.load_frozen(Strategy.TOMORROW, "2026-07-16") is not None
    d25 = repository.load_frozen(Strategy.D25, "2026-07-16")
    assert d25 is not None
    assert d25.phase == "close_fallback"
    assert d25.recommendations
    assert any("board_data_reliability_below_threshold" in reason for reason in d25.degraded_reasons)
    assert all(item.action.value == "observe" for item in d25.recommendations)
    assert "d25 close rebuild degraded" not in state.snapshot()["last_error"]


def test_after_close_publishes_unreliable_board_features_as_degraded_observe(
    application_feature_factory,
) -> None:
    clock = MutableClock(datetime.fromisoformat("2026-07-16T15:05:00+08:00"))
    features = _three_board_features(application_feature_factory, clock.now())
    repository = MemoryRepository()
    market_data = UnreliableClosingMarketData(features)
    pipeline = build_pipeline(
        market_data,
        TradingDayCalendar(),
        None,
        repository,
        repository,
        SnapshotPublisher(history_size=32, client_queue_size=4),
        RecommendationEngine(
            _recommendation_policy(
                load_strategy_settings(Path(__file__).parents[2] / "config" / "v2" / "strategy.json")
            )
        ),
        RuntimeState(),
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=32,
        priority_queue_size=4,
        now=clock.now,
    )
    pipeline.initialize()

    recovered = pipeline.run_once(clock.now())

    assert {snapshot.strategy for snapshot in recovered} == {
        Strategy.TODAY,
        Strategy.TOMORROW,
        Strategy.D25,
    }
    assert all(
        repository.load_frozen(strategy, "2026-07-16") is not None
        for strategy in (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25)
    )
    d25 = next(snapshot for snapshot in recovered if snapshot.strategy is Strategy.D25)
    assert any("board_data_reliability_below_threshold" in reason for reason in d25.degraded_reasons)
    assert all(item.action.value == "observe" for item in d25.recommendations)


@pytest.mark.parametrize(
    ("strategy", "boundary", "phase", "quote_age_seconds", "maximum_age"),
    (
        (Strategy.TODAY, "2026-07-16T11:20:00+08:00", "today_late", 21.0, 20.0),
        (Strategy.TODAY, "2026-07-16T11:20:00+08:00", "today_late", -1.0, 20.0),
        (Strategy.TOMORROW, "2026-07-16T14:50:00+08:00", "final_quote", 31.0, 30.0),
        (Strategy.D25, "2026-07-16T14:50:00+08:00", "final_quote", 31.0, 30.0),
    ),
)
def test_freeze_rejects_snapshot_when_any_quote_is_outside_boundary_age(
    recommendation_policy,
    application_feature_factory,
    strategy,
    boundary,
    phase,
    quote_age_seconds,
    maximum_age,
) -> None:
    boundary = datetime.fromisoformat(boundary)
    draft_time = boundary - timedelta(seconds=10)
    state = RuntimeState()
    repository = MemoryRepository()
    engine = RecommendationEngine(recommendation_policy)
    draft = engine.build_snapshot(
        strategy,
        (
            application_feature_factory("600001", draft_time),
            application_feature_factory("600002", draft_time),
        ),
        now=draft_time,
        phase=phase,
        trade_date="2026-07-16",
        data_version="stale-anchor",
        review_port=None,
        review_deadline=boundary,
        max_age_seconds=maximum_age,
        filtered_count=0,
        filter_reasons={},
    )
    recommendation = draft.recommendations[0]
    stale_quote = replace(
        recommendation.features.quote,
        source_time=boundary - timedelta(seconds=quote_age_seconds),
    )
    draft = replace(
        draft,
        recommendations=(
            replace(recommendation, features=replace(recommendation.features, quote=stale_quote)),
            *draft.recommendations[1:],
        ),
        config_version="config-v2",
    )
    repository.publish(draft)
    state.publish(draft)
    pipeline = build_pipeline(
        StaticMarketData(()),
        TradingDayCalendar(),
        None,
        repository,
        repository,
        SnapshotPublisher(history_size=4, client_queue_size=2),
        engine,
        state,
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=8,
        priority_queue_size=2,
        now=lambda: boundary,
    )

    assert pipeline._freeze_available_snapshots(boundary, (strategy.value,)) == ()
    assert repository.frozen == {}
    assert "quote age" in pipeline.status()["last_error"]


@pytest.mark.parametrize(
    ("strategy", "boundary", "phase", "maximum_age"),
    (
        (Strategy.TODAY, "2026-07-16T11:20:00+08:00", "today_late", 20.0),
        (Strategy.TOMORROW, "2026-07-16T14:50:00+08:00", "final_quote", 30.0),
        (Strategy.D25, "2026-07-16T14:50:00+08:00", "final_quote", 30.0),
    ),
)
def test_freeze_accepts_exact_quote_age_boundary(
    recommendation_policy,
    application_feature_factory,
    strategy,
    boundary,
    phase,
    maximum_age,
) -> None:
    boundary = datetime.fromisoformat(boundary)
    draft_time = boundary - timedelta(seconds=10)
    feature = application_feature_factory("600001", draft_time)
    feature = replace(feature, quote=replace(feature.quote, source_time=boundary - timedelta(seconds=maximum_age)))
    state = RuntimeState()
    repository = MemoryRepository()
    engine = RecommendationEngine(recommendation_policy)
    draft = engine.build_snapshot(
        strategy,
        (feature,),
        now=draft_time,
        phase=phase,
        trade_date="2026-07-16",
        data_version="boundary-anchor",
        review_port=None,
        review_deadline=boundary,
        max_age_seconds=maximum_age,
        filtered_count=0,
        filter_reasons={},
    )
    draft = replace(draft, config_version="config-v2")
    repository.publish(draft)
    state.publish(draft)
    pipeline = build_pipeline(
        StaticMarketData(()),
        TradingDayCalendar(),
        None,
        repository,
        repository,
        SnapshotPublisher(history_size=4, client_queue_size=2),
        engine,
        state,
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=8,
        priority_queue_size=2,
        now=lambda: boundary,
    )

    frozen = pipeline._freeze_available_snapshots(boundary, (strategy.value,))

    assert len(frozen) == 1
    assert frozen[0].metadata["freeze_anchor"]["600001"]["age_seconds"] == maximum_age


def test_freeze_p6_rejection_keeps_formal_record_without_advancing_runtime(
    recommendation_policy,
    application_feature_factory,
) -> None:
    boundary = datetime.fromisoformat("2026-07-16T11:20:00+08:00")
    draft_time = boundary - timedelta(seconds=10)
    feature = application_feature_factory("600001", draft_time)
    engine = RecommendationEngine(recommendation_policy)
    draft = replace(
        engine.build_snapshot(
            Strategy.TODAY,
            (feature,),
            now=draft_time,
            phase="today_late",
            trade_date="2026-07-16",
            data_version="p6-rejected-freeze",
            review_port=None,
            review_deadline=boundary,
            max_age_seconds=20.0,
            filtered_count=0,
            filter_reasons={},
        ),
        config_version="config-v2",
    )
    repository = MemoryRepository()
    repository.publish(draft)
    repository.save_checkpoint(draft, boundary_at=boundary)
    state = RuntimeState()
    state.publish(draft)
    publisher = SnapshotPublisher(history_size=4, client_queue_size=2)
    pipeline = build_pipeline(
        StaticMarketData(()),
        TradingDayCalendar(),
        None,
        repository,
        repository,
        publisher,
        engine,
        state,
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=8,
        priority_queue_size=2,
        now=lambda: boundary,
        published_snapshots=PublishedSnapshotIndex(repository, maximum_view_bytes=1),
    )

    assert pipeline._freeze_available_snapshots(boundary, (Strategy.TODAY.value,)) == ()

    formal = repository.load_frozen(Strategy.TODAY, draft.trade_date)
    assert formal is not None
    assert formal.frozen is True
    assert state.latest(Strategy.TODAY) == draft
    assert state.is_frozen(Strategy.TODAY, draft.trade_date) is False
    assert repository.load_checkpoint(Strategy.TODAY, draft.trade_date, boundary_at=boundary) == draft
    assert publisher.last_sequence() == 0


def test_frozen_topk_uses_recoverable_overlay_and_keeps_close_value(
    recommendation_policy,
    application_feature_factory,
) -> None:
    frozen_at = datetime.fromisoformat("2026-07-16T14:50:00+08:00")
    quote_at = frozen_at - timedelta(seconds=10)
    repository = MemoryRepository()
    state = RuntimeState()
    snapshot = RecommendationEngine(recommendation_policy).build_snapshot(
        Strategy.TOMORROW,
        (application_feature_factory("600001", quote_at),),
        now=quote_at,
        phase="final_quote",
        trade_date="2026-07-16",
        data_version="freeze-v1",
        review_port=None,
        review_deadline=frozen_at,
        max_age_seconds=30.0,
        filtered_count=0,
        filter_reasons={},
    )
    frozen = replace(snapshot, frozen=True, published_at=frozen_at, config_version="config-v2")
    repository.frozen[(Strategy.TOMORROW, frozen.trade_date)] = frozen
    repository.published[Strategy.TOMORROW] = frozen
    clock = MutableClock(frozen_at + timedelta(seconds=10))
    market_data = DegradingCandidateMarketData((application_feature_factory("600001", clock.now()),))
    pipeline = build_pipeline(
        market_data,
        TradingDayCalendar(),
        None,
        repository,
        repository,
        SnapshotPublisher(history_size=4, client_queue_size=2),
        RecommendationEngine(recommendation_policy),
        state,
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=8,
        priority_queue_size=2,
        now=clock.now,
    )
    pipeline.initialize()

    assert pipeline.run_once(clock.now()) == ()
    overlay = state.load_live_overlay(Strategy.TOMORROW, "2026-07-16")
    assert isinstance(overlay, LiveOverlay)
    assert overlay.snapshot_id == frozen.snapshot_id
    assert overlay.closing is False
    assert (Strategy.TOMORROW, "2026-07-16") not in repository.overlays
    market_data.candidate_unavailable = True
    clock.set(datetime.fromisoformat("2026-07-16T14:50:20+08:00"))
    pipeline.run_once(clock.now())
    assert state.load_live_overlay(Strategy.TOMORROW, "2026-07-16").version == overlay.version
    market_data.candidate_unavailable = False
    clock.set(datetime.fromisoformat("2026-07-16T15:00:00+08:00"))
    pipeline.run_once(clock.now())
    closing = repository.overlays[(Strategy.TOMORROW, "2026-07-16")]
    assert closing.closing is True
    assert state.load_live_overlay(Strategy.TOMORROW, "2026-07-16") == closing
    clock.set(datetime.fromisoformat("2026-07-16T15:01:00+08:00"))
    pipeline.run_once(clock.now())
    assert repository.overlays[(Strategy.TOMORROW, "2026-07-16")].version == closing.version


def test_initialize_restores_frozen_gate(recommendation_policy, application_feature_factory) -> None:
    now = datetime.fromisoformat("2026-07-16T10:00:00+08:00")
    features = (application_feature_factory("600001", now),)
    repository = MemoryRepository()
    repository.frozen[(Strategy.TODAY, "2026-07-16")] = object()
    pipeline = build_pipeline(
        StaticMarketData(features),
        TradingDayCalendar(),
        None,
        repository,
        repository,
        SnapshotPublisher(history_size=4, client_queue_size=2),
        RecommendationEngine(recommendation_policy),
        RuntimeState(),
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=8,
        priority_queue_size=2,
        now=lambda: now,
    )

    pipeline.initialize()

    assert pipeline.run_once(now) == ()
    assert repository.published == {}


def test_initialize_catches_up_pre_cutoff_today_snapshot_after_missed_window(
    recommendation_policy,
    application_feature_factory,
) -> None:
    clock = MutableClock(datetime.fromisoformat("2026-07-16T11:19:50+08:00"))
    features = (application_feature_factory("600001", clock.now()),)
    repository = MemoryRepository()
    first = build_pipeline(
        StaticMarketData(features),
        TradingDayCalendar(),
        None,
        repository,
        repository,
        SnapshotPublisher(history_size=4, client_queue_size=2),
        RecommendationEngine(recommendation_policy),
        RuntimeState(),
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=8,
        priority_queue_size=2,
        now=clock.now,
    )
    first.initialize()
    assert first.run_once(clock.now())[0].strategy is Strategy.TODAY

    clock.set(datetime.fromisoformat("2026-07-16T13:05:00+08:00"))
    restarted = build_pipeline(
        StaticMarketData(features),
        TradingDayCalendar(),
        None,
        repository,
        repository,
        SnapshotPublisher(history_size=4, client_queue_size=2),
        RecommendationEngine(recommendation_policy),
        RuntimeState(),
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=8,
        priority_queue_size=2,
        now=clock.now,
    )

    recovery = restarted.initialize()

    frozen = repository.frozen[(Strategy.TODAY, "2026-07-16")]
    assert isinstance(frozen, RecommendationSnapshot)
    assert recovery["catchup_frozen"] == 1
    assert frozen.frozen is True
    assert frozen.published_at.hour == 11
    assert frozen.published_at.minute == 20


def test_initialize_skips_today_freeze_when_no_pre_cutoff_snapshot(
    recommendation_policy,
    application_feature_factory,
) -> None:
    now = datetime.fromisoformat("2026-07-16T14:55:00+08:00")
    draft_time = datetime.fromisoformat("2026-07-16T14:49:50+08:00")
    repository = MemoryRepository()
    engine = RecommendationEngine(recommendation_policy)

    tomorrow = engine.build_snapshot(
        Strategy.TOMORROW,
        (application_feature_factory("600001", draft_time),),
        now=draft_time,
        phase="final_quote",
        trade_date="2026-07-16",
        data_version="init-catchup",
        review_port=None,
        review_deadline=now.replace(hour=14, minute=50, second=0),
        max_age_seconds=30.0,
        filtered_count=0,
        filter_reasons={},
    )
    d25 = engine.build_snapshot(
        Strategy.D25,
        (application_feature_factory("600002", draft_time),),
        now=draft_time,
        phase="final_quote",
        trade_date="2026-07-16",
        data_version="init-catchup",
        review_port=None,
        review_deadline=now.replace(hour=14, minute=50, second=0),
        max_age_seconds=30.0,
        filtered_count=0,
        filter_reasons={},
    )
    boundary = now.replace(hour=14, minute=50, second=0, microsecond=0)
    repository.save_checkpoint(replace(tomorrow, config_version="config-v2"), boundary_at=boundary)
    repository.save_checkpoint(replace(d25, config_version="config-v2"), boundary_at=boundary)
    pipeline = build_pipeline(
        StaticMarketData(()),
        TradingDayCalendar(),
        None,
        repository,
        repository,
        SnapshotPublisher(history_size=4, client_queue_size=2),
        engine,
        RuntimeState(),
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=8,
        priority_queue_size=2,
        now=lambda: now,
    )

    recovery = pipeline.initialize()

    assert recovery["catchup_frozen"] == 2
    assert "today freeze unavailable: no current pre-cutoff snapshot" not in pipeline.status()["last_error"]
    assert set(repository.frozen) == {
        (Strategy.TOMORROW, "2026-07-16"),
        (Strategy.D25, "2026-07-16"),
    }


def test_initialize_skips_today_freeze_when_pre_cutoff_snapshot_is_stale(
    recommendation_policy,
    application_feature_factory,
) -> None:
    now = datetime.fromisoformat("2026-07-16T14:55:00+08:00")
    draft_time = datetime.fromisoformat("2026-07-16T14:49:00+08:00")
    repository = MemoryRepository()
    engine = RecommendationEngine(recommendation_policy)

    stale_today = engine.build_snapshot(
        Strategy.TODAY,
        (application_feature_factory("600001", draft_time),),
        now=draft_time,
        phase="final_quote",
        trade_date="2026-07-16",
        data_version="stale-init-catchup",
        review_port=None,
        review_deadline=now.replace(hour=11, minute=20, second=0),
        max_age_seconds=20.0,
        filtered_count=0,
        filter_reasons={},
    )
    repository.publish(replace(stale_today, config_version="config-v2"))
    pipeline = build_pipeline(
        StaticMarketData(()),
        TradingDayCalendar(),
        None,
        repository,
        repository,
        SnapshotPublisher(history_size=4, client_queue_size=2),
        engine,
        RuntimeState(),
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=8,
        priority_queue_size=2,
        now=lambda: now,
    )

    recovery = pipeline.initialize()

    assert recovery["catchup_frozen"] == 0
    assert "today freeze unavailable: no current pre-cutoff snapshot" not in pipeline.status()["last_error"]
    assert not any(strategy is Strategy.TODAY for strategy, _ in repository.frozen)


def test_outcome_settlement_superseded_request_does_not_replace_last_error(
    recommendation_policy,
) -> None:
    now = datetime.fromisoformat("2026-07-16T15:05:00+08:00")
    state = RuntimeState()
    state.record_error("d25 close rebuild degraded: research fields missing")
    pipeline = build_pipeline(
        StaticMarketData(()),
        TradingDayCalendar(),
        None,
        MemoryRepository(),
        MemoryRepository(),
        SnapshotPublisher(history_size=4, client_queue_size=2),
        RecommendationEngine(recommendation_policy),
        state,
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=8,
        priority_queue_size=2,
        now=lambda: now,
        outcome_settlement=SupersededOutcomeSettlement(),
    )

    pipeline._settle_outcomes(now)

    status = pipeline.status()
    assert status["last_error"] == "d25 close rebuild degraded: research fields missing"
    assert status["counters"]["outcome_settlement_superseded"] == 1
    assert "outcome_settlement_failures" not in status["counters"]


def test_missing_pre_cutoff_freeze_is_counted_without_replacing_last_error(
    recommendation_policy,
) -> None:
    now = datetime.fromisoformat("2026-07-16T14:50:00+08:00")
    state = RuntimeState()
    state.record_error("after-close recovery waiting for history")
    pipeline = build_pipeline(
        StaticMarketData(()),
        TradingDayCalendar(),
        None,
        MemoryRepository(),
        MemoryRepository(),
        SnapshotPublisher(history_size=4, client_queue_size=2),
        RecommendationEngine(recommendation_policy),
        state,
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=8,
        priority_queue_size=2,
        now=lambda: now,
    )

    assert freeze_available_snapshots(pipeline, now, ("d25",)) == ()

    status = pipeline.status()
    assert status["last_error"] == "after-close recovery waiting for history"
    assert status["counters"]["freeze_missing_pre_cutoff_snapshot"] == 1


def test_freeze_reuses_persisted_snapshot_when_state_has_no_live_copy(
    recommendation_policy,
    application_feature_factory,
) -> None:
    boundary = datetime.fromisoformat("2026-07-16T11:20:00+08:00")
    draft_time = boundary - timedelta(seconds=10)
    feature = application_feature_factory("600001", draft_time)
    engine = RecommendationEngine(recommendation_policy)
    draft = engine.build_snapshot(
        Strategy.TODAY,
        (feature,),
        now=draft_time,
        phase="today_late",
        trade_date="2026-07-16",
        data_version="state-recovery",
        review_port=None,
        review_deadline=boundary,
        max_age_seconds=20.0,
        filtered_count=0,
        filter_reasons={},
    )
    draft = replace(draft, config_version="config-v2")

    repository = MemoryRepository()
    repository.save_checkpoint(draft, boundary_at=boundary)

    pipeline = build_pipeline(
        StaticMarketData((feature,)),
        TradingDayCalendar(),
        None,
        repository,
        repository,
        SnapshotPublisher(history_size=4, client_queue_size=2),
        engine,
        RuntimeState(),
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=8,
        priority_queue_size=2,
        now=lambda: boundary,
    )

    frozen = pipeline._freeze_available_snapshots(boundary, (Strategy.TODAY.value,))

    assert len(frozen) == 1
    assert frozen[0].strategy is Strategy.TODAY
    assert frozen[0].frozen is True
    assert frozen[0].published_at == boundary
    assert frozen[0].snapshot_id == draft.snapshot_id
    assert repository.frozen[(Strategy.TODAY, "2026-07-16")] == frozen[0]


def test_market_data_unavailability_preserves_candidates_and_records_degradation(
    recommendation_policy,
    application_feature_factory,
    caplog,
) -> None:
    clock = MutableClock(datetime.fromisoformat("2026-07-16T10:00:00+08:00"))
    market_data = DegradingMarketData((application_feature_factory("600001", clock.now()),))
    repository = MemoryRepository()
    pipeline = build_pipeline(
        market_data,
        TradingDayCalendar(),
        None,
        repository,
        repository,
        SnapshotPublisher(history_size=4, client_queue_size=2),
        RecommendationEngine(recommendation_policy),
        RuntimeState(),
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=8,
        priority_queue_size=2,
        now=clock.now,
    )
    pipeline.initialize()
    assert len(pipeline.run_once(clock.now())) == 1

    market_data.market_unavailable = True
    clock.set(datetime.fromisoformat("2026-07-16T10:00:10+08:00"))
    degraded = pipeline.run_once(clock.now())

    assert len(degraded) == 1
    assert degraded[0].recommendations
    status = pipeline.status()
    assert status["counters"]["market_refresh_failures"] == 1
    assert status["last_error"] == "market data degraded during today_main: all full-market sources failed"
    assert "Traceback" not in caplog.text


def test_history_warming_empty_selection_preserves_last_valid_candidate_pool(
    recommendation_policy,
    application_feature_factory,
) -> None:
    now = datetime.fromisoformat("2026-07-16T10:00:00+08:00")
    feature = application_feature_factory("600001", now)
    market_data = StaticMarketData((feature,))
    repository = MemoryRepository()
    pipeline = build_pipeline(
        market_data,
        TradingDayCalendar(),
        None,
        repository,
        repository,
        SnapshotPublisher(history_size=4, client_queue_size=2),
        RecommendationEngine(recommendation_policy),
        RuntimeState(),
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=8,
        priority_queue_size=2,
        now=lambda: now,
    )

    refresh_candidates(pipeline, now, MarketPhase.TODAY_MAIN)
    assert pipeline._candidate_codes == ("600001",)
    market_data._features = (
        replace(
            feature,
            values={**feature.values, "amount_median_20d": None},
            history_days=0,
        ),
    )

    refresh_candidates(pipeline, now + timedelta(seconds=1), MarketPhase.TODAY_MAIN)

    assert pipeline._candidate_codes == ("600001",)
    assert pipeline.status()["counters"]["candidate_selection_preserved_degraded"] == 1
    assert pipeline._filter_reasons["history_warming"] == 1


def test_status_uses_recorded_phase_without_calling_calendar(recommendation_policy) -> None:
    now = datetime.fromisoformat("2026-07-16T10:00:00+08:00")
    state = RuntimeState()
    state.record_tick("today_main", now)
    repository = MemoryRepository()
    pipeline = build_pipeline(
        StaticMarketData(()),
        ForbiddenStatusCalendar(),
        None,
        repository,
        repository,
        SnapshotPublisher(history_size=4, client_queue_size=2),
        RecommendationEngine(recommendation_policy),
        state,
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=8,
        priority_queue_size=2,
        now=lambda: now,
    )

    status = pipeline.status()

    assert status["phase"] == "today_main"


def test_freeze_tick_uses_reserved_priority_and_is_reserved_before_enqueue(
    recommendation_policy,
    application_feature_factory,
) -> None:
    now = datetime.fromisoformat("2026-07-16T11:20:00+08:00")
    repository = MemoryRepository()
    pipeline = build_pipeline(
        StaticMarketData((application_feature_factory("600001", now),)),
        TradingDayCalendar(),
        None,
        repository,
        repository,
        SnapshotPublisher(history_size=4, client_queue_size=2),
        RecommendationEngine(recommendation_policy),
        RuntimeState(),
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=2,
        priority_queue_size=1,
        now=lambda: now,
    )
    queue = RecordingQueue()
    pipeline._queue = queue

    assert pipeline.submit_tick(now) is True

    event = queue.events[0]
    assert event.event_type == "freeze"
    assert event.priority is EventPriority.FREEZE
    assert repository.events[0].status is EventStatus.PENDING


def test_risk_event_is_reserved_before_priority_enqueue(
    recommendation_policy,
    application_feature_factory,
) -> None:
    now = datetime.fromisoformat("2026-07-16T10:00:00+08:00")
    repository = MemoryRepository()
    pipeline = build_pipeline(
        StaticMarketData((application_feature_factory("600001", now),)),
        TradingDayCalendar(),
        None,
        repository,
        repository,
        SnapshotPublisher(history_size=4, client_queue_size=2),
        RecommendationEngine(recommendation_policy),
        RuntimeState(),
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=2,
        priority_queue_size=1,
        now=lambda: now,
    )
    queue = RecordingQueue()
    pipeline._queue = queue
    event = new_event(
        "risk_change",
        subject_key="600001",
        trade_date="2026-07-16",
        phase="today_main",
        strategy=Strategy.TODAY,
        priority=EventPriority.RISK,
        data_version="risk-v1",
        config_version="config-v2",
        created_at=now,
    )

    assert pipeline.submit_event(event) is True
    assert queue.events == [event]
    assert repository.events[0].event_id == event.event_id
    assert repository.events[0].status is EventStatus.PENDING


def test_initialize_does_not_replay_event_state_from_repository(
    recommendation_policy,
    application_feature_factory,
) -> None:
    now = datetime.fromisoformat("2026-07-16T11:20:00+08:00")
    event = new_event(
        "freeze",
        subject_key="market",
        trade_date="2026-07-16",
        phase="midday",
        strategy=None,
        priority=EventPriority.FREEZE,
        data_version="tick:112000",
        config_version="old-config",
        created_at=now,
        payload={"freeze_strategies": ["today"]},
    )
    repository = MemoryRepository()
    repository.events.append(event.audit_record(status=EventStatus.PENDING))
    pipeline = build_pipeline(
        StaticMarketData((application_feature_factory("600001", now),)),
        TradingDayCalendar(),
        None,
        repository,
        repository,
        SnapshotPublisher(history_size=4, client_queue_size=2),
        RecommendationEngine(recommendation_policy),
        RuntimeState(),
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=4,
        priority_queue_size=1,
        now=lambda: now,
    )

    pipeline.initialize()

    assert pipeline._queue.empty() is True
    assert repository.events == [event.audit_record(status=EventStatus.PENDING)]


def test_started_pipeline_routes_stages_to_bounded_workers_and_isolates_long(
    recommendation_policy,
    application_feature_factory,
) -> None:
    now = datetime.fromisoformat("2026-07-16T14:30:00+08:00")
    features = tuple(application_feature_factory(f"60000{index}", now) for index in range(1, 4))
    repository = MemoryRepository()
    reviewer = ThreadRecordingReviewer()
    engine = ThreadRecordingEngine(recommendation_policy)
    market_data = StaticMarketData(features)
    pipeline = build_pipeline(
        market_data,
        TradingDayCalendar(),
        reviewer,
        repository,
        InMemoryEventLedger(),
        SnapshotPublisher(history_size=8, client_queue_size=2),
        engine,
        RuntimeState(),
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=16,
        priority_queue_size=4,
        market_workers=2,
        normalization_workers=2,
        strategy_workers=3,
        deepseek_workers=4,
        now=lambda: now,
        long_codes=("600001",),
    )
    pipeline.initialize()
    assert pipeline.start() is True
    try:
        assert pipeline.submit_tick(now) is True
        _wait_until(lambda: pipeline.status()["counters"]["events_completed"] == 1)
        running_status = pipeline.status()
        running_thread_names = [thread.name for thread in threading.enumerate()]
    finally:
        pipeline.stop(timeout_seconds=2.0)

    assert market_data.fetch_threads and all(name.startswith("trader-data") for name in market_data.fetch_threads)
    assert engine.preselect_threads and all(name.startswith("trader-normalize") for name in engine.preselect_threads)
    assert {strategy for strategy, _name in engine.prepare_threads} == {
        Strategy.TOMORROW,
        Strategy.D25,
        Strategy.LONG,
    }
    assert all(
        name.startswith("trader-strategy") for strategy, name in engine.prepare_threads if strategy is not Strategy.LONG
    )
    assert all(name.startswith("trader-long") for strategy, name in engine.prepare_threads if strategy is Strategy.LONG)
    assert reviewer.review_threads and all(name.startswith("trader-deepseek") for name in reviewer.review_threads)
    assert engine.finalize_threads and all(name == "trader-merge" for name in engine.finalize_threads)
    assert repository.write_threads == []
    assert repository.events == []
    pools = running_status["dependencies"]["worker_pools"]
    assert pools["data"]["workers"] == 2
    assert pools["normalization"]["workers"] == 2
    assert pools["strategy"]["workers"] == 3
    assert pools["deepseek"]["workers"] == 4
    assert pools["long"]["workers"] == 1
    assert pools["merge"]["workers"] == 1
    assert pools["merge"]["queue_capacity"] == 16
    assert pools["merge"]["submitted_count"] == 1
    assert pools["merge"]["rejected_count"] == 0
    assert pools["merge"]["running"] is True
    assert pools["persistence"]["workers"] == 1
    assert "persistent_audit" not in running_status["dependencies"]
    for strategy in ("tomorrow", "d25", "long"):
        strategy_status = running_status["strategies"][strategy]
        assert strategy_status["candidate_count"] >= strategy_status["topk_count"]
        assert strategy_status["score_latency_ms"] is not None
        assert strategy_status["data_version"]
        assert strategy_status["strategy_version"]
        assert strategy_status["config_version"] == "config-v2"
        assert strategy_status["veto_count"] >= 0
    assert sum(name.startswith("trader-data") for name in running_thread_names) == 2
    assert sum(name.startswith("trader-normalize") for name in running_thread_names) == 2
    assert sum(name.startswith("trader-strategy") for name in running_thread_names) == 3
    assert sum(name.startswith("trader-deepseek") for name in running_thread_names) == 4
    assert sum(name.startswith("trader-long") for name in running_thread_names) == 1
    assert sum(name.startswith("trader-persistence") for name in running_thread_names) == 1
    assert running_thread_names.count("trader-merge") == 1
    assert not any(thread.name.startswith("trader-") for thread in threading.enumerate())


def test_afternoon_score_event_refreshes_tail_before_cached_tomorrow_scoring(
    recommendation_policy,
    application_feature_factory,
) -> None:
    now = datetime.fromisoformat("2026-07-16T14:30:00+08:00")
    market_data = StaticMarketData((application_feature_factory("600001", now),))
    repository = MemoryRepository()
    pipeline = build_pipeline(
        market_data,
        TradingDayCalendar(),
        None,
        repository,
        repository,
        SnapshotPublisher(history_size=4, client_queue_size=2),
        RecommendationEngine(recommendation_policy),
        RuntimeState(),
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=8,
        priority_queue_size=2,
        now=lambda: now,
    )
    pipeline._candidate_codes = ("600001",)
    event = new_event(
        "score",
        subject_key="market",
        trade_date="2026-07-16",
        phase="afternoon",
        strategy=None,
        priority=EventPriority.MARKET_QUOTES,
        data_version="tail-refresh-regression",
        config_version="config-v2",
        created_at=now,
        deadline=now + timedelta(seconds=15),
        payload={"schedule_task": "score"},
    )

    pipeline.initialize()
    assert pipeline.start() is True
    try:
        assert pipeline.submit_event(event) is True
        _wait_until(lambda: repository.events and repository.events[-1].status is EventStatus.SUCCESS)
    finally:
        pipeline.stop(timeout_seconds=2.0)

    assert market_data.tail_refreshes == [("600001",)]
    assert True in market_data.candidate_tail_requests


def test_expired_full_market_event_does_not_commit_candidates_or_set_recent_error(
    recommendation_policy,
    application_feature_factory,
) -> None:
    started_at = datetime.fromisoformat("2026-07-16T12:31:38+08:00")
    clock = MutableClock(started_at)
    feature = application_feature_factory("600001", started_at)
    repository = MemoryRepository()
    market_data = DeadlineAdvancingMarketData((feature,), clock)
    pipeline = build_pipeline(
        market_data,
        TradingDayCalendar(),
        None,
        repository,
        repository,
        SnapshotPublisher(history_size=4, client_queue_size=2),
        RecommendationEngine(recommendation_policy),
        RuntimeState(),
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=8,
        priority_queue_size=2,
        now=clock.now,
    )
    deadline = started_at + timedelta(seconds=20)
    event = new_event(
        "full_market",
        subject_key="market",
        trade_date="2026-07-16",
        phase="midday",
        strategy=None,
        priority=EventPriority.MARKET_QUOTES,
        data_version="deadline-regression",
        config_version="config-v2",
        created_at=started_at,
        deadline=deadline,
        payload={"schedule_task": "full_market"},
    )

    pipeline.initialize()
    assert pipeline.start() is True
    try:
        assert pipeline.submit_event(event) is True
        _wait_until(lambda: bool(repository.events) and repository.events[-1].status is EventStatus.EXPIRED)
    finally:
        pipeline.stop(timeout_seconds=2.0)

    status = pipeline.status()
    assert pipeline._candidate_codes == ()
    assert status["counters"]["events_expired"] == 1
    assert status["counters"]["events_failed"] == 0
    assert "deadline" not in status["last_error"]


def test_periodic_full_market_event_requires_a_fresh_physical_refresh(
    recommendation_policy,
    application_feature_factory,
) -> None:
    now = datetime.fromisoformat("2026-07-16T10:00:00+08:00")
    market_data = StaticMarketData((application_feature_factory("600001", now),))
    repository = MemoryRepository()
    pipeline = build_pipeline(
        market_data,
        TradingDayCalendar(),
        None,
        repository,
        repository,
        SnapshotPublisher(history_size=4, client_queue_size=2),
        RecommendationEngine(recommendation_policy),
        RuntimeState(),
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=8,
        priority_queue_size=2,
        now=lambda: now,
    )
    event = new_event(
        "full_market",
        subject_key="market",
        trade_date="2026-07-16",
        phase="today_main",
        strategy=None,
        priority=EventPriority.MARKET_QUOTES,
        data_version="fresh-refresh-regression",
        config_version="config-v2",
        created_at=now,
        deadline=now + timedelta(seconds=20),
        payload={"schedule_task": "full_market"},
    )

    pipeline.initialize()
    assert pipeline.start() is True
    try:
        assert pipeline.submit_event(event) is True
        _wait_until(lambda: bool(repository.events) and repository.events[-1].status is EventStatus.SUCCESS)
    finally:
        pipeline.stop(timeout_seconds=2.0)

    assert market_data.market_force_requests == [True]


def test_current_quote_recovery_populates_market_view_without_scoring(
    recommendation_policy,
    application_feature_factory,
) -> None:
    now = datetime.fromisoformat("2026-07-16T15:05:00+08:00")
    market_data = StaticMarketData((application_feature_factory("600001", now),))
    repository = MemoryRepository()
    pipeline = build_pipeline(
        market_data,
        TradingDayCalendar(),
        None,
        repository,
        repository,
        SnapshotPublisher(history_size=4, client_queue_size=2),
        RecommendationEngine(recommendation_policy),
        RuntimeState(),
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=8,
        priority_queue_size=2,
        now=lambda: now,
        cadence_policy=CadencePolicy.from_seconds(
            {
                "full_market": {"today_main": 5},
                "candidate_quotes": {"today_main": 1},
                "topk_quotes": {"today_main": 1},
                "score": {"today_main": 3},
                "industry_heat": {"today_main": 60},
                "market_news": {"today_main": 60},
                "stock_risk": {"today_main": 180},
            }
        ),
    )

    event = new_event(
        "current_quotes",
        subject_key="market",
        trade_date="2026-07-16",
        phase="after_close",
        strategy=None,
        priority=EventPriority.MARKET_QUOTES,
        data_version="current-quote-recovery-regression",
        config_version="config-v2",
        created_at=now,
        deadline=now + timedelta(seconds=20),
        payload={"schedule_task": "current_quotes"},
    )

    pipeline.initialize()
    assert pipeline.start() is True
    try:
        assert pipeline.submit_event(event) is True
        _wait_until(lambda: pipeline.status()["counters"].get("current_quote_recoveries") == 1)
    finally:
        pipeline.stop(timeout_seconds=2.0)

    assert market_data.market_force_requests == [True]
    assert [feature.quote.code for feature in pipeline._market_features] == ["600001"]
    assert pipeline._candidate_codes == ()
    assert repository.published == {}
    assert pipeline.status()["counters"]["current_quote_recoveries"] == 1


def test_periodic_event_evaluates_new_quotes_at_execution_time(
    recommendation_policy,
    application_feature_factory,
) -> None:
    scheduled_at = datetime.fromisoformat("2026-07-16T10:00:00+08:00")
    execution_time = scheduled_at + timedelta(seconds=2)
    market_data = StaticMarketData((application_feature_factory("600001", scheduled_at),))
    repository = MemoryRepository()
    pipeline = build_pipeline(
        market_data,
        TradingDayCalendar(),
        None,
        repository,
        repository,
        SnapshotPublisher(history_size=4, client_queue_size=2),
        RecommendationEngine(recommendation_policy),
        RuntimeState(),
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=8,
        priority_queue_size=2,
        now=lambda: execution_time,
    )
    event = new_event(
        "full_market",
        subject_key="market",
        trade_date="2026-07-16",
        phase="today_main",
        strategy=None,
        priority=EventPriority.MARKET_QUOTES,
        data_version="execution-clock-regression",
        config_version="config-v2",
        created_at=scheduled_at,
        deadline=scheduled_at + timedelta(seconds=20),
        payload={"schedule_task": "full_market"},
    )

    pipeline.initialize()
    assert pipeline.start() is True
    try:
        assert pipeline.submit_event(event) is True
        _wait_until(lambda: bool(repository.events) and repository.events[-1].status is EventStatus.SUCCESS)
    finally:
        pipeline.stop(timeout_seconds=2.0)

    assert pipeline._market_features[0].quote.source_time == execution_time


def test_synchronous_and_worker_paths_publish_identical_business_snapshots(
    recommendation_policy,
    application_feature_factory,
) -> None:
    now = datetime.fromisoformat("2026-07-16T14:30:00+08:00")
    features = tuple(application_feature_factory(f"60000{index}", now) for index in range(1, 3))
    sync_repository = MemoryRepository()
    async_repository = MemoryRepository()

    def build(repository: MemoryRepository) -> RecommendationPipeline:
        return build_pipeline(
            StaticMarketData(features),
            TradingDayCalendar(),
            None,
            repository,
            repository,
            SnapshotPublisher(history_size=4, client_queue_size=2),
            RecommendationEngine(recommendation_policy),
            RuntimeState(),
            config_version="config-v2",
            candidate_pool_size=120,
            event_queue_size=8,
            priority_queue_size=2,
            now=lambda: now,
            long_codes=("600001",),
        )

    synchronous = build(sync_repository)
    synchronous.initialize()
    sync_snapshots = synchronous.run_once(now)
    asynchronous = build(async_repository)
    asynchronous.initialize()
    asynchronous.start()
    try:
        assert asynchronous.submit_tick(now) is True
        _wait_until(lambda: asynchronous.status()["counters"]["events_completed"] == 1)
    finally:
        asynchronous.stop(timeout_seconds=2.0)

    assert tuple(sync_repository.published.values()) == tuple(async_repository.published.values())
    assert tuple(snapshot.strategy for snapshot in sync_snapshots) == (
        Strategy.TOMORROW,
        Strategy.D25,
        Strategy.LONG,
    )


def test_deepseek_worker_failure_falls_back_to_local_snapshot(
    recommendation_policy,
    application_feature_factory,
) -> None:
    now = datetime.fromisoformat("2026-07-16T10:00:00+08:00")
    repository = MemoryRepository()
    state = RuntimeState()
    pipeline = build_pipeline(
        StaticMarketData((application_feature_factory("600001", now),)),
        TradingDayCalendar(),
        FailingReviewer(),
        repository,
        repository,
        SnapshotPublisher(history_size=4, client_queue_size=2),
        RecommendationEngine(recommendation_policy),
        state,
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=8,
        priority_queue_size=2,
        now=lambda: now,
    )
    pipeline.initialize()
    pipeline.start()
    try:
        assert pipeline.submit_tick(now) is True
        _wait_until(lambda: pipeline.status()["counters"]["events_completed"] == 1)
        snapshot = state.latest(Strategy.TODAY)
    finally:
        pipeline.stop(timeout_seconds=2.0)

    assert snapshot is not None
    assert snapshot.fusion_mode.value == "local_degraded"
    assert "DeepSeek review degraded" in pipeline.status()["last_error"]


def test_long_review_starts_after_shared_strategy_reviews_complete(
    recommendation_policy,
    application_feature_factory,
) -> None:
    now = datetime.fromisoformat("2026-07-16T14:30:00+08:00")
    features = (application_feature_factory("600001", now),)
    repository = MemoryRepository()
    reviewer = SequencedReviewer()
    pipeline = build_pipeline(
        StaticMarketData(features),
        TradingDayCalendar(),
        reviewer,
        repository,
        repository,
        SnapshotPublisher(history_size=4, client_queue_size=2),
        RecommendationEngine(recommendation_policy),
        RuntimeState(),
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=8,
        priority_queue_size=2,
        now=lambda: now,
        long_codes=("600001",),
    )
    pipeline.initialize()
    pipeline.start()
    try:
        assert pipeline.submit_tick(now) is True
        _wait_until(lambda: pipeline.status()["counters"]["events_completed"] == 1)
    finally:
        pipeline.stop(timeout_seconds=2.0)

    assert reviewer.out_of_order is False
    assert reviewer.completed_strategies == {Strategy.TOMORROW, Strategy.D25, Strategy.LONG}


def test_one_strategy_data_failure_does_not_block_other_snapshots(
    recommendation_policy,
    application_feature_factory,
) -> None:
    now = datetime.fromisoformat("2026-07-16T14:30:00+08:00")
    features = (application_feature_factory("600001", now),)
    repository = MemoryRepository()
    state = RuntimeState()
    pipeline = build_pipeline(
        TomorrowFailingMarketData(features),
        TradingDayCalendar(),
        None,
        repository,
        repository,
        SnapshotPublisher(history_size=4, client_queue_size=2),
        RecommendationEngine(recommendation_policy),
        state,
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=8,
        priority_queue_size=2,
        now=lambda: now,
        long_codes=("600001",),
    )
    pipeline.initialize()
    pipeline.start()
    try:
        assert pipeline.submit_tick(now) is True
        _wait_until(lambda: pipeline.status()["counters"]["events_completed"] == 1)
    finally:
        pipeline.stop(timeout_seconds=2.0)

    assert state.latest(Strategy.TOMORROW) is None
    assert state.latest(Strategy.D25) is not None
    assert state.latest(Strategy.LONG) is not None
    assert "tomorrow data degraded" in pipeline.status()["last_error"]


def test_stop_waits_for_freeze_write_then_rejects_new_events(
    recommendation_policy,
    application_feature_factory,
) -> None:
    draft_at = datetime.fromisoformat("2026-07-16T11:19:50+08:00")
    freeze_at = datetime.fromisoformat("2026-07-16T11:20:00+08:00")
    repository = BlockingFreezeRepository()
    pipeline = build_pipeline(
        StaticMarketData((application_feature_factory("600001", draft_at),)),
        TradingDayCalendar(),
        None,
        repository,
        repository,
        SnapshotPublisher(history_size=4, client_queue_size=2),
        RecommendationEngine(recommendation_policy),
        RuntimeState(),
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=8,
        priority_queue_size=2,
        market_workers=1,
        normalization_workers=1,
        strategy_workers=3,
        deepseek_workers=1,
        now=lambda: freeze_at,
    )
    pipeline.initialize()
    assert pipeline.run_once(draft_at)[0].strategy is Strategy.TODAY
    assert pipeline.start() is True
    stopper: threading.Thread | None = None
    try:
        assert pipeline.submit_tick(freeze_at) is True
        assert repository.freeze_started.wait(timeout=2.0)

        stopper = threading.Thread(target=pipeline.stop, kwargs={"timeout_seconds": 2.0})
        stopper.start()
        time.sleep(0.05)
        assert stopper.is_alive()
        repository.allow_freeze.set()
        stopper.join(timeout=2.0)
    finally:
        repository.allow_freeze.set()
        if stopper is not None:
            stopper.join(timeout=2.0)
        pipeline.stop(timeout_seconds=2.0)

    assert stopper is not None and not stopper.is_alive()
    assert repository.frozen[(Strategy.TODAY, "2026-07-16")].frozen is True
    assert pipeline.submit_tick(freeze_at) is False
    assert pipeline.status()["dependencies"]["event_queue"]["closed"] is True
    assert not any(thread.name.startswith("trader-") for thread in threading.enumerate())


def test_partial_pipeline_start_failure_rolls_back_started_pools(
    recommendation_policy,
    application_feature_factory,
    monkeypatch,
) -> None:
    now = datetime.fromisoformat("2026-07-16T10:00:00+08:00")
    repository = MemoryRepository()
    pipeline = build_pipeline(
        StaticMarketData((application_feature_factory("600001", now),)),
        TradingDayCalendar(),
        None,
        repository,
        repository,
        SnapshotPublisher(history_size=4, client_queue_size=2),
        RecommendationEngine(recommendation_policy),
        RuntimeState(),
        config_version="config-v2",
        candidate_pool_size=120,
        event_queue_size=8,
        priority_queue_size=2,
        now=lambda: now,
    )
    original_start = threading.Thread.start

    def fail_normalization_start(thread: threading.Thread) -> None:
        if thread.name.startswith("trader-normalize"):
            raise RuntimeError("normalization start failed")
        original_start(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_normalization_start)

    with pytest.raises(RuntimeError, match="normalization start failed"):
        pipeline.start()

    assert pipeline.status()["runtime_started"] is False
    assert not any(thread.name.startswith("trader-") for thread in threading.enumerate())


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self._value = value

    def now(self) -> datetime:
        return self._value

    def set(self, value: datetime) -> None:
        self._value = value


class TradingDayCalendar:
    @staticmethod
    def is_trading_day(_day) -> bool:
        return True


class ForbiddenStatusCalendar:
    @staticmethod
    def is_trading_day(_day) -> bool:
        raise AssertionError("status must not refresh the trading calendar")


class StaticMarketData:
    def __init__(self, features: Sequence[FeatureSnapshot]) -> None:
        self._features = tuple(features)
        self.candidate_tail_requests: list[bool] = []
        self.tail_refreshes: list[tuple[str, ...]] = []
        self.fetch_threads: list[str] = []
        self.market_force_requests: list[bool] = []

    def fetch_market_features(
        self,
        observed_at: datetime,
        *,
        force: bool = False,
        deadline: datetime | None = None,
    ) -> Sequence[FeatureSnapshot]:
        del deadline
        self.fetch_threads.append(threading.current_thread().name)
        self.market_force_requests.append(force)
        return tuple(_at_time(feature, observed_at) for feature in self._features)

    def fetch_candidate_features(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        include_intraday_tail: bool = False,
        include_structured_research: bool = False,
    ) -> Sequence[FeatureSnapshot]:
        del include_structured_research
        self.fetch_threads.append(threading.current_thread().name)
        self.candidate_tail_requests.append(include_intraday_tail)
        requested = set(codes)
        return tuple(_at_time(feature, observed_at) for feature in self._features if feature.quote.code in requested)

    def refresh_candidate_quotes(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        force: bool = False,
        deadline: datetime | None = None,
    ) -> Sequence[FeatureSnapshot]:
        del force, deadline
        self.fetch_threads.append(threading.current_thread().name)
        requested = set(codes)
        return tuple(_at_time(feature, observed_at) for feature in self._features if feature.quote.code in requested)

    def refresh_industry_heat(self, observed_at: datetime) -> Sequence[FeatureSnapshot]:
        return tuple(_at_time(feature, observed_at) for feature in self._features)

    @staticmethod
    def refresh_market_news(
        codes: Sequence[str],
        observed_at: datetime,
        *,
        force: bool = False,
        deadline: datetime | None = None,
    ) -> None:
        del codes, observed_at, deadline

    @staticmethod
    def refresh_stock_risk(
        codes: Sequence[str],
        observed_at: datetime,
        *,
        deadline: datetime | None = None,
    ) -> None:
        del codes, observed_at, deadline

    @staticmethod
    def refresh_reference_data(
        codes: Sequence[str],
        observed_at: datetime,
        *,
        force: bool = False,
    ) -> None:
        del codes, observed_at, force

    def refresh_intraday_tail(self, codes: Sequence[str], observed_at: datetime) -> None:
        del observed_at
        self.tail_refreshes.append(tuple(codes))

    def read_candidate_features(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        include_intraday_tail: bool = False,
        include_structured_research: bool = False,
    ) -> Sequence[FeatureSnapshot]:
        return self.fetch_candidate_features(
            codes,
            observed_at,
            include_intraday_tail=include_intraday_tail,
            include_structured_research=include_structured_research,
        )

    @staticmethod
    def health() -> Mapping[str, object]:
        return {"status": "ok"}


class ClosingPriceMarketData(StaticMarketData):
    @staticmethod
    def _closing(features: Sequence[FeatureSnapshot], observed_at: datetime) -> tuple[FeatureSnapshot, ...]:
        if observed_at.hour < 15:
            return tuple(features)
        return tuple(
            replace(
                feature,
                quote=replace(
                    feature.quote,
                    price=20.0,
                    high=max(20.0, feature.quote.high or 0.0),
                    pct_change=5.0,
                    source_time=observed_at.replace(hour=15, minute=0, second=0, microsecond=0),
                    received_time=observed_at,
                    data_version=f"close:{observed_at.date().isoformat()}:{feature.quote.code}",
                ),
                observed_at=observed_at,
            )
            for feature in features
        )

    def fetch_market_features(
        self,
        observed_at: datetime,
        *,
        force: bool = False,
        deadline: datetime | None = None,
    ) -> Sequence[FeatureSnapshot]:
        return self._closing(super().fetch_market_features(observed_at, force=force, deadline=deadline), observed_at)

    def fetch_candidate_features(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        include_intraday_tail: bool = False,
        include_structured_research: bool = False,
    ) -> Sequence[FeatureSnapshot]:
        return self._closing(
            super().fetch_candidate_features(
                codes,
                observed_at,
                include_intraday_tail=include_intraday_tail,
                include_structured_research=include_structured_research,
            ),
            observed_at,
        )

    def refresh_candidate_quotes(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        force: bool = False,
        deadline: datetime | None = None,
    ) -> Sequence[FeatureSnapshot]:
        return self._closing(
            super().refresh_candidate_quotes(codes, observed_at, force=force, deadline=deadline),
            observed_at,
        )


class IncompleteClosingMarketData(ClosingPriceMarketData):
    def __init__(self, features: Sequence[FeatureSnapshot]) -> None:
        super().__init__(features)
        self.complete = False

    def fetch_market_features(
        self,
        observed_at: datetime,
        *,
        force: bool = False,
        deadline: datetime | None = None,
    ) -> Sequence[FeatureSnapshot]:
        features = tuple(super().fetch_market_features(observed_at, force=force, deadline=deadline))
        if self.complete:
            return features
        return tuple(feature for feature in features if not feature.quote.code.startswith("688"))


class WarmingClosingMarketData(ClosingPriceMarketData):
    def __init__(self, features: Sequence[FeatureSnapshot]) -> None:
        super().__init__(features)
        self.warmed = False

    def fetch_market_features(
        self,
        observed_at: datetime,
        *,
        force: bool = False,
        deadline: datetime | None = None,
    ) -> Sequence[FeatureSnapshot]:
        features = tuple(super().fetch_market_features(observed_at, force=force, deadline=deadline))
        if self.warmed:
            return features
        return tuple(
            replace(
                feature,
                values={name: value for name, value in feature.values.items() if name != "amount_median_20d"},
                history_days=0,
            )
            for feature in features
        )


class DelayedClosingMarketData(ClosingPriceMarketData):
    def __init__(self, features: Sequence[FeatureSnapshot], clock: MutableClock) -> None:
        super().__init__(features)
        self._clock = clock

    def fetch_market_features(
        self,
        observed_at: datetime,
        *,
        force: bool = False,
        deadline: datetime | None = None,
    ) -> Sequence[FeatureSnapshot]:
        features = tuple(super().fetch_market_features(observed_at, force=force, deadline=deadline))
        completed_at = observed_at + timedelta(seconds=5)
        self._clock.set(completed_at)
        return tuple(
            replace(
                feature,
                quote=replace(
                    feature.quote,
                    source_time=completed_at,
                    received_time=completed_at,
                ),
                observed_at=completed_at,
            )
            for feature in features
        )


class UnreliableClosingMarketData(ClosingPriceMarketData):
    def __init__(self, features: Sequence[FeatureSnapshot]) -> None:
        super().__init__(features)
        self.reliable = False

    def _with_reliability(self, features: Sequence[FeatureSnapshot]) -> tuple[FeatureSnapshot, ...]:
        if self.reliable:
            return tuple(features)
        blocked = {
            "entry_quality",
            "growth_score",
            "industry_trend",
            "quality_score",
            "value_score",
        }
        return tuple(
            replace(
                feature,
                values={name: value for name, value in feature.values.items() if name not in blocked},
            )
            for feature in features
        )

    def fetch_market_features(
        self,
        observed_at: datetime,
        *,
        force: bool = False,
        deadline: datetime | None = None,
    ) -> Sequence[FeatureSnapshot]:
        return self._with_reliability(super().fetch_market_features(observed_at, force=force, deadline=deadline))

    def fetch_candidate_features(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        include_intraday_tail: bool = False,
        include_structured_research: bool = False,
    ) -> Sequence[FeatureSnapshot]:
        return self._with_reliability(
            super().fetch_candidate_features(
                codes,
                observed_at,
                include_intraday_tail=include_intraday_tail,
                include_structured_research=include_structured_research,
            )
        )


class CachedOnlyClosingMarketData(ClosingPriceMarketData):
    def __init__(self, features: Sequence[FeatureSnapshot]) -> None:
        super().__init__(features)
        self.cached_candidate_reads = 0

    def fetch_candidate_features(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        include_intraday_tail: bool = False,
        include_structured_research: bool = False,
    ) -> Sequence[FeatureSnapshot]:
        del codes, observed_at, include_intraday_tail, include_structured_research
        raise AssertionError("close fallback must not fetch candidate research")

    def read_candidate_features(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        include_intraday_tail: bool = False,
        include_structured_research: bool = False,
    ) -> Sequence[FeatureSnapshot]:
        self.cached_candidate_reads += 1
        return ClosingPriceMarketData.fetch_candidate_features(
            self,
            codes,
            observed_at,
            include_intraday_tail=include_intraday_tail,
            include_structured_research=include_structured_research,
        )


class CachedCloseMarketOnlyMarketData(CachedOnlyClosingMarketData):
    def __init__(self, features: Sequence[FeatureSnapshot]) -> None:
        super().__init__(features)
        self.market_fetch_attempts = 0

    def fetch_market_features(
        self,
        observed_at: datetime,
        *,
        force: bool = False,
        deadline: datetime | None = None,
    ) -> Sequence[FeatureSnapshot]:
        del observed_at, force, deadline
        self.market_fetch_attempts += 1
        raise AssertionError("after-close retry must reuse complete cached close market features")


class MissingD25ResearchClosingMarketData(ClosingPriceMarketData):
    def fetch_market_features(
        self,
        observed_at: datetime,
        *,
        force: bool = False,
        deadline: datetime | None = None,
    ) -> Sequence[FeatureSnapshot]:
        return self._without_d25_research(super().fetch_market_features(observed_at, force=force, deadline=deadline))

    def fetch_candidate_features(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        include_intraday_tail: bool = False,
        include_structured_research: bool = False,
    ) -> Sequence[FeatureSnapshot]:
        return self._without_d25_research(
            super().fetch_candidate_features(
                codes,
                observed_at,
                include_intraday_tail=include_intraday_tail,
                include_structured_research=include_structured_research,
            )
        )

    def read_candidate_features(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        include_intraday_tail: bool = False,
        include_structured_research: bool = False,
    ) -> Sequence[FeatureSnapshot]:
        return self.fetch_candidate_features(
            codes,
            observed_at,
            include_intraday_tail=include_intraday_tail,
            include_structured_research=include_structured_research,
        )

    @staticmethod
    def _without_d25_research(features: Sequence[FeatureSnapshot]) -> tuple[FeatureSnapshot, ...]:
        blocked = {"growth_score", "quality_score", "value_score"}
        return tuple(
            replace(
                feature,
                values={name: value for name, value in feature.values.items() if name not in blocked},
            )
            for feature in features
        )


class DegradingMarketData(StaticMarketData):
    def __init__(self, features: Sequence[FeatureSnapshot]) -> None:
        super().__init__(features)
        self.market_unavailable = False

    def fetch_market_features(
        self,
        observed_at: datetime,
        *,
        force: bool = False,
        deadline: datetime | None = None,
    ) -> Sequence[FeatureSnapshot]:
        if self.market_unavailable:
            raise MarketDataUnavailableError("all full-market sources failed")
        return super().fetch_market_features(observed_at, force=force, deadline=deadline)


class DeadlineAdvancingMarketData(StaticMarketData):
    def __init__(self, features: Sequence[FeatureSnapshot], clock: MutableClock) -> None:
        super().__init__(features)
        self._clock = clock

    def fetch_market_features(
        self,
        observed_at: datetime,
        *,
        force: bool = False,
        deadline: datetime | None = None,
    ) -> Sequence[FeatureSnapshot]:
        assert deadline is not None
        result = super().fetch_market_features(observed_at, force=force, deadline=deadline)
        self._clock.set(deadline)
        return result


class DegradingCandidateMarketData(StaticMarketData):
    def __init__(self, features: Sequence[FeatureSnapshot]) -> None:
        super().__init__(features)
        self.candidate_unavailable = False

    def fetch_candidate_features(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        include_intraday_tail: bool = False,
        include_structured_research: bool = False,
    ) -> Sequence[FeatureSnapshot]:
        if self.candidate_unavailable:
            raise MarketDataUnavailableError("candidate quote source failed")
        return super().fetch_candidate_features(
            codes,
            observed_at,
            include_intraday_tail=include_intraday_tail,
            include_structured_research=include_structured_research,
        )

    def refresh_candidate_quotes(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        force: bool = False,
        deadline: datetime | None = None,
    ) -> Sequence[FeatureSnapshot]:
        if self.candidate_unavailable:
            raise MarketDataUnavailableError("candidate quote source failed")
        return super().refresh_candidate_quotes(codes, observed_at, force=force, deadline=deadline)


class TomorrowFailingMarketData(StaticMarketData):
    def fetch_candidate_features(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        include_intraday_tail: bool = False,
        include_structured_research: bool = False,
    ) -> Sequence[FeatureSnapshot]:
        if include_intraday_tail:
            raise MarketDataUnavailableError("tomorrow candidate source failed")
        return super().fetch_candidate_features(
            codes,
            observed_at,
            include_intraday_tail=include_intraday_tail,
            include_structured_research=include_structured_research,
        )


class MemoryRepository:
    def __init__(self) -> None:
        self.published: dict[Strategy, RecommendationSnapshot] = {}
        self.frozen: dict[tuple[Strategy, str], object] = {}
        self.overlays: dict[tuple[Strategy, str], LiveOverlay] = {}
        self.checkpoints: dict[tuple[Strategy, str, datetime], RecommendationSnapshot] = {}
        self.events: list[EventAuditRecord] = []
        self.write_threads: list[str] = []
        self._event_lock = threading.Lock()

    @staticmethod
    def initialize() -> None:
        return None

    @staticmethod
    def recover() -> RecoverySummary:
        return RecoverySummary()

    def publish(self, snapshot: RecommendationSnapshot) -> None:
        self.write_threads.append(threading.current_thread().name)
        self.published[snapshot.strategy] = snapshot

    def freeze(self, snapshot: RecommendationSnapshot) -> None:
        self.write_threads.append(threading.current_thread().name)
        key = (snapshot.strategy, snapshot.trade_date)
        if key in self.frozen:
            raise AssertionError("frozen snapshot was modified")
        self.frozen[key] = snapshot
        self.published[snapshot.strategy] = snapshot

    def save_checkpoint(self, snapshot: RecommendationSnapshot, *, boundary_at: datetime) -> None:
        self.checkpoints[(snapshot.strategy, snapshot.trade_date, boundary_at)] = snapshot

    def load_checkpoint(
        self, strategy: Strategy, trade_date: str, *, boundary_at: datetime
    ) -> RecommendationSnapshot | None:
        return self.checkpoints.get((strategy, trade_date, boundary_at))

    def consume_checkpoint(self, strategy: Strategy, trade_date: str, *, boundary_at: datetime) -> None:
        self.checkpoints.pop((strategy, trade_date, boundary_at), None)

    def latest(self, strategy: Strategy) -> RecommendationSnapshot | None:
        return self.published.get(strategy)

    def load_frozen(self, strategy: Strategy, trade_date: str) -> RecommendationSnapshot | None:
        snapshot = self.frozen.get((strategy, trade_date))
        return snapshot if isinstance(snapshot, RecommendationSnapshot) else None

    def save_live_overlay(self, overlay: LiveOverlay) -> bool:
        self.write_threads.append(threading.current_thread().name)
        existing = self.overlays.get((overlay.strategy, overlay.trade_date))
        if existing is not None and (existing.closing or existing.observed_at >= overlay.observed_at):
            return False
        self.overlays[(overlay.strategy, overlay.trade_date)] = overlay
        return True

    def load_live_overlay(self, strategy: Strategy, trade_date: str) -> LiveOverlay | None:
        return self.overlays.get((strategy, trade_date))

    def recommendation_dates(self, strategy: Strategy) -> Sequence[str]:
        return tuple(day for candidate, day in self.frozen if candidate is strategy)

    def reserve_event(self, event: EventAuditRecord) -> bool:
        self.write_threads.append(threading.current_thread().name)
        identity = (
            event.trade_date,
            event.phase,
            event.strategy,
            event.event_type,
            event.subject_key,
            event.data_version,
        )
        with self._event_lock:
            for stored in self.events:
                stored_identity = (
                    stored.trade_date,
                    stored.phase,
                    stored.strategy,
                    stored.event_type,
                    stored.subject_key,
                    stored.data_version,
                )
                if stored_identity == identity:
                    return False
            self.events.append(event)
            return True

    def compare_and_set_event(
        self,
        event_id: str,
        *,
        expected_status: EventStatus,
        status: EventStatus,
        retry_count: int,
        error: str = "",
    ) -> bool:
        self.write_threads.append(threading.current_thread().name)
        with self._event_lock:
            for index, event in enumerate(self.events):
                if event.event_id != event_id or event.status is not expected_status:
                    continue
                payload = event.payload if isinstance(event.payload, Mapping) else {}
                self.events[index] = replace(
                    event, status=status, retry_count=retry_count, error=error, payload=payload
                )
                return True
        return False


def _at_time(feature: FeatureSnapshot, observed_at: datetime) -> FeatureSnapshot:
    quote = replace(
        feature.quote,
        source_time=observed_at,
        received_time=observed_at,
        data_version=f"static:{observed_at.isoformat()}",
    )
    return replace(feature, quote=quote, observed_at=observed_at)


def _three_board_features(application_feature_factory, observed_at: datetime) -> tuple[FeatureSnapshot, ...]:
    return tuple(
        application_feature_factory(code, observed_at, industry=f"行业-{index % 3}")
        for index, code in enumerate(
            (
                *(f"600{suffix:03d}" for suffix in range(1, 101)),
                *(f"300{suffix:03d}" for suffix in range(1, 101)),
                *(f"688{suffix:03d}" for suffix in range(1, 101)),
            )
        )
    )


class RecordingQueue:
    def __init__(self) -> None:
        self.events: list[PipelineEvent] = []

    def put(self, event: PipelineEvent) -> bool:
        self.events.append(event)
        return True

    def put_with_superseded(self, event: PipelineEvent) -> tuple[bool, tuple[str, ...]]:
        return self.put(event), ()


class ThreadRecordingReviewer:
    def __init__(self) -> None:
        self.review_threads: list[str] = []

    def review(
        self,
        _strategy: Strategy,
        _candidates: Sequence[FeatureSnapshot],
        *,
        phase: str,
        deadline: datetime,
        contexts=None,
    ) -> Mapping[str, object]:
        del phase, deadline, contexts
        self.review_threads.append(threading.current_thread().name)
        return {}

    def preheat(
        self,
        candidates: Sequence[FeatureSnapshot],
        *,
        phase: str,
        deadline: datetime,
    ) -> Mapping[str, object]:
        return self.review(Strategy.TODAY, candidates, phase=phase, deadline=deadline)

    @staticmethod
    def status() -> Mapping[str, object]:
        return {"enabled": True}


class FailingReviewer(ThreadRecordingReviewer):
    def review(
        self,
        _strategy: Strategy,
        _candidates: Sequence[FeatureSnapshot],
        *,
        phase: str,
        deadline: datetime,
        contexts=None,
    ) -> Mapping[str, object]:
        del phase, deadline, contexts
        raise RuntimeError("review transport failed")


class SupersededOutcomeSettlement:
    @staticmethod
    def settle(_now: datetime, _market_features: Sequence[FeatureSnapshot]) -> None:
        raise SourceRequestSupersededError("history source request was superseded")


class SequencedReviewer(ThreadRecordingReviewer):
    def __init__(self) -> None:
        super().__init__()
        self.d25_done = threading.Event()
        self.long_started = threading.Event()
        self.out_of_order = False
        self.completed_strategies: set[Strategy] = set()
        self._lock = threading.Lock()

    def review(
        self,
        strategy: Strategy,
        _candidates: Sequence[FeatureSnapshot],
        *,
        phase: str,
        deadline: datetime,
        contexts=None,
    ) -> Mapping[str, object]:
        del phase, deadline, contexts
        if strategy is Strategy.D25:
            self.long_started.wait(timeout=0.1)
            self.d25_done.set()
        elif strategy is Strategy.LONG:
            self.out_of_order = not self.d25_done.is_set()
            self.long_started.set()
        with self._lock:
            self.completed_strategies.add(strategy)
        return {}


class ThreadRecordingEngine(RecommendationEngine):
    def __init__(self, policy) -> None:
        super().__init__(policy)
        self.preselect_threads: list[str] = []
        self.prepare_threads: list[tuple[Strategy, str]] = []
        self.finalize_threads: list[str] = []

    def preselect(self, *args, **kwargs):
        self.preselect_threads.append(threading.current_thread().name)
        return super().preselect(*args, **kwargs)

    def prepare_snapshot(self, strategy, *args, **kwargs):
        self.prepare_threads.append((strategy, threading.current_thread().name))
        return super().prepare_snapshot(strategy, *args, **kwargs)

    def finalize_snapshot(self, *args, **kwargs):
        self.finalize_threads.append(threading.current_thread().name)
        return super().finalize_snapshot(*args, **kwargs)


class BlockingFreezeRepository(MemoryRepository):
    def __init__(self) -> None:
        super().__init__()
        self.freeze_started = threading.Event()
        self.allow_freeze = threading.Event()

    def freeze(self, snapshot: RecommendationSnapshot) -> None:
        self.freeze_started.set()
        assert self.allow_freeze.wait(timeout=2.0)
        super().freeze(snapshot)


def _wait_until(predicate, *, timeout_seconds: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met before timeout")
