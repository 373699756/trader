"""Durable unpublished Tencent results for the history download phase."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path

from trader.download.domain.history_sync import HistoryGapSummary
from trader.download.domain.published_history import PublishedHistoryWindow
from trader.download.infra.published_history_codec import (
    decode_published_history_cell_payload,
    encode_published_history_cell,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    sync_identity TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidates (
    code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (code, trade_date)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS outcomes (
    code TEXT PRIMARY KEY,
    failure_reason TEXT
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS gaps (
    code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (code, trade_date, reason)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS supplemented (
    code TEXT PRIMARY KEY
) WITHOUT ROWID;
"""


class HistoryTencentStage:
    def __init__(self, path: Path, sync_identity: str) -> None:
        self._path = path
        self._sync_identity = sync_identity

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self._path, timeout=5.0)) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(_SCHEMA)
            row = connection.execute("SELECT sync_identity FROM metadata WHERE singleton=1").fetchone()
            if row is None:
                connection.execute("INSERT INTO metadata VALUES (1, ?)", (self._sync_identity,))
            elif row[0] != self._sync_identity:
                raise RuntimeError("history_tencent_stage_identity_conflict")

    def contains(self, code: str) -> bool:
        with closing(sqlite3.connect(self._path, timeout=5.0)) as connection:
            return connection.execute("SELECT 1 FROM outcomes WHERE code=?", (code,)).fetchone() is not None

    def save(self, window: PublishedHistoryWindow) -> None:
        with closing(sqlite3.connect(self._path, timeout=5.0)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM candidates WHERE code=?", (window.code,))
            connection.executemany(
                "INSERT INTO candidates(code, trade_date, payload_json) VALUES (?, ?, ?)",
                (
                    (window.code, cell.trade_date.isoformat(), encode_published_history_cell(cell))
                    for cell in window.cells
                ),
            )
            connection.execute(
                "INSERT INTO outcomes(code, failure_reason) VALUES (?, NULL) "
                "ON CONFLICT(code) DO UPDATE SET failure_reason=NULL",
                (window.code,),
            )

    def fail(self, code: str, reason: str) -> None:
        with closing(sqlite3.connect(self._path, timeout=5.0)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM candidates WHERE code=?", (code,))
            connection.execute(
                "INSERT INTO outcomes(code, failure_reason) VALUES (?, ?) "
                "ON CONFLICT(code) DO UPDATE SET failure_reason=excluded.failure_reason",
                (code, reason),
            )

    def read(self, code: str) -> PublishedHistoryWindow:
        with closing(sqlite3.connect(self._path, timeout=5.0)) as connection:
            rows = connection.execute(
                "SELECT trade_date, payload_json FROM candidates WHERE code=? ORDER BY trade_date", (code,)
            ).fetchall()
        return PublishedHistoryWindow(
            code,
            tuple(decode_published_history_cell_payload(code, str(day), str(payload)) for day, payload in rows),
        )

    def save_gap_inventory(self, gaps: Iterable[tuple[str, str, str]]) -> None:
        with closing(sqlite3.connect(self._path, timeout=5.0)) as connection, connection:
            connection.execute("DELETE FROM gaps")
            connection.executemany(
                "INSERT INTO gaps(code, trade_date, reason) VALUES (?, ?, ?)",
                gaps,
            )

    def supplemented_codes(self) -> frozenset[str]:
        with closing(sqlite3.connect(self._path, timeout=5.0)) as connection:
            return frozenset(str(row[0]) for row in connection.execute("SELECT code FROM supplemented"))

    def mark_supplemented(self, codes: tuple[str, ...]) -> None:
        with closing(sqlite3.connect(self._path, timeout=5.0)) as connection, connection:
            connection.executemany("INSERT OR IGNORE INTO supplemented VALUES (?)", ((code,) for code in codes))

    def gap_dates(self, code: str, reason: str) -> tuple[str, ...]:
        with closing(sqlite3.connect(self._path, timeout=5.0)) as connection:
            rows = connection.execute(
                "SELECT trade_date FROM gaps WHERE code=? AND reason=? ORDER BY trade_date", (code, reason)
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def gap_summary(self) -> HistoryGapSummary:
        with closing(sqlite3.connect(self._path, timeout=5.0)) as connection:
            prices = connection.execute("SELECT COUNT(*) FROM gaps WHERE reason='baostock_price_pair'").fetchone()[0]
            failures = connection.execute("SELECT COUNT(*) FROM outcomes WHERE failure_reason IS NOT NULL").fetchone()[
                0
            ]
        return HistoryGapSummary(int(prices), int(failures))

    def price_gap_codes(self) -> tuple[str, ...]:
        with closing(sqlite3.connect(self._path, timeout=5.0)) as connection:
            return tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT code FROM gaps WHERE reason='baostock_price_pair' ORDER BY code"
                )
            )

    def failure_reason(self, code: str) -> str | None:
        with closing(sqlite3.connect(self._path, timeout=5.0)) as connection:
            row = connection.execute("SELECT failure_reason FROM outcomes WHERE code=?", (code,)).fetchone()
        return None if row is None else row[0]

    def clear(self) -> None:
        for path in (self._path, Path(f"{self._path}-wal"), Path(f"{self._path}-shm")):
            path.unlink(missing_ok=True)
