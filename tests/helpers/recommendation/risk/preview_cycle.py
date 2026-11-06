from __future__ import annotations
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
import pytest
from tests.helpers.recommendation.runtime.scheduler import (
    DataRefresh,
    FixedClock,
    Freezes,
    Sealing,
    Settlement,
    SharedReviews,
    TradingCalendar,
    _cadence,
    noop_research_factory,
)
from tests.helpers.recommendation.publication.preview_identity import STARTED_AT, preview_freeze
from trader.infra.workers import BoundedExecutor
from trader.recommendation.application.pipeline.freeze_publish.freeze_observers import AsyncFreezeObserver
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedFreezeIndex
from trader.recommendation.application.ports.runtime import CycleRequest, PipelineTaskRequest, RefreshOutcome
from trader.recommendation.application.runtime.cadence import PipelineTask, ScheduledPipelineTask
from trader.recommendation.application.runtime.recommendation_round import RecommendationRounds
from trader.recommendation.application.runtime.runtime_dependencies import RuntimeDependencies
from trader.recommendation.application.runtime.schedule import MarketPhase
from trader.recommendation.application.runtime.trading_session import SessionLease
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.publication.preview import PreviewContext


class PreviewData(DataRefresh):
    def __init__(self) -> None:
        super().__init__()
        self.contexts: list[PreviewContext] = []
        self.preview_requests: list[PipelineTaskRequest] = []
        self.failure_task: PipelineTask | None = None
        self.failure: Exception | None = None

    def prepare_preview(self, context: PreviewContext) -> None:
        self.contexts.append(context)

    def refresh_task(self, request: PipelineTaskRequest) -> RefreshOutcome:
        self.preview_requests.append(request)
        if request.task is self.failure_task and self.failure is not None:
            raise self.failure
        return super().refresh_task(request)


def dependencies(data: PreviewData) -> RuntimeDependencies:
    return RuntimeDependencies(
        control_pool=BoundedExecutor(worker_count=1, queue_capacity=1, thread_name_prefix="test-preview"),
        clock=FixedClock(STARTED_AT),
        calendar=TradingCalendar(),
        cadence=_cadence(STARTED_AT),
        data=data,
        freezes=Freezes(),
        reviews=SharedReviews(),
        index=UnifiedFreezeIndex(),
        observer=AsyncFreezeObserver((), capacity=1, thread_name="test-preview-observer"),
        sealing=Sealing(),
        settlement=Settlement(),
        research_factory=noop_research_factory,
        publish_freeze=lambda _event: pytest.fail("runner published a freeze directly"),
        publish_overlay=lambda _overlay: pytest.fail("runner published an overlay directly"),
        monotonic=lambda: 0.0,
        rounds=RecommendationRounds(monotonic=lambda: 0.0),
    )


def request_builder(strategy: Strategy, observed_at: datetime, phase: str) -> CycleRequest:
    return CycleRequest(
        strategy,
        observed_at.date(),
        observed_at,
        phase,
        1,
        "preview:fixture",
        True,
        observed_at + timedelta(seconds=30),
    )


def publish_pair(deps: RuntimeDependencies) -> Callable[[tuple[CycleRequest, CycleRequest], int], None]:
    def execute(requests: tuple[CycleRequest, CycleRequest], sequence: int) -> None:
        for request in requests:
            current = preview_freeze(strategy=request.strategy)
            assert current.preview_context == request.preview_context
            previous = deps.index.snapshot(request.strategy).current
            assert deps.index.publish(current, expected_version=previous.version if previous else None).accepted

    return execute


def _scheduled(deps: RuntimeDependencies, session: SessionLease | None = None) -> ScheduledPipelineTask:
    task = ScheduledPipelineTask(PipelineTask.SCORE, STARTED_AT, MarketPhase.BEFORE_CLOSE, session=session)
    admission = deps.rounds.enter(deps.round_profile, (task,))
    assert admission is not None
    return replace(
        task,
        round_sequence=admission.sequence,
        round_started_monotonic=admission.started_monotonic,
        round_deadline_monotonic=admission.deadline_monotonic,
        round_deadline=STARTED_AT + timedelta(seconds=120),
    )
