"""Scoped SQLite instrumentation for isolated diagnostic workers only."""

from __future__ import annotations

import resource
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock, Thread
from types import TracebackType
from typing import ParamSpec, TypeVar
from unittest.mock import patch

from trader.infra import sqlite_connection
from trader.infra.sqlite_connection import SqliteConnection, SqliteParameters, SqliteResult, SqliteTransaction

_Parameters = ParamSpec("_Parameters")
_Value = TypeVar("_Value")
_MAX_PENDING_CALLS = 1024


@dataclass(frozen=True)
class _PendingSqliteCall:
    operation: str
    started: float


class DatabaseMeasurement:
    """Count SQLite wrapper operations without retaining SQL text, parameters or rows."""

    def __init__(self, directory: Path, *, sample_wal: bool = True) -> None:
        self._directory = directory
        self._sample_wal = sample_wal
        self._lock = Lock()
        self._stop = Event()
        self._stack = ExitStack()
        self._sampler = Thread(target=self._sample, name="diagnostic-wal-sampler", daemon=False)
        self._connections = 0
        self._statements = 0
        self._transactions = 0
        self._commits = 0
        self._sqlite_seconds = 0.0
        self._commit_seconds = 0.0
        self._connection_open_seconds = 0.0
        self._connection_close_seconds = 0.0
        self._wal_peak = 0
        self._wal_sampling_failure = False
        self._wall = 0.0
        self._cpu = 0.0
        self._before_io: tuple[int, int] | None = None
        self._measuring = False
        self._pending: dict[object, _PendingSqliteCall] = {}
        self._untracked_calls = 0
        self.result: dict[str, object] = {}

    def __enter__(self) -> DatabaseMeasurement:
        run, run_many = SqliteConnection.run, SqliteConnection.run_many

        def counted_run(connection: SqliteConnection, sql: str, parameters: SqliteParameters = ()) -> SqliteResult:
            words = sql.lstrip().split(None, 1)
            command = words[0].upper() if words else ""
            with self._lock:
                self._statements += 1
            token, started = self._begin_call("commit" if command == "COMMIT" else "execute")
            try:
                result = run(connection, sql, parameters)
                with self._lock:
                    self._transactions += int(command == "BEGIN")
                    self._commits += int(command == "COMMIT")
                return result
            finally:
                self._elapsed(token, started, commit=command == "COMMIT")

        def counted_many(
            connection: SqliteConnection, sql: str, parameters: Iterable[SqliteParameters]
        ) -> SqliteResult:
            def counted_rows() -> Iterator[SqliteParameters]:
                for row in parameters:
                    with self._lock:
                        self._statements += 1
                    yield row

            token, started = self._begin_call("execute_many")
            try:
                return run_many(connection, sql, counted_rows())
            finally:
                self._elapsed(token, started)

        for owner, name, replacement in (
            (sqlite_connection, "OpenSqliteConnection", self._timed_call(sqlite_connection.OpenSqliteConnection, category="connection_open")),
            (SqliteConnection, "__init__", self._count_connection(SqliteConnection.__init__)),
            (SqliteConnection, "close", self._timed_call(SqliteConnection.close, category="connection_close")),
            (SqliteConnection, "run", counted_run),
            (SqliteConnection, "run_many", counted_many),
            (SqliteTransaction, "commit", self._count_commit(SqliteTransaction.commit)),
            (SqliteResult, "fetchone", self._timed_call(SqliteResult.fetchone)),
            (SqliteResult, "fetchmany", self._timed_call(SqliteResult.fetchmany)),
            (SqliteResult, "fetchall", self._timed_call(SqliteResult.fetchall)),
            (SqliteResult, "__iter__", self._timed_iterator(SqliteResult.__iter__)),
        ):
            self._stack.enter_context(patch.object(owner, name, replacement))
        self._before_io = _physical_io()
        self._wall, self._cpu = time.perf_counter(), time.process_time()
        with self._lock:
            self._measuring = True
        try:
            if self._sample_wal:
                self._sampler.start()
        except RuntimeError:
            with self._lock:
                self._measuring = False
            self._stack.close()
            raise
        return self

    def _count_connection(self, initialize: Callable[_Parameters, _Value]) -> Callable[_Parameters, _Value]:
        def counted(*args: _Parameters.args, **kwargs: _Parameters.kwargs) -> _Value:
            result = initialize(*args, **kwargs)
            with self._lock:
                self._connections += 1
            return result

        return counted

    def _count_commit(self, operation: Callable[[SqliteTransaction], None]) -> Callable[[SqliteTransaction], None]:
        def counted(transaction: SqliteTransaction) -> None:
            token, started = self._begin_call("commit")
            try:
                operation(transaction)
                with self._lock:
                    self._commits += 1
            finally:
                self._elapsed(token, started, commit=True)

        return counted

    def _timed_call(
        self, operation: Callable[_Parameters, _Value], *, category: str = "fetch"
    ) -> Callable[_Parameters, _Value]:
        def timed(*args: _Parameters.args, **kwargs: _Parameters.kwargs) -> _Value:
            token, started = self._begin_call(category)
            try:
                return operation(*args, **kwargs)
            finally:
                self._elapsed(token, started, category=category)

        return timed

    def _timed_iterator(
        self, operation: Callable[_Parameters, Iterator[_Value]]
    ) -> Callable[_Parameters, Iterator[_Value]]:
        def timed(*args: _Parameters.args, **kwargs: _Parameters.kwargs) -> Iterator[_Value]:
            iterator = operation(*args, **kwargs)
            while True:
                token, started = self._begin_call("fetch")
                try:
                    row = next(iterator)
                except StopIteration:
                    return
                finally:
                    self._elapsed(token, started)
                yield row

        return timed

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        wall, cpu = time.perf_counter() - self._wall, time.process_time() - self._cpu
        after_io = _physical_io()
        try:
            self._stop.set()
            if self._sample_wal:
                self._sampler.join()
        finally:
            self._stack.close()
        with self._lock:
            self._measuring = False
        before = self._before_io
        self.result = {
            "wall_seconds": wall,
            "cpu_seconds": cpu,
            "connections": self._connections,
            "statements": self._statements,
            "transactions": self._transactions,
            "commits": self._commits,
            "sqlite_call_seconds": self._sqlite_seconds,
            "commit_seconds": self._commit_seconds,
            "connection_open_seconds": self._connection_open_seconds,
            "connection_close_seconds": self._connection_close_seconds,
            "physical_read_bytes": after_io[0] - before[0] if before and after_io else None,
            "physical_write_bytes": after_io[1] - before[1] if before and after_io else None,
            "sampled_wal_peak_bytes": self._wal_peak,
            "wal_sampling_enabled": self._sample_wal,
            "wal_sampling_failed": self._wal_sampling_failure,
            "process_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            * (1 if sys.platform == "darwin" else 1024),
            "completed": exc_type is None,
        }

    def running_snapshot(self) -> dict[str, object]:
        """Project counters without querying SQLite or scanning database files.

        Call timings include nested operations. Pending ages describe calls that
        have not returned and are separate from accumulated completed-call time.
        """
        now, cpu, io = time.perf_counter(), time.process_time(), _physical_io()
        with self._lock:
            if not self._measuring:
                return {}
            before = self._before_io
            pending = tuple(self._pending.values())
            return {
                "wall_seconds": now - self._wall,
                "cpu_seconds": cpu - self._cpu,
                "connections": self._connections,
                "statements": self._statements,
                "transactions": self._transactions,
                "commits": self._commits,
                "sqlite_call_seconds": self._sqlite_seconds,
                "commit_seconds": self._commit_seconds,
                "connection_open_seconds": self._connection_open_seconds,
                "connection_close_seconds": self._connection_close_seconds,
                "pending_calls": [
                    {
                        "operation": operation,
                        "calls": len(starts),
                        "oldest_seconds": max(0.0, now - min(starts)) if starts else 0.0,
                    }
                    for operation in ("connection_open", "execute", "execute_many", "fetch", "commit", "connection_close")
                    for starts in ([call.started for call in pending if call.operation == operation],)
                ],
                "pending_call_limit": _MAX_PENDING_CALLS,
                "untracked_call_count": self._untracked_calls,
                "timing": "inclusive_completed_calls; pending_ages_separate; overlapping_calls_must_not_be_summed",
                "physical_read_bytes": io[0] - before[0] if before and io else None,
                "physical_write_bytes": io[1] - before[1] if before and io else None,
                "wal_sampling_enabled": self._sample_wal,
                "completed": False,
            }

    def _begin_call(self, operation: str) -> tuple[object, float]:
        token, started = object(), time.perf_counter()
        with self._lock:
            if len(self._pending) < _MAX_PENDING_CALLS:
                self._pending[token] = _PendingSqliteCall(operation, started)
            else:
                self._untracked_calls += 1
        return token, started

    def _elapsed(
        self, token: object, started: float, *, commit: bool = False, category: str = ""
    ) -> None:
        elapsed = time.perf_counter() - started
        with self._lock:
            self._pending.pop(token, None)
            self._sqlite_seconds += elapsed
            if commit:
                self._commit_seconds += elapsed
            if category == "connection_open":
                self._connection_open_seconds += elapsed
            elif category == "connection_close":
                self._connection_close_seconds += elapsed

    def _sample(self) -> None:
        while True:
            try:
                total = 0
                for path in self._directory.rglob("*-wal"):
                    try:
                        total += path.stat().st_size
                    except FileNotFoundError:
                        pass
                self._wal_peak = max(self._wal_peak, total)
            except OSError:
                self._wal_sampling_failure = True
            if self._stop.wait(0.05):
                return


def _physical_io() -> tuple[int, int] | None:
    try:
        fields = {
            key: int(value)
            for key, value in (line.split(":") for line in Path("/proc/self/io").read_text().splitlines())
        }
        return fields["read_bytes"], fields["write_bytes"]
    except (OSError, KeyError, ValueError):
        return None
