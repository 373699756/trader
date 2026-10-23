"""Training-owned due and reminder records in the existing history catalog."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Literal, cast
from zoneinfo import ZoneInfo

from trader.download.domain.history_control import HistoryActiveSnapshot
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession, SqliteTransaction
from trader.training.domain.training_due import TrainingDueReason, TrainingDueState
from trader.training.domain.training_reminder import (
    TrainingMaintenanceState,
    TrainingReminderClaim,
    TrainingReminderOutcome,
    TrainingReminderState,
)

MaintenanceKind = Literal["due", "reminder_claim", "reminder"]
MaintenanceRecord = TrainingDueState | TrainingReminderClaim | TrainingReminderState
_SHANGHAI = ZoneInfo("Asia/Shanghai")


class TrainingMaintenanceError(RuntimeError):
    """Training maintenance observations are unavailable or corrupt."""


class TrainingMaintenanceConflictError(TrainingMaintenanceError):
    """An immutable training maintenance identity conflicts."""


class TrainingMaintenanceIndex:
    """Own only training records; history publication and schema stay with download."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def save_due_state(self, value: TrainingDueState) -> None:
        self._save("due", value.due_identity, value.content_hash, _due_payload(value))

    def read_due_state(self, identity: str, *, latest: bool = False) -> TrainingDueState | None:
        try:
            with SqliteSession(self._path, SqliteConnectionOptions(read_only=True)) as connection:
                row = connection.run(
                    "SELECT content_hash,payload_json FROM immutable_records WHERE kind='due' "
                    "AND (record_key=? OR record_key LIKE ?) "
                    "ORDER BY json_extract(payload_json,'$.observed_at') DESC LIMIT 1",
                    (identity, identity + ":%" if latest else identity),
                ).fetchone()
                if row is None:
                    return None
                value = _decode_record("due", row[1])
                if not isinstance(value, TrainingDueState) or value.content_hash != row[0]:
                    raise TrainingMaintenanceError("history due observation is invalid")
                return value
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise TrainingMaintenanceError("history due observation read failed") from exc

    def claim_reminder(self, value: TrainingReminderClaim) -> bool:
        """Atomically claim one due identity on one Shanghai date.

        The claim is durable before the external desktop-notification side effect.
        A process restart or a concurrent scheduled trigger therefore observes the
        existing identity and does not emit the same reminder again.
        """

        key = f"{value.due_identity}:{value.reminder_date.isoformat()}"
        payload_json = json.dumps(
            _reminder_claim_payload(value),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            with (
                SqliteSession(self._path, SqliteConnectionOptions(foreign_keys=True)) as connection,
                SqliteTransaction(connection, immediate=True),
            ):
                existing = connection.run(
                    "SELECT 1 FROM immutable_records WHERE kind=? AND record_key=?",
                    ("reminder_claim", key),
                ).fetchone()
                if existing is not None:
                    return False
                connection.run(
                    "INSERT INTO immutable_records(kind, record_key, content_hash, payload_json) VALUES (?, ?, ?, ?)",
                    ("reminder_claim", key, value.content_hash, payload_json),
                )
        except sqlite3.Error as exc:
            raise TrainingMaintenanceError("history reminder claim write failed") from exc
        return True

    def save_reminder(self, value: TrainingReminderState) -> None:
        key = f"{value.due_identity}:{value.reminder_date.isoformat()}"
        self._save("reminder", key, value.content_hash, _reminder_payload(value))

    def load_state(self, *, active_snapshot: HistoryActiveSnapshot | None = None) -> TrainingMaintenanceState:
        try:
            with (
                SqliteSession(self._path, SqliteConnectionOptions(read_only=True)) as connection,
                SqliteTransaction(connection),
            ):
                identity = connection.run("SELECT schema_identity FROM metadata WHERE singleton=1").fetchone()
                if identity is None or tuple(identity) != ("history_catalog",):
                    raise TrainingMaintenanceError("training maintenance catalog identity is invalid")
                rows = connection.run(
                    "SELECT kind, content_hash, payload_json FROM immutable_records "
                    "WHERE kind IN ('due', 'reminder_claim', 'reminder') ORDER BY kind, record_key"
                ).fetchall()
            decoded: dict[str, list[MaintenanceRecord]] = {"due": [], "reminder_claim": [], "reminder": []}
            for kind, content_hash, payload in rows:
                value = _decode_record(cast(MaintenanceKind, kind), payload)
                if value.content_hash != content_hash:
                    raise TrainingMaintenanceError("training maintenance record hash is invalid")
                decoded[kind].append(value)
            return TrainingMaintenanceState(
                active_snapshot,
                cast(tuple[TrainingDueState, ...], tuple(decoded["due"])),
                cast(tuple[TrainingReminderState, ...], tuple(decoded["reminder"])),
                cast(tuple[TrainingReminderClaim, ...], tuple(decoded["reminder_claim"])),
            )
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise TrainingMaintenanceError("training maintenance state read failed") from exc

    def restore_state(self, state: TrainingMaintenanceState) -> None:
        """Restore verified records into an isolated catalog before its atomic replacement."""
        for value in state.due_states:
            self.save_due_state(value)
        for claim in state.reminder_claims:
            self.claim_reminder(claim)
        for reminder in state.reminders:
            self.save_reminder(reminder)
        if self.load_state(active_snapshot=state.active_snapshot) != state:
            raise TrainingMaintenanceError("restored training maintenance state differs")

    def _save(self, kind: MaintenanceKind, key: str, content_hash: str, payload: dict[str, object]) -> None:
        payload_json = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        try:
            with (
                SqliteSession(self._path, SqliteConnectionOptions(foreign_keys=True)) as connection,
                SqliteTransaction(connection, immediate=True),
            ):
                existing = connection.run(
                    "SELECT content_hash, payload_json FROM immutable_records WHERE kind=? AND record_key=?",
                    (kind, key),
                ).fetchone()
                if existing is not None and tuple(existing) != (content_hash, payload_json):
                    raise TrainingMaintenanceConflictError("history immutable control identity conflicts")
                connection.run(
                    "INSERT OR IGNORE INTO immutable_records(kind, record_key, content_hash, payload_json) "
                    "VALUES (?, ?, ?, ?)",
                    (kind, key, content_hash, payload_json),
                )
        except sqlite3.Error as exc:
            raise TrainingMaintenanceError("history immutable control record write failed") from exc


def _due_payload(value: TrainingDueState) -> dict[str, object]:
    return {
        "due_identity": value.due_identity,
        "reason": value.reason,
        "baseline_label_cutoff": (
            value.baseline_label_cutoff.isoformat() if value.baseline_label_cutoff is not None else None
        ),
        "current_label_cutoff": (
            value.current_label_cutoff.isoformat() if value.current_label_cutoff is not None else None
        ),
        "matured_label_days_since_training": value.matured_label_days_since_training,
        "input_revision": value.input_revision,
        "observed_at": value.observed_at.isoformat(),
    }


def _reminder_payload(value: TrainingReminderState) -> dict[str, object]:
    return {
        "due_identity": value.due_identity,
        "reminder_date": value.reminder_date.isoformat(),
        "outcome": value.outcome,
        "attempted_at": value.attempted_at.isoformat(),
        "error_code": value.error_code,
    }


def _reminder_claim_payload(value: TrainingReminderClaim) -> dict[str, object]:
    return {
        "due_identity": value.due_identity,
        "reminder_date": value.reminder_date.isoformat(),
        "claimed_at": value.claimed_at.isoformat(),
    }


def _decode_record(kind: MaintenanceKind, payload_json: str) -> MaintenanceRecord:
    try:
        payload = json.loads(payload_json)
        if not isinstance(payload, dict):
            raise TypeError("control payload must be an object")
        return _decode_payload(kind, cast(dict[str, object], payload))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TrainingMaintenanceError("history control payload is invalid") from exc


def _decode_payload(kind: MaintenanceKind, payload: dict[str, object]) -> MaintenanceRecord:
    value: MaintenanceRecord
    if kind == "due":
        _require_keys(
            payload,
            {
                "due_identity",
                "reason",
                "baseline_label_cutoff",
                "current_label_cutoff",
                "matured_label_days_since_training",
                "input_revision",
                "observed_at",
            },
        )
        value = TrainingDueState(
            _text(payload, "due_identity"),
            cast(TrainingDueReason, _text(payload, "reason")),
            _optional_date(payload, "baseline_label_cutoff"),
            _optional_date(payload, "current_label_cutoff"),
            _integer(payload, "matured_label_days_since_training"),
            _boolean(payload, "input_revision"),
            _shanghai_datetime(payload, "observed_at"),
        )
    elif kind == "reminder_claim":
        _require_keys(payload, {"due_identity", "reminder_date", "claimed_at"})
        value = TrainingReminderClaim(
            _text(payload, "due_identity"),
            date.fromisoformat(_text(payload, "reminder_date")),
            _shanghai_datetime(payload, "claimed_at"),
        )
    elif kind == "reminder":
        _require_keys(payload, {"due_identity", "reminder_date", "outcome", "attempted_at", "error_code"})
        value = TrainingReminderState(
            _text(payload, "due_identity"),
            date.fromisoformat(_text(payload, "reminder_date")),
            cast(TrainingReminderOutcome, _text(payload, "outcome")),
            _shanghai_datetime(payload, "attempted_at"),
            _optional_text(payload, "error_code"),
        )
    else:
        raise TrainingMaintenanceError("training maintenance record kind is invalid")
    return value


def _require_keys(payload: dict[str, object], expected: set[str]) -> None:
    if set(payload) != expected:
        raise ValueError("history control payload fields are invalid")


def _text(payload: dict[str, object], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str) or not value:
        raise TypeError(f"history control {key} must be text")
    return value


def _optional_text(payload: dict[str, object], key: str) -> str | None:
    value = payload[key]
    if value is not None and not isinstance(value, str):
        raise TypeError(f"history control {key} must be optional text")
    return value


def _integer(payload: dict[str, object], key: str) -> int:
    value = payload[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"history control {key} must be integer")
    return value


def _boolean(payload: dict[str, object], key: str) -> bool:
    value = payload[key]
    if not isinstance(value, bool):
        raise TypeError(f"history control {key} must be boolean")
    return value


def _optional_date(payload: dict[str, object], key: str) -> date | None:
    value = _optional_text(payload, key)
    return date.fromisoformat(value) if value is not None else None


def _shanghai_datetime(payload: dict[str, object], key: str) -> datetime:
    value = datetime.fromisoformat(_text(payload, key))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"history control {key} must include timezone")
    return value.astimezone(_SHANGHAI)


__all__ = ["TrainingMaintenanceIndex", "TrainingMaintenanceError", "TrainingMaintenanceConflictError"]
