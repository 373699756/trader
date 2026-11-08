"""Training-owned due and reminder transactions, independent of published history."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy

import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from datetime import date
from pathlib import Path
from typing import Literal, cast

from trader.download.domain.history_control import HistorySnapshot
from trader.infra.process_lock import ProcessLock, ProcessLockError
from trader.infra.sqlite_codec import DecodeDate, DecodeHash, DecodeTime, EncodeDate, EncodeHash, EncodeTime
from trader.infra.sqlite_connection import (
    EnsureSqliteSchema,
    HasSqliteSchema,
    SqliteConnection,
    SqliteConnectionOptions,
    SqliteSchema,
    SqliteSession,
    SqliteTransaction,
)
from trader.training.domain.training_due import TrainingDueReason, TrainingDueState
from trader.training.domain.training_reminder import (
    TrainingMaintenanceState,
    TrainingReminderClaim,
    TrainingReminderOutcome,
    TrainingReminderState,
)

MaintenanceKind = Literal["due", "reminder_claim", "reminder"]
MaintenanceRecord = TrainingDueState | TrainingReminderClaim | TrainingReminderState
_TABLES = {"due": "training_due", "reminder_claim": "training_reminder_claim", "reminder": "training_reminder"}
_SCHEMA = SqliteSchema((
    "CREATE TABLE schema_contract(singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
    "schema_id TEXT NOT NULL CHECK(schema_id='training_maintenance')) WITHOUT ROWID",
    "CREATE TABLE training_due(due_id TEXT PRIMARY KEY, reason TEXT NOT NULL, baseline_date INTEGER, "
    "cutoff_date INTEGER, matured_days INTEGER NOT NULL CHECK(matured_days>=0), "
    "input_revision INTEGER NOT NULL CHECK(input_revision IN (0,1)), observed_at INTEGER NOT NULL, "
    "content_hash BLOB NOT NULL CHECK(length(content_hash)=32)) WITHOUT ROWID",
    "CREATE TABLE training_reminder_claim(due_id TEXT NOT NULL REFERENCES training_due(due_id), "
    "reminder_date INTEGER NOT NULL, claimed_at INTEGER NOT NULL, "
    "content_hash BLOB NOT NULL CHECK(length(content_hash)=32), PRIMARY KEY(due_id,reminder_date)) WITHOUT ROWID",
    "CREATE TABLE training_reminder(due_id TEXT NOT NULL REFERENCES training_due(due_id), "
    "reminder_date INTEGER NOT NULL, outcome TEXT NOT NULL CHECK(outcome IN ('sent','notification_degraded')), "
    "attempted_at INTEGER NOT NULL, failure_code TEXT, content_hash BLOB NOT NULL CHECK(length(content_hash)=32), "
    "PRIMARY KEY(due_id,reminder_date)) WITHOUT ROWID",
))


class TrainingMaintenanceError(RuntimeError):
    """Training maintenance observations are unavailable or corrupt."""


class TrainingMaintenanceConflictError(TrainingMaintenanceError):
    """An immutable training maintenance identity conflicts."""


class TrainingMaintenanceIndex:
    """Own one mutable SQLite database; never modify or reseal download publications."""

    def __init__(self, path: Path, *, sqlite_policy: SqliteIoPolicy) -> None:
        self._sqlite_policy = sqlite_policy
        self._path = path

    def _is_missing(self) -> bool:
        try:
            metadata = self._path.lstat()
        except FileNotFoundError:
            return True
        except OSError as exc:
            raise TrainingMaintenanceError("training maintenance database unavailable") from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise TrainingMaintenanceError("training maintenance path is unsafe")
        return False

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[SqliteConnection]:
        try:
            missing = self._is_missing()
            if write and not missing:
                # Reject a miswired history catalog before a writable connection
                # can checkpoint committed WAL content into its database.
                with SqliteSession(
                    self._path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy,
                ) as existing:
                    HasSqliteSchema(existing, _SCHEMA, _validate_contract)
            if write:
                self._path.parent.mkdir(parents=True, exist_ok=True)
            lease = ProcessLock(self._path.with_suffix(".lock"), label="training-maintenance") if write else nullcontext()
            with lease:
                with (
                    SqliteSession(
                        self._path, SqliteConnectionOptions(read_only=not write, journal_mode="WAL" if write else None, foreign_keys=True), sqlite_policy=self._sqlite_policy
                    ) as connection,
                    SqliteTransaction(connection, immediate=write),
                ):
                    if write:
                        EnsureSqliteSchema(
                            connection, _SCHEMA, allow_create=True,
                            initialize=_initialize_contract, validate=_validate_contract,
                        )
                    yield connection
        except (OSError, sqlite3.Error, TypeError, ValueError, ProcessLockError) as exc:
            raise TrainingMaintenanceError("training maintenance database unavailable") from exc

    def save_due_state(self, value: TrainingDueState) -> None:
        self._save("due", value)

    def read_due_state(self, identity: str, *, latest: bool = False) -> TrainingDueState | None:
        if self._is_missing():
            return None
        with self._connection() as connection:
            if not HasSqliteSchema(connection, _SCHEMA, _validate_contract):
                return None
            row = connection.run(
                "SELECT * FROM training_due WHERE due_id=? OR due_id LIKE ? ORDER BY observed_at DESC,due_id DESC LIMIT 1",
                (identity, identity + ":%" if latest else identity),
            ).fetchone()
            return cast(TrainingDueState, _decode("due", row)) if row is not None else None

    def claim_reminder(self, value: TrainingReminderClaim) -> bool:
        """Commit a first-wins daily claim before the desktop notification side effect."""
        with self._connection(write=True) as connection:
            if (
                connection.run(
                    "SELECT 1 FROM training_reminder_claim WHERE due_id=? AND reminder_date=?",
                    (value.due_identity, EncodeDate(value.reminder_date)),
                ).fetchone()
                is not None
            ):
                return False
            connection.run("INSERT INTO training_reminder_claim VALUES(?,?,?,?)", _columns(value))
        return True

    def save_reminder(self, value: TrainingReminderState) -> None:
        self._save("reminder", value)

    def load_state(self, *, published_snapshot: HistorySnapshot | None = None) -> TrainingMaintenanceState:
        if self._is_missing():
            return TrainingMaintenanceState(published_snapshot, (), (), ())
        with self._connection() as connection:
            if not HasSqliteSchema(connection, _SCHEMA, _validate_contract):
                return TrainingMaintenanceState(published_snapshot, (), (), ())
            due = tuple(
                cast(TrainingDueState, _decode("due", row))
                for row in connection.run("SELECT * FROM training_due ORDER BY due_id").fetchall()
            )
            claims = tuple(
                cast(TrainingReminderClaim, _decode("reminder_claim", row))
                for row in connection.run(
                    "SELECT * FROM training_reminder_claim ORDER BY due_id,reminder_date"
                ).fetchall()
            )
            reminders = tuple(
                cast(TrainingReminderState, _decode("reminder", row))
                for row in connection.run("SELECT * FROM training_reminder ORDER BY due_id,reminder_date").fetchall()
            )
            return TrainingMaintenanceState(published_snapshot, due, reminders, claims)

    def _save(self, kind: MaintenanceKind, value: MaintenanceRecord) -> None:
        columns = _columns(value)
        predicate, key = ("due_id=?", columns[:1]) if kind == "due" else ("due_id=? AND reminder_date=?", columns[:2])
        with self._connection(write=True) as connection:
            previous = connection.run(f"SELECT * FROM {_TABLES[kind]} WHERE {predicate}", key).fetchone()
            if previous is not None:
                if _decode(kind, previous) != value:
                    raise TrainingMaintenanceConflictError("training maintenance immutable identity conflicts")
                return
            connection.run(f"INSERT INTO {_TABLES[kind]} VALUES({','.join('?' for _ in columns)})", columns)


def _initialize_contract(connection: SqliteConnection) -> None:
    connection.run("INSERT INTO schema_contract VALUES(1,'training_maintenance')")


def _validate_contract(connection: SqliteConnection) -> None:
    contract = connection.run("SELECT singleton,schema_id FROM schema_contract").fetchall()
    if [tuple(row) for row in contract] != [(1, "training_maintenance")]:
        raise TrainingMaintenanceError("training maintenance database identity is invalid")


def _columns(value: MaintenanceRecord) -> tuple[str | int | bytes | None, ...]:
    if isinstance(value, TrainingDueState):
        return (
            value.due_identity,
            value.reason,
            EncodeDate(value.baseline_label_cutoff) if value.baseline_label_cutoff else None,
            EncodeDate(value.current_label_cutoff) if value.current_label_cutoff else None,
            value.matured_label_days_since_training,
            int(value.input_revision),
            EncodeTime(value.observed_at),
            EncodeHash(value.content_hash),
        )
    if isinstance(value, TrainingReminderClaim):
        return (
            value.due_identity,
            EncodeDate(value.reminder_date),
            EncodeTime(value.claimed_at),
            EncodeHash(value.content_hash),
        )
    return (
        value.due_identity,
        EncodeDate(value.reminder_date),
        value.outcome,
        EncodeTime(value.attempted_at),
        value.error_code,
        EncodeHash(value.content_hash),
    )


def _optional_date(value: object) -> date | None:
    return DecodeDate(value) if value is not None else None


def _decode(kind: MaintenanceKind, row: sqlite3.Row) -> MaintenanceRecord:
    value: MaintenanceRecord
    if kind == "due":
        if type(row[5]) is not int or row[5] not in (0, 1):
            raise TrainingMaintenanceError("training due revision flag is invalid")
        value = TrainingDueState(
            row[0],
            cast(TrainingDueReason, row[1]),
            _optional_date(row[2]),
            _optional_date(row[3]),
            row[4],
            bool(row[5]),
            DecodeTime(row[6]),
        )
    elif kind == "reminder_claim":
        value = TrainingReminderClaim(row[0], DecodeDate(row[1]), DecodeTime(row[2]))
    else:
        value = TrainingReminderState(
            row[0], DecodeDate(row[1]), cast(TrainingReminderOutcome, row[2]), DecodeTime(row[3]), row[4]
        )
    if value.content_hash != DecodeHash(row[-1]):
        raise TrainingMaintenanceError("training maintenance record hash is invalid")
    return value


__all__ = ["TrainingMaintenanceIndex", "TrainingMaintenanceError", "TrainingMaintenanceConflictError"]
