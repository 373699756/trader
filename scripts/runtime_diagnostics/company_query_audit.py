"""Bounded real CNInfo count/filter audit; local databases are strictly read-only."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy

import json
import time
from collections.abc import Mapping
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal, cast
from urllib.parse import parse_qs

from scripts.runtime_diagnostics.company_publication_audit import AuditCompanyPublication
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
from trader.download.infra.download_settings import LoadCompanySyncConfiguration
from trader.infra.http_contracts import HttpRequest, HttpResponse
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession, SqliteTransaction


class _Capture:
    def __init__(self) -> None:
        self.total: int | None = None
        self.rows = 0
        self.outside_codes = 0
        self.same_issuer = 0
        self.b_shares = 0

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
            self.b_shares += int(any(code.startswith(("900", "200")) for code in codes))
            if selected and not selected.intersection(codes):
                self.outside_codes += 1
                self.same_issuer += int(str(row.get("orgId") or "") in issuers)


def AuditCompanyQueries(path: Path, *, budget_seconds: float, sqlite_policy: SqliteIoPolicy) -> dict[str, object]:
    configuration = LoadCompanySyncConfiguration(Path(__file__).resolve().parents[2])
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


def AuditCompanyStorage(
    path: Path, *, now: datetime, budget_seconds: float, facts_path: Path | None = None, sqlite_policy: SqliteIoPolicy
) -> dict[str, object]:
    """Count local records and persisted retry reasons without reading bodies into the report."""
    deadline = time.monotonic() + budget_seconds
    cutoff = (now.date() - timedelta(days=365)).isoformat()
    pending = cooling = ready = deterministic = 0
    failures: dict[str, int] = {}
    failure_classes: dict[str, int] = {}
    with (
        SqliteSession(
            path,
            SqliteConnectionOptions(read_only=True, timeout_seconds=5),
            cancel_requested=lambda: time.monotonic() >= deadline, sqlite_policy=sqlite_policy,
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
    if facts_path is not None and facts_path.exists():
        with SqliteSession(
            facts_path, SqliteConnectionOptions(read_only=True, timeout_seconds=5),
            cancel_requested=lambda: time.monotonic() >= deadline, sqlite_policy=sqlite_policy,
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
    publication = AuditCompanyPublication(path, facts_path, now=now, deadline=deadline, sqlite_policy=sqlite_policy)
    publication_findings = publication["findings"]
    if not isinstance(publication_findings, list):
        raise ValueError("company_publication_findings_invalid")
    return {
        "schema_version": "company-resume",
        "status": "degraded" if partitions or pending or publication_findings else "passed",
        "summary": {
            "local_storage": {
                "read_only": True,
                "pdf_archived": False,
                "database_bytes": path.stat().st_size,
                "free_bytes": page_size * free_pages,
                "table_bytes": table_bytes,
                "announcement_records": totals[0],
                "extracted_bodies": totals[1] or 0,
                "retained_bodies": totals[2],
                "formal": formal,
                "publication": publication,
                "pending_ranges": partitions,
                "pending_documents": pending,
                "stored_retry_cooling": cooling,
                "stored_retry_ready": ready,
                "deterministic_failures": deterministic,
                "partition_failures": partition_failures,
                "document_failures": [
                    {"failure": failure, "documents": count} for failure, count in sorted(failures.items())
                ],
                "document_failure_classes": [
                    {"class": name, "documents": count} for name, count in sorted(failure_classes.items())
                ],
                "retry_payload_hashes": retry_payload_hashes,
                "ocr_diagnostics": ocr_diagnostics,
                "document_extractions": extraction_methods,
            }
        },
        "findings": (
            (["company_download_pending"] if partitions or ready or cooling else [])
            + (["company_documents_deterministic_failure"] if deterministic else [])
            + publication_findings
        ),
    }


__all__ = ["AuditCompanyQueries", "AuditCompanyStorage"]
