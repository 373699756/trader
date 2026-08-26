from __future__ import annotations

import threading

from trader.application.latency import LatencyWaterfall
from trader.application.shutdown import ShutdownDeadline
from trader.application.v2_lifecycle import LatestWinsOffer, LatestWinsTelemetry, LatestWinsWorker


def test_latest_wins_keeps_running_item_and_only_the_newest_pending_item() -> None:
    entered = threading.Event()
    release = threading.Event()
    processed: list[int] = []

    def process(value: int) -> None:
        if value == 1:
            entered.set()
            release.wait(timeout=1.0)
        processed.append(value)

    worker = LatestWinsWorker("test-v2-latest", process, order_key=lambda value: value)
    assert worker.start()
    assert worker.offer(1) is LatestWinsOffer.ACCEPTED
    assert entered.wait(timeout=1.0)
    assert worker.offer(1) is LatestWinsOffer.COALESCED
    assert worker.offer(2) is LatestWinsOffer.ACCEPTED
    assert worker.offer(3) is LatestWinsOffer.REPLACED
    assert worker.offer(2) is LatestWinsOffer.STALE
    release.set()

    assert worker.wait_idle(1.0)
    status = worker.status()
    assert processed == [1, 3]
    assert status.replaced_count == 1
    assert status.coalesced_count == 1
    assert status.stale_count == 1
    assert status.pending is False
    assert worker.stop(deadline=ShutdownDeadline.start(1.0)).completed
    assert not any(thread.name == "test-v2-latest" for thread in threading.enumerate())


def test_latest_wins_stop_uses_shared_deadline_and_cancels_pending() -> None:
    entered = threading.Event()
    release = threading.Event()

    def block(_value: int) -> None:
        entered.set()
        release.wait()

    worker = LatestWinsWorker("test-v2-deadline", block, order_key=lambda value: value)
    worker.start()
    worker.offer(1)
    assert entered.wait(timeout=1.0)
    worker.offer(2)

    try:
        step = worker.stop(deadline=ShutdownDeadline.start(0.02))
        assert step.completed is False
        assert step.timed_out is True
        assert step.cancelled_count == 1
        assert worker.offer(3) is LatestWinsOffer.REJECTED
    finally:
        release.set()
        worker.stop(deadline=ShutdownDeadline.start(1.0))


def test_latest_wins_exposes_running_supersession_and_records_bounded_waterfall() -> None:
    entered = threading.Event()
    release = threading.Event()
    latency = LatencyWaterfall()

    def process(value: int) -> None:
        entered.set()
        if value == 1:
            release.wait(timeout=1.0)

    worker = LatestWinsWorker(
        "test-v2-waterfall",
        process,
        order_key=lambda value: value,
        telemetry=LatestWinsTelemetry(
            latency,
            lambda value: f"score:{value}",
            lambda _value: "score:tomorrow",
        ),
    )
    worker.start()
    worker.offer(1)
    assert entered.wait(timeout=1.0)
    worker.offer(2)

    assert worker.is_superseded(1) is True
    published: list[int] = []
    assert worker.execute_if_current(1, lambda: bool(published.append(1))) is False
    assert published == []
    release.set()
    assert worker.wait_idle(1.0)
    worker.stop(deadline=ShutdownDeadline.start(1.0))

    status = latency.status()
    assert status.completed_count == 1
    assert status.superseded_count == 1
    assert status.stages["queue_wait:score:tomorrow"].sample_count == 2
