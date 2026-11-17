"""Read-only integrity checks for an explicitly stopped external candidate."""

from pathlib import Path

from trader.infra.sqlite_connection import CheckSqliteIntegrity, SqliteConnectionOptions, SqliteSession
from trader.infra.sqlite_policy import SqliteIoPolicy


def ValidateCandidateDatabase(path: Path, *, sqlite_policy: SqliteIoPolicy) -> None:
    for suffix in ("-wal", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ValueError("migration_requires_stopped_checkpointed_source")
    with SqliteSession(path, SqliteConnectionOptions(read_only=True, immutable=True), sqlite_policy=sqlite_policy) as connection:
        CheckSqliteIntegrity(connection)
        if connection.run("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("migration_foreign_key_invalid")
