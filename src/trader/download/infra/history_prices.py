"""Annual structured prices with revision isolation and calendar-month proofs."""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
from collections.abc import Callable, Collection, Generator, Iterable, Iterator
from contextlib import closing, nullcontext
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import islice
from pathlib import Path
from typing import Literal, cast

from trader.download.domain.baostock_daily import BaoStockBoard, BaoStockCellStatus
from trader.download.domain.history_control import HistorySnapshotPartition
from trader.download.domain.history_revision import HistoryRevision, HistoryTrainingPoint, HistoryTrainingRow
from trader.download.domain.published_history import PublishedHistoryCell
from trader.download.infra.history_partition_proof import CalculateHistoryProof
from trader.download.infra.history_revision_codec import (
    PublishedCellFromRevisionColumns,
    RevisionColumnValues,
    RevisionFromColumns,
)
from trader.download.infra.history_sqlite_activity import HistorySqliteActivity
from trader.download.infra.history_sqlite_read_connection import OpenHistoryPriceReader, WatchHistoryRead
from trader.download.infra.price_columns import PRICE_COLUMNS, PRICE_SQL, PriceValues
from trader.infra.sqlite_codec import DecodeDate, DecodeHash, EncodeDate, EncodeHash
from trader.infra.sqlite_connection import (
    EnsureSqliteSchema,
    OpenSqliteConnection,
    SqliteConnection,
    SqliteConnectionOptions,
    SqliteSchema,
    SqliteSession,
    SqliteTransaction,
)

_SCHEMA_IDENTITY = "history_annual_prices"
_CODE = re.compile(r"^[0-9]{6}$")
HistoryPartitionVerificationPhase = Literal["hash", "integrity", "row_count", "logical"]
HistoryPartitionVerificationProgress = Callable[[int, int, HistoryPartitionVerificationPhase], None]
_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS schema_contract (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    schema_id TEXT NOT NULL,
    calendar_year INTEGER NOT NULL,
    schema_version INTEGER NOT NULL CHECK(schema_version=2)
);
CREATE TABLE IF NOT EXISTS daily_price_revision (
    trade_date INTEGER NOT NULL,
    code TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    first_seen_sequence INTEGER NOT NULL CHECK(first_seen_sequence > 0),
    board TEXT NOT NULL,
    {PRICE_SQL},
    content_hash BLOB NOT NULL CHECK(length(content_hash)=32),
    PRIMARY KEY (trade_date, code, revision_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS daily_price_observation (
    trade_date INTEGER NOT NULL,
    code TEXT NOT NULL,
    sync_sequence INTEGER NOT NULL CHECK(sync_sequence > 0),
    revision_id TEXT NOT NULL,
    PRIMARY KEY (trade_date, code, sync_sequence),
    FOREIGN KEY (trade_date, code, revision_id)
        REFERENCES daily_price_revision(trade_date, code, revision_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS history_price_code_date_idx
ON daily_price_revision(code, trade_date, first_seen_sequence DESC, revision_id);
CREATE INDEX IF NOT EXISTS history_price_date_board_code_idx
ON daily_price_revision(trade_date, board, code, first_seen_sequence DESC, revision_id);
CREATE INDEX IF NOT EXISTS history_price_observation_code_date_idx
ON daily_price_observation(code, trade_date, sync_sequence DESC, revision_id);
CREATE INDEX IF NOT EXISTS history_price_sequence_idx ON daily_price_observation(sync_sequence);
CREATE INDEX IF NOT EXISTS history_price_price_gap_idx
ON daily_price_revision(code, trade_date, revision_id)
WHERE status IN ('unknown_missing','qfq_missing','unadjusted_missing');
"""
_SCHEMA = SqliteSchema(2, tuple(statement.strip() for statement in _SCHEMA_SQL.split(";") if statement.strip()))
_PRICE_SELECT = ", ".join(f"records.{name}" for name in PRICE_COLUMNS)
_REVISION_COLUMNS = (
    "records.trade_date, records.code, records.revision_id, records.first_seen_sequence, records.board, "
    + _PRICE_SELECT
    + ", records.content_hash"
)
_INSERT_RECORD = (
    "INSERT INTO daily_price_revision VALUES (" + ",".join("?" for _ in range(6 + len(PRICE_COLUMNS))) + ")"
)
_LATEST_WINDOW = """
WITH latest AS (
    SELECT trade_date, code, revision_id,
           ROW_NUMBER() OVER (
               PARTITION BY trade_date, code
               ORDER BY sync_sequence DESC
           ) AS revision_rank
    FROM daily_price_observation
    WHERE sync_sequence <= ? AND trade_date BETWEEN ? AND ?
{observation_filter}
)
SELECT {columns}
FROM latest
JOIN daily_price_revision AS records
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
FROM daily_price_revision AS records INDEXED BY {index}
WHERE records.trade_date BETWEEN ? AND ?
  AND records.revision_id = (
      SELECT observed.revision_id
      FROM daily_price_observation AS observed INDEXED BY {observation_index}
      WHERE observed.code = records.code AND observed.trade_date = records.trade_date
        AND observed.sync_sequence <= ?
      ORDER BY observed.sync_sequence DESC
      LIMIT 1
  )
ORDER BY records.code, records.trade_date
"""
_LATEST_RANGE_BY_CODE_SQL = _RANGE_BY_CODE.format(
    columns=_REVISION_COLUMNS,
    index="history_price_code_date_idx",
    observation_index="history_price_observation_code_date_idx",
)
# Published month windows must seek dates in the annual file, then sort only the requested rows.
_PUBLISHED_RANGE_BY_CODE_SQL = _RANGE_BY_CODE.format(
    columns=_REVISION_COLUMNS,
    index="sqlite_autoindex_daily_price_revision_1",
    observation_index="sqlite_autoindex_daily_price_observation_1",
)
_LATEST_SHADOW_SQL = """
WITH latest AS (
    SELECT trade_date, code, revision_id,
           ROW_NUMBER() OVER (
               PARTITION BY trade_date, code
               ORDER BY sync_sequence DESC
           ) AS revision_rank
    FROM daily_price_observation
    WHERE sync_sequence <= ? AND trade_date BETWEEN ? AND ?
)
SELECT records.trade_date, records.code,
       records.raw_present,
       records.qfq_present
FROM latest
JOIN daily_price_revision AS records
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
    columns=_REVISION_COLUMNS, observation_filter="      AND code = ?", record_filter=""
)
_TRAINING_RANGE_SQL = """
SELECT records.trade_date,
       records.code,
       records.board,
       records.status,
       records.raw_trading_status,
       records.qfq_trading_status,
       records.qfq_close,
       records.qfq_amount,
       records.qfq_open,
       records.qfq_high,
       records.qfq_low,
       records.raw_volume
FROM daily_price_revision AS records
WHERE records.trade_date BETWEEN ? AND ?
  AND records.revision_id = (
      SELECT observed.revision_id
      FROM daily_price_observation AS observed INDEXED BY sqlite_autoindex_daily_price_observation_1
      WHERE observed.code = records.code AND observed.trade_date = records.trade_date
        AND observed.sync_sequence <= ?
      ORDER BY observed.sync_sequence DESC
      LIMIT 1
  )
ORDER BY records.trade_date, records.code
"""
_COUNT_CODE_BATCH_SIZE = 500
_ALL_REVISION_SCAN_SQL = "SELECT " + _REVISION_COLUMNS + " FROM daily_price_revision AS records"
_EXISTING_OBSERVATIONS_SQL = """
SELECT observations.trade_date, observations.code, observations.sync_sequence, observations.revision_id,
       records.status
FROM requested_history_revisions AS requested
CROSS JOIN daily_price_observation AS observations
LEFT JOIN daily_price_revision AS records
  ON records.trade_date=observations.trade_date
 AND records.code=observations.code
 AND records.revision_id=observations.revision_id
WHERE observations.trade_date=requested.trade_date
  AND observations.code=requested.code
  AND observations.sync_sequence=requested.sync_sequence
"""
_EXISTING_REVISIONS_SQL = f"""
SELECT records.trade_date, records.code, records.revision_id, records.first_seen_sequence,
       {_PRICE_SELECT}, records.content_hash
FROM requested_history_revisions AS requested
CROSS JOIN daily_price_revision AS records
WHERE records.trade_date=requested.trade_date
  AND records.code=requested.code
  AND records.revision_id=requested.revision_id
"""
_LATEST_SEQUENCES_SQL = """
SELECT requested.trade_date, requested.code, MAX(observations.sync_sequence)
FROM requested_history_revisions AS requested
CROSS JOIN daily_price_observation AS observations
WHERE observations.trade_date=requested.trade_date
  AND observations.code=requested.code
GROUP BY requested.trade_date, requested.code
"""


@dataclass(frozen=True)
class _EncodedRevision:
    value: HistoryRevision
    trade_date: int
    price_values: PriceValues

    @property
    def observation_key(self) -> tuple[int, str, int]:
        return self.trade_date, self.value.code, self.value.first_seen_sequence

    @property
    def revision_key(self) -> tuple[int, str, str]:
        return self.trade_date, self.value.code, self.value.revision_id


class HistoryPricePartitionError(RuntimeError):
    """A month partition cannot be trusted or queried."""


class HistoryPricePartitionConflictError(HistoryPricePartitionError):
    """An immutable logical row conflicts with an existing revision."""


class HistoryPricePartition:
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

    def _month_bounds(self) -> tuple[int, int]:
        start = date(self._calendar_year, self._calendar_month, 1)
        end = date(self._calendar_year + (self._calendar_month == 12), self._calendar_month % 12 + 1, 1)
        return EncodeDate(start), EncodeDate(end)

    def _clip_range(self, start: date, end: date) -> tuple[date, date]:
        first, after = self._month_bounds()
        return max(start, DecodeDate(first)), min(end, DecodeDate(after) - timedelta(days=1))

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with SqliteSession(
                self._path, SqliteConnectionOptions(journal_mode="WAL", foreign_keys=True, page_size=8192)
            ) as connection:
                if self._statement_trace is not None:
                    connection.observe_statements(self._statement_trace)

                def initialize_metadata(connection: SqliteConnection) -> None:
                    connection.run(
                        "INSERT OR IGNORE INTO schema_contract(singleton,schema_id,calendar_year,schema_version) VALUES(1,?,?,2)",
                        (_SCHEMA_IDENTITY, self._calendar_year),
                    )

                EnsureSqliteSchema(
                    connection,
                    _SCHEMA,
                    allow_create=True,
                    initialize=initialize_metadata,
                    validate=self._require_metadata,
                )
                self._require_schema(connection)
        except (sqlite3.Error, ValueError) as exc:
            raise HistoryPricePartitionError("history month partition initialization failed") from exc
        try:
            _fsync_file(self._path)
            _fsync_directory(self._path.parent)
        except OSError as exc:
            raise HistoryPricePartitionError("history month partition durability sync failed") from exc

    def changed_revisions(self, revisions: tuple[HistoryRevision, ...]) -> tuple[HistoryRevision, ...]:
        """Compare bounded incoming keys with their latest visible price identity, read-only."""
        return tuple(item for batch in self.iter_changed_batches(revisions) for item in batch)

    def has_observations_after(self, sequence: int) -> bool:
        with closing(self._read_connection()) as connection:
            return (
                connection.run(
                    "SELECT 1 FROM daily_price_observation WHERE sync_sequence>? AND trade_date>=? AND trade_date<? LIMIT 1",
                    (sequence, *self._month_bounds()),
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
                rows = connection.run(
                    "WITH requested(day,code,sequence) AS (VALUES " + placeholders + ") "
                    "SELECT (SELECT o.revision_id FROM daily_price_observation o "
                    "WHERE o.trade_date=r.day AND o.code=r.code AND o.sync_sequence<=r.sequence "
                    "ORDER BY o.sync_sequence DESC LIMIT 1) FROM requested r",
                    tuple(
                        v for item in batch for v in (EncodeDate(item.trade_date), item.code, item.first_seen_sequence)
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
                        raise HistoryPricePartitionError("disk_space_insufficient")
                    with SqliteTransaction(connection, immediate=True):
                        self._save_batch(connection, self._prepare_revisions(batch))
                        connection.run("DROP TABLE requested_history_revisions")
        except sqlite3.Error as exc:
            raise HistoryPricePartitionError("history month revision write failed") from exc

    def open_write_session(
        self, *, fresh: bool = False, pending_sequence: int | None = None
    ) -> HistoryPriceWriteSession:
        """Open one thread-confined writer reused across durable batch transactions."""
        return HistoryPriceWriteSession(self, fresh=fresh, pending_sequence=pending_sequence)

    def read_price_keys(self, keys: tuple[tuple[str, date], ...], sequence: int) -> tuple[HistoryRevision, ...]:
        """Read a bounded stock batch through exact observation/record keys on one connection."""
        result: list[HistoryRevision] = []
        with (
            closing(self._read_connection()) as connection,
            self._activity.watch(connection) if self._activity else nullcontext(),
        ):
            self._require_metadata(connection)
            present_codes: set[str] = set()
            for code in sorted({code for code, _day in keys}):
                if (
                    connection.run(
                        "SELECT 1 FROM daily_price_revision INDEXED BY history_price_code_date_idx WHERE code=? LIMIT 1",
                        (code,),
                    ).fetchone()
                    is not None
                ):
                    present_codes.add(code)
            # Interrupted first builds usually leave whole stocks unwritten. One
            # compact index lookup avoids thousands of scattered date-key misses.
            readable = tuple(key for key in keys if key[0] in present_codes)
            for start in range(0, len(readable), 200):
                if self._activity is not None:
                    self._activity.tick()
                batch = readable[start : start + 200]
                values = ",".join("(?,?)" for _ in batch)
                sql = (
                    "WITH requested(code,day) AS (VALUES "
                    + values
                    + ") SELECT "
                    + _REVISION_COLUMNS
                    + " FROM requested AS k CROSS JOIN daily_price_revision AS records "
                    "WHERE records.trade_date=k.day AND records.code=k.code AND records.revision_id=("
                    "SELECT o.revision_id FROM daily_price_observation o WHERE o.trade_date=k.day AND o.code=k.code "
                    "AND o.sync_sequence<=? ORDER BY o.sync_sequence DESC LIMIT 1)"
                )
                result.extend(
                    _decode_row(tuple(row))
                    for row in connection.run(
                        sql, (*tuple(value for code, day in batch for value in (code, EncodeDate(day))), sequence)
                    )
                )
        return tuple(result)

    def save_revisions(self, revisions: Iterable[HistoryRevision]) -> None:
        prepared = self._prepare_revisions(revisions)
        if not prepared:
            return
        try:
            with closing(self._write_connection()) as connection, SqliteTransaction(connection, immediate=True):
                self._require_metadata(connection)
                self._save_batch(connection, prepared)
        except HistoryPricePartitionConflictError:
            raise
        except sqlite3.Error as exc:
            raise HistoryPricePartitionError("history month revision write failed") from exc

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
            rows = connection.run(
                "SELECT r.code,r.trade_date FROM daily_price_revision r INDEXED BY history_price_price_gap_idx "
                "WHERE r.trade_date>=? AND r.trade_date<? AND r.status IN "
                "('unknown_missing','qfq_missing','unadjusted_missing') "
                "AND r.revision_id=(SELECT o.revision_id FROM daily_price_observation o "
                "WHERE o.code=r.code AND o.trade_date=r.trade_date AND o.sync_sequence<=? "
                "ORDER BY o.sync_sequence DESC LIMIT 1) ORDER BY r.code,r.trade_date",
                (*self._month_bounds(), snapshot_sequence),
            ).fetchall()
        return tuple((str(code), DecodeDate(day)) for code, day in rows)

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
                rows = connection.run(
                    "SELECT records.code, COUNT(*) FROM daily_price_revision AS records "
                    "INDEXED BY history_price_price_gap_idx "
                    "WHERE records.code IN (" + placeholders + ") "
                    "AND records.trade_date>=? AND records.trade_date<? AND records.status IN "
                    "('unknown_missing','qfq_missing','unadjusted_missing') "
                    "AND records.revision_id=(SELECT observations.revision_id FROM daily_price_observation AS observations "
                    "WHERE observations.code=records.code AND observations.trade_date=records.trade_date "
                    "AND observations.sync_sequence<=? ORDER BY observations.sync_sequence DESC LIMIT 1) "
                    "GROUP BY records.code",
                    (*batch, *self._month_bounds(), snapshot_sequence),
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
        start, end = self._clip_range(start, end)
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
                    row = connection.run(
                        "SELECT COUNT(*) FROM ("
                        "SELECT trade_date, code FROM daily_price_observation "
                        "WHERE sync_sequence <= ? AND trade_date BETWEEN ? AND ? "
                        f"AND code IN ({placeholders}) GROUP BY trade_date, code"
                        ")",
                        (snapshot_sequence, EncodeDate(start), EncodeDate(end), *batch),
                    ).fetchone()
                    if row is None:
                        raise HistoryPricePartitionError("history month count is unavailable")
                    count += cast(int, row[0])
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise HistoryPricePartitionError("history month count failed") from exc
        return count

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
        start, end = self._clip_range(start, end)
        try:
            with closing(self._read_connection()) as connection:
                self._require_metadata(connection)
                query, filters = _latest_query(code, board)
                cursor = connection.run(
                    query,
                    (snapshot_sequence, EncodeDate(start), EncodeDate(end), *filters),
                )
                while rows := cursor.fetchmany(512):
                    for row in rows:
                        yield _decode_row(tuple(row))
        except HistoryPricePartitionError:
            raise
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise HistoryPricePartitionError("history month query failed") from exc

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
        start, end = self._clip_range(start, end)
        try:
            with closing(self._read_connection()) as connection:
                self._require_metadata(connection)
                cursor = connection.run(
                    _LATEST_RANGE_BY_CODE_SQL,
                    (EncodeDate(start), EncodeDate(end), snapshot_sequence),
                )
                while rows := cursor.fetchmany(512):
                    for row in rows:
                        yield _decode_row(tuple(row))
        except HistoryPricePartitionError:
            raise
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise HistoryPricePartitionError("history month code-ordered query failed") from exc

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
        start, end = self._clip_range(start, end)
        try:
            with closing(self._read_connection()) as connection:
                self._require_metadata(connection)
                cursor = connection.run(
                    _TRAINING_RANGE_SQL,
                    (EncodeDate(start), EncodeDate(end), snapshot_sequence),
                )
                while rows := cursor.fetchmany(4_096):
                    for row in rows:
                        yield _decode_training_row(tuple(row))
        except HistoryPricePartitionError:
            raise
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise HistoryPricePartitionError("history month training query failed") from exc

    def iter_published_cells(
        self,
        start: date,
        end: date,
        *,
        snapshot_sequence: int,
        code: str | None = None,
        check: Callable[[], None] | None = None,
        annual_range: bool = False,
    ) -> Generator[PublishedHistoryCell, None, None]:
        """Project sealed daily facts without materializing full revision objects."""
        if start > end or snapshot_sequence < 1 or (code is not None and _CODE.fullmatch(code) is None):
            raise ValueError("published daily query range is invalid")
        start, end = (
            (max(start, date(self._calendar_year, 1, 1)), min(end, date(self._calendar_year, 12, 31)))
            if annual_range
            else self._clip_range(start, end)
        )
        query = (
            (_LATEST_RANGE_BY_CODE_SQL if annual_range else _PUBLISHED_RANGE_BY_CODE_SQL)
            if code is None
            else _PUBLISHED_CODE_SQL
        )
        parameters = (
            (EncodeDate(start), EncodeDate(end), snapshot_sequence)
            if code is None
            else (snapshot_sequence, EncodeDate(start), EncodeDate(end), code)
        )
        try:
            with closing(self._read_connection()) as connection, WatchHistoryRead(connection, check):
                self._require_metadata(connection)
                cursor = connection.run(query, parameters)
                while rows := cursor.fetchmany(512):
                    if check is not None:
                        check()
                    for row in rows:
                        yield PublishedCellFromRevisionColumns(tuple(row))
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise HistoryPricePartitionError("published daily query failed") from exc

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
        start, end = self._clip_range(start, end)
        try:
            with closing(self._read_connection()) as connection:
                self._require_metadata(connection)
                cursor = connection.run(
                    _LATEST_SHADOW_SQL,
                    (snapshot_sequence, EncodeDate(start), EncodeDate(end)),
                )
                while rows := cursor.fetchmany(2048):
                    for trade_date, code, raw_present, qfq_present in rows:
                        yield str(code), DecodeDate(trade_date), bool(raw_present), bool(qfq_present)
        except HistoryPricePartitionError:
            raise
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise HistoryPricePartitionError("history month shadow query failed") from exc

    def seal(self, sequence: int | None = None) -> HistorySnapshotPartition:
        """Produce a logical proof without copying, renaming or freezing the database."""
        try:
            with (
                closing(self._read_connection()) as connection,
                SqliteTransaction(connection),
                self._activity.watch(connection) if self._activity else nullcontext(),
            ):
                self._require_metadata(connection)
                self._require_schema(connection)
                if sequence is None:
                    sequence = connection.run(
                        "SELECT COALESCE(MAX(sync_sequence),1) FROM daily_price_observation"
                    ).require_one()[0]
                proof = CalculateHistoryProof(
                    connection,
                    sequence,
                    # Typed writes already validate the new values. Publication seals
                    # visible identities; cold readers and offline audits verify payloads.
                    validate_payloads=False,
                    dates=self._month_bounds(),
                    heartbeat=lambda count: self._activity.tick() if self._activity else None,
                )
                return HistorySnapshotPartition(
                    f"prices_{self._calendar_year:04d}.sqlite3",
                    proof.logical_hash,
                    proof.row_count,
                    self._calendar_month,
                    sequence,
                )
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise HistoryPricePartitionError("history month logical proof failed") from exc

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
        check: Callable[[], None] | None = None,
    ) -> None:
        del retain_pages
        candidate = cls(path, reference.calendar_year, reference.calendar_month)
        try:
            with (
                closing(candidate._read_connection()) as connection,
                WatchHistoryRead(connection, check),
                SqliteTransaction(connection),
            ):
                candidate._require_metadata(connection)
                candidate._require_schema(connection)
                proof = CalculateHistoryProof(
                    connection,
                    reference.sequence,
                    validate_payloads=validate_payloads,
                    dates=candidate._month_bounds(),
                    heartbeat=lambda count: progress(count, reference.row_count, "logical") if progress else None,
                    check=check,
                )
                if proof.logical_hash != reference.logical_hash or proof.row_count != reference.row_count:
                    raise HistoryPricePartitionError("history month logical hash mismatch")
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise HistoryPricePartitionError("history month logical verification failed") from exc

    def release_read_pages(self) -> None:
        """Release clean OS pages only after a sequential consumer finishes."""
        _release_file_cache(self._path)

    def _prepare_revisions(self, revisions: Iterable[HistoryRevision]) -> tuple[_EncodedRevision, ...]:
        prepared: list[_EncodedRevision] = []
        previous_key: tuple[date, str, int, str] | None = None
        observations: dict[tuple[int, str, int], str] = {}
        for value in revisions:
            if value.trade_date.year != self._calendar_year:
                raise ValueError("history revision does not belong to the target year")
            key = (value.trade_date, value.code, value.first_seen_sequence, value.revision_id)
            if previous_key is not None and key < previous_key:
                raise ValueError("history month revisions must be written in deterministic order")
            encoded = _EncodedRevision(value, EncodeDate(value.trade_date), RevisionColumnValues(value))
            observed_revision = observations.setdefault(encoded.observation_key, value.revision_id)
            if observed_revision != value.revision_id:
                raise HistoryPricePartitionConflictError("history monthly revision sequence conflicts")
            prepared.append(encoded)
            previous_key = key
        return tuple(prepared)

    @staticmethod
    def _insert_fresh_batch(connection: SqliteConnection, prepared: tuple[_EncodedRevision, ...]) -> None:
        """Insert disjoint first-build keys; primary keys reject any repeated identity."""
        connection.run_many(
            _INSERT_RECORD,
            (
                (
                    item.trade_date,
                    item.value.code,
                    item.value.revision_id,
                    item.value.first_seen_sequence,
                    item.value.board,
                    *item.price_values,
                    EncodeHash(item.value.content_hash),
                )
                for item in prepared
            ),
        )
        connection.run_many(
            "INSERT INTO daily_price_observation(trade_date, code, sync_sequence, revision_id) VALUES (?, ?, ?, ?)",
            ((*item.observation_key, item.value.revision_id) for item in prepared),
        )

    @staticmethod
    def _can_insert_disjoint(connection: SqliteConnection, prepared: tuple[_EncodedRevision, ...]) -> bool:
        # A resumed initial batch can contain stocks never written to this year.
        # Prove absence under the writer transaction using the compact code indexes,
        # rather than probing every date across the large date-ordered tables.
        if len({item.observation_key for item in prepared}) != len(prepared) or len(
            {item.revision_key for item in prepared}
        ) != len(prepared):
            return False
        codes = tuple(sorted({item.value.code for item in prepared}))
        for offset in range(0, len(codes), 200):
            batch = codes[offset : offset + 200]
            placeholders = ",".join("?" for _ in batch)
            if (
                connection.run(
                    "SELECT 1 FROM daily_price_revision INDEXED BY history_price_code_date_idx "
                    f"WHERE code IN ({placeholders}) UNION ALL "
                    "SELECT 1 FROM daily_price_observation INDEXED BY history_price_observation_code_date_idx "
                    f"WHERE code IN ({placeholders}) LIMIT 1",
                    (*batch, *batch),
                ).fetchone()
                is not None
            ):
                return False
        return True

    def _save_batch(
        self,
        connection: SqliteConnection,
        prepared: tuple[_EncodedRevision, ...],
        *,
        pending_sequence: int | None = None,
    ) -> None:
        connection.run(
            "CREATE TEMP TABLE requested_history_revisions ("
            "trade_date INTEGER NOT NULL, code TEXT NOT NULL, sync_sequence INTEGER NOT NULL, "
            "revision_id TEXT NOT NULL, PRIMARY KEY (trade_date, code, sync_sequence, revision_id)"
            ") WITHOUT ROWID"
        )
        connection.run_many(
            "INSERT OR IGNORE INTO requested_history_revisions VALUES (?, ?, ?, ?)",
            ((*item.observation_key, item.value.revision_id) for item in prepared),
        )
        existing_observations = {
            (cast(int, row[0]), cast(str, row[1]), cast(int, row[2])): cast(tuple[str, BaoStockCellStatus], row[3:])
            for row in connection.run(_EXISTING_OBSERVATIONS_SQL)
        }
        existing_revisions = {
            (cast(int, row[0]), cast(str, row[1]), cast(str, row[2])): (
                cast(int, row[3]),
                cast(PriceValues, row[4:-1]),
                DecodeHash(row[-1]),
            )
            for row in connection.run(_EXISTING_REVISIONS_SQL)
        }
        latest_sequences = {
            (cast(int, row[0]), cast(str, row[1])): cast(int, row[2]) for row in connection.run(_LATEST_SEQUENCES_SQL)
        }
        new_records: list[tuple[object, ...]] = []
        new_observations: list[tuple[object, ...]] = []
        corrected_observations: list[tuple[str, int, str, int]] = []
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
                        raise HistoryPricePartitionConflictError("history monthly observation parent is missing")
                    _require_revision_identity(existing, value)
                    continue
                self._require_observation_upgrade(connection, item, observed, pending_sequence)
                corrected_observations.append((value.revision_id, *observation_key))
            date_code = item.trade_date, value.code
            current_sequence = latest_sequences.get(date_code)
            if current_sequence is not None and value.first_seen_sequence < current_sequence:
                raise HistoryPricePartitionConflictError("history monthly revision cannot backdate content")
            existing = existing_revisions.get(revision_key)
            if existing is None:
                existing = (value.first_seen_sequence, item.price_values, value.content_hash)
                existing_revisions[revision_key] = existing
                new_records.append(
                    (
                        item.trade_date,
                        value.code,
                        value.revision_id,
                        value.first_seen_sequence,
                        value.board,
                        *item.price_values,
                        EncodeHash(value.content_hash),
                    )
                )
            else:
                _require_revision_identity(existing, value)
            latest_sequences[date_code] = max(value.first_seen_sequence, current_sequence or 0)
            existing_observations[observation_key] = (value.revision_id, value.cell.status)
            if observed is None:
                new_observations.append((*observation_key, value.revision_id))
        connection.run_many(
            _INSERT_RECORD,
            new_records,
        )
        connection.run_many(
            "INSERT INTO daily_price_observation(trade_date, code, sync_sequence, revision_id) VALUES (?, ?, ?, ?)",
            new_observations,
        )
        connection.run_many(
            "UPDATE daily_price_observation SET revision_id=? WHERE trade_date=? AND code=? AND sync_sequence=?",
            corrected_observations,
        )

    @staticmethod
    def _require_observation_upgrade(
        connection: SqliteConnection,
        item: _EncodedRevision,
        observed: tuple[str, BaoStockCellStatus],
        pending_sequence: int | None,
    ) -> None:
        value = item.value
        observed_revision, observed_status = observed
        if not value.can_upgrade_observation(observed_status, pending_sequence):
            raise HistoryPricePartitionConflictError("history monthly revision sequence conflicts")
        if observed_status not in {"qfq_missing", "unadjusted_missing"}:
            return
        previous_row = connection.run(
            f"SELECT {_REVISION_COLUMNS} FROM daily_price_revision AS records "
            "WHERE records.trade_date=? AND records.code=? AND records.revision_id=?",
            (item.trade_date, value.code, observed_revision),
        ).fetchone()
        if previous_row is None:
            raise HistoryPricePartitionConflictError("history monthly observation parent is missing")
        previous_cell = _decode_row(tuple(previous_row)).cell
        if previous_cell.status != observed_status or not value.retains_partial_prices(previous_cell):
            raise HistoryPricePartitionConflictError("history monthly retained price conflicts")

    def _require_metadata(self, connection: SqliteConnection) -> None:
        metadata = connection.run(
            "SELECT schema_id, calendar_year, schema_version FROM schema_contract WHERE singleton=1"
        ).fetchone()
        if metadata is None or tuple(metadata) != (_SCHEMA_IDENTITY, self._calendar_year, 2):
            raise HistoryPricePartitionError("history month metadata conflicts")

    def _require_schema(self, connection: SqliteConnection) -> None:
        expected_indexes = {
            "history_price_code_date_idx": (
                "code",
                "trade_date",
                "first_seen_sequence",
                "revision_id",
            ),
            "history_price_date_board_code_idx": (
                "trade_date",
                "board",
                "code",
                "first_seen_sequence",
                "revision_id",
            ),
            "history_price_observation_code_date_idx": (
                "code",
                "trade_date",
                "sync_sequence",
                "revision_id",
            ),
        }
        for name, expected in expected_indexes.items():
            columns = tuple(row[2] for row in connection.run(f"PRAGMA index_info('{name}')"))
            if columns != expected:
                raise HistoryPricePartitionError("history month index contract is invalid")
        for table_name in ("daily_price_revision", "daily_price_observation"):
            table_sql_row = connection.run(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            ).fetchone()
            if table_sql_row is None or "WITHOUT ROWID" not in cast(str, table_sql_row[0]).upper():
                raise HistoryPricePartitionError("history month table contract is invalid")

    def _write_connection(self) -> SqliteConnection:
        return OpenSqliteConnection(
            self._path,
            SqliteConnectionOptions(
                journal_mode="WAL", foreign_keys=True, cache_kib=8192, mmap_bytes=0, temp_storage="FILE"
            ),
            statement_trace=self._statement_trace,
        )

    def _read_connection(self) -> SqliteConnection:
        return OpenHistoryPriceReader(
            self._path, statement_trace=self._statement_trace, mmap_bytes=0, activity=self._activity
        )


class HistoryPriceWriteSession:
    """Reuse one month's connection without weakening per-batch FULL durability."""

    def __init__(
        self, prices: HistoryPricePartition, *, fresh: bool = False, pending_sequence: int | None = None
    ) -> None:
        self._prices = prices
        self._fresh = fresh
        self._pending_sequence = pending_sequence
        self._connection = prices._write_connection()
        try:
            prices._require_metadata(self._connection)
        except (HistoryPricePartitionError, sqlite3.Error):
            self._connection.close()
            raise

    def save_revision_batches(self, revisions: Iterable[HistoryRevision]) -> None:
        incoming = iter(revisions)
        try:
            with self._prices._activity.watch(self._connection) if self._prices._activity else nullcontext():
                while batch := tuple(islice(incoming, 4096)):
                    if shutil.disk_usage(self._prices._path.parent).free < 64 * 1024**2:
                        raise HistoryPricePartitionError("disk_space_insufficient")
                    with SqliteTransaction(self._connection, immediate=True):
                        prepared = self._prices._prepare_revisions(batch)
                        if self._fresh or self._prices._can_insert_disjoint(self._connection, prepared):
                            self._prices._insert_fresh_batch(self._connection, prepared)
                        else:
                            self._prices._save_batch(
                                self._connection, prepared, pending_sequence=self._pending_sequence
                            )
                            self._connection.run("DROP TABLE requested_history_revisions")
        except sqlite3.Error as exc:
            raise HistoryPricePartitionError("history month revision write failed") from exc

    def close(self) -> None:
        try:
            self._connection.close()
        except sqlite3.Error as exc:
            raise HistoryPricePartitionError("history month writer close failed") from exc


def _latest_query(code: str | None, board: str | None) -> tuple[str, tuple[str, ...]]:
    if code is not None and board is not None:
        return _LATEST_CODE_BOARD_SQL, (code, board)
    if code is not None:
        return _LATEST_CODE_SQL, (code,)
    if board is not None:
        return _LATEST_BOARD_SQL, (board,)
    return _LATEST_RANGE_SQL, ()


def _require_revision_identity(
    existing: tuple[int, PriceValues, str],
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
    if existing_payload != RevisionColumnValues(persisted) or existing_hash != persisted.content_hash:
        raise HistoryPricePartitionConflictError("history monthly revision identity conflicts")


def _decode_row(row: tuple[object, ...]) -> HistoryRevision:
    try:
        return RevisionFromColumns(row)
    except (TypeError, ValueError) as exc:
        raise HistoryPricePartitionError("history price row invalid") from exc


def _decode_training_row(row: tuple[object, ...]) -> HistoryTrainingRow:
    if len(row) != 12:
        raise HistoryPricePartitionError("history month training row shape is invalid")
    (
        trade_date_value,
        code,
        board,
        cell_status,
        raw_status,
        qfq_status,
        qfq_close,
        qfq_amount,
        qfq_open,
        qfq_high,
        qfq_low,
        raw_volume,
    ) = row
    if (
        type(trade_date_value) is not int
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
        raise HistoryPricePartitionError("history month training row fields are invalid")
    trade_date = DecodeDate(trade_date_value)
    point = (
        HistoryTrainingPoint(
            trade_date,
            _training_number(qfq_close),
            _training_number(qfq_amount),
            _training_number(qfq_open),
            _training_number(qfq_high),
            _training_number(qfq_low),
            _training_number(raw_volume),
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
        raise HistoryPricePartitionError("history month training number is invalid")
    return float(value)


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
    "HistoryPricePartitionConflictError",
    "HistoryPricePartitionError",
    "HistoryPartitionVerificationPhase",
    "HistoryPricePartition",
    "HistoryPriceWriteSession",
]
