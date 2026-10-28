"""Crash-safe SQLite control plane for zero-argument history maintenance."""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

from trader.download.domain.history_control import (
    HistoryCalendarIdentity,
    HistoryControlState,
    HistoryDiskRequirement,
    HistoryPublishedControlState,
    HistorySnapshot,
    HistorySourceIdentity,
    HistorySyncCheckpoint,
    HistoryUniverseIdentity,
)
from trader.download.domain.history_reference import HistoryReferenceSnapshot
from trader.download.domain.prepared_reference import PreparedReference
from trader.download.infra.history_control_rows import (
    HISTORY_CONTROL_SCHEMA,
    ControlKeys,
    ControlKind,
    ControlRecord,
    ReadControlRecord,
    WriteControlRecord,
)
from trader.download.infra.history_download_index import HISTORY_DOWNLOAD_SCHEMA
from trader.download.infra.history_reference_index import ReadHistoryReference, WriteHistoryReference
from trader.download.infra.reference_index import SQLiteReferenceIndex
from trader.infra.process_lock import ProcessLock, ProcessLockError
from trader.infra.sqlite_codec import DecodeHash, EncodeHash
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


class DiskUsageResult(Protocol):
    @property
    def free(self) -> int: ...


DiskUsage = Callable[[Path], DiskUsageResult]

_SCHEMA_IDENTITY = "history_catalog"


class HistoryControlError(RuntimeError):
    """The history control database cannot be trusted or updated."""


class HistoryControlConflictError(HistoryControlError):
    """An immutable control identity already has different content."""


class HistoryControlRegressionError(HistoryControlError):
    """An published snapshot update would move the published pointer backward."""


class HistoryMaintenanceAlreadyRunningError(HistoryControlError):
    """Another history maintenance process owns the non-blocking lock."""


@dataclass(frozen=True)
class HistoryControlIntegrityStatus:
    state: Literal["missing", "healthy", "corrupt"]
    reason: str | None


@dataclass(frozen=True)
class HistoryRebuildInput:
    state: HistoryControlState
    reference_path: Path
    references: tuple[tuple[HistoryReferenceSnapshot, str], ...]
    reference_ids: tuple[tuple[str, str], ...]


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
    def __init__(
        self, path: Path, *, reference_path: Path | None = None, fault_injector: FaultInjector | None = None
    ) -> None:
        self._path = path
        self._reference_path = reference_path
        self._fault_injector = fault_injector or (lambda _stage: None)

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with SqliteSession(
                self._path, SqliteConnectionOptions(journal_mode="WAL", foreign_keys=True)
            ) as connection:
                EnsureSqliteSchema(
                    connection,
                    SqliteSchema(
                        HISTORY_CONTROL_SCHEMA.version,
                        HISTORY_CONTROL_SCHEMA.statements + HISTORY_DOWNLOAD_SCHEMA.statements,
                    ),
                    allow_create=True,
                    initialize=self._initialize_metadata,
                    validate=self._validate_metadata,
                )
                CheckSqliteIntegrity(connection)
        except (sqlite3.Error, ValueError) as exc:
            raise HistoryControlError("history control initialization failed") from exc
        self._fault_injector("schema_initialized")
        _fsync_file(self._path)
        _fsync_directory(self._path.parent)

    def _initialize_metadata(self, connection: SqliteConnection) -> None:
        relative = (
            os.path.relpath(self._reference_path.resolve(), self._path.parent.resolve())
            if self._reference_path
            else None
        )
        connection.run("INSERT INTO schema_contract VALUES(1,?,2,?)", (_SCHEMA_IDENTITY, relative))

    def _validate_metadata(self, connection: SqliteConnection) -> None:
        row = connection.run(
            "SELECT schema_id,schema_version,reference_file FROM schema_contract WHERE singleton=1"
        ).fetchone()
        if row is None or tuple(row)[:2] != (_SCHEMA_IDENTITY, 2):
            raise HistoryControlError("history_format_requires_rebuild")
        if self._reference_path is not None and (
            row[2] is None or (self._path.parent / row[2]).resolve() != self._reference_path.resolve()
        ):
            raise HistoryControlError("history_reference_location_conflict")

    def _reference_index(self, connection: SqliteConnection, base: Path | None = None) -> SQLiteReferenceIndex | None:
        row = connection.run("SELECT reference_file FROM schema_contract WHERE singleton=1").require_one()
        return SQLiteReferenceIndex(((base or self._path.parent) / row[0]).resolve()) if row[0] is not None else None

    def integrity(self) -> HistoryControlIntegrityStatus:
        if not self._path.is_file():
            return HistoryControlIntegrityStatus("missing", "control_database_missing")
        try:
            with SqliteSession(self._path, SqliteConnectionOptions(read_only=True)) as connection:
                CheckSqliteIntegrity(connection)
                self._validate_metadata(connection)
        except (sqlite3.Error, ValueError, HistoryControlError):
            return HistoryControlIntegrityStatus("corrupt", "control_database_invalid")
        return HistoryControlIntegrityStatus("healthy", None)

    def save_source(self, value: HistorySourceIdentity) -> None:
        self._save(value)

    def save_calendar(self, value: HistoryCalendarIdentity, publication_id: str) -> None:
        self._save(value, publication_id)

    def save_universe(self, value: HistoryUniverseIdentity, publication_id: str) -> None:
        self._save(value, publication_id)

    def save_checkpoint(self, value: HistorySyncCheckpoint) -> None:
        self._save(value)

    def save_reference(self, reference: HistoryReferenceSnapshot, publication_id: str) -> None:
        with (
            SqliteSession(self._path, SqliteConnectionOptions(foreign_keys=True)) as connection,
            SqliteTransaction(connection, immediate=True),
        ):
            index = self._reference_index(connection)
            if index is None:
                raise HistoryControlError("history_reference_location_missing")
            WriteHistoryReference(connection, reference, index, publication_id)

    def save_inputs(
        self,
        parents: tuple[HistorySourceIdentity, HistoryCalendarIdentity, HistoryUniverseIdentity],
        reference: HistoryReferenceSnapshot,
        publication_id: str,
    ) -> None:
        """Seal the reference and all unpublished parents in one durable transaction."""
        with (
            SqliteSession(self._path, SqliteConnectionOptions(foreign_keys=True)) as connection,
            SqliteTransaction(connection, immediate=True),
        ):
            index = self._reference_index(connection)
            if index is None:
                raise HistoryControlError("history_reference_location_missing")
            references: dict[str, PreparedReference] = {}
            WriteHistoryReference(connection, reference, index, publication_id, references=references)
            for parent in parents:
                WriteControlRecord(connection, parent, index, publication_id, references=references)

    def read_universe_reference(self, universe_hash: str) -> PreparedReference:
        try:
            with SqliteSession(self._path, SqliteConnectionOptions(read_only=True)) as connection:
                self._validate_metadata(connection)
                index = self._reference_index(connection)
                row = connection.run(
                    "SELECT publication_id FROM universe_version WHERE id=?", (EncodeHash(universe_hash),)
                ).fetchone()
                if index is None or row is None:
                    raise HistoryControlError("history_reference_missing")
                reference = index.read(row[0])
                if reference is None:
                    raise HistoryControlError("history_reference_missing")
                return reference
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise HistoryControlError("history_reference_unavailable") from exc

    def reference_path(self) -> Path:
        try:
            with SqliteSession(self._path, SqliteConnectionOptions(read_only=True)) as connection:
                self._validate_metadata(connection)
                index = self._reference_index(connection)
                if index is None:
                    raise HistoryControlError("history_reference_location_missing")
                return index.path
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise HistoryControlError("history_reference_location_invalid") from exc

    def publish_snapshot(self, value: HistorySnapshot, *, reference: HistoryReferenceSnapshot | None = None) -> None:
        try:
            source = self.read_source(value.source_identity_hash)
        except HistoryControlError as exc:
            raise HistoryControlConflictError("history snapshot parent identities are not sealed") from exc
        digest = source.supplier_contract[12:76]
        if not source.supplier_contract.startswith("history_ref_") or (
            reference is not None and reference.content_hash != digest
        ):
            raise HistoryControlConflictError("history_reference_identity_conflict")
        try:
            # Validate the referenced publication before exposing a catalog pointer.
            references: dict[str, PreparedReference] = {}
            bound = ReadHistoryReference(self._path, digest, references=references)
            if reference is not None and bound != reference:
                raise HistoryControlConflictError("history_reference_identity_conflict")
            with (
                SqliteSession(self._path, SqliteConnectionOptions(foreign_keys=True)) as connection,
                SqliteTransaction(connection, immediate=True),
            ):
                index = self._reference_index(connection)
                parents: tuple[tuple[ControlKind, str], ...] = (
                    ("source", value.source_identity_hash),
                    ("calendar", value.calendar_hash),
                    ("universe", value.universe_hash),
                )
                for kind, key in parents:
                    if ReadControlRecord(connection, kind, key, index, references) is None:
                        raise HistoryControlConflictError("history snapshot parent identities are not sealed")
                current = connection.run(
                    "SELECT snapshot_hash,sync_sequence FROM published_snapshot WHERE singleton=1"
                ).fetchone()
                if current is not None:
                    if DecodeHash(current[0]) == value.content_hash:
                        return
                    if current[1] >= value.sequence:
                        raise HistoryControlRegressionError("history published snapshot would regress or conflict")
                WriteControlRecord(connection, value, index)
                self._fault_injector("snapshot_committed")
                connection.run(
                    "INSERT INTO published_snapshot VALUES(1,?,?) ON CONFLICT(singleton) DO UPDATE SET "
                    "snapshot_hash=excluded.snapshot_hash,sync_sequence=excluded.sync_sequence",
                    (EncodeHash(value.content_hash), value.sequence),
                )
        except (sqlite3.Error, ValueError) as exc:
            raise HistoryControlConflictError("history published snapshot publication failed") from exc
        self._fault_injector("published_snapshot_committed")

    def load_state(self, *, reference_base: Path | None = None) -> HistoryControlState:
        try:
            with (
                SqliteSession(self._path, SqliteConnectionOptions(read_only=True)) as connection,
                SqliteTransaction(connection),
            ):
                self._validate_metadata(connection)
                index = self._reference_index(connection, reference_base)
                references: dict[str, PreparedReference] = {}
                sources = tuple(
                    self._read(connection, "source", key, index) for key in ControlKeys(connection, "source")
                )
                calendars = tuple(
                    self._read(connection, "calendar", key, index, references)
                    for key in ControlKeys(connection, "calendar")
                )
                universes = tuple(
                    self._read(connection, "universe", key, index, references)
                    for key in ControlKeys(connection, "universe")
                )
                checkpoints = tuple(
                    self._read(connection, "checkpoint", key, index) for key in ControlKeys(connection, "checkpoint")
                )
                snapshots = tuple(
                    self._read(connection, "snapshot", key, index) for key in ControlKeys(connection, "snapshot")
                )
                current_snapshot = connection.run(
                    "SELECT snapshot_hash,sync_sequence FROM published_snapshot WHERE singleton=1"
                ).fetchone()
            state = HistoryControlState(
                cast(tuple[HistorySourceIdentity, ...], sources),
                cast(tuple[HistoryCalendarIdentity, ...], calendars),
                cast(tuple[HistoryUniverseIdentity, ...], universes),
                cast(tuple[HistorySyncCheckpoint, ...], checkpoints),
                cast(tuple[HistorySnapshot, ...], snapshots),
                DecodeHash(current_snapshot[0]) if current_snapshot is not None else None,
            )
            if current_snapshot is not None and (
                state.published_snapshot is None or state.published_snapshot.sequence != current_snapshot[1]
            ):
                raise HistoryControlError("history published snapshot pointer is inconsistent")
            return state
        except (sqlite3.Error, ValueError, TypeError) as exc:
            raise HistoryControlError("history control state read failed") from exc

    @staticmethod
    def _read(
        connection: SqliteConnection,
        kind: ControlKind,
        key: str,
        index: SQLiteReferenceIndex | None,
        references: dict[str, PreparedReference] | None = None,
    ) -> ControlRecord:
        value = ReadControlRecord(connection, kind, key, index, references)
        if value is None:
            raise HistoryControlError("history publication record is missing")
        return value

    def read_source(self, content_hash: str) -> HistorySourceIdentity:
        try:
            with SqliteSession(self._path, SqliteConnectionOptions(read_only=True)) as connection:
                self._validate_metadata(connection)
                source = self._read(connection, "source", content_hash, None)
                if not isinstance(source, HistorySourceIdentity):
                    raise HistoryControlError("history source identity is invalid")
                return source
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise HistoryControlError("history source identity read failed") from exc

    def load_published_state(
        self, *, sequence: int | None = None, references: dict[str, PreparedReference] | None = None
    ) -> HistoryPublishedControlState | None:
        if sequence is not None and sequence < 1:
            raise ValueError("history publication sequence must be positive")
        try:
            with (
                SqliteSession(self._path, SqliteConnectionOptions(read_only=True)) as connection,
                SqliteTransaction(connection),
            ):
                self._validate_metadata(connection)
                current_snapshot = connection.run(
                    "SELECT snapshot_hash,sync_sequence FROM published_snapshot WHERE singleton=1"
                ).fetchone()
                if current_snapshot is None or (sequence is not None and sequence > current_snapshot[1]):
                    return None
                selected = sequence if sequence is not None else int(current_snapshot[1])
                index = self._reference_index(connection)
                snapshot = self._read(connection, "snapshot", str(selected), index)
                if not isinstance(snapshot, HistorySnapshot) or (
                    snapshot.sequence != selected
                    or selected == current_snapshot[1]
                    and snapshot.content_hash != DecodeHash(current_snapshot[0])
                ):
                    raise HistoryControlError("history published snapshot pointer is inconsistent")
                source = self._read(connection, "source", snapshot.source_identity_hash, index)
                if references is None:
                    references = {}
                calendar = self._read(connection, "calendar", snapshot.calendar_hash, index, references)
                universe = self._read(connection, "universe", snapshot.universe_hash, index, references)
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
        inputs: HistoryRebuildInput,
        *,
        copy_related_records: Callable[[Path], None],
        fault_injector: FaultInjector | None = None,
    ) -> None:
        state = inputs.state
        current = cls(path, reference_path=inputs.reference_path)
        if current.integrity().state == "healthy":
            raise HistoryControlError("healthy history control database must not be rebuilt")
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.rebuild.", dir=path.parent)
        os.close(descriptor)
        temporary = Path(temporary_name)
        temporary.unlink()
        injector = fault_injector or (lambda _stage: None)
        try:
            rebuilt = cls(temporary, reference_path=inputs.reference_path)
            rebuilt.initialize()
            for reference, publication_id in inputs.references:
                rebuilt.save_reference(reference, publication_id)
            publications = dict(inputs.reference_ids)
            records: tuple[ControlRecord, ...] = (
                *state.sources,
                *state.calendars,
                *state.universes,
                *state.checkpoints,
                *state.snapshots,
            )
            for record in records:
                rebuilt._save(record, publications.get(record.content_hash))
            if state.published_snapshot is not None:
                rebuilt.publish_snapshot(state.published_snapshot)
            copy_related_records(temporary)
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

    def _save(self, value: ControlRecord, publication_id: str | None = None) -> None:
        try:
            with (
                SqliteSession(self._path, SqliteConnectionOptions(foreign_keys=True)) as connection,
                SqliteTransaction(connection, immediate=True),
            ):
                WriteControlRecord(connection, value, self._reference_index(connection), publication_id)
        except (sqlite3.Error, ValueError) as exc:
            raise HistoryControlConflictError("history immutable control record write failed") from exc


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
    "HistoryRebuildInput",
    "InspectHistoryDisk",
]
