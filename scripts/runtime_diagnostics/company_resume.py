"""Replay failed company slices against real CNInfo only in an external SQLite backup."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Literal, cast
from zoneinfo import ZoneInfo

from scripts.runtime_diagnostics.company_pagination_audit import AuditCompanyPagination
from scripts.runtime_diagnostics.company_pipeline_audit import AuditCompanyPipeline
from scripts.runtime_diagnostics.company_ocr_audit import AuditCompanyOcr
from scripts.runtime_diagnostics.company_query_audit import AuditCompanyQueries, AuditCompanyStorage
from trader.bootstrap import OpenExternalHttp
from trader.download.application.company_evidence import CompanyEvidenceDependencies, SyncCompanyEvidence
from trader.download.domain.company_events import CompanyEventPolicy
from trader.download.domain.company_evidence import (
    CompanyDocumentExtraction,
    CompanyDocumentPolicies,
    CompanyDocumentTask,
    CompanyEvidenceRequest,
    CompanySyncConfiguration,
    Disclosure,
    DisclosurePage,
    DisclosurePartition,
    Market,
)
from trader.download.infra.company_disclosure_source import CninfoMarketDisclosureSource
from trader.download.infra.company_document_files import CompanyDocumentFiles
from trader.download.infra.company_evidence_index import SQLiteCompanyEvidenceIndex
from trader.download.infra.company_pipeline_queries import ReleaseCompletedCompanyQueryRows
from trader.download.infra.company_structured_source import EastmoneyCompanySource
from trader.download.infra.download_settings import LoadCompanySyncConfiguration
from trader.infra.http_contracts import HttpTransport
from trader.infra.settings import LoadStrategySettings
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession, SqliteTransaction

_ROOT = Path(__file__).resolve().parents[2]


class _ReplayIndex(SQLiteCompanyEvidenceIndex):
    def __init__(
        self,
        path: Path,
        selection: frozenset[tuple[str, str, str]],
        configuration: CompanySyncConfiguration,
        *,
        facts_path: Path,
        event_policy: CompanyEventPolicy,
    ) -> None:
        super().__init__(
            path,
            facts_path=facts_path,
            documents=CompanyDocumentFiles(
                path.parent / "text",
                max_text_characters=configuration.extended_document_limits.max_text_characters,
                max_bytes=configuration.temporary_text_capacity_bytes,
                reserve_bytes=configuration.temporary_disk_reserve_bytes,
            ),
            event_policy=event_policy,
        )
        self._selection = selection

    def partitions(self, markets: Sequence[Market], observed_at: datetime) -> tuple[DisclosurePartition, ...]:
        return tuple(
            p
            for p in super().partitions(markets, observed_at)
            if (p.market, str(p.start), str(p.end)) in self._selection
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
    ) -> tuple[CompanyDocumentTask, ...]:
        return cast(
            tuple[CompanyDocumentTask, ...],
            super().pending_documents(
                codes,
                observed_at,
                limit=min(limit or 32, 32),
                policies=policies,
                excluded=excluded,
                repair=repair,
            ),
        )


class _ObservedSource(CninfoMarketDisclosureSource):
    def __init__(
        self, session: HttpTransport, configuration: CompanySyncConfiguration, *, cancel_requested: Callable[[], bool]
    ) -> None:
        super().__init__(
            session,
            monotonic=time.monotonic,
            sleeper=time.sleep,
            timeout_seconds=configuration.source_timeout_seconds,
            document_parse_timeout_seconds=configuration.document_parse_timeout_seconds,
            document_limits=configuration.document_limits,
            extended_document_limits=configuration.extended_document_limits,
            request_interval_seconds=configuration.request_interval_seconds,
            cancel_requested=cancel_requested,
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

    def document(self, item: Disclosure, *, route: Literal["standard", "ocr", "limits"] = "standard") -> CompanyDocumentExtraction:
        self.documents += 1
        return super().document(item, route=route)


def _copy(source: Path, destination: Path, *, deadline: float) -> None:
    with tempfile.NamedTemporaryFile(
        prefix=".company-copy-", suffix=".sqlite3", dir=destination.parent, delete=False
    ) as temporary:
        pending = Path(temporary.name)
    try:
        with SqliteSession(source, SqliteConnectionOptions(read_only=True, timeout_seconds=8)) as original:
            with SqliteSession(pending, SqliteConnectionOptions(timeout_seconds=8)) as replica:
                original.backup_to(replica, deadline=deadline, monotonic=time.monotonic)
        pending.replace(destination)
    finally:
        pending.unlink(missing_ok=True)


def _saved_pages(path: Path, pages: list[tuple[str, str, str, str, int]]) -> set[tuple[str, str, str, str, int]]:
    saved = set()
    with SqliteSession(path, SqliteConnectionOptions(read_only=True)) as c:
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


def _retained_digest(path: Path, deadline: float) -> str:
    digest = hashlib.sha256()
    with (
        SqliteSession(
            path, SqliteConnectionOptions(read_only=True), cancel_requested=lambda: time.monotonic() >= deadline
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


def _compact_storage(source: Path, directory: Path, *, budget_seconds: float) -> dict[str, object]:
    if not directory.is_absolute() or directory == _ROOT or _ROOT in directory.parents:
        raise ValueError("external absolute directory required")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "company_compacted.sqlite3"
    if target.exists():
        raise ValueError("company_compaction_target_exists")
    deadline = time.monotonic() + budget_seconds
    _copy(source, target, deadline=deadline)
    before_digest = _retained_digest(target, deadline)
    before_bytes = target.stat().st_size
    with SqliteSession(target, cancel_requested=lambda: time.monotonic() >= deadline) as connection:
        with SqliteTransaction(connection, immediate=True):
            ReleaseCompletedCompanyQueryRows(connection)
            removed = connection.total_changes
        connection.run("VACUUM")
        if connection.run("PRAGMA integrity_check").require_one()[0] != "ok":
            raise RuntimeError("company_compaction_integrity_failed")
        if connection.run("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("company_compaction_foreign_key_failed")
    preserved = before_digest == _retained_digest(target, deadline)
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--facts-database", type=Path)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--budget-seconds", required=True, type=float)
    audit = parser.add_mutually_exclusive_group()
    audit.add_argument("--audit-only", action="store_true")
    audit.add_argument("--storage-only", action="store_true")
    audit.add_argument("--pagination-only", action="store_true")
    audit.add_argument("--pipeline-only", action="store_true")
    audit.add_argument("--ocr-only", action="store_true")
    args = parser.parse_args()
    implementation = hashlib.sha256()
    for relative in (
        "scripts/runtime_diagnostics/company_resume.py",
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
    if args.storage_only:
        report = AuditCompanyStorage(
            source_path,
            now=datetime.now(ZoneInfo("Asia/Shanghai")),
            budget_seconds=min(180.0, args.budget_seconds),
            facts_path=args.facts_database,
        )
        if args.directory is not None:
            summary = report["summary"]
            if not isinstance(summary, dict):
                raise ValueError("company_storage_summary_invalid")
            summary["compaction"] = _compact_storage(
                source_path, args.directory.resolve(), budget_seconds=args.budget_seconds
            )
        print(json.dumps(report, ensure_ascii=False))
        return 0
    if args.pagination_only:
        print(
            json.dumps(
                AuditCompanyPagination(source_path, budget_seconds=min(300.0, args.budget_seconds)), ensure_ascii=False
            )
        )
        return 0
    if args.audit_only:
        report = AuditCompanyQueries(source_path, budget_seconds=min(300.0, args.budget_seconds))
        print(json.dumps(report, ensure_ascii=False))
        return 0
    if args.directory is None:
        parser.error("replay requires an external absolute directory")
    directory = args.directory.resolve()
    if not args.directory.is_absolute() or directory == _ROOT or _ROOT in directory.parents:
        parser.error("external absolute directory required")
    directory.mkdir(parents=True, exist_ok=True)
    if args.ocr_only:
        report = AuditCompanyOcr(source_path, budget_seconds=min(150.0, args.budget_seconds))
        (directory / "ocr-result.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))
        return 0
    if args.pipeline_only:
        print(
            json.dumps(
                AuditCompanyPipeline(source_path, directory, budget_seconds=args.budget_seconds), ensure_ascii=False
            )
        )
        return 0
    destination = directory / "evidence.sqlite3"
    if destination.resolve() == source_path or (destination.exists() and destination.samefile(source_path)):
        parser.error("source and replay database must differ")
    started = time.monotonic()
    if not destination.exists():
        _copy(source_path, destination, deadline=started + args.budget_seconds)
    selection_path = directory / "selection.json"
    with SqliteSession(destination, SqliteConnectionOptions(read_only=True)) as c:
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
        _copy(args.facts_database, facts_path, deadline=started + args.budget_seconds)
    configuration = replace(LoadCompanySyncConfiguration(_ROOT), document_workers=1)
    event_policy = LoadStrategySettings(_ROOT / "config" / "strategy.json").company_scoring.events
    index = _ReplayIndex(
        destination,
        selection,
        configuration,
        facts_path=facts_path,
        event_policy=event_policy,
    )
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
                session, configuration, cancel_requested=lambda: time.monotonic() >= started + args.budget_seconds
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
                ),
                CompanyEvidenceRequest(codes, now, configuration, event_policy),
            )
            if number == 0:
                saved = _saved_pages(destination, source.pages)
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
    with SqliteSession(destination, SqliteConnectionOptions(read_only=True)) as c:
        completed = sum(
            bool(c.run("SELECT is_complete FROM partitions WHERE market=? AND start_date=? AND end_date=?", key).require_one()[0])
            for key in selection
        )
        covered = sum(
            c.run("SELECT 1 FROM coverage WHERE market=? AND start_date=? AND end_date=?", key).fetchone() is not None
            for key in selection
        )
    passed = completed == covered == len(selection) and replayed == 0
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
            "runs": runs,
        },
        "findings": [] if passed else ["company_resume_incomplete"],
    }
    (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
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


if __name__ == "__main__":
    raise SystemExit(Main())


__all__ = ["Main"]
