from __future__ import annotations

import threading
from dataclasses import replace
from datetime import date, datetime, timedelta

from tests.unit.domain.test_decision_identity import NOW, decision
from trader.application.decision_core import UnifiedDecisionIndex
from trader.application.decision_observers import AsyncDecisionObserver
from trader.application.ports.v2_runtime import (
    SharedDeepSeekRuntimeContract,
    V2CycleRequest,
    V2DataRefreshUnavailableError,
    V2ReviewUnavailableError,
)
from trader.application.schedule import SHANGHAI
from trader.application.shutdown import ShutdownDeadline
from trader.application.v2_runtime import V2RuntimeDependencies, V2SchedulerRuntime
from trader.domain.recommendation.decision_identity import LongProjection, LongProjectionItem, ScoredDecision
from trader.domain.recommendation.models import Strategy


class TradingCalendar:
    def is_trading_day(self, _day: date) -> bool:
        return True


class FixedClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current


class DataRefresh:
    def __init__(self) -> None:
        self.calls: list[Strategy] = []

    def refresh(self, request: V2CycleRequest) -> None:
        self.calls.append(request.strategy)


class Decisions:
    def build_local(self, request: V2CycleRequest):
        if request.strategy is Strategy.LONG:
            return LongProjection(
                request.trade_date,
                request.sequence,
                request.observed_at,
                (("quotes", request.input_version),),
                (LongProjectionItem("600001", "core", request.input_version),),
            )
        return replace(
            decision(request.strategy, sequence=request.sequence),
            trade_date=request.trade_date,
            observed_at=request.observed_at,
        )


class SharedReviews:
    runtime_contract = SharedDeepSeekRuntimeContract(
        daily_physical_limit=168,
        shared_cache=True,
        shared_single_flight=True,
    )

    def __init__(self) -> None:
        self.calls: list[Strategy] = []

    def build_hybrid(self, local: ScoredDecision, request: V2CycleRequest) -> ScoredDecision:
        self.calls.append(request.strategy)
        return replace(
            local,
            sequence=local.sequence + 1,
            stage="hybrid",
            parent_version=local.version,
            items=(replace(local.items[0], final_score=89.0),),
        )


class Freezes:
    def __init__(self) -> None:
        self.calls: list[tuple[Strategy, str | None]] = []

    def freeze(self, strategy: Strategy, _at: datetime, current) -> None:
        self.calls.append((strategy, current.version if current is not None else None))


class Settlement:
    def __init__(self) -> None:
        self.calls: list[datetime] = []

    def settle(self, at: datetime) -> None:
        self.calls.append(at)


def test_v2_fixture_runs_without_the_legacy_pipeline_through_shutdown() -> None:
    morning = datetime(2026, 8, 11, 10, 0, tzinfo=SHANGHAI)
    freeze_at = datetime(2026, 8, 11, 14, 50, tzinfo=SHANGHAI)
    close_at = datetime(2026, 8, 11, 15, 0, tzinfo=SHANGHAI)
    data = DataRefresh()
    reviews = SharedReviews()
    freezes = Freezes()
    settlement = Settlement()
    observed: list[str] = []
    observer = AsyncDecisionObserver(
        (lambda event: observed.append(event.event_id),),
        capacity=16,
        thread_name="test-v2-fixture-observer",
    )
    index = UnifiedDecisionIndex()
    runtime = V2SchedulerRuntime(
        V2RuntimeDependencies(
            clock=FixedClock(morning),
            calendar=TradingCalendar(),
            data=data,
            decisions=Decisions(),
            reviews=reviews,
            index=index,
            observer=observer,
            freezes=freezes,
            settlement=settlement,
        ),
        config_version="runtime-v2",
    )

    assert runtime.start()
    runtime.submit_due()
    assert runtime.wait_idle(2.0)
    runtime.submit_due(freeze_at)
    runtime.submit_due(freeze_at)
    runtime.submit_due(close_at)
    runtime.submit_due(close_at)
    assert runtime.wait_idle(2.0)
    status = runtime.status()
    report = runtime.stop(ShutdownDeadline.start(2.0))

    assert report.completed
    assert set(data.calls) == {Strategy.TODAY, Strategy.TOMORROW, Strategy.D25, Strategy.LONG}
    assert set(reviews.calls) == {Strategy.TODAY, Strategy.TOMORROW, Strategy.D25}
    assert len(observed) == 6
    assert {strategy for strategy, version in freezes.calls if version} == {Strategy.TOMORROW, Strategy.D25}
    assert settlement.calls == [close_at]
    assert status.freeze_completed_count == 2
    assert status.settlement_completed_count == 1
    assert status.config_version == "runtime-v2"
    assert all(index.snapshot(strategy).current is not None for strategy in Strategy)
    assert not any(thread.name.startswith("trader-v2-") for thread in threading.enumerate())
    assert runtime.submit_tick(close_at) is False
    assert runtime.status().control_rejected_count == status.control_rejected_count


def test_tomorrow_lane_progresses_while_today_lane_is_blocked() -> None:
    today_entered = threading.Event()
    today_release = threading.Event()

    class BlockingDecisions(Decisions):
        def build_local(self, request: V2CycleRequest):
            if request.strategy is Strategy.TODAY:
                today_entered.set()
                today_release.wait(timeout=1.0)
            return super().build_local(request)

    index = UnifiedDecisionIndex()
    runtime = V2SchedulerRuntime(
        V2RuntimeDependencies(
            clock=FixedClock(NOW),
            calendar=TradingCalendar(),
            data=DataRefresh(),
            decisions=BlockingDecisions(),
            reviews=SharedReviews(),
            index=index,
            observer=AsyncDecisionObserver((), capacity=1, thread_name="test-v2-reserved-observer"),
            freezes=Freezes(),
            settlement=Settlement(),
        ),
        config_version="runtime-v2",
    )
    runtime.start()
    today = V2CycleRequest(Strategy.TODAY, NOW.date(), NOW, "afternoon", 1, "today-v1", True, NOW)
    tomorrow = replace(today, strategy=Strategy.TOMORROW, input_version="tomorrow-v1")
    runtime.submit_cycle(today)
    assert today_entered.wait(timeout=1.0)
    runtime.submit_cycle(tomorrow)

    try:
        assert _wait_for(lambda: index.snapshot(Strategy.TOMORROW).current is not None)
        assert index.snapshot(Strategy.TODAY).current is None
    finally:
        today_release.set()
        runtime.stop(ShutdownDeadline.start(2.0))


def test_refresh_and_review_failure_retain_and_publish_the_local_decision() -> None:
    class FailingData(DataRefresh):
        def refresh(self, request: V2CycleRequest) -> None:
            super().refresh(request)
            raise V2DataRefreshUnavailableError("source unavailable")

    class FailingReviews(SharedReviews):
        def build_hybrid(self, local: ScoredDecision, request: V2CycleRequest) -> ScoredDecision:
            raise V2ReviewUnavailableError("review unavailable")

    index = UnifiedDecisionIndex()
    runtime = V2SchedulerRuntime(
        V2RuntimeDependencies(
            clock=FixedClock(NOW),
            calendar=TradingCalendar(),
            data=FailingData(),
            decisions=Decisions(),
            reviews=FailingReviews(),
            index=index,
            observer=AsyncDecisionObserver((), capacity=1, thread_name="test-v2-failure-observer"),
            freezes=Freezes(),
            settlement=Settlement(),
        ),
        config_version="runtime-v2",
    )
    request = V2CycleRequest(
        Strategy.TOMORROW,
        NOW.date(),
        NOW,
        "afternoon",
        1,
        "input-v1",
        True,
        NOW + timedelta(minutes=1),
    )
    runtime.start()
    runtime.submit_cycle(request)
    assert runtime.wait_idle(1.0)
    status = runtime.status()
    runtime.stop(ShutdownDeadline.start(1.0))

    current = index.snapshot(Strategy.TOMORROW).current
    assert isinstance(current, ScoredDecision)
    assert current.stage == "local"
    assert status.refresh_failure_count == 1
    assert status.review_failure_count == 1
    assert status.local_publish_count == 1
    assert status.hybrid_publish_count == 0


def test_review_deadline_prevents_a_late_model_upgrade() -> None:
    reviews = SharedReviews()
    index = UnifiedDecisionIndex()
    runtime = V2SchedulerRuntime(
        V2RuntimeDependencies(
            clock=FixedClock(NOW),
            calendar=TradingCalendar(),
            data=DataRefresh(),
            decisions=Decisions(),
            reviews=reviews,
            index=index,
            observer=AsyncDecisionObserver((), capacity=1, thread_name="test-v2-deadline-observer"),
            freezes=Freezes(),
            settlement=Settlement(),
        ),
        config_version="runtime-v2",
    )
    request = V2CycleRequest(Strategy.TOMORROW, NOW.date(), NOW, "afternoon", 1, "input-v1", True, NOW)
    runtime.start()
    runtime.submit_cycle(request)
    assert runtime.wait_idle(1.0)
    status = runtime.status()
    runtime.stop(ShutdownDeadline.start(1.0))

    current = index.snapshot(Strategy.TOMORROW).current
    assert isinstance(current, ScoredDecision)
    assert current.stage == "local"
    assert reviews.calls == []
    assert status.local_publish_count == 1
    assert status.hybrid_publish_count == 0


def _wait_for(predicate, timeout: float = 1.0) -> bool:
    deadline = threading.Event()
    for _attempt in range(int(timeout / 0.01)):
        if predicate():
            return True
        deadline.wait(0.01)
    return predicate()
