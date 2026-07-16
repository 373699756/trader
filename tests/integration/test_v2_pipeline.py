from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime

from trader.application.events import EventPriority, PipelineEvent, new_event
from trader.application.pipeline import RecommendationPipeline
from trader.application.publisher import SnapshotPublisher
from trader.application.recommendations import RecommendationEngine
from trader.application.status import RuntimeState
from trader.domain.models import FeatureSnapshot, RecommendationSnapshot, Strategy


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
    pipeline = RecommendationPipeline(
        StaticMarketData(features),
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

    clock.set(datetime.fromisoformat("2026-07-16T11:20:00+08:00"))
    morning_freeze = pipeline.run_once(clock.now())
    assert morning_freeze[-1].strategy is Strategy.TODAY
    assert morning_freeze[-1].frozen is True

    clock.set(datetime.fromisoformat("2026-07-16T14:30:00+08:00"))
    afternoon = pipeline.run_once(clock.now())
    assert {snapshot.strategy for snapshot in afternoon} == {Strategy.TOMORROW, Strategy.D25, Strategy.LONG}

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

    def fetch_market_features(self, observed_at: datetime) -> Sequence[FeatureSnapshot]:
        return tuple(_at_time(feature, observed_at) for feature in self._features)

    def fetch_candidate_features(
        self,
        codes: Sequence[str],
        observed_at: datetime,
    ) -> Sequence[FeatureSnapshot]:
        requested = set(codes)
        return tuple(_at_time(feature, observed_at) for feature in self._features if feature.quote.code in requested)

    @staticmethod
    def health() -> Mapping[str, object]:
        return {"status": "ok"}


class MemoryRepository:
    def __init__(self) -> None:
        self.published: dict[Strategy, RecommendationSnapshot] = {}
        self.frozen: dict[tuple[Strategy, str], object] = {}
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

    def latest(self, strategy: Strategy) -> RecommendationSnapshot | None:
        return self.published.get(strategy)

    def load_frozen(self, strategy: Strategy, trade_date: str) -> RecommendationSnapshot | None:
        return None

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
