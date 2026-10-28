"""Atomic first-wins freeze records with verified canonical bytes."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path

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


class SQLiteFreezeRecords:
    """One immutable canonical payload per strategy/day; reads never initialize state."""

    def __init__(self, path: Path, *, fault_injector: FaultInjector | None = None) -> None:
        self._database = path
        self._fault_injector = fault_injector or (lambda _stage: None)
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self._database.parent.mkdir(parents=True, exist_ok=True)
        try:
            with SqliteSession(self._database, SqliteConnectionOptions(journal_mode="WAL")) as connection:
                EnsureSqliteSchema(
                    connection, _SCHEMA, allow_create=True, initialize=_initialize_schema, validate=_validate_schema
                )
                CheckSqliteIntegrity(connection)
            self.recover()
        except (OSError, sqlite3.Error) as exc:
            raise FreezeRecordUnavailableError("freeze record initialization failed") from exc

    @contextmanager
    def _connect(self, *, write: bool = False) -> Iterator[SqliteConnection]:
        try:
            with SqliteSession(
                self._database,
                SqliteConnectionOptions(
                    read_only=not write,
                    timeout_seconds=10.0,
                    journal_mode="WAL" if write else None,
                ),
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
        if not self._database.exists():
            return None
        with self._lock, self._connect() as connection:
            row = connection.run(
                "SELECT * FROM freeze_record WHERE strategy=? AND trade_date=?",
                (strategy.value, EncodeDate(trade_date)),
            ).fetchone()
        return self._load_record(row) if row is not None else None

    def list_dates(self, strategy: Strategy, *, limit: int = 31) -> tuple[date, ...]:
        if not 1 <= limit <= 366:
            raise ValueError("freeze date limit must be between 1 and 366")
        if not self._database.exists():
            return ()
        with self._connect() as connection:
            rows = connection.run(
                "SELECT trade_date FROM freeze_record WHERE strategy=? AND status='committed' ORDER BY trade_date DESC LIMIT ?",
                (strategy.value, limit),
            ).fetchall()
        return tuple(DecodeDate(row["trade_date"]) for row in rows)

    def recover(self) -> FreezeRecordRecoverySummary:
        quarantined = 0
        with self._lock, self._connect(write=True) as connection:
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
