from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event, RLock

import pytest

from tests.integration.test_scheduler_runtime import (
    NOW,
    DataRefresh,
    FixedClock,
    Freezes,
    Sealing,
    Settlement,
    SharedReviews,
    TradingCalendar,
    _cadence,
    _round_cleanup_runtime,
    noop_research_factory,
)
from trader.infra.shutdown import ShutdownDeadline
from trader.infra.workers import BoundedExecutor
from trader.recommendation.application.pipeline.freeze_publish.freeze_observers import AsyncFreezeObserver
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedFreezeIndex
from trader.recommendation.application.ports.runtime import CycleRequest
from trader.recommendation.application.runtime.latest_wins import LatestWinsOffer
from trader.recommendation.application.runtime.cadence import PipelineTask, ScheduledPipelineTask
from trader.recommendation.application.runtime.recommendation_round import RecommendationRounds
from trader.recommendation.application.runtime.schedule import MarketPhase
from trader.recommendation.application.runtime.runtime_dependencies import RuntimeDependencies
from trader.recommendation.application.runtime.scheduler_runtime import SchedulerRuntime
from trader.recommendation.domain.publication.models import Strategy


@pytest.mark.parametrize("jump", (-1, 60, 86400))
def test_round_rechecks_session_after_preparing_overlays(monkeypatch, jump):
    runtime = _round_cleanup_runtime()
    deps = runtime._dependencies
    requests = tuple(
        replace(runtime._scheduled_request(strategy, NOW, "before_close"), allow_review=False)
        for strategy in (Strategy.TOMORROW, Strategy.D25)
    )
    admission = deps.rounds.enter(
        deps.round_profile, (ScheduledPipelineTask(PipelineTask.SCORE, NOW, MarketPhase.BEFORE_CLOSE),)
    )
    assert admission is not None
    original = deps.freezes.initial_overlay

    def rotate_session(identity):
        overlay = original(identity)
        if identity.strategy is Strategy.D25:
            deps.clock.current = NOW + timedelta(seconds=jump)
            runtime._session.capture()
        return overlay

    monkeypatch.setattr(deps.freezes, "initial_overlay", rotate_session)
    runtime._cycles.execute_round_pair(requests, round_sequence=admission.sequence)
    assert runtime._session.status().generation == 2
    assert all(deps.index.snapshot(strategy).current is None for strategy in (Strategy.TOMORROW, Strategy.D25))
    assert runtime.status().local_publish_count == 0
    deps.rounds.finish(deps.round_profile, admission.sequence)


@pytest.mark.parametrize("invalid_profile", (None, "v1", "v5"))
def test_batch_checks_every_session_after_waiting_for_other_profiles(invalid_profile):
    events = []
    rounds = RecommendationRounds(publish_batch_event=lambda sequence, batch: events.append((sequence, batch)))
    transaction = RLock()
    template = _round_cleanup_runtime()._dependencies
    runtimes = {
        profile: SchedulerRuntime(
            replace(template, clock=FixedClock(NOW), rounds=rounds, round_profile=profile,
                    index=UnifiedFreezeIndex(transaction_lock=transaction)),
            config_version="session-batch", shutdown_timeout_seconds=2.0,
        )
        for profile in ("v1", "v5")
    }
    requests = {
        profile: tuple(
            replace(runtime._scheduled_request(strategy, NOW, "before_close"), allow_review=False)
            for strategy in (Strategy.TOMORROW, Strategy.D25)
        )
        for profile, runtime in runtimes.items()
    }
    tasks = (ScheduledPipelineTask(PipelineTask.SCORE, NOW, MarketPhase.BEFORE_CLOSE),)
    admission = rounds.enter("v1", tasks)
    assert admission is not None and rounds.enter("v5", tasks) is admission
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(runtimes["v1"]._cycles.execute_round_pair, requests["v1"], round_sequence=admission.sequence)
        with rounds._condition:
            assert rounds._condition.wait_for(lambda: "v1" in rounds._staged, timeout=2.0)
        if invalid_profile is not None:
            runtime = runtimes[invalid_profile]
            runtime._dependencies.clock.current -= timedelta(seconds=1)
            runtime._session.capture()
        runtimes["v5"]._cycles.execute_round_pair(requests["v5"], round_sequence=admission.sequence)
        rounds.finish("v5", admission.sequence)
        first.result(timeout=2.0)
    assert len(events) == (1 if invalid_profile is None else 0)
    for profile, runtime in runtimes.items():
        for strategy in (Strategy.TOMORROW, Strategy.D25):
            assert (runtime._dependencies.index.snapshot(strategy).current is not None) == (invalid_profile is None)
        rounds.finish(profile, admission.sequence)


@pytest.mark.parametrize("jump", (-60, 60, 301, 86400))
@pytest.mark.parametrize("stage", ("local", "hybrid"))
def test_old_inflight_result_cannot_replace_visible_snapshot_after_clock_jump(jump, stage):
    started, release = Event(), Event()
    clock = FixedClock(NOW)
    elapsed = [10.0]

    class BlockingFreezes(Freezes):
        def build_local(self, request):
            result = super().build_local(request)
            if stage == "local" and request.sequence == 1:
                started.set()
                assert release.wait(5)
            return result

    class BlockingReviews(SharedReviews):
        def build_hybrid(self, local, request):
            result = super().build_hybrid(local, request)
            started.set()
            assert release.wait(5)
            return result

    index = UnifiedFreezeIndex()
    events = []
    runtime = SchedulerRuntime(
        RuntimeDependencies(
            control_pool=BoundedExecutor(
                worker_count=2, urgent_worker_count=1, queue_capacity=4, thread_name_prefix="session-test"
            ),
            clock=clock,
            monotonic=lambda: elapsed[0],
            calendar=TradingCalendar(),
            cadence=_cadence(NOW),
            data=DataRefresh(),
            freezes=BlockingFreezes(),
            reviews=BlockingReviews(),
            index=index,
            observer=AsyncFreezeObserver((), capacity=4, thread_name="session-observer"),
            sealing=Sealing(),
            settlement=Settlement(),
            research_factory=noop_research_factory,
            publish_freeze=events.append,
            publish_overlay=lambda _overlay: None,
        ),
        config_version="test",
        shutdown_timeout_seconds=2.0,
    )
    request = CycleRequest(
        Strategy.TOMORROW, NOW.date(), NOW, "afternoon", 1, "old", stage == "hybrid", NOW + timedelta(minutes=1)
    )
    runtime.start()
    try:
        runtime.submit_cycle(request)
        assert started.wait(3)
        before = index.snapshot(Strategy.TOMORROW).current
        clock.current += timedelta(seconds=jump)
        elapsed[0] += jump if jump > 300 else 1
        release.set()
        assert runtime.wait_idle(3)
        assert index.snapshot(Strategy.TOMORROW).current == before
        assert runtime.status().session.generation == 2
        new = replace(
            request,
            trade_date=clock.current.date(),
            observed_at=clock.current,
            sequence=2,
            input_version="new",
            allow_review=False,
        )
        runtime.submit_cycle(new)
        assert runtime.wait_idle(3)
        current = index.snapshot(Strategy.TOMORROW).current
        assert current is not None and current.sequence == 2 and current.trade_date == clock.current.date()
        if jump == 86400:
            assert runtime.submit_cycle(replace(request, sequence=3)) is LatestWinsOffer.REJECTED
        assert len(events) == (2 if stage == "hybrid" else 1)
    finally:
        release.set()
        runtime.stop(ShutdownDeadline.start(3))
