"""Read-only integrity and paired-publication audit of the shared daily window."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from trader.download.infra.qfq_sqlite import SQLiteQfqWindowCache
from trader.infra.sqlite_connection import (
    CheckSqliteIntegrity,
    SqliteConnectionOptions,
    SqliteSession,
    SqliteTransaction,
)

from .reporting import emit_report


def AuditActiveQfq(path: Path) -> dict[str, object]:
    """Report aggregate evidence only; no initialization or recovery writes."""
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError("qfq database missing or unsafe")
    caches = (SQLiteQfqWindowCache(path, "v2"), SQLiteQfqWindowCache(path, "v3"))
    before = tuple(cache.manifest() for cache in caches)
    profiles: list[dict[str, object]] = []
    failures: list[str] = []
    for cache, manifest in zip(caches, before, strict=True):
        if manifest is None:
            profiles.append({"profile": cache.profile, "codes": 0, "rows": 0})
            failures.append("profile_empty")
            continue
        rows = 0
        for window in cache.iter_windows(manifest, sessions=cache.sessions):
            rows += len(window.cells)
        profiles.append({"profile": cache.profile, "codes": len(manifest.universe_codes), "rows": rows})
    with (
        SqliteSession(path, SqliteConnectionOptions(read_only=True)) as connection,
        SqliteTransaction(connection),
    ):
        CheckSqliteIntegrity(connection)
        mode = str(connection.run("PRAGMA journal_mode").require_one()[0]).lower()
        missing = int(
            connection.run(
                "SELECT COUNT(*) FROM identities a LEFT JOIN identities b ON a.code=b.code AND a.profile<>b.profile "
                "WHERE b.code IS NULL OR a.source<>b.source OR a.cutoff<>b.cutoff"
            ).require_one()[0]
        )
        unpaired = int(
            connection.run(
                "SELECT COUNT(*) FROM completions c WHERE NOT EXISTS "
                "(SELECT 1 FROM identities a JOIN identities b ON a.code=b.code "
                "WHERE a.profile='v2' AND b.profile='v3' AND a.code=c.code "
                "AND a.source=c.source AND b.source=c.source AND a.cutoff=c.day AND b.cutoff=c.day)"
            ).require_one()[0]
        )
        unconfirmed = int(
            connection.run(
                "SELECT COUNT(*) FROM identities a WHERE a.profile='v2' AND NOT EXISTS "
                "(SELECT 1 FROM completions c WHERE c.code=a.code AND c.source=a.source AND c.day=a.cutoff)"
            ).require_one()[0]
        )
    if tuple(cache.manifest() for cache in caches) != before:
        failures.append("publication_changed_during_audit")
    status = "failed" if failures else "degraded" if missing or unpaired or unconfirmed or mode != "wal" else "passed"
    return {
        "schema_version": "qfq-sqlite-diagnostic",
        "status": status,
        "summary": {
            "mode": "shared_daily_window",
            "active_data_writes": 0,
            "databases": 1,
            "profiles": profiles,
            "journal_mode": mode,
            "paired_identity_mismatches": missing,
            "checkpoint_not_paired": unpaired,
            "database_not_checkpointed": unconfirmed,
            "failures": tuple(sorted(set(failures))),
        },
    }


def ClassifyQfqDiagnosticError(exc: BaseException) -> str:
    if isinstance(exc, sqlite3.OperationalError) and any(marker in str(exc).lower() for marker in ("locked", "busy")):
        return "qfq_pair_database_busy"
    if isinstance(exc, FileNotFoundError):
        return "qfq_database_unavailable"
    if isinstance(exc, sqlite3.DatabaseError):
        return "qfq_database_invalid"
    return "qfq_diagnostic_failed"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = AuditActiveQfq(args.database.expanduser().resolve())
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        report = {
            "schema_version": "qfq-sqlite-diagnostic",
            "status": "failed",
            "error": ClassifyQfqDiagnosticError(exc),
        }
    emit_report(report)
    return 1 if report["status"] == "failed" else 0


__all__ = ["AuditActiveQfq", "ClassifyQfqDiagnosticError"]

if __name__ == "__main__":
    raise SystemExit(main())
