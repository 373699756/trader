"""Runtime-owned bounded worker executor."""

from __future__ import annotations

import threading
import time
import math
from collections.abc import Callable
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass
from functools import partial
from typing import ParamSpec, Protocol, TypeVar, cast
from types import TracebackType

from trader.infra.shutdown import ShutdownDeadline, ShutdownStep
from trader.infra.sqlite_observation import CurrentSqliteObservation

_P = ParamSpec("_P")
_T = TypeVar("_T")
_START_TIMEOUT_SECONDS = 5.0
_CAPACITY_WAIT_SECONDS = 5.0


class WorkerExecutor(Protocol):
    def submit(
        self,
        function: Callable[_P, _T],
        /,
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> Future[_T] | None: ...


class ManagedWorkerExecutor(WorkerExecutor, Protocol):
    """Injected execution capability for an application-owned start/stop sequence."""

    def start(self) -> bool: ...

    def stop(
        self,
        *,
        wait: bool = True,
        cancel_futures: bool = False,
        deadline: ShutdownDeadline | None = None,
    ) -> ShutdownStep: ...


class WorkerResourceRejectedError(RuntimeError):
    """An injected execution resource cannot accept new work."""


class WorkerScope:
    """Start injected workers and stop them under one bounded exit deadline.

    Construction remains in the composition root. A scope owns only workers it
    successfully started, including cleanup after a later start fails.
    """

    def __init__(
        self, workers: tuple[ManagedWorkerExecutor, ...], *, timeout_seconds: float,
        shutdown_deadline: Callable[[], ShutdownDeadline] | None = None,
    ) -> None:
        if not workers or type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("worker_scope_options_invalid")
        if len({id(worker) for worker in workers}) != len(workers):
            raise ValueError("worker_scope_duplicate_owner")
        self._workers = workers
        self._timeout_seconds = timeout_seconds
        self._shutdown_deadline = shutdown_deadline
        self._started: list[ManagedWorkerExecutor] = []
        self._entered = False
        self._closed = False

    def __enter__(self) -> WorkerScope:
        if self._entered or self._closed:
            raise RuntimeError("worker_scope_already_entered")
        self._entered = True
        try:
            for worker in self._workers:
                if not worker.start():
                    raise WorkerResourceRejectedError("worker_scope_requires_unstarted_workers")
                self._started.append(worker)
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(
        self, exception_type: type[BaseException] | None, exception: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        deadline = self._shutdown_deadline() if self._shutdown_deadline is not None else ShutdownDeadline.start(self._timeout_seconds)
        failure: BaseException | None = None
        incomplete: list[str] = []
        for worker in reversed(self._started):
            try:
                report = worker.stop(wait=True, cancel_futures=True, deadline=deadline)
                if not report.completed:
                    incomplete.append(report.name)
            except BaseException as exc:
                # Cleanup every owned worker before re-raising the first failure.
                if failure is None:
                    failure = exc
        if failure is not None:
            raise failure
        if incomplete:
            raise RuntimeError("worker_scope_shutdown_incomplete:" + ",".join(incomplete))


class WorkerFutureExecutor(Executor):
    """Use standard future mapping over a resource explicitly owned by the caller."""

    def __init__(self, workers: WorkerExecutor) -> None:
        self._workers = workers

    def submit(self, fn: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs) -> Future[_T]:
        future = self._workers.submit(fn, *args, **kwargs)
        if future is None:
            raise WorkerResourceRejectedError("bounded performance work was rejected")
        return future


@dataclass(frozen=True)
class BoundedExecutorStatus:
    workers: int
    urgent_workers: int
    queue_capacity: int
    urgent_queue_capacity: int
    inflight: int
    urgent_inflight: int
    submitted_count: int
    urgent_submitted_count: int
    completed_count: int
    urgent_completed_count: int
    rejected_count: int
    urgent_rejected_count: int
    running: bool


class BoundedExecutor:
    def __init__(
        self,
        *,
        worker_count: int,
        urgent_worker_count: int = 0,
        queue_capacity: int,
        thread_name_prefix: str,
    ) -> None:
        self._worker_count = max(1, worker_count)
        if urgent_worker_count < 0 or urgent_worker_count >= self._worker_count:
            raise ValueError("urgent_worker_count must be non-negative and smaller than worker_count")
        self._urgent_worker_count = urgent_worker_count
        self._normal_worker_count = self._worker_count - self._urgent_worker_count
        self._queue_capacity = max(0, queue_capacity)
        self._urgent_queue_capacity = self._urgent_worker_count
        self._thread_name_prefix = thread_name_prefix
        self._normal_slots = threading.BoundedSemaphore(self._normal_worker_count + self._queue_capacity)
        self._urgent_slots = (
            threading.BoundedSemaphore(self._urgent_worker_count + self._urgent_queue_capacity)
            if self._urgent_worker_count
            else None
        )
        self._lock = threading.Condition(threading.RLock())
        self._executor: ThreadPoolExecutor | None = None
        self._urgent_executor: ThreadPoolExecutor | None = None
        self._running = False
        self._submitted_count = 0
        self._completed_count = 0
        self._rejected_count = 0
        self._inflight = 0
        self._urgent_submitted_count = 0
        self._urgent_completed_count = 0
        self._urgent_rejected_count = 0
        self._urgent_inflight = 0
        self._normal_running_count = 0
        self._worker_idents: set[int] = set()
        self._worker_threads: tuple[threading.Thread, ...] = ()
        self._futures: set[Future[object]] = set()

    def start(self) -> bool:
        with self._lock:
            if self._running:
                return False
            if self._executor is not None or self._urgent_executor is not None:
                raise RuntimeError("bounded executor cannot restart after stop")
            executor = ThreadPoolExecutor(
                max_workers=self._normal_worker_count,
                thread_name_prefix=self._thread_name_prefix,
            )
            urgent_executor = (
                ThreadPoolExecutor(
                    max_workers=self._urgent_worker_count,
                    thread_name_prefix=f"{self._thread_name_prefix}-urgent",
                )
                if self._urgent_worker_count
                else None
            )
            ready = threading.Barrier(self._worker_count + 1)
            warmups: list[Future[threading.Thread]] = []
            try:
                warmups.extend(executor.submit(_warm_worker, ready) for _index in range(self._normal_worker_count))
                if urgent_executor is not None:
                    warmups.extend(
                        urgent_executor.submit(_warm_worker, ready) for _index in range(self._urgent_worker_count)
                    )
                ready.wait(_START_TIMEOUT_SECONDS)
                self._worker_threads = tuple(future.result() for future in warmups)
                # Warmup results come from already started pool threads.
                self._worker_idents = {cast(int, thread.ident) for thread in self._worker_threads}
            except BaseException:
                ready.abort()
                for future in warmups:
                    future.cancel()
                executor.shutdown(wait=True, cancel_futures=True)
                if urgent_executor is not None:
                    urgent_executor.shutdown(wait=True, cancel_futures=True)
                self._worker_threads = ()
                self._worker_idents.clear()
                self._executor = executor
                self._urgent_executor = urgent_executor
                raise
            self._executor = executor
            self._urgent_executor = urgent_executor
            self._running = True
            return True

    def submit(self, function: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs) -> Future[_T] | None:
        return self._submit(False, function, *args, **kwargs)

    def submit_urgent(
        self,
        function: Callable[_P, _T],
        /,
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> Future[_T] | None:
        return self._submit(True, function, *args, **kwargs)

    def _submit(
        self,
        urgent: bool,
        function: Callable[_P, _T],
        /,
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> Future[_T] | None:
        with self._lock:
            use_urgent_lane = urgent and self._urgent_executor is not None and self._urgent_slots is not None
            executor = self._urgent_executor if use_urgent_lane else self._executor
            slots = self._urgent_slots if use_urgent_lane else self._normal_slots
            if not self._running or executor is None or slots is None or not slots.acquire(blocking=False):
                self._rejected_count += 1
                if urgent:
                    self._urgent_rejected_count += 1
                return None
            self._submitted_count += 1
            self._inflight += 1
            if use_urgent_lane:
                self._urgent_submitted_count += 1
                self._urgent_inflight += 1

            def run_tracked() -> _T:
                with self._lock:
                    if not use_urgent_lane:
                        self._normal_running_count += 1
                try:
                    return function(*args, **kwargs)
                finally:
                    with self._lock:
                        if not use_urgent_lane:
                            self._normal_running_count -= 1

            observation = CurrentSqliteObservation()
            if observation is not None:
                observation.worker_submitted()
            try:
                future = executor.submit(copy_context().run, run_tracked)
            except BaseException:
                if observation is not None:
                    observation.worker_finished()
                self._submitted_count -= 1
                self._inflight -= 1
                if use_urgent_lane:
                    self._urgent_submitted_count -= 1
                    self._urgent_inflight -= 1
                slots.release()
                raise
            self._futures.add(cast(Future[object], future))
        future.add_done_callback(partial(self._complete, urgent=use_urgent_lane))
        if observation is not None:
            future.add_done_callback(lambda _future: observation.worker_finished())
        return future

    def stop(
        self,
        *,
        wait: bool = True,
        cancel_futures: bool = False,
        deadline: ShutdownDeadline | None = None,
    ) -> ShutdownStep:
        with self._lock:
            first_stop = self._running
            self._running = False
            executor = self._executor
            urgent_executor = self._urgent_executor
            futures = tuple(self._futures)
            workers = self._worker_threads
        can_wait = wait and threading.current_thread() not in workers
        cancel_pending = cancel_futures or (first_stop and deadline is not None)
        cancelled = sum(future.cancel() for future in futures) if cancel_futures else 0
        if can_wait and deadline is not None:
            with self._lock:
                while self._inflight:
                    remaining = deadline.remaining_seconds()
                    if remaining <= 0.0:
                        break
                    self._lock.wait(remaining)
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=cancel_pending)
        if urgent_executor is not None:
            urgent_executor.shutdown(wait=False, cancel_futures=cancel_pending)
        if can_wait:
            for worker in workers:
                worker.join(None if deadline is None else deadline.remaining_seconds())
        # A Future can be done and our counters zero while its worker is still
        # executing later callbacks. Only joined workers close that lifetime.
        with self._lock:
            completed = self._inflight == 0 and not any(worker.is_alive() for worker in workers)
        return ShutdownStep(
            name=self._thread_name_prefix,
            completed=completed,
            timed_out=wait and not completed and deadline is not None and deadline.expired,
            cancelled_count=cancelled,
            detail="worker threads or task callbacks remain at shutdown" if not completed else "",
        )

    def status(self) -> BoundedExecutorStatus:
        with self._lock:
            return BoundedExecutorStatus(
                workers=self._worker_count,
                urgent_workers=self._urgent_worker_count,
                queue_capacity=self._queue_capacity,
                urgent_queue_capacity=self._urgent_queue_capacity,
                inflight=self._inflight,
                urgent_inflight=self._urgent_inflight,
                submitted_count=self._submitted_count,
                urgent_submitted_count=self._urgent_submitted_count,
                completed_count=self._completed_count,
                urgent_completed_count=self._urgent_completed_count,
                rejected_count=self._rejected_count,
                urgent_rejected_count=self._urgent_rejected_count,
                running=self._running,
            )

    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def owns_current_thread(self) -> bool:
        with self._lock:
            return threading.get_ident() in self._worker_idents

    def has_spare_worker(self) -> bool:
        with self._lock:
            return self._running and self._normal_running_count < self._normal_worker_count

    @property
    def worker_count(self) -> int:
        return self._worker_count

    def _complete(self, future: Future[_T], *, urgent: bool) -> None:
        with self._lock:
            self._futures.discard(cast(Future[object], future))
            self._completed_count += 1
            self._inflight -= 1
            if urgent:
                self._urgent_completed_count += 1
                self._urgent_inflight -= 1
            self._lock.notify_all()
        slots = self._urgent_slots if urgent else self._normal_slots
        if slots is not None:
            slots.release()


def _warm_worker(ready: threading.Barrier) -> threading.Thread:
    ready.wait(_START_TIMEOUT_SECONDS)
    return threading.current_thread()


def InjectedExecutor(shared: BoundedExecutor | None, *, inline: bool = False) -> WorkerExecutor:
    """Use the caller's pool, or explicit serial execution, without creating resources."""
    if shared is None:
        return _InlineExecutor()
    if shared.is_running() and (inline or shared.owns_current_thread()):
        return _InlineExecutor()
    return shared


def SubmitOrReject(
    executor: WorkerExecutor,
    function: Callable[_P, _T],
    /,
    *args: _P.args,
    **kwargs: _P.kwargs,
) -> Future[_T]:
    future = executor.submit(function, *args, **kwargs)
    if future is not None:
        return future
    completed: Future[_T] = Future()
    completed.set_exception(WorkerResourceRejectedError("resource_rejected: injected worker queue is full or stopped"))
    return completed


def SubmitWithBackpressure(
    executor: WorkerExecutor,
    function: Callable[_P, _T],
    /,
    *args: _P.args,
    **kwargs: _P.kwargs,
) -> Future[_T]:
    """Wait briefly for a completed worker's capacity callback before rejecting."""
    return SubmitBeforeDeadline(
        executor, ShutdownDeadline.start(_CAPACITY_WAIT_SECONDS), function, *args, **kwargs,
    )


def SubmitBeforeDeadline(
    executor: WorkerExecutor,
    deadline: ShutdownDeadline,
    function: Callable[_P, _T],
    /,
    *args: _P.args,
    **kwargs: _P.kwargs,
) -> Future[_T]:
    """Use the caller's remaining budget for worker capacity, without restarting it."""
    while True:
        future = executor.submit(function, *args, **kwargs)
        if future is not None:
            return future
        remaining = deadline.remaining_seconds()
        if remaining <= 0:
            completed: Future[_T] = Future()
            completed.set_exception(WorkerResourceRejectedError("resource_rejected: worker capacity unavailable"))
            return completed
        time.sleep(min(0.01, remaining))


class _InlineExecutor:
    def submit(
        self,
        function: Callable[_P, _T],
        /,
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> Future[_T]:
        future: Future[_T] = Future()
        try:
            future.set_result(function(*args, **kwargs))
        except BaseException as exc:
            future.set_exception(exc)
        return future


__all__ = [
    "BoundedExecutor",
    "BoundedExecutorStatus",
    "ManagedWorkerExecutor",
    "WorkerExecutor",
    "WorkerResourceRejectedError",
    "WorkerScope",
    "WorkerFutureExecutor",
    "InjectedExecutor",
    "SubmitOrReject",
    "SubmitBeforeDeadline",
    "SubmitWithBackpressure",
]
