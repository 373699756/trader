"""Durable download metadata in the control database; never contains price payloads."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from trader.download.domain.history_maintenance import HistoryPriceGapReason, HistoryPriceGapSummary
from trader.infra.sqlite_connection import (
    SqliteConnectionOptions,
    SqliteSchema,
    SqliteSession,
    SqliteTransaction,
)

_QFQ_UNUSABLE_RECHECK_SESSIONS = 20


@dataclass(frozen=True)
class HistoryDownloadTask:
    code: str
    dates: tuple[date, ...]
    tencent_done: bool = False
    attempts: int = 0
    failure: str | None = None

    def __post_init__(self) -> None:
        if (
            re.fullmatch(r"[0-9]{6}", self.code) is None
            or self.dates != tuple(sorted(set(self.dates)))
            or any(type(day) is not date for day in self.dates)
            or type(self.tencent_done) is not bool
            or type(self.attempts) is not int
            or not 0 <= self.attempts <= 3
            or (self.failure is not None and re.fullmatch(r"[a-z0-9_]{1,64}", self.failure) is None)
        ):
            raise ValueError("history_download_task_invalid")


@dataclass(frozen=True)
class HistoryDownloadCompletion:
    task: HistoryDownloadTask
    missing: tuple[date, ...]
    failure: str | None
    unusable_qfq_dates: tuple[date, ...] = ()

    def __post_init__(self) -> None:
        if (
            self.missing != tuple(sorted(set(self.missing)))
            or not set(self.missing).issubset(self.task.dates)
            or (self.failure is not None and re.fullmatch(r"[a-z0-9_]{1,64}", self.failure) is None)
            or self.unusable_qfq_dates != tuple(sorted(set(self.unusable_qfq_dates)))
            or not set(self.unusable_qfq_dates).issubset(self.missing)
        ):
            raise ValueError("history_download_completion_invalid")


@dataclass(frozen=True)
class HistoryDownloadBatch:
    sequence: int
    source_hash: str
    calendar_hash: str
    universe_hash: str
    cutoff: date

    def __post_init__(self) -> None:
        if (
            type(self.sequence) is not int
            or self.sequence < 1
            or type(self.cutoff) is not date
            or any(
                re.fullmatch(r"[0-9a-f]{64}", digest) is None
                for digest in (self.source_hash, self.calendar_hash, self.universe_hash)
            )
        ):
            raise ValueError("history_download_batch_invalid")


HISTORY_DOWNLOAD_SCHEMA = SqliteSchema(
    None,
    (
        """
        CREATE TABLE IF NOT EXISTS download_batch (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1), sequence INTEGER NOT NULL,
            source_hash TEXT NOT NULL, calendar_hash TEXT NOT NULL, universe_hash TEXT NOT NULL,
            cutoff TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS download_tasks (
            code TEXT PRIMARY KEY, dates_json TEXT NOT NULL, tencent_done INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 3), failure TEXT
        ) WITHOUT ROWID
        """,
        """
        CREATE TABLE IF NOT EXISTS download_gaps (
            code TEXT NOT NULL, trade_date TEXT NOT NULL, PRIMARY KEY(code,trade_date)
        ) WITHOUT ROWID
        """,
        """
        CREATE TABLE IF NOT EXISTS download_qfq_unusable (
            code TEXT NOT NULL, trade_date TEXT NOT NULL, checked_cutoff TEXT NOT NULL,
            PRIMARY KEY(code,trade_date)
        ) WITHOUT ROWID
        """,
    ),
)


class HistoryDownloadIndex:
    def __init__(self, path: Path) -> None:
        self._path = path

    def pending(self, published_sequence: int) -> HistoryDownloadBatch | None:
        with SqliteSession(self._path) as connection:
            row = connection.run(
                "SELECT sequence,source_hash,calendar_hash,universe_hash,cutoff FROM download_batch"
            ).fetchone()
        if row is None or row[0] <= published_sequence:
            return None
        return HistoryDownloadBatch(row[0], row[1], row[2], row[3], date.fromisoformat(row[4]))

    def begin(self, batch: HistoryDownloadBatch, tasks: tuple[HistoryDownloadTask, ...]) -> None:
        with SqliteSession(self._path) as connection, SqliteTransaction(connection, immediate=True):
            connection.run("DELETE FROM download_tasks")
            connection.run(
                "INSERT OR REPLACE INTO download_batch VALUES(1,?,?,?,?,?)",
                (
                    batch.sequence,
                    batch.source_hash,
                    batch.calendar_hash,
                    batch.universe_hash,
                    batch.cutoff.isoformat(),
                ),
            )
            connection.run_many(
                "INSERT INTO download_tasks(code,dates_json) VALUES(?,?)",
                ((task.code, json.dumps([day.isoformat() for day in task.dates])) for task in tasks),
            )

    def tasks(self) -> tuple[HistoryDownloadTask, ...]:
        with SqliteSession(self._path) as connection:
            rows = connection.run(
                "SELECT code,dates_json,tencent_done,attempts,failure FROM download_tasks ORDER BY code"
            ).fetchall()
        return tuple(
            HistoryDownloadTask(
                code, tuple(date.fromisoformat(day) for day in json.loads(dates)), bool(done), attempts, failure
            )
            for code, dates, done, attempts, failure in rows
        )

    def rebind_context(
        self,
        previous: HistoryDownloadBatch,
        current: HistoryDownloadBatch,
        *,
        removed_codes: frozenset[str] = frozenset(),
        added_tasks: tuple[HistoryDownloadTask, ...] = (),
    ) -> None:
        """Rebind an unpublished batch and reconcile its task population atomically."""
        if previous.sequence != current.sequence or previous.cutoff != current.cutoff:
            raise ValueError("history_download_identity_conflict")
        added_codes = frozenset(task.code for task in added_tasks)
        if len(added_codes) != len(added_tasks) or removed_codes & added_codes:
            raise ValueError("history_download_task_population_conflict")
        with SqliteSession(self._path) as connection, SqliteTransaction(connection, immediate=True):
            stored_codes = frozenset(row[0] for row in connection.run("SELECT code FROM download_tasks"))
            if not removed_codes.issubset(stored_codes) or added_codes & stored_codes:
                raise RuntimeError("history_download_task_population_conflict")
            updated = connection.run(
                """UPDATE download_batch SET source_hash=?,calendar_hash=?,universe_hash=?
                   WHERE singleton=1 AND sequence=? AND cutoff=?
                     AND source_hash=? AND calendar_hash=? AND universe_hash=?
                     AND NOT EXISTS (SELECT 1 FROM active_snapshot WHERE sequence>=?)""",
                (
                    current.source_hash,
                    current.calendar_hash,
                    current.universe_hash,
                    previous.sequence,
                    previous.cutoff.isoformat(),
                    previous.source_hash,
                    previous.calendar_hash,
                    previous.universe_hash,
                    previous.sequence,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError("history_download_identity_conflict")
            if removed_codes:
                ordered_removed_codes = tuple(sorted(removed_codes))
                placeholders = ",".join("?" for _ in ordered_removed_codes)
                connection.run(f"DELETE FROM download_tasks WHERE code IN ({placeholders})", ordered_removed_codes)
            connection.run_many(
                "INSERT INTO download_tasks(code,dates_json) VALUES(?,?)",
                ((task.code, json.dumps([day.isoformat() for day in task.dates])) for task in added_tasks),
            )

    def gap_codes(self) -> frozenset[str]:
        """Read the recovery population once, without one connection per stock."""
        with SqliteSession(self._path) as connection:
            return frozenset(row[0] for row in connection.run("SELECT DISTINCT code FROM download_gaps"))

    def gaps_by_code(self, calendar_dates: tuple[date, ...] | None = None) -> dict[str, tuple[date, ...]]:
        """One bounded-cursor scan per phase; calendar selects only retryable gaps."""
        if calendar_dates is not None and (not calendar_dates or calendar_dates != tuple(sorted(set(calendar_dates)))):
            raise ValueError("history_retry_calendar_invalid")
        positions = {day: index for index, day in enumerate(calendar_dates or ())}
        last = len(positions) - 1
        grouped: dict[str, list[date]] = {}
        with SqliteSession(self._path, SqliteConnectionOptions(read_only=True)) as connection:
            for code, day, checked in connection.run(
                "SELECT g.code,g.trade_date,u.checked_cutoff FROM download_gaps g "
                "LEFT JOIN download_qfq_unusable u ON u.code=g.code AND u.trade_date=g.trade_date "
                "ORDER BY g.code,g.trade_date"
            ):
                if (
                    calendar_dates is not None
                    and checked is not None
                    and (
                        last - positions.get(date.fromisoformat(checked), -_QFQ_UNUSABLE_RECHECK_SESSIONS)
                        < _QFQ_UNUSABLE_RECHECK_SESSIONS
                    )
                ):
                    continue
                grouped.setdefault(code, []).append(date.fromisoformat(day))
        return {code: tuple(days) for code, days in grouped.items()}

    def finish(self, completions: tuple[HistoryDownloadCompletion, ...]) -> None:
        """Advance one durable batch only after all of its monthly price commits succeed."""
        if not completions or len({item.task.code for item in completions}) != len(completions):
            raise ValueError("history_download_completions_invalid")
        with SqliteSession(self._path) as connection, SqliteTransaction(connection, immediate=True):
            cutoff_row = connection.run("SELECT cutoff FROM download_batch WHERE singleton=1").fetchone()
            if cutoff_row is None:
                raise ValueError("history_download_batch_missing")
            checked_cutoff = cutoff_row[0]
            codes = tuple(item.task.code for item in completions)
            placeholders = ",".join("?" for _ in codes)
            gap_codes = {
                row[0]
                for row in connection.run(
                    f"SELECT DISTINCT code FROM download_gaps WHERE code IN ({placeholders})",  # noqa: S608
                    codes,
                )
            }
            for completion in completions:
                task = completion.task
                connection.run_many(
                    "DELETE FROM download_qfq_unusable WHERE code=? AND trade_date=?",
                    ((task.code, day.isoformat()) for day in task.dates),
                )
                connection.run_many(
                    "INSERT INTO download_qfq_unusable VALUES(?,?,?)",
                    ((task.code, day.isoformat(), checked_cutoff) for day in completion.unusable_qfq_dates),
                )
                if task.code in gap_codes:
                    connection.run_many(
                        "DELETE FROM download_gaps WHERE code=? AND trade_date=?",
                        ((task.code, day.isoformat()) for day in task.dates),
                    )
                connection.run_many(
                    "INSERT OR IGNORE INTO download_gaps VALUES(?,?)",
                    ((task.code, day.isoformat()) for day in completion.missing),
                )
                connection.run(
                    "UPDATE download_tasks SET tencent_done=1,failure=? WHERE code=?",
                    (completion.failure, task.code),
                )

    def begin_attempt(self, code: str) -> int:
        with SqliteSession(self._path) as connection, SqliteTransaction(connection, immediate=True):
            connection.run("UPDATE download_tasks SET attempts=attempts+1 WHERE code=? AND attempts<3", (code,))
            return int(connection.run("SELECT attempts FROM download_tasks WHERE code=?", (code,)).require_one()[0])

    def gap_summary(self, codes: tuple[str, ...]) -> HistoryPriceGapSummary:
        selected = frozenset(codes)
        stocks: set[str] = set()
        reasons: dict[str, int] = {}
        dates: list[date] = []
        with SqliteSession(self._path, SqliteConnectionOptions(read_only=True)) as connection:
            rows = connection.run(
                "SELECT g.code,CASE WHEN u.trade_date IS NOT NULL THEN 'tencent_qfq_invalid_price' "
                "ELSE COALESCE(NULLIF(t.failure,'tencent_qfq_invalid_price'),'history_price_pending') "
                "END AS reason,COUNT(*),MIN(g.trade_date),MAX(g.trade_date) "
                "FROM download_gaps g LEFT JOIN download_qfq_unusable u ON u.code=g.code AND u.trade_date=g.trade_date "
                "LEFT JOIN download_tasks t ON t.code=g.code GROUP BY g.code,reason"
            )
            for code, reason, count, first, last in rows:
                if code in selected:
                    stocks.add(code)
                    reasons[reason] = reasons.get(reason, 0) + count
                    dates.extend((date.fromisoformat(first), date.fromisoformat(last)))
        return HistoryPriceGapSummary(
            len(stocks),
            sum(reasons.values()),
            min(dates) if dates else None,
            max(dates) if dates else None,
            tuple(HistoryPriceGapReason(reason, count) for reason, count in sorted(reasons.items())),
        )


__all__ = [
    "HISTORY_DOWNLOAD_SCHEMA",
    "HistoryDownloadBatch",
    "HistoryDownloadCompletion",
    "HistoryDownloadIndex",
    "HistoryDownloadTask",
]
