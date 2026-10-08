from __future__ import annotations

import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tests.integration.test_scheduler_runtime import (
    DataRefresh,
    Decisions,
    FixedClock,
    Freezes,
    Settlement,
    SharedReviews,
    TradingCalendar,
    _cadence,
    noop_research_factory,
)
from trader.recommendation.application.pipeline.freeze_publish.decision_observers import AsyncDecisionObserver
from trader.recommendation.application.pipeline.freeze_publish.draft_index import UnifiedDecisionDraftIndex
from trader.recommendation.application.pipeline.freeze_publish.freeze_coordinator import (
    DecisionRuntimeIdentity,
    ScoredFreezeCoordinator,
)
from trader.recommendation.application.pipeline.freeze_publish.read_only_queries import UnifiedDecisionQueries
from trader.recommendation.application.pipeline.freeze_publish.runtime_adapters import FreezeAdapter
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedDecisionIndex
from trader.recommendation.application.ports.runtime import (
    DataRefreshUnavailableError,
    DecisionUnavailableError,
    SettlementUnavailableError,
)
from trader.recommendation.application.runtime.cadence import PipelineTask, SchedulePointKey, SchedulePointStatus
from trader.recommendation.application.runtime.schedule import SHANGHAI, SchedulePoint
from trader.recommendation.application.runtime.scheduler_runtime import RuntimeDependencies, SchedulerRuntime
from trader.recommendation.application.runtime.schedule_requests import pipeline_lane
from trader.recommendation.application.runtime.shutdown import ShutdownDeadline
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.persistence.decision_records import SQLiteDecisionRecordRepository


@pytest.mark.parametrize("failure", ("prepare", "local", "commit", "commit_after", "settlement"))
def test_close_recovery_retries_downstream_without_recollecting(tmp_path: Path, failure: str) -> None:
    at = datetime(2026, 8, 11, 15, 5, tzinfo=SHANGHAI)
    clock = FixedClock(at)
    index = UnifiedDecisionIndex()
    attempts = []
    builds = []

    class Data(DataRefresh):
        def refresh(self, request):
            super().refresh(request)
            if failure == "prepare" and self.calls.count(Strategy.TOMORROW) == 1:
                if request.strategy is Strategy.TOMORROW:
                    raise DataRefreshUnavailableError("controlled_prepare_failure")

    class Builder(Decisions):
        def build_local(self, request):
            builds.append(request.strategy)
            if failure == "local" and request.strategy is Strategy.TOMORROW:
                if builds.count(Strategy.TOMORROW) == 1:
                    raise DecisionUnavailableError("controlled_local_failure")
            return super().build_local(request)

    class Repository(SQLiteDecisionRecordRepository):
        def commit(self, record):
            if record.strategy is Strategy.TOMORROW:
                attempts.append(record)
                if failure == "commit" and len(attempts) == 1:
                    raise OSError("controlled_write_failure")
            super().commit(record)
            if failure == "commit_after" and record.strategy is Strategy.TOMORROW and len(attempts) == 1:
                raise OSError("controlled_failure_after_durable_write")

    class Outcomes(Settlement):
        def settle(self, observed_at):
            assert all(index.snapshot(strategy).formal is not None for strategy in (Strategy.TOMORROW, Strategy.D25))
            super().settle(observed_at)
            if failure == "settlement" and len(self.calls) == 1:
                raise SettlementUnavailableError("controlled_settlement_failure")

    repository = Repository(tmp_path)
    repository.initialize()
    freezers = tuple(
        ScoredFreezeCoordinator(
            index,
            repository,
            clock,
            runtime_identity=DecisionRuntimeIdentity("config-current", "strategy-current", "fusion-current"),
            strategy=strategy,
        )
        for strategy in (Strategy.TOMORROW, Strategy.D25)
    )
    data, reviews, settlement = Data(), SharedReviews(), Outcomes()
    runtime = SchedulerRuntime(
        RuntimeDependencies(
            clock=clock,
            calendar=TradingCalendar(),
            cadence=_cadence(at),
            data=data,
            decisions=Builder(),
            reviews=reviews,
            index=index,
            observer=AsyncDecisionObserver((), capacity=4, thread_name="test-close-recovery"),
            freezes=FreezeAdapter(*freezers),
            settlement=settlement,
            research_factory=noop_research_factory,
            publish_decision=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-current",
    )
    key = SchedulePointKey(at.date().isoformat(), SchedulePoint.CLOSE_QUOTES, "-")
    queries = UnifiedDecisionQueries(index, UnifiedDecisionDraftIndex(), repository, clock)
    runtime.start()
    try:
        runtime.submit_due(at)
        assert runtime.wait_idle(2)
        d25 = index.snapshot(Strategy.D25).formal
        assert d25 is not None
        assert runtime.status().cadence.schedule_points[key].status is SchedulePointStatus.RETRY_WAIT
        if failure != "settlement":
            assert queries.current(Strategy.TOMORROW).status == "not_ready"
            assert settlement.calls == []
        if failure in {"commit", "commit_after"}:
            assert index.is_sealed(Strategy.TOMORROW, at.date())
        before = tuple(builds)
        clock.current = at + timedelta(milliseconds=999)
        runtime.submit_due(clock.current)
        assert runtime.wait_idle(2)
        assert tuple(builds) == before
        clock.current = at + timedelta(seconds=1)
        runtime.submit_due(clock.current)
        assert runtime.wait_idle(2)
        assert runtime.status().cadence.schedule_points[key].status is SchedulePointStatus.RETRY_WAIT
        clock.current = at + timedelta(seconds=3)
        runtime.submit_due(clock.current)
        assert runtime.wait_idle(2)
        assert runtime.status().cadence.schedule_points[key].status is SchedulePointStatus.COMPLETED
        assert index.snapshot(Strategy.D25).formal == d25
        formal = index.snapshot(Strategy.TOMORROW).formal
        assert formal is not None
        for view in (queries.current(Strategy.TOMORROW), queries.history(Strategy.TOMORROW, at.date())):
            assert view.status == "ready" and view.frozen
            assert view.content_hash == formal.decision.content_hash
        if failure == "commit":
            assert len(attempts) == 2 and attempts[0] == attempts[1]
            assert builds.count(Strategy.TOMORROW) == 1
        if failure == "commit_after":
            assert len(attempts) == 1 and attempts[0] == formal
            assert builds.count(Strategy.TOMORROW) == 1
        if failure == "settlement":
            assert builds.count(Strategy.TOMORROW) == before.count(Strategy.TOMORROW)
            assert len(settlement.calls) == 2
        clock.current = at + timedelta(seconds=31)
        runtime.submit_due(clock.current)
        assert runtime.wait_idle(2)
        assert index.snapshot(Strategy.TOMORROW).formal == formal
        assert runtime.status().settlement_completed_count == 1
        assert runtime.status().settlement_failure_count == (1 if failure == "settlement" else 0)
        assert sum(request.task is PipelineTask.CLOSE_QUOTES for request in data.task_requests) == 1
        assert builds.count(Strategy.D25) == 1
        assert reviews.calls == []
        assert index.snapshot(Strategy.LONG).formal is None
    finally:
        runtime.stop(ShutdownDeadline.start(2))


@pytest.mark.parametrize("blocking_stage", ("local", "settlement"))
def test_close_retry_does_not_duplicate_inflight_work(blocking_stage: str) -> None:
    # Reuse the full scheduler fixture while holding local scoring across retry ticks.
    at = datetime(2026, 8, 11, 15, 5, tzinfo=SHANGHAI)
    clock, data, index = FixedClock(at), DataRefresh(), UnifiedDecisionIndex()
    started, release = threading.Event(), threading.Event()
    calls = []

    class Builder(Decisions):
        def build_local(self, request):
            calls.append(request.strategy)
            if blocking_stage == "local" and request.strategy is Strategy.TOMORROW:
                started.set()
                assert release.wait(3)
            return super().build_local(request)

    class Outcomes(Settlement):
        def settle(self, at):
            super().settle(at)
            if blocking_stage == "settlement":
                started.set()
                assert release.wait(3)

    settlement = Outcomes()
    runtime = SchedulerRuntime(
        RuntimeDependencies(
            clock=clock,
            calendar=TradingCalendar(),
            cadence=_cadence(at),
            data=data,
            decisions=Builder(),
            reviews=SharedReviews(),
            index=index,
            observer=AsyncDecisionObserver((), capacity=4, thread_name="test-close-running"),
            freezes=Freezes(index),
            settlement=settlement,
            research_factory=noop_research_factory,
            publish_decision=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-current",
    )
    runtime.start()
    try:
        runtime.submit_due(at)
        if blocking_stage == "settlement":
            assert runtime.wait_idle(2)
            clock.current = at + timedelta(seconds=1)
            runtime.submit_due(clock.current)
        assert started.wait(1)
        for seconds in (3, 8):
            clock.current = at + timedelta(seconds=seconds)
            runtime.submit_due(clock.current)
        release.set()
        assert runtime.wait_idle(2)
        assert calls.count(Strategy.TOMORROW) == 1
        assert index.snapshot(Strategy.TOMORROW).formal is not None
        assert sum(request.task is PipelineTask.CLOSE_QUOTES for request in data.task_requests) == 1
        if blocking_stage == "settlement":
            assert len(settlement.calls) == 1
            key = SchedulePointKey(at.date().isoformat(), SchedulePoint.CLOSE_QUOTES, "-")
            assert runtime.status().cadence.schedule_points[key].status is SchedulePointStatus.COMPLETED
    finally:
        release.set()
        runtime.stop(ShutdownDeadline.start(2))


@pytest.mark.parametrize("transition", ("session", "next_day", "fast_handoff"))
def test_close_receipt_lifetime_and_fast_handoff(tmp_path: Path, monkeypatch, transition: str) -> None:
    at = datetime(2026, 8, 11, 15, 5, tzinfo=SHANGHAI)
    clock, data, index = FixedClock(at), DataRefresh(), UnifiedDecisionIndex()
    cadence, settlement = _cadence(at), Settlement()
    repository = SQLiteDecisionRecordRepository(tmp_path)
    repository.initialize()
    freezers = tuple(
        ScoredFreezeCoordinator(
            index,
            repository,
            clock,
            runtime_identity=DecisionRuntimeIdentity("config-current", "strategy-current", "fusion-current"),
            strategy=strategy,
        )
        for strategy in (Strategy.TOMORROW, Strategy.D25)
    )
    runtime = SchedulerRuntime(
        RuntimeDependencies(
            clock=clock,
            calendar=TradingCalendar(),
            cadence=cadence,
            data=data,
            decisions=Decisions(),
            reviews=SharedReviews(),
            index=index,
            observer=AsyncDecisionObserver((), capacity=4, thread_name="test-close-transition"),
            freezes=FreezeAdapter(*freezers),
            settlement=settlement,
            research_factory=noop_research_factory,
            publish_decision=lambda _event: None,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-current",
    )
    if transition == "fast_handoff":
        lane = runtime._task_lanes[pipeline_lane(PipelineTask.CLOSE_QUOTES)]  # noqa: SLF001
        offer = lane.offer

        def finish_before_offer_returns(scheduled):
            result = offer(scheduled)
            assert runtime.wait_idle(2)
            return result

        monkeypatch.setattr(lane, "offer", finish_before_offer_returns)
    runtime.start()
    try:
        runtime.submit_due(at)
        assert runtime.wait_idle(2)
        key = SchedulePointKey(at.date().isoformat(), SchedulePoint.CLOSE_QUOTES, "-")
        assert runtime.status().cadence.schedule_points[key].status is SchedulePointStatus.RETRY_WAIT
        if transition == "session":
            cadence.rotate_session(at, reason="controlled_rotation")
        clock.current = at + timedelta(seconds=1)
        runtime.submit_due(clock.current)
        assert runtime.wait_idle(2)
        assert runtime.status().cadence.schedule_points[key].status is SchedulePointStatus.COMPLETED
        if transition == "next_day":
            clock.current = at + timedelta(days=1)
            runtime.submit_due(clock.current)
            assert runtime.wait_idle(2)
            # An already-running scheduler first completes its next-day normal freeze point.
            clock.current += timedelta(seconds=1)
            runtime.submit_due(clock.current)
            assert runtime.wait_idle(2)
            for strategy in (Strategy.TOMORROW, Strategy.D25):
                formal = index.snapshot(strategy).formal
                assert formal is not None and formal.trade_date == clock.current.date()
            clock.current += timedelta(seconds=2)
            runtime.submit_due(clock.current)
            assert runtime.wait_idle(2)
        expected = 2 if transition == "next_day" else 1
        assert sum(request.task is PipelineTask.CLOSE_QUOTES for request in data.task_requests) == expected
        assert runtime.status().settlement_completed_count == expected
        assert len(settlement.calls) == expected
    finally:
        runtime.stop(ShutdownDeadline.start(2))
