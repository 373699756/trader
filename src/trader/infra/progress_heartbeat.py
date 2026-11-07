"""Explicitly scoped progress heartbeat for bounded blocking I/O."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from types import TracebackType
from typing import Generic, TypeVar

from trader.infra.shutdown import ObserveShutdownStep, ShutdownDeadline, ShutdownStep

_Progress = TypeVar("_Progress")


class ProgressHeartbeat(Generic[_Progress]):
    def __init__(self, report: Callable[[_Progress], None] | None, *, interval_seconds: float = 5.0) -> None:
        if interval_seconds <= 0:
            raise ValueError("heartbeat_interval_invalid")
        self._report = report
        self._interval = interval_seconds
        self._latest: _Progress | None = None
        self._emitted_at = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: OSError | None = None

    def __enter__(self) -> ProgressHeartbeat[_Progress]:
        if self._report is not None:
            self._thread = threading.Thread(target=self._run, name="download-progress", daemon=False)
            self._thread.start()
        return self

    def __call__(self, progress: _Progress) -> None:
        with self._lock:
            self._latest = progress
            self._emitted_at = time.monotonic()
            if self._report is not None:
                self._report(progress)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                with self._lock:
                    if (
                        self._latest is not None
                        and self._report is not None
                        and time.monotonic() - self._emitted_at >= self._interval
                    ):
                        self._report(self._latest)
                        self._emitted_at = time.monotonic()
            except OSError as exc:
                self._error = exc
                return

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        if exception_type is None and self._error is not None:
            raise self._error

    @property
    def failure(self) -> OSError | None:
        return self._error

    def stop(self, deadline: ShutdownDeadline) -> ShutdownStep:
        """Stop within the caller's existing shutdown budget, retaining pending threads."""
        self._stop.set()
        with ObserveShutdownStep(deadline, "progress_heartbeat"):
            if self._thread is not None:
                self._thread.join(timeout=deadline.remaining_seconds())
        completed = self._thread is None or not self._thread.is_alive()
        return ShutdownStep(
            "progress_heartbeat", completed, not completed and deadline.expired,
            detail="" if completed else "progress callback remains blocked",
        )


__all__ = ["ProgressHeartbeat"]
