"""Read-only integrity and paired-publication audit of the shared daily window."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy

import argparse
import re
import sqlite3
import signal
from pathlib import Path

from trader.download.infra.qfq_sqlite import SQLiteQfqWindowCache
from trader.download.infra.download_failure_diagnostics import ReadDownloadFailures
from trader.download.infra.download_settings import LoadDownloadFailureConfiguration
from trader.infra.sqlite_codec import DecodeDate
from trader.infra.sqlite_publication import SqlitePublicationError
from trader.infra.sqlite_connection import (
    CheckSqliteIntegrity,
    SqliteConnectionOptions,
    SqliteSession,
    SqliteTransaction,
)

from scripts.core.reporting import emit_report
from scripts.download.history.history_publication_audit import (
    AuditPriceFields,
    CapturePriceAuditSources,
    LoadPriceAuditReference,
    PriceAuditBudget,
    PriceAuditInterrupted,
    PriceAuditPlan,
    PriceAuditUnit,
)
from trader.infra.serialization.canonical_codec import CanonicalDigest


def AuditQfqDatabase(path: Path, *, sqlite_policy: SqliteIoPolicy) -> dict[str, object]:
    """Report integrity and bounded missing-price dates without recovery writes."""
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
        retained_codes = int(connection.run(
            "SELECT COUNT(DISTINCT b.code) FROM window_binding b WHERE NOT EXISTS "
            "(SELECT 1 FROM window_population p WHERE p.profile=b.profile AND p.code=b.code)"
        ).require_one()[0])
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
        latest_without_prices: list[dict[str, object]] = []
        nontrading_available = connection.run(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='price_nontrading'"
        ).fetchone() is not None
        for code, start, end, last_price in connection.run(
            "SELECT a.code,a.start_date,a.end_date,"
            "(SELECT MAX(d.trade_date) FROM daily_price d WHERE d.code=a.code "
            "AND d.trade_date BETWEEN a.start_date AND a.end_date) "
            "FROM window_binding a WHERE a.profile='v2' AND NOT EXISTS "
            "(SELECT 1 FROM daily_price d WHERE d.code=a.code AND d.trade_date=a.end_date) "
            "ORDER BY a.code LIMIT 50"
        ).fetchall():
            nontrading = tuple(
                DecodeDate(row[0]).isoformat() for row in connection.run(
                    "SELECT trade_date FROM price_nontrading WHERE code=? AND trade_date BETWEEN ? AND ? "
                    "ORDER BY trade_date", (code, start, end),
                )
            ) if nontrading_available else ()
            latest_without_prices.append({
                "code": code,
                "cutoff": DecodeDate(end).isoformat(),
                "last_price_date": DecodeDate(last_price).isoformat() if last_price is not None else None,
                "nontrading_dates": nontrading,
                "nontrading_is_not_exchange_confirmation": True,
            })
    if tuple(cache.manifest() for cache in caches) != before:
        failures.append("publication_changed_during_audit")
    issues = caches[0].price_issues()
    published_codes = frozenset(code for manifest in before if manifest is not None for code in manifest.universe_codes)
    published_pending = sum(issue.code in published_codes for issue in issues)
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
            "published_pending_stocks": published_pending,
            "unpublished_pending_stocks": len(issues) - published_pending,
            "retained_binding_codes": retained_codes,
            "latest_without_prices": latest_without_prices,
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
    if isinstance(exc, SqlitePublicationError) and re.fullmatch(r"sqlite_publication_[a-z_]{1,64}", str(exc)):
        return str(exc)
    return "qfq_diagnostic_failed"


def AuditQfqPriceFields(
    path: Path,
    *,
    reference_database: Path,
    sqlite_policy: SqliteIoPolicy,
    budget: PriceAuditBudget,
    checkpoint: Path | None = None,
    configuration: tuple[Path, ...] = (),
) -> dict[str, object]:
    """Scan both published price windows using explicit local listing/calendar facts."""
    metadata_sources = CapturePriceAuditSources((path, reference_database), budget)
    securities, reference_calendar = LoadPriceAuditReference(reference_database, None, budget, sqlite_policy)
    units: list[PriceAuditUnit] = []
    identities: list[tuple[object, ...]] = []
    with budget.connection(path, sqlite_policy) as connection:
        for profile in ("v2", "v3"):
            calendar = tuple(int(row[0]) for row in connection.execute(
                "SELECT trade_date FROM window_calendar WHERE profile=? ORDER BY trade_date", (profile,),
            ))
            members = tuple(str(row[0]) for row in connection.execute(
                "SELECT code FROM window_population WHERE profile=? ORDER BY code", (profile,),
            ))
            bindings = tuple(tuple(row) for row in connection.execute(
                "SELECT b.code,b.source,hex(b.content_hash),b.start_date,b.end_date "
                "FROM window_binding b JOIN window_population p ON p.profile=b.profile AND p.code=b.code "
                "WHERE b.profile=? ORDER BY b.code", (profile,),
            ))
            if not calendar or len(calendar) > 2000 or not members or len(members) > 12000 or len(bindings) != len(members):
                raise ValueError("price_audit_qfq_members_or_calendar_invalid")
            if calendar != tuple(day for day in reference_calendar if day <= calendar[-1])[-len(calendar):]:
                raise ValueError("price_audit_qfq_reference_calendar_mismatch")
            member_codes = frozenset(members)
            selected = tuple(security for security in securities if security.code in member_codes)
            if tuple(security.code for security in selected) != members:
                raise ValueError("price_audit_qfq_listing_reference_missing")
            if any(int(binding[3]) != calendar[0] or int(binding[4]) != calendar[-1] for binding in bindings):
                raise ValueError("price_audit_qfq_binding_window_mismatch")
            units.append(PriceAuditUnit(
                profile, path, calendar, selected, profile=profile,
                sources=tuple(str(binding[1]) for binding in bindings),
                binding_hashes=tuple(str(binding[2]).lower() for binding in bindings),
            ))
            identities.extend(bindings)
            publication = connection.execute("SELECT publication_id FROM window_publication WHERE profile=?", (profile,)).fetchone()
            if publication is None:
                raise ValueError("price_audit_qfq_publication_missing")
            identities.append((profile, str(publication[0]), calendar, members))
        mismatches = int(connection.execute(
            "SELECT COUNT(*) FROM window_binding a JOIN window_population p ON p.profile=a.profile AND p.code=a.code "
            "LEFT JOIN window_binding b ON b.code=a.code AND b.profile='v3' "
            "LEFT JOIN window_completion c ON c.code=a.code WHERE a.profile='v2' AND "
            "(b.code IS NULL OR c.code IS NULL OR a.source<>b.source OR a.end_date<>b.end_date "
            "OR a.source<>c.source OR a.end_date<>c.cutoff_date)"
        ).fetchone()[0])
    plan = PriceAuditPlan(
        CanonicalDigest(tuple(identities)), tuple(units), ((path, True), (reference_database, False)),
        cutoff=DecodeDate(units[0].dates[-1]).isoformat(), metadata_integrity=True, metadata_sources=metadata_sources,
    )
    fields = AuditPriceFields(plan, budget=budget, sqlite_policy=sqlite_policy, checkpoint=checkpoint, configuration=configuration)
    status = "failed" if fields["status"] == "failed" else "degraded" if mismatches else fields["status"]
    return {"schema_version": "qfq-sqlite-diagnostic", "status": status, "summary": {
        "mode": "shared_daily_window", "source_data_writes": 0, "databases": 1,
        "paired_identity_mismatches": mismatches,
        "profiles": [{"profile": unit.name, "codes": len(unit.securities), "sessions": len(unit.dates),
                      "cutoff": DecodeDate(unit.dates[-1]).isoformat()} for unit in units],
        "field_audit": fields,
    }}


def Main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--failures-only", action="store_true")
    parser.add_argument("--runtime-config", type=Path, required=True)
    parser.add_argument("--price-field-audit", action="store_true")
    parser.add_argument("--audit-checkpoint", type=Path)
    parser.add_argument("--audit-budget-seconds", type=float, default=170.0)
    args = parser.parse_args()
    from trader.bootstrap import BuildSqlitePolicy
    from trader.infra.settings import LoadRuntimeSettings
    report: dict[str, object]
    stopped = False
    def cancel(_signal: int, _frame: object) -> None:
        nonlocal stopped
        stopped = True
    previous_signal = signal.signal(signal.SIGINT, cancel) if args.price_field_audit else None
    budget = PriceAuditBudget(args.audit_budget_seconds, cancelled=lambda: stopped)
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
        elif args.price_field_audit:
            report = AuditQfqPriceFields(
                path, reference_database=settings.data_paths.reference_database, sqlite_policy=sqlite_policy,
                budget=budget, checkpoint=args.audit_checkpoint,
                configuration=(args.runtime_config, args.runtime_config.parent / "download.json"),
            )
        else:
            report = AuditQfqDatabase(path, sqlite_policy=sqlite_policy)
    except PriceAuditInterrupted as exc:
        report = {"schema_version": "qfq-sqlite-diagnostic", "status": "failed", "summary": {
            "field_audit": {"complete": False, "failure": "price_audit_" + str(exc), "phase": budget.phase},
        }}
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        report = {
            "schema_version": "qfq-sqlite-diagnostic",
            "status": "failed",
            "error": ClassifyQfqDiagnosticError(exc),
        }
        if args.price_field_audit:
            report["summary"] = {"field_audit": {
                "complete": False, "phase": budget.phase, "failure": ClassifyQfqDiagnosticError(exc),
                "category": type(exc).__name__, "units": [], "actual_selected_rows": 0,
            }}
    finally:
        if previous_signal is not None:
            signal.signal(signal.SIGINT, previous_signal)
    emit_report(report)
    return 1 if report["status"] == "failed" else 0


__all__ = ["AuditQfqDatabase", "AuditQfqPriceFields", "ClassifyQfqDiagnosticError"]
