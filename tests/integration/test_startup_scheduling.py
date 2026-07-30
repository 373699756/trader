"""Integration coverage for cold-start scheduling and freeze eligibility."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast

import pytest

from tests.pipeline_factory import build_pipeline
from trader.application.cadence import CadencePlanner, CadencePolicy, PipelineTask, SchedulePointResult
from trader.application.freeze_attempts import FreezeAttemptStore
from trader.application.ports.snapshots import PublishedSnapshotReadPort, SnapshotReaderPort
from trader.application.publisher import SnapshotPublisher
from trader.application.queries import CloseFallbackReplay, RecommendationQueries
from trader.application.recommendations import RecommendationEngine
from trader.application.schedule import SHANGHAI
from trader.application.status import RuntimeState
from trader.application.trading_session import TradingSessionTracker
from trader.domain.recommendation.models import RecommendationSnapshot, Strategy


@dataclass(frozen=True)
class FrozenFixture:
    snapshot_id: str
    trade_date: str
    published_at: datetime
    values: tuple[int, ...]


@dataclass(frozen=True)
class EmptyFallbackFixture:
    snapshot_id: str
    strategy: Strategy
    trade_date: str
    phase: str
    frozen: bool
    recommendations: tuple[object, ...] = ()


class EmptySnapshots:
    def __init__(self, frozen: object | None = None) -> None:
        self.frozen = frozen

    def latest(self, _strategy: Strategy) -> None:
        return None

    def load_frozen(self, _strategy: Strategy, _trade_date: str) -> object | None:
        return self.frozen

    def load_live_overlay(self, _strategy: Strategy, _trade_date: str) -> None:
        return None

    def recommendation_dates(self, _strategy: Strategy) -> tuple[str, ...]:
        return ()


class ForbiddenReplay:
    @staticmethod
    def replay(_snapshot: object) -> object:
        raise AssertionError("read-only queries must not replay a snapshot")


class FreezeRetryRepository:
    def __init__(self) -> None:
        self.attempted: list[RecommendationSnapshot] = []
        self.frozen: dict[tuple[Strategy, str], RecommendationSnapshot] = {}

    def recommendation_dates(self, strategy: Strategy) -> tuple[str, ...]:
        return tuple(trade_date for candidate, trade_date in self.frozen if candidate is strategy)

    def load_frozen(self, strategy: Strategy, trade_date: str) -> RecommendationSnapshot | None:
        return self.frozen.get((strategy, trade_date))

    @staticmethod
    def load_checkpoint(
        _strategy: Strategy,
        _trade_date: str,
        *,
        boundary_at: datetime,
    ) -> None:
        del boundary_at
        return None

    def freeze(self, snapshot: RecommendationSnapshot) -> None:
        self.attempted.append(snapshot)
        if len(self.attempted) == 1:
            raise OSError("temporary JSON failure")
        self.frozen[(snapshot.strategy, snapshot.trade_date)] = snapshot

    @staticmethod
    def consume_checkpoint(
        _strategy: Strategy,
        _trade_date: str,
        *,
        boundary_at: datetime,
    ) -> None:
        del boundary_at


class UnusedMarketPorts:
    pass


@pytest.mark.parametrize(
    ("started_clock", "expected_freezes", "expects_close"),
    (
        ("09:15:00", (), False),
        ("09:30:00", (), False),
        ("11:19:49", (), False),
        ("11:19:50", (), False),
        ("11:20:00", (), False),
        ("11:20:01", (), False),
        ("14:49:19", (), False),
        ("14:49:20", (), False),
        ("14:49:50", (), False),
        ("14:50:00", ("tomorrow", "d25"), False),
        ("14:50:01", ("tomorrow", "d25"), False),
        ("15:00:00", (), True),
        ("15:10:00", (), True),
        ("19:30:00", (), True),
        ("23:59:00", (), True),
    ),
)
def test_cold_start_boundary_matrix(
    started_clock: str,
    expected_freezes: tuple[str, ...],
    expects_close: bool,
) -> None:
    started_at = datetime.fromisoformat(f"2026-07-16T{started_clock}").replace(tzinfo=SHANGHAI)
    planner = CadencePlanner(_policy(), started_at=started_at)

    tasks = planner.plan(started_at, is_trading_day=True).tasks
    freeze = next((task for task in tasks if task.task is PipelineTask.FREEZE), None)

    assert (() if freeze is None else freeze.freeze_strategies) == expected_freezes
    assert (PipelineTask.CLOSE_QUOTES in {task.task for task in tasks}) is expects_close
    if started_at.time() >= datetime.fromisoformat("2026-07-16T14:50:00").time():
        assert PipelineTask.DEEPSEEK_CUTOFF not in {task.task for task in tasks}
        assert PipelineTask.FINAL_CANDIDATE_QUOTES not in {task.task for task in tasks}


def test_continuously_running_scheduler_may_submit_one_second_late() -> None:
    started_at = datetime(2026, 7, 16, 11, 19, 49, tzinfo=SHANGHAI)
    planner = CadencePlanner(_policy(), started_at=started_at)
    planner.plan(started_at, is_trading_day=True)

    tasks = planner.plan(started_at + timedelta(seconds=12), is_trading_day=True).tasks

    freeze = next(task for task in tasks if task.task is PipelineTask.FREEZE)
    assert freeze.freeze_strategies == ("today",)


def test_freeze_retry_preserves_snapshot_id_canonical_bytes_and_sha256() -> None:
    boundary = datetime(2026, 7, 16, 14, 50, tzinfo=SHANGHAI)
    snapshot = FrozenFixture("snapshot-fixed", "2026-07-16", boundary, (3, 1, 4))
    store = FreezeAttemptStore()

    first = store.seal_snapshot(
        strategy="tomorrow",
        trade_date="2026-07-16",
        boundary_at=boundary,
        frozen_snapshot=snapshot,
    )
    second = store.retry(first.key, at=boundary)
    third = store.retry(first.key, at=boundary + timedelta(seconds=1))

    assert first.frozen_snapshot is snapshot
    assert second.frozen_snapshot is snapshot
    assert third.frozen_snapshot is snapshot
    assert first.snapshot_id == second.snapshot_id == third.snapshot_id == "snapshot-fixed"
    assert first.canonical_payload == second.canonical_payload == third.canonical_payload
    assert first.canonical_sha256 == second.canonical_sha256 == third.canonical_sha256
    assert second.next_retry_at == boundary + timedelta(seconds=1)
    assert third.next_retry_at == boundary + timedelta(seconds=3)


def test_snapshot_workflow_retries_the_exact_same_frozen_object(
    recommendation_policy,
    application_feature_factory,
) -> None:
    boundary = datetime(2026, 7, 16, 14, 50, tzinfo=SHANGHAI)
    draft_at = boundary - timedelta(seconds=10)
    engine = RecommendationEngine(recommendation_policy)
    draft = engine.build_snapshot(
        Strategy.TOMORROW,
        (application_feature_factory("600001", draft_at),),
        now=draft_at,
        phase="final_quote",
        trade_date="2026-07-16",
        data_version="fixed-freeze",
        review_port=None,
        review_deadline=boundary,
        max_age_seconds=30.0,
        filtered_count=0,
        filter_reasons={},
    )
    state = RuntimeState()
    state.publish(draft)
    repository = FreezeRetryRepository()
    pipeline = build_pipeline(
        UnusedMarketPorts(),
        object(),
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
        cadence_policy=_policy(),
    )

    assert pipeline._freeze_available_snapshots(boundary, ("tomorrow",)) == ()
    retried = pipeline._freeze_available_snapshots(boundary + timedelta(seconds=1), ("tomorrow",))

    assert len(retried) == 1
    assert len(repository.attempted) == 2
    assert repository.attempted[0] is repository.attempted[1]
    assert repository.attempted[0].snapshot_id == repository.attempted[1].snapshot_id


def test_pending_boundary_attempt_blocks_close_fallback_until_terminal() -> None:
    started_at = datetime(2026, 7, 16, 9, 15, tzinfo=SHANGHAI)
    boundary = datetime(2026, 7, 16, 14, 50, tzinfo=SHANGHAI)
    planner = CadencePlanner(_policy(), started_at=started_at)
    freeze = next(
        task for task in planner.plan(boundary, is_trading_day=True).tasks if task.task is PipelineTask.FREEZE
    )
    planner.record_submission(freeze, accepted=True, at=boundary)
    planner.record_results(
        freeze,
        {
            "tomorrow": SchedulePointResult.RETRY,
            "d25": SchedulePointResult.RETRY,
        },
        at=boundary,
    )

    at_close = planner.plan(boundary + timedelta(minutes=10), is_trading_day=True)

    assert PipelineTask.CLOSE_QUOTES not in {task.task for task in at_close.tasks}
    assert next(task for task in at_close.tasks if task.task is PipelineTask.FREEZE).freeze_strategies == (
        "tomorrow",
        "d25",
    )


@pytest.mark.parametrize(
    ("clock", "calendar_result", "calendar_failure", "expected"),
    (
        ("08:30:00", True, False, "before_market_open"),
        ("10:00:00", False, False, "market_closed"),
        ("10:00:00", False, True, "calendar_unavailable"),
        ("15:10:00", True, False, "official_record_missing"),
    ),
)
def test_session_aware_readiness_reasons(
    clock: str,
    calendar_result: bool,
    calendar_failure: bool,
    expected: str,
) -> None:
    now = datetime.fromisoformat(f"2026-07-16T{clock}").replace(tzinfo=SHANGHAI)
    tracker = TradingSessionTracker(now)

    def calendar(_day) -> bool:
        if calendar_failure:
            raise OSError("calendar offline")
        return calendar_result

    tracker.refresh(now, calendar)
    queries = RecommendationQueries(
        cast(PublishedSnapshotReadPort, EmptySnapshots()),
        now=lambda: now,
        session_status=tracker.status,
    )

    assert queries.recommendation(Strategy.TOMORROW).readiness_reason == expected


def test_read_only_query_keeps_empty_fallback_without_calling_replay() -> None:
    now = datetime(2026, 7, 16, 15, 10, tzinfo=SHANGHAI)
    frozen = EmptyFallbackFixture("empty", Strategy.D25, "2026-07-16", "close_fallback", True)
    snapshots = EmptySnapshots(frozen)
    queries = RecommendationQueries(
        cast(PublishedSnapshotReadPort, snapshots),
        now=lambda: now,
        close_fallback_replay=CloseFallbackReplay(
            cast(SnapshotReaderPort, snapshots),
            cast(RecommendationEngine, ForbiddenReplay()),
        ),
    )

    result = queries.recommendation(Strategy.D25, "2026-07-16")

    assert result.status == "ready"
    assert result.snapshot is cast(RecommendationSnapshot, frozen)


def _policy() -> CadencePolicy:
    return CadencePolicy.from_seconds(
        {
            "full_market": {"today_main": 30, "midday": 60, "final_window": 30},
            "candidate_quotes": {"today_main": 5, "midday": 60, "final_window": 2},
            "topk_quotes": {"today_main": 3, "midday": 60, "final_window": 3},
            "long_quotes": {"today_main": 3, "midday": 60, "final_window": 3},
            "score": {"today_main": 10},
            "industry_heat": {"today_main": 60},
            "market_news": {"today_main": 60},
            "stock_risk": {"today_main": 180},
        }
    )
