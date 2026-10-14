"""Clock continuity and atomic admission of results from a trading session."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime

from trader.recommendation.application.runtime.schedule import shanghai_now


class StaleSessionError(RuntimeError):
    """Work completed after its trading session lost clock continuity."""


@dataclass(frozen=True)
class TradingSessionStatus:
    generation: int
    trade_date: date | None
    discontinuity_reason: str


@dataclass(frozen=True)
class SessionLease:
    generation: int
    trade_date: date
    _owner: TradingSession = field(repr=False, compare=False)

    @contextmanager
    def guard(self) -> Iterator[None]:
        with self._owner.admit(self):
            yield

    def remember(self, version: str) -> None:
        with self.guard():
            self._owner.remember(version)

    def allows_version(self, version: str) -> bool:
        with self.guard():
            return self.generation == 1 or self._owner.has_version(version)


class TradingSession:
    def __init__(self, now: Callable[[], datetime], monotonic: Callable[[], float]) -> None:
        self._now = now
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._wall: datetime | None = None
        self._elapsed: float | None = None
        self._generation = 0
        self._reason = "startup"
        self._versions: tuple[str, ...] = ()

    def capture(self) -> SessionLease:
        with self._lock:
            at = shanghai_now(self._now())
            elapsed = self._monotonic()
            reason = self._discontinuity(at, elapsed)
            if reason:
                self._generation += 1
                self._reason = reason
                self._versions = ()
            self._wall, self._elapsed = at, elapsed
            return SessionLease(self._generation, at.date(), self)

    def _discontinuity(self, at: datetime, elapsed: float) -> str:
        if self._wall is None or self._elapsed is None:
            return "startup"
        wall_delta = (at - self._wall).total_seconds()
        monotonic_delta = elapsed - self._elapsed
        if at.date() != self._wall.date():
            return "trade_date_changed"
        if wall_delta < 0:
            return "wall_clock_rollback"
        if monotonic_delta < 0 or abs(wall_delta - monotonic_delta) > 5.0:
            return "clock_divergence"
        if max(wall_delta, monotonic_delta) > 300.0:
            return "resume_after_suspend"
        return ""

    @contextmanager
    def admit(self, lease: SessionLease) -> Iterator[None]:
        with self._lock:
            current = self.capture()
            if lease.generation != current.generation or lease.trade_date != current.trade_date:
                raise StaleSessionError("stale_session_generation")
            yield

    def status(self) -> TradingSessionStatus:
        with self._lock:
            return TradingSessionStatus(
                self._generation, self._wall.date() if self._wall is not None else None, self._reason
            )

    def remember(self, version: str) -> None:
        with self._lock:
            self._versions = (*self._versions[-63:], version)

    def has_version(self, version: str) -> bool:
        with self._lock:
            return version in self._versions


@contextmanager
def session_guard(lease: SessionLease | None) -> Iterator[None]:
    """Unscheduled use cases have no runtime lease; SchedulerRuntime always stamps one."""
    if lease is None:
        yield
    else:
        with lease.guard():
            yield


__all__ = ["SessionLease", "StaleSessionError", "TradingSession", "TradingSessionStatus", "session_guard"]
