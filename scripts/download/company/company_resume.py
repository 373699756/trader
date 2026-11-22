"""Read-only or isolated company audits; live sync has its own foreground action."""

from __future__ import annotations

from scripts.core.paths import PROJECT_ROOT as TOOL_ROOT

from trader.infra.sqlite_policy import SqliteIoPolicy

import argparse
import hashlib
import json
import tempfile
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.download.company.cli import ParseCompanyReplayObservation
from scripts.download.company.company_pagination_audit import AuditCompanyPagination
from scripts.download.company.company_pipeline_audit import AuditCompanyPipeline
from scripts.download.company.company_ocr_audit import AuditCompanyOcr
from scripts.download.company.company_document_download_audit import AuditCompanyDocumentDownloads, AuditCompanyFieldProofRecovery
from scripts.download.company.company_field_audit import InspectCompanyFieldMaterial
from scripts.download.company.output_paths import RequireIsolatedReplay
from scripts.download.company.company_query_audit import AuditCompanyQueries
from scripts.download.company.company_query_audit import AuditCompanyDatabase
from scripts.download.company.company_publication_audit import AuditCompanyConsumer
from scripts.download.company.company_field_proof_audit import AuditCompanyFieldProofs
from trader.bootstrap import OpenExternalHttp
from trader.download.infra.company_pipeline_queries import ReleaseCompletedCompanyQueryRows
from trader.infra.http_contracts import HttpAttempt
from trader.recommendation.infra.settings.loading import LoadStrategySettings
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession, SqliteTransaction

_ROOT = TOOL_ROOT


def _copy(source: Path, destination: Path, *, deadline: float, sqlite_policy: SqliteIoPolicy) -> None:
    with tempfile.NamedTemporaryFile(
        prefix=".company-copy-", suffix=".sqlite3", dir=destination.parent, delete=False
    ) as temporary:
        pending = Path(temporary.name)
    try:
        with SqliteSession(source, SqliteConnectionOptions(read_only=True, timeout_seconds=8), sqlite_policy=sqlite_policy) as original:
            with SqliteSession(pending, SqliteConnectionOptions(timeout_seconds=8), sqlite_policy=sqlite_policy) as replica:
                original.backup_to(replica, deadline=deadline, monotonic=time.monotonic)
        pending.replace(destination)
    finally:
        pending.unlink(missing_ok=True)


def _retained_digest(path: Path, deadline: float, *, sqlite_policy: SqliteIoPolicy) -> str:
    digest = hashlib.sha256()
    with (
        SqliteSession(
            path, SqliteConnectionOptions(read_only=True), cancel_requested=lambda: time.monotonic() >= deadline, sqlite_policy=sqlite_policy
        ) as connection,
        SqliteTransaction(connection),
    ):
        tables = tuple(
            row[0]
            for row in connection.run(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT GLOB 'sqlite_*' "
                "AND name NOT IN ('partition_rows','disclosure_query_rows') ORDER BY name"
            )
        )
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            columns = tuple(connection.run(f"PRAGMA table_info({quoted})"))
            keys = tuple(row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5])
            order = ",".join('"' + key.replace('"', '""') + '"' for key in keys) or "rowid"
            digest.update(table.encode())
            for row in connection.run(f"SELECT * FROM {quoted} ORDER BY {order}"):
                digest.update(repr(tuple(row)).encode())
    return digest.hexdigest()


def _compact_database(source: Path, directory: Path, *, budget_seconds: float, sqlite_policy: SqliteIoPolicy) -> dict[str, object]:
    if not directory.is_absolute() or directory == _ROOT or _ROOT in directory.parents:
        raise ValueError("external absolute directory required")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "company_compacted.sqlite3"
    if target.exists():
        raise ValueError("company_compaction_target_exists")
    deadline = time.monotonic() + budget_seconds
    _copy(source, target, deadline=deadline, sqlite_policy=sqlite_policy)
    before_digest = _retained_digest(target, deadline, sqlite_policy=sqlite_policy)
    before_bytes = target.stat().st_size
    with SqliteSession(target, cancel_requested=lambda: time.monotonic() >= deadline, sqlite_policy=sqlite_policy) as connection:
        with SqliteTransaction(connection, immediate=True):
            ReleaseCompletedCompanyQueryRows(connection)
            removed = connection.total_changes
        connection.run("VACUUM")
        if connection.run("PRAGMA integrity_check").require_one()[0] != "ok":
            raise RuntimeError("company_compaction_integrity_failed")
        if connection.run("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("company_compaction_foreign_key_failed")
    preserved = before_digest == _retained_digest(target, deadline, sqlite_policy=sqlite_policy)
    if not preserved:
        raise RuntimeError("company_compaction_retained_data_changed")
    return {
        "isolated_copy": True,
        "before_bytes": before_bytes,
        "after_bytes": target.stat().st_size,
        "removed_completed_members": removed,
        "retained_data_equal": preserved,
    }


def Main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--facts-database", type=Path)
    parser.add_argument("--risk-baseline", type=Path)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--budget-seconds", type=float)
    parser.add_argument("--config", type=Path, help="Runtime policy for isolated audit modes.")
    audit = parser.add_mutually_exclusive_group()
    audit.add_argument("--audit-only", action="store_true")
    audit.add_argument("--database-only", action="store_true")
    audit.add_argument("--consumer-only", action="store_true")
    parser.add_argument("--consumer-http", action="store_true")
    audit.add_argument("--source-qualification-only", action="store_true")
    audit.add_argument("--field-proof-only", action="store_true")
    audit.add_argument("--field-proof-recovery-only", action="store_true")
    parser.add_argument("--field-proof-recovery-manifest", type=Path)
    audit.add_argument("--field-replay-keys", nargs="+")
    parser.add_argument("--recovered-text", type=Path)
    parser.add_argument("--replay-observed-at", type=ParseCompanyReplayObservation)
    audit.add_argument("--pagination-only", action="store_true")
    audit.add_argument("--pipeline-only", action="store_true")
    audit.add_argument("--pacing-only", action="store_true", help="Compare shared/per-host pacing with identical real PDFs and one complete directory sample in isolation.")
    audit.add_argument("--material-keys", nargs="+", help="Inspect bounded local source excerpts and contract facts for 1..6 source keys; requires --facts-database.")
    audit.add_argument("--ocr-only", action="store_true")
    audit.add_argument("--document-only", action="store_true")
    audit.add_argument("--directory-only", action="store_true")
    args = parser.parse_args()
    if args.directory_only or not any((
        args.audit_only, args.database_only, args.consumer_only, args.field_proof_only, args.source_qualification_only,
        args.field_replay_keys, args.field_proof_recovery_only, args.pagination_only,
        args.pipeline_only, args.pacing_only, args.material_keys,
        args.ocr_only, args.document_only,
    )):
        parser.error("company_structured_source_admission_pending")
    if args.risk_baseline is not None and (
        not args.database_only or args.directory is not None or args.facts_database is None
    ):
        parser.error("--risk-baseline requires read-only --database-only with --facts-database and without --directory")
    if args.budget_seconds is None:
        parser.error("--budget-seconds is required")
    if args.consumer_only and (args.facts_database is None or args.directory is not None):
        parser.error("--consumer-only requires --facts-database and does not accept --directory")
    if args.consumer_http and not args.consumer_only:
        parser.error("--consumer-http requires --consumer-only")
    if args.source_qualification_only and args.directory is not None:
        parser.error("--source-qualification-only does not accept --directory")
    if args.field_proof_only and (args.facts_database is None or args.directory is not None):
        parser.error("--field-proof-only requires --facts-database and does not accept --directory")
    if args.field_proof_recovery_only and (
        args.facts_database is None or args.directory is None or args.field_proof_recovery_manifest is None
    ):
        parser.error("--field-proof-recovery-only requires --facts-database, --field-proof-recovery-manifest and a new --directory")
    if args.field_proof_recovery_manifest is not None and not args.field_proof_recovery_only:
        parser.error("--field-proof-recovery-manifest requires --field-proof-recovery-only")
    if args.database is None and not args.consumer_only:
        parser.error("--database is required")
    from trader.bootstrap import BuildSqlitePolicy
    from trader.infra.settings import LoadRuntimeSettings
    runtime = LoadRuntimeSettings(args.config or TOOL_ROOT / "config/runtime.json")
    if args.directory is not None:
        RequireIsolatedReplay(args.directory, (
            TOOL_ROOT, runtime.project_root, runtime.data_paths.root, runtime.runtime_dir,
        ))
    sqlite_policy = BuildSqlitePolicy(runtime)
    implementation = hashlib.sha256()
    for relative in (
        "scripts/download/company/company_resume.py",
        "scripts/download/company/company_failure_audit.py",
        "scripts/download/company/company_query_audit.py",
        "scripts/download/company/company_publication_audit.py",
        "scripts/download/company/company_field_proof_audit.py",
        "scripts/download/company/company_document_download_audit.py",
        "src/trader/download/infra/company_disclosure_source.py",
        "src/trader/download/infra/company_disclosure_queries.py",
        "src/trader/download/infra/company_document_parser.py",
        "src/trader/download/domain/company_annual_report.py",
        "src/trader/download/domain/company_financial_report.py",
        "src/trader/download/domain/company_announcement_numbers.py",
        "src/trader/download/domain/company_event_extraction.py",
        "src/trader/download/domain/company_reduction_scope.py",
        "src/trader/download/infra/company_annual_field_publication.py",
        "src/trader/download/infra/company_financial_publication.py",
        "src/trader/download/infra/company_material_index.py",
        "src/trader/download/infra/company_normalized_index.py",
        "src/trader/download/infra/company_field_publication.py",
        "src/trader/download/infra/company_field_recovery.py",
        "src/trader/download/infra/company_pipeline_queries.py",
    ):
        implementation.update((_ROOT / relative).read_bytes())
    if not 30 <= args.budget_seconds <= 10800:
        parser.error("budget within 30..10800 seconds required")
    if args.recovered_text is not None and args.field_replay_keys is None:
        parser.error("--recovered-text requires --field-replay-keys")
    if args.replay_observed_at is not None and args.field_replay_keys is None:
        parser.error("--replay-observed-at requires --field-replay-keys")
    if args.source_qualification_only:
        from scripts.download.company.company_source_qualification import AuditCompanySourceQualification
        from trader.download.infra.company_document_files import CompanyDocumentFiles
        from trader.download.infra.download_settings import LoadCompanySyncConfiguration

        paths = runtime.data_paths
        if args.database.resolve() != paths.company_collection.resolve() or (
            args.facts_database is not None and args.facts_database.resolve() != paths.company.resolve()
        ):
            parser.error("--source-qualification-only requires company paths from the configured runtime")
        configuration = LoadCompanySyncConfiguration(runtime.project_root)
        strategy = LoadStrategySettings(runtime.strategy_config_path)
        report = AuditCompanySourceQualification(
            paths,
            documents=CompanyDocumentFiles(
                paths.company_documents,
                max_text_characters=configuration.extended_document_limits.max_text_characters,
                max_bytes=configuration.temporary_text_capacity_bytes,
                reserve_bytes=configuration.temporary_disk_reserve_bytes,
            ),
            sqlite_policy=sqlite_policy,
            policy=strategy.long_research,
            unlock_days=strategy.company_scoring.events.unlock_days,
            deadline=time.monotonic() + args.budget_seconds,
            monotonic=time.monotonic,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.field_replay_keys is not None:
        if args.facts_database is None or args.directory is None:
            parser.error("--field-replay-keys requires --facts-database and a new external --directory")
        from scripts.download.company.company_field_replay import AuditCompanyFieldReplay

        report = AuditCompanyFieldReplay(
            args.database.expanduser().absolute(), args.facts_database.expanduser().absolute(), tuple(args.field_replay_keys), args.directory,
            observed_at=args.replay_observed_at if args.replay_observed_at is not None else datetime.now(ZoneInfo("Asia/Shanghai")), budget_seconds=args.budget_seconds,
            sqlite_policy=sqlite_policy, event_policy=LoadStrategySettings(runtime.strategy_config_path).company_scoring.events,
            protected_roots=(runtime.project_root, runtime.data_paths.root, runtime.runtime_dir),
            recovered_text=args.recovered_text,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.field_proof_recovery_only:
        attempts: list[HttpAttempt] = []
        with OpenExternalHttp(observe=attempts.append) as session:
            report = AuditCompanyFieldProofRecovery(
                args.database.resolve(strict=True), args.facts_database.resolve(strict=True),
                args.field_proof_recovery_manifest, args.directory,
                observed_at=datetime.now(ZoneInfo("Asia/Shanghai")), budget_seconds=args.budget_seconds,
                sqlite_policy=sqlite_policy,
                event_policy=LoadStrategySettings(runtime.strategy_config_path).company_scoring.events,
                session=session, http_attempts=attempts,
            )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report["status"] == "failed" else 0
    if args.field_proof_only:
        report = AuditCompanyFieldProofs(
            args.database.resolve(strict=True), args.facts_database.resolve(strict=True),
            now=datetime.now(ZoneInfo("Asia/Shanghai")), budget_seconds=args.budget_seconds,
            sqlite_policy=sqlite_policy,
            event_policy=LoadStrategySettings(runtime.strategy_config_path).company_scoring.events,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report["status"] == "failed" else 0
    if args.consumer_only:
        if args.consumer_http and args.facts_database.resolve(strict=True) != runtime.data_paths.company.resolve(strict=True):
            parser.error("--consumer-http requires the configured formal company database")
        report = AuditCompanyConsumer(
            args.facts_database.resolve(strict=True),
            now=datetime.now(ZoneInfo("Asia/Shanghai")),
            budget_seconds=min(180.0, args.budget_seconds),
            sqlite_policy=sqlite_policy,
            event_policy=LoadStrategySettings(runtime.strategy_config_path).company_scoring.events,
            http_configuration=runtime.config_path if args.consumer_http else None,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report["status"] == "failed" else 0
    source_path = args.database.resolve(strict=True)
    if args.document_only:
        report = AuditCompanyDocumentDownloads(
            source_path, budget_seconds=min(150.0, args.budget_seconds), sqlite_policy=sqlite_policy,
            directory=args.directory,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.material_keys is not None:
        if args.facts_database is None:
            parser.error("--material-keys requires --facts-database")
        print(json.dumps(InspectCompanyFieldMaterial(
            source_path, args.facts_database.resolve(strict=True), tuple(args.material_keys), sqlite_policy=sqlite_policy,
            observed_at=datetime.now(ZoneInfo("Asia/Shanghai")),
            event_policy=LoadStrategySettings(runtime.strategy_config_path).company_scoring.events,
        ), ensure_ascii=False, indent=2))
        return 0
    if args.database_only:
        report = AuditCompanyDatabase(
            source_path,
            now=datetime.now(ZoneInfo("Asia/Shanghai")),
            budget_seconds=min(180.0, args.budget_seconds),
            facts_path=args.facts_database, sqlite_policy=sqlite_policy,
            risk_baseline=args.risk_baseline,
            event_policy=LoadStrategySettings(runtime.strategy_config_path).company_scoring.events,
        )
        if args.directory is not None and report["status"] != "failed":
            summary = report["summary"]
            if not isinstance(summary, dict):
                raise ValueError("company_database_summary_invalid")
            summary["compaction"] = _compact_database(
                source_path, args.directory.resolve(), budget_seconds=args.budget_seconds, sqlite_policy=sqlite_policy
            )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report["status"] == "failed" else 0
    if args.pagination_only:
        print(
            json.dumps(
                AuditCompanyPagination(source_path, budget_seconds=min(300.0, args.budget_seconds), sqlite_policy=sqlite_policy), ensure_ascii=False, indent=2
            )
        )
        return 0
    if args.audit_only:
        report = AuditCompanyQueries(source_path, budget_seconds=min(300.0, args.budget_seconds), sqlite_policy=sqlite_policy)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.directory is None:
        parser.error("replay requires an external absolute directory")
    directory = args.directory.resolve()
    if not args.directory.is_absolute() or directory == _ROOT or _ROOT in directory.parents:
        parser.error("external absolute directory required")
    directory.mkdir(parents=True, exist_ok=True)
    if args.ocr_only:
        report = AuditCompanyOcr(source_path, directory, budget_seconds=min(1800.0, args.budget_seconds), sqlite_policy=sqlite_policy)
        (directory / "ocr-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.pipeline_only or args.pacing_only:
        print(
            json.dumps(
                AuditCompanyPipeline(source_path, directory, budget_seconds=args.budget_seconds, sqlite_policy=sqlite_policy,
                                     compare_pacing=args.pacing_only), ensure_ascii=False, indent=2
            )
        )
        return 0
    raise ValueError("company_structured_source_admission_pending")
