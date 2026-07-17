from __future__ import annotations

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


class StaticMarketData:
    def __init__(self, features: Sequence[FeatureSnapshot]) -> None:
        self._features = tuple(features)
        self.candidate_tail_requests: list[bool] = []

    def fetch_market_features(self, observed_at: datetime) -> Sequence[FeatureSnapshot]:
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
        self.candidate_tail_requests.append(include_intraday_tail)
        requested = set(codes)
        return tuple(_at_time(feature, observed_at) for feature in self._features if feature.quote.code in requested)

    @staticmethod
    def health() -> Mapping[str, object]:
        return {"status": "ok"}


class DegradingMarketData(StaticMarketData):
    def __init__(self, features: Sequence[FeatureSnapshot]) -> None:
        super().__init__(features)
        self.market_unavailable = False

    def fetch_market_features(self, observed_at: datetime) -> Sequence[FeatureSnapshot]:
        if self.market_unavailable:
            raise MarketDataUnavailable("all full-market sources failed")
        return super().fetch_market_features(observed_at)


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


class MemoryRepository:
    def __init__(self) -> None:
        self.published: dict[Strategy, RecommendationSnapshot] = {}
        self.frozen: dict[tuple[Strategy, str], object] = {}
        self.overlays: dict[tuple[Strategy, str], LiveOverlay] = {}
        self.events: list[Mapping[str, object]] = []

    @staticmethod
    def initialize() -> None:
        return None

    @staticmethod
    def recover() -> Mapping[str, int]:
        return {"recovered": 0, "quarantined": 0, "orphaned": 0}

    def publish(self, snapshot: RecommendationSnapshot) -> None:
        self.published[snapshot.strategy] = snapshot

    def freeze(self, snapshot: RecommendationSnapshot) -> None:
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
        existing = self.overlays.get((overlay.strategy, overlay.trade_date))
        if existing is not None and (existing.closing or existing.observed_at >= overlay.observed_at):
            return False
        self.overlays[(overlay.strategy, overlay.trade_date)] = overlay
        return True

    def load_live_overlay(self, strategy: Strategy, trade_date: str) -> LiveOverlay | None:
        return self.overlays.get((strategy, trade_date))

    def recommendation_dates(self, strategy: Strategy) -> Sequence[str]:
        return tuple(day for candidate, day in self.frozen if candidate is strategy)

    def append_event(self, event: Mapping[str, object]) -> None:
        self.events.append(event)

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
