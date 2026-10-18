"""FIFO admission and pacing for the single CNInfo HTTP request lane."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from threading import Condition


class CninfoDeadlineExceeded(TimeoutError):
    """The local wave budget expired; no supplier failure is implied."""


class CninfoRequestGate:
    def __init__(
        self,
        *,
        minimum_interval_seconds: float,
        monotonic: Callable[[], float],
        sleeper: Callable[[float], None],
    ) -> None:
        self._minimum_interval_seconds = minimum_interval_seconds
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._condition = Condition()
        self._waiting: deque[int] = deque()
        self._next_ticket = 0
        self._next_request_at = 0.0

    @contextmanager
    def enter(self, deadline: float) -> Iterator[float]:
        with self._condition:
            ticket = self._next_ticket
            self._next_ticket += 1
            self._waiting.append(ticket)
        try:
            with self._condition:
                while self._waiting[0] != ticket:
                    remaining = deadline - self._monotonic()
                    if remaining <= 0.0:
                        raise CninfoDeadlineExceeded("CNInfo queue reached wave deadline")
                    self._condition.wait(timeout=remaining)
            delay = max(0.0, self._next_request_at - self._monotonic())
            if delay >= deadline - self._monotonic():
                raise CninfoDeadlineExceeded("CNInfo pacing reached wave deadline")
            if delay:
                self._sleeper(delay)
            started = self._monotonic()
            if started >= deadline:
                raise CninfoDeadlineExceeded("CNInfo request reached wave deadline")
            self._next_request_at = started + self._minimum_interval_seconds
            yield deadline - started
        finally:
            with self._condition:
                self._waiting.remove(ticket)
                self._condition.notify_all()


__all__ = ["CninfoDeadlineExceeded", "CninfoRequestGate"]
