"""Scoped SQLite counters, including work submitted to context-aware executors."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class SqliteActivity:
    connection_attempts: int = 0
    statements: int = 0
    worker_inflight: int = 0
    worker_peak: int = 0


class SqliteObservation:
    """Count operations without retaining SQL, parameters or database paths."""

    def __init__(self, *, on_drained: Callable[[], None] | None = None) -> None:
        self._lock = threading.Lock()
        self._connections = 0
        self._statements = 0
        self._inflight = 0
        self._peak = 0
        self._on_drained = on_drained

    @contextmanager
    def bind(self) -> Iterator[None]:
        token = _observation.set(self)
        try:
            yield
        finally:
            _observation.reset(token)

    def snapshot(self) -> SqliteActivity:
        with self._lock:
            return SqliteActivity(self._connections, self._statements, self._inflight, self._peak)

    def worker_submitted(self) -> None:
        with self._lock:
            self._inflight += 1
            self._peak = max(self._peak, self._inflight)

    def worker_finished(self) -> None:
        with self._lock:
            self._inflight -= 1
            drained = self._inflight == 0
        if drained and self._on_drained is not None:
            self._on_drained()

    def record_connection(self) -> None:
        with self._lock:
            self._connections += 1

    def record_statement(self) -> None:
        with self._lock:
            self._statements += 1


_observation: ContextVar[SqliteObservation | None] = ContextVar("sqlite_observation", default=None)


def CurrentSqliteObservation() -> SqliteObservation | None:
    return _observation.get()


@contextmanager
def IndependentSqliteWork() -> Iterator[None]:
    """Detach explicitly separate asynchronous work from the caller's measurement."""
    token = _observation.set(None)
    try:
        yield
    finally:
        _observation.reset(token)


def ObserveSqliteConnection() -> None:
    observation = _observation.get()
    if observation is not None:
        observation.record_connection()


def ObserveSqliteStatement() -> None:
    observation = _observation.get()
    if observation is not None:
        observation.record_statement()
