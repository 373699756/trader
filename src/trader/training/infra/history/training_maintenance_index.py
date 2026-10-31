"""Training-owned relational due, claim and reminder records in the history catalog."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Literal, cast

from trader.download.domain.history_control import HistorySnapshot
from trader.infra.sqlite_codec import DecodeDate, DecodeHash, DecodeTime, EncodeDate, EncodeHash, EncodeTime
from trader.infra.sqlite_connection import SqliteConnection, SqliteConnectionOptions, SqliteSession, SqliteTransaction
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
_SCHEMA = (
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
)


class TrainingMaintenanceError(RuntimeError):
    """Training maintenance observations are unavailable or corrupt."""


class TrainingMaintenanceConflictError(TrainingMaintenanceError):
    """An immutable training maintenance identity conflicts."""


class TrainingMaintenanceIndex:
    """Own training tables and records; history publication stays with download."""

    def __init__(self, path: Path, *, sqlite_policy: SqliteIoPolicy) -> None:
        self._sqlite_policy = sqlite_policy
        self._path = path

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[SqliteConnection]:
        try:
            with (
                SqliteSession(
                    self._path, SqliteConnectionOptions(read_only=not write, foreign_keys=True), sqlite_policy=self._sqlite_policy
                ) as connection,
                SqliteTransaction(connection, immediate=write),
            ):
                contract = connection.run(
                    "SELECT schema_id,schema_version FROM schema_contract WHERE singleton=1"
                ).fetchone()
                if contract is None or tuple(contract) != ("history_catalog", 2):
                    raise TrainingMaintenanceError("training maintenance catalog identity is invalid")
                existing = {
                    row[0]
                    for row in connection.run(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                        "('training_due','training_reminder_claim','training_reminder')"
                    )
                }
                if existing and existing != set(_TABLES.values()):
                    raise TrainingMaintenanceError("training maintenance schema is incomplete")
                if write and not existing:
                    for statement in _SCHEMA:
                        connection.run(statement)
                yield connection
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise TrainingMaintenanceError("training maintenance database unavailable") from exc

    def save_due_state(self, value: TrainingDueState) -> None:
        self._save("due", value)

    def read_due_state(self, identity: str, *, latest: bool = False) -> TrainingDueState | None:
        with self._connection() as connection:
            if not _initialized(connection):
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
        with self._connection() as connection:
            if not _initialized(connection):
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

    def copy_state(self, state: TrainingMaintenanceState) -> None:
        """Copy verified records to an isolated catalog before atomic replacement."""
        for value in state.due_states:
            self.save_due_state(value)
        for claim in state.reminder_claims:
            self.claim_reminder(claim)
        for reminder in state.reminders:
            self.save_reminder(reminder)
        if self.load_state(published_snapshot=state.published_snapshot) != state:
            raise TrainingMaintenanceError("copied training maintenance state differs")

    def _save(self, kind: MaintenanceKind, value: MaintenanceRecord) -> None:
        columns = _columns(value)
        predicate, key = ("due_id=?", columns[:1]) if kind == "due" else ("due_id=? AND reminder_date=?", columns[:2])
        with self._connection(write=True) as connection:
            previous = connection.run(f"SELECT * FROM {_TABLES[kind]} WHERE {predicate}", key).fetchone()
            if previous is not None:
                if _decode(kind, previous) != value:
                    raise TrainingMaintenanceConflictError("history immutable control identity conflicts")
                return
            connection.run(f"INSERT INTO {_TABLES[kind]} VALUES({','.join('?' for _ in columns)})", columns)


def _initialized(connection: SqliteConnection) -> bool:
    return connection.run("SELECT 1 FROM sqlite_master WHERE name='training_due'").fetchone() is not None


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
