"""Read-only verification of published history and durable download receipts."""

from __future__ import annotations

import argparse
import sqlite3
from itertools import groupby
from pathlib import Path

from trader.download.infra.history_control_index import HistoryControlIndex
from trader.download.infra.history_prices import HistoryPricePartition
from trader.download.infra.history_sync_failure import HistoryFailureDetail
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.infra.sqlite_codec import DecodeDate, DecodeHash
from trader.infra.sqlite_connection import (
    CheckSqliteIntegrity,
    SqliteConnectionOptions,
    SqliteSession,
    SqliteTransaction,
)
from trader.infra.sqlite_policy import SqliteIoPolicy

from .reporting import emit_report


def _receipts(path: Path, *, sqlite_policy: SqliteIoPolicy) -> dict[str, object]:
    with (
        SqliteSession(path, SqliteConnectionOptions(read_only=True), sqlite_policy=sqlite_policy) as connection,
        SqliteTransaction(connection),
    ):
        batch = connection.run(
            "SELECT sync_sequence,cutoff_date,source_hash,calendar_hash,universe_hash FROM download_batch"
        ).fetchone()
        gaps = connection.run("SELECT COUNT(*),COUNT(DISTINCT code) FROM download_gaps").require_one()
        tasks = connection.run(
            "SELECT COUNT(*),COALESCE(SUM(tencent_done=0),0) FROM download_tasks"
        ).require_one()
        task_members = CanonicalDigest(tuple(row[0] for row in connection.run("SELECT code FROM download_tasks ORDER BY code")))
        candidates = connection.run("SELECT COUNT(*) FROM price_replacement").require_one()[0]
        unresolved = connection.run(
            "SELECT COUNT(*) FROM price_repair_attempt WHERE state<>'resolved'"
        ).require_one()[0]
        attempts = [
            {"state": row[0], "days": row[1], "stocks": row[2]}
            for row in connection.run(
                "SELECT state,COUNT(*),COUNT(DISTINCT code) FROM price_repair_attempt GROUP BY state ORDER BY state"
            )
        ]
    return {
        "batch_sequence": batch[0] if batch else None,
        "batch_cutoff": DecodeDate(batch[1]).isoformat() if batch else None,
        "batch_source_hash": DecodeHash(batch[2]) if batch else None,
        "batch_calendar_hash": DecodeHash(batch[3]) if batch else None,
        "batch_universe_hash": DecodeHash(batch[4]) if batch else None,
        "gap_days": gaps[0],
        "gap_stocks": gaps[1],
        "tasks": tasks[0],
        "task_members_hash": task_members,
        "unfinished_tasks": tasks[1],
        "replacement_rows": candidates,
        "unresolved_repair_days": unresolved,
        "repair_attempts": attempts,
    }


def AuditHistoryPublication(root: Path, *, sqlite_policy: SqliteIoPolicy) -> dict[str, object]:
    """Verify every published month using the production codec, without repair or initialization."""
    path = root / "catalog.sqlite3"
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError("history_catalog_unavailable")
    control = HistoryControlIndex(path, sqlite_policy=sqlite_policy)
    if control.integrity().state != "healthy":
        raise RuntimeError("history_catalog_invalid")
    before = control.load_published_state()
    if before is None:
        raise RuntimeError("history_publication_missing")
    receipts = _receipts(path, sqlite_policy=sqlite_policy)
    snapshot = before.snapshot
    checked_paths: set[Path] = set()
    rows = 0
    published_gaps = 0
    codes = frozenset(security.code for security in before.universe.securities)
    dates = frozenset(before.calendar.open_dates)
    for relative_path, group in groupby(snapshot.partitions, key=lambda item: item.relative_path):
        partitions = tuple(group)
        price_path = root / relative_path
        if price_path.is_symlink() or not price_path.is_file():
            raise FileNotFoundError("history_partition_unavailable")
        if price_path not in checked_paths:
            with SqliteSession(
                price_path, SqliteConnectionOptions(read_only=True), sqlite_policy=sqlite_policy
            ) as connection:
                CheckSqliteIntegrity(connection)
            checked_paths.add(price_path)
        HistoryPricePartition.verify_months(price_path, partitions, sqlite_policy=sqlite_policy)
        for partition in partitions:
            monthly = HistoryPricePartition(
                price_path, partition.calendar_year, partition.calendar_month, sqlite_policy=sqlite_policy
            )
            published_gaps += sum(
                code in codes and day in dates for code, day in monthly.missing_price_dates(partition.sequence)
            )
            rows += partition.row_count
    failures: list[str] = []
    if control.load_published_state() != before or _receipts(path, sqlite_policy=sqlite_policy) != receipts:
        failures.append("history_changed_during_audit")
    pending: list[str] = []
    if receipts["gap_days"]:
        pending.append("history_price_gaps")
    if published_gaps:
        pending.append("history_published_price_gaps")
    if receipts["unfinished_tasks"]:
        pending.append("history_tasks_unfinished")
    if receipts["replacement_rows"]:
        pending.append("history_replacement_pending")
    if receipts["unresolved_repair_days"]:
        pending.append("history_repair_unresolved")
    if (
        receipts["batch_sequence"] != snapshot.sequence
        or receipts["batch_cutoff"] != snapshot.data_cutoff.isoformat()
        or receipts["batch_source_hash"] != snapshot.source_identity_hash
        or receipts["batch_calendar_hash"] != snapshot.calendar_hash
        or receipts["batch_universe_hash"] != snapshot.universe_hash
    ):
        pending.append("history_batch_publication_mismatch")
    if receipts["task_members_hash"] != CanonicalDigest(tuple(sorted(codes))):
        pending.append("history_task_membership_mismatch")
    return {
        "schema_version": "history-publication-audit",
        "status": "failed" if failures else "degraded" if pending else "passed",
        "summary": {
            "read_only": True,
            "source_data_writes": 0,
            "network_requests": 0,
            "snapshot_hash": snapshot.content_hash,
            "sequence": snapshot.sequence,
            "data_cutoff": snapshot.data_cutoff.isoformat(),
            "sessions": len(before.calendar.open_dates),
            "stocks": len(before.universe.securities),
            "annual_databases": len(checked_paths),
            "verified_months": len(snapshot.partitions),
            "verified_rows": rows,
            "published_gap_days": published_gaps,
            "receipts": receipts,
            "failures": failures,
            "pending": pending,
            "scope": "published_content_and_receipts_not_supplier_completeness",
        },
    }


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-root", type=Path, required=True)
    parser.add_argument("--runtime-config", type=Path, required=True)
    args = parser.parse_args()
    from trader.bootstrap import BuildSqlitePolicy
    from trader.infra.settings import LoadRuntimeSettings

    policy = BuildSqlitePolicy(LoadRuntimeSettings(args.runtime_config))
    try:
        report = AuditHistoryPublication(args.history_root, sqlite_policy=policy)
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        report = {
            "schema_version": "history-publication-audit",
            "status": "failed",
            "summary": {
                "failure": "history_publication_audit_failed", "category": type(exc).__name__,
                "diagnostic": HistoryFailureDetail(exc),
            },
        }
    emit_report(report)
    return 1 if report["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(_main())
