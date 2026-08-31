from __future__ import annotations

import threading

from tests.unit.domain.test_decision_identity import decision
from trader.application.decisions.decision_events import build_v2_decision_committed
from trader.application.decisions.decision_observers import AsyncDecisionObserver
from trader.application.research_audit import V2DecisionObservation
from trader.application.runtime.shutdown import ShutdownDeadline


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

    observer = AsyncDecisionObserver(
        (blocking_consumer, failing_consumer),
        capacity=1,
        thread_name="test-v2-observer",
    )
    event = build_v2_decision_committed(decision())
    observation = V2DecisionObservation(event, None)
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

    observer = AsyncDecisionObserver((blocked,), capacity=1, thread_name="test-v2-observer-deadline")
    observer.start()
    observer.offer(V2DecisionObservation(build_v2_decision_committed(decision()), None))
    assert entered.wait(timeout=1.0)

    try:
        step = observer.stop(deadline=ShutdownDeadline.start(0.02))
        assert step.completed is False
        assert step.timed_out is True
    finally:
        release.set()
        observer.stop(deadline=ShutdownDeadline.start(1.0))
