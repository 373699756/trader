"""Atomic first-wins freeze records with verified canonical bytes."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy

import hashlib
import sqlite3
import threading
from calendar import monthrange
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from trader.infra.cache import BoundedLruCache
from trader.infra.cache_contracts import CacheIdentity
from trader.infra.sqlite_codec import DecodeDate, EncodeDate, EncodeHash, EncodeTime
from trader.infra.sqlite_connection import (
    CheckSqliteIntegrity,
    EnsureSqliteSchema,
    SqliteConnection,
    SqliteConnectionOptions,
    SqliteSchema,
    SqliteSession,
    SqliteTransaction,
)
from trader.recommendation.application.ports.freeze_records import (
    FreezeRecordConflictError,
    FreezeRecordRecoverySummary,
    FreezeRecordUnavailableError,
)
from trader.recommendation.domain.publication.freeze_identity import CommittedFreezeRecord, IsPreviewFreeze
from trader.recommendation.domain.publication.historical_freeze import HistoricalFreezeRecord
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.recommendation.infra.persistence.freeze_record_codec import CommittedRecordBytes, CommittedRecordFromBytes
from trader.recommendation.infra.persistence.historical_freeze_codec import HistoricalRecordFromBytes

FaultInjector = Callable[[str], None]
_MAX_RECOVERY_PAYLOAD_BYTES = 8 * 1024 * 1024
_SCHEMA = SqliteSchema(
    (
        "CREATE TABLE freeze_record("
        "strategy TEXT NOT NULL CHECK(strategy IN ('tomorrow','d25')),trade_date INTEGER NOT NULL,"
        "freeze_id TEXT NOT NULL UNIQUE,freeze_hash BLOB NOT NULL CHECK(length(freeze_hash)=32),"
        "record_hash BLOB NOT NULL CHECK(length(record_hash)=32),record_bytes BLOB NOT NULL,"
        "committed_at INTEGER NOT NULL,commit_kind TEXT NOT NULL,"
        "status TEXT NOT NULL CHECK(status IN ('committed','quarantined')),failure_code TEXT,"
        "PRIMARY KEY(strategy,trade_date))",
    ),
)
_GROUPED_SCHEMA = SqliteSchema(
    (
        "CREATE TABLE freeze_record("
        "strategy TEXT NOT NULL CHECK(strategy='tomorrow'),trade_date INTEGER NOT NULL,"
        "freeze_id TEXT NOT NULL UNIQUE,freeze_hash BLOB NOT NULL CHECK(length(freeze_hash)=32),"
        "record_hash BLOB NOT NULL CHECK(length(record_hash)=32),record_bytes BLOB NOT NULL,"
        "committed_at INTEGER NOT NULL,commit_kind TEXT NOT NULL,"
        "status TEXT NOT NULL CHECK(status IN ('committed','quarantined')),failure_code TEXT,"
        "prediction_target TEXT NOT NULL,PRIMARY KEY(trade_date,prediction_target))",
        "CREATE TABLE tomorrow_profile_day("
        "profile TEXT NOT NULL CHECK(profile IN ('v1','v2','v3','v5')),trade_date INTEGER NOT NULL,"
        "freeze_id TEXT NOT NULL REFERENCES freeze_record(freeze_id),PRIMARY KEY(profile,trade_date))",
    ),
)


@dataclass(frozen=True)
class _FreezeDates:
    dates: tuple[date, ...]


class SQLiteFreezeRecords:
    """One immutable canonical payload per strategy/day; reads never initialize state."""

    def __init__(
        self,
        path: Path,
        *,
        fault_injector: FaultInjector | None = None,
        cache: BoundedLruCache[object] | None = None,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc), sqlite_policy: SqliteIoPolicy,
        read_session: Callable[[Path], AbstractContextManager[SqliteConnection]] | None = None,
        grouped: bool = False,
    ) -> None:
        self._sqlite_policy = sqlite_policy
        self._read_session = read_session
        self._database = path
        self._fault_injector = fault_injector or (lambda _stage: None)
        self._lock = threading.RLock()
        self._cache = cache
        self._schema = _GROUPED_SCHEMA if grouped else _SCHEMA
        self._wall_clock = wall_clock
        self._cache_source = "freeze:" + hashlib.sha256(str(path.absolute()).encode()).hexdigest()[:24]

    def initialize(self) -> None:
        self._require_current_archive()
        self._database.parent.mkdir(parents=True, exist_ok=True)
        try:
            if self._database.exists():
                self._validate_current_structure()
            with SqliteSession(self._database, SqliteConnectionOptions(journal_mode="WAL"), sqlite_policy=self._sqlite_policy) as connection:
                EnsureSqliteSchema(
                    connection, self._schema, allow_create=True, exact=True,
                )
        except (OSError, sqlite3.Error) as exc:
            raise FreezeRecordUnavailableError("freeze record initialization failed") from exc

    @contextmanager
    def _connect(self, *, write: bool = False) -> Iterator[SqliteConnection]:
        try:
            if write:
                self._require_current_archive()
                self._validate_current_structure()
            session = self._read_session(self._database) if not write and self._read_session is not None else SqliteSession(
                self._database,
                SqliteConnectionOptions(
                    read_only=not write,
                    journal_mode="WAL" if write else None,
                ), sqlite_policy=self._sqlite_policy,
            )
            with session as connection:
                EnsureSqliteSchema(connection, self._schema, exact=True)
                with SqliteTransaction(connection, immediate=write):
                    yield connection
        except InterruptedError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise FreezeRecordUnavailableError("freeze database unavailable") from exc

    def _validate_current_structure(self) -> None:
        with SqliteSession(self._database, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as connection:
            EnsureSqliteSchema(connection, self._schema, exact=True)

    def _require_current_archive(self) -> None:
        if self._schema is _GROUPED_SCHEMA:
            raise FreezeRecordUnavailableError("grouped Tomorrow history is read-only")

    def commit(self, record: CommittedFreezeRecord) -> None:
        if not isinstance(record, CommittedFreezeRecord):
            raise ValueError("history is read-only and cannot be committed")
        if IsPreviewFreeze(record.freeze):
            raise ValueError("preview freezes cannot be written to formal records")
        self.import_record_bytes(CommittedRecordBytes(record))

    def import_record_bytes(self, payload: bytes) -> None:
        """Preserve verified original bytes during explicitly authorized offline migration."""
        digest = _bounded_digest(payload)
        try:
            record = CommittedRecordFromBytes(payload)
        except (ValueError, TypeError, UnicodeError) as exc:
            raise FreezeRecordUnavailableError("freeze payload invalid") from exc
        if record.strategy.value not in ("tomorrow", "d25"):
            raise ValueError("only scored strategies have formal records")
        with self._lock, self._connect(write=True) as connection:
            if self._cache is not None:
                self._cache.discard_scope("published_date_index", self._cache_source)
            row = connection.run(
                "SELECT * FROM freeze_record WHERE strategy=? AND trade_date=?",
                (record.strategy.value, EncodeDate(record.trade_date)),
            ).fetchone()
            if row is not None:
                self._load_record(row)
                if row["record_hash"] != digest or bytes(row["record_bytes"]) != payload:
                    raise FreezeRecordConflictError("freeze record is already committed with different content")
                return
            self._insert(connection, record, payload, digest)
            self._fault_injector("before_commit")
        self._fault_injector("after_commit")

    @staticmethod
    def _insert(
        connection: SqliteConnection, record: CommittedFreezeRecord, payload: bytes, digest: bytes,
    ) -> None:
        fields = (
            record.strategy.value,
            EncodeDate(record.trade_date),
            record.version,
            EncodeHash(record.payload_hash),
            digest,
            payload,
            EncodeTime(record.committed_at),
            record.commit_kind,
        )
        connection.run("INSERT INTO freeze_record VALUES (?,?,?,?,?,?,?,?,'committed',NULL)", fields)

    def load_tomorrow_profile(self, profile: ScoringProfileId, trade_date: date) -> CommittedFreezeRecord | None:
        row = self._tomorrow_profile_row(profile, trade_date)
        return None if row is None else self._load_record(row)

    def load_tomorrow_history(self, profile: ScoringProfileId, trade_date: date) -> HistoricalFreezeRecord | None:
        row = self._tomorrow_profile_row(profile, trade_date)
        return None if row is None else self._load_history_record(row)

    def _tomorrow_profile_row(self, profile: ScoringProfileId, trade_date: date) -> sqlite3.Row | None:
        if not self._database.exists():
            return None
        with self._lock, self._history_connection() as connection:
            return connection.run(
                "SELECT f.* FROM tomorrow_profile_day p JOIN freeze_record f USING(freeze_id) "
                "WHERE p.profile=? AND p.trade_date=? AND f.trade_date=p.trade_date", (profile, EncodeDate(trade_date)),
            ).fetchone()

    def tomorrow_records(self, trade_date: date) -> tuple[CommittedFreezeRecord, ...]:
        if not self._database.exists():
            return ()
        with self._lock, self._history_connection() as connection:
            rows = connection.run(
                "SELECT * FROM freeze_record WHERE trade_date=? AND status='committed' ORDER BY prediction_target",
                (EncodeDate(trade_date),),
            ).fetchall()
        return tuple(self._load_record(row) for row in rows)

    def list_tomorrow_profile_dates(
        self, profile: ScoringProfileId, *, limit: int, year: int | None, month: int | None,
    ) -> tuple[date, ...]:
        if (year is None) != (month is None) or not 1 <= limit <= 366:
            raise ValueError("invalid Tomorrow profile date range")
        if not self._database.exists():
            return ()
        with self._lock, self._history_connection() as connection:
            query = (
                "SELECT p.trade_date FROM tomorrow_profile_day p JOIN freeze_record f USING(freeze_id) "
                "WHERE p.profile=? AND f.status='committed' "
            )
            if year is None or month is None:
                rows = connection.run(query + "ORDER BY p.trade_date DESC LIMIT ?", (profile, limit)).fetchall()
            else:
                first = EncodeDate(date(year, month, 1))
                last = EncodeDate(date(year, month, monthrange(year, month)[1]))
                rows = connection.run(
                    query + "AND p.trade_date BETWEEN ? AND ? ORDER BY p.trade_date DESC", (profile, first, last),
                ).fetchall()
        return tuple(DecodeDate(row["trade_date"]) for row in rows)

    def load(self, strategy: Strategy, trade_date: date) -> CommittedFreezeRecord | None:
        identity = self._cache_identity("published_recommendation_view", strategy, trade_date.isoformat())
        with self._lock:
            cached = self._cache.get(identity) if self._cache is not None else None
            if cached is not None and isinstance(cached.value, CommittedFreezeRecord):
                return cached.value
            if not self._database.exists():
                return None
            with self._connect() as connection:
                row = connection.run(
                    "SELECT * FROM freeze_record WHERE strategy=? AND trade_date=?",
                    (strategy.value, EncodeDate(trade_date)),
                ).fetchone()
            if row is None:
                return None
            record = self._load_record(row)
            if self._cache is not None:
                self._cache.put(identity, record, data_version=record.version, source_time=record.committed_at)
            return record

    @contextmanager
    def _history_connection(self) -> Iterator[SqliteConnection]:
        try:
            session = self._read_session(self._database) if self._read_session is not None else SqliteSession(
                self._database, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy)
            with session as connection:
                EnsureSqliteSchema(connection, self._schema, exact=self._schema is _GROUPED_SCHEMA)
                with SqliteTransaction(connection):
                    yield connection
        except InterruptedError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise FreezeRecordUnavailableError("freeze history unavailable") from exc

    def load_history(self, strategy: Strategy, trade_date: date) -> HistoricalFreezeRecord | None:
        """Project saved bytes; never initialize, recover, rewrite or adopt a record."""
        with self._lock:
            if not self._database.exists():
                return None
            with self._history_connection() as connection:
                row = connection.run("SELECT * FROM freeze_record WHERE strategy=? AND trade_date=?",
                                     (strategy.value, EncodeDate(trade_date))).fetchone()
            return None if row is None else self._load_history_record(row)

    def list_dates(
        self,
        strategy: Strategy,
        *,
        limit: int = 31,
        year: int | None = None,
        month: int | None = None,
    ) -> tuple[date, ...]:
        if (year is None) != (month is None):
            raise ValueError("freeze date month requires both year and month")
        month_start: date | None = None
        month_end: date | None = None
        if year is not None and month is not None:
            month_start = date(year, month, 1)
            month_end = date(year, month, monthrange(year, month)[1])
        elif not 1 <= limit <= 366:
            raise ValueError("freeze date limit must be between 1 and 366")
        date_key = f"{year:04d}-{month:02d}" if year is not None and month is not None else "dates"
        identity = self._cache_identity("published_date_index", strategy, date_key)
        with self._lock:
            cached = self._cache.get(identity) if self._cache is not None else None
            if cached is not None and isinstance(cached.value, _FreezeDates) and cached.state == "fresh":
                return cached.value.dates if month_start is not None else cached.value.dates[:limit]
            if not self._database.exists():
                return ()
            with self._history_connection() as connection:
                if month_start is not None and month_end is not None:
                    rows = connection.run(
                        "SELECT trade_date FROM freeze_record "
                        "WHERE strategy=? AND status='committed' AND trade_date>=? AND trade_date<=? "
                        "ORDER BY trade_date DESC",
                        (strategy.value, EncodeDate(month_start), EncodeDate(month_end)),
                    ).fetchall()
                else:
                    rows = connection.run(
                        "SELECT trade_date FROM freeze_record WHERE strategy=? AND status='committed' "
                        "ORDER BY trade_date DESC LIMIT ?",
                        (strategy.value, 366 if self._cache is not None else limit),
                    ).fetchall()
            dates = tuple(DecodeDate(row["trade_date"]) for row in rows)
            if self._cache is not None:
                self._cache.put(identity, _FreezeDates(dates), data_version="freeze_dates", source_time=self._wall_clock())
            return dates if month_start is not None else dates[:limit]

    def list_settlement_dates(
        self, strategy: Strategy, *, after: date | None, limit: int
    ) -> tuple[date, ...]:
        if not 1 <= limit <= 366:
            raise ValueError("settlement date limit must be between 1 and 366")
        if not self._database.exists():
            return ()
        with self._lock, self._history_connection() as connection:
            rows = connection.run(
                "SELECT DISTINCT trade_date FROM freeze_record WHERE strategy=? AND status='committed' "
                "AND trade_date>? ORDER BY trade_date LIMIT ?",
                (strategy.value, EncodeDate(after) if after is not None else 0, limit),
            ).fetchall()
        return tuple(DecodeDate(row["trade_date"]) for row in rows)

    def _cache_identity(self, dataset: str, strategy: Strategy, trade_date: str) -> CacheIdentity:
        return CacheIdentity(
            dataset,
            self._cache_source,
            strategy.value,
            (),
            trade_date,
            "formal",
            None,
            "immutable",
        )

    def recover(self) -> FreezeRecordRecoverySummary:
        quarantined = 0
        with self._lock, self._connect(write=True) as connection:
            if self._cache is not None:
                self._cache.discard_scope("published_recommendation_view", self._cache_source)
                self._cache.discard_scope("published_date_index", self._cache_source)
            CheckSqliteIntegrity(connection)
            for row in connection.run("SELECT * FROM freeze_record WHERE status='committed'").fetchall():
                try:
                    self._load_history_record(row)
                except FreezeRecordUnavailableError:
                    connection.run(
                        "UPDATE freeze_record SET status='quarantined',failure_code='payload_invalid' WHERE freeze_id=?",
                        (row["freeze_id"],),
                    )
                    quarantined += 1
        return FreezeRecordRecoverySummary(quarantined=quarantined)

    @staticmethod
    def _load_history_record(row: sqlite3.Row) -> HistoricalFreezeRecord:
        if row["status"] != "committed":
            raise FreezeRecordUnavailableError("freeze record quarantined")
        raw = row["record_bytes"]
        if not isinstance(raw, bytes) or _bounded_digest(raw) != row["record_hash"]:
            raise FreezeRecordUnavailableError("freeze payload hash mismatch")
        try:
            record = HistoricalRecordFromBytes(raw)
        except (ValueError, TypeError, UnicodeError) as exc:
            raise FreezeRecordUnavailableError("historical freeze payload invalid") from exc
        _verify_row_identity(row, record)
        return record

    @staticmethod
    def _load_record(row: sqlite3.Row) -> CommittedFreezeRecord:
        if row["status"] != "committed":
            raise FreezeRecordUnavailableError("freeze record quarantined")
        record = _decode_payload(row)
        _verify_row_identity(row, record)
        return record


def _verify_row_identity(row: sqlite3.Row, record: CommittedFreezeRecord | HistoricalFreezeRecord) -> None:
    if (
            record.strategy.value != row["strategy"]
            or EncodeDate(record.trade_date) != row["trade_date"]
            or record.version != row["freeze_id"]
            or EncodeHash(record.payload_hash) != row["freeze_hash"]
            or EncodeTime(record.committed_at) != row["committed_at"]
            or record.commit_kind != row["commit_kind"]
            or ("prediction_target" in row.keys() and dict(record.freeze.input_versions).get("prediction_target")
                != row["prediction_target"])
        ):
        raise FreezeRecordUnavailableError("freeze record identity mismatch")


def _bounded_digest(payload: bytes) -> bytes:
    if not 0 < len(payload) <= _MAX_RECOVERY_PAYLOAD_BYTES:
        raise FreezeRecordUnavailableError("freeze payload exceeds the bounded limit")
    return hashlib.sha256(payload).digest()


def _decode_payload(row: sqlite3.Row) -> CommittedFreezeRecord:
    raw = row["record_bytes"]
    if not isinstance(raw, bytes) or _bounded_digest(raw) != row["record_hash"]:
        raise FreezeRecordUnavailableError("freeze payload hash mismatch")
    try:
        return CommittedRecordFromBytes(raw)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise FreezeRecordUnavailableError("freeze payload invalid") from exc


__all__ = ["SQLiteFreezeRecords"]
