"""Bounded progress and cancellation for long offline SQLite statements."""

import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from threading import Event, Lock


class HistorySqliteActivity:
    def __init__(
        self,
        progress: Callable[[], None],
        cancel_requested: Callable[[], bool],
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._progress = progress
        self._cancel_requested = cancel_requested
        self._monotonic = monotonic
        self._reported = monotonic()
        self._stopped = Event()
        self._report_lock = Lock()

    def cancel(self) -> None:
        self._stopped.set()

    def tick(self) -> None:
        if self._stopped.is_set() or self._cancel_requested():
            raise KeyboardInterrupt
        now = self._monotonic()
        with self._report_lock:
            if now - self._reported >= 5.0:
                self._progress()
                self._reported = now

    @contextmanager
    def watch(self, connection: sqlite3.Connection) -> Iterator[None]:
        cancelled = False

        def check() -> int:
            nonlocal cancelled
            try:
                self.tick()
            except KeyboardInterrupt:
                cancelled = True
                return 1
            return 0

        self.tick()
        connection.set_progress_handler(check, 10_000)
        try:
            yield
        except sqlite3.OperationalError:
            if cancelled:
                raise KeyboardInterrupt from None
            raise
        finally:
            connection.set_progress_handler(None, 0)
