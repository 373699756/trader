"""Crash-recoverable SQLite manifests and immutable files for tomorrow v2."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal, cast

from trader.application.official_records import official_checkpoint, official_freeze
from trader.application.ports.decision_freezes import (
    DecisionFreezeConflictError,
    DecisionFreezeUnavailableError,
)
from trader.domain.recommendation.tomorrow_freeze import (
    TomorrowDecisionFreeze,
    TomorrowFreezeCheckpoint,
)
from trader.infra.persistence.snapshot_files import (
    SnapshotConflictError,
    _atomic_create_immutable,
    _fsync_directory,
)
from trader.infra.persistence.tomorrow_decision_records import (
    checkpoint_bytes,
    checkpoint_from_bytes,
    freeze_bytes,
    freeze_from_bytes,
)

FaultInjector = Callable[[str], None]
RecordKind = Literal["checkpoint", "freeze"]
_MAX_RECOVERY_PAYLOAD_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class DecisionRecoverySummary:
    recovered: int = 0
    quarantined: int = 0
    orphaned: int = 0


class TomorrowDecisionFreezeRepository:
    """Owns only the isolated ``tomorrow-v2`` runtime namespace."""

    def __init__(
        self,
        runtime_dir: Path,
        *,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        self._root = runtime_dir / "tomorrow-v2"
        self._database = self._root / "tomorrow-v2.sqlite3"
        self._quarantine = self._root / "quarantine"
        self._fault_injector = fault_injector or (lambda _stage: None)
        self._lock = threading.RLock()

    def initialize(self) -> None:
        with self._lock:
            self._root.mkdir(parents=True, exist_ok=True)
            for directory in ("checkpoints", "freezes", "quarantine"):
                (self._root / directory).mkdir(exist_ok=True)
            _fsync_directory(self._root)
            with self._connect() as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                self._initialize_schema(connection)
            self.recover()

    def save_checkpoint(self, checkpoint: TomorrowFreezeCheckpoint) -> None:
        checkpoint = official_checkpoint(checkpoint)
        payload = checkpoint_bytes(checkpoint)
        digest = _bounded_digest(payload)
        relative = Path("checkpoints") / checkpoint.trade_date.isoformat() / f"{checkpoint.version}.json"
        trade_date = checkpoint.trade_date.isoformat()
        with self._lock:
            previous_relative = ""
            try:
                with self._connect() as connection:
                    current = connection.execute(
                        """
                        SELECT version, decision_observed_at, decision_sequence, relative_path
                        FROM tomorrow_freeze_checkpoints
                        WHERE trade_date = ?
                        """,
                        (trade_date,),
                    ).fetchone()
                    if current is not None and _checkpoint_is_newer(current, checkpoint):
                        return
                    previous_relative = str(current["relative_path"]) if current is not None else ""
                    connection.execute(
                        """
                        INSERT INTO tomorrow_freeze_checkpoints (
                            trade_date, version, content_hash, payload_sha256,
                            relative_path, boundary_at, decision_observed_at,
                            decision_sequence, status, consumed_at, recovery_payload,
                            recovery_sha256, error
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'staged', NULL, ?, ?, '')
                        ON CONFLICT(trade_date) DO UPDATE SET
                            version = excluded.version,
                            content_hash = excluded.content_hash,
                            payload_sha256 = excluded.payload_sha256,
                            relative_path = excluded.relative_path,
                            boundary_at = excluded.boundary_at,
                            decision_observed_at = excluded.decision_observed_at,
                            decision_sequence = excluded.decision_sequence,
                            status = 'staged',
                            consumed_at = NULL,
                            recovery_payload = excluded.recovery_payload,
                            recovery_sha256 = excluded.recovery_sha256,
                            error = ''
                        """,
                        (
                            trade_date,
                            checkpoint.version,
                            checkpoint.content_hash,
                            digest,
                            relative.as_posix(),
                            checkpoint.boundary_at.isoformat(),
                            checkpoint.decision.observed_at.isoformat(),
                            checkpoint.decision.sequence,
                            payload,
                            digest,
                        ),
                    )
            except sqlite3.Error as exc:
                raise DecisionFreezeUnavailableError("checkpoint manifest staging failed") from exc
            self._fault_injector("manifest_staged")
            self._fault_injector("payload_staged")
            self._write_immutable(relative, payload, digest)
            try:
                with self._connect() as connection:
                    connection.execute(
                        """
                        UPDATE tomorrow_freeze_checkpoints
                        SET status = 'active', recovery_payload = NULL,
                            recovery_sha256 = '', error = ''
                        WHERE trade_date = ? AND version = ? AND status = 'staged'
                        """,
                        (trade_date, checkpoint.version),
                    )
            except sqlite3.Error as exc:
                raise DecisionFreezeUnavailableError("checkpoint manifest commit failed") from exc
            self._fault_injector("manifest_committed")
            self._isolate_replaced_file(previous_relative, relative.as_posix(), "checkpoint_replaced")

    def load_checkpoint(self, trade_date: date) -> TomorrowFreezeCheckpoint | None:
        with self._lock:
            try:
                with self._connect() as connection:
                    row = connection.execute(
                        """
                        SELECT version, content_hash, payload_sha256, relative_path
                        FROM tomorrow_freeze_checkpoints
                        WHERE trade_date = ? AND status = 'active'
                        """,
                        (trade_date.isoformat(),),
                    ).fetchone()
            except sqlite3.Error as exc:
                raise DecisionFreezeUnavailableError("checkpoint manifest read failed") from exc
            if row is None:
                return None
            payload = self._verified_payload(str(row[3]), str(row[2]))
            try:
                checkpoint = checkpoint_from_bytes(payload)
            except (ValueError, TypeError, UnicodeError) as exc:
                raise DecisionFreezeUnavailableError("checkpoint verification failed") from exc
            if checkpoint.version != row[0] or checkpoint.content_hash != row[1]:
                raise DecisionFreezeUnavailableError("checkpoint manifest verification failed")
            return checkpoint

    def consume_checkpoint(
        self,
        checkpoint_version: str,
        *,
        consumed_at: datetime,
    ) -> None:
        with self._lock:
            try:
                with self._connect() as connection:
                    connection.execute(
                        """
                        UPDATE tomorrow_freeze_checkpoints
                        SET status = 'consumed', consumed_at = ?
                        WHERE version = ? AND status = 'active'
                        """,
                        (consumed_at.isoformat(), checkpoint_version),
                    )
            except sqlite3.Error as exc:
                raise DecisionFreezeUnavailableError("checkpoint consumption failed") from exc

    def commit_freeze(self, frozen: TomorrowDecisionFreeze) -> None:
        frozen = official_freeze(frozen)
        payload = freeze_bytes(frozen)
        digest = _bounded_digest(payload)
        relative = Path("freezes") / frozen.trade_date.isoformat() / f"{frozen.version}.json"
        trade_date = frozen.trade_date.isoformat()
        with self._lock:
            existing = self._freeze_manifest(frozen.trade_date)
            if existing is not None:
                if existing["version"] != frozen.version or existing["content_hash"] != frozen.content_hash:
                    raise DecisionFreezeConflictError(f"tomorrow freeze for {trade_date} is already committed")
                if existing["status"] == "committed":
                    self._verified_payload(str(existing["relative_path"]), str(existing["payload_sha256"]))
                    return
            try:
                with self._connect() as connection:
                    connection.execute(
                        """
                        INSERT INTO tomorrow_decision_freezes (
                            trade_date, version, content_hash, payload_sha256,
                            relative_path, frozen_at, freeze_kind, status,
                            recovery_payload, recovery_sha256, error
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'staged', ?, ?, '')
                        ON CONFLICT(trade_date) DO UPDATE SET
                            payload_sha256 = excluded.payload_sha256,
                            relative_path = excluded.relative_path,
                            frozen_at = excluded.frozen_at,
                            freeze_kind = excluded.freeze_kind,
                            recovery_payload = excluded.recovery_payload,
                            recovery_sha256 = excluded.recovery_sha256,
                            error = ''
                        WHERE tomorrow_decision_freezes.version = excluded.version
                          AND tomorrow_decision_freezes.content_hash = excluded.content_hash
                          AND tomorrow_decision_freezes.status = 'staged'
                        """,
                        (
                            trade_date,
                            frozen.version,
                            frozen.content_hash,
                            digest,
                            relative.as_posix(),
                            frozen.frozen_at.isoformat(),
                            frozen.freeze_kind,
                            payload,
                            digest,
                        ),
                    )
            except sqlite3.IntegrityError as exc:
                raise DecisionFreezeConflictError(f"tomorrow freeze for {trade_date} is already committed") from exc
            except sqlite3.Error as exc:
                raise DecisionFreezeUnavailableError("freeze manifest staging failed") from exc
            self._fault_injector("manifest_staged")
            self._fault_injector("payload_staged")
            self._write_immutable(relative, payload, digest)
            try:
                with self._connect() as connection:
                    changed = connection.execute(
                        """
                        UPDATE tomorrow_decision_freezes
                        SET status = 'committed', recovery_payload = NULL,
                            recovery_sha256 = '', error = ''
                        WHERE trade_date = ? AND version = ? AND status = 'staged'
                        """,
                        (trade_date, frozen.version),
                    ).rowcount
                    if changed != 1:
                        raise DecisionFreezeConflictError(f"tomorrow freeze for {trade_date} changed during commit")
            except sqlite3.Error as exc:
                raise DecisionFreezeUnavailableError("freeze manifest commit failed") from exc
            self._fault_injector("manifest_committed")

    def load_frozen(self, trade_date: date) -> TomorrowDecisionFreeze | None:
        with self._lock:
            row = self._freeze_manifest(trade_date)
            if row is None or row["status"] != "committed":
                return None
            payload = self._verified_payload(str(row["relative_path"]), str(row["payload_sha256"]))
            try:
                frozen = freeze_from_bytes(payload)
            except (ValueError, TypeError, UnicodeError) as exc:
                raise DecisionFreezeUnavailableError("freeze verification failed") from exc
            if frozen.version != row["version"] or frozen.content_hash != row["content_hash"]:
                raise DecisionFreezeUnavailableError("freeze manifest verification failed")
            return official_freeze(frozen)

    def recover(self) -> DecisionRecoverySummary:
        recovered = 0
        quarantined = 0
        with self._lock:
            try:
                with self._connect() as connection:
                    record_tables: tuple[tuple[RecordKind, str, str], ...] = (
                        ("checkpoint", "tomorrow_freeze_checkpoints", "active"),
                        ("freeze", "tomorrow_decision_freezes", "committed"),
                    )
                    for kind, table, committed_status in record_tables:
                        staged = connection.execute(f"SELECT * FROM {table} WHERE status = 'staged'").fetchall()
                        for row in staged:
                            outcome = self._recover_staged(connection, kind, table, row, committed_status)
                            if outcome is True:
                                recovered += 1
                            elif outcome is False:
                                quarantined += 1
                        protected = connection.execute(
                            f"SELECT * FROM {table} WHERE status = ?",
                            (committed_status,),
                        ).fetchall()
                        for row in protected:
                            error = self._protected_record_error(kind, row)
                            if error:
                                connection.execute(
                                    f"UPDATE {table} SET error = ? WHERE trade_date = ?",
                                    (error, row["trade_date"]),
                                )
                                quarantined += self._audit(connection, kind, row, error)
                    orphaned = self._quarantine_orphans(connection)
            except sqlite3.Error as exc:
                raise DecisionFreezeUnavailableError("tomorrow freeze recovery failed") from exc
        return DecisionRecoverySummary(recovered, quarantined, orphaned)

    def _recover_staged(
        self,
        connection: sqlite3.Connection,
        kind: RecordKind,
        table: str,
        row: sqlite3.Row,
        committed_status: str,
    ) -> bool | None:
        payload = _recovery_payload(row)
        error = _recovery_error(kind, row, payload)
        try:
            target = self._safe_path(str(row["relative_path"]))
        except DecisionFreezeUnavailableError:
            self._audit(connection, kind, row, "recovery_path_invalid")
            connection.execute(
                f"DELETE FROM {table} WHERE trade_date = ? AND status = 'staged'",
                (row["trade_date"],),
            )
            return False
        if not error and payload is not None:
            try:
                if not target.is_file() or _sha256(target.read_bytes()) != str(row["payload_sha256"]):
                    self._isolate_path(target, kind, row, "damaged_staged_file")
                    _atomic_create_immutable(
                        target,
                        payload,
                        expected_sha256=str(row["payload_sha256"]),
                    )
                connection.execute(
                    f"""
                    UPDATE {table}
                    SET status = ?, recovery_payload = NULL, recovery_sha256 = '', error = ''
                    WHERE trade_date = ? AND status = 'staged'
                    """,
                    (committed_status, row["trade_date"]),
                )
                return True
            except (OSError, SnapshotConflictError):
                return None
        self._isolate_path(target, kind, row, error or "invalid_recovery_payload")
        self._audit(connection, kind, row, error or "invalid_recovery_payload")
        connection.execute(
            f"DELETE FROM {table} WHERE trade_date = ? AND status = 'staged'",
            (row["trade_date"],),
        )
        return False

    def _protected_record_error(self, kind: RecordKind, row: sqlite3.Row) -> str:
        try:
            payload = self._verified_payload(str(row["relative_path"]), str(row["payload_sha256"]))
            record = checkpoint_from_bytes(payload) if kind == "checkpoint" else freeze_from_bytes(payload)
        except (DecisionFreezeUnavailableError, ValueError, TypeError, UnicodeError):
            return "committed_file_missing_or_invalid"
        if record.version != row["version"] or record.content_hash != row["content_hash"]:
            return "committed_manifest_mismatch"
        return ""

    def _freeze_manifest(self, trade_date: date) -> sqlite3.Row | None:
        try:
            with self._connect() as connection:
                return cast(
                    sqlite3.Row | None,
                    connection.execute(
                        "SELECT * FROM tomorrow_decision_freezes WHERE trade_date = ?",
                        (trade_date.isoformat(),),
                    ).fetchone(),
                )
        except sqlite3.Error as exc:
            raise DecisionFreezeUnavailableError("freeze manifest read failed") from exc

    def _verified_payload(self, relative_path: str, expected_sha256: str) -> bytes:
        path = self._safe_path(relative_path)
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise DecisionFreezeUnavailableError("decision file verification failed") from exc
        if _sha256(payload) != expected_sha256:
            raise DecisionFreezeUnavailableError("decision file verification failed")
        return payload

    def _write_immutable(self, relative: Path, payload: bytes, digest: str) -> None:
        target = self._safe_path(relative.as_posix())
        try:
            _atomic_create_immutable(
                target,
                payload,
                expected_sha256=digest,
                fault_injector=self._fault_injector,
            )
        except (OSError, SnapshotConflictError) as exc:
            raise DecisionFreezeUnavailableError("immutable decision file write failed") from exc

    def _safe_path(self, relative_path: str) -> Path:
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise DecisionFreezeUnavailableError("decision manifest path verification failed")
        try:
            root = self._root.resolve()
            target = (root / relative).resolve(strict=False)
            target.relative_to(root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise DecisionFreezeUnavailableError("decision manifest path verification failed") from exc
        return target

    def _isolate_replaced_file(self, previous: str, current: str, reason: str) -> None:
        if not previous or previous == current:
            return
        path = self._safe_path(previous)
        row = {
            "trade_date": "",
            "version": path.stem,
            "relative_path": previous,
        }
        self._isolate_path(path, "checkpoint", row, reason)

    def _isolate_path(
        self,
        path: Path,
        kind: RecordKind,
        row: sqlite3.Row | dict[str, str],
        reason: str,
    ) -> None:
        if not path.exists():
            return
        source_parent = path.parent
        try:
            relative = path.relative_to(self._root)
        except ValueError:
            return
        destination = self._quarantine / reason / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            destination = destination.with_name(f"{destination.stem}-{row['version']}{destination.suffix}")
        shutil.move(str(path), str(destination))
        _fsync_directory(source_parent)
        _fsync_directory(destination.parent)

    def _audit(
        self,
        connection: sqlite3.Connection,
        kind: RecordKind,
        row: sqlite3.Row,
        reason: str,
    ) -> int:
        audit_key = f"{kind}:{row['trade_date']}:{row['version']}:{reason}"
        return connection.execute(
            """
            INSERT OR IGNORE INTO tomorrow_freeze_quarantine_audit(
                audit_key, record_kind, trade_date, version, relative_path, reason
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                audit_key,
                kind,
                row["trade_date"],
                row["version"],
                row["relative_path"],
                reason[:240],
            ),
        ).rowcount

    def _quarantine_orphans(self, connection: sqlite3.Connection) -> int:
        known = {
            str(row["relative_path"])
            for table in ("tomorrow_freeze_checkpoints", "tomorrow_decision_freezes")
            for row in connection.execute(f"SELECT relative_path FROM {table}").fetchall()
        }
        count = 0
        for directory, kind in (("checkpoints", "checkpoint"), ("freezes", "freeze")):
            for path in (self._root / directory).rglob("*"):
                if not path.is_file():
                    continue
                relative = path.relative_to(self._root).as_posix()
                if relative in known:
                    continue
                destination = self._quarantine / "orphans" / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    destination = destination.with_name(f"{destination.stem}-duplicate{destination.suffix}")
                source_parent = path.parent
                shutil.move(str(path), str(destination))
                _fsync_directory(source_parent)
                _fsync_directory(destination.parent)
                connection.execute(
                    """
                    INSERT OR IGNORE INTO tomorrow_freeze_quarantine_audit(
                        audit_key, record_kind, trade_date, version, relative_path, reason
                    ) VALUES (?, ?, ?, ?, ?, 'orphan_without_manifest')
                    """,
                    (
                        f"orphan:{relative}",
                        kind,
                        path.parent.name,
                        path.stem,
                        relative,
                    ),
                )
                count += 1
        return count

    def _initialize_schema(self, connection: sqlite3.Connection) -> None:
        checkpoint_sql = _table_sql(connection, "tomorrow_freeze_checkpoints")
        if checkpoint_sql and "'staged'" not in checkpoint_sql:
            connection.execute("ALTER TABLE tomorrow_freeze_checkpoints RENAME TO tomorrow_freeze_checkpoints_legacy")
        freeze_sql = _table_sql(connection, "tomorrow_decision_freezes")
        if freeze_sql and "recovery_payload" not in freeze_sql:
            connection.execute("ALTER TABLE tomorrow_decision_freezes RENAME TO tomorrow_decision_freezes_legacy")
        connection.executescript(_SCHEMA)
        if _table_sql(connection, "tomorrow_freeze_checkpoints_legacy"):
            connection.execute(
                """
                INSERT OR IGNORE INTO tomorrow_freeze_checkpoints(
                    trade_date, version, content_hash, payload_sha256, relative_path,
                    boundary_at, decision_observed_at, decision_sequence, status, consumed_at
                )
                SELECT trade_date, version, content_hash, payload_sha256, relative_path,
                       boundary_at, decision_observed_at, decision_sequence, status, consumed_at
                FROM tomorrow_freeze_checkpoints_legacy
                """
            )
            connection.execute("DROP TABLE tomorrow_freeze_checkpoints_legacy")
        if _table_sql(connection, "tomorrow_decision_freezes_legacy"):
            connection.execute(
                """
                INSERT OR IGNORE INTO tomorrow_decision_freezes(
                    trade_date, version, content_hash, payload_sha256, relative_path,
                    frozen_at, freeze_kind, status
                )
                SELECT trade_date, version, content_hash, payload_sha256, relative_path,
                       frozen_at, freeze_kind, 'committed'
                FROM tomorrow_decision_freezes_legacy
                """
            )
            connection.execute("DROP TABLE tomorrow_decision_freezes_legacy")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database, timeout=10.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 10000")
            with connection:
                yield connection
        finally:
            connection.close()


def _checkpoint_is_newer(row: sqlite3.Row, checkpoint: TomorrowFreezeCheckpoint) -> bool:
    observed_at = datetime.fromisoformat(str(row["decision_observed_at"]))
    return observed_at > checkpoint.decision.observed_at or (
        observed_at == checkpoint.decision.observed_at and int(row["decision_sequence"]) > checkpoint.decision.sequence
    )


def _bounded_digest(payload: bytes) -> str:
    if len(payload) > _MAX_RECOVERY_PAYLOAD_BYTES:
        raise ValueError("decision recovery payload exceeds the bounded persistence limit")
    return _sha256(payload)


def _recovery_payload(row: sqlite3.Row) -> bytes | None:
    raw = row["recovery_payload"]
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        return None
    payload = bytes(raw)
    return payload if 0 < len(payload) <= _MAX_RECOVERY_PAYLOAD_BYTES else None


def _recovery_error(kind: RecordKind, row: sqlite3.Row, payload: bytes | None) -> str:
    if payload is None:
        return "recovery_payload_missing_or_oversized"
    digest = _sha256(payload)
    if digest != str(row["recovery_sha256"]) or digest != str(row["payload_sha256"]):
        return "recovery_payload_hash_mismatch"
    try:
        record = checkpoint_from_bytes(payload) if kind == "checkpoint" else freeze_from_bytes(payload)
    except (ValueError, TypeError, UnicodeError):
        return "recovery_payload_invalid"
    if record.version != row["version"] or record.content_hash != row["content_hash"]:
        return "recovery_manifest_mismatch"
    return ""


def _table_sql(connection: sqlite3.Connection, table: str) -> str:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return str(row[0]) if row is not None and row[0] is not None else ""


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tomorrow_freeze_checkpoints (
    trade_date TEXT PRIMARY KEY,
    version TEXT NOT NULL UNIQUE,
    content_hash TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    boundary_at TEXT NOT NULL,
    decision_observed_at TEXT NOT NULL,
    decision_sequence INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('staged', 'active', 'committed', 'consumed')),
    consumed_at TEXT,
    recovery_payload BLOB,
    recovery_sha256 TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS tomorrow_decision_freezes (
    trade_date TEXT PRIMARY KEY,
    version TEXT NOT NULL UNIQUE,
    content_hash TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    frozen_at TEXT NOT NULL,
    freeze_kind TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('staged', 'active', 'committed', 'consumed')),
    recovery_payload BLOB,
    recovery_sha256 TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS tomorrow_freeze_quarantine_audit (
    audit_key TEXT PRIMARY KEY,
    record_kind TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    version TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    reason TEXT NOT NULL
);
"""


__all__ = ["DecisionRecoverySummary", "TomorrowDecisionFreezeRepository"]
