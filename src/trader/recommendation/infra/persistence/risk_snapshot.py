"""Fixed risk task databases and atomic runtime snapshot publication."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Literal

from trader.recommendation.application.ports.data_plane_records import DataPlaneUnavailableError

RiskDatabaseKind = Literal["risk_download", "risk_history_download", "risk_snapshot"]

_RISK_COLUMNS = (
    "code",
    "evidence_id",
    "observed_at",
    "source_time",
    "source",
    "data_version",
    "schema_version",
    "payload_hash",
    "payload",
    "status",
    "error",
    "recovery_payload",
    "recovery_sha256",
)
_CURSOR_COLUMNS = (
    "cursor_name",
    "observed_at",
    "source_time",
    "source",
    "data_version",
    "schema_version",
    "payload_hash",
    "payload",
    "cursor_value",
    "status",
    "error",
    "recovery_payload",
    "recovery_sha256",
)


def RiskDatabasePath(root: Path, kind: RiskDatabaseKind) -> Path:
    """Return the fixed database path owned by one risk workflow."""
    return root / f"{kind}.sqlite3"


def PublishRiskSnapshot(root: Path) -> Path | None:
    """Publish the latest current and official-history task results as one snapshot."""
    current = RiskDatabasePath(root, "risk_download")
    history = RiskDatabasePath(root, "risk_history_download")
    snapshot = RiskDatabasePath(root, "risk_snapshot")
    if not current.is_file():
        return None

    root.mkdir(parents=True, exist_ok=True)
    temporary = root / f".{snapshot.name}.{os.getpid()}.building"
    try:
        _remove_database_files(temporary)
        _require_healthy_task_database(current, expected_source="current")
        if history.is_file():
            _require_healthy_task_database(history, expected_source="cninfo")
        _backup_database(current, temporary)
        if history.is_file():
            _merge_cninfo_history(temporary, history)
        _checkpoint_database(temporary)
        _require_quick_check(temporary)
        os.replace(temporary, snapshot)
        _remove_sidecars(temporary)
        return snapshot
    except (OSError, sqlite3.DatabaseError) as exc:
        _remove_database_files(temporary)
        raise DataPlaneUnavailableError("risk snapshot publication failed") from exc
    except DataPlaneUnavailableError:
        _remove_database_files(temporary)
        raise


def _require_healthy_task_database(database: Path, *, expected_source: Literal["current", "cninfo"]) -> None:
    _require_quick_check(database)
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    try:
        if expected_source == "current":
            evidence_query = "SELECT 1 FROM risk_evidence_recent WHERE source = 'cninfo' LIMIT 1"
            cursor_query = "SELECT 1 FROM source_cursor_recent WHERE source = 'cninfo' LIMIT 1"
        else:
            evidence_query = "SELECT 1 FROM risk_evidence_recent WHERE source <> 'cninfo' LIMIT 1"
            cursor_query = "SELECT 1 FROM source_cursor_recent WHERE source <> 'cninfo' LIMIT 1"
        contaminated = connection.execute(evidence_query).fetchone() or connection.execute(cursor_query).fetchone()
    finally:
        connection.close()
    if contaminated is not None:
        raise DataPlaneUnavailableError(f"{expected_source} risk database contains records owned by another task")


def _backup_database(source: Path, target: Path) -> None:
    source_connection = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
    target_connection = sqlite3.connect(target)
    try:
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()


def _merge_cninfo_history(target: Path, history: Path) -> None:
    connection = sqlite3.connect(target, uri=True)
    try:
        connection.execute("ATTACH DATABASE ? AS history", (f"{history.resolve().as_uri()}?mode=ro",))
        risk_columns = ", ".join(_RISK_COLUMNS)
        cursor_columns = ", ".join(_CURSOR_COLUMNS)
        with connection:
            connection.execute("DELETE FROM risk_evidence_recent WHERE source = 'cninfo'")
            connection.execute("DELETE FROM source_cursor_recent WHERE source = 'cninfo'")
            connection.execute(
                f"INSERT INTO risk_evidence_recent ({risk_columns}) "
                f"SELECT {risk_columns} FROM history.risk_evidence_recent WHERE status = 'committed'"
            )
            connection.execute(
                f"INSERT INTO source_cursor_recent ({cursor_columns}) "
                f"SELECT {cursor_columns} FROM history.source_cursor_recent WHERE status = 'committed'"
            )
        connection.execute("DETACH DATABASE history")
    finally:
        connection.close()


def _require_quick_check(database: Path) -> None:
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    try:
        row = connection.execute("PRAGMA quick_check").fetchone()
    finally:
        connection.close()
    if row is None or str(row[0]).lower() != "ok":
        raise DataPlaneUnavailableError("risk database quick_check failed")


def _checkpoint_database(database: Path) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        connection.execute("PRAGMA journal_mode=DELETE").fetchone()
    finally:
        connection.close()


def _remove_database_files(database: Path) -> None:
    try:
        database.unlink()
    except FileNotFoundError:
        pass
    _remove_sidecars(database)


def _remove_sidecars(database: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            database.with_name(f"{database.name}{suffix}").unlink()
        except FileNotFoundError:
            pass


__all__ = ["PublishRiskSnapshot", "RiskDatabaseKind", "RiskDatabasePath"]
