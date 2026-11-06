"""Bounded discovery and durable review receipts for unconfirmed suspension facts."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from trader.download.domain.baostock_daily import BaoStockDailyCell
from trader.download.domain.history_sync import HistorySupplierContext
from trader.download.domain.price_review import PriceReviewBudget, PriceReviewBudgetExhausted, PriceReviewConfiguration
from trader.infra.sqlite_codec import DecodeDate, EncodeDate
from trader.infra.sqlite_connection import SqliteConnection, SqliteConnectionOptions, SqliteSession, SqliteTransaction
from trader.infra.sqlite_policy import SqliteIoPolicy

if TYPE_CHECKING:
    from trader.download.infra.history_direct_prices import HistoryDirectPrices


HISTORY_INFERRED_REVIEW_SQL = (
    """CREATE TABLE IF NOT EXISTS download_inferred_suspension (
        code TEXT NOT NULL, trade_date INTEGER NOT NULL, observation_sequence INTEGER NOT NULL,
        checked_cutoff INTEGER NOT NULL, PRIMARY KEY(code,trade_date)
    ) WITHOUT ROWID""",
    """CREATE INDEX IF NOT EXISTS download_inferred_review_due_idx
        ON download_inferred_suspension(checked_cutoff,code,trade_date)""",
    """CREATE TABLE IF NOT EXISTS download_inferred_cursor (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), after_code TEXT NOT NULL
    )""",
)


@dataclass(frozen=True)
class HistoryInferredReviewTask:
    code: str
    dates: tuple[date, ...]
    inferred_dates: tuple[date, ...]


def IsHistoryReviewBusy(exc: sqlite3.OperationalError) -> bool:
    """Treat only SQLite's documented lock outcomes as an auxiliary deferral."""
    return str(exc) in {"database is locked", "database table is locked", "database schema is locked"}


def RecordHistoryInferredSuspensions(
    connection: SqliteConnection,
    code: str,
    dates: tuple[date, ...],
    inferred_dates: tuple[date, ...],
    sequence: int,
    cutoff: date,
    *,
    discovery: bool = False,
) -> None:
    """Track qualified output only; repeated observations retain their review age."""
    inferred = frozenset(inferred_dates)
    observed = frozenset(dates)
    previous = tuple(
        DecodeDate(row[0]) for row in connection.run(
            "SELECT trade_date FROM download_inferred_suspension WHERE code=?", (code,),
        )
    )
    connection.run_many(
        "DELETE FROM download_inferred_suspension WHERE code=? AND trade_date=?",
        ((code, EncodeDate(day)) for day in previous if day in observed and day not in inferred),
    )
    connection.run_many(
        "INSERT INTO download_inferred_suspension VALUES(?,?,?,?) "
        "ON CONFLICT(code,trade_date) DO UPDATE SET observation_sequence=excluded.observation_sequence",
        ((code, EncodeDate(day), sequence, EncodeDate(day if discovery else cutoff)) for day in inferred_dates),
    )


@contextmanager
def _watch_budget(connection: SqliteConnection, budget: PriceReviewBudget) -> Iterator[None]:
    expired: PriceReviewBudgetExhausted | None = None

    def check() -> int:
        nonlocal expired
        try:
            budget.check_deadline()
        except PriceReviewBudgetExhausted as exc:
            expired = exc
            return 1
        return 0

    budget.check_deadline()
    connection.observe_progress(check, 1000)
    try:
        yield
    except sqlite3.OperationalError:
        if expired is not None:
            raise expired from None
        raise
    finally:
        connection.observe_progress(None, 0)


class HistoryInferredReviewIndex:
    """Metadata never changes old prices, publication identities, or ordinary gaps."""

    def __init__(self, path: Path, *, sqlite_policy: SqliteIoPolicy) -> None:
        self._path = path
        self._sqlite_policy = replace(
            sqlite_policy, connection_timeout_seconds=min(sqlite_policy.connection_timeout_seconds, 0.001),
        )

    def known_inferred_codes(self, budget: PriceReviewBudget) -> int:
        """Return a budgeted inventory count, or the safe lower bound on exhaustion."""
        try:
            with (
                SqliteSession(self._path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as connection,
                _watch_budget(connection, budget),
            ):
                return int(connection.run("SELECT COUNT(DISTINCT code) FROM download_inferred_suspension").require_one()[0])
        except PriceReviewBudgetExhausted:
            return 0
        except sqlite3.OperationalError as exc:
            if not IsHistoryReviewBusy(exc):
                raise
            return 0
        finally:
            budget.pause()

    def plan(
        self,
        context: HistorySupplierContext,
        prices: HistoryDirectPrices,
        sequence: int,
        configuration: PriceReviewConfiguration,
        budget: PriceReviewBudget,
        cancel: Callable[[], bool],
    ) -> tuple[HistoryInferredReviewTask, ...]:
        """Prefer due facts, then rotate through exact keys for at most eight stocks."""
        calendar = context.calendar.open_dates
        eligible = {item.code: item for item in context.universe}
        threshold = calendar[-1 - configuration.interval_sessions] if len(calendar) > configuration.interval_sessions else date.min
        grouped: dict[str, list[date]] = {}
        with SqliteSession(self._path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as connection:
            with _watch_budget(connection, budget):
                rows = connection.run(
                    "SELECT code,trade_date FROM download_inferred_suspension "
                    "WHERE observation_sequence<=? AND checked_cutoff<=? ORDER BY checked_cutoff,code,trade_date",
                    (sequence, EncodeDate(threshold)),
                )
                for code, encoded in rows:
                    if cancel():
                        raise KeyboardInterrupt
                    day = DecodeDate(encoded)
                    if code not in eligible or day not in context.calendar.expected_dates(eligible[code]):
                        continue
                    if code not in grouped and not budget.reserve_code(code):
                        break
                    grouped.setdefault(code, []).append(day)
        selected = set(grouped)
        with SqliteSession(self._path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as connection:
            cursor_row = connection.run("SELECT after_code FROM download_inferred_cursor WHERE singleton=1").fetchone()
        cursor = str(cursor_row[0]) if cursor_row else ""
        codes = sorted(eligible)
        candidates = (*tuple(code for code in codes if code > cursor), *tuple(code for code in codes if code <= cursor))
        try:
            for code in candidates:
                if code in selected:
                    continue
                if cancel():
                    raise KeyboardInterrupt
                if not budget.reserve_code(code):
                    break
                cells = prices.read_for_review(code, context.calendar.expected_dates(eligible[code]), budget)
                inferred = tuple(cell.trade_date for cell in cells if cell.status == "tencent_inferred_suspended")
                with (
                    SqliteSession(self._path, sqlite_policy=self._sqlite_policy) as connection,
                    SqliteTransaction(connection, immediate=True),
                    _watch_budget(connection, budget),
                ):
                    RecordHistoryInferredSuspensions(
                        connection,
                        code,
                        tuple(cell.trade_date for cell in cells),
                        inferred,
                        sequence,
                        calendar[-1],
                        discovery=True,
                    )
                    connection.run(
                        "INSERT INTO download_inferred_cursor VALUES(1,?) "
                        "ON CONFLICT(singleton) DO UPDATE SET after_code=excluded.after_code", (code,),
                    )
                    due = tuple(
                        DecodeDate(row[0]) for row in connection.run(
                            "SELECT trade_date FROM download_inferred_suspension "
                            "WHERE code=? AND checked_cutoff<=? ORDER BY trade_date",
                            (code, EncodeDate(threshold)),
                        ) if DecodeDate(row[0]) in inferred
                    )
                if due:
                    grouped[code] = list(due)
                budget.check_deadline()
        except PriceReviewBudgetExhausted:
            pass
        tasks = []
        for code, days in grouped.items():
            expected = context.calendar.expected_dates(eligible[code])
            dates = tuple(sorted(set(days) | set(expected[-5:])))
            tasks.append(HistoryInferredReviewTask(code, dates, tuple(sorted(set(days)))))
        return tuple(tasks)

    def checked(
        self,
        code: str,
        cells: tuple[BaoStockDailyCell, ...],
        sequence: int,
        cutoff: date,
        budget: PriceReviewBudget,
    ) -> None:
        """A durable review result advances age without touching download task state."""
        with (
            SqliteSession(self._path, sqlite_policy=self._sqlite_policy) as connection,
            _watch_budget(connection, budget),
            SqliteTransaction(connection, immediate=True),
        ):
            RecordHistoryInferredSuspensions(
                connection,
                code,
                tuple(cell.trade_date for cell in cells),
                tuple(cell.trade_date for cell in cells if cell.status == "tencent_inferred_suspended"),
                sequence,
                cutoff,
            )
            connection.run_many(
                "UPDATE download_inferred_suspension SET checked_cutoff=? WHERE code=? AND trade_date=?",
                ((EncodeDate(cutoff), code, EncodeDate(cell.trade_date)) for cell in cells if cell.status == "tencent_inferred_suspended"),
            )


__all__ = [
    "HISTORY_INFERRED_REVIEW_SQL",
    "HistoryInferredReviewIndex",
    "HistoryInferredReviewTask",
    "IsHistoryReviewBusy",
    "RecordHistoryInferredSuspensions",
]
