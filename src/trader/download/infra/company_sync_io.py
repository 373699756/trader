"""One explicit monotonic budget for company SQL and cancellation."""

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from math import isfinite
from pathlib import Path

from trader.download.domain.company_sync import CompanySyncInterrupted
from trader.infra.sqlite_connection import SqliteConnection, SqliteConnectionOptions, SqliteSession
from trader.infra.sqlite_policy import SqliteIoPolicy


class CompanySyncIoBudget:
    def __init__(self, *, deadline: float | None, monotonic: Callable[[], float], cancel_requested: Callable[[], bool]) -> None:
        if deadline is not None and not isfinite(deadline):
            raise ValueError("company_sync_io_deadline_invalid")
        self.deadline, self._monotonic, self._cancel_requested = deadline, monotonic, cancel_requested

    def stopped(self) -> bool:
        return self._cancel_requested() or (self.deadline is not None and self._monotonic() >= self.deadline)

    def check(self) -> None:
        if self.stopped():
            raise CompanySyncInterrupted("company_sync_io_budget_exhausted")

    def remaining_policy(self, policy: SqliteIoPolicy) -> SqliteIoPolicy:
        self.check()
        if self.deadline is None:
            return policy
        remaining = self.deadline - self._monotonic()
        self.check()
        return replace(policy, connection_timeout_seconds=min(policy.connection_timeout_seconds, remaining))

    @contextmanager
    def access(self) -> Iterator[None]:
        self.check()
        try:
            yield
            self.check()
        except sqlite3.OperationalError as exc:
            if str(exc) in {"interrupted", "database is locked"} and self.stopped():
                raise CompanySyncInterrupted("company_sync_sql_budget_exhausted") from exc
            raise

    @contextmanager
    def session(self, path: Path, options: SqliteConnectionOptions, *, sqlite_policy: SqliteIoPolicy,
        cancel_requested: Callable[[], bool] | None = None) -> Iterator[SqliteConnection]:
        policy = self.remaining_policy(sqlite_policy)
        with self.access(), SqliteSession(path, options, sqlite_policy=policy,
            cancel_requested=lambda: self.stopped() or cancel_requested is not None and cancel_requested()) as connection:
            yield connection


__all__ = ["CompanySyncIoBudget"]
