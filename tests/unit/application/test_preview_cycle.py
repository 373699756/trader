from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta

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
from tests.unit.domain.test_decision_identity import decision
from tests.unit.domain.test_preview_identity import MARKET_DATE, STARTED_AT, preview_decision
from trader.infra.workers import BoundedExecutor
from trader.recommendation.application.pipeline.freeze_publish.decision_observers import AsyncDecisionObserver
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedDecisionIndex
from trader.recommendation.application.ports.runtime import (
    CycleRequest,
    DataRefreshPendingError,
    DataRefreshUnavailableError,
    PipelineTaskRequest,
    RefreshOutcome,
)
from trader.recommendation.application.runtime.cadence import PipelineTask
from trader.recommendation.application.runtime.preview_cycle import PreviewCycleRunner
from trader.recommendation.application.runtime.runtime_dependencies import RuntimeDependencies
from trader.recommendation.application.runtime.trading_session import TradingSession
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
        decisions=Decisions(),
        reviews=SharedReviews(),
        index=UnifiedDecisionIndex(),
        observer=AsyncDecisionObserver((), capacity=1, thread_name="test-preview-observer"),
        freezes=Freezes(),
        settlement=Settlement(),
        research_factory=noop_research_factory,
        publish_decision=lambda _event: pytest.fail("runner published a decision directly"),
        publish_overlay=lambda _overlay: pytest.fail("runner published an overlay directly"),
        monotonic=lambda: 0.0,
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


def publish_pair(deps: RuntimeDependencies) -> Callable[[tuple[CycleRequest, CycleRequest]], None]:
    def execute(requests: tuple[CycleRequest, CycleRequest]) -> None:
        for request in requests:
            current = preview_decision(strategy=request.strategy)
            assert current.preview_context == request.preview_context
            previous = deps.index.snapshot(request.strategy).current
            assert deps.index.publish(current, expected_version=previous.version if previous else None).accepted

    return execute


def test_preview_runner_refreshes_fresh_inputs_and_executes_both_strategies_once() -> None:
    data = PreviewData()
    deps = dependencies(data)
    session = TradingSession(deps.clock.now, deps.monotonic).capture()
    context = PreviewContext("preview-a", MARKET_DATE, STARTED_AT)
    calls: list[tuple[CycleRequest, CycleRequest]] = []

    def execute(requests: tuple[CycleRequest, CycleRequest]) -> None:
        calls.append(requests)
        publish_pair(deps)(requests)

    runner = PreviewCycleRunner(deps, execute, request_builder)
    runner.execute(context, session)
    assert runner.status().state == "ready" and runner.status().error is None
    assert [request.task for request in data.preview_requests] == [
        PipelineTask.FULL_MARKET,
        PipelineTask.INDUSTRY_HEAT,
        PipelineTask.CANDIDATE_QUOTES,
        PipelineTask.INTRADAY_TAIL,
    ]
    assert all(request.preview_context == context and request.session is session for request in data.preview_requests)
    assert [request.strategy for request in calls[0]] == [Strategy.TOMORROW, Strategy.D25]
    assert all(
        request.trade_date == STARTED_AT.date()
        and request.observed_at == STARTED_AT
        and request.preview_context == context
        and request.session is session
        for request in calls[0]
    )
    runner.execute(context, session)
    assert data.contexts == [context] and len(calls) == 1
    assert not deps.control_pool.status().running and not deps.observer.status().running


@pytest.mark.parametrize(
    "task",
    (PipelineTask.FULL_MARKET, PipelineTask.INDUSTRY_HEAT, PipelineTask.CANDIDATE_QUOTES, PipelineTask.INTRADAY_TAIL),
)
def test_pending_preview_inputs_retry_without_computing_or_persisting(task: PipelineTask) -> None:
    data = PreviewData()
    data.failure_task, data.failure = task, DataRefreshPendingError("history_projection_pending")
    deps = dependencies(data)
    context = PreviewContext("preview-a", MARKET_DATE, STARTED_AT)
    calls: list[tuple[CycleRequest, CycleRequest]] = []

    def execute(requests: tuple[CycleRequest, CycleRequest]) -> None:
        calls.append(requests)
        publish_pair(deps)(requests)

    runner = PreviewCycleRunner(deps, execute, request_builder)
    runner.execute(context, None)
    assert runner.status().state == "waiting_inputs"
    assert runner.status().error == "history_projection_pending"
    assert not calls and deps.index.snapshot(Strategy.TOMORROW).current is None
    data.failure = None
    runner.execute(context, None)
    assert runner.status().state == "ready" and len(calls) == 1


@pytest.mark.parametrize(
    "failure", (DataRefreshUnavailableError("close_quote_unavailable"), OSError("unsupported input [details]"))
)
def test_failed_preview_retains_old_currents_and_does_not_repeat(failure: Exception) -> None:
    data = PreviewData()
    data.failure_task, data.failure = PipelineTask.CANDIDATE_QUOTES, failure
    deps = dependencies(data)
    old = decision()
    assert deps.index.publish(old, expected_version=None).accepted
    runner = PreviewCycleRunner(deps, lambda _requests: pytest.fail("failed inputs scored"), request_builder)
    context = PreviewContext("preview-a", MARKET_DATE, STARTED_AT)
    runner.execute(context, None)
    assert runner.status().state == "failed"
    assert runner.status().error in {"close_quote_unavailable", "preview_run_failed"}
    assert deps.index.snapshot(Strategy.TOMORROW).current is old
    count = len(data.preview_requests)
    runner.execute(context, None)
    assert len(data.preview_requests) == count


def test_preview_is_ready_only_when_both_current_decisions_belong_to_the_run() -> None:
    deps = dependencies(PreviewData())
    other = preview_decision(run_id="another-run", strategy=Strategy.D25)
    assert deps.index.publish(other, expected_version=None).accepted

    def execute(_requests: tuple[CycleRequest, CycleRequest]) -> None:
        assert deps.index.publish(preview_decision(), expected_version=None).accepted

    runner = PreviewCycleRunner(deps, execute, request_builder)
    runner.execute(PreviewContext("preview-a", MARKET_DATE, STARTED_AT), None)
    assert runner.status().state == "failed"
    assert runner.status().error == "preview_decision_unavailable"


def test_running_preview_claim_rejects_duplicate_concurrent_execution() -> None:
    deps = dependencies(PreviewData())
    entered, release = threading.Event(), threading.Event()
    calls: list[tuple[CycleRequest, CycleRequest]] = []

    def execute(requests: tuple[CycleRequest, CycleRequest]) -> None:
        calls.append(requests)
        entered.set()
        assert release.wait(2.0)
        publish_pair(deps)(requests)

    context = PreviewContext("preview-a", MARKET_DATE, STARTED_AT)
    runner = PreviewCycleRunner(deps, execute, request_builder)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(runner.execute, context, None)
        assert entered.wait(2.0)
        assert runner.status().state == "running"
        runner.execute(context, None)
        release.set()
        future.result(timeout=2.0)
    assert runner.status().state == "ready" and len(calls) == 1


def test_preview_refresh_latency_uses_milliseconds() -> None:
    data = PreviewData()
    ticks = iter((1.0, 1.25, 2.0, 2.25, 3.0, 3.25, 4.0, 4.25))
    deps = replace(dependencies(data), monotonic=lambda: next(ticks))
    runner = PreviewCycleRunner(deps, publish_pair(deps), request_builder)
    runner.execute(PreviewContext("preview-a", MARKET_DATE, STARTED_AT), None)
    assert runner.status().state == "ready"
    assert deps.latency.status().stages["preview:full_market"].p50_ms == 250.0
