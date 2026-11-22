from __future__ import annotations

import gc
import threading
import weakref

from tests.helpers.recommendation.publication.freeze_identity import freeze
from trader.infra.shutdown import ShutdownDeadline
from trader.recommendation.application.pipeline.freeze_publish.freeze_events import (
    BuildFreezeCommitted,
    FreezeObservation,
)
from trader.recommendation.application.pipeline.freeze_publish.freeze_observers import AsyncFreezeObserver


def test_observer_is_bounded_non_blocking_and_isolates_consumer_failure() -> None:
    entered = threading.Event()
    release = threading.Event()
    handled: list[str] = []

    def blocking_consumer(observation) -> None:
        entered.set()
        release.wait(timeout=1.0)
        handled.append(observation.event.event_id)

    def failing_consumer(_event) -> None:
        raise RuntimeError("research failed")

    observer = AsyncFreezeObserver(
        (blocking_consumer, failing_consumer),
        capacity=1,
        thread_name="test-observer",
    )
    event = BuildFreezeCommitted(freeze())
    observation = FreezeObservation(event, None)
    observer.start()
    assert observer.offer(observation)
    assert entered.wait(timeout=1.0)
    assert observer.offer(observation)
    assert observer.offer(observation) is False
    release.set()

    assert observer.wait_idle(1.0)
    status = observer.status()
    assert handled == [event.event_id, event.event_id]
    assert status.accepted_count == 2
    assert status.rejected_count == 1
    assert status.consumer_failure_count == 2
    assert observer.stop(deadline=ShutdownDeadline.start(1.0)).completed


def test_observer_shutdown_does_not_reset_the_shared_deadline() -> None:
    entered = threading.Event()
    release = threading.Event()

    def blocked(_event) -> None:
        entered.set()
        release.wait()

    observer = AsyncFreezeObserver((blocked,), capacity=1, thread_name="test-observer-deadline")
    observer.start()
    observer.offer(FreezeObservation(BuildFreezeCommitted(freeze()), None))
    assert entered.wait(timeout=1.0)

    try:
        step = observer.stop(deadline=ShutdownDeadline.start(0.02))
        assert step.completed is False
        assert step.timed_out is True
    finally:
        release.set()
        observer.stop(deadline=ShutdownDeadline.start(1.0))


def test_idle_observer_releases_the_processed_observation() -> None:
    handled: list[str] = []

    def consume(observation: FreezeObservation) -> None:
        handled.append(observation.event.event_id)

    observer = AsyncFreezeObserver((consume,), capacity=1)
    observation = FreezeObservation(BuildFreezeCommitted(freeze()), None)
    event_id = observation.event.event_id
    reference = weakref.ref(observation)
    observer.start()
    try:
        assert observer.offer(observation)
        del observation
        assert observer.wait_idle(1.0)
        gc.collect()
        assert handled == [event_id]
        assert observer.status().thread_alive
        assert reference() is None
    finally:
        assert observer.stop(deadline=ShutdownDeadline.start(1.0)).completed
