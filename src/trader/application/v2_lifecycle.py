"""Single-running, single-pending latest-wins lifecycle worker."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Generic, TypeVar

from trader.application.latency import LatencyWaterfall
from trader.application.shutdown import ShutdownDeadline, ShutdownStep

_T = TypeVar("_T")


class LatestWinsOffer(str, Enum):
    ACCEPTED = "accepted"
    REPLACED = "replaced"
    COALESCED = "coalesced"
    STALE = "stale"
    REJECTED = "rejected"


@dataclass(frozen=True)
class LatestWinsStatus:
    name: str
    accepting: bool
    thread_alive: bool
    running: bool
    pending: bool
    offered_count: int
    completed_count: int
    failed_count: int
    replaced_count: int
    coalesced_count: int
    stale_count: int
    rejected_count: int
    cancelled_count: int
    last_error_code: str


@dataclass(frozen=True)
class LatestWinsTelemetry(Generic[_T]):
    latency: LatencyWaterfall
    correlation_id: Callable[[_T], str]
    cycle_kind: Callable[[_T], str]


class LatestWinsWorker(Generic[_T]):
    """Allows one running item to finish while retaining only the newest pending item."""

    def __init__(
        self,
        name: str,
        processor: Callable[[_T], None],
        *,
        order_key: Callable[[_T], int],
        telemetry: LatestWinsTelemetry[_T] | None = None,
    ) -> None:
        if not name:
            raise ValueError("latest-wins worker name must not be empty")
        self._name = name
        self._processor = processor
        self._order_key = order_key
        self._telemetry = telemetry
        self._condition = threading.Condition(threading.RLock())
        self._thread: threading.Thread | None = None
        self._accepting = False
        self._running = False
        self._running_key: int | None = None
        self._pending: _T | None = None
        self._offered_count = 0
        self._completed_count = 0
        self._failed_count = 0
        self._replaced_count = 0
        self._coalesced_count = 0
        self._stale_count = 0
        self._rejected_count = 0
        self._cancelled_count = 0
        self._last_error_code = ""

    def start(self) -> bool:
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                return False
            if self._thread is not None or self._accepting:
                raise RuntimeError("latest-wins worker cannot restart after stop")
            self._accepting = True
            thread = threading.Thread(target=self._run, name=self._name, daemon=False)
            self._thread = thread
            try:
                thread.start()
            except BaseException:
                self._accepting = False
                self._thread = None
                raise
            return True

    def offer(self, item: _T) -> LatestWinsOffer:
        key = self._order_key(item)
        with self._condition:
            self._offered_count += 1
            if not self._accepting:
                self._rejected_count += 1
                return LatestWinsOffer.REJECTED
            newest_key = self._order_key(self._pending) if self._pending is not None else self._running_key
            if newest_key is not None and key < newest_key:
                self._stale_count += 1
                return LatestWinsOffer.STALE
            if newest_key is not None and key == newest_key:
                self._coalesced_count += 1
                return LatestWinsOffer.COALESCED
            replaced = self._pending is not None
            if replaced:
                self._finish_latency(self._pending, "superseded")
            self._pending = item
            if replaced:
                self._replaced_count += 1
            self._plan_latency(item)
            self._condition.notify_all()
            return LatestWinsOffer.REPLACED if replaced else LatestWinsOffer.ACCEPTED

    def is_superseded(self, item: _T) -> bool:
        key = self._order_key(item)
        with self._condition:
            return self._pending is not None and self._order_key(self._pending) > key

    def execute_if_current(self, item: _T, action: Callable[[], bool]) -> bool:
        """Linearize a short publication action before a newer offer can be accepted."""

        key = self._order_key(item)
        with self._condition:
            if self._pending is not None and self._order_key(self._pending) > key:
                return False
            return action()

    def close(self) -> int:
        with self._condition:
            self._accepting = False
            cancelled = int(self._pending is not None)
            self._finish_latency(self._pending, "dropped")
            self._pending = None
            self._cancelled_count += cancelled
            self._condition.notify_all()
            return cancelled

    def stop(self, *, deadline: ShutdownDeadline) -> ShutdownStep:
        self.close()
        with self._condition:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(deadline.remaining_seconds())
        alive = thread is not None and thread.is_alive()
        return ShutdownStep(
            name=self._name,
            completed=not alive,
            timed_out=alive and deadline.expired,
            cancelled_count=self.status().cancelled_count,
            detail="latest-wins worker remains active at shutdown deadline" if alive else "",
        )

    def wait_idle(self, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        with self._condition:
            while self._running or self._pending is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    return False
                self._condition.wait(remaining)
            return True

    def status(self) -> LatestWinsStatus:
        with self._condition:
            thread = self._thread
            return LatestWinsStatus(
                name=self._name,
                accepting=self._accepting,
                thread_alive=bool(thread is not None and thread.is_alive()),
                running=self._running,
                pending=self._pending is not None,
                offered_count=self._offered_count,
                completed_count=self._completed_count,
                failed_count=self._failed_count,
                replaced_count=self._replaced_count,
                coalesced_count=self._coalesced_count,
                stale_count=self._stale_count,
                rejected_count=self._rejected_count,
                cancelled_count=self._cancelled_count,
                last_error_code=self._last_error_code,
            )

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._pending is None and self._accepting:
                    self._condition.wait()
                if self._pending is None:
                    return
                item = self._pending
                self._pending = None
                self._running = True
                self._running_key = self._order_key(item)
                self._enter_latency(item)
            failed = False
            try:
                self._processor(item)
            except Exception as exc:
                failed = True
                with self._condition:
                    self._last_error_code = type(exc).__name__
            finally:
                with self._condition:
                    superseded = self._pending is not None and self._order_key(self._pending) > self._order_key(item)
                    self._running = False
                    self._running_key = None
                    self._completed_count += int(not failed)
                    self._failed_count += int(failed)
                    outcome = "failed" if failed else "superseded" if superseded else "success"
                    self._finish_latency(item, outcome)
                    self._condition.notify_all()

    def _plan_latency(self, item: _T) -> None:
        if self._telemetry is not None:
            self._telemetry.latency.plan(
                self._telemetry.correlation_id(item),
                self._telemetry.cycle_kind(item),
            )

    def _enter_latency(self, item: _T) -> None:
        if self._telemetry is not None:
            self._telemetry.latency.enter(self._telemetry.correlation_id(item))

    def _finish_latency(self, item: _T | None, outcome: str) -> None:
        if item is not None and self._telemetry is not None:
            self._telemetry.latency.finish(self._telemetry.correlation_id(item), outcome=outcome)


__all__ = ["LatestWinsOffer", "LatestWinsStatus", "LatestWinsTelemetry", "LatestWinsWorker"]
