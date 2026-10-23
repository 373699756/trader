"""Crash-safe SQLite control plane for zero-argument history maintenance."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal, Protocol, cast
from zoneinfo import ZoneInfo

from trader.download.domain.history_control import (
    HistoryActiveSnapshot,
    HistoryCalendarIdentity,
    HistoryControlState,
    HistoryDiskRequirement,
    HistoryPublishedControlState,
    HistorySecurityBoard,
    HistorySecurityIdentity,
    HistorySnapshotPartition,
    HistorySourceIdentity,
    HistorySyncCheckpoint,
    HistorySyncState,
    HistoryUniverseIdentity,
)
from trader.download.domain.history_reference import HistoryReferenceSnapshot
from trader.download.infra.history_download_index import HISTORY_DOWNLOAD_SCHEMA
from trader.download.infra.history_reference_codec import WriteHistoryReference
from trader.infra.process_lock import ProcessLock, ProcessLockError
from trader.infra.sqlite_connection import (
    CheckSqliteIntegrity,
    EnsureSqliteSchema,
    SqliteConnection,
    SqliteConnectionOptions,
    SqliteSchema,
    SqliteSession,
    SqliteTransaction,
)

FaultInjector = Callable[[str], None]
ControlKind = Literal[
    "source",
    "calendar",
    "universe",
    "checkpoint",
    "snapshot",
]
ControlRecord = (
    HistorySourceIdentity
    | HistoryCalendarIdentity
    | HistoryUniverseIdentity
    | HistorySyncCheckpoint
    | HistoryActiveSnapshot
)


class DiskUsageResult(Protocol):
    @property
    def free(self) -> int: ...


DiskUsage = Callable[[Path], DiskUsageResult]

_SCHEMA_IDENTITY = "history_catalog"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_SCHEMA = SqliteSchema(
    None,
    (
        "CREATE TABLE IF NOT EXISTS bound_references(content_hash TEXT PRIMARY KEY,payload_json TEXT NOT NULL) WITHOUT ROWID",
        """
        CREATE TABLE IF NOT EXISTS metadata (
            singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
            schema_identity TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS immutable_records (
            kind TEXT NOT NULL,
            record_key TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY(kind, record_key)
        ) WITHOUT ROWID
        """,
        """
        CREATE TABLE IF NOT EXISTS active_snapshot (
            singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
            snapshot_hash TEXT NOT NULL,
            sequence INTEGER NOT NULL CHECK(sequence > 0)
        )
        """,
    ),
)


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


class HistoryControlIndex:
    def __init__(self, path: Path, *, fault_injector: FaultInjector | None = None) -> None:
        self._path = path
        self._fault_injector = fault_injector or (lambda _stage: None)

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with SqliteSession(
                self._path, SqliteConnectionOptions(journal_mode="WAL", foreign_keys=True)
            ) as connection:
                EnsureSqliteSchema(
                    connection,
                    SqliteSchema(None, _SCHEMA.statements + HISTORY_DOWNLOAD_SCHEMA.statements),
                    allow_create=True,
                    initialize=self._initialize_metadata,
                    validate=self._validate_metadata,
                )
                CheckSqliteIntegrity(connection)
        except sqlite3.Error as exc:
            raise HistoryControlError("history control initialization failed") from exc
        self._fault_injector("schema_initialized")
        _fsync_file(self._path)
        _fsync_directory(self._path.parent)

    @staticmethod
    def _initialize_metadata(connection: SqliteConnection) -> None:
        connection.run("INSERT INTO metadata(singleton,schema_identity) VALUES (1,?)", (_SCHEMA_IDENTITY,))

    @staticmethod
    def _validate_metadata(connection: SqliteConnection) -> None:
        row = connection.run("SELECT schema_identity FROM metadata WHERE singleton=1").fetchone()
        if row is None or tuple(row) != (_SCHEMA_IDENTITY,):
            raise HistoryControlError("history_format_requires_rebuild")

    def integrity(self) -> HistoryControlIntegrityStatus:
        if not self._path.is_file():
            return HistoryControlIntegrityStatus("missing", "control_database_missing")
        try:
            with SqliteSession(self._path, SqliteConnectionOptions(read_only=True)) as connection:
                check = connection.run("PRAGMA quick_check").fetchone()
                identity = connection.run("SELECT schema_identity FROM metadata WHERE singleton=1").fetchone()
        except sqlite3.Error:
            return HistoryControlIntegrityStatus("corrupt", "control_database_invalid")
        if check is None or identity is None or tuple(check) != ("ok",) or tuple(identity) != (_SCHEMA_IDENTITY,):
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

    def save_reference(self, reference: HistoryReferenceSnapshot) -> None:
        with (
            SqliteSession(self._path, SqliteConnectionOptions(foreign_keys=True)) as connection,
            SqliteTransaction(connection, immediate=True),
        ):
            WriteHistoryReference(connection, reference)

    def publish_snapshot(
        self, value: HistoryActiveSnapshot, *, reference: HistoryReferenceSnapshot | None = None
    ) -> None:
        self._require_snapshot_parents(value)
        source = self.read_source(value.source_identity_hash)
        digest = source.supplier_contract[len("history_ref_") : len("history_ref_") + 64]
        if (
            not source.supplier_contract.startswith("history_ref_")
            or reference is not None
            and reference.content_hash != digest
        ):
            raise HistoryControlConflictError("history_reference_identity_conflict")
        payload = json.dumps(_snapshot_payload(value), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        try:
            with (
                SqliteSession(self._path, SqliteConnectionOptions(foreign_keys=True)) as connection,
                SqliteTransaction(connection, immediate=True),
            ):
                if reference is not None:
                    WriteHistoryReference(connection, reference)
                if connection.run("SELECT 1 FROM bound_references WHERE content_hash=?", (digest,)).fetchone() is None:
                    raise HistoryControlConflictError("history_reference_missing")
                current = connection.run(
                    "SELECT snapshot_hash, sequence FROM active_snapshot WHERE singleton=1"
                ).fetchone()
                if current is not None:
                    current_hash, current_sequence = cast(tuple[str, int], current)
                    if current_hash == value.content_hash:
                        return
                    if current_sequence >= value.sequence:
                        raise HistoryControlRegressionError("history active snapshot would regress or conflict")
                existing = connection.run(
                    "SELECT content_hash, payload_json FROM immutable_records WHERE kind='snapshot' AND record_key=?",
                    (str(value.sequence),),
                ).fetchone()
                if existing is not None and tuple(existing) != (value.content_hash, payload):
                    raise HistoryControlConflictError("history immutable control identity conflicts")
                connection.run(
                    "INSERT OR IGNORE INTO immutable_records(kind,record_key,content_hash,payload_json) "
                    "VALUES ('snapshot',?,?,?)",
                    (str(value.sequence), value.content_hash, payload),
                )
                # The injection remains inside the transaction: neither row may survive alone.
                self._fault_injector("snapshot_committed")
                connection.run(
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
            with SqliteSession(self._path, SqliteConnectionOptions(read_only=True)) as connection:
                present = {
                    (kind, record_key)
                    for kind, record_key in connection.run(
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
            with SqliteSession(self._path, SqliteConnectionOptions(read_only=True)) as connection:
                rows = connection.run(
                    "SELECT kind, content_hash, payload_json FROM immutable_records WHERE kind IN ('source','calendar','universe','checkpoint','snapshot') ORDER BY kind, record_key"
                ).fetchall()
                active = connection.run(
                    "SELECT snapshot_hash, sequence FROM active_snapshot WHERE singleton=1"
                ).fetchone()
        except sqlite3.Error as exc:
            raise HistoryControlError("history control state read failed") from exc
        decoded: dict[str, list[object]] = {
            "source": [],
            "calendar": [],
            "universe": [],
            "checkpoint": [],
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
                cast(tuple[HistoryActiveSnapshot, ...], tuple(decoded["snapshot"])),
                cast(str | None, active[0] if active is not None else None),
            )
        except (TypeError, ValueError) as exc:
            raise HistoryControlError("history control state is inconsistent") from exc
        if active is not None and (state.active_snapshot is None or state.active_snapshot.sequence != active[1]):
            raise HistoryControlError("history active snapshot pointer is inconsistent")
        return state

    def read_source(self, content_hash: str) -> HistorySourceIdentity:
        """Read one immutable supplier identity without scanning historical checkpoints."""
        try:
            with SqliteSession(self._path, SqliteConnectionOptions(read_only=True)) as connection:
                source = _read_publication_record(connection, "source", content_hash)
                if not isinstance(source, HistorySourceIdentity):
                    raise HistoryControlError("history source identity is invalid")
                return source
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise HistoryControlError("history source identity read failed") from exc

    def load_published_state(self, *, sequence: int | None = None) -> HistoryPublishedControlState | None:
        """Read one immutable published sequence, defaulting to the active pointer."""
        if sequence is not None and sequence < 1:
            raise ValueError("history publication sequence must be positive")
        try:
            with (
                SqliteSession(self._path, SqliteConnectionOptions(read_only=True)) as connection,
                SqliteTransaction(connection),
            ):
                identity = connection.run("SELECT schema_identity FROM metadata WHERE singleton=1").fetchone()
                if identity is None or tuple(identity) != (_SCHEMA_IDENTITY,):
                    raise HistoryControlError("history control schema identity is invalid")
                active = connection.run(
                    "SELECT snapshot_hash, sequence FROM active_snapshot WHERE singleton=1"
                ).fetchone()
                if active is None or (sequence is not None and sequence > active[1]):
                    return None
                selected = sequence if sequence is not None else int(active[1])
                snapshot = _read_publication_record(connection, "snapshot", str(selected))
                if not isinstance(snapshot, HistoryActiveSnapshot) or (
                    snapshot.sequence != selected or (selected == active[1] and snapshot.content_hash != active[0])
                ):
                    raise HistoryControlError("history active snapshot pointer is inconsistent")
                source = _read_publication_record(connection, "source", snapshot.source_identity_hash)
                calendar = _read_publication_record(connection, "calendar", snapshot.calendar_hash)
                universe = _read_publication_record(connection, "universe", snapshot.universe_hash)
                if (
                    not isinstance(source, HistorySourceIdentity)
                    or not isinstance(calendar, HistoryCalendarIdentity)
                    or not isinstance(universe, HistoryUniverseIdentity)
                ):
                    raise HistoryControlError("history publication parents are invalid")
                return HistoryPublishedControlState(snapshot, source, calendar, universe)
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise HistoryControlError("history publication control read failed") from exc

    @classmethod
    def rebuild(
        cls,
        path: Path,
        state: HistoryControlState,
        *,
        references: tuple[HistoryReferenceSnapshot, ...],
        restore_related_records: Callable[[Path], None],
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
            for reference in references:
                rebuilt.save_reference(reference)
            _populate_rebuilt_control(rebuilt, state)
            restore_related_records(temporary)
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
            with (
                SqliteSession(self._path, SqliteConnectionOptions(foreign_keys=True)) as connection,
                SqliteTransaction(connection, immediate=True),
            ):
                existing = connection.run(
                    "SELECT content_hash, payload_json FROM immutable_records WHERE kind=? AND record_key=?",
                    (kind, key),
                ).fetchone()
                if existing is not None and tuple(existing) != (content_hash, payload_json):
                    raise HistoryControlConflictError("history immutable control identity conflicts")
                connection.run(
                    "INSERT OR IGNORE INTO immutable_records(kind, record_key, content_hash, payload_json) "
                    "VALUES (?, ?, ?, ?)",
                    (kind, key, content_hash, payload_json),
                )
        except sqlite3.Error as exc:
            raise HistoryControlError("history immutable control record write failed") from exc


def _read_publication_record(connection: SqliteConnection, kind: ControlKind, key: str) -> ControlRecord:
    row = connection.run(
        "SELECT content_hash, payload_json FROM immutable_records WHERE kind=? AND record_key=?", (kind, key)
    ).fetchone()
    if row is None:
        raise HistoryControlError("history publication record is missing")
    value = _decode_record(kind, row[1])
    if value.content_hash != row[0]:
        raise HistoryControlError("history publication record hash is invalid")
    return value


def _populate_rebuilt_control(
    repository: HistoryControlIndex,
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
    for snapshot in state.snapshots:
        repository._save(
            "snapshot",
            str(snapshot.sequence),
            snapshot.content_hash,
            _snapshot_payload(snapshot),
        )
    if state.active_snapshot is not None:
        repository.publish_snapshot(state.active_snapshot)


def InspectHistoryDisk(
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


def _snapshot_payload(value: HistoryActiveSnapshot) -> dict[str, object]:
    return {
        "sequence": value.sequence,
        "data_cutoff": value.data_cutoff.isoformat(),
        "label_cutoff": value.label_cutoff.isoformat(),
        "calendar_hash": value.calendar_hash,
        "universe_hash": value.universe_hash,
        "source_identity_hash": value.source_identity_hash,
        "partitions": [
            {
                "relative_path": item.relative_path,
                "logical_hash": item.logical_hash,
                "row_count": item.row_count,
                "sequence": item.sequence,
                "calendar_month": item.calendar_month,
            }
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
    _require_keys(payload, {"relative_path", "logical_hash", "row_count", "sequence", "calendar_month"})
    return HistorySnapshotPartition(
        _text(payload, "relative_path"),
        _text(payload, "logical_hash"),
        _integer(payload, "row_count"),
        _integer(payload, "calendar_month"),
        _integer(payload, "sequence"),
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
        with SqliteSession(path) as connection:
            result = connection.run("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    except sqlite3.Error as exc:
        raise HistoryControlError("history control WAL checkpoint failed") from exc
    if result is None or result[0] != 0:
        raise HistoryControlError("history control WAL checkpoint did not complete")


__all__ = [
    "HistoryControlConflictError",
    "HistoryControlError",
    "HistoryControlIntegrityStatus",
    "HistoryControlRegressionError",
    "HistoryControlIndex",
    "HistoryDiskPreflightStatus",
    "HistoryMaintenanceAlreadyRunningError",
    "HistoryMaintenanceLock",
    "InspectHistoryDisk",
]
