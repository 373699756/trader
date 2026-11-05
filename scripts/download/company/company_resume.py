"""Read-only or isolated company audits; live sync has its own foreground action."""

from __future__ import annotations

from scripts.core.paths import PROJECT_ROOT as TOOL_ROOT

from trader.infra.sqlite_policy import SqliteIoPolicy

import argparse
import hashlib
import json
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from scripts.download.company.company_pagination_audit import AuditCompanyPagination
from scripts.download.company.company_pipeline_audit import AuditCompanyPipeline
from scripts.download.company.company_pipeline_audit import AuditCompanyIoCandidates
from scripts.download.company.company_ocr_audit import AuditCompanyOcr
from scripts.download.company.company_document_download_audit import AuditCompanyDocumentDownloads
from scripts.download.company.company_field_audit import InspectCompanyFieldMaterial
from scripts.download.company.output_paths import RequireIsolatedReplay
from scripts.download.company.company_query_audit import AuditCompanyQueries
from scripts.download.company.company_query_audit import AuditCompanyDatabase
from trader.bootstrap import OpenExternalHttp
from trader.download.application.company_evidence import CompanyEvidenceDependencies, SyncCompanyEvidence
from trader.download.domain.company_events import CompanyEventPolicy
from trader.download.domain.company_evidence import (
    BuildCompanyDocumentPolicies,
    CompanyDocumentPolicies,
    CompanyDocumentTask,
    CompanyDocumentWorkload,
    CompanyEvidenceRequest,
    CompanySyncConfiguration,
    Disclosure,
    DisclosurePage,
    DisclosurePartition,
    Market,
)
from trader.download.infra.company_disclosure_source import CninfoMarketDisclosureSource
from trader.download.infra.company_document_files import CompanyDocumentFiles
from trader.download.infra.company_document_ocr import CompanyOcrEngine, DetectCompanyOcr
from trader.download.infra.company_document_parser import CompanyTextCapability, DetectCompanyText
from trader.download.infra.company_raw_files import CompanyRawFiles
from trader.download.infra.company_evidence_index import SQLiteCompanyEvidenceIndex
from trader.download.infra.company_pipeline_queries import ReleaseCompletedCompanyQueryRows
from trader.download.infra.company_structured_source import EastmoneyCompanySource
from trader.download.infra.company_fact_inputs import CompanyCoverageComplete
from trader.download.infra.download_settings import LoadCompanySyncConfiguration
from trader.infra.http_contracts import HttpTransport
from trader.infra.settings import LoadStrategySettings
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession, SqliteTransaction

_ROOT = TOOL_ROOT


class _ReplayIndex(SQLiteCompanyEvidenceIndex):
    def __init__(
        self,
        path: Path,
        selection: frozenset[tuple[str, str, str]],
        configuration: CompanySyncConfiguration,
        *,
        facts_path: Path,
        event_policy: CompanyEventPolicy,
        sqlite_policy: SqliteIoPolicy,
        raw_files: CompanyRawFiles,
        directory_only: bool = False,
    ) -> None:
        super().__init__(
            path,
            facts_path=facts_path,
            raw_files=raw_files,
            documents=CompanyDocumentFiles(
                path.parent / "text",
                max_text_characters=configuration.extended_document_limits.max_text_characters,
                max_bytes=configuration.temporary_text_capacity_bytes,
                reserve_bytes=configuration.temporary_disk_reserve_bytes,
            ),
            event_policy=event_policy,
            sqlite_policy=sqlite_policy,
        )
        self._selection = selection
        self._directory_only = directory_only

    def partitions(self, markets: Sequence[Market], observed_at: datetime) -> tuple[DisclosurePartition, ...]:
        return tuple(
            p
            for p in super().partitions(markets, observed_at)
            if any(p.market == market and start <= str(p.start) <= str(p.end) <= end for market, start, end in self._selection)
        )

    def component_due(self, component: str, scope: str, observed_at: datetime) -> bool:
        # This diagnostic measures announcement recovery, not another full structured-source download.
        return False

    def pending_documents(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        limit: int | None = None,
        policies: CompanyDocumentPolicies,
        excluded: Sequence[tuple[str, str, str]] = (),
        repair: Literal["ocr", "limits"] | None = None,
        local_only: bool = False,
    ) -> tuple[CompanyDocumentTask, ...]:
        if self._directory_only:
            return ()
        return tuple(super().pending_documents(
            codes,
            observed_at,
            limit=min(limit or 32, 32),
            policies=policies,
            excluded=excluded,
            repair=repair,
            local_only=local_only,
        ))

    def document_workload(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        policies: CompanyDocumentPolicies,
        *,
        excluded: Sequence[tuple[str, str, str]] = (),
        repair: Literal["ocr", "limits"] | None = None,
    ) -> CompanyDocumentWorkload:
        if self._directory_only:
            return CompanyDocumentWorkload(0, 0)
        return super().document_workload(codes, observed_at, policies, excluded=excluded, repair=repair)


class _ObservedSource(CninfoMarketDisclosureSource):
    def __init__(
        self, session: HttpTransport, configuration: CompanySyncConfiguration, *, cancel_requested: Callable[[], bool],
        raw_files: CompanyRawFiles, engine: CompanyOcrEngine, policies: CompanyDocumentPolicies, text_capability: CompanyTextCapability,
    ) -> None:
        super().__init__(
            session,
            monotonic=time.monotonic,
            sleeper=time.sleep,
            timeout_seconds=configuration.source_timeout_seconds,
            document_download_timeout_seconds=configuration.document_download_timeout_seconds,
            document_parse_timeout_seconds=configuration.document_parse_timeout_seconds,
            document_limits=configuration.document_limits,
            extended_document_limits=configuration.extended_document_limits,
            request_interval_seconds=configuration.request_interval_seconds,
            cancel_requested=cancel_requested,
            raw_files=raw_files,
            ocr_engine=engine,
            document_policies=policies,
            text_capability=text_capability,
            text_reserve_bytes=configuration.text_task_reserve_bytes,
            ocr_reserve_bytes=configuration.ocr_task_reserve_bytes,
        )
        self.pages: list[tuple[str, str, str, str, int]] = []
        self.documents = 0

    def fetch(self, partition: DisclosurePartition, *, order: Literal["", "asc", "desc"] = "") -> DisclosurePage:
        page = super().fetch(partition, order=order)
        self.pages.append(
            (
                partition.market,
                str(partition.start),
                str(partition.end),
                partition.query.key if partition.query else "",
                partition.page,
            )
        )
        return page

    def download_document(self, item: Disclosure) -> None:
        self.documents += 1
        super().download_document(item)


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


def _saved_pages(path: Path, pages: list[tuple[str, str, str, str, int]], *, sqlite_policy: SqliteIoPolicy) -> set[tuple[str, str, str, str, int]]:
    saved = set()
    with SqliteSession(path, SqliteConnectionOptions(read_only=True), sqlite_policy=sqlite_policy) as c:
        for market, start, end, scope, page in pages:
            if scope:
                row = c.run(
                    "SELECT next_page FROM disclosure_queries WHERE market=? AND start_date=? AND end_date=? AND scope=?",
                    (market, start, end, scope),
                ).fetchone()
            else:
                row = c.run(
                    "SELECT next_page FROM partitions p WHERE market=? AND start_date=? AND end_date=? AND NOT EXISTS(SELECT 1 FROM disclosure_queries q WHERE q.market=p.market AND q.start_date=p.start_date AND q.end_date=p.end_date)",
                    (market, start, end),
                ).fetchone()
            if row is not None and row[0] > page:
                saved.add((market, start, end, scope, page))
    return saved


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
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--budget-seconds", type=float)
    parser.add_argument("--config", type=Path, help="Runtime policy for isolated audit modes.")
    audit = parser.add_mutually_exclusive_group()
    audit.add_argument("--audit-only", action="store_true")
    audit.add_argument("--database-only", action="store_true")
    audit.add_argument("--pagination-only", action="store_true")
    audit.add_argument("--pipeline-only", action="store_true")
    audit.add_argument("--pacing-only", action="store_true", help="Compare shared/per-host pacing with identical real PDFs and one complete directory sample in isolation.")
    audit.add_argument("--io-candidates-only", action="store_true", help="Five paired isolated samples per 15/30/60-second publication candidate; never enables production changes.")
    audit.add_argument("--material-keys", nargs="+", help="Inspect bounded local source excerpts and contract facts for 1..6 source keys; requires --facts-database.")
    audit.add_argument("--ocr-only", action="store_true")
    audit.add_argument("--document-only", action="store_true")
    audit.add_argument("--directory-only", action="store_true")
    args = parser.parse_args()
    if args.database is None or args.budget_seconds is None:
        parser.error("--database and --budget-seconds are required")
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
        "src/trader/download/application/company_evidence.py",
        "src/trader/download/infra/company_disclosure_source.py",
        "src/trader/download/infra/company_disclosure_queries.py",
        "src/trader/download/infra/company_evidence_index.py",
        "src/trader/download/infra/company_document_parser.py",
    ):
        implementation.update((_ROOT / relative).read_bytes())
    source_path = args.database.resolve(strict=True)
    if not 30 <= args.budget_seconds <= 10800:
        parser.error("budget within 30..10800 seconds required")
    if args.document_only:
        report = AuditCompanyDocumentDownloads(source_path, budget_seconds=min(150.0, args.budget_seconds), sqlite_policy=sqlite_policy)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.material_keys is not None:
        if args.facts_database is None:
            parser.error("--material-keys requires --facts-database")
        print(json.dumps(InspectCompanyFieldMaterial(
            source_path, args.facts_database.resolve(strict=True), tuple(args.material_keys), sqlite_policy=sqlite_policy,
        ), ensure_ascii=False, indent=2))
        return 0
    if args.database_only:
        report = AuditCompanyDatabase(
            source_path,
            now=datetime.now(ZoneInfo("Asia/Shanghai")),
            budget_seconds=min(180.0, args.budget_seconds),
            facts_path=args.facts_database, sqlite_policy=sqlite_policy,
        )
        if args.directory is not None:
            summary = report["summary"]
            if not isinstance(summary, dict):
                raise ValueError("company_database_summary_invalid")
            summary["compaction"] = _compact_database(
                source_path, args.directory.resolve(), budget_seconds=args.budget_seconds, sqlite_policy=sqlite_policy
            )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
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
    if args.io_candidates_only:
        print(json.dumps(AuditCompanyIoCandidates(
            source_path,
            directory,
            budget_seconds=args.budget_seconds,
            sqlite_policy=sqlite_policy,
        ), ensure_ascii=False, indent=2))
        return 0
    destination = directory / "evidence.sqlite3"
    if destination.resolve() == source_path or (destination.exists() and destination.samefile(source_path)):
        parser.error("source and replay database must differ")
    started = time.monotonic()
    if not destination.exists():
        _copy(source_path, destination, deadline=started + args.budget_seconds, sqlite_policy=sqlite_policy)
    selection_path = directory / "selection.json"
    with SqliteSession(destination, SqliteConnectionOptions(read_only=True), sqlite_policy=sqlite_policy) as c:
        codes = tuple(row[0] for row in c.run("SELECT code FROM company_population ORDER BY code"))
        if selection_path.exists():
            selection = _selection(json.loads(selection_path.read_text(encoding="utf-8")))
        else:
            selection = frozenset(
                tuple(row)
                for row in c.run(
                    "SELECT market,start_date,end_date FROM partitions WHERE is_complete=0 AND (failure<>'' OR next_page>1)"
                )
            )
            selection_path.write_text(json.dumps(sorted(selection)), encoding="utf-8")
    if not selection or not codes:
        parser.error("backup needs failed slices and a published population")
    if args.facts_database is None:
        parser.error("replay requires --facts-database")
    facts_path = directory / "company_facts.sqlite3"
    if not facts_path.exists():
        _copy(args.facts_database, facts_path, deadline=started + args.budget_seconds, sqlite_policy=sqlite_policy)
    configuration = replace(LoadCompanySyncConfiguration(_ROOT), document_workers=1)
    raw_files = CompanyRawFiles(directory, capacity=configuration.raw_capacity_bytes,
                               analysis_reserve=configuration.raw_analysis_reserve_bytes,
                               free_reserve=configuration.temporary_disk_reserve_bytes)
    ocr_capability = DetectCompanyOcr(configuration.document_ocr_languages)
    text_capability = DetectCompanyText()
    policies = BuildCompanyDocumentPolicies(configuration, ocr_capability.identity, text_identity=text_capability.identity)
    engine = CompanyOcrEngine(ocr_capability, timeout_seconds=configuration.document_ocr_timeout_seconds,
                              page_timeout_seconds=configuration.document_ocr_page_timeout_seconds,
                              max_pages=configuration.document_ocr_max_pages, dpi=configuration.document_ocr_dpi,
                              cancel_requested=lambda: time.monotonic() >= started + args.budget_seconds)
    event_policy = LoadStrategySettings(_ROOT / "config" / "strategy.json").company_scoring.events
    index = _ReplayIndex(
        destination,
        selection,
        configuration,
        facts_path=facts_path,
        raw_files=raw_files,
        event_policy=event_policy, sqlite_policy=sqlite_policy,
        directory_only=args.directory_only,
    )
    if args.directory_only:
        with SqliteSession(destination, sqlite_policy=sqlite_policy) as c, SqliteTransaction(c, immediate=True):
            for key in selection:
                c.run("DELETE FROM partition_retries WHERE market=? AND start_date=? AND end_date=?", key)
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    runs = []
    saved: set[tuple[str, str, str, str, int]] = set()
    replayed = 0
    for number in range(3):
        remaining = args.budget_seconds - (time.monotonic() - started)
        if remaining < 15:
            break
        with OpenExternalHttp() as session:
            source = _ObservedSource(
                session, configuration, cancel_requested=lambda: time.monotonic() >= started + args.budget_seconds,
                raw_files=raw_files, engine=engine, policies=policies, text_capability=text_capability,
            )

            def interrupt(number: int = number, source: _ObservedSource = source) -> bool:
                return time.monotonic() >= started + args.budget_seconds or (number == 0 and len(source.pages) >= 2)

            result = SyncCompanyEvidence(
                CompanyEvidenceDependencies(
                    index,
                    source,
                    EastmoneyCompanySource(
                        session,
                        monotonic=time.monotonic,
                        timeout_seconds=configuration.source_timeout_seconds,
                        cancel_requested=interrupt,
                    ),
                    time.monotonic,
                    cancel_requested=interrupt,
                    ocr_identity=ocr_capability.identity,
                    text_identity=text_capability.identity,
                ),
                CompanyEvidenceRequest(codes, now, configuration, event_policy),
            )
            if number == 0:
                saved = _saved_pages(destination, source.pages, sqlite_policy=sqlite_policy)
            else:
                replayed += len(saved.intersection(source.pages))
            runs.append(
                {
                    "interrupted": "task:cancelled" in result.failures,
                    "pages": len(source.pages),
                    "document_attempts": source.documents,
                    "pending_slices": result.pending_partitions,
                    "sampled_pending_documents": result.pending_documents,
                    "failures": result.failures,
                    "elapsed_seconds": round(result.elapsed_seconds, 3),
                }
            )
        time.sleep(configuration.request_interval_seconds)
    with SqliteSession(destination, SqliteConnectionOptions(read_only=True), sqlite_policy=sqlite_policy) as c:
        completed = sum(
            not c.run("SELECT 1 FROM partitions WHERE market=? AND start_date>=? AND end_date<=? AND is_complete=0 LIMIT 1", key).fetchone()
            for key in selection
        )
        covered = sum(
            CompanyCoverageComplete(tuple(c.run("SELECT start_date,end_date FROM coverage WHERE market=?", (key[0],))), date.fromisoformat(key[1]), date.fromisoformat(key[2]))
            for key in selection
        )
    passed = completed == covered == len(selection) and (args.directory_only or replayed == 0)
    report = {
        "schema_version": "company-resume",
        "status": "passed" if passed else "degraded",
        "summary": {
            "isolated_backup": True,
            "implementation_digest": implementation.hexdigest(),
            "selected_slices": len(selection),
            "completed_slices": completed,
            "covered_slices": covered,
            "saved_pages_replayed": replayed,
            "document_sample_limit": 32,
            "directory_only": args.directory_only,
            "runs": runs,
        },
        "findings": [] if passed else ["company_resume_incomplete"],
    }
    (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def _selection(payload: object) -> frozenset[tuple[str, str, str]]:
    if not isinstance(payload, list):
        raise ValueError("company_replay_selection_invalid")
    keys = []
    for row in payload:
        if not isinstance(row, list) or len(row) != 3 or not all(isinstance(s, str) for s in row):
            raise ValueError("company_replay_selection_invalid")
        if row[0] not in {"sse", "szse", "bse"}:
            raise ValueError("company_replay_selection_invalid")
        keys.append((row[0], row[1], row[2]))
    return frozenset(keys)
