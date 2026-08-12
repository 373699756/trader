"""Crash-recoverable formal storage for unified V2 scored decisions."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import cast

from trader.application.ports.decision_records import (
    DecisionRecordConflictError,
    DecisionRecordRecoverySummary,
    DecisionRecordUnavailableError,
    V2DecisionCheckpoint,
)
from trader.domain.recommendation.decision_identity import (
    CommittedDecisionRecord,
    committed_record_bytes,
    committed_record_from_bytes,
)
from trader.domain.recommendation.models import Strategy

FaultInjector = Callable[[str], None]
_MAX_RECOVERY_PAYLOAD_BYTES = 8 * 1024 * 1024


class SnapshotConflictError(RuntimeError):
    """An immutable V2 decision path already contains different bytes."""


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_create_immutable(
    target: Path,
    payload: bytes,
    *,
    expected_sha256: str,
    fault_injector: FaultInjector | None = None,
) -> None:
    if target.exists():
        if _sha256(target.read_bytes()) == expected_sha256:
            return
        raise SnapshotConflictError(f"immutable V2 decision path has different content: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if fault_injector is not None:
            fault_injector("payload_fsynced")
        try:
            os.link(temporary_name, target)
        except FileExistsError as exc:
            if _sha256(target.read_bytes()) != expected_sha256:
                raise SnapshotConflictError(f"immutable V2 decision path has different content: {target}") from exc
        os.unlink(temporary_name)
        _fsync_directory(target.parent)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


class SQLiteDecisionRecordRepository:
    """Stores one immutable formal decision per strategy and trade date."""

    def __init__(
        self,
        runtime_dir: Path,
        *,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        self._root = runtime_dir / "v2-decisions"
        self._database = self._root / "v2-decisions.sqlite3"
        self._records = self._root / "records"
        self._checkpoints = self._root / "checkpoints"
        self._quarantine = self._root / "quarantine"
        self._fault_injector = fault_injector or (lambda _stage: None)
        self._lock = threading.RLock()

    def initialize(self) -> None:
        with self._lock:
            for directory in (self._root, self._records, self._checkpoints, self._quarantine):
                directory.mkdir(parents=True, exist_ok=True)
            _fsync_directory(self._root)
            try:
                with self._connect() as connection:
                    connection.execute("PRAGMA journal_mode=WAL")
                    connection.executescript(_SCHEMA)
            except sqlite3.Error as exc:
                raise DecisionRecordUnavailableError("decision record initialization failed") from exc
            self.recover()

    def commit(self, record: CommittedDecisionRecord) -> None:
        payload = committed_record_bytes(record)
        digest = _bounded_digest(payload)
        relative = Path("records") / record.strategy.value / record.trade_date.isoformat() / f"{record.version}.json"
        with self._lock:
            existing = self._manifest(record.strategy, record.trade_date)
            if existing is not None:
                self._validate_existing_commit(existing, record)
                if existing["status"] == "committed":
                    self._load_manifest(existing)
                    return
            already_committed = self._stage_manifest(record, payload, digest, relative)
            if already_committed is not None:
                self._load_manifest(already_committed)
                return
            self._fault_injector("manifest_staged")
            self._write_immutable(relative, payload, digest)
            self._commit_manifest(record)
            self._fault_injector("manifest_committed")

    def _stage_manifest(
        self,
        record: CommittedDecisionRecord,
        payload: bytes,
        digest: str,
        relative: Path,
    ) -> sqlite3.Row | None:
        try:
            with self._connect() as connection:
                changed = connection.execute(
                    """
                    INSERT INTO decision_records(
                        strategy, trade_date, version, payload_hash,
                        payload_sha256, relative_path, committed_at,
                        commit_kind, status, recovery_payload,
                        recovery_sha256, error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'staged', ?, ?, '')
                    ON CONFLICT(strategy, trade_date) DO UPDATE SET
                        payload_sha256 = excluded.payload_sha256,
                        relative_path = excluded.relative_path,
                        committed_at = excluded.committed_at,
                        commit_kind = excluded.commit_kind,
                        recovery_payload = excluded.recovery_payload,
                        recovery_sha256 = excluded.recovery_sha256,
                        error = ''
                    WHERE decision_records.version = excluded.version
                      AND decision_records.payload_hash = excluded.payload_hash
                      AND decision_records.status = 'staged'
                    """,
                    (
                        record.strategy.value,
                        record.trade_date.isoformat(),
                        record.version,
                        record.payload_hash,
                        digest,
                        relative.as_posix(),
                        record.committed_at.isoformat(),
                        record.commit_kind,
                        payload,
                        digest,
                    ),
                ).rowcount
                concurrent = self._select_manifest(connection, record) if changed != 1 else None
                if concurrent is not None and concurrent["status"] == "committed" and _row_matches(concurrent, record):
                    return concurrent
                if changed != 1:
                    raise self._conflict(record)
        except sqlite3.IntegrityError as exc:
            raise self._conflict(record) from exc
        except sqlite3.Error as exc:
            raise DecisionRecordUnavailableError("decision record staging failed") from exc
        return None

    def _commit_manifest(self, record: CommittedDecisionRecord) -> None:
        try:
            with self._connect() as connection:
                changed = connection.execute(
                    """
                    UPDATE decision_records
                    SET status = 'committed', recovery_payload = NULL,
                        recovery_sha256 = '', error = ''
                    WHERE strategy = ? AND trade_date = ?
                      AND version = ? AND status = 'staged'
                    """,
                    (record.strategy.value, record.trade_date.isoformat(), record.version),
                ).rowcount
                concurrent = self._select_manifest(connection, record) if changed != 1 else None
                if changed != 1 and (
                    concurrent is None or concurrent["status"] != "committed" or not _row_matches(concurrent, record)
                ):
                    raise self._conflict(record)
        except sqlite3.Error as exc:
            raise DecisionRecordUnavailableError("decision record commit failed") from exc

    def load(self, strategy: Strategy, trade_date: date) -> CommittedDecisionRecord | None:
        with self._lock:
            row = self._manifest(strategy, trade_date)
            if row is None:
                return None
            if row["status"] == "quarantined":
                raise DecisionRecordUnavailableError(
                    f"decision record for {strategy.value}/{trade_date.isoformat()} is quarantined"
                )
            if row["status"] != "committed":
                return None
            return self._load_manifest(row)

    def list_dates(self, strategy: Strategy, *, limit: int = 31) -> tuple[date, ...]:
        if limit < 1 or limit > 366:
            raise ValueError("decision date limit must be between 1 and 366")
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT trade_date
                    FROM decision_records
                    WHERE strategy = ? AND status = 'committed'
                    ORDER BY trade_date DESC
                    LIMIT ?
                    """,
                    (strategy.value, limit),
                ).fetchall()
        except sqlite3.Error as exc:
            raise DecisionRecordUnavailableError("decision date listing failed") from exc
        return tuple(date.fromisoformat(str(row["trade_date"])) for row in rows)

    def save_checkpoint(self, checkpoint: V2DecisionCheckpoint) -> None:
        envelope = CommittedDecisionRecord(
            checkpoint.decision,
            checkpoint.boundary_at,
            "checkpoint_recovery",
        )
        payload = committed_record_bytes(envelope)
        digest = _bounded_digest(payload)
        relative = (
            Path("checkpoints")
            / checkpoint.decision.strategy.value
            / checkpoint.decision.trade_date.isoformat()
            / f"{checkpoint.version}.json"
        )
        with self._lock:
            try:
                with self._connect() as connection:
                    row = connection.execute(
                        "SELECT * FROM decision_checkpoints WHERE strategy = ? AND trade_date = ?",
                        (checkpoint.decision.strategy.value, checkpoint.decision.trade_date.isoformat()),
                    ).fetchone()
                    if row is not None:
                        if row["version"] == checkpoint.version:
                            self._load_checkpoint_row(row)
                            return
                        if row["consumed_at"] or str(row["boundary_at"]) != checkpoint.boundary_at.isoformat():
                            raise DecisionRecordConflictError("decision checkpoint cannot be replaced")
                        current = self._load_checkpoint_row(row)
                        if current.decision.observed_at >= checkpoint.decision.observed_at:
                            raise DecisionRecordConflictError("decision checkpoint is not newer")
                    self._write_immutable(relative, payload, digest)
                    changed = connection.execute(
                        """
                        INSERT INTO decision_checkpoints(
                            strategy, trade_date, version, decision_version, observed_at, boundary_at,
                            payload_sha256, relative_path, consumed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                        ON CONFLICT(strategy, trade_date) DO UPDATE SET
                            version = excluded.version,
                            decision_version = excluded.decision_version,
                            observed_at = excluded.observed_at,
                            boundary_at = excluded.boundary_at,
                            payload_sha256 = excluded.payload_sha256,
                            relative_path = excluded.relative_path,
                            consumed_at = NULL
                        WHERE decision_checkpoints.consumed_at IS NULL
                          AND excluded.boundary_at = decision_checkpoints.boundary_at
                          AND excluded.observed_at > decision_checkpoints.observed_at
                        """,
                        (
                            checkpoint.decision.strategy.value,
                            checkpoint.decision.trade_date.isoformat(),
                            checkpoint.version,
                            checkpoint.decision.version,
                            checkpoint.decision.observed_at.isoformat(),
                            checkpoint.boundary_at.isoformat(),
                            digest,
                            relative.as_posix(),
                        ),
                    ).rowcount
                    if changed != 1:
                        concurrent = connection.execute(
                            "SELECT version FROM decision_checkpoints WHERE strategy = ? AND trade_date = ?",
                            (checkpoint.decision.strategy.value, checkpoint.decision.trade_date.isoformat()),
                        ).fetchone()
                        if concurrent is None or concurrent["version"] != checkpoint.version:
                            raise DecisionRecordConflictError("decision checkpoint concurrent update conflict")
            except sqlite3.Error as exc:
                raise DecisionRecordUnavailableError("decision checkpoint save failed") from exc

    def load_checkpoint(self, strategy: Strategy, trade_date: date) -> V2DecisionCheckpoint | None:
        with self._lock:
            try:
                with self._connect() as connection:
                    row = connection.execute(
                        "SELECT * FROM decision_checkpoints WHERE strategy = ? AND trade_date = ?",
                        (strategy.value, trade_date.isoformat()),
                    ).fetchone()
            except sqlite3.Error as exc:
                raise DecisionRecordUnavailableError("decision checkpoint read failed") from exc
            if row is None or row["consumed_at"]:
                return None
            return self._load_checkpoint_row(row)

    def consume_checkpoint(
        self,
        checkpoint: V2DecisionCheckpoint,
        *,
        consumed_at: datetime,
    ) -> None:
        with self._lock:
            try:
                with self._connect() as connection:
                    changed = connection.execute(
                        """
                        UPDATE decision_checkpoints SET consumed_at = ?
                        WHERE strategy = ? AND trade_date = ? AND version = ?
                          AND consumed_at IS NULL
                        """,
                        (
                            consumed_at.isoformat(),
                            checkpoint.decision.strategy.value,
                            checkpoint.decision.trade_date.isoformat(),
                            checkpoint.version,
                        ),
                    ).rowcount
                    if changed == 0:
                        row = connection.execute(
                            "SELECT version, consumed_at FROM decision_checkpoints WHERE strategy = ? AND trade_date = ?",
                            (checkpoint.decision.strategy.value, checkpoint.decision.trade_date.isoformat()),
                        ).fetchone()
                        if row is None or row["version"] != checkpoint.version or not row["consumed_at"]:
                            raise DecisionRecordConflictError("decision checkpoint consume conflict")
            except sqlite3.Error as exc:
                raise DecisionRecordUnavailableError("decision checkpoint consume failed") from exc

    def _load_checkpoint_row(self, row: sqlite3.Row) -> V2DecisionCheckpoint:
        payload = self._verified_payload(str(row["relative_path"]), str(row["payload_sha256"]))
        try:
            envelope = committed_record_from_bytes(payload)
            boundary = envelope.committed_at
            checkpoint = V2DecisionCheckpoint(envelope.decision, boundary)
        except (TypeError, UnicodeError, ValueError) as exc:
            raise DecisionRecordUnavailableError("decision checkpoint verification failed") from exc
        if (
            checkpoint.version != row["version"]
            or checkpoint.decision.version != row["decision_version"]
            or checkpoint.boundary_at.isoformat() != row["boundary_at"]
            or envelope.commit_kind != "checkpoint_recovery"
        ):
            raise DecisionRecordUnavailableError("decision checkpoint identity mismatch")
        return checkpoint

    def recover(self) -> DecisionRecordRecoverySummary:
        recovered = 0
        quarantined = 0
        with self._lock:
            try:
                with self._connect() as connection:
                    for row in connection.execute("SELECT * FROM decision_records WHERE status = 'staged'").fetchall():
                        if self._recover_staged(connection, row):
                            recovered += 1
                        else:
                            quarantined += 1
                    for row in connection.execute(
                        "SELECT * FROM decision_records WHERE status = 'committed'"
                    ).fetchall():
                        if self._committed_error(row):
                            self._quarantine_manifest(connection, row, "committed_file_invalid")
                            quarantined += 1
                    for row in connection.execute(
                        "SELECT * FROM decision_checkpoints WHERE consumed_at IS NULL"
                    ).fetchall():
                        try:
                            self._load_checkpoint_row(row)
                        except DecisionRecordUnavailableError:
                            self._quarantine_checkpoint(connection, row)
                            quarantined += 1
                    orphaned = self._quarantine_orphans(connection)
            except sqlite3.Error as exc:
                raise DecisionRecordUnavailableError("decision record recovery failed") from exc
        return DecisionRecordRecoverySummary(recovered, quarantined, orphaned)

    def _validate_existing_commit(
        self,
        row: sqlite3.Row,
        record: CommittedDecisionRecord,
    ) -> None:
        if row["status"] == "quarantined":
            raise DecisionRecordUnavailableError(
                f"decision record for {record.strategy.value}/{record.trade_date.isoformat()} is quarantined"
            )
        if row["version"] != record.version or row["payload_hash"] != record.payload_hash:
            raise self._conflict(record)

    def _recover_staged(self, connection: sqlite3.Connection, row: sqlite3.Row) -> bool:
        payload = _recovery_payload(row)
        if payload is None or self._record_error(row, payload):
            self._quarantine_manifest(connection, row, "recovery_payload_invalid")
            return False
        try:
            target = self._safe_path(str(row["relative_path"]))
        except DecisionRecordUnavailableError:
            self._quarantine_manifest(connection, row, "recovery_path_invalid")
            return False
        try:
            if not target.is_file() or _sha256(target.read_bytes()) != str(row["payload_sha256"]):
                self._isolate_file(target, "damaged_staged_file")
                _atomic_create_immutable(
                    target,
                    payload,
                    expected_sha256=str(row["payload_sha256"]),
                    fault_injector=self._fault_injector,
                )
        except (OSError, SnapshotConflictError) as exc:
            raise DecisionRecordUnavailableError("staged decision recovery failed") from exc
        connection.execute(
            """
            UPDATE decision_records
            SET status = 'committed', recovery_payload = NULL,
                recovery_sha256 = '', error = ''
            WHERE strategy = ? AND trade_date = ? AND status = 'staged'
            """,
            (row["strategy"], row["trade_date"]),
        )
        return True

    def _committed_error(self, row: sqlite3.Row) -> str:
        try:
            payload = self._verified_payload(str(row["relative_path"]), str(row["payload_sha256"]))
        except DecisionRecordUnavailableError:
            return "committed_file_missing_or_invalid"
        return self._record_error(row, payload)

    def _record_error(self, row: sqlite3.Row, payload: bytes) -> str:
        if _sha256(payload) != str(row["payload_sha256"]):
            return "payload_hash_mismatch"
        recovery_sha = str(row["recovery_sha256"])
        if recovery_sha and _sha256(payload) != recovery_sha:
            return "recovery_hash_mismatch"
        try:
            record = committed_record_from_bytes(payload)
        except (ValueError, TypeError, UnicodeError):
            return "payload_invalid"
        if (
            record.strategy.value != row["strategy"]
            or record.trade_date.isoformat() != row["trade_date"]
            or record.version != row["version"]
            or record.payload_hash != row["payload_hash"]
            or record.committed_at.isoformat() != row["committed_at"]
            or record.commit_kind != row["commit_kind"]
        ):
            return "manifest_mismatch"
        return ""

    def _load_manifest(self, row: sqlite3.Row) -> CommittedDecisionRecord:
        payload = self._verified_payload(str(row["relative_path"]), str(row["payload_sha256"]))
        error = self._record_error(row, payload)
        if error:
            raise DecisionRecordUnavailableError("decision record verification failed")
        return committed_record_from_bytes(payload)

    def _manifest(self, strategy: Strategy, trade_date: date) -> sqlite3.Row | None:
        try:
            with self._connect() as connection:
                return cast(
                    sqlite3.Row | None,
                    connection.execute(
                        "SELECT * FROM decision_records WHERE strategy = ? AND trade_date = ?",
                        (strategy.value, trade_date.isoformat()),
                    ).fetchone(),
                )
        except sqlite3.Error as exc:
            raise DecisionRecordUnavailableError("decision record manifest read failed") from exc

    @staticmethod
    def _select_manifest(
        connection: sqlite3.Connection,
        record: CommittedDecisionRecord,
    ) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            connection.execute(
                "SELECT * FROM decision_records WHERE strategy = ? AND trade_date = ?",
                (record.strategy.value, record.trade_date.isoformat()),
            ).fetchone(),
        )

    def _write_immutable(self, relative: Path, payload: bytes, digest: str) -> None:
        try:
            _atomic_create_immutable(
                self._safe_path(relative.as_posix()),
                payload,
                expected_sha256=digest,
                fault_injector=self._fault_injector,
            )
        except (OSError, SnapshotConflictError) as exc:
            raise DecisionRecordUnavailableError("immutable decision record write failed") from exc

    def _verified_payload(self, relative_path: str, expected_sha256: str) -> bytes:
        try:
            payload = self._safe_path(relative_path).read_bytes()
        except OSError as exc:
            raise DecisionRecordUnavailableError("decision record file verification failed") from exc
        if _sha256(payload) != expected_sha256:
            raise DecisionRecordUnavailableError("decision record file verification failed")
        return payload

    def _safe_path(self, relative_path: str) -> Path:
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise DecisionRecordUnavailableError("decision record manifest path is invalid")
        try:
            root = self._root.resolve()
            target = (root / relative).resolve(strict=False)
            target.relative_to(root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise DecisionRecordUnavailableError("decision record manifest path is invalid") from exc
        return target

    def _quarantine_manifest(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        reason: str,
    ) -> None:
        try:
            source = self._safe_path(str(row["relative_path"]))
        except DecisionRecordUnavailableError:
            source = None
        if source is not None:
            self._isolate_file(source, reason)
        connection.execute(
            """
            UPDATE decision_records
            SET status = 'quarantined', recovery_payload = NULL,
                recovery_sha256 = '', error = ?
            WHERE strategy = ? AND trade_date = ?
            """,
            (reason, row["strategy"], row["trade_date"]),
        )

    def _isolate_file(self, source: Path, reason: str) -> None:
        if not source.exists():
            return
        relative = source.relative_to(self._root)
        destination = self._quarantine / reason / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            destination = destination.with_name(f"{destination.stem}-duplicate{destination.suffix}")
        source_parent = source.parent
        shutil.move(str(source), str(destination))
        _fsync_directory(source_parent)
        _fsync_directory(destination.parent)

    def _quarantine_checkpoint(self, connection: sqlite3.Connection, row: sqlite3.Row) -> None:
        try:
            source = self._safe_path(str(row["relative_path"]))
        except DecisionRecordUnavailableError:
            source = None
        if source is not None:
            self._isolate_file(source, "checkpoint_invalid")
        connection.execute(
            "DELETE FROM decision_checkpoints WHERE strategy = ? AND trade_date = ?",
            (row["strategy"], row["trade_date"]),
        )

    def _quarantine_orphans(self, connection: sqlite3.Connection) -> int:
        known_records = {
            str(row["relative_path"])
            for row in connection.execute("SELECT relative_path FROM decision_records").fetchall()
        }
        known_checkpoints = {
            str(row["relative_path"])
            for row in connection.execute("SELECT relative_path FROM decision_checkpoints").fetchall()
        }
        count = 0
        for root, known in ((self._records, known_records), (self._checkpoints, known_checkpoints)):
            for path in root.rglob("*.json"):
                relative = path.relative_to(self._root).as_posix()
                if relative in known:
                    continue
                self._isolate_file(path, "orphan_without_manifest")
                count += 1
        return count

    @staticmethod
    def _conflict(record: CommittedDecisionRecord) -> DecisionRecordConflictError:
        return DecisionRecordConflictError(
            f"decision record for {record.strategy.value}/{record.trade_date.isoformat()} is already committed"
        )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database, timeout=10.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=10000")
            with connection:
                yield connection
        finally:
            connection.close()


def _bounded_digest(payload: bytes) -> str:
    if len(payload) > _MAX_RECOVERY_PAYLOAD_BYTES:
        raise DecisionRecordUnavailableError("decision recovery payload exceeds the bounded limit")
    return _sha256(payload)


def _recovery_payload(row: sqlite3.Row) -> bytes | None:
    raw = row["recovery_payload"]
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        return None
    payload = bytes(raw)
    if not 0 < len(payload) <= _MAX_RECOVERY_PAYLOAD_BYTES:
        return None
    return payload


def _row_matches(row: sqlite3.Row, record: CommittedDecisionRecord) -> bool:
    return str(row["version"]) == record.version and str(row["payload_hash"]) == record.payload_hash


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS decision_records (
    strategy TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    version TEXT NOT NULL UNIQUE,
    payload_hash TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    commit_kind TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('staged', 'committed', 'quarantined')),
    recovery_payload BLOB,
    recovery_sha256 TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(strategy, trade_date)
);
CREATE TABLE IF NOT EXISTS decision_checkpoints (
    strategy TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    version TEXT NOT NULL UNIQUE,
    decision_version TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    boundary_at TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    consumed_at TEXT,
    PRIMARY KEY(strategy, trade_date)
);
"""


__all__ = ["SQLiteDecisionRecordRepository"]
