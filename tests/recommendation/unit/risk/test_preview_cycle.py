from __future__ import annotations

import threading


from concurrent.futures import ThreadPoolExecutor

from dataclasses import replace

from datetime import timedelta

import pytest


from tests.helpers.recommendation.publication.freeze_identity import freeze

from tests.helpers.recommendation.publication.preview_identity import MARKET_DATE, STARTED_AT, preview_freeze


from trader.recommendation.application.pipeline.data_source.input_assembly import TaskDeadline



from trader.recommendation.application.ports.runtime import CycleRequest, DataRefreshPendingError, DataRefreshUnavailableError, PipelineTaskRequest

from trader.recommendation.application.runtime.cadence import PipelineTask

from trader.recommendation.application.runtime.preview_cycle import PreviewCycleRunner

from trader.recommendation.application.runtime.recommendation_round import RecommendationRounds



from trader.recommendation.application.runtime.trading_session import TradingSession

from trader.recommendation.domain.publication.models import Strategy

from trader.recommendation.domain.publication.preview import PreviewContext

from tests.helpers.recommendation.risk.preview_cycle import (
    PreviewData,
    _scheduled,
    dependencies,
    publish_pair,
    request_builder,
)


@pytest.mark.parametrize(
    "task,live_seconds,preview_seconds",
    (
        (PipelineTask.FULL_MARKET, 20, 180),
        (PipelineTask.CANDIDATE_QUOTES, 15, 180),
        (PipelineTask.INTRADAY_TAIL, 3, 3),
        (PipelineTask.SCORE, 15, 15),
        (PipelineTask.LONG_QUOTES, 3, 3),
    ),
)
def test_preview_close_collection_has_a_bounded_budget_independent_of_live_refresh(task, live_seconds, preview_seconds):
    request = PipelineTaskRequest(task, STARTED_AT)
    context = PreviewContext("collection-budget", MARKET_DATE, STARTED_AT)
    assert TaskDeadline(request) == STARTED_AT + timedelta(seconds=live_seconds)
    assert TaskDeadline(replace(request, preview_context=context)) == STARTED_AT + timedelta(seconds=preview_seconds)


def test_preview_runner_refreshes_fresh_inputs_and_executes_both_strategies_once() -> None:
    data = PreviewData()
    deps = dependencies(data)
    session = TradingSession(deps.clock.now, deps.monotonic).capture()
    context = PreviewContext("preview-a", MARKET_DATE, STARTED_AT)
    calls: list[tuple[CycleRequest, CycleRequest]] = []

    def execute(requests: tuple[CycleRequest, CycleRequest], sequence: int) -> None:
        calls.append(requests)
        publish_pair(deps)(requests, sequence)

    runner = PreviewCycleRunner(deps, execute, request_builder)
    deps.rounds.register(deps.round_profile)
    scheduled = _scheduled(deps, session)
    runner.execute(context, scheduled)
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
    assert all(request.round_deadline == scheduled.round_deadline for request in data.preview_requests)
    assert all(request.round_deadline_monotonic == scheduled.round_deadline_monotonic for request in calls[0])
    runner.execute(context, scheduled)
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

    def execute(requests: tuple[CycleRequest, CycleRequest], sequence: int) -> None:
        calls.append(requests)
        publish_pair(deps)(requests, sequence)

    runner = PreviewCycleRunner(deps, execute, request_builder)
    deps.rounds.register(deps.round_profile)
    scheduled = _scheduled(deps)
    runner.execute(context, scheduled)
    assert runner.status().state == "waiting_inputs"
    assert runner.status().error == "history_projection_pending"
    assert not calls and deps.index.snapshot(Strategy.TOMORROW).current is None
    deps.rounds.finish(deps.round_profile, scheduled.round_sequence)
    data.failure = None
    runner.execute(context, _scheduled(deps))
    assert runner.status().state == "ready" and len(calls) == 1


@pytest.mark.parametrize(
    "failure", (DataRefreshUnavailableError("close_quote_unavailable"), OSError("unsupported input [details]"))
)
def test_failed_preview_retains_old_currents_and_does_not_repeat(failure: Exception) -> None:
    data = PreviewData()
    data.failure_task, data.failure = PipelineTask.CANDIDATE_QUOTES, failure
    deps = dependencies(data)
    old = freeze()
    assert deps.index.publish(old, expected_version=None).accepted
    runner = PreviewCycleRunner(deps, lambda _requests, _sequence: pytest.fail("failed inputs scored"), request_builder)
    context = PreviewContext("preview-a", MARKET_DATE, STARTED_AT)
    deps.rounds.register(deps.round_profile)
    scheduled = _scheduled(deps)
    runner.execute(context, scheduled)
    assert runner.status().state == "failed"
    assert runner.status().error in {"close_quote_unavailable", "preview_run_failed"}
    assert deps.index.snapshot(Strategy.TOMORROW).current is old
    count = len(data.preview_requests)
    runner.execute(context, scheduled)
    assert len(data.preview_requests) == count


def test_preview_is_ready_only_when_both_current_freezes_belong_to_the_run() -> None:
    deps = dependencies(PreviewData())
    other = preview_freeze(run_id="another-run", strategy=Strategy.D25)
    assert deps.index.publish(other, expected_version=None).accepted

    def execute(_requests: tuple[CycleRequest, CycleRequest], _sequence: int) -> None:
        assert deps.index.publish(preview_freeze(), expected_version=None).accepted

    runner = PreviewCycleRunner(deps, execute, request_builder)
    deps.rounds.register(deps.round_profile)
    runner.execute(PreviewContext("preview-a", MARKET_DATE, STARTED_AT), _scheduled(deps))
    assert runner.status().state == "failed"
    assert runner.status().error == "preview_freeze_unavailable"


def test_running_preview_claim_rejects_duplicate_concurrent_execution() -> None:
    deps = dependencies(PreviewData())
    entered, release = threading.Event(), threading.Event()
    calls: list[tuple[CycleRequest, CycleRequest]] = []

    def execute(requests: tuple[CycleRequest, CycleRequest], sequence: int) -> None:
        calls.append(requests)
        entered.set()
        assert release.wait(2.0)
        publish_pair(deps)(requests, sequence)

    context = PreviewContext("preview-a", MARKET_DATE, STARTED_AT)
    runner = PreviewCycleRunner(deps, execute, request_builder)
    deps.rounds.register(deps.round_profile)
    scheduled = _scheduled(deps)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(runner.execute, context, scheduled)
        assert entered.wait(2.0)
        assert runner.status().state == "running"
        runner.execute(context, scheduled)
        release.set()
        future.result(timeout=2.0)
    assert runner.status().state == "ready" and len(calls) == 1


def test_preview_refresh_latency_uses_milliseconds() -> None:
    data = PreviewData()
    ticks = iter((1.0, 1.25, 2.0, 2.25, 3.0, 3.25, 4.0, 4.25))
    deps = replace(dependencies(data), monotonic=lambda: next(ticks))
    runner = PreviewCycleRunner(deps, publish_pair(deps), request_builder)
    deps.rounds.register(deps.round_profile)
    runner.execute(PreviewContext("preview-a", MARKET_DATE, STARTED_AT), _scheduled(deps))
    assert runner.status().state == "ready"
    assert deps.latency.status().stages["preview:full_market"].p50_ms == 250.0


@pytest.mark.parametrize("expire_at", ("queue", "prepare", "full_market"))
def test_preview_stops_dispatching_when_the_shared_deadline_expires(expire_at):
    elapsed = [0.0]

    class SlowInputs(PreviewData):
        def prepare_preview(self, context):
            super().prepare_preview(context)
            if expire_at == "prepare":
                elapsed[0] = 120.0

        def refresh_task(self, request):
            result = super().refresh_task(request)
            elapsed[0] = 120.0
            return result

    data = SlowInputs()
    deps = replace(dependencies(data), rounds=RecommendationRounds(monotonic=lambda: elapsed[0]))
    deps.rounds.register(deps.round_profile)
    scheduled = _scheduled(deps)
    runner = PreviewCycleRunner(deps, lambda _requests, _sequence: pytest.fail("expired preview scored"), request_builder)
    if expire_at == "queue":
        elapsed[0] = 120.0
    runner.execute(PreviewContext("preview-a", MARKET_DATE, STARTED_AT), scheduled)
    assert runner.status().state == "failed"
    assert runner.status().error == "preview_round_cancelled"
    assert len(data.preview_requests) == (1 if expire_at == "full_market" else 0)
    assert deps.index.snapshot(Strategy.TOMORROW).current is None
    assert deps.index.snapshot(Strategy.D25).current is None
