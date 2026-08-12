"""Bounded asynchronous delivery of generic V2 decision events."""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from trader.application.research_audit import V2DecisionObservation
from trader.application.shutdown import ShutdownDeadline, ShutdownStep

DecisionEventConsumer = Callable[[V2DecisionObservation], None]


@dataclass(frozen=True)
class DecisionObserverStatus:
    capacity: int
    accepting: bool
    thread_alive: bool
    running: bool
    depth: int
    accepted_count: int
    rejected_count: int
    completed_count: int
    consumer_failure_count: int
    last_error_code: str


class DecisionObserver(Protocol):
    def offer(self, observation: V2DecisionObservation) -> bool: ...


class DecisionObserverRuntime(DecisionObserver, Protocol):
    def start(self) -> bool: ...

    def close(self) -> None: ...

    def stop(self, *, deadline: ShutdownDeadline) -> ShutdownStep: ...

    def wait_idle(self, timeout_seconds: float) -> bool: ...

    def status(self) -> DecisionObserverStatus: ...


class AsyncDecisionObserver:
    """Keeps research consumers outside publication and freeze capacity."""

    def __init__(
        self,
        consumers: tuple[DecisionEventConsumer, ...],
        *,
        capacity: int,
        thread_name: str = "trader-v2-observer",
    ) -> None:
        if capacity < 1:
            raise ValueError("decision observer capacity must be positive")
        self._consumers = tuple(consumers)
        self._capacity = capacity
        self._thread_name = thread_name
        self._condition = threading.Condition(threading.RLock())
        self._queue: deque[V2DecisionObservation] = deque()
        self._thread: threading.Thread | None = None
        self._accepting = False
        self._running = False
        self._accepted_count = 0
        self._rejected_count = 0
        self._completed_count = 0
        self._consumer_failure_count = 0
        self._last_error_code = ""

    def start(self) -> bool:
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                return False
            if self._thread is not None or self._accepting:
                raise RuntimeError("decision observer cannot restart after stop")
            self._accepting = True
            thread = threading.Thread(target=self._run, name=self._thread_name, daemon=False)
            self._thread = thread
            try:
                thread.start()
            except BaseException:
                self._accepting = False
                self._thread = None
                raise
            return True

    def offer(self, observation: V2DecisionObservation) -> bool:
        with self._condition:
            if not self._accepting or len(self._queue) >= self._capacity:
                self._rejected_count += 1
                return False
            self._queue.append(observation)
            self._accepted_count += 1
            self._condition.notify_all()
            return True

    def close(self) -> None:
        with self._condition:
            self._accepting = False
            self._condition.notify_all()

    def stop(self, *, deadline: ShutdownDeadline) -> ShutdownStep:
        self.close()
        with self._condition:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(deadline.remaining_seconds())
        alive = thread is not None and thread.is_alive()
        return ShutdownStep(
            name=self._thread_name,
            completed=not alive,
            timed_out=alive and deadline.expired,
            detail="decision observer remains active at shutdown deadline" if alive else "",
        )

    def wait_idle(self, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        with self._condition:
            while self._running or self._queue:
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    return False
                self._condition.wait(remaining)
            return True

    def status(self) -> DecisionObserverStatus:
        with self._condition:
            thread = self._thread
            return DecisionObserverStatus(
                capacity=self._capacity,
                accepting=self._accepting,
                thread_alive=bool(thread is not None and thread.is_alive()),
                running=self._running,
                depth=len(self._queue),
                accepted_count=self._accepted_count,
                rejected_count=self._rejected_count,
                completed_count=self._completed_count,
                consumer_failure_count=self._consumer_failure_count,
                last_error_code=self._last_error_code,
            )

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._queue and self._accepting:
                    self._condition.wait()
                if not self._queue:
                    return
                event = self._queue.popleft()
                self._running = True
            for consumer in self._consumers:
                try:
                    consumer(event)
                except Exception as exc:
                    with self._condition:
                        self._consumer_failure_count += 1
                        self._last_error_code = type(exc).__name__
            with self._condition:
                self._running = False
                self._completed_count += 1
                self._condition.notify_all()


__all__ = [
    "AsyncDecisionObserver",
    "DecisionEventConsumer",
    "DecisionObserver",
    "DecisionObserverRuntime",
    "DecisionObserverStatus",
]
