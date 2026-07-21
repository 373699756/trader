"""Latest-wins scheduling for the fixed market-data source lanes."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime
from typing import ParamSpec, TypeVar, cast

from trader.application.workers import BoundedExecutor

_P = ParamSpec("_P")
_T = TypeVar("_T")
_SOURCE_NAMES = ("eastmoney", "sina", "tencent", "tushare", "akshare")


class SourceRequestSuperseded(RuntimeError):
    """A queued source request was replaced by a newer observation point."""


@dataclass
class _LaneRequest:
    identity: str
    observed_at: datetime
    sequence: int
    call: Callable[[], object]
    future: Future[object]


class LatestRequestLane:
    """Run at most one source task and retain only its newest pending request."""

    def __init__(self, source: str, executor: BoundedExecutor) -> None:
        normalized = source.strip().lower()
        if not normalized:
            raise ValueError("source lane name must not be empty")
        self._source = normalized
        self._executor = executor
        self._condition = threading.Condition()
        self._running: _LaneRequest | None = None
        self._pending: _LaneRequest | None = None
        self._runner_future: Future[None] | None = None
        self._active_thread_ident: int | None = None
        self._sequence = 0
        self._stopped = False
        self._completed_count = 0
        self._coalesced_count = 0
        self._superseded_count = 0
        self._rejected_count = 0

    def submit(
        self,
        identity: str,
        observed_at: datetime,
        function: Callable[_P, _T],
        /,
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> Future[_T]:
        return self._submit(False, identity, observed_at, function, *args, **kwargs)

    def submit_urgent(
        self,
        identity: str,
        observed_at: datetime,
        function: Callable[_P, _T],
        /,
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> Future[_T]:
        return self._submit(True, identity, observed_at, function, *args, **kwargs)

    def _submit(
        self,
        urgent: bool,
        identity: str,
        observed_at: datetime,
        function: Callable[_P, _T],
        /,
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> Future[_T]:
        normalized_identity = identity.strip()
        if not normalized_identity:
            raise ValueError("source lane request identity must not be empty")
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("source lane observed_at must be timezone-aware")

        created: Future[object] = Future()
        with self._condition:
            if self._stopped:
                created.set_exception(RuntimeError(f"{self._source} source lane is stopped"))
                self._rejected_count += 1
                return cast(Future[_T], created)
            if (
                self._running is not None
                and self._running.identity == normalized_identity
                and observed_at <= self._running.observed_at
                and not self._running.future.cancelled()
            ):
                self._coalesced_count += 1
                return cast(Future[_T], self._running.future)
            if (
                self._pending is not None
                and self._pending.identity == normalized_identity
                and not self._pending.future.cancelled()
            ):
                self._coalesced_count += 1
                if observed_at >= self._pending.observed_at:
                    self._sequence += 1
                    self._pending = _LaneRequest(
                        normalized_identity,
                        observed_at,
                        self._sequence,
                        lambda: function(*args, **kwargs),
                        self._pending.future,
                    )
                return cast(Future[_T], self._pending.future)

            self._sequence += 1
            request = _LaneRequest(
                normalized_identity,
                observed_at,
                self._sequence,
                lambda: function(*args, **kwargs),
                created,
            )
            if self._running is None:
                self._running = request
                submit = self._executor.submit_urgent if urgent else self._executor.submit
                runner = submit(self._drain)
                if runner is None:
                    self._running = None
                    self._rejected_count += 1
                    created.set_exception(RuntimeError(f"{self._source} source lane queue is full or stopped"))
                else:
                    self._runner_future = runner
                    runner.add_done_callback(self._runner_finished)
                return cast(Future[_T], created)

            if (request.observed_at, request.sequence) <= (
                self._running.observed_at,
                self._running.sequence,
            ):
                self._superseded_count += 1
                created.set_exception(SourceRequestSuperseded(f"{self._source} source request was superseded"))
                return cast(Future[_T], created)
            if self._pending is not None:
                pending = self._pending
                if (request.observed_at, request.sequence) <= (pending.observed_at, pending.sequence):
                    self._superseded_count += 1
                    created.set_exception(SourceRequestSuperseded(f"{self._source} source request was superseded"))
                    return cast(Future[_T], created)
                self._superseded_count += 1
                if not pending.future.done():
                    pending.future.set_exception(
                        SourceRequestSuperseded(f"{self._source} source request was superseded")
                    )
            self._pending = request
            return cast(Future[_T], created)

    def stop(self, *, wait: bool = True, timeout_seconds: float | None = None) -> None:
        deadline = None if timeout_seconds is None else time.monotonic() + max(0.0, timeout_seconds)
        with self._condition:
            self._stopped = True
            if self._pending is not None:
                if not self._pending.future.done():
                    self._pending.future.set_exception(RuntimeError(f"{self._source} source lane stopped"))
                self._pending = None
            if self._running is not None and self._active_thread_ident is None and self._runner_future is not None:
                self._runner_future.cancel()
            while wait and self._running is not None:
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)

    def owns_current_thread(self) -> bool:
        with self._condition:
            return self._active_thread_ident == threading.get_ident()

    def is_stopped(self) -> bool:
        with self._condition:
            return self._stopped

    def status(self) -> dict[str, object]:
        with self._condition:
            return {
                "source": self._source,
                "running": self._running is not None,
                "pending": self._pending is not None,
                "completed_count": self._completed_count,
                "coalesced_count": self._coalesced_count,
                "superseded_count": self._superseded_count,
                "rejected_count": self._rejected_count,
                "stopped": self._stopped,
            }

    def _drain(self) -> None:
        while True:
            with self._condition:
                request = self._running
                if request is None:
                    return
                self._active_thread_ident = threading.get_ident()
            has_next = False
            should_run = request.future.set_running_or_notify_cancel()
            try:
                result = request.call() if should_run else None
            except BaseException as exc:
                if not request.future.done():
                    request.future.set_exception(exc)
            else:
                if should_run and not request.future.done():
                    request.future.set_result(result)
            finally:
                with self._condition:
                    self._completed_count += 1
                    self._active_thread_ident = None
                    if self._stopped:
                        if self._pending is not None and not self._pending.future.done():
                            self._pending.future.set_exception(RuntimeError(f"{self._source} source lane stopped"))
                        self._pending = None
                        self._running = None
                    elif self._pending is not None:
                        self._running = self._pending
                        self._pending = None
                    else:
                        self._running = None
                    has_next = self._running is not None
                    self._condition.notify_all()
            if not has_next:
                return

    def _runner_finished(self, future: Future[None]) -> None:
        with self._condition:
            if future is not self._runner_future:
                return
            self._runner_future = None
            if self._running is None or self._active_thread_ident is not None:
                return
            error = RuntimeError(f"{self._source} source lane runner stopped before execution")
            if not self._running.future.done():
                self._running.future.set_exception(error)
            if self._pending is not None and not self._pending.future.done():
                self._pending.future.set_exception(error)
            self._running = None
            self._pending = None
            self._condition.notify_all()


class SourceLaneRegistry:
    """Fixed five-source lane registry sharing one lifecycle-owned executor."""

    def __init__(self, executor: BoundedExecutor) -> None:
        self._lanes = {source: LatestRequestLane(source, executor) for source in _SOURCE_NAMES}

    def submit(
        self,
        source: str,
        identity: str,
        observed_at: datetime,
        function: Callable[_P, _T],
        /,
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> Future[_T]:
        return self._lane(source).submit(identity, observed_at, function, *args, **kwargs)

    def submit_urgent(
        self,
        source: str,
        identity: str,
        observed_at: datetime,
        function: Callable[_P, _T],
        /,
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> Future[_T]:
        return self._lane(source).submit_urgent(identity, observed_at, function, *args, **kwargs)

    def owns_current_thread(self, source: str) -> bool:
        return self._lane(source).owns_current_thread()

    def is_stopped(self, source: str) -> bool:
        return self._lane(source).is_stopped()

    def stop(self, *, wait: bool = True, timeout_seconds: float | None = None) -> None:
        started = time.monotonic()
        for lane in self._lanes.values():
            remaining = None
            if timeout_seconds is not None:
                remaining = max(0.0, timeout_seconds - (time.monotonic() - started))
            lane.stop(wait=wait, timeout_seconds=remaining)

    def status(self) -> dict[str, dict[str, object]]:
        return {source: lane.status() for source, lane in self._lanes.items()}

    def _lane(self, source: str) -> LatestRequestLane:
        try:
            return self._lanes[source.strip().lower()]
        except KeyError as exc:
            raise ValueError(f"unknown source lane: {source}") from exc


__all__ = ["LatestRequestLane", "SourceLaneRegistry", "SourceRequestSuperseded"]
