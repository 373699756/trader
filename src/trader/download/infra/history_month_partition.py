"""SQLite boundary for one immutable-history calendar month."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
from collections.abc import Callable, Collection, Generator, Iterable, Iterator
from contextlib import closing, nullcontext
from dataclasses import dataclass
from datetime import date
from itertools import islice
from pathlib import Path
from typing import Literal, cast

from trader.download.domain.baostock_daily import BaoStockBoard, BaoStockCellStatus
from trader.download.domain.history_control import HistorySnapshotPartition
from trader.download.domain.history_revision import HistoryRevision, HistoryTrainingPoint, HistoryTrainingRow
from trader.download.domain.published_history import PublishedHistoryCell
from trader.download.infra.history_partition_proof import calculate_history_proof
from trader.download.infra.history_revision_codec import (
    decode_history_revision,
    encode_history_revision,
)
from trader.download.infra.history_sqlite_activity import HistorySqliteActivity
from trader.download.infra.history_sqlite_read_connection import open_history_month_reader
from trader.download.infra.published_history_codec import decode_published_history_cell

_SCHEMA_IDENTITY = "history_direct_prices"
_CODE = re.compile(r"^[0-9]{6}$")
_HASH_CHUNK_BYTES = 1024 * 1024
_CACHE_RELEASE_BYTES = 16 * 1024 * 1024
_VERIFICATION_MMAP_BYTES = 256 * 1024 * 1024
HistoryPartitionVerificationPhase = Literal["hash", "integrity", "row_count", "logical"]
HistoryPartitionVerificationProgress = Callable[[int, int, HistoryPartitionVerificationPhase], None]
_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    schema_identity TEXT NOT NULL,
    calendar_year INTEGER NOT NULL,
    calendar_month INTEGER NOT NULL CHECK(calendar_month BETWEEN 1 AND 12)
);
CREATE TABLE IF NOT EXISTS daily_records (
    trade_date TEXT NOT NULL,
    code TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    first_seen_sequence INTEGER NOT NULL CHECK(first_seen_sequence > 0),
    board TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    PRIMARY KEY (trade_date, code, revision_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS daily_observations (
    trade_date TEXT NOT NULL,
    code TEXT NOT NULL,
    sync_sequence INTEGER NOT NULL CHECK(sync_sequence > 0),
    revision_id TEXT NOT NULL,
    PRIMARY KEY (trade_date, code, sync_sequence),
    FOREIGN KEY (trade_date, code, revision_id)
        REFERENCES daily_records(trade_date, code, revision_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS history_month_code_date_idx
ON daily_records(code, trade_date, first_seen_sequence DESC, revision_id);
CREATE INDEX IF NOT EXISTS history_month_date_board_code_idx
ON daily_records(trade_date, board, code, first_seen_sequence DESC, revision_id);
CREATE INDEX IF NOT EXISTS history_month_observation_code_date_idx
ON daily_observations(code, trade_date, sync_sequence DESC, revision_id);
CREATE INDEX IF NOT EXISTS history_month_sequence_idx ON daily_observations(sync_sequence);
CREATE INDEX IF NOT EXISTS history_month_price_gap_idx
ON daily_records(code, trade_date, revision_id)
WHERE json_extract(payload_json, '$.cell.status') IN ('unknown_missing','qfq_missing','unadjusted_missing');
"""
_REVISION_COLUMNS = """records.trade_date, records.code, records.revision_id, records.first_seen_sequence,
       records.board, records.payload_json, records.content_hash"""
_DAILY_COLUMNS = "records.trade_date, records.code, json_extract(records.payload_json, '$.cell')"
_LATEST_WINDOW = """
WITH latest AS (
    SELECT trade_date, code, revision_id,
           ROW_NUMBER() OVER (
               PARTITION BY trade_date, code
               ORDER BY sync_sequence DESC
           ) AS revision_rank
    FROM daily_observations
    WHERE sync_sequence <= ? AND trade_date BETWEEN ? AND ?
{observation_filter}
)
SELECT {columns}
FROM latest
JOIN daily_records AS records
  ON records.trade_date = latest.trade_date
 AND records.code = latest.code
 AND records.revision_id = latest.revision_id
WHERE latest.revision_rank = 1
{record_filter}
ORDER BY records.trade_date, records.code
"""
_LATEST_RANGE_SQL = _LATEST_WINDOW.format(columns=_REVISION_COLUMNS, observation_filter="", record_filter="")
_RANGE_BY_CODE = """
SELECT {columns}
FROM daily_records AS records INDEXED BY history_month_code_date_idx
WHERE records.trade_date BETWEEN ? AND ?
  AND records.revision_id = (
      SELECT observed.revision_id
      FROM daily_observations AS observed
      WHERE observed.code = records.code AND observed.trade_date = records.trade_date
        AND observed.sync_sequence <= ?
      ORDER BY observed.sync_sequence DESC
      LIMIT 1
  )
ORDER BY records.code, records.trade_date
"""
_LATEST_RANGE_BY_CODE_SQL = _RANGE_BY_CODE.format(columns=_REVISION_COLUMNS)
_PUBLISHED_RANGE_BY_CODE_SQL = _RANGE_BY_CODE.format(columns=_DAILY_COLUMNS)
_LATEST_SHADOW_SQL = """
WITH latest AS (
    SELECT trade_date, code, revision_id,
           ROW_NUMBER() OVER (
               PARTITION BY trade_date, code
               ORDER BY sync_sequence DESC
           ) AS revision_rank
    FROM daily_observations
    WHERE sync_sequence <= ? AND trade_date BETWEEN ? AND ?
)
SELECT records.trade_date, records.code,
       json_extract(records.payload_json, '$.cell.unadjusted') IS NOT NULL,
       json_extract(records.payload_json, '$.cell.qfq') IS NOT NULL
FROM latest
JOIN daily_records AS records
  ON records.trade_date = latest.trade_date
 AND records.code = latest.code
 AND records.revision_id = latest.revision_id
WHERE latest.revision_rank = 1
ORDER BY records.code, records.trade_date
"""
_LATEST_CODE_SQL = _LATEST_WINDOW.format(
    columns=_REVISION_COLUMNS,
    observation_filter="      AND code = ?",
    record_filter="",
)
_LATEST_BOARD_SQL = _LATEST_WINDOW.format(
    columns=_REVISION_COLUMNS,
    observation_filter="",
    record_filter="  AND records.board = ?",
)
_LATEST_CODE_BOARD_SQL = _LATEST_WINDOW.format(
    columns=_REVISION_COLUMNS,
    observation_filter="      AND code = ?",
    record_filter="  AND records.board = ?",
)
_PUBLISHED_CODE_SQL = _LATEST_WINDOW.format(
    columns=_DAILY_COLUMNS, observation_filter="      AND code = ?", record_filter=""
)
_TRAINING_RANGE_SQL = """
SELECT records.trade_date,
       records.code,
       records.board,
       json_extract(records.payload_json, '$.cell.status'),
       json_extract(records.payload_json, '$.cell.unadjusted.trading_status'),
       json_extract(records.payload_json, '$.cell.qfq.trading_status'),
       json_extract(records.payload_json, '$.cell.qfq.close_price'),
       json_extract(records.payload_json, '$.cell.qfq.amount')
FROM daily_records AS records
WHERE records.trade_date BETWEEN ? AND ?
  AND records.revision_id = (
      SELECT observed.revision_id
      FROM daily_observations AS observed
      WHERE observed.code = records.code AND observed.trade_date = records.trade_date
        AND observed.sync_sequence <= ?
      ORDER BY observed.sync_sequence DESC
      LIMIT 1
  )
ORDER BY records.trade_date, records.code
"""
_COUNT_CODE_BATCH_SIZE = 500
_ALL_REVISION_SCAN_SQL = (
    "SELECT trade_date, code, revision_id, first_seen_sequence, board, payload_json, content_hash FROM daily_records"
)
_EXISTING_OBSERVATIONS_SQL = """
SELECT observations.trade_date, observations.code, observations.sync_sequence, observations.revision_id,
       json_extract(records.payload_json, '$.cell.status')
FROM requested_history_revisions AS requested
CROSS JOIN daily_observations AS observations
LEFT JOIN daily_records AS records
  ON records.trade_date=observations.trade_date
 AND records.code=observations.code
 AND records.revision_id=observations.revision_id
WHERE observations.trade_date=requested.trade_date
  AND observations.code=requested.code
  AND observations.sync_sequence=requested.sync_sequence
"""
_EXISTING_REVISIONS_SQL = """
SELECT records.trade_date, records.code, records.revision_id, records.first_seen_sequence,
       records.payload_json, records.content_hash
FROM requested_history_revisions AS requested
CROSS JOIN daily_records AS records
WHERE records.trade_date=requested.trade_date
  AND records.code=requested.code
  AND records.revision_id=requested.revision_id
"""
_LATEST_SEQUENCES_SQL = """
SELECT requested.trade_date, requested.code, MAX(observations.sync_sequence)
FROM requested_history_revisions AS requested
CROSS JOIN daily_observations AS observations
WHERE observations.trade_date=requested.trade_date
  AND observations.code=requested.code
GROUP BY requested.trade_date, requested.code
"""


@dataclass(frozen=True)
class _EncodedRevision:
    value: HistoryRevision
    trade_date: str
    payload_json: str

    @property
    def observation_key(self) -> tuple[str, str, int]:
        return self.trade_date, self.value.code, self.value.first_seen_sequence

    @property
    def revision_key(self) -> tuple[str, str, str]:
        return self.trade_date, self.value.code, self.value.revision_id


class HistoryMonthPartitionError(RuntimeError):
    """A month partition cannot be trusted or queried."""


class HistoryMonthPartitionConflictError(HistoryMonthPartitionError):
    """An immutable logical row conflicts with an existing revision."""


class HistoryMonthPrices:
    def __init__(  # noqa: PLR0913 - physical identity and optional diagnostic/cancellation boundaries
        self,
        path: Path,
        calendar_year: int,
        calendar_month: int,
        *,
        statement_trace: Callable[[str], None] | None = None,
        activity: HistorySqliteActivity | None = None,
    ) -> None:
        if calendar_year < 1990 or not 1 <= calendar_month <= 12:
            raise ValueError("history month partition identity is invalid")
        self._path = path
        self._calendar_year = calendar_year
        self._calendar_month = calendar_month
        self._statement_trace = statement_trace
        self._activity = activity

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        new_database = not self._path.exists() or self._path.stat().st_size == 0
        try:
            with closing(sqlite3.connect(self._path, timeout=5.0)) as connection, connection:
                if self._statement_trace is not None:
                    connection.set_trace_callback(self._statement_trace)
                if new_database:
                    connection.execute("PRAGMA page_size=8192")
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                # One durable schema transaction, not one FULL fsync per table/index.
                connection.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA)
                connection.execute(
                    "INSERT OR IGNORE INTO metadata"
                    "(singleton, schema_identity, calendar_year, calendar_month) VALUES (1, ?, ?, ?)",
                    (_SCHEMA_IDENTITY, self._calendar_year, self._calendar_month),
                )
                self._require_metadata(connection)
                self._require_schema(connection)
        except (sqlite3.Error, ValueError) as exc:
            raise HistoryMonthPartitionError("history month partition initialization failed") from exc
        try:
            _fsync_file(self._path)
            _fsync_directory(self._path.parent)
        except OSError as exc:
            raise HistoryMonthPartitionError("history month partition durability sync failed") from exc

    def changed_revisions(self, revisions: tuple[HistoryRevision, ...]) -> tuple[HistoryRevision, ...]:
        """Compare bounded incoming keys with their latest visible price identity, read-only."""
        return tuple(item for batch in self.iter_changed_batches(revisions) for item in batch)

    def has_observations_after(self, sequence: int) -> bool:
        with closing(self._read_connection()) as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM daily_observations WHERE sync_sequence>? LIMIT 1", (sequence,)
                ).fetchone()
                is not None
            )

    def iter_changed_batches(
        self, revisions: Iterable[HistoryRevision]
    ) -> Generator[tuple[HistoryRevision, ...], None, None]:
        incoming = iter(revisions)
        with closing(self._read_connection()) as connection:
            self._require_metadata(connection)
            while batch := tuple(islice(incoming, 200)):
                placeholders = ",".join("(?,?,?)" for _ in batch)
                rows = connection.execute(
                    "WITH requested(day,code,sequence) AS (VALUES " + placeholders + ") "
                    "SELECT (SELECT o.revision_id FROM daily_observations o "
                    "WHERE o.trade_date=r.day AND o.code=r.code AND o.sync_sequence<=r.sequence "
                    "ORDER BY o.sync_sequence DESC LIMIT 1) FROM requested r",
                    tuple(
                        v for item in batch for v in (item.trade_date.isoformat(), item.code, item.first_seen_sequence)
                    ),
                ).fetchall()
                changed = tuple(item for item, row in zip(batch, rows, strict=True) if item.revision_id != row[0])
                if changed:
                    yield changed

    def save_revision_batches(self, revisions: Iterable[HistoryRevision]) -> None:
        """Keep one month connection warm; bound each durable transaction to 4096 rows."""
        incoming = iter(revisions)
        try:
            with (
                closing(self._write_connection()) as connection,
                self._activity.watch(connection) if self._activity else nullcontext(),
            ):
                self._require_metadata(connection)
                while batch := tuple(islice(incoming, 4096)):
                    if shutil.disk_usage(self._path.parent).free < 64 * 1024**2:
                        raise HistoryMonthPartitionError("disk_space_insufficient")
                    with connection:
                        connection.execute("BEGIN IMMEDIATE")
                        self._save_batch(connection, self._prepare_revisions(batch))
                        connection.execute("DROP TABLE requested_history_revisions")
        except sqlite3.Error as exc:
            raise HistoryMonthPartitionError("history month revision write failed") from exc

    def open_write_session(
        self, *, fresh: bool = False, pending_sequence: int | None = None
    ) -> HistoryMonthWriteSession:
        """Open one thread-confined writer reused across durable batch transactions."""
        return HistoryMonthWriteSession(self, fresh=fresh, pending_sequence=pending_sequence)

    def read_price_keys(self, keys: tuple[tuple[str, date], ...], sequence: int) -> tuple[HistoryRevision, ...]:
        """Read a bounded stock batch through exact observation/record keys on one connection."""
        result: list[HistoryRevision] = []
        with (
            closing(self._read_connection()) as connection,
            self._activity.watch(connection) if self._activity else nullcontext(),
        ):
            self._require_metadata(connection)
            for start in range(0, len(keys), 200):
                if self._activity is not None:
                    self._activity.tick()
                batch = keys[start : start + 200]
                values = ",".join("(?,?)" for _ in batch)
                sql = (
                    "WITH requested(code,day) AS (VALUES "
                    + values
                    + ") SELECT "
                    + _REVISION_COLUMNS
                    + " FROM requested AS k CROSS JOIN daily_records AS records "
                    "WHERE records.trade_date=k.day AND records.code=k.code AND records.revision_id=("
                    "SELECT o.revision_id FROM daily_observations o WHERE o.trade_date=k.day AND o.code=k.code "
                    "AND o.sync_sequence<=? ORDER BY o.sync_sequence DESC LIMIT 1)"
                )
                result.extend(
                    _decode_row(row)
                    for row in connection.execute(
                        sql, (*tuple(value for code, day in batch for value in (code, day.isoformat())), sequence)
                    )
                )
        return tuple(result)

    def contains_before(self, first_date: date) -> bool:
        with closing(self._read_connection()) as connection:
            self._require_metadata(connection)
            return (
                connection.execute(
                    "SELECT 1 FROM daily_records WHERE trade_date<? LIMIT 1", (first_date.isoformat(),)
                ).fetchone()
                is not None
            )

    def save_revisions(self, revisions: Iterable[HistoryRevision]) -> None:
        prepared = self._prepare_revisions(revisions)
        if not prepared:
            return
        try:
            with closing(self._write_connection()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                self._require_metadata(connection)
                self._save_batch(connection, prepared)
        except HistoryMonthPartitionConflictError:
            raise
        except sqlite3.Error as exc:
            raise HistoryMonthPartitionError("history month revision write failed") from exc

    def read_day(
        self,
        trade_date: date,
        *,
        snapshot_sequence: int,
        board: str | None = None,
    ) -> tuple[HistoryRevision, ...]:
        if trade_date.year != self._calendar_year or trade_date.month != self._calendar_month:
            raise ValueError("history day does not belong to the target month")
        return tuple(self.iter_range(trade_date, trade_date, snapshot_sequence=snapshot_sequence, board=board))

    def read_code(
        self,
        code: str,
        start: date,
        end: date,
        *,
        snapshot_sequence: int,
    ) -> tuple[HistoryRevision, ...]:
        if _CODE.fullmatch(code) is None:
            raise ValueError("history month code is invalid")
        return tuple(self.iter_range(start, end, snapshot_sequence=snapshot_sequence, code=code))

    def missing_price_dates(self, snapshot_sequence: int) -> tuple[tuple[str, date], ...]:
        """Project only unresolved latest observations, without decoding daily payloads."""
        with (
            closing(self._read_connection()) as connection,
            self._activity.watch(connection) if self._activity else nullcontext(),
        ):
            self._require_metadata(connection)
            rows = connection.execute(
                "SELECT r.code,r.trade_date FROM daily_records r INDEXED BY history_month_price_gap_idx "
                "WHERE json_extract(r.payload_json, '$.cell.status') IN "
                "('unknown_missing','qfq_missing','unadjusted_missing') "
                "AND r.revision_id=(SELECT o.revision_id FROM daily_observations o "
                "WHERE o.code=r.code AND o.trade_date=r.trade_date AND o.sync_sequence<=? "
                "ORDER BY o.sync_sequence DESC LIMIT 1) ORDER BY r.code,r.trade_date",
                (snapshot_sequence,),
            ).fetchall()
        return tuple((str(code), date.fromisoformat(str(day))) for code, day in rows)

    def missing_price_counts(self, snapshot_sequence: int, codes: Collection[str]) -> dict[str, int]:
        """Count unresolved latest observations by recovery code."""

        ordered_codes = tuple(sorted(set(codes)))
        if snapshot_sequence < 1 or any(_CODE.fullmatch(code) is None for code in ordered_codes):
            raise ValueError("history month missing-price count input is invalid")
        if not ordered_codes:
            return {}
        counts: dict[str, int] = {}
        with closing(self._read_connection()) as connection:
            self._require_metadata(connection)
            for offset in range(0, len(ordered_codes), _COUNT_CODE_BATCH_SIZE):
                batch = ordered_codes[offset : offset + _COUNT_CODE_BATCH_SIZE]
                placeholders = ",".join("?" for _code in batch)
                rows = connection.execute(
                    "SELECT records.code, COUNT(*) FROM daily_records AS records "
                    "INDEXED BY history_month_price_gap_idx "
                    "WHERE records.code IN (" + placeholders + ") "
                    "AND json_extract(records.payload_json, '$.cell.status') IN "
                    "('unknown_missing','qfq_missing','unadjusted_missing') "
                    "AND records.revision_id=(SELECT observations.revision_id FROM daily_observations AS observations "
                    "WHERE observations.code=records.code AND observations.trade_date=records.trade_date "
                    "AND observations.sync_sequence<=? ORDER BY observations.sync_sequence DESC LIMIT 1) "
                    "GROUP BY records.code",
                    (*batch, snapshot_sequence),
                ).fetchall()
                for code, count in rows:
                    counts[str(code)] = counts.get(str(code), 0) + cast(int, count)
        return counts

    def count_range(
        self,
        start: date,
        end: date,
        *,
        snapshot_sequence: int,
        codes: Collection[str],
    ) -> int:
        if start > end or snapshot_sequence < 1:
            raise ValueError("history month count range is invalid")
        ordered_codes = tuple(sorted(set(codes)))
        if any(len(code) != 6 or not code.isdigit() for code in ordered_codes):
            raise ValueError("history month count codes are invalid")
        if not ordered_codes:
            return 0
        try:
            with closing(self._read_connection()) as connection:
                self._require_metadata(connection)
                count = 0
                for offset in range(0, len(ordered_codes), _COUNT_CODE_BATCH_SIZE):
                    batch = ordered_codes[offset : offset + _COUNT_CODE_BATCH_SIZE]
                    placeholders = ",".join("?" for _code in batch)
                    row = connection.execute(
                        "SELECT COUNT(*) FROM ("
                        "SELECT trade_date, code FROM daily_observations "
                        "WHERE sync_sequence <= ? AND trade_date BETWEEN ? AND ? "
                        f"AND code IN ({placeholders}) GROUP BY trade_date, code"
                        ")",
                        (snapshot_sequence, start.isoformat(), end.isoformat(), *batch),
                    ).fetchone()
                    if row is None:
                        raise HistoryMonthPartitionError("history month count is unavailable")
                    count += cast(int, row[0])
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise HistoryMonthPartitionError("history month count failed") from exc
        return count

    def prune_before(self, first_date: date) -> None:
        """Remove rows outside a new rolling window from a mutable side copy."""
        if (first_date.year, first_date.month) != (self._calendar_year, self._calendar_month):
            raise ValueError("history rolling-window boundary does not belong to the target month")
        try:
            with closing(self._write_connection()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                self._require_metadata(connection)
                connection.execute("DELETE FROM daily_observations WHERE trade_date < ?", (first_date.isoformat(),))
                connection.execute(
                    "DELETE FROM daily_records WHERE NOT EXISTS ("
                    "SELECT 1 FROM daily_observations AS observations "
                    "WHERE observations.trade_date=daily_records.trade_date "
                    "AND observations.code=daily_records.code "
                    "AND observations.revision_id=daily_records.revision_id)",
                )
        except sqlite3.Error as exc:
            raise HistoryMonthPartitionError("history month rolling-window prune failed") from exc

    def iter_range(
        self,
        start: date,
        end: date,
        *,
        snapshot_sequence: int,
        code: str | None = None,
        board: str | None = None,
    ) -> Iterator[HistoryRevision]:
        if start > end or snapshot_sequence < 1:
            raise ValueError("history month query range is invalid")
        if code is not None and _CODE.fullmatch(code) is None:
            raise ValueError("history month code is invalid")
        if board is not None and board not in {"main", "chinext", "star"}:
            raise ValueError("history month board is invalid")
        try:
            with closing(self._read_connection()) as connection:
                self._require_metadata(connection)
                query, filters = _latest_query(code, board)
                cursor = connection.execute(
                    query,
                    (snapshot_sequence, start.isoformat(), end.isoformat(), *filters),
                )
                while rows := cursor.fetchmany(512):
                    for row in rows:
                        yield _decode_row(row)
        except HistoryMonthPartitionError:
            raise
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise HistoryMonthPartitionError("history month query failed") from exc

    def iter_range_by_code(
        self,
        start: date,
        end: date,
        *,
        snapshot_sequence: int,
    ) -> Iterator[HistoryRevision]:
        """Stream a partition in code/date order for bounded cross-month merging."""

        if start > end or snapshot_sequence < 1:
            raise ValueError("history month query range is invalid")
        try:
            with closing(self._read_connection()) as connection:
                self._require_metadata(connection)
                cursor = connection.execute(
                    _LATEST_RANGE_BY_CODE_SQL,
                    (start.isoformat(), end.isoformat(), snapshot_sequence),
                )
                while rows := cursor.fetchmany(512):
                    for row in rows:
                        yield _decode_row(row)
        except HistoryMonthPartitionError:
            raise
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise HistoryMonthPartitionError("history month code-ordered query failed") from exc

    def iter_training_rows(
        self,
        start: date,
        end: date,
        *,
        snapshot_sequence: int,
    ) -> Iterator[HistoryTrainingRow]:
        """Stream the narrow training projection without revision hashes or temporary sorts."""

        if start > end or snapshot_sequence < 1:
            raise ValueError("history month training range is invalid")
        try:
            with closing(self._read_connection()) as connection:
                self._require_metadata(connection)
                cursor = connection.execute(
                    _TRAINING_RANGE_SQL,
                    (start.isoformat(), end.isoformat(), snapshot_sequence),
                )
                while rows := cursor.fetchmany(4_096):
                    for row in rows:
                        yield _decode_training_row(row)
        except HistoryMonthPartitionError:
            raise
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise HistoryMonthPartitionError("history month training query failed") from exc

    def iter_published_cells(
        self,
        start: date,
        end: date,
        *,
        snapshot_sequence: int,
        code: str | None = None,
    ) -> Iterator[PublishedHistoryCell]:
        """Project sealed daily facts without materializing full revision objects."""
        if start > end or snapshot_sequence < 1 or (code is not None and _CODE.fullmatch(code) is None):
            raise ValueError("published daily query range is invalid")
        query = _PUBLISHED_RANGE_BY_CODE_SQL if code is None else _PUBLISHED_CODE_SQL
        parameters = (
            (start.isoformat(), end.isoformat(), snapshot_sequence)
            if code is None
            else (snapshot_sequence, start.isoformat(), end.isoformat(), code)
        )
        try:
            with closing(self._read_connection()) as connection:
                self._require_metadata(connection)
                cursor = connection.execute(query, parameters)
                while rows := cursor.fetchmany(512):
                    for row in rows:
                        yield decode_published_history_cell(row)
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise HistoryMonthPartitionError("published daily query failed") from exc

    def iter_shadow_facts(
        self,
        start: date,
        end: date,
        *,
        snapshot_sequence: int,
    ) -> Iterator[tuple[str, date, bool, bool]]:
        """Stream adjustment-presence facts without decoding revision payloads."""
        if start > end or snapshot_sequence < 1:
            raise ValueError("history month query range is invalid")
        try:
            with closing(self._read_connection()) as connection:
                self._require_metadata(connection)
                cursor = connection.execute(
                    _LATEST_SHADOW_SQL,
                    (snapshot_sequence, start.isoformat(), end.isoformat()),
                )
                while rows := cursor.fetchmany(2048):
                    for trade_date, code, raw_present, qfq_present in rows:
                        yield str(code), date.fromisoformat(str(trade_date)), bool(raw_present), bool(qfq_present)
        except HistoryMonthPartitionError:
            raise
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise HistoryMonthPartitionError("history month shadow query failed") from exc

    def seal(self, sequence: int | None = None) -> HistorySnapshotPartition:
        """Produce a logical proof without copying, renaming or freezing the database."""
        try:
            with (
                closing(self._read_connection()) as connection,
                self._activity.watch(connection) if self._activity else nullcontext(),
            ):
                connection.execute("BEGIN")
                self._require_metadata(connection)
                self._require_schema(connection)
                if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise HistoryMonthPartitionError("history month integrity check failed")
                if sequence is None:
                    sequence = connection.execute(
                        "SELECT COALESCE(MAX(sync_sequence),1) FROM daily_observations"
                    ).fetchone()[0]
                proof = calculate_history_proof(
                    connection,
                    sequence,
                    validate_payloads=False,
                    heartbeat=lambda count: self._activity.tick() if self._activity else None,
                )
                return HistorySnapshotPartition(
                    f"partitions/{self._calendar_year:04d}/{self._calendar_month:02d}.sqlite3",
                    proof.logical_hash,
                    proof.row_count,
                    sequence,
                )
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise HistoryMonthPartitionError("history month logical proof failed") from exc

    @classmethod
    def verify(
        cls,
        path: Path,
        reference: HistorySnapshotPartition,
        progress: HistoryPartitionVerificationProgress | None = None,
    ) -> None:
        cls.verify_logical_content(path, reference, progress, validate_payloads=True)

    @classmethod
    def verify_logical_content(
        cls,
        path: Path,
        reference: HistorySnapshotPartition,
        progress: HistoryPartitionVerificationProgress | None = None,
        *,
        retain_pages: bool = False,
        validate_payloads: bool = False,
    ) -> None:
        del retain_pages
        parts = Path(reference.relative_path).parts
        candidate = cls(path, int(parts[1]), int(Path(parts[2]).stem))
        try:
            with closing(candidate._read_connection()) as connection:
                connection.execute("BEGIN")
                candidate._require_metadata(connection)
                candidate._require_schema(connection)
                proof = calculate_history_proof(
                    connection,
                    reference.sequence,
                    validate_payloads=validate_payloads,
                    heartbeat=lambda count: progress(count, reference.row_count, "logical") if progress else None,
                )
                if proof.logical_hash != reference.logical_hash or proof.row_count != reference.row_count:
                    raise HistoryMonthPartitionError("history month logical hash mismatch")
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise HistoryMonthPartitionError("history month logical verification failed") from exc

    def release_read_pages(self) -> None:
        """Release clean OS pages only after a sequential consumer finishes."""
        _release_file_cache(self._path)

    def _prepare_revisions(self, revisions: Iterable[HistoryRevision]) -> tuple[_EncodedRevision, ...]:
        prepared: list[_EncodedRevision] = []
        previous_key: tuple[date, str, int, str] | None = None
        observations: dict[tuple[str, str, int], str] = {}
        for value in revisions:
            if value.trade_date.year != self._calendar_year or value.trade_date.month != self._calendar_month:
                raise ValueError("history revision does not belong to the target month")
            key = (value.trade_date, value.code, value.first_seen_sequence, value.revision_id)
            if previous_key is not None and key < previous_key:
                raise ValueError("history month revisions must be written in deterministic order")
            encoded = _EncodedRevision(value, value.trade_date.isoformat(), encode_history_revision(value))
            observed_revision = observations.setdefault(encoded.observation_key, value.revision_id)
            if observed_revision != value.revision_id:
                raise HistoryMonthPartitionConflictError("history monthly revision sequence conflicts")
            prepared.append(encoded)
            previous_key = key
        return tuple(prepared)

    @staticmethod
    def _insert_fresh_batch(connection: sqlite3.Connection, prepared: tuple[_EncodedRevision, ...]) -> None:
        """Insert disjoint first-build keys; primary keys reject any repeated identity."""
        connection.executemany(
            "INSERT INTO daily_records"
            "(trade_date, code, revision_id, first_seen_sequence, board, payload_json, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    item.trade_date,
                    item.value.code,
                    item.value.revision_id,
                    item.value.first_seen_sequence,
                    item.value.board,
                    item.payload_json,
                    item.value.content_hash,
                )
                for item in prepared
            ),
        )
        connection.executemany(
            "INSERT INTO daily_observations(trade_date, code, sync_sequence, revision_id) VALUES (?, ?, ?, ?)",
            ((*item.observation_key, item.value.revision_id) for item in prepared),
        )

    def _save_batch(
        self,
        connection: sqlite3.Connection,
        prepared: tuple[_EncodedRevision, ...],
        *,
        pending_sequence: int | None = None,
    ) -> None:
        connection.execute(
            "CREATE TEMP TABLE requested_history_revisions ("
            "trade_date TEXT NOT NULL, code TEXT NOT NULL, sync_sequence INTEGER NOT NULL, "
            "revision_id TEXT NOT NULL, PRIMARY KEY (trade_date, code, sync_sequence, revision_id)"
            ") WITHOUT ROWID"
        )
        connection.executemany(
            "INSERT OR IGNORE INTO requested_history_revisions VALUES (?, ?, ?, ?)",
            ((*item.observation_key, item.value.revision_id) for item in prepared),
        )
        existing_observations = {
            (cast(str, row[0]), cast(str, row[1]), cast(int, row[2])): cast(tuple[str, BaoStockCellStatus], row[3:])
            for row in connection.execute(_EXISTING_OBSERVATIONS_SQL)
        }
        existing_revisions = {
            (cast(str, row[0]), cast(str, row[1]), cast(str, row[2])): (
                cast(int, row[3]),
                cast(str, row[4]),
                cast(str, row[5]),
            )
            for row in connection.execute(_EXISTING_REVISIONS_SQL)
        }
        latest_sequences = {
            (cast(str, row[0]), cast(str, row[1])): cast(int, row[2])
            for row in connection.execute(_LATEST_SEQUENCES_SQL)
        }
        new_records: list[tuple[object, ...]] = []
        new_observations: list[tuple[object, ...]] = []
        corrected_observations: list[tuple[str, str, str, int]] = []
        for item in prepared:
            value = item.value
            observation_key = item.observation_key
            revision_key = item.revision_key
            observed = existing_observations.get(observation_key)
            if observed is not None:
                observed_revision, _observed_status = observed
                if observed_revision == value.revision_id:
                    existing = existing_revisions.get(revision_key)
                    if existing is None:
                        raise HistoryMonthPartitionConflictError("history monthly observation parent is missing")
                    _require_revision_identity(existing, value)
                    continue
                self._require_observation_upgrade(connection, item, observed, pending_sequence)
                corrected_observations.append((value.revision_id, *observation_key))
            date_code = item.trade_date, value.code
            current_sequence = latest_sequences.get(date_code)
            if current_sequence is not None and value.first_seen_sequence < current_sequence:
                raise HistoryMonthPartitionConflictError("history monthly revision cannot backdate content")
            existing = existing_revisions.get(revision_key)
            if existing is None:
                existing = (value.first_seen_sequence, item.payload_json, value.content_hash)
                existing_revisions[revision_key] = existing
                new_records.append(
                    (
                        item.trade_date,
                        value.code,
                        value.revision_id,
                        value.first_seen_sequence,
                        value.board,
                        item.payload_json,
                        value.content_hash,
                    )
                )
            else:
                _require_revision_identity(existing, value)
            latest_sequences[date_code] = max(value.first_seen_sequence, current_sequence or 0)
            existing_observations[observation_key] = (value.revision_id, value.cell.status)
            if observed is None:
                new_observations.append((*observation_key, value.revision_id))
        connection.executemany(
            "INSERT INTO daily_records"
            "(trade_date, code, revision_id, first_seen_sequence, board, payload_json, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            new_records,
        )
        connection.executemany(
            "INSERT INTO daily_observations(trade_date, code, sync_sequence, revision_id) VALUES (?, ?, ?, ?)",
            new_observations,
        )
        connection.executemany(
            "UPDATE daily_observations SET revision_id=? WHERE trade_date=? AND code=? AND sync_sequence=?",
            corrected_observations,
        )

    @staticmethod
    def _require_observation_upgrade(
        connection: sqlite3.Connection,
        item: _EncodedRevision,
        observed: tuple[str, BaoStockCellStatus],
        pending_sequence: int | None,
    ) -> None:
        value = item.value
        observed_revision, observed_status = observed
        if not value.can_upgrade_observation(observed_status, pending_sequence):
            raise HistoryMonthPartitionConflictError("history monthly revision sequence conflicts")
        if observed_status not in {"qfq_missing", "unadjusted_missing"}:
            return
        previous_row = connection.execute(
            f"SELECT {_REVISION_COLUMNS} FROM daily_records AS records "
            "WHERE records.trade_date=? AND records.code=? AND records.revision_id=?",
            (item.trade_date, value.code, observed_revision),
        ).fetchone()
        if previous_row is None:
            raise HistoryMonthPartitionConflictError("history monthly observation parent is missing")
        previous_cell = _decode_row(previous_row).cell
        if previous_cell.status != observed_status or not value.retains_partial_prices(previous_cell):
            raise HistoryMonthPartitionConflictError("history monthly retained price conflicts")

    def _require_metadata(self, connection: sqlite3.Connection) -> None:
        metadata = connection.execute(
            "SELECT schema_identity, calendar_year, calendar_month FROM metadata WHERE singleton=1"
        ).fetchone()
        if metadata != (_SCHEMA_IDENTITY, self._calendar_year, self._calendar_month):
            raise HistoryMonthPartitionError("history month metadata conflicts")

    def _require_schema(self, connection: sqlite3.Connection) -> None:
        expected_indexes = {
            "history_month_code_date_idx": (
                "code",
                "trade_date",
                "first_seen_sequence",
                "revision_id",
            ),
            "history_month_date_board_code_idx": (
                "trade_date",
                "board",
                "code",
                "first_seen_sequence",
                "revision_id",
            ),
            "history_month_observation_code_date_idx": (
                "code",
                "trade_date",
                "sync_sequence",
                "revision_id",
            ),
        }
        for name, expected in expected_indexes.items():
            columns = tuple(row[2] for row in connection.execute(f"PRAGMA index_info('{name}')"))
            if columns != expected:
                raise HistoryMonthPartitionError("history month index contract is invalid")
        for table_name in ("daily_records", "daily_observations"):
            table_sql_row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            ).fetchone()
            if table_sql_row is None or "WITHOUT ROWID" not in cast(str, table_sql_row[0]).upper():
                raise HistoryMonthPartitionError("history month table contract is invalid")

    def _validate_all_rows(self, connection: sqlite3.Connection) -> int:
        cursor = connection.execute(_ALL_REVISION_SCAN_SQL)
        row_count = 0
        while rows := cursor.fetchmany(512):
            if self._activity is not None:
                self._activity.tick()
            for row in rows:
                _decode_row(row)
                row_count += 1
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise HistoryMonthPartitionError("history month observation reference is invalid")
        first_seen_mismatch = connection.execute(
            "SELECT 1 FROM daily_records AS records "
            "WHERE records.first_seen_sequence != ("
            "SELECT MIN(observations.sync_sequence) FROM daily_observations AS observations "
            "WHERE observations.trade_date=records.trade_date AND observations.code=records.code "
            "AND observations.revision_id=records.revision_id"
            ") LIMIT 1"
        ).fetchone()
        if first_seen_mismatch is not None:
            raise HistoryMonthPartitionError("history month first observation is inconsistent")
        return row_count

    def _write_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5.0)
        if self._statement_trace is not None:
            connection.set_trace_callback(self._statement_trace)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA cache_size=-8192")
        connection.execute("PRAGMA mmap_size=0")
        connection.execute("PRAGMA temp_store=FILE")
        return connection

    def _read_connection(self) -> sqlite3.Connection:
        return open_history_month_reader(
            self._path, statement_trace=self._statement_trace, mmap_bytes=0, activity=self._activity
        )

    def _verification_connection(self) -> sqlite3.Connection:
        return open_history_month_reader(
            self._path,
            statement_trace=self._statement_trace,
            mmap_bytes=_VERIFICATION_MMAP_BYTES,
            activity=self._activity,
        )


class HistoryMonthWriteSession:
    """Reuse one month's connection without weakening per-batch FULL durability."""

    def __init__(self, prices: HistoryMonthPrices, *, fresh: bool = False, pending_sequence: int | None = None) -> None:
        self._prices = prices
        self._fresh = fresh
        self._pending_sequence = pending_sequence
        self._connection = prices._write_connection()
        try:
            prices._require_metadata(self._connection)
        except (HistoryMonthPartitionError, sqlite3.Error):
            self._connection.close()
            raise

    def save_revision_batches(self, revisions: Iterable[HistoryRevision]) -> None:
        incoming = iter(revisions)
        try:
            with self._prices._activity.watch(self._connection) if self._prices._activity else nullcontext():
                while batch := tuple(islice(incoming, 4096)):
                    if shutil.disk_usage(self._prices._path.parent).free < 64 * 1024**2:
                        raise HistoryMonthPartitionError("disk_space_insufficient")
                    with self._connection:
                        self._connection.execute("BEGIN IMMEDIATE")
                        prepared = self._prices._prepare_revisions(batch)
                        if self._fresh:
                            self._prices._insert_fresh_batch(self._connection, prepared)
                        else:
                            self._prices._save_batch(
                                self._connection, prepared, pending_sequence=self._pending_sequence
                            )
                            self._connection.execute("DROP TABLE requested_history_revisions")
        except sqlite3.Error as exc:
            raise HistoryMonthPartitionError("history month revision write failed") from exc

    def close(self) -> None:
        try:
            self._connection.close()
        except sqlite3.Error as exc:
            raise HistoryMonthPartitionError("history month writer close failed") from exc


def _latest_query(code: str | None, board: str | None) -> tuple[str, tuple[str, ...]]:
    if code is not None and board is not None:
        return _LATEST_CODE_BOARD_SQL, (code, board)
    if code is not None:
        return _LATEST_CODE_SQL, (code,)
    if board is not None:
        return _LATEST_BOARD_SQL, (board,)
    return _LATEST_RANGE_SQL, ()


def _require_revision_identity(
    existing: tuple[int, str, str],
    value: HistoryRevision,
) -> None:
    first_seen_sequence, existing_payload, existing_hash = existing
    persisted = HistoryRevision(
        first_seen_sequence,
        value.board,
        value.cell,
        value.is_st,
        value.industry,
        value.industry_classification,
    )
    if existing_payload != encode_history_revision(persisted) or existing_hash != persisted.content_hash:
        raise HistoryMonthPartitionConflictError("history monthly revision identity conflicts")


def _decode_row(row: tuple[object, ...]) -> HistoryRevision:
    if len(row) != 7:
        raise HistoryMonthPartitionError("history month row shape is invalid")
    trade_date_value, code, revision_id, first_seen_sequence, board, payload_json, content_hash = row
    if not all(
        isinstance(value, str) for value in (trade_date_value, code, revision_id, board, payload_json, content_hash)
    ):
        raise HistoryMonthPartitionError("history month row fields are invalid")
    if not isinstance(first_seen_sequence, int) or isinstance(first_seen_sequence, bool):
        raise HistoryMonthPartitionError("history month sequence is invalid")
    value = decode_history_revision(cast(str, payload_json))
    if (
        value.trade_date.isoformat() != trade_date_value
        or value.code != code
        or value.revision_id != revision_id
        or value.first_seen_sequence != first_seen_sequence
        or value.board != board
        or value.content_hash != content_hash
    ):
        raise HistoryMonthPartitionError("history month row identity is inconsistent")
    return value


def _decode_training_row(row: tuple[object, ...]) -> HistoryTrainingRow:
    if len(row) != 8:
        raise HistoryMonthPartitionError("history month training row shape is invalid")
    trade_date_value, code, board, cell_status, raw_status, qfq_status, qfq_close, qfq_amount = row
    if (
        not isinstance(trade_date_value, str)
        or not isinstance(code, str)
        or not isinstance(board, str)
        or cell_status
        not in {
            "complete",
            "supplier_marked_suspended",
            "tencent_inferred_suspended",
            "unadjusted_missing",
            "qfq_missing",
            "unknown_missing",
        }
        or raw_status not in {None, "trading", "suspended"}
        or qfq_status not in {None, "trading", "suspended"}
    ):
        raise HistoryMonthPartitionError("history month training row fields are invalid")
    trade_date = date.fromisoformat(trade_date_value)
    point = (
        HistoryTrainingPoint(
            trade_date,
            _training_number(qfq_close),
            _training_number(qfq_amount),
        )
        if raw_status == qfq_status == "trading"
        else None
    )
    return HistoryTrainingRow(
        code,
        trade_date,
        cast(BaoStockBoard, board),
        None,
        point,
        cell_status,
    )


def _training_number(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HistoryMonthPartitionError("history month training number is invalid")
    return float(value)


def _sha256_file(
    path: Path,
    progress: HistoryPartitionVerificationProgress | None = None,
    *,
    release_cache: bool = True,
) -> str:
    digest = hashlib.sha256()
    total = path.stat().st_size
    with path.open("rb", buffering=0) as handle:
        _advise_file_access(handle.fileno(), 0, 0, "POSIX_FADV_SEQUENTIAL")
        released_offset = 0
        offset = 0
        for chunk in iter(lambda: handle.read(_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
            offset += len(chunk)
            if offset - released_offset >= _CACHE_RELEASE_BYTES:
                if release_cache:
                    _advise_file_access(
                        handle.fileno(), released_offset, offset - released_offset, "POSIX_FADV_DONTNEED"
                    )
                released_offset = offset
                if progress is not None:
                    progress(offset, total, "hash")
        if release_cache and offset > released_offset:
            _advise_file_access(
                handle.fileno(),
                released_offset,
                offset - released_offset,
                "POSIX_FADV_DONTNEED",
            )
        if progress is not None:
            progress(offset, total, "hash")
    return digest.hexdigest()


def _advise_file_access(descriptor: int, offset: int, length: int, advice_name: str) -> None:
    advise = getattr(os, "posix_fadvise", None)
    advice = getattr(os, advice_name, None)
    if advise is None or advice is None:
        return
    try:
        advise(descriptor, offset, length, advice)
    except OSError:
        pass


def _release_file_cache(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        _advise_file_access(descriptor, 0, 0, "POSIX_FADV_DONTNEED")
    finally:
        os.close(descriptor)


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "HistoryMonthPartitionConflictError",
    "HistoryMonthPartitionError",
    "HistoryPartitionVerificationPhase",
    "HistoryMonthPrices",
    "HistoryMonthWriteSession",
]
