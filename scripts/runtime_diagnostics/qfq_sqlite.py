"""Read-only integrity and paired-publication audit of the shared daily window."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy

import argparse
import sqlite3
from pathlib import Path

from trader.download.infra.qfq_sqlite import SQLiteQfqWindowCache
from trader.download.infra.download_failure_diagnostics import ReadDownloadFailures
from trader.download.infra.download_settings import LoadDownloadFailureConfiguration
from trader.infra.sqlite_connection import (
    CheckSqliteIntegrity,
    SqliteConnectionOptions,
    SqliteSession,
    SqliteTransaction,
)

from .reporting import emit_report


def AuditQfqDatabase(path: Path, *, sqlite_policy: SqliteIoPolicy) -> dict[str, object]:
    """Report aggregate evidence only; no initialization or recovery writes."""
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError("qfq database missing or unsafe")
    caches = (SQLiteQfqWindowCache(path, "v2", sqlite_policy=sqlite_policy), SQLiteQfqWindowCache(path, "v3", sqlite_policy=sqlite_policy))
    before = tuple(cache.manifest() for cache in caches)
    issues_before = caches[0].price_issues()
    profiles: list[dict[str, object]] = []
    failures: list[str] = []
    for cache, manifest in zip(caches, before, strict=True):
        if manifest is None:
            profiles.append({"profile": cache.profile, "codes": 0, "rows": 0})
            failures.append("profile_empty")
            continue
        rows = 0
        for window in cache.iter_windows(manifest, sessions=len(manifest.calendar_dates)):
            rows += len(window.cells)
        profiles.append({
            "profile": cache.profile, "codes": len(manifest.universe_codes), "rows": rows,
            "cutoff": manifest.data_cutoff.isoformat(), "sessions": len(manifest.calendar_dates),
            "snapshot_hash": manifest.snapshot_hash,
        })
    with (
        SqliteSession(path, SqliteConnectionOptions(read_only=True), sqlite_policy=sqlite_policy) as connection,
        SqliteTransaction(connection),
    ):
        CheckSqliteIntegrity(connection)
        mode = str(connection.run("PRAGMA journal_mode").require_one()[0]).lower()
        missing = int(
            connection.run(
                "SELECT COUNT(*) FROM window_binding a LEFT JOIN window_binding b ON a.code=b.code AND a.profile<>b.profile "
                "WHERE b.code IS NULL OR a.source<>b.source OR a.end_date<>b.end_date"
            ).require_one()[0]
        )
        unpaired = int(
            connection.run(
                "SELECT COUNT(*) FROM window_completion c WHERE NOT EXISTS "
                "(SELECT 1 FROM window_binding a JOIN window_binding b ON a.code=b.code "
                "WHERE a.profile='v2' AND b.profile='v3' AND a.code=c.code "
                "AND a.source=c.source AND b.source=c.source AND a.end_date=c.cutoff_date AND b.end_date=c.cutoff_date)"
            ).require_one()[0]
        )
        unconfirmed = int(
            connection.run(
                "SELECT COUNT(*) FROM window_binding a WHERE a.profile='v2' AND NOT EXISTS "
                "(SELECT 1 FROM window_completion c WHERE c.code=a.code AND c.source=a.source AND c.cutoff_date=a.end_date)"
            ).require_one()[0]
        )
    if tuple(cache.manifest() for cache in caches) != before:
        failures.append("publication_changed_during_audit")
    issues = caches[0].price_issues()
    if issues != issues_before:
        failures.append("price_issues_changed_during_audit")
    status = "failed" if failures else "degraded" if issues or missing or unpaired or unconfirmed or mode != "wal" else "passed"
    return {
        "schema_version": "qfq-sqlite-diagnostic",
        "status": status,
        "summary": {
            "mode": "shared_daily_window",
            "source_data_writes": 0,
            "databases": 1,
            "profiles": profiles,
            "journal_mode": mode,
            "paired_identity_mismatches": missing,
            "checkpoint_not_paired": unpaired,
            "database_not_checkpointed": unconfirmed,
            "pending_stocks": len(issues),
            "pending_days": sum(len(issue.dates) for issue in issues),
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
    parser.add_argument("--failures-only", action="store_true")
    parser.add_argument("--runtime-config", type=Path, required=True)
    args = parser.parse_args()
    from trader.bootstrap import BuildSqlitePolicy
    from trader.infra.settings import LoadRuntimeSettings
    report: dict[str, object]
    try:
        settings = LoadRuntimeSettings(args.runtime_config)
        sqlite_policy = BuildSqlitePolicy(settings)
        path = args.database.expanduser().resolve()
        if args.failures_only:
            configuration = LoadDownloadFailureConfiguration(settings.project_root)
            if path.is_symlink() or not path.is_file():
                raise FileNotFoundError("qfq database missing or unsafe")
            with SqliteSession(path, SqliteConnectionOptions(read_only=True), sqlite_policy=sqlite_policy) as connection:
                with SqliteTransaction(connection):
                    failures = ReadDownloadFailures(connection, configuration)
            report = {
                "schema_version": "download-failures-diagnostic", "status": "passed",
                "summary": {"read_only": True, "failure_attempts_are_not_pending_state": True, **failures},
            }
        else:
            report = AuditQfqDatabase(path, sqlite_policy=sqlite_policy)
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        report = {
            "schema_version": "qfq-sqlite-diagnostic",
            "status": "failed",
            "error": ClassifyQfqDiagnosticError(exc),
        }
    emit_report(report)
    return 1 if report["status"] == "failed" else 0


__all__ = ["AuditQfqDatabase", "ClassifyQfqDiagnosticError"]

if __name__ == "__main__":
    raise SystemExit(main())
