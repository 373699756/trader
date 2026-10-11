"""Durable unpublished Tencent results for the history download phase."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Generator, Iterable, Iterator
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from trader.download.domain.baostock_daily import BaoStockDailyCell
from trader.download.domain.history_control import HistorySnapshotPartition
from trader.download.domain.history_revision import HistoryRevision
from trader.download.domain.history_sync import HistoryGapSummary, HistoryTencentSummary
from trader.download.domain.published_history import PublishedHistoryWindow
from trader.download.infra.history_revision_codec import decode_history_revision, encode_history_revision
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
CREATE TABLE IF NOT EXISTS qualified_prices (
    trade_date TEXT NOT NULL,
    code TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (trade_date, code)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS outcomes (
    code TEXT PRIMARY KEY,
    failure_reason TEXT
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS recovery_attempts (
    code TEXT PRIMARY KEY,
    attempts INTEGER NOT NULL CHECK(attempts BETWEEN 1 AND 3),
    succeeded INTEGER NOT NULL CHECK(succeeded IN (0,1)),
    failure_reason TEXT
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS recovery_prices (
    code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(code, trade_date)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS legacy_recovery (
    code TEXT PRIMARY KEY
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
CREATE TABLE IF NOT EXISTS sealed_months (
    calendar_year INTEGER NOT NULL,
    calendar_month INTEGER NOT NULL CHECK(calendar_month BETWEEN 1 AND 12),
    relative_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    row_count INTEGER NOT NULL CHECK(row_count >= 0),
    device INTEGER NOT NULL,
    inode INTEGER NOT NULL,
    file_size INTEGER NOT NULL CHECK(file_size >= 0),
    mtime_ns INTEGER NOT NULL,
    ctime_ns INTEGER NOT NULL,
    PRIMARY KEY (calendar_year, calendar_month)
) WITHOUT ROWID;
"""


@dataclass(frozen=True)
class HistoryRecoveryCheckpoint:
    attempts: int = 0
    succeeded: bool = False
    failure_reason: str | None = None


@dataclass(frozen=True)
class HistorySealedMonthCheckpoint:
    reference: HistorySnapshotPartition
    device: int
    inode: int
    file_size: int
    mtime_ns: int
    ctime_ns: int

    def __post_init__(self) -> None:
        if min(self.device, self.inode, self.file_size, self.mtime_ns, self.ctime_ns) < 0:
            raise ValueError("history sealed month checkpoint identity is invalid")

    @classmethod
    def capture(cls, path: Path, reference: HistorySnapshotPartition) -> HistorySealedMonthCheckpoint:
        status = path.stat()
        return cls(
            reference,
            status.st_dev,
            status.st_ino,
            status.st_size,
            status.st_mtime_ns,
            status.st_ctime_ns,
        )

    def matches(self, path: Path) -> bool:
        try:
            status = path.stat()
            wal = Path(f"{path}-wal")
            return (
                status.st_dev,
                status.st_ino,
                status.st_size,
                status.st_mtime_ns,
                status.st_ctime_ns,
            ) == (self.device, self.inode, self.file_size, self.mtime_ns, self.ctime_ns) and (
                not wal.exists() or wal.stat().st_size == 0
            )
        except OSError:
            return False


class HistoryTencentStage:
    def __init__(self, path: Path, sync_identity: str) -> None:
        self._path = path
        self._sync_identity = sync_identity

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self._path, timeout=5.0)) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA)
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
            self._migrate_qualified_candidates(connection)

    def _migrate_qualified_candidates(self, connection: sqlite3.Connection) -> None:
        """Consume the legacy format marker once, in the initialization transaction."""
        markers = connection.execute("SELECT name FROM phase_state WHERE name GLOB 'qualified:*'").fetchall()
        for (marker,) in markers:
            code = marker.removeprefix("qualified:")
            rows = connection.execute(
                "SELECT c.trade_date,c.payload_json,q.payload_json FROM candidates c "
                "LEFT JOIN qualified_prices q ON q.trade_date=c.trade_date AND q.code=c.code "
                "WHERE c.code=? ORDER BY c.trade_date",
                (code,),
            )
            for day, payload, existing in rows:
                try:
                    revision = decode_history_revision(payload)
                except ValueError as exc:
                    raise RuntimeError("history_legacy_candidate_invalid") from exc
                if revision.code != code or revision.trade_date.isoformat() != day:
                    raise RuntimeError("history_legacy_candidate_identity_conflict")
                if existing is not None and existing != payload:
                    raise RuntimeError("history_legacy_qualified_conflict")
            connection.execute(
                "INSERT INTO qualified_prices(trade_date,code,payload_json) "
                "SELECT trade_date,code,payload_json FROM candidates WHERE code=? "
                "ON CONFLICT(trade_date,code) DO NOTHING",
                (code,),
            )
            connection.execute("DELETE FROM candidates WHERE code=?", (code,))
            connection.execute("DELETE FROM phase_state WHERE name=?", (marker,))

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

    def save_qualified(self, batches: tuple[tuple[str, tuple[HistoryRevision, ...], int], ...]) -> None:
        """Atomically move supplier candidates to date-clustered qualified rows in the same database."""
        with closing(sqlite3.connect(self._path, timeout=5.0)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            for code, revisions, missing in batches:
                connection.execute("DELETE FROM candidates WHERE code=?", (code,))
                connection.executemany(
                    "INSERT OR REPLACE INTO qualified_prices VALUES (?,?,?)",
                    ((item.trade_date.isoformat(), code, encode_history_revision(item)) for item in revisions),
                )
                connection.execute("INSERT OR REPLACE INTO supplemented VALUES (?,?)", (code, missing))
                connection.execute("DELETE FROM recovery_prices WHERE code=?", (code,))

    def iter_qualified_month(self, year: int, month: int) -> Generator[HistoryRevision, None, None]:
        start = date(year, month, 1).isoformat()
        end = date(year + (month == 12), 1 if month == 12 else month + 1, 1).isoformat()
        with closing(sqlite3.connect(self._path, timeout=5.0)) as connection:
            cursor = connection.execute(
                "SELECT payload_json FROM qualified_prices "
                "WHERE trade_date>=? AND trade_date<? ORDER BY trade_date,code",
                (start, end),
            )
            for (payload,) in cursor:
                yield decode_history_revision(payload)

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

    def tencent_summary(self) -> HistoryTencentSummary:
        with closing(sqlite3.connect(self._path, timeout=5.0)) as connection:
            row = connection.execute(
                "SELECT COUNT(*), "
                "SUM(CASE WHEN failure_reason IS NULL THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN failure_reason IS NOT NULL THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN failure_reason='history_tencent_overlap_conflict' THEN 1 ELSE 0 END) "
                "FROM outcomes"
            ).fetchone()
        if row is None:
            raise RuntimeError("history_tencent_summary_unavailable")
        processed, succeeded, failed, overlap = row
        return HistoryTencentSummary(
            int(processed or 0),
            int(succeeded or 0),
            int(failed or 0),
            int(overlap or 0),
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

    def sealed_month(self, year: int, month: int) -> HistorySealedMonthCheckpoint | None:
        with closing(sqlite3.connect(self._path, timeout=5.0)) as connection:
            row = connection.execute(
                "SELECT relative_path, sha256, row_count, device, inode, file_size, mtime_ns, ctime_ns "
                "FROM sealed_months WHERE calendar_year=? AND calendar_month=?",
                (year, month),
            ).fetchone()
        if row is None:
            return None
        reference = HistorySnapshotPartition(str(row[0]), str(row[1]), int(row[2]))
        if reference.relative_path != f"partitions/{year:04d}/{month:02d}.sqlite3":
            raise ValueError("history sealed month checkpoint path is invalid")
        return HistorySealedMonthCheckpoint(reference, *(int(value) for value in row[3:]))

    def save_sealed_month(self, year: int, month: int, value: HistorySealedMonthCheckpoint) -> None:
        with closing(sqlite3.connect(self._path, timeout=5.0)) as connection, connection:
            connection.execute(
                "INSERT INTO sealed_months(calendar_year, calendar_month, relative_path, sha256, row_count, "
                "device, inode, file_size, mtime_ns, ctime_ns) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(calendar_year, calendar_month) DO UPDATE SET "
                "relative_path=excluded.relative_path, sha256=excluded.sha256, row_count=excluded.row_count, "
                "device=excluded.device, inode=excluded.inode, file_size=excluded.file_size, "
                "mtime_ns=excluded.mtime_ns, ctime_ns=excluded.ctime_ns",
                (
                    year,
                    month,
                    value.reference.relative_path,
                    value.reference.sha256,
                    value.reference.row_count,
                    value.device,
                    value.inode,
                    value.file_size,
                    value.mtime_ns,
                    value.ctime_ns,
                ),
            )

    def discard_sealed_month(self, year: int, month: int) -> None:
        with closing(sqlite3.connect(self._path, timeout=5.0)) as connection, connection:
            connection.execute(
                "DELETE FROM sealed_months WHERE calendar_year=? AND calendar_month=?",
                (year, month),
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

    def prioritized_recovery_codes(self) -> tuple[str, ...]:
        gaps = self.price_gap_codes()
        failed_set = self.failed_codes()
        return tuple(code for code in gaps if code in failed_set) + tuple(
            code for code in gaps if code not in failed_set
        )

    def recovery_checkpoint(self, code: str) -> HistoryRecoveryCheckpoint:
        with closing(sqlite3.connect(self._path, timeout=5.0)) as connection:
            row = connection.execute(
                "SELECT attempts,succeeded,failure_reason FROM recovery_attempts WHERE code=?", (code,)
            ).fetchone()
        return HistoryRecoveryCheckpoint(int(row[0]), bool(row[1]), row[2]) if row else HistoryRecoveryCheckpoint()

    def begin_recovery_attempt(self, code: str) -> int:
        """Count dispatch durably before contacting the supplier, including interrupted attempts."""
        with closing(sqlite3.connect(self._path, timeout=5.0)) as connection, connection:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT attempts,succeeded FROM recovery_attempts WHERE code=?", (code,)
            ).fetchone()
            if row is not None and (row[0] >= 3 or row[1]):
                raise RuntimeError("history_recovery_attempts_exhausted")
            attempt = 1 if row is None else int(row[0]) + 1
            connection.execute(
                "INSERT OR REPLACE INTO recovery_attempts VALUES (?,?,0,'history_recovery_interrupted')",
                (code, attempt),
            )
        return attempt

    def finish_recovery_attempt(
        self, code: str, failure_reason: str | None, revisions: tuple[HistoryRevision, ...] = ()
    ) -> None:
        if (failure_reason is None) != bool(revisions) or any(item.code != code for item in revisions):
            raise ValueError("history_recovery_result_invalid")
        with closing(sqlite3.connect(self._path, timeout=5.0)) as connection, connection:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM recovery_prices WHERE code=?", (code,))
            connection.executemany(
                "INSERT INTO recovery_prices VALUES (?,?,?)",
                ((code, item.trade_date.isoformat(), encode_history_revision(item)) for item in revisions),
            )
            cursor = connection.execute(
                "UPDATE recovery_attempts SET succeeded=?,failure_reason=? WHERE code=?",
                (int(failure_reason is None), failure_reason, code),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("history_recovery_attempt_missing")

    def recovery_cells(self, code: str) -> tuple[BaoStockDailyCell, ...]:
        with closing(sqlite3.connect(self._path, timeout=5.0)) as connection:
            rows = connection.execute(
                "SELECT payload_json FROM recovery_prices WHERE code=? ORDER BY trade_date", (code,)
            )
            cells = tuple(decode_history_revision(row[0]).cell for row in rows)
        if any(cell.code != code for cell in cells):
            raise RuntimeError("history_recovery_checkpoint_identity_conflict")
        return cells

    def is_supplemented(self, code: str) -> bool:
        with closing(sqlite3.connect(self._path, timeout=5.0)) as connection:
            return connection.execute("SELECT 1 FROM supplemented WHERE code=?", (code,)).fetchone() is not None

    def legacy_recovery_codes(self) -> frozenset[str]:
        with closing(sqlite3.connect(self._path, timeout=5.0)) as connection:
            return frozenset(
                row[0]
                for row in connection.execute(
                    "SELECT code FROM supplemented WHERE (unresolved_price_cells IS NULL OR unresolved_price_cells>0) "
                    "AND code NOT IN (SELECT code FROM recovery_attempts) UNION SELECT code FROM legacy_recovery"
                )
            )

    def qualified_cells(self, code: str, dates: tuple[date, ...]) -> tuple[BaoStockDailyCell, ...]:
        with closing(sqlite3.connect(self._path, timeout=5.0)) as connection:
            return tuple(
                decode_history_revision(row[0]).cell
                for day in dates
                for row in connection.execute(
                    "SELECT payload_json FROM qualified_prices WHERE trade_date=? AND code=?", (day.isoformat(), code)
                )
            )

    def reopen_legacy_recovery(self, window: PublishedHistoryWindow) -> None:
        with closing(sqlite3.connect(self._path, timeout=5.0)) as connection, connection:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute("SELECT 1 FROM legacy_recovery WHERE code=?", (window.code,)).fetchone()
            if exists:
                return
            connection.execute("INSERT INTO legacy_recovery VALUES (?)", (window.code,))
            connection.execute("DELETE FROM supplemented WHERE code=?", (window.code,))
            connection.execute("DELETE FROM gaps WHERE code=?", (window.code,))
            missing = tuple(
                cell for cell in window.cells if cell.status not in {"complete", "supplier_marked_suspended"}
            )
            anchors = tuple(cell for cell in window.cells if cell.status == "complete")[-5:]
            connection.executemany(
                "INSERT INTO gaps VALUES (?,?,'baostock_price_pair')",
                ((window.code, cell.trade_date.isoformat()) for cell in missing),
            )
            if missing:
                connection.executemany(
                    "INSERT INTO gaps VALUES (?,?,'baostock_basis_anchor')",
                    ((window.code, cell.trade_date.isoformat()) for cell in anchors),
                )
            for cell in window.cells:
                connection.execute(
                    "INSERT OR REPLACE INTO candidates VALUES (?,?,?,?)",
                    (
                        window.code,
                        cell.trade_date.isoformat(),
                        encode_published_history_cell(cell),
                        int(cell.unadjusted is not None and cell.qfq is not None),
                    ),
                )
                connection.execute(
                    "DELETE FROM qualified_prices WHERE trade_date=? AND code=?",
                    (cell.trade_date.isoformat(), window.code),
                )

    def clear(self) -> None:
        for path in (self._path, Path(f"{self._path}-wal"), Path(f"{self._path}-shm")):
            path.unlink(missing_ok=True)
