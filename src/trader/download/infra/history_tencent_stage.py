"""Durable unpublished Tencent results for the history download phase."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Iterator
from contextlib import closing
from datetime import date
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
    price_pair_complete INTEGER NOT NULL CHECK(price_pair_complete IN (0, 1)),
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
    code TEXT PRIMARY KEY,
    unresolved_price_cells INTEGER CHECK(unresolved_price_cells >= 0)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS phase_state (
    name TEXT PRIMARY KEY
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
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(supplemented)")}
            if "unresolved_price_cells" not in columns:
                connection.execute(
                    "ALTER TABLE supplemented ADD COLUMN unresolved_price_cells INTEGER "
                    "CHECK(unresolved_price_cells >= 0)"
                )
            candidate_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(candidates)")}
            if "price_pair_complete" not in candidate_columns:
                connection.execute("ALTER TABLE candidates ADD COLUMN price_pair_complete INTEGER")
            if (
                connection.execute("SELECT 1 FROM candidates WHERE price_pair_complete IS NULL LIMIT 1").fetchone()
                is None
            ):
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS history_tencent_price_gap_idx "
                    "ON candidates(code, trade_date) WHERE price_pair_complete=0"
                )
            row = connection.execute("SELECT sync_identity FROM metadata WHERE singleton=1").fetchone()
            if row is None:
                connection.execute("INSERT INTO metadata VALUES (1, ?)", (self._sync_identity,))
            elif row[0] != self._sync_identity:
                raise RuntimeError("history_tencent_stage_identity_conflict")

    def completed_codes(self) -> frozenset[str]:
        with closing(sqlite3.connect(self._path, timeout=5.0)) as connection:
            return frozenset(str(row[0]) for row in connection.execute("SELECT code FROM outcomes"))

    def save_batch(
        self,
        windows: tuple[PublishedHistoryWindow, ...],
        failures: tuple[tuple[str, str], ...],
    ) -> None:
        codes = tuple(window.code for window in windows) + tuple(code for code, _reason in failures)
        if len(codes) != len(set(codes)):
            raise ValueError("history Tencent stage batch contains duplicate codes")
        with closing(sqlite3.connect(self._path, timeout=5.0)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            for window in windows:
                connection.execute("DELETE FROM candidates WHERE code=?", (window.code,))
                connection.executemany(
                    "INSERT INTO candidates(code, trade_date, payload_json, price_pair_complete) VALUES (?, ?, ?, ?)",
                    (
                        (
                            window.code,
                            cell.trade_date.isoformat(),
                            encode_published_history_cell(cell),
                            int(cell.unadjusted is not None and cell.qfq is not None),
                        )
                        for cell in window.cells
                    ),
                )
                connection.execute(
                    "INSERT INTO outcomes(code, failure_reason) VALUES (?, NULL) "
                    "ON CONFLICT(code) DO UPDATE SET failure_reason=NULL",
                    (window.code,),
                )
            for code, reason in failures:
                connection.execute("DELETE FROM candidates WHERE code=?", (code,))
                connection.execute(
                    "INSERT INTO outcomes(code, failure_reason) VALUES (?, ?) "
                    "ON CONFLICT(code) DO UPDATE SET failure_reason=excluded.failure_reason",
                    (code, reason),
                )

    def read_supplement_context(
        self,
        code: str,
    ) -> tuple[PublishedHistoryWindow, tuple[str, ...], tuple[str, ...]]:
        with closing(sqlite3.connect(self._path, timeout=5.0)) as connection:
            rows = connection.execute(
                "SELECT trade_date, payload_json FROM candidates WHERE code=? ORDER BY trade_date", (code,)
            ).fetchall()
            gaps = connection.execute(
                "SELECT trade_date, reason FROM gaps WHERE code=? "
                "AND reason IN ('baostock_price_pair','baostock_basis_anchor') ORDER BY trade_date",
                (code,),
            ).fetchall()
        window = PublishedHistoryWindow(
            code,
            tuple(decode_published_history_cell_payload(code, str(day), str(payload)) for day, payload in rows),
        )
        missing = tuple(str(day) for day, reason in gaps if reason == "baostock_price_pair")
        anchors = tuple(str(day) for day, reason in gaps if reason == "baostock_basis_anchor")
        return window, missing, anchors

    def save_gap_inventory(self, gaps: Iterable[tuple[str, str, str]]) -> None:
        with closing(sqlite3.connect(self._path, timeout=5.0)) as connection, connection:
            connection.execute("DELETE FROM gaps")
            connection.executemany(
                "INSERT INTO gaps(code, trade_date, reason) VALUES (?, ?, ?)",
                gaps,
            )
            connection.execute("INSERT OR IGNORE INTO phase_state VALUES ('gap_inventory')")

    def iter_gap_inventory(
        self,
        requested: Iterable[tuple[str, tuple[date, ...]]],
        reread_sessions: int,
        cancel_requested: Callable[[], bool],
    ) -> Iterator[tuple[str, str, str]]:
        failed_codes = self.failed_codes()
        missing_by_code = self.price_gap_dates()
        for code, dates in requested:
            if cancel_requested():
                raise RuntimeError("cancelled")
            if not dates:
                continue
            missing = tuple(day.isoformat() for day in dates) if code in failed_codes else missing_by_code.get(code, ())
            for day in missing:
                yield code, day, "baostock_price_pair"
            if missing:
                for anchor_day in self.complete_tail_dates(code, min(5, reread_sessions)):
                    yield code, anchor_day, "baostock_basis_anchor"

    def has_gap_inventory(self) -> bool:
        with closing(sqlite3.connect(self._path, timeout=5.0)) as connection:
            if connection.execute("SELECT 1 FROM phase_state WHERE name='gap_inventory'").fetchone() is not None:
                return True
            return (
                connection.execute("SELECT 1 FROM gaps UNION ALL SELECT 1 FROM supplemented LIMIT 1").fetchone()
                is not None
            )

    def failed_codes(self) -> frozenset[str]:
        with closing(sqlite3.connect(self._path, timeout=5.0)) as connection:
            return frozenset(
                str(row[0]) for row in connection.execute("SELECT code FROM outcomes WHERE failure_reason IS NOT NULL")
            )

    def price_gap_dates(self) -> dict[str, tuple[str, ...]]:
        grouped: dict[str, list[str]] = {}
        with closing(sqlite3.connect(self._path, timeout=5.0)) as connection:
            legacy = connection.execute("SELECT 1 FROM candidates WHERE price_pair_complete IS NULL LIMIT 1").fetchone()
            rows = connection.execute(
                "SELECT code, trade_date FROM candidates WHERE price_pair_complete=0 ORDER BY code, trade_date"
                if legacy is None
                else "SELECT code, trade_date FROM candidates WHERE price_pair_complete=0 OR "
                "(price_pair_complete IS NULL AND (json_type(payload_json, '$.unadjusted')='null' "
                "OR json_type(payload_json, '$.qfq')='null')) ORDER BY code, trade_date"
            )
            for code, trade_date in rows:
                grouped.setdefault(str(code), []).append(str(trade_date))
        return {code: tuple(dates) for code, dates in grouped.items()}

    def complete_tail_dates(self, code: str, limit: int) -> tuple[str, ...]:
        with closing(sqlite3.connect(self._path, timeout=5.0)) as connection:
            legacy = connection.execute(
                "SELECT 1 FROM candidates WHERE code=? AND price_pair_complete IS NULL LIMIT 1", (code,)
            ).fetchone()
            rows = connection.execute(
                "SELECT trade_date FROM candidates WHERE code=? AND price_pair_complete=1 "
                "ORDER BY trade_date DESC LIMIT ?"
                if legacy is None
                else "SELECT trade_date FROM candidates WHERE code=? AND (price_pair_complete=1 OR "
                "(price_pair_complete IS NULL AND json_type(payload_json, '$.unadjusted')!='null' "
                "AND json_type(payload_json, '$.qfq')!='null')) ORDER BY trade_date DESC LIMIT ?",
                (code, limit),
            ).fetchall()
        return tuple(str(row[0]) for row in reversed(rows))

    def supplemented_codes(self) -> frozenset[str]:
        with closing(sqlite3.connect(self._path, timeout=5.0)) as connection:
            return frozenset(str(row[0]) for row in connection.execute("SELECT code FROM supplemented"))

    def mark_supplemented(self, results: tuple[tuple[str, int], ...]) -> None:
        with closing(sqlite3.connect(self._path, timeout=5.0)) as connection, connection:
            connection.executemany(
                "INSERT INTO supplemented(code, unresolved_price_cells) VALUES (?, ?) "
                "ON CONFLICT(code) DO UPDATE SET unresolved_price_cells=excluded.unresolved_price_cells",
                results,
            )

    def unresolved_price_counts(self) -> tuple[dict[str, int], frozenset[str]]:
        with closing(sqlite3.connect(self._path, timeout=5.0)) as connection:
            rows = connection.execute("SELECT code, unresolved_price_cells FROM supplemented").fetchall()
        known = {str(code): int(count) for code, count in rows if count is not None}
        unknown = frozenset(str(code) for code, count in rows if count is None)
        return known, unknown

    def pre_recovery_price_gap_counts(self) -> dict[str, int]:
        with closing(sqlite3.connect(self._path, timeout=5.0)) as connection:
            return {
                str(code): int(count)
                for code, count in connection.execute(
                    "SELECT code, COUNT(*) FROM gaps WHERE reason='baostock_price_pair' GROUP BY code"
                )
            }

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

    def clear(self) -> None:
        for path in (self._path, Path(f"{self._path}-wal"), Path(f"{self._path}-shm")):
            path.unlink(missing_ok=True)
