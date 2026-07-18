from __future__ import annotations

import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from trader.application.events import EventPriority, PipelineEvent, new_event
from trader.application.pipeline import RecommendationPipeline
from trader.application.ports import MarketDataUnavailable
from trader.application.publisher import SnapshotPublisher
from trader.application.recommendations import RecommendationEngine
from trader.application.status import RuntimeState
from trader.domain.models import FeatureSnapshot, LiveOverlay, RecommendationSnapshot, Strategy


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
    pipeline = RecommendationPipeline(
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
        long_codes=("600001", "600002"),
    )
    pipeline.initialize()

    today = pipeline.run_once(clock.now())
    assert [snapshot.strategy for snapshot in today] == [Strategy.TODAY]
    assert today[0].fusion_mode.value == "local_degraded"

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
    pipeline = RecommendationPipeline(
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
    pipeline = RecommendationPipeline(
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
    pipeline = RecommendationPipeline(
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
    overlay = repository.overlays[(Strategy.TOMORROW, "2026-07-16")]
    assert isinstance(overlay, LiveOverlay)
    assert overlay.snapshot_id == frozen.snapshot_id
    assert overlay.closing is False
    market_data.candidate_unavailable = True
    clock.set(datetime.fromisoformat("2026-07-16T14:50:20+08:00"))
    pipeline.run_once(clock.now())
    assert repository.overlays[(Strategy.TOMORROW, "2026-07-16")].version == overlay.version
    market_data.candidate_unavailable = False
    clock.set(datetime.fromisoformat("2026-07-16T15:00:00+08:00"))
    pipeline.run_once(clock.now())
    closing = repository.overlays[(Strategy.TOMORROW, "2026-07-16")]
    assert closing.closing is True
    clock.set(datetime.fromisoformat("2026-07-16T15:01:00+08:00"))
    pipeline.run_once(clock.now())
    assert repository.overlays[(Strategy.TOMORROW, "2026-07-16")].version == closing.version


def test_initialize_restores_frozen_gate(recommendation_policy, application_feature_factory) -> None:
    now = datetime.fromisoformat("2026-07-16T10:00:00+08:00")
    features = (application_feature_factory("600001", now),)
    repository = MemoryRepository()
    repository.frozen[(Strategy.TODAY, "2026-07-16")] = object()
    pipeline = RecommendationPipeline(
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
    first = RecommendationPipeline(
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
    restarted = RecommendationPipeline(
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


def test_market_data_unavailability_preserves_candidates_and_records_degradation(
    recommendation_policy,
    application_feature_factory,
    caplog,
) -> None:
    clock = MutableClock(datetime.fromisoformat("2026-07-16T10:00:00+08:00"))
    market_data = DegradingMarketData((application_feature_factory("600001", clock.now()),))
    repository = MemoryRepository()
    pipeline = RecommendationPipeline(
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


def test_status_uses_recorded_phase_without_calling_calendar(recommendation_policy) -> None:
    now = datetime.fromisoformat("2026-07-16T10:00:00+08:00")
    state = RuntimeState()
    state.record_tick("today_main", now)
    repository = MemoryRepository()
    pipeline = RecommendationPipeline(
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


def test_freeze_tick_uses_reserved_priority_and_is_persisted_before_enqueue(
    recommendation_policy,
    application_feature_factory,
) -> None:
    now = datetime.fromisoformat("2026-07-16T11:20:00+08:00")
    repository = MemoryRepository()
    pipeline = RecommendationPipeline(
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
    assert repository.events[0]["status"] == "pending"


def test_risk_event_is_persisted_before_reserved_enqueue(
    recommendation_policy,
    application_feature_factory,
) -> None:
    now = datetime.fromisoformat("2026-07-16T10:00:00+08:00")
    repository = MemoryRepository()
    pipeline = RecommendationPipeline(
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
    assert repository.events[0]["event_id"] == event.event_id
    assert repository.events[0]["status"] == "pending"


def test_initialize_replays_persisted_priority_event(recommendation_policy, application_feature_factory) -> None:
    now = datetime.fromisoformat("2026-07-16T11:20:00+08:00")
    event = new_event(
        "freeze",
        subject_key="market",
        trade_date="2026-07-16",
        phase="midday",
        strategy=None,
        priority=EventPriority.FREEZE,
        data_version="tick:112000",
        config_version="config-v2",
        created_at=now,
        payload={"freeze_strategies": ["today"]},
    )
    repository = MemoryRepository()
    repository.events.append(event.audit_record(status="pending"))
    pipeline = RecommendationPipeline(
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

    replayed = pipeline._queue.get()
    assert replayed is not None
    assert replayed.event_id == event.event_id
    assert replayed.retry_count == 1
    assert pipeline.status()["counters"]["events_replayed"] == 1


def test_initialize_rejects_priority_event_from_another_config(
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
    repository.events.append(event.audit_record(status="pending"))
    pipeline = RecommendationPipeline(
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
    assert repository.events[0]["status"] == "failed"
    assert repository.events[0]["error"] == "config_version_mismatch"
    assert "config version" in pipeline.status()["last_error"]


def test_initialize_closes_malformed_priority_event(
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
        config_version="config-v2",
        created_at=now,
        payload={"freeze_strategies": ["today"]},
    )
    repository = MemoryRepository()
    repository.events.append({**event.audit_record(status="pending"), "payload": []})
    pipeline = RecommendationPipeline(
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
    assert repository.events[0]["status"] == "failed"
    assert repository.events[0]["error"] == "invalid_persisted_event"


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
    pipeline = RecommendationPipeline(
        market_data,
        TradingDayCalendar(),
        reviewer,
        repository,
        repository,
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
    assert repository.write_threads and all(name.startswith("trader-persistence") for name in repository.write_threads)
    assert {event["status"] for event in repository.events} == {"success"}
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
    assert running_status["dependencies"]["persistent_audit"] == {}
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


def test_synchronous_and_worker_paths_publish_identical_business_snapshots(
    recommendation_policy,
    application_feature_factory,
) -> None:
    now = datetime.fromisoformat("2026-07-16T14:30:00+08:00")
    features = tuple(application_feature_factory(f"60000{index}", now) for index in range(1, 3))
    sync_repository = MemoryRepository()
    async_repository = MemoryRepository()

    def build(repository: MemoryRepository) -> RecommendationPipeline:
        return RecommendationPipeline(
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
    pipeline = RecommendationPipeline(
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
    pipeline = RecommendationPipeline(
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
    pipeline = RecommendationPipeline(
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
    pipeline = RecommendationPipeline(
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
    pipeline = RecommendationPipeline(
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
        self.fetch_threads: list[str] = []

    def fetch_market_features(
        self,
        observed_at: datetime,
        *,
        force: bool = False,
        deadline: datetime | None = None,
    ) -> Sequence[FeatureSnapshot]:
        del force, deadline
        self.fetch_threads.append(threading.current_thread().name)
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
        deadline: datetime | None = None,
    ) -> Sequence[FeatureSnapshot]:
        del deadline
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

    @staticmethod
    def refresh_intraday_tail(codes: Sequence[str], observed_at: datetime) -> None:
        del codes, observed_at

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
            raise MarketDataUnavailable("all full-market sources failed")
        return super().fetch_market_features(observed_at, force=force, deadline=deadline)


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
            raise MarketDataUnavailable("candidate quote source failed")
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
        deadline: datetime | None = None,
    ) -> Sequence[FeatureSnapshot]:
        if self.candidate_unavailable:
            raise MarketDataUnavailable("candidate quote source failed")
        return super().refresh_candidate_quotes(codes, observed_at, deadline=deadline)


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
            raise MarketDataUnavailable("tomorrow candidate source failed")
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
        self.events: list[Mapping[str, object]] = []
        self.write_threads: list[str] = []
        self._event_lock = threading.Lock()

    @staticmethod
    def initialize() -> None:
        return None

    @staticmethod
    def recover() -> Mapping[str, int]:
        return {"recovered": 0, "quarantined": 0, "orphaned": 0}

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

    def reserve_event(self, event: Mapping[str, object]) -> bool:
        self.write_threads.append(threading.current_thread().name)
        identity = tuple(
            event[name] for name in ("trade_date", "phase", "strategy", "event_type", "subject_key", "data_version")
        )
        with self._event_lock:
            for stored in self.events:
                stored_identity = tuple(
                    stored[name]
                    for name in ("trade_date", "phase", "strategy", "event_type", "subject_key", "data_version")
                )
                if stored_identity == identity:
                    return False
            self.events.append(dict(event))
            return True

    def compare_and_set_event(
        self,
        event_id: str,
        *,
        expected_status: str,
        status: str,
        retry_count: int,
        error: str = "",
    ) -> bool:
        self.write_threads.append(threading.current_thread().name)
        with self._event_lock:
            for index, event in enumerate(self.events):
                if event["event_id"] != event_id or event["status"] != expected_status:
                    continue
                self.events[index] = {**event, "status": status, "retry_count": retry_count, "error": error}
                return True
        return False

    def pending_priority_events(self) -> Sequence[Mapping[str, object]]:
        latest = {str(event["event_id"]): event for event in self.events}
        return tuple(
            event
            for event in latest.values()
            if event.get("status") in {"pending", "running"}
            and isinstance(event.get("priority"), int)
            and int(event["priority"]) <= int(EventPriority.RISK)
        )

    def list_events(self, *, cursor: int, limit: int) -> Sequence[Mapping[str, object]]:
        return tuple(self.events[cursor : cursor + limit])


def _at_time(feature: FeatureSnapshot, observed_at: datetime) -> FeatureSnapshot:
    quote = replace(
        feature.quote,
        source_time=observed_at,
        received_time=observed_at,
        data_version=f"static:{observed_at.isoformat()}",
    )
    return replace(feature, quote=quote, observed_at=observed_at)


class RecordingQueue:
    def __init__(self) -> None:
        self.events: list[PipelineEvent] = []

    def put(self, event: PipelineEvent) -> bool:
        self.events.append(event)
        return True


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
    ) -> Mapping[str, object]:
        del phase, deadline
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
    ) -> Mapping[str, object]:
        del phase, deadline
        raise RuntimeError("review transport failed")


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
    ) -> Mapping[str, object]:
        del phase, deadline
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
