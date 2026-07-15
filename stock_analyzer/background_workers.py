"""Lifecycle primitives for a group of cooperative background threads."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from typing import TypedDict

WorkerStarter = Callable[[threading.Event], threading.Thread | None]


class BackgroundWorkerGroupStatus(TypedDict):
    configured_workers: int
    running_workers: int
    starting: bool
    stop_requested: bool


class BackgroundWorkerGroup:
    """Own threads created by cooperative starters sharing one stop event."""

    def __init__(self, starters: Iterable[WorkerStarter]) -> None:
        self._starters = tuple(starters)
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._starting = False

    def start(self) -> bool:
        with self._lock:
            self._threads = [thread for thread in self._threads if thread.is_alive()]
            if self._starting or self._threads:
                return False
            self._starting = True
            self._stop_event.clear()

        started_threads: list[threading.Thread] = []
        try:
            for starter in self._starters:
                if self._stop_event.is_set():
                    break
                thread = starter(self._stop_event)
                if thread is None:
                    continue
                started_threads.append(thread)
                with self._lock:
                    self._threads.append(thread)
        except Exception:
            self._stop_event.set()
            self._join(started_threads, timeout_seconds=5.0)
            with self._lock:
                self._threads = [thread for thread in self._threads if thread.is_alive()]
            raise
        finally:
            with self._lock:
                self._starting = False
        return bool(started_threads)

    def stop(self, timeout_seconds: float = 5.0) -> None:
        self._stop_event.set()
        with self._lock:
            threads = list(self._threads)
        self._join(threads, timeout_seconds)
        with self._lock:
            self._threads = [thread for thread in threads if thread.is_alive()]

    def status(self) -> BackgroundWorkerGroupStatus:
        with self._lock:
            running_workers = sum(thread.is_alive() for thread in self._threads)
            return {
                "configured_workers": len(self._starters),
                "running_workers": running_workers,
                "starting": self._starting,
                "stop_requested": self._stop_event.is_set(),
            }

    @staticmethod
    def _join(threads: Iterable[threading.Thread], timeout_seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        for thread in threads:
            if thread is threading.current_thread():
                continue
            thread.join(max(0.0, deadline - time.monotonic()))


__all__ = [
    "BackgroundWorkerGroup",
    "BackgroundWorkerGroupStatus",
    "WorkerStarter",
]
