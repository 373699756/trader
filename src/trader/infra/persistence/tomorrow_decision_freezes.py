"""Separate SQLite manifest and immutable files for tomorrow v2 freezes."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path
from typing import cast

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
)
from trader.infra.persistence.tomorrow_decision_records import (
    checkpoint_bytes,
    checkpoint_from_bytes,
    freeze_bytes,
    freeze_from_bytes,
)


class TomorrowDecisionFreezeRepository:
    """Owns only the isolated ``tomorrow-v2`` runtime namespace."""

    def __init__(self, runtime_dir: Path) -> None:
        self._root = runtime_dir / "tomorrow-v2"
        self._database = self._root / "tomorrow-v2.sqlite3"
        self._lock = threading.RLock()

    def initialize(self) -> None:
        with self._lock:
            self._root.mkdir(parents=True, exist_ok=True)
            (self._root / "checkpoints").mkdir(exist_ok=True)
            (self._root / "freezes").mkdir(exist_ok=True)
            with self._connect() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS tomorrow_freeze_checkpoints (
                        trade_date TEXT PRIMARY KEY,
                        version TEXT NOT NULL UNIQUE,
                        content_hash TEXT NOT NULL,
                        payload_sha256 TEXT NOT NULL,
                        relative_path TEXT NOT NULL,
                        boundary_at TEXT NOT NULL,
                        decision_observed_at TEXT NOT NULL,
                        decision_sequence INTEGER NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('active', 'consumed')),
                        consumed_at TEXT
                    );
                    CREATE TABLE IF NOT EXISTS tomorrow_decision_freezes (
                        trade_date TEXT PRIMARY KEY,
                        version TEXT NOT NULL UNIQUE,
                        content_hash TEXT NOT NULL,
                        payload_sha256 TEXT NOT NULL,
                        relative_path TEXT NOT NULL,
                        frozen_at TEXT NOT NULL,
                        freeze_kind TEXT NOT NULL
                    );
                    """
                )

    def save_checkpoint(self, checkpoint: TomorrowFreezeCheckpoint) -> None:
        checkpoint = official_checkpoint(checkpoint)
        payload = checkpoint_bytes(checkpoint)
        digest = _sha256(payload)
        relative = Path("checkpoints") / checkpoint.trade_date.isoformat() / f"{checkpoint.version}.json"
        with self._lock:
            self._write_immutable(relative, payload, digest)
            try:
                with self._connect() as connection:
                    current = connection.execute(
                        """
                        SELECT decision_observed_at
                        FROM tomorrow_freeze_checkpoints
                        WHERE trade_date = ?
                        """,
                        (checkpoint.trade_date.isoformat(),),
                    ).fetchone()
                    if (
                        current is not None
                        and datetime.fromisoformat(str(current[0])) > checkpoint.decision.observed_at
                    ):
                        return
                    connection.execute(
                        """
                        INSERT INTO tomorrow_freeze_checkpoints (
                            trade_date, version, content_hash, payload_sha256,
                            relative_path, boundary_at, decision_observed_at,
                            decision_sequence, status, consumed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL)
                        ON CONFLICT(trade_date) DO UPDATE SET
                            version = excluded.version,
                            content_hash = excluded.content_hash,
                            payload_sha256 = excluded.payload_sha256,
                            relative_path = excluded.relative_path,
                            boundary_at = excluded.boundary_at,
                            decision_observed_at = excluded.decision_observed_at,
                            decision_sequence = excluded.decision_sequence,
                            status = 'active',
                            consumed_at = NULL
                        WHERE excluded.decision_observed_at >
                                  tomorrow_freeze_checkpoints.decision_observed_at
                           OR (
                               excluded.decision_observed_at =
                                   tomorrow_freeze_checkpoints.decision_observed_at
                               AND excluded.decision_sequence >=
                                   tomorrow_freeze_checkpoints.decision_sequence
                           )
                        """,
                        (
                            checkpoint.trade_date.isoformat(),
                            checkpoint.version,
                            checkpoint.content_hash,
                            digest,
                            relative.as_posix(),
                            checkpoint.boundary_at.isoformat(),
                            checkpoint.decision.observed_at.isoformat(),
                            checkpoint.decision.sequence,
                        ),
                    )
            except sqlite3.Error as exc:
                raise DecisionFreezeUnavailableError("checkpoint manifest write failed") from exc

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
        digest = _sha256(payload)
        relative = Path("freezes") / frozen.trade_date.isoformat() / f"{frozen.version}.json"
        with self._lock:
            existing = self._freeze_manifest(frozen.trade_date)
            if existing is not None:
                if existing[0] != frozen.version or existing[1] != frozen.content_hash:
                    raise DecisionFreezeConflictError(
                        f"tomorrow freeze for {frozen.trade_date.isoformat()} is already committed"
                    )
                self._verified_payload(str(existing[3]), str(existing[2]))
                return
            self._write_immutable(relative, payload, digest)
            try:
                with self._connect() as connection:
                    connection.execute(
                        """
                        INSERT INTO tomorrow_decision_freezes (
                            trade_date, version, content_hash, payload_sha256,
                            relative_path, frozen_at, freeze_kind
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            frozen.trade_date.isoformat(),
                            frozen.version,
                            frozen.content_hash,
                            digest,
                            relative.as_posix(),
                            frozen.frozen_at.isoformat(),
                            frozen.freeze_kind,
                        ),
                    )
            except sqlite3.IntegrityError as exc:
                raise DecisionFreezeConflictError(
                    f"tomorrow freeze for {frozen.trade_date.isoformat()} is already committed"
                ) from exc
            except sqlite3.Error as exc:
                raise DecisionFreezeUnavailableError("freeze manifest write failed") from exc

    def load_frozen(self, trade_date: date) -> TomorrowDecisionFreeze | None:
        with self._lock:
            row = self._freeze_manifest(trade_date)
            if row is None:
                return None
            payload = self._verified_payload(str(row[3]), str(row[2]))
            try:
                frozen = freeze_from_bytes(payload)
            except (ValueError, TypeError, UnicodeError) as exc:
                raise DecisionFreezeUnavailableError("freeze verification failed") from exc
            if frozen.version != row[0] or frozen.content_hash != row[1]:
                raise DecisionFreezeUnavailableError("freeze manifest verification failed")
            return official_freeze(frozen)

    def _freeze_manifest(self, trade_date: date) -> sqlite3.Row | None:
        try:
            with self._connect() as connection:
                return cast(
                    sqlite3.Row | None,
                    connection.execute(
                        """
                        SELECT version, content_hash, payload_sha256, relative_path
                        FROM tomorrow_decision_freezes
                        WHERE trade_date = ?
                        """,
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
            _atomic_create_immutable(target, payload, expected_sha256=digest)
        except (OSError, SnapshotConflictError) as exc:
            raise DecisionFreezeUnavailableError("immutable decision file write failed") from exc

    def _safe_path(self, relative_path: str) -> Path:
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise DecisionFreezeUnavailableError("decision manifest path verification failed")
        return self._root / relative

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


__all__ = ["TomorrowDecisionFreezeRepository"]
