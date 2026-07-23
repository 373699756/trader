"""Latest-wins scheduling for the fixed market-data source lanes."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime
from typing import ParamSpec, TypeVar, cast

from trader.application.latency import LatencyWaterfall
from trader.application.workers import BoundedExecutor

_P = ParamSpec("_P")
_T = TypeVar("_T")
_SOURCE_NAMES = ("eastmoney", "history", "sina", "tencent", "tushare", "akshare")


class SourceRequestSupersededError(RuntimeError):
    """A queued source request was replaced by a newer observation point."""


@dataclass
class _LaneRequest:
    identity: str
    observed_at: datetime
    sequence: int
    call: Callable[[], object]
    future: Future[object]
    submitted_at: float


class LatestRequestLane:
    """Run at most one source task and retain only its newest pending request."""

    def __init__(
        self,
        source: str,
        executor: BoundedExecutor,
        *,
        latency: LatencyWaterfall | None = None,
        monotonic: Callable[[], float] = time.perf_counter,
    ) -> None:
        normalized = source.strip().lower()
        if not normalized:
            raise ValueError("source lane name must not be empty")
        self._source = normalized
        self._executor = executor
        self._latency = latency
        self._monotonic = monotonic
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
        _validate_request(normalized_identity, observed_at)
        created: Future[object] = Future()

        def call() -> object:
            return function(*args, **kwargs)

        with self._condition:
            existing = self._existing_future(normalized_identity, observed_at, call, created)
            if existing is not None:
                return cast(Future[_T], existing)
            self._sequence += 1
            request = _LaneRequest(
                normalized_identity,
                observed_at,
                self._sequence,
                call,
                created,
                self._monotonic(),
            )
            return cast(Future[_T], self._enqueue(request, urgent))

    def _existing_future(
        self,
        identity: str,
        observed_at: datetime,
        call: Callable[[], object],
        created: Future[object],
    ) -> Future[object] | None:
        if self._stopped:
            created.set_exception(RuntimeError(f"{self._source} source lane is stopped"))
            self._rejected_count += 1
            return created
        running = self._running
        if (
            running is not None
            and running.identity == identity
            and observed_at <= running.observed_at
            and not running.future.cancelled()
        ):
            self._coalesced_count += 1
            return running.future
        pending = self._pending
        if pending is None or pending.identity != identity or pending.future.cancelled():
            return None
        self._coalesced_count += 1
        if observed_at >= pending.observed_at:
            self._sequence += 1
            self._pending = _LaneRequest(
                identity,
                observed_at,
                self._sequence,
                call,
                pending.future,
                self._monotonic(),
            )
        return pending.future

    def _enqueue(self, request: _LaneRequest, urgent: bool) -> Future[object]:
        if self._running is None:
            self._start_runner(request, urgent)
            return request.future
        if _request_order(request) <= _request_order(self._running):
            self._supersede(request.future)
            return request.future
        pending = self._pending
        if pending is not None and _request_order(request) <= _request_order(pending):
            self._supersede(request.future)
            return request.future
        if pending is not None:
            self._supersede(pending.future)
        self._pending = request
        return request.future

    def _start_runner(self, request: _LaneRequest, urgent: bool) -> None:
        self._running = request
        submit = self._executor.submit_urgent if urgent else self._executor.submit
        runner = submit(self._drain)
        if runner is None:
            self._running = None
            self._rejected_count += 1
            request.future.set_exception(RuntimeError(f"{self._source} source lane queue is full or stopped"))
            return
        self._runner_future = runner
        runner.add_done_callback(self._runner_finished)

    def _supersede(self, future: Future[object]) -> None:
        self._superseded_count += 1
        if not future.done():
            future.set_exception(SourceRequestSupersededError(f"{self._source} source request was superseded"))

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
            self._execute(request)
            if not self._advance():
                return

    def _execute(self, request: _LaneRequest) -> None:
        should_run = request.future.set_running_or_notify_cancel()
        if should_run and self._latency is not None:
            self._latency.record_duration(
                "source_queue_wait",
                max(0.0, (self._monotonic() - request.submitted_at) * 1000.0),
            )
        try:
            result = request.call() if should_run else None
        except BaseException as exc:
            if not request.future.done():
                request.future.set_exception(exc)
        else:
            if should_run and not request.future.done():
                request.future.set_result(result)

    def _advance(self) -> bool:
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
            return has_next

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
    """Fixed five-source registry with an isolated daily-history activity lane."""

    def __init__(self, executor: BoundedExecutor, *, latency: LatencyWaterfall | None = None) -> None:
        self._lanes = {source: LatestRequestLane(source, executor, latency=latency) for source in _SOURCE_NAMES}

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


__all__ = ["LatestRequestLane", "SourceLaneRegistry", "SourceRequestSupersededError"]


def _validate_request(identity: str, observed_at: datetime) -> None:
    if not identity:
        raise ValueError("source lane request identity must not be empty")
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("source lane observed_at must be timezone-aware")


def _request_order(request: _LaneRequest) -> tuple[datetime, int]:
    return request.observed_at, request.sequence
