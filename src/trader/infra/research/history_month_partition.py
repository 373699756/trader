"""SQLite boundary for one immutable-history calendar month."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from collections.abc import Callable, Collection, Iterable, Iterator
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal, cast

from trader.domain.research.history_control import HistorySnapshotPartition
from trader.domain.research.history_monthly import HistoryMonthlyRevision
from trader.infra.research.history_month_codec import (
    decode_history_monthly_revision,
    encode_history_monthly_revision,
)

_SCHEMA_IDENTITY = "history_month_partition"
_CODE = re.compile(r"^[0-9]{6}$")
_HASH_CHUNK_BYTES = 1024 * 1024
_CACHE_RELEASE_BYTES = 16 * 1024 * 1024
HistoryPartitionVerificationPhase = Literal["hash", "integrity", "row_count"]
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
"""
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
SELECT records.trade_date, records.code, records.revision_id, records.first_seen_sequence,
       records.board, records.payload_json, records.content_hash
FROM latest
JOIN daily_records AS records
  ON records.trade_date = latest.trade_date
 AND records.code = latest.code
 AND records.revision_id = latest.revision_id
WHERE latest.revision_rank = 1
{record_filter}
ORDER BY records.trade_date, records.code
"""
_LATEST_RANGE_SQL = _LATEST_WINDOW.format(observation_filter="", record_filter="")
_LATEST_CODE_SQL = _LATEST_WINDOW.format(
    observation_filter="      AND code = ?",
    record_filter="",
)
_LATEST_BOARD_SQL = _LATEST_WINDOW.format(
    observation_filter="",
    record_filter="  AND records.board = ?",
)
_LATEST_CODE_BOARD_SQL = _LATEST_WINDOW.format(
    observation_filter="      AND code = ?",
    record_filter="  AND records.board = ?",
)
_COUNT_CODE_BATCH_SIZE = 500


@dataclass(frozen=True)
class _EncodedRevision:
    value: HistoryMonthlyRevision
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


class SQLiteHistoryMonthPartitionRepository:
    def __init__(
        self,
        path: Path,
        calendar_year: int,
        calendar_month: int,
        *,
        statement_trace: Callable[[str], None] | None = None,
        immutable_read: bool = False,
    ) -> None:
        if calendar_year < 1990 or not 1 <= calendar_month <= 12:
            raise ValueError("history month partition identity is invalid")
        self._path = path
        self._calendar_year = calendar_year
        self._calendar_month = calendar_month
        self._statement_trace = statement_trace
        self._immutable_read = immutable_read

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        new_database = not self._path.exists() or self._path.stat().st_size == 0
        try:
            with closing(sqlite3.connect(self._path, timeout=5.0)) as connection, connection:
                if new_database:
                    connection.execute("PRAGMA page_size=8192")
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                connection.executescript(_SCHEMA)
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

    def save_revisions(self, revisions: Iterable[HistoryMonthlyRevision]) -> None:
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
    ) -> tuple[HistoryMonthlyRevision, ...]:
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
    ) -> tuple[HistoryMonthlyRevision, ...]:
        if _CODE.fullmatch(code) is None:
            raise ValueError("history month code is invalid")
        return tuple(self.iter_range(start, end, snapshot_sequence=snapshot_sequence, code=code))

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
    ) -> Iterator[HistoryMonthlyRevision]:
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

    def seal(self) -> HistorySnapshotPartition:
        try:
            with closing(sqlite3.connect(self._path, timeout=5.0)) as connection:
                self._require_metadata(connection)
                self._require_schema(connection)
                check = connection.execute("PRAGMA quick_check").fetchone()
                if check != ("ok",):
                    raise HistoryMonthPartitionError("history month integrity check failed")
                checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint is None or checkpoint[0] != 0:
                    raise HistoryMonthPartitionError("history month WAL checkpoint failed")
                row_count = self._validate_all_rows(connection)
        except HistoryMonthPartitionError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise HistoryMonthPartitionError("history month sealing failed") from exc
        try:
            _fsync_file(self._path)
            if self._path.parent.name != f"{self._calendar_year:04d}" or self._path.parent.parent.name != "partitions":
                raise HistoryMonthPartitionError("history month partition path is outside the archive layout")
            database_sha256 = _sha256_file(self._path)
            reference = HistorySnapshotPartition(
                f"partitions/{self._calendar_year:04d}/{self._calendar_month:02d}.sqlite3",
                database_sha256,
                row_count,
            )
            destination = self._path.parent / f"{self._calendar_month:02d}.sqlite3"
            if self._path != destination:
                os.replace(self._path, destination)
            _fsync_directory(destination.parent)
            self.verify(destination, reference)
            return reference
        except HistoryMonthPartitionError:
            raise
        except (OSError, ValueError) as exc:
            raise HistoryMonthPartitionError("history month sealing failed") from exc

    @classmethod
    def verify(
        cls,
        path: Path,
        reference: HistorySnapshotPartition,
        progress: HistoryPartitionVerificationProgress | None = None,
    ) -> None:
        if not path.is_file():
            raise HistoryMonthPartitionError("history month partition is missing")
        try:
            file_size = path.stat().st_size
            if _sha256_file(path, progress) != reference.sha256:
                raise HistoryMonthPartitionError("history month partition hash mismatch")
            wal = Path(f"{path}-wal")
            if wal.exists() and wal.stat().st_size > 0:
                raise HistoryMonthPartitionError("history month partition has pending WAL")
            parts = Path(reference.relative_path).parts
            year = int(parts[1])
            month = int(Path(parts[2]).stem)
            candidate = cls(path, year, month)
            with closing(candidate._read_connection()) as connection:
                if progress is not None:
                    progress(file_size, file_size, "integrity")
                candidate._require_metadata(connection)
                candidate._require_schema(connection)
                if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise HistoryMonthPartitionError("history month integrity check failed")
                if progress is not None:
                    progress(file_size, file_size, "row_count")
                row_count = cast(int, connection.execute("SELECT COUNT(*) FROM daily_records").fetchone()[0])
        except HistoryMonthPartitionError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise HistoryMonthPartitionError("history month verification failed") from exc
        finally:
            _release_file_cache(path)
        if row_count != reference.row_count:
            raise HistoryMonthPartitionError("history month partition row count mismatch")

    def _prepare_revisions(self, revisions: Iterable[HistoryMonthlyRevision]) -> tuple[_EncodedRevision, ...]:
        prepared: list[_EncodedRevision] = []
        previous_key: tuple[date, str, int, str] | None = None
        observations: dict[tuple[str, str, int], str] = {}
        for value in revisions:
            if value.trade_date.year != self._calendar_year or value.trade_date.month != self._calendar_month:
                raise ValueError("history revision does not belong to the target month")
            key = (value.trade_date, value.code, value.first_seen_sequence, value.revision_id)
            if previous_key is not None and key < previous_key:
                raise ValueError("history month revisions must be written in deterministic order")
            encoded = _EncodedRevision(value, value.trade_date.isoformat(), encode_history_monthly_revision(value))
            observed_revision = observations.setdefault(encoded.observation_key, value.revision_id)
            if observed_revision != value.revision_id:
                raise HistoryMonthPartitionConflictError("history monthly revision sequence conflicts")
            prepared.append(encoded)
            previous_key = key
        return tuple(prepared)

    def _save_batch(self, connection: sqlite3.Connection, prepared: tuple[_EncodedRevision, ...]) -> None:
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
            (cast(str, row[0]), cast(str, row[1]), cast(int, row[2])): cast(str, row[3])
            for row in connection.execute(
                "SELECT observations.trade_date, observations.code, observations.sync_sequence, "
                "observations.revision_id FROM daily_observations AS observations "
                "JOIN requested_history_revisions AS requested "
                "ON requested.trade_date=observations.trade_date AND requested.code=observations.code "
                "AND requested.sync_sequence=observations.sync_sequence"
            )
        }
        existing_revisions = {
            (cast(str, row[0]), cast(str, row[1]), cast(str, row[2])): (
                cast(int, row[3]),
                cast(str, row[4]),
                cast(str, row[5]),
            )
            for row in connection.execute(
                "SELECT records.trade_date, records.code, records.revision_id, records.first_seen_sequence, "
                "records.payload_json, records.content_hash FROM daily_records AS records "
                "JOIN requested_history_revisions AS requested "
                "ON requested.trade_date=records.trade_date AND requested.code=records.code "
                "AND requested.revision_id=records.revision_id GROUP BY records.trade_date, records.code, "
                "records.revision_id"
            )
        }
        latest_sequences = {
            (cast(str, row[0]), cast(str, row[1])): cast(int, row[2])
            for row in connection.execute(
                "SELECT observations.trade_date, observations.code, MAX(observations.sync_sequence) "
                "FROM daily_observations AS observations JOIN ("
                "SELECT DISTINCT trade_date, code FROM requested_history_revisions"
                ") AS requested ON requested.trade_date=observations.trade_date "
                "AND requested.code=observations.code GROUP BY observations.trade_date, observations.code"
            )
        }
        new_records: list[tuple[object, ...]] = []
        new_observations: list[tuple[object, ...]] = []
        for item in prepared:
            value = item.value
            observation_key = item.observation_key
            revision_key = item.revision_key
            observed_revision = existing_observations.get(observation_key)
            if observed_revision is not None:
                if observed_revision != value.revision_id:
                    raise HistoryMonthPartitionConflictError("history monthly revision sequence conflicts")
                existing = existing_revisions.get(revision_key)
                if existing is None:
                    raise HistoryMonthPartitionConflictError("history monthly observation parent is missing")
                _require_revision_identity(existing, value)
                continue
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
            existing_observations[observation_key] = value.revision_id
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
        cursor = connection.execute(
            "SELECT trade_date, code, revision_id, first_seen_sequence, board, payload_json, content_hash "
            "FROM daily_records ORDER BY trade_date, code, first_seen_sequence, revision_id"
        )
        row_count = 0
        while rows := cursor.fetchmany(512):
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
        connection.execute("PRAGMA temp_store=FILE")
        return connection

    def _read_connection(self) -> sqlite3.Connection:
        immutable = "&immutable=1" if self._immutable_read else ""
        connection = sqlite3.connect(f"file:{self._path.as_posix()}?mode=ro{immutable}", uri=True, timeout=5.0)
        if self._statement_trace is not None:
            connection.set_trace_callback(self._statement_trace)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA cache_size=-8192")
        connection.execute("PRAGMA mmap_size=0")
        connection.execute("PRAGMA temp_store=FILE")
        return connection


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
    value: HistoryMonthlyRevision,
) -> None:
    first_seen_sequence, existing_payload, existing_hash = existing
    persisted = HistoryMonthlyRevision(
        first_seen_sequence,
        value.board,
        value.cell,
        value.is_st,
        value.industry,
        value.industry_classification,
    )
    if existing_payload != encode_history_monthly_revision(persisted) or existing_hash != persisted.content_hash:
        raise HistoryMonthPartitionConflictError("history monthly revision identity conflicts")


def _decode_row(row: tuple[object, ...]) -> HistoryMonthlyRevision:
    if len(row) != 7:
        raise HistoryMonthPartitionError("history month row shape is invalid")
    trade_date_value, code, revision_id, first_seen_sequence, board, payload_json, content_hash = row
    if not all(
        isinstance(value, str) for value in (trade_date_value, code, revision_id, board, payload_json, content_hash)
    ):
        raise HistoryMonthPartitionError("history month row fields are invalid")
    if not isinstance(first_seen_sequence, int) or isinstance(first_seen_sequence, bool):
        raise HistoryMonthPartitionError("history month sequence is invalid")
    value = decode_history_monthly_revision(cast(str, payload_json))
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


def _sha256_file(path: Path, progress: HistoryPartitionVerificationProgress | None = None) -> str:
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
                _advise_file_access(
                    handle.fileno(),
                    released_offset,
                    offset - released_offset,
                    "POSIX_FADV_DONTNEED",
                )
                released_offset = offset
                if progress is not None:
                    progress(offset, total, "hash")
        if offset > released_offset:
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
    "SQLiteHistoryMonthPartitionRepository",
]
