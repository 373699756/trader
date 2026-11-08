"""Bounded real CNInfo count/filter audit; local databases are strictly read-only."""

from __future__ import annotations

from scripts.core.paths import PROJECT_ROOT as TOOL_ROOT

from trader.infra.sqlite_policy import SqliteIoPolicy

import json
import re
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal, cast
from urllib.parse import parse_qs

from scripts.download.company.company_publication_audit import AuditCompanyPublication
from scripts.download.company.company_risk_comparison import CompareCompanyRisks, ProjectCompanyRiskComparison
from trader.bootstrap import OpenExternalHttp
from trader.download.domain.company_event_extraction import CompanyDocumentNeedsReview
from trader.download.domain.company_evidence import (
    CompanyDocumentRouteForFailure,
    DisclosurePartition,
    DisclosureQuery,
    DisclosureSecurity,
    IsTransientCompanyDocumentFailure,
    Market,
)
from trader.download.infra.company_disclosure_source import CninfoMarketDisclosureSource
from trader.download.infra.company_fact_inputs import DecodeCompanyDisclosure
from trader.download.infra.company_raw_files import CompanyRawFiles
from trader.download.infra.download_failure_diagnostics import ReadDownloadFailures
from trader.download.infra.download_settings import LoadCompanySyncConfiguration, LoadDownloadFailureConfiguration
from trader.infra.http_contracts import HttpRequest, HttpResponse
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession, SqliteTransaction


class _CompanyDatabaseAuditBudgetExceeded(TimeoutError):
    """The local audit deadline expired between SQL statements."""


class _Capture:
    def __init__(self) -> None:
        self.total: int | None = None
        self.rows = 0
        self.outside_codes = 0
        self.same_issuer = 0
        self.b_shares = 0
        self.code_prefixes: set[str] = set()

    def inspect(self, request: HttpRequest, response: HttpResponse) -> None:
        if request.method != "POST" or not request.url.endswith("/hisAnnouncement/query"):
            return
        payload = response.json()
        if not isinstance(payload, Mapping):
            return
        total = payload.get("totalRecordNum")
        self.total = total if type(total) is int else None
        rows = payload.get("announcements")
        if not isinstance(rows, list):
            return
        self.rows = len(rows)
        stock = parse_qs((request.body or b"").decode()).get("stock", [""])[0]
        securities = [item.split(",", 1) for item in stock.split(";") if "," in item]
        selected = {item[0] for item in securities}
        issuers = {item[1] for item in securities}
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            codes = set(str(row.get("secCode") or "").split(","))
            self.code_prefixes.update(code[:3] for code in codes if len(code) == 6 and code.isascii() and code.isdigit())
            self.b_shares += int(any(code.startswith(("900", "200")) for code in codes))
            if selected and not selected.intersection(codes):
                self.outside_codes += 1
                self.same_issuer += int(str(row.get("orgId") or "") in issuers)


def AuditCompanyQueries(path: Path, *, budget_seconds: float, sqlite_policy: SqliteIoPolicy) -> dict[str, object]:
    configuration = LoadCompanySyncConfiguration(TOOL_ROOT)
    deadline = time.monotonic() + budget_seconds
    with SqliteSession(path, SqliteConnectionOptions(read_only=True), sqlite_policy=sqlite_policy) as connection:
        targets = tuple(
            DisclosurePartition(cast(Market, row[0]), date.fromisoformat(row[1]), date.fromisoformat(row[2]))
            for row in connection.run(
                "SELECT market,start_date,end_date FROM partitions WHERE is_complete=0 AND failure<>'' ORDER BY start_date DESC LIMIT 3"
            )
        )
        failed = tuple(
            DisclosurePartition(
                cast(Market, row[0]),
                date.fromisoformat(row[1]),
                date.fromisoformat(row[2]),
                page=row[3],
                query=DisclosureQuery(row[4], tuple(DisclosureSecurity(*item) for item in json.loads(row[5]))),
            )
            for row in connection.run(
                "SELECT market,start_date,end_date,next_page,plate,securities FROM disclosure_queries "
                "WHERE is_complete=0 AND failure<>'' ORDER BY start_date DESC LIMIT 3"
            )
        )
    capture = _Capture()
    pages: list[dict[str, object]] = []
    totals: list[dict[str, object]] = []
    with OpenExternalHttp(inspect_response=capture.inspect) as session:
        source = CninfoMarketDisclosureSource(
            session,
            monotonic=time.monotonic,
            sleeper=time.sleep,
            timeout_seconds=configuration.source_timeout_seconds,
            document_download_timeout_seconds=configuration.document_download_timeout_seconds,
            document_parse_timeout_seconds=configuration.document_parse_timeout_seconds,
            request_interval_seconds=configuration.request_interval_seconds,
            document_limits=configuration.document_limits,
            extended_document_limits=configuration.extended_document_limits,
            cancel_requested=lambda: time.monotonic() >= deadline,
        )

        def fetch(partition: DisclosurePartition, *, order: Literal["", "asc", "desc"] = "") -> int | None:
            if time.monotonic() >= deadline or len(pages) >= 192:
                return None
            capture.total = None
            capture.rows = capture.outside_codes = capture.same_issuer = capture.b_shares = 0
            capture.code_prefixes.clear()
            failure = ""
            try:
                source.fetch(partition, order=order)
            except (OSError, ValueError, RuntimeError) as exc:
                failure = (
                    str(exc) if isinstance(exc, ValueError) and str(exc).startswith("cninfo_") else type(exc).__name__
                )
            pages.append(
                {
                    "market": partition.market,
                    "start": str(partition.start),
                    "end": str(partition.end),
                    "plate": partition.query.plate if partition.query else "",
                    "page": partition.page,
                    "order": order,
                    "members": len(partition.query.securities) if partition.query else 0,
                    "total": capture.total,
                    "rows": capture.rows,
                    "outside_codes": capture.outside_codes,
                    "same_issuer": capture.same_issuer,
                    "b_share_rows": capture.b_shares,
                    "code_prefixes": sorted(capture.code_prefixes),
                    "failure": failure,
                }
            )
            return capture.total

        for partition in failed:
            fetch(partition)
        # Boundary samples expose board filters that include historical/B-share
        # codes, before the bounded aggregate count audit uses its page budget.
        for partition in targets:
            for query in source.split_queries(partition):
                for order in ("asc", "desc"):
                    fetch(replace(partition, query=query), order=order)
        for partition in targets:
            parent_total = fetch(partition)
            if parent_total is None:
                break
            board_total = 0
            group_total = 0
            complete = True
            for query in source.split_queries(partition):
                board = replace(partition, query=query)
                total = fetch(board)
                if total is None:
                    complete = False
                    break
                board_total += total
                if total <= 3000:
                    group_total += total
                    continue
                for group in source.split_queries(board):
                    count = fetch(replace(partition, query=group))
                    if count is None:
                        complete = False
                        break
                    group_total += count
            totals.append(
                {
                    "market": partition.market,
                    "start": str(partition.start),
                    "parent_total": parent_total,
                    "board_total": board_total,
                    "group_total": group_total,
                    "sample_complete": complete,
                }
            )
    passed = (
        bool(totals)
        and all(
            item["sample_complete"] and item["parent_total"] == item["board_total"] == item["group_total"]
            for item in totals
        )
        and all(not item["failure"] for item in pages)
    )
    return {
        "schema_version": "company-resume",
        "status": "passed" if passed else "degraded",
        "summary": {"query_audit": {"read_only": True, "pages": pages, "totals": totals}},
        "findings": [] if passed else ["company_query_coverage_inconsistent"],
    }


def AuditCompanyDatabase(
    path: Path, *, now: datetime, budget_seconds: float, facts_path: Path | None = None, sqlite_policy: SqliteIoPolicy,
    risk_baseline: Path | None = None,
) -> dict[str, object]:
    """Count local records and persisted retry reasons without reading bodies into the report."""
    deadline = time.monotonic() + budget_seconds
    deadline_cancelled = False

    def cancel_requested() -> bool:
        nonlocal deadline_cancelled
        if time.monotonic() >= deadline:
            deadline_cancelled = True
        return deadline_cancelled

    try:
        return _audit_company_database(
            path, now=now, deadline=deadline, cancel_requested=cancel_requested,
            facts_path=facts_path, sqlite_policy=sqlite_policy,
            risk_baseline=risk_baseline,
        )
    except (sqlite3.OperationalError, _CompanyDatabaseAuditBudgetExceeded) as exc:
        # Python 3.10 has no sqlite_errorcode. The exact SQLite interruption
        # message alone is insufficient: our own deadline callback must cancel.
        if not deadline_cancelled or isinstance(exc, sqlite3.OperationalError) and str(exc) != "interrupted":
            raise
        return {
            "schema_version": "company-resume",
            "status": "failed",
            "summary": {"database": {
                "read_only": True,
                "complete": False,
                "budget_exhausted": True,
                "failure_category": "budget_exhausted",
                "failure_code": "company_database_audit_budget_exhausted",
                "field_correctness_verified": False,
                "full_market_verified": False,
            }},
            "findings": ["company_database_audit_budget_exhausted"],
        }


def _audit_company_database(
    path: Path,
    *,
    now: datetime,
    deadline: float,
    cancel_requested: Callable[[], bool],
    facts_path: Path | None,
    sqlite_policy: SqliteIoPolicy,
    risk_baseline: Path | None,
) -> dict[str, object]:
    cutoff = (now.date() - timedelta(days=365)).isoformat()
    pending = cooling = ready = deterministic = 0
    failures: dict[str, int] = {}
    failure_classes: dict[str, int] = {}
    project_root = TOOL_ROOT
    configuration = LoadCompanySyncConfiguration(project_root)
    failure_configuration = LoadDownloadFailureConfiguration(project_root)
    raw = CompanyRawFiles(
        path.parent,
        capacity=configuration.raw_capacity_bytes,
        analysis_reserve=configuration.raw_analysis_reserve_bytes,
        free_reserve=configuration.temporary_disk_reserve_bytes,
        maximum_document_bytes=configuration.extended_document_limits.max_bytes,
    )
    with (
        SqliteSession(
            path,
            SqliteConnectionOptions(read_only=True, timeout_seconds=5),
            cancel_requested=cancel_requested, sqlite_policy=sqlite_policy,
        ) as connection,
        SqliteTransaction(connection),
    ):
        page_size = connection.run("PRAGMA page_size").require_one()[0]
        free_pages = connection.run("PRAGMA freelist_count").require_one()[0]
        table_bytes = [
            {"name": row[0], "bytes": row[1]}
            for row in connection.run("SELECT name,SUM(pgsize) FROM dbstat GROUP BY name ORDER BY SUM(pgsize) DESC")
        ]
        totals = connection.run(
            "SELECT COUNT(*),SUM(body_checked=1),COALESCE(SUM(body_retained=1),0) FROM disclosures WHERE kind='announcement'"
        ).require_one()
        partitions = connection.run(
            "SELECT COUNT(*) FROM partitions WHERE is_complete=0 AND end_date>=?", (cutoff,)
        ).require_one()[0]
        partition_failures = [
            {"failure": row[0], "ranges": row[1]}
            for row in connection.run(
                "SELECT failure,COUNT(*) FROM partitions WHERE is_complete=0 AND end_date>=? AND failure<>'' GROUP BY failure",
                (cutoff,),
            )
        ]
        failure_history = ReadDownloadFailures(
            connection, replace(failure_configuration, query_limit=failure_configuration.retention_limit)
        )
        retry_inventory = []
        for row in connection.run(
            "SELECT d.source,d.source_key,d.kind,d.title,d.published_at,d.url,d.record_hash,MIN(c.code) AS code,"
            "d.id,r.failure,r.attempted_at,r.retry_at,r.attempts,r.retry_policy_id,r.record_hash AS retry_hash,"
            "r.ocr_diagnostic,r.ocr_timeout_attempts,t.stage,d.body_checked,d.body_retained "
            "FROM document_retries r JOIN disclosures d ON d.id=r.disclosure_id "
            "JOIN disclosure_codes c ON c.disclosure_id=d.id LEFT JOIN document_tasks t ON t.source=d.source "
            "AND t.source_key=d.source_key AND t.record_hash=d.record_hash GROUP BY d.id ORDER BY d.id"
        ):
            if cancel_requested():
                raise _CompanyDatabaseAuditBudgetExceeded("company_database_audit_budget_exhausted")
            item = DecodeCompanyDisclosure(row)
            directory = raw.directory(item)
            codes = tuple(code[0] for code in connection.run(
                "SELECT code FROM disclosure_codes WHERE disclosure_id=? ORDER BY code", (row[8],)
            ))
            # Inspect names only: raw.local/result can rename corrupt files and are not read-only.
            try:
                files = sorted(file.name for file in directory.iterdir() if file.is_file())
                directory_available = True
            except FileNotFoundError:
                files = []
                directory_available = False
            retry_inventory.append({
                "disclosure_id": row[8], "source_key": item.identity, "record_hash": item.digest,
                "codes": codes, "published_at": item.published_at.isoformat(), "failure": row[9],
                "attempted_at": row[10], "retry_at": row[11], "attempts": row[12], "policy": row[13],
                "same_version": row[14] == item.digest, "ocr_diagnostic": json.loads(row[15]) if row[15] else None,
                "ocr_timeout_attempts": row[16], "task_stage": row[17], "body_checked": bool(row[18]),
                "body_retained": bool(row[19]), "directory": directory.relative_to(path.parent).as_posix(),
                "directory_available": directory_available,
                "files": files, "pdf_files": sum(name.endswith(".pdf") for name in files),
                "failure_records": sum(name.startswith("failure-") and name.endswith(".json") for name in files),
            })
        for row in connection.run(
            "SELECT d.title,r.failure,r.retry_at FROM disclosures d LEFT JOIN document_retries r "
            "ON r.disclosure_id=d.id AND r.record_hash=d.record_hash WHERE d.kind='announcement' AND d.body_checked=0 "
            "AND d.published_at>=? AND d.published_at<=? AND EXISTS(SELECT 1 FROM disclosure_codes c "
            "JOIN company_population p ON p.code=c.code WHERE c.disclosure_id=d.id)",
            (cutoff, now.isoformat()),
        ):
            if not CompanyDocumentNeedsReview(row[0]):
                continue
            pending += 1
            if row[1]:
                failures[row[1]] = failures.get(row[1], 0) + 1
                classification = (
                    "transient"
                    if IsTransientCompanyDocumentFailure(row[1])
                    else CompanyDocumentRouteForFailure(row[1])
                )
                failure_classes[classification] = failure_classes.get(classification, 0) + 1
            if row[1] and not IsTransientCompanyDocumentFailure(row[1]):
                deterministic += 1
            elif row[2] and datetime.fromisoformat(row[2]) > now:
                cooling += 1
            else:
                ready += 1
        retry_payload_hashes = 0
        recent_failures = [
            {
                "disclosure_id": row[0], "failure": row[1], "attempted_at": row[2],
                "retry_at": row[3], "payload_hash": row[4].hex() if row[4] is not None else None,
            }
            for row in connection.run(
                "SELECT r.disclosure_id,r.failure,r.attempted_at,r.retry_at,r.payload_hash "
                "FROM document_retries r JOIN disclosures d ON d.id=r.disclosure_id "
                "AND d.record_hash=r.record_hash ORDER BY r.attempted_at DESC LIMIT 10"
            )
        ]
        retry_columns = {
            str(row[1]) for row in connection.run("PRAGMA table_info(document_retries)").fetchall()
        }
        ocr_diagnostics: list[dict[str, object]] = []
        if "ocr_diagnostic" in retry_columns:
            for row in connection.run(
                "SELECT failure,attempted_at,ocr_timeout_attempts,ocr_diagnostic FROM document_retries "
                "WHERE ocr_diagnostic IS NOT NULL ORDER BY attempted_at DESC LIMIT 10"
            ):
                diagnostic = json.loads(row[3])
                if not isinstance(diagnostic, dict):
                    raise ValueError("company_ocr_diagnostic_invalid")
                ocr_diagnostics.append({
                    "failure": row[0], "attempted_at": row[1], "timeout_attempts": row[2],
                    "diagnostic": {key: diagnostic.get(key) for key in (
                        "phase", "page", "pages", "characters", "confidence", "segmentation_mode"
                    )},
                })
        if "payload_hash" in retry_columns:
            retry_payload_hashes = connection.run(
                "SELECT COUNT(*) FROM document_retries WHERE payload_hash IS NOT NULL"
            ).require_one()[0]
        extraction_methods: list[dict[str, object]] = []
        if connection.run(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='document_extractions'"
        ).fetchone():
            extraction_methods = [
                {"method": row[0], "documents": row[1]}
                for row in connection.run(
                    "SELECT method,COUNT(*) FROM document_extractions GROUP BY method ORDER BY method"
                )
            ]
    formal: dict[str, object] = {"available": False}
    failure_receipts: list[dict[str, object]] = []
    if facts_path is not None and facts_path.exists():
        with SqliteSession(
            facts_path, SqliteConnectionOptions(read_only=True, timeout_seconds=5),
            cancel_requested=cancel_requested, sqlite_policy=sqlite_policy,
        ) as connection, SqliteTransaction(connection):
            formal = {
                "available": True,
                "database_bytes": facts_path.stat().st_size,
                "stocks": connection.run("SELECT COUNT(*) FROM company_facts").require_one()[0],
                "table_bytes": [
                    {"name": row[0], "bytes": row[1]}
                    for row in connection.run(
                        "SELECT name,SUM(pgsize) FROM dbstat GROUP BY name ORDER BY SUM(pgsize) DESC"
                    )
                ],
            }
            events = cast(list[dict[str, object]], failure_history["events"])
            source_keys = sorted({
                match[1] for event in events
                if (match := re.search(r";record=([a-zA-Z0-9_-]+);version=", str(event["diagnostic"])))
            })
            for source_key in source_keys:
                for receipt in connection.run(
                    "SELECT source,source_key,code,record_hash,outcome,policy_id,published_at "
                    "FROM company_input_receipt WHERE source='cninfo' AND source_key=?", (source_key,)
                ):
                    failure_receipts.append(dict(zip(
                        ("source", "source_key", "code", "record_hash", "outcome", "policy", "published_at"),
                        tuple(receipt), strict=True,
                    )))
    publication = AuditCompanyPublication(
        path, facts_path, now=now, deadline=deadline, sqlite_policy=sqlite_policy, cancel_requested=cancel_requested,
    )
    publication_findings = publication["findings"]
    if not isinstance(publication_findings, list):
        raise ValueError("company_publication_findings_invalid")
    risk_comparison: dict[str, object] | None = None
    risk_findings: list[str] = []
    if risk_baseline is not None:
        if facts_path is None:
            raise ValueError("company_risk_comparison_requires_formal_database")
        comparison = CompareCompanyRisks(
            facts_path, risk_baseline, deadline=deadline, sqlite_policy=sqlite_policy,
            cancel_requested=cancel_requested,
        )
        risk_comparison = ProjectCompanyRiskComparison(comparison)
        if not comparison.all_old_risks_and_evidence_preserved:
            risk_findings.append("company_old_risks_require_review")
        if not comparison.stable_read or comparison.publication_hash != publication.get("publication_hash"):
            risk_findings.append("company_audit_concurrent_change")
    return {
        "schema_version": "company-resume",
        "status": "degraded" if partitions or pending or publication_findings or risk_findings else "passed",
        "summary": {
            "database": {
                "read_only": True,
                "pdf_archived": (path.parent / "failed").exists(),
                "retained_failed_pdfs": sum(1 for _ in (path.parent / "failed").rglob("*.pdf")),
                "database_bytes": path.stat().st_size,
                "free_bytes": page_size * free_pages,
                "table_bytes": table_bytes,
                "announcement_records": totals[0],
                "extracted_bodies": totals[1] or 0,
                "retained_bodies": totals[2],
                "formal": formal,
                "publication": publication,
                **({"risk_comparison": risk_comparison} if risk_comparison is not None else {}),
                "pending_ranges": partitions,
                "pending_documents": pending,
                "recorded_retry_cooling": cooling,
                "recorded_retry_ready": ready,
                "deterministic_failures": deterministic,
                "partition_failures": partition_failures,
                "recent_document_failures": recent_failures,
                "document_failures": [
                    {"failure": failure, "documents": count} for failure, count in sorted(failures.items())
                ],
                "document_failure_classes": [
                    {"class": name, "documents": count} for name, count in sorted(failure_classes.items())
                ],
                "retry_payload_hashes": retry_payload_hashes,
                "failure_history": failure_history,
                "failure_receipts": failure_receipts,
                "retry_inventory": retry_inventory,
                "file_inventory_is_atomic": False,
                "ocr_diagnostics": ocr_diagnostics,
                "document_extractions": extraction_methods,
            }
        },
        "findings": (
            (["company_download_pending"] if partitions or ready or cooling else [])
            + (["company_documents_deterministic_failure"] if deterministic else [])
            + publication_findings
            + risk_findings
        ),
    }


__all__ = ["AuditCompanyQueries", "AuditCompanyDatabase"]
