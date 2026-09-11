"""Crash-safe SQLite control plane for zero-argument history maintenance."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal, Protocol, cast
from zoneinfo import ZoneInfo

from trader.domain.research.history_control import (
    HistoryActiveSnapshot,
    HistoryAutomationControlState,
    HistoryCalendarIdentity,
    HistoryControlState,
    HistoryDiskRequirement,
    HistoryReminderClaim,
    HistoryReminderOutcome,
    HistoryReminderState,
    HistorySecurityBoard,
    HistorySecurityIdentity,
    HistorySnapshotPartition,
    HistorySourceIdentity,
    HistorySyncCheckpoint,
    HistorySyncState,
    HistoryTrainingDueReason,
    HistoryTrainingDueState,
    HistoryUniverseIdentity,
)
from trader.infra.process_lock import ProcessLock, ProcessLockError

FaultInjector = Callable[[str], None]
ControlKind = Literal[
    "source",
    "calendar",
    "universe",
    "checkpoint",
    "due",
    "reminder_claim",
    "reminder",
    "snapshot",
]
ControlRecord = (
    HistorySourceIdentity
    | HistoryCalendarIdentity
    | HistoryUniverseIdentity
    | HistorySyncCheckpoint
    | HistoryTrainingDueState
    | HistoryReminderClaim
    | HistoryReminderState
    | HistoryActiveSnapshot
)


class DiskUsageResult(Protocol):
    @property
    def free(self) -> int: ...


DiskUsage = Callable[[Path], DiskUsageResult]

_SCHEMA_IDENTITY = "history_control"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    schema_identity TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS immutable_records (
    kind TEXT NOT NULL,
    record_key TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(kind, record_key)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS active_snapshot (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    snapshot_hash TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK(sequence > 0)
);
"""


class HistoryControlError(RuntimeError):
    """The history control database cannot be trusted or updated."""


class HistoryControlConflictError(HistoryControlError):
    """An immutable control identity already has different content."""


class HistoryControlRegressionError(HistoryControlError):
    """An active snapshot update would move the published pointer backward."""


class HistoryMaintenanceAlreadyRunningError(HistoryControlError):
    """Another history maintenance process owns the non-blocking lock."""


@dataclass(frozen=True)
class HistoryControlIntegrityStatus:
    state: Literal["missing", "healthy", "corrupt"]
    reason: str | None


@dataclass(frozen=True)
class HistoryDiskPreflightStatus:
    required_free_bytes: int
    available_free_bytes: int
    sufficient: bool

    def __post_init__(self) -> None:
        if min(self.required_free_bytes, self.available_free_bytes) < 0:
            raise ValueError("history disk preflight values are invalid")
        if self.sufficient != (self.available_free_bytes >= self.required_free_bytes):
            raise ValueError("history disk preflight result is inconsistent")


class HistoryMaintenanceLock:
    def __init__(self, path: Path) -> None:
        self._lock = ProcessLock(path)

    def acquire(self) -> None:
        try:
            self._lock.acquire()
        except ProcessLockError as exc:
            raise HistoryMaintenanceAlreadyRunningError("history maintenance is already running") from exc

    def release(self) -> None:
        self._lock.release()

    def __enter__(self) -> HistoryMaintenanceLock:
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


class SQLiteHistoryControlRepository:
    def __init__(self, path: Path, *, fault_injector: FaultInjector | None = None) -> None:
        self._path = path
        self._fault_injector = fault_injector or (lambda _stage: None)

    def initialize(self) -> None:
        if self._path.exists() and self.integrity().state == "corrupt":
            raise HistoryControlError("history control database is corrupt")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                connection.executescript(_SCHEMA)
                connection.execute(
                    "INSERT OR IGNORE INTO metadata(singleton, schema_identity) VALUES (1, ?)",
                    (_SCHEMA_IDENTITY,),
                )
                identity = connection.execute("SELECT schema_identity FROM metadata WHERE singleton=1").fetchone()
                if identity != (_SCHEMA_IDENTITY,):
                    raise HistoryControlConflictError("history control schema identity conflicts")
        except sqlite3.Error as exc:
            raise HistoryControlError("history control initialization failed") from exc
        self._fault_injector("schema_initialized")
        _fsync_file(self._path)
        _fsync_directory(self._path.parent)

    def integrity(self) -> HistoryControlIntegrityStatus:
        if not self._path.is_file():
            return HistoryControlIntegrityStatus("missing", "control_database_missing")
        try:
            with closing(self._read_connection()) as connection:
                check = connection.execute("PRAGMA quick_check").fetchone()
                identity = connection.execute("SELECT schema_identity FROM metadata WHERE singleton=1").fetchone()
        except sqlite3.Error:
            return HistoryControlIntegrityStatus("corrupt", "control_database_invalid")
        if check != ("ok",) or identity != (_SCHEMA_IDENTITY,):
            return HistoryControlIntegrityStatus("corrupt", "control_database_invalid")
        return HistoryControlIntegrityStatus("healthy", None)

    def save_source(self, value: HistorySourceIdentity) -> None:
        self._save("source", value.content_hash, value.content_hash, _source_payload(value))

    def save_calendar(self, value: HistoryCalendarIdentity) -> None:
        self._save("calendar", value.content_hash, value.content_hash, _calendar_payload(value))

    def save_universe(self, value: HistoryUniverseIdentity) -> None:
        self._save("universe", value.content_hash, value.content_hash, _universe_payload(value))

    def save_checkpoint(self, value: HistorySyncCheckpoint) -> None:
        key = f"{value.sync_identity}:{value.ordinal}"
        self._save("checkpoint", key, value.content_hash, _checkpoint_payload(value))

    def save_due_state(self, value: HistoryTrainingDueState) -> None:
        self._save("due", value.due_identity, value.content_hash, _due_payload(value))

    def claim_reminder(self, value: HistoryReminderClaim) -> bool:
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
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT 1 FROM immutable_records WHERE kind=? AND record_key=?",
                    ("reminder_claim", key),
                ).fetchone()
                if existing is not None:
                    return False
                connection.execute(
                    "INSERT INTO immutable_records(kind, record_key, content_hash, payload_json) VALUES (?, ?, ?, ?)",
                    ("reminder_claim", key, value.content_hash, payload_json),
                )
        except sqlite3.Error as exc:
            raise HistoryControlError("history reminder claim write failed") from exc
        return True

    def save_reminder(self, value: HistoryReminderState) -> None:
        key = f"{value.due_identity}:{value.reminder_date.isoformat()}"
        self._save("reminder", key, value.content_hash, _reminder_payload(value))

    def publish_snapshot(self, value: HistoryActiveSnapshot) -> None:
        self._require_snapshot_parents(value)
        self._save("snapshot", str(value.sequence), value.content_hash, _snapshot_payload(value))
        self._fault_injector("snapshot_committed")
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    "SELECT snapshot_hash, sequence FROM active_snapshot WHERE singleton=1"
                ).fetchone()
                if current is not None:
                    current_hash, current_sequence = cast(tuple[str, int], current)
                    if current_hash == value.content_hash:
                        return
                    if current_sequence >= value.sequence:
                        raise HistoryControlRegressionError("history active snapshot would regress or conflict")
                connection.execute(
                    "INSERT INTO active_snapshot(singleton, snapshot_hash, sequence) VALUES (1, ?, ?) "
                    "ON CONFLICT(singleton) DO UPDATE SET snapshot_hash=excluded.snapshot_hash, "
                    "sequence=excluded.sequence",
                    (value.content_hash, value.sequence),
                )
        except sqlite3.Error as exc:
            raise HistoryControlError("history active snapshot publication failed") from exc
        self._fault_injector("active_snapshot_committed")

    def _require_snapshot_parents(self, value: HistoryActiveSnapshot) -> None:
        required = (
            ("source", value.source_identity_hash),
            ("calendar", value.calendar_hash),
            ("universe", value.universe_hash),
        )
        try:
            with closing(self._read_connection()) as connection:
                present = {
                    (kind, record_key)
                    for kind, record_key in connection.execute(
                        "SELECT kind, record_key FROM immutable_records "
                        "WHERE (kind=? AND record_key=?) OR (kind=? AND record_key=?) OR (kind=? AND record_key=?)",
                        tuple(item for pair in required for item in pair),
                    ).fetchall()
                }
        except sqlite3.Error as exc:
            raise HistoryControlError("history snapshot parent identity read failed") from exc
        if present != set(required):
            raise HistoryControlConflictError("history snapshot parent identities are not sealed")

    def load_state(self) -> HistoryControlState:
        if self.integrity().state != "healthy":
            raise HistoryControlError("history control database is unavailable")
        try:
            with closing(self._read_connection()) as connection:
                rows = connection.execute(
                    "SELECT kind, content_hash, payload_json FROM immutable_records ORDER BY kind, record_key"
                ).fetchall()
                active = connection.execute(
                    "SELECT snapshot_hash, sequence FROM active_snapshot WHERE singleton=1"
                ).fetchone()
        except sqlite3.Error as exc:
            raise HistoryControlError("history control state read failed") from exc
        decoded: dict[str, list[object]] = {
            "source": [],
            "calendar": [],
            "universe": [],
            "checkpoint": [],
            "due": [],
            "reminder_claim": [],
            "reminder": [],
            "snapshot": [],
        }
        for kind, content_hash, payload_json in rows:
            if kind not in decoded:
                raise HistoryControlError("history control record kind is invalid")
            value = _decode_record(cast(ControlKind, kind), payload_json)
            if value.content_hash != content_hash:
                raise HistoryControlError("history control record hash is invalid")
            decoded[kind].append(value)
        try:
            state = HistoryControlState(
                cast(tuple[HistorySourceIdentity, ...], tuple(decoded["source"])),
                cast(tuple[HistoryCalendarIdentity, ...], tuple(decoded["calendar"])),
                cast(tuple[HistoryUniverseIdentity, ...], tuple(decoded["universe"])),
                cast(tuple[HistorySyncCheckpoint, ...], tuple(decoded["checkpoint"])),
                cast(tuple[HistoryTrainingDueState, ...], tuple(decoded["due"])),
                cast(tuple[HistoryReminderState, ...], tuple(decoded["reminder"])),
                cast(tuple[HistoryReminderClaim, ...], tuple(decoded["reminder_claim"])),
                cast(tuple[HistoryActiveSnapshot, ...], tuple(decoded["snapshot"])),
                cast(str | None, active[0] if active is not None else None),
            )
        except (TypeError, ValueError) as exc:
            raise HistoryControlError("history control state is inconsistent") from exc
        if active is not None and (state.active_snapshot is None or state.active_snapshot.sequence != active[1]):
            raise HistoryControlError("history active snapshot pointer is inconsistent")
        return state

    def load_automation_state(self) -> HistoryAutomationControlState:
        """Read only the bounded records needed by scheduled-run observability."""

        if self.integrity().state != "healthy":
            raise HistoryControlError("history control database is unavailable")
        try:
            with closing(self._read_connection()) as connection:
                active = connection.execute(
                    "SELECT snapshot_hash, sequence FROM active_snapshot WHERE singleton=1"
                ).fetchone()
                active_hash = cast(str | None, active[0] if active is not None else None)
                rows = connection.execute(
                    "SELECT kind, content_hash, payload_json FROM immutable_records "
                    "WHERE kind IN ('due', 'reminder_claim', 'reminder') "
                    "OR (kind='snapshot' AND content_hash=?) ORDER BY kind, record_key",
                    (active_hash,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise HistoryControlError("history automation control read failed") from exc
        decoded: dict[str, list[ControlRecord]] = {
            "due": [],
            "reminder_claim": [],
            "reminder": [],
            "snapshot": [],
        }
        for kind, content_hash, payload_json in rows:
            value = _decode_record(cast(ControlKind, kind), payload_json)
            if value.content_hash != content_hash:
                raise HistoryControlError("history automation control record hash is invalid")
            decoded[kind].append(value)
        snapshots = cast(tuple[HistoryActiveSnapshot, ...], tuple(decoded["snapshot"]))
        if active is not None and (len(snapshots) != 1 or snapshots[0].sequence != active[1]):
            raise HistoryControlError("history active snapshot pointer is inconsistent")
        try:
            return HistoryAutomationControlState(
                snapshots[0] if snapshots else None,
                cast(tuple[HistoryTrainingDueState, ...], tuple(decoded["due"])),
                cast(tuple[HistoryReminderState, ...], tuple(decoded["reminder"])),
                cast(tuple[HistoryReminderClaim, ...], tuple(decoded["reminder_claim"])),
            )
        except (TypeError, ValueError) as exc:
            raise HistoryControlError("history automation control is inconsistent") from exc

    @classmethod
    def rebuild(
        cls,
        path: Path,
        state: HistoryControlState,
        *,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        current = cls(path)
        if current.integrity().state == "healthy":
            raise HistoryControlError("healthy history control database must not be rebuilt")
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.rebuild.", dir=path.parent)
        os.close(descriptor)
        temporary = Path(temporary_name)
        temporary.unlink()
        injector = fault_injector or (lambda _stage: None)
        try:
            rebuilt = cls(temporary)
            rebuilt.initialize()
            _populate_rebuilt_control(rebuilt, state)
            if rebuilt.load_state() != state:
                raise HistoryControlError("rebuilt history control state does not match its source")
            _checkpoint_wal(temporary)
            _fsync_file(temporary)
            injector("rebuild_ready")
            _remove_sqlite_sidecars(path)
            os.replace(temporary, path)
            _fsync_directory(path.parent)
            injector("rebuild_committed")
        finally:
            _remove_sqlite_files(temporary)

    def _save(self, kind: ControlKind, key: str, content_hash: str, payload: dict[str, object]) -> None:
        payload_json = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT content_hash, payload_json FROM immutable_records WHERE kind=? AND record_key=?",
                    (kind, key),
                ).fetchone()
                if existing is not None and existing != (content_hash, payload_json):
                    raise HistoryControlConflictError("history immutable control identity conflicts")
                connection.execute(
                    "INSERT OR IGNORE INTO immutable_records(kind, record_key, content_hash, payload_json) "
                    "VALUES (?, ?, ?, ?)",
                    (kind, key, content_hash, payload_json),
                )
        except sqlite3.Error as exc:
            raise HistoryControlError("history immutable control record write failed") from exc

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5.0)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _read_connection(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self._path.as_posix()}?mode=ro", uri=True, timeout=5.0)


def _populate_rebuilt_control(
    repository: SQLiteHistoryControlRepository,
    state: HistoryControlState,
) -> None:
    for source in state.sources:
        repository.save_source(source)
    for calendar in state.calendars:
        repository.save_calendar(calendar)
    for universe in state.universes:
        repository.save_universe(universe)
    for checkpoint in state.checkpoints:
        repository.save_checkpoint(checkpoint)
    for due_state in state.due_states:
        repository.save_due_state(due_state)
    _populate_rebuilt_reminders(repository, state)
    for snapshot in state.snapshots:
        repository._save(
            "snapshot",
            str(snapshot.sequence),
            snapshot.content_hash,
            _snapshot_payload(snapshot),
        )
    if state.active_snapshot is not None:
        repository.publish_snapshot(state.active_snapshot)


def _populate_rebuilt_reminders(
    repository: SQLiteHistoryControlRepository,
    state: HistoryControlState,
) -> None:
    for reminder_claim in state.reminder_claims:
        if not repository.claim_reminder(reminder_claim):
            raise HistoryControlConflictError("history reminder claim rebuild conflicts")
    for reminder in state.reminders:
        repository.save_reminder(reminder)


def inspect_history_disk(
    path: Path,
    requirement: HistoryDiskRequirement,
    *,
    disk_usage: DiskUsage | None = None,
) -> HistoryDiskPreflightStatus:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    usage = (disk_usage or _system_disk_usage)(probe)
    available = usage.free
    required = requirement.required_free_bytes
    return HistoryDiskPreflightStatus(required, available, available >= required)


def _system_disk_usage(path: Path) -> DiskUsageResult:
    return shutil.disk_usage(path)


def _source_payload(value: HistorySourceIdentity) -> dict[str, object]:
    return {
        "source": value.source,
        "dataset": value.dataset,
        "supplier_contract": value.supplier_contract,
        "observed_at": value.observed_at.isoformat(),
    }


def _calendar_payload(value: HistoryCalendarIdentity) -> dict[str, object]:
    return {
        "open_dates": [item.isoformat() for item in value.open_dates],
        "source_identity_hash": value.source_identity_hash,
    }


def _security_payload(value: HistorySecurityIdentity) -> dict[str, object]:
    return {
        "code": value.code,
        "name": value.name,
        "board": value.board,
        "listed_on": value.listed_on.isoformat(),
        "delisted_on": value.delisted_on.isoformat() if value.delisted_on is not None else None,
    }


def _universe_payload(value: HistoryUniverseIdentity) -> dict[str, object]:
    return {
        "securities": [_security_payload(item) for item in value.securities],
        "source_identity_hash": value.source_identity_hash,
    }


def _checkpoint_payload(value: HistorySyncCheckpoint) -> dict[str, object]:
    return {
        "sync_identity": value.sync_identity,
        "ordinal": value.ordinal,
        "state": value.state,
        "observed_at": value.observed_at.isoformat(),
        "completed_units": value.completed_units,
        "total_units": value.total_units,
        "error_code": value.error_code,
    }


def _due_payload(value: HistoryTrainingDueState) -> dict[str, object]:
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


def _reminder_payload(value: HistoryReminderState) -> dict[str, object]:
    return {
        "due_identity": value.due_identity,
        "reminder_date": value.reminder_date.isoformat(),
        "outcome": value.outcome,
        "attempted_at": value.attempted_at.isoformat(),
        "error_code": value.error_code,
    }


def _reminder_claim_payload(value: HistoryReminderClaim) -> dict[str, object]:
    return {
        "due_identity": value.due_identity,
        "reminder_date": value.reminder_date.isoformat(),
        "claimed_at": value.claimed_at.isoformat(),
    }


def _snapshot_payload(value: HistoryActiveSnapshot) -> dict[str, object]:
    return {
        "sequence": value.sequence,
        "data_cutoff": value.data_cutoff.isoformat(),
        "label_cutoff": value.label_cutoff.isoformat(),
        "calendar_hash": value.calendar_hash,
        "universe_hash": value.universe_hash,
        "source_identity_hash": value.source_identity_hash,
        "partitions": [
            {"relative_path": item.relative_path, "sha256": item.sha256, "row_count": item.row_count}
            for item in value.partitions
        ],
    }


def _decode_record(kind: ControlKind, payload_json: str) -> ControlRecord:
    try:
        payload = json.loads(payload_json)
        if not isinstance(payload, dict):
            raise TypeError("control payload must be an object")
        return _decode_payload(kind, cast(dict[str, object], payload))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HistoryControlError("history control payload is invalid") from exc


def _decode_payload(kind: ControlKind, payload: dict[str, object]) -> ControlRecord:
    value: ControlRecord
    if kind == "source":
        _require_keys(payload, {"source", "dataset", "supplier_contract", "observed_at"})
        value = HistorySourceIdentity(
            _text(payload, "source"),
            _text(payload, "dataset"),
            _text(payload, "supplier_contract"),
            _shanghai_datetime(payload, "observed_at"),
        )
    elif kind == "calendar":
        _require_keys(payload, {"open_dates", "source_identity_hash"})
        value = HistoryCalendarIdentity(
            tuple(date.fromisoformat(value) for value in _strings(payload, "open_dates")),
            _text(payload, "source_identity_hash"),
        )
    elif kind == "universe":
        _require_keys(payload, {"securities", "source_identity_hash"})
        raw_securities = payload["securities"]
        if not isinstance(raw_securities, list):
            raise TypeError("history securities must be a list")
        securities = tuple(_decode_security(item) for item in raw_securities)
        value = HistoryUniverseIdentity(securities, _text(payload, "source_identity_hash"))
    elif kind == "checkpoint":
        _require_keys(
            payload,
            {"sync_identity", "ordinal", "state", "observed_at", "completed_units", "total_units", "error_code"},
        )
        value = HistorySyncCheckpoint(
            _text(payload, "sync_identity"),
            _integer(payload, "ordinal"),
            cast(HistorySyncState, _text(payload, "state")),
            _shanghai_datetime(payload, "observed_at"),
            _integer(payload, "completed_units"),
            _integer(payload, "total_units"),
            _optional_text(payload, "error_code"),
        )
    elif kind == "due":
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
        value = HistoryTrainingDueState(
            _text(payload, "due_identity"),
            cast(HistoryTrainingDueReason, _text(payload, "reason")),
            _optional_date(payload, "baseline_label_cutoff"),
            _optional_date(payload, "current_label_cutoff"),
            _integer(payload, "matured_label_days_since_training"),
            _boolean(payload, "input_revision"),
            _shanghai_datetime(payload, "observed_at"),
        )
    elif kind == "reminder_claim":
        _require_keys(payload, {"due_identity", "reminder_date", "claimed_at"})
        value = HistoryReminderClaim(
            _text(payload, "due_identity"),
            date.fromisoformat(_text(payload, "reminder_date")),
            _shanghai_datetime(payload, "claimed_at"),
        )
    elif kind == "reminder":
        _require_keys(payload, {"due_identity", "reminder_date", "outcome", "attempted_at", "error_code"})
        value = HistoryReminderState(
            _text(payload, "due_identity"),
            date.fromisoformat(_text(payload, "reminder_date")),
            cast(HistoryReminderOutcome, _text(payload, "outcome")),
            _shanghai_datetime(payload, "attempted_at"),
            _optional_text(payload, "error_code"),
        )
    else:
        _require_keys(
            payload,
            {
                "sequence",
                "data_cutoff",
                "label_cutoff",
                "calendar_hash",
                "universe_hash",
                "source_identity_hash",
                "partitions",
            },
        )
        raw_partitions = payload["partitions"]
        if not isinstance(raw_partitions, list):
            raise TypeError("history snapshot partitions must be a list")
        value = HistoryActiveSnapshot(
            _integer(payload, "sequence"),
            date.fromisoformat(_text(payload, "data_cutoff")),
            date.fromisoformat(_text(payload, "label_cutoff")),
            _text(payload, "calendar_hash"),
            _text(payload, "universe_hash"),
            _text(payload, "source_identity_hash"),
            tuple(_decode_partition(item) for item in raw_partitions),
        )
    return value


def _decode_security(raw: object) -> HistorySecurityIdentity:
    if not isinstance(raw, dict):
        raise TypeError("history security must be an object")
    payload = cast(dict[str, object], raw)
    _require_keys(payload, {"code", "name", "board", "listed_on", "delisted_on"})
    return HistorySecurityIdentity(
        _text(payload, "code"),
        _text(payload, "name"),
        cast(HistorySecurityBoard, _text(payload, "board")),
        date.fromisoformat(_text(payload, "listed_on")),
        _optional_date(payload, "delisted_on"),
    )


def _decode_partition(raw: object) -> HistorySnapshotPartition:
    if not isinstance(raw, dict):
        raise TypeError("history partition must be an object")
    payload = cast(dict[str, object], raw)
    _require_keys(payload, {"relative_path", "sha256", "row_count"})
    return HistorySnapshotPartition(
        _text(payload, "relative_path"),
        _text(payload, "sha256"),
        _integer(payload, "row_count"),
    )


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


def _strings(payload: dict[str, object], key: str) -> tuple[str, ...]:
    value = payload[key]
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise TypeError(f"history control {key} must be a text list")
    return tuple(cast(list[str], value))


def _optional_date(payload: dict[str, object], key: str) -> date | None:
    value = _optional_text(payload, key)
    return date.fromisoformat(value) if value is not None else None


def _shanghai_datetime(payload: dict[str, object], key: str) -> datetime:
    value = datetime.fromisoformat(_text(payload, key))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"history control {key} must include timezone")
    return value.astimezone(_SHANGHAI)


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


def _remove_sqlite_files(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


def _remove_sqlite_sidecars(path: Path) -> None:
    for candidate in (Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


def _checkpoint_wal(path: Path) -> None:
    try:
        with closing(sqlite3.connect(path)) as connection:
            result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    except sqlite3.Error as exc:
        raise HistoryControlError("history control WAL checkpoint failed") from exc
    if result is None or result[0] != 0:
        raise HistoryControlError("history control WAL checkpoint did not complete")


__all__ = [
    "HistoryControlConflictError",
    "HistoryControlError",
    "HistoryControlIntegrityStatus",
    "HistoryControlRegressionError",
    "SQLiteHistoryControlRepository",
    "HistoryDiskPreflightStatus",
    "HistoryMaintenanceAlreadyRunningError",
    "HistoryMaintenanceLock",
    "inspect_history_disk",
]
