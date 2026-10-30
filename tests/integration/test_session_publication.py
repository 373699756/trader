from dataclasses import replace
from datetime import timedelta
from threading import Event

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
    noop_research_factory,
)
from trader.infra.shutdown import ShutdownDeadline
from trader.infra.workers import BoundedExecutor
from trader.recommendation.application.pipeline.freeze_publish.freeze_observers import AsyncFreezeObserver
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedFreezeIndex
from trader.recommendation.application.ports.runtime import CycleRequest
from trader.recommendation.application.runtime.latest_wins import LatestWinsOffer
from trader.recommendation.application.runtime.runtime_dependencies import RuntimeDependencies
from trader.recommendation.application.runtime.scheduler_runtime import SchedulerRuntime
from trader.recommendation.domain.publication.models import Strategy


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
