from __future__ import annotations

import threading

from trader.application.shutdown import ShutdownDeadline
from trader.application.v2_lifecycle import LatestWinsOffer, LatestWinsWorker


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
