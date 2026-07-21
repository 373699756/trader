"""Lifecycle-owned bounded worker executor."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from functools import partial
from typing import ParamSpec, Protocol, TypeVar

_P = ParamSpec("_P")
_T = TypeVar("_T")
_START_TIMEOUT_SECONDS = 5.0


class WorkerExecutor(Protocol):
    def submit(
        self,
        function: Callable[_P, _T],
        /,
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> Future[_T] | None: ...


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
        self._lock = threading.Lock()
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
        self._normal_active_count = 0
        self._worker_idents: set[int] = set()

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
            warmups: list[Future[int]] = []
            try:
                warmups.extend(executor.submit(_warm_worker, ready) for _index in range(self._normal_worker_count))
                if urgent_executor is not None:
                    warmups.extend(
                        urgent_executor.submit(_warm_worker, ready) for _index in range(self._urgent_worker_count)
                    )
                ready.wait(_START_TIMEOUT_SECONDS)
                self._worker_idents = {future.result() for future in warmups}
            except BaseException:
                ready.abort()
                for future in warmups:
                    future.cancel()
                executor.shutdown(wait=True, cancel_futures=True)
                if urgent_executor is not None:
                    urgent_executor.shutdown(wait=True, cancel_futures=True)
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
                        self._normal_active_count += 1
                try:
                    return function(*args, **kwargs)
                finally:
                    with self._lock:
                        if not use_urgent_lane:
                            self._normal_active_count -= 1

            try:
                future = executor.submit(run_tracked)
            except BaseException:
                self._submitted_count -= 1
                self._inflight -= 1
                if use_urgent_lane:
                    self._urgent_submitted_count -= 1
                    self._urgent_inflight -= 1
                slots.release()
                raise
        future.add_done_callback(partial(self._complete, urgent=use_urgent_lane))
        return future

    def stop(self, *, wait: bool = True, cancel_futures: bool = False) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
            executor = self._executor
            urgent_executor = self._urgent_executor
        if executor is not None:
            executor.shutdown(wait=wait, cancel_futures=cancel_futures)
        if urgent_executor is not None:
            urgent_executor.shutdown(wait=wait, cancel_futures=cancel_futures)

    def status(self) -> dict[str, object]:
        with self._lock:
            return {
                "workers": self._worker_count,
                "urgent_workers": self._urgent_worker_count,
                "queue_capacity": self._queue_capacity,
                "urgent_queue_capacity": self._urgent_queue_capacity,
                "inflight": self._inflight,
                "urgent_inflight": self._urgent_inflight,
                "submitted_count": self._submitted_count,
                "urgent_submitted_count": self._urgent_submitted_count,
                "completed_count": self._completed_count,
                "urgent_completed_count": self._urgent_completed_count,
                "rejected_count": self._rejected_count,
                "urgent_rejected_count": self._urgent_rejected_count,
                "running": self._running,
            }

    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def owns_current_thread(self) -> bool:
        with self._lock:
            return threading.get_ident() in self._worker_idents

    def has_spare_worker(self) -> bool:
        with self._lock:
            return self._running and self._normal_active_count < self._normal_worker_count

    @property
    def worker_count(self) -> int:
        return self._worker_count

    def _complete(self, _future: Future[_T], *, urgent: bool) -> None:
        with self._lock:
            self._completed_count += 1
            self._inflight -= 1
            if urgent:
                self._urgent_completed_count += 1
                self._urgent_inflight -= 1
        slots = self._urgent_slots if urgent else self._normal_slots
        if slots is not None:
            slots.release()


def _warm_worker(ready: threading.Barrier) -> int:
    ready.wait(_START_TIMEOUT_SECONDS)
    return threading.get_ident()


@contextmanager
def borrow_executor(
    shared: BoundedExecutor | None,
    *,
    worker_count: int,
    thread_name_prefix: str,
    queue_capacity: int | None = None,
    wait_on_exit: bool = True,
) -> Iterator[WorkerExecutor]:
    if shared is not None and shared.is_running():
        # When every worker enters a nested path together, queued work cannot
        # start. Keep normal nested fan-out when at least one worker is spare.
        if shared.owns_current_thread() and not shared.has_spare_worker():
            yield _InlineExecutor()
        else:
            yield shared
        return

    local = BoundedExecutor(
        worker_count=max(1, worker_count),
        queue_capacity=max(1, queue_capacity if queue_capacity is not None else worker_count),
        thread_name_prefix=thread_name_prefix,
    )
    local.start()
    try:
        yield local
    finally:
        local.stop(wait=wait_on_exit, cancel_futures=not wait_on_exit)


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


__all__ = ["BoundedExecutor", "WorkerExecutor", "borrow_executor"]
