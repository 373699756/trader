"""Atomic first-wins decisions and recovery checkpoints in one SQLite database."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from trader.infra.sqlite_connection import (
    CheckSqliteIntegrity,
    EnsureSqliteSchema,
    SqliteConnection,
    SqliteConnectionOptions,
    SqliteSchema,
    SqliteSession,
    SqliteTransaction,
)
from trader.recommendation.application.ports.decision_records import (
    DecisionCheckpoint,
    DecisionRecordConflictError,
    DecisionRecordRecoverySummary,
    DecisionRecordUnavailableError,
)
from trader.recommendation.domain.publication.decision_identity import CommittedDecisionRecord, IsPreviewDecision
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.persistence.decision_record_codec import CommittedRecordBytes, CommittedRecordFromBytes

FaultInjector = Callable[[str], None]
_MAX_RECOVERY_PAYLOAD_BYTES = 8 * 1024 * 1024
_SCHEMA = SqliteSchema(
    1,
    (
        "CREATE TABLE decision_records(strategy TEXT NOT NULL,trade_date TEXT NOT NULL,version TEXT NOT NULL UNIQUE,"
        "payload_hash TEXT NOT NULL,payload_sha256 TEXT NOT NULL,payload BLOB NOT NULL,committed_at TEXT NOT NULL,"
        "commit_kind TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('committed','quarantined')),"
        "error TEXT NOT NULL DEFAULT '',PRIMARY KEY(strategy,trade_date))",
        "CREATE TABLE decision_checkpoints(strategy TEXT NOT NULL,trade_date TEXT NOT NULL,version TEXT NOT NULL UNIQUE,"
        "decision_version TEXT NOT NULL,observed_at TEXT NOT NULL,boundary_at TEXT NOT NULL,"
        "payload_sha256 TEXT NOT NULL,payload BLOB NOT NULL,consumed_at TEXT,error TEXT NOT NULL DEFAULT '',"
        "PRIMARY KEY(strategy,trade_date))",
    ),
)


class SQLiteDecisionRecords:
    """One immutable canonical payload per strategy/day; reads never initialize state."""

    def __init__(self, path: Path, *, fault_injector: FaultInjector | None = None) -> None:
        self._database = path
        self._fault_injector = fault_injector or (lambda _stage: None)
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self._database.parent.mkdir(parents=True, exist_ok=True)
        try:
            with SqliteSession(self._database, SqliteConnectionOptions(journal_mode="WAL")) as connection:
                EnsureSqliteSchema(connection, _SCHEMA, allow_create=True)
                CheckSqliteIntegrity(connection)
            self.recover()
        except (OSError, sqlite3.Error) as exc:
            raise DecisionRecordUnavailableError("decision record initialization failed") from exc

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
                EnsureSqliteSchema(connection, _SCHEMA)
                with SqliteTransaction(connection, immediate=write):
                    yield connection
        except (OSError, sqlite3.Error) as exc:
            raise DecisionRecordUnavailableError("decision database unavailable") from exc

    def commit(self, record: CommittedDecisionRecord) -> None:
        if IsPreviewDecision(record.decision):
            raise ValueError("preview decisions cannot be written to formal records")
        self.import_record_bytes(CommittedRecordBytes(record))

    def import_record_bytes(self, payload: bytes) -> None:
        """Preserve verified original bytes during explicitly authorized offline migration."""
        digest = _bounded_digest(payload)
        try:
            record = CommittedRecordFromBytes(payload)
        except (ValueError, TypeError, UnicodeError) as exc:
            raise DecisionRecordUnavailableError("decision payload invalid") from exc
        if record.strategy.value not in ("tomorrow", "d25"):
            raise ValueError("only scored strategies have formal records")
        with self._lock, self._connect(write=True) as connection:
            row = connection.run(
                "SELECT * FROM decision_records WHERE strategy=? AND trade_date=?",
                (record.strategy.value, record.trade_date.isoformat()),
            ).fetchone()
            if row is not None:
                self._load_record(row)
                if row["payload_sha256"] != digest or bytes(row["payload"]) != payload:
                    raise DecisionRecordConflictError("decision record is already committed with different content")
                return
            connection.run(
                "INSERT INTO decision_records VALUES (?,?,?,?,?,?,?,?,'committed','')",
                (
                    record.strategy.value,
                    record.trade_date.isoformat(),
                    record.version,
                    record.payload_hash,
                    digest,
                    payload,
                    record.committed_at.isoformat(),
                    record.commit_kind,
                ),
            )
            self._fault_injector("before_commit")
        self._fault_injector("after_commit")

    def load(self, strategy: Strategy, trade_date: date) -> CommittedDecisionRecord | None:
        if not self._database.exists():
            return None
        with self._lock, self._connect() as connection:
            row = connection.run(
                "SELECT * FROM decision_records WHERE strategy=? AND trade_date=?",
                (strategy.value, trade_date.isoformat()),
            ).fetchone()
        return self._load_record(row) if row is not None else None

    def list_dates(self, strategy: Strategy, *, limit: int = 31) -> tuple[date, ...]:
        if not 1 <= limit <= 366:
            raise ValueError("decision date limit must be between 1 and 366")
        if not self._database.exists():
            return ()
        with self._connect() as connection:
            rows = connection.run(
                "SELECT trade_date FROM decision_records WHERE strategy=? AND status='committed' ORDER BY trade_date DESC LIMIT ?",
                (strategy.value, limit),
            ).fetchall()
        return tuple(date.fromisoformat(row["trade_date"]) for row in rows)

    def save_checkpoint(self, checkpoint: DecisionCheckpoint) -> None:
        if IsPreviewDecision(checkpoint.decision):
            raise ValueError("preview decisions cannot be written to formal checkpoints")
        envelope = CommittedDecisionRecord(checkpoint.decision, checkpoint.boundary_at, "checkpoint_recovery")
        self.import_checkpoint_bytes(CommittedRecordBytes(envelope))

    def import_checkpoint_bytes(self, payload: bytes) -> None:
        """Import verified checkpoint bytes without rewriting their canonical representation."""
        envelope = CommittedRecordFromBytes(payload)
        if envelope.commit_kind != "checkpoint_recovery":
            raise ValueError("checkpoint_envelope_kind_invalid")
        checkpoint = DecisionCheckpoint(envelope.decision, envelope.committed_at)
        digest = _bounded_digest(payload)
        with self._lock, self._connect(write=True) as connection:
            row = connection.run(
                "SELECT * FROM decision_checkpoints WHERE strategy=? AND trade_date=?",
                (checkpoint.decision.strategy.value, checkpoint.decision.trade_date.isoformat()),
            ).fetchone()
            if row is not None:
                current = self._load_checkpoint_row(row)
                if row["version"] == checkpoint.version:
                    if bytes(row["payload"]) != payload:
                        raise DecisionRecordConflictError("decision checkpoint content conflict")
                    return
                if (
                    row["consumed_at"]
                    or current.boundary_at != checkpoint.boundary_at
                    or current.decision.observed_at >= checkpoint.decision.observed_at
                ):
                    raise DecisionRecordConflictError("decision checkpoint cannot be replaced")
            connection.run(
                "INSERT INTO decision_checkpoints VALUES (?,?,?,?,?,?,?,?,NULL,'') "
                "ON CONFLICT(strategy,trade_date) DO UPDATE SET version=excluded.version,decision_version=excluded.decision_version,"
                "observed_at=excluded.observed_at,boundary_at=excluded.boundary_at,payload_sha256=excluded.payload_sha256,"
                "payload=excluded.payload,consumed_at=NULL,error=''",
                (
                    checkpoint.decision.strategy.value,
                    checkpoint.decision.trade_date.isoformat(),
                    checkpoint.version,
                    checkpoint.decision.version,
                    checkpoint.decision.observed_at.isoformat(),
                    checkpoint.boundary_at.isoformat(),
                    digest,
                    payload,
                ),
            )

    def load_checkpoint(self, strategy: Strategy, trade_date: date) -> DecisionCheckpoint | None:
        if not self._database.exists():
            return None
        with self._lock, self._connect() as connection:
            row = connection.run(
                "SELECT * FROM decision_checkpoints WHERE strategy=? AND trade_date=?",
                (strategy.value, trade_date.isoformat()),
            ).fetchone()
        if row is None or row["consumed_at"]:
            return None
        return self._load_checkpoint_row(row)

    def consume_checkpoint(self, checkpoint: DecisionCheckpoint, *, consumed_at: datetime) -> None:
        if IsPreviewDecision(checkpoint.decision):
            raise ValueError("preview decisions cannot consume formal checkpoints")
        if consumed_at.tzinfo is None or consumed_at.utcoffset() is None:
            raise ValueError("checkpoint consumption requires an aware clock")
        with self._lock, self._connect(write=True) as connection:
            row = connection.run(
                "SELECT * FROM decision_checkpoints WHERE strategy=? AND trade_date=?",
                (checkpoint.decision.strategy.value, checkpoint.decision.trade_date.isoformat()),
            ).fetchone()
            if row is None or row["version"] != checkpoint.version:
                raise DecisionRecordConflictError("decision checkpoint consume conflict")
            self._load_checkpoint_row(row)
            if not row["consumed_at"]:
                connection.run(
                    "UPDATE decision_checkpoints SET consumed_at=? WHERE strategy=? AND trade_date=?",
                    (
                        consumed_at.isoformat(),
                        checkpoint.decision.strategy.value,
                        checkpoint.decision.trade_date.isoformat(),
                    ),
                )

    def recover(self) -> DecisionRecordRecoverySummary:
        quarantined = 0
        with self._lock, self._connect(write=True) as connection:
            for row in connection.run("SELECT * FROM decision_records WHERE status='committed'").fetchall():
                try:
                    self._load_record(row)
                except DecisionRecordUnavailableError:
                    connection.run(
                        "UPDATE decision_records SET status='quarantined',error='payload_invalid' WHERE strategy=? AND trade_date=?",
                        (row["strategy"], row["trade_date"]),
                    )
                    quarantined += 1
            for row in connection.run(
                "SELECT * FROM decision_checkpoints WHERE consumed_at IS NULL AND error=''"
            ).fetchall():
                try:
                    self._load_checkpoint_row(row)
                except DecisionRecordUnavailableError:
                    connection.run(
                        "UPDATE decision_checkpoints SET error='payload_invalid' WHERE strategy=? AND trade_date=?",
                        (row["strategy"], row["trade_date"]),
                    )
                    quarantined += 1
        return DecisionRecordRecoverySummary(quarantined=quarantined)

    @staticmethod
    def _load_record(row: sqlite3.Row) -> CommittedDecisionRecord:
        if row["status"] != "committed":
            raise DecisionRecordUnavailableError("decision record quarantined")
        record = _decode_payload(row)
        if (
            record.strategy.value != row["strategy"]
            or record.trade_date.isoformat() != row["trade_date"]
            or record.version != row["version"]
            or record.payload_hash != row["payload_hash"]
            or record.committed_at.isoformat() != row["committed_at"]
            or record.commit_kind != row["commit_kind"]
        ):
            raise DecisionRecordUnavailableError("decision record identity mismatch")
        return record

    @staticmethod
    def _load_checkpoint_row(row: sqlite3.Row) -> DecisionCheckpoint:
        if row["error"]:
            raise DecisionRecordUnavailableError("decision checkpoint quarantined")
        envelope = _decode_payload(row)
        try:
            checkpoint = DecisionCheckpoint(envelope.decision, envelope.committed_at)
        except (TypeError, ValueError) as exc:
            raise DecisionRecordUnavailableError("decision checkpoint invalid") from exc
        if (
            checkpoint.version != row["version"]
            or checkpoint.decision.version != row["decision_version"]
            or checkpoint.boundary_at.isoformat() != row["boundary_at"]
            or envelope.commit_kind != "checkpoint_recovery"
            or checkpoint.decision.strategy.value != row["strategy"]
            or checkpoint.decision.trade_date.isoformat() != row["trade_date"]
            or checkpoint.decision.observed_at.isoformat() != row["observed_at"]
        ):
            raise DecisionRecordUnavailableError("decision checkpoint identity mismatch")
        return checkpoint


def _bounded_digest(payload: bytes) -> str:
    if not 0 < len(payload) <= _MAX_RECOVERY_PAYLOAD_BYTES:
        raise DecisionRecordUnavailableError("decision payload exceeds the bounded limit")
    return hashlib.sha256(payload).hexdigest()


def _decode_payload(row: sqlite3.Row) -> CommittedDecisionRecord:
    raw = row["payload"]
    if not isinstance(raw, bytes) or _bounded_digest(raw) != row["payload_sha256"]:
        raise DecisionRecordUnavailableError("decision payload hash mismatch")
    try:
        return CommittedRecordFromBytes(raw)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise DecisionRecordUnavailableError("decision payload invalid") from exc


__all__ = ["SQLiteDecisionRecords"]
