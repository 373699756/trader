"""Durable download metadata in the control database; never contains price payloads."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from pathlib import Path

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


_SCHEMA = """
CREATE TABLE IF NOT EXISTS download_batch (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1), sequence INTEGER NOT NULL,
    source_hash TEXT NOT NULL, calendar_hash TEXT NOT NULL, universe_hash TEXT NOT NULL,
    cutoff TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS download_tasks (
    code TEXT PRIMARY KEY, dates_json TEXT NOT NULL, tencent_done INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 3), failure TEXT
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS download_gaps (
    code TEXT NOT NULL, trade_date TEXT NOT NULL, PRIMARY KEY(code,trade_date)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS download_qfq_unusable (
    code TEXT NOT NULL, trade_date TEXT NOT NULL, checked_cutoff TEXT NOT NULL,
    PRIMARY KEY(code,trade_date)
) WITHOUT ROWID;
"""


class HistoryDownloadIndex:
    def __init__(self, path: Path) -> None:
        self._path = path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5.0)
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(_SCHEMA)

    def pending(self, published_sequence: int) -> HistoryDownloadBatch | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT sequence,source_hash,calendar_hash,universe_hash,cutoff FROM download_batch"
            ).fetchone()
        if row is None or row[0] <= published_sequence:
            return None
        return HistoryDownloadBatch(row[0], row[1], row[2], row[3], date.fromisoformat(row[4]))

    def begin(self, batch: HistoryDownloadBatch, tasks: tuple[HistoryDownloadTask, ...]) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM download_tasks")
            connection.execute(
                "INSERT OR REPLACE INTO download_batch VALUES(1,?,?,?,?,?)",
                (
                    batch.sequence,
                    batch.source_hash,
                    batch.calendar_hash,
                    batch.universe_hash,
                    batch.cutoff.isoformat(),
                ),
            )
            connection.executemany(
                "INSERT INTO download_tasks(code,dates_json) VALUES(?,?)",
                ((task.code, json.dumps([day.isoformat() for day in task.dates])) for task in tasks),
            )

    def tasks(self) -> tuple[HistoryDownloadTask, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT code,dates_json,tencent_done,attempts,failure FROM download_tasks ORDER BY code"
            ).fetchall()
        return tuple(
            HistoryDownloadTask(
                code, tuple(date.fromisoformat(day) for day in json.loads(dates)), bool(done), attempts, failure
            )
            for code, dates, done, attempts, failure in rows
        )

    def rebind_context(self, previous: HistoryDownloadBatch, current: HistoryDownloadBatch) -> None:
        """Replace only an unpublished batch's parents, preserving all durable progress."""
        if previous.sequence != current.sequence or previous.cutoff != current.cutoff:
            raise ValueError("history_download_identity_conflict")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
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

    def gaps(self, code: str) -> tuple[date, ...]:
        with closing(self._connect()) as connection:
            return tuple(
                date.fromisoformat(row[0])
                for row in connection.execute(
                    "SELECT trade_date FROM download_gaps WHERE code=? ORDER BY trade_date",
                    (code,),
                )
            )

    def retryable_gaps(self, code: str, calendar_dates: tuple[date, ...]) -> tuple[date, ...]:
        """Defer confirmed nonpositive qfq dates for a bounded market-session interval."""
        if not calendar_dates:
            raise ValueError("history_retry_calendar_invalid")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT gap.trade_date, unusable.checked_cutoff
                   FROM download_gaps AS gap
                   LEFT JOIN download_qfq_unusable AS unusable
                     ON unusable.code=gap.code AND unusable.trade_date=gap.trade_date
                   WHERE gap.code=? ORDER BY gap.trade_date""",
                (code,),
            ).fetchall()
        if not any(checked_cutoff is not None for _, checked_cutoff in rows):
            return tuple(date.fromisoformat(trade_date) for trade_date, _ in rows)
        if calendar_dates != tuple(sorted(set(calendar_dates))):
            raise ValueError("history_retry_calendar_invalid")
        positions = {day: index for index, day in enumerate(calendar_dates)}
        cutoff_position = len(calendar_dates) - 1
        retryable: list[date] = []
        for trade_date, checked_cutoff in rows:
            day = date.fromisoformat(trade_date)
            if checked_cutoff is None or (
                cutoff_position - positions.get(date.fromisoformat(checked_cutoff), -_QFQ_UNUSABLE_RECHECK_SESSIONS)
                >= _QFQ_UNUSABLE_RECHECK_SESSIONS
            ):
                retryable.append(day)
        return tuple(retryable)

    def gap_codes(self) -> frozenset[str]:
        """Read the recovery population once, without one connection per stock."""
        with closing(self._connect()) as connection:
            return frozenset(row[0] for row in connection.execute("SELECT DISTINCT code FROM download_gaps"))

    def finish(self, completions: tuple[HistoryDownloadCompletion, ...]) -> None:
        """Advance one durable batch only after all of its monthly price commits succeed."""
        if not completions or len({item.task.code for item in completions}) != len(completions):
            raise ValueError("history_download_completions_invalid")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            cutoff_row = connection.execute("SELECT cutoff FROM download_batch WHERE singleton=1").fetchone()
            if cutoff_row is None:
                raise ValueError("history_download_batch_missing")
            checked_cutoff = cutoff_row[0]
            codes = tuple(item.task.code for item in completions)
            placeholders = ",".join("?" for _ in codes)
            gap_codes = {
                row[0]
                for row in connection.execute(
                    f"SELECT DISTINCT code FROM download_gaps WHERE code IN ({placeholders})",  # noqa: S608
                    codes,
                )
            }
            for completion in completions:
                task = completion.task
                connection.executemany(
                    "DELETE FROM download_qfq_unusable WHERE code=? AND trade_date=?",
                    ((task.code, day.isoformat()) for day in task.dates),
                )
                connection.executemany(
                    "INSERT INTO download_qfq_unusable VALUES(?,?,?)",
                    ((task.code, day.isoformat(), checked_cutoff) for day in completion.unusable_qfq_dates),
                )
                if task.code in gap_codes:
                    connection.executemany(
                        "DELETE FROM download_gaps WHERE code=? AND trade_date=?",
                        ((task.code, day.isoformat()) for day in task.dates),
                    )
                connection.executemany(
                    "INSERT OR IGNORE INTO download_gaps VALUES(?,?)",
                    ((task.code, day.isoformat()) for day in completion.missing),
                )
                connection.execute(
                    "UPDATE download_tasks SET tencent_done=1,failure=? WHERE code=?",
                    (completion.failure, task.code),
                )

    def begin_attempt(self, code: str) -> int:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("UPDATE download_tasks SET attempts=attempts+1 WHERE code=? AND attempts<3", (code,))
            return int(connection.execute("SELECT attempts FROM download_tasks WHERE code=?", (code,)).fetchone()[0])

    def unresolved(self, codes: tuple[str, ...]) -> int:
        selected = frozenset(codes)
        if not selected:
            return 0
        with closing(self._connect()) as connection:
            return sum(
                count
                for code, count in connection.execute("SELECT code,COUNT(*) FROM download_gaps GROUP BY code")
                if code in selected
            )


__all__ = ["HistoryDownloadBatch", "HistoryDownloadCompletion", "HistoryDownloadIndex", "HistoryDownloadTask"]
