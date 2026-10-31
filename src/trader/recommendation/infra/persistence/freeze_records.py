"""Atomic first-wins freeze records with verified canonical bytes."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy

import hashlib
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
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
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.persistence.freeze_record_codec import CommittedRecordBytes, CommittedRecordFromBytes

FaultInjector = Callable[[str], None]
_MAX_RECOVERY_PAYLOAD_BYTES = 8 * 1024 * 1024
_SCHEMA = SqliteSchema(
    2,
    (
        "CREATE TABLE schema_contract(schema_id TEXT NOT NULL PRIMARY KEY, schema_version INTEGER NOT NULL)",
        "CREATE TABLE freeze_record("
        "strategy TEXT NOT NULL CHECK(strategy IN ('tomorrow','d25')),trade_date INTEGER NOT NULL,"
        "freeze_id TEXT NOT NULL UNIQUE,freeze_hash BLOB NOT NULL CHECK(length(freeze_hash)=32),"
        "record_hash BLOB NOT NULL CHECK(length(record_hash)=32),record_bytes BLOB NOT NULL,"
        "committed_at INTEGER NOT NULL,commit_kind TEXT NOT NULL,"
        "status TEXT NOT NULL CHECK(status IN ('committed','quarantined')),failure_code TEXT,"
        "PRIMARY KEY(strategy,trade_date))",
    ),
)


def _initialize_schema(connection: SqliteConnection) -> None:
    connection.run("INSERT INTO schema_contract VALUES ('freeze_record',2)")


def _validate_schema(connection: SqliteConnection) -> None:
    rows = connection.run("SELECT schema_id,schema_version FROM schema_contract").fetchall()
    if [tuple(row) for row in rows] != [("freeze_record", 2)]:
        raise sqlite3.DatabaseError("freeze_schema_invalid")


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
    ) -> None:
        self._sqlite_policy = sqlite_policy
        self._database = path
        self._fault_injector = fault_injector or (lambda _stage: None)
        self._lock = threading.RLock()
        self._cache = cache
        self._wall_clock = wall_clock
        self._cache_source = "freeze:" + hashlib.sha256(str(path.absolute()).encode()).hexdigest()[:24]

    def initialize(self) -> None:
        self._database.parent.mkdir(parents=True, exist_ok=True)
        try:
            with SqliteSession(self._database, SqliteConnectionOptions(journal_mode="WAL"), sqlite_policy=self._sqlite_policy) as connection:
                EnsureSqliteSchema(
                    connection, _SCHEMA, allow_create=True, initialize=_initialize_schema, validate=_validate_schema
                )
        except (OSError, sqlite3.Error) as exc:
            raise FreezeRecordUnavailableError("freeze record initialization failed") from exc

    @contextmanager
    def _connect(self, *, write: bool = False) -> Iterator[SqliteConnection]:
        try:
            with SqliteSession(
                self._database,
                SqliteConnectionOptions(
                    read_only=not write,
                    journal_mode="WAL" if write else None,
                ), sqlite_policy=self._sqlite_policy,
            ) as connection:
                EnsureSqliteSchema(connection, _SCHEMA, validate=_validate_schema)
                with SqliteTransaction(connection, immediate=write):
                    yield connection
        except (OSError, sqlite3.Error) as exc:
            raise FreezeRecordUnavailableError("freeze database unavailable") from exc

    def commit(self, record: CommittedFreezeRecord) -> None:
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
            connection.run(
                "INSERT INTO freeze_record VALUES (?,?,?,?,?,?,?,?,'committed',NULL)",
                (
                    record.strategy.value,
                    EncodeDate(record.trade_date),
                    record.version,
                    EncodeHash(record.payload_hash),
                    digest,
                    payload,
                    EncodeTime(record.committed_at),
                    record.commit_kind,
                ),
            )
            self._fault_injector("before_commit")
        self._fault_injector("after_commit")

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

    def list_dates(self, strategy: Strategy, *, limit: int = 31) -> tuple[date, ...]:
        if not 1 <= limit <= 366:
            raise ValueError("freeze date limit must be between 1 and 366")
        identity = self._cache_identity("published_date_index", strategy, "dates")
        with self._lock:
            cached = self._cache.get(identity) if self._cache is not None else None
            if cached is not None and isinstance(cached.value, _FreezeDates) and cached.state == "fresh":
                return cached.value.dates[:limit]
            if not self._database.exists():
                return ()
            with self._connect() as connection:
                rows = connection.run(
                    "SELECT trade_date FROM freeze_record WHERE strategy=? AND status='committed' ORDER BY trade_date DESC LIMIT ?",
                    (strategy.value, 366 if self._cache is not None else limit),
                ).fetchall()
            dates = tuple(DecodeDate(row["trade_date"]) for row in rows)
            if self._cache is not None:
                self._cache.put(identity, _FreezeDates(dates), data_version="freeze_dates", source_time=self._wall_clock())
            return dates[:limit]

    def _cache_identity(self, dataset: str, strategy: Strategy, trade_date: str) -> CacheIdentity:
        return CacheIdentity(
            dataset,
            self._cache_source,
            strategy.value,
            (),
            trade_date,
            "formal",
            "2",
            "immutable",
            "2",
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
                    self._load_record(row)
                except FreezeRecordUnavailableError:
                    connection.run(
                        "UPDATE freeze_record SET status='quarantined',failure_code='payload_invalid' WHERE strategy=? AND trade_date=?",
                        (row["strategy"], row["trade_date"]),
                    )
                    quarantined += 1
        return FreezeRecordRecoverySummary(quarantined=quarantined)

    @staticmethod
    def _load_record(row: sqlite3.Row) -> CommittedFreezeRecord:
        if row["status"] != "committed":
            raise FreezeRecordUnavailableError("freeze record quarantined")
        record = _decode_payload(row)
        if (
            record.strategy.value != row["strategy"]
            or EncodeDate(record.trade_date) != row["trade_date"]
            or record.version != row["freeze_id"]
            or EncodeHash(record.payload_hash) != row["freeze_hash"]
            or EncodeTime(record.committed_at) != row["committed_at"]
            or record.commit_kind != row["commit_kind"]
        ):
            raise FreezeRecordUnavailableError("freeze record identity mismatch")
        return record


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
