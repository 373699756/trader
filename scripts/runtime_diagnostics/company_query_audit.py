"""Bounded real CNInfo count/filter audit; local databases are strictly read-only."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal, cast
from urllib.parse import parse_qs

from trader.bootstrap import OpenExternalHttp
from trader.download.domain.company_event_extraction import CompanyDocumentNeedsReview
from trader.download.domain.company_evidence import DisclosurePartition, DisclosureQuery, DisclosureSecurity, Market
from trader.download.infra.company_disclosure_source import CninfoMarketDisclosureSource
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


def AuditCompanyQueries(path: Path, *, budget_seconds: float) -> dict[str, object]:
    deadline = time.monotonic() + budget_seconds
    with SqliteSession(path, SqliteConnectionOptions(read_only=True)) as connection:
        targets = tuple(
            DisclosurePartition(cast(Market, row[0]), date.fromisoformat(row[1]), date.fromisoformat(row[2]))
            for row in connection.run(
                "SELECT market,start,end FROM partitions WHERE complete=0 AND failure<>'' ORDER BY start DESC LIMIT 3"
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
                "SELECT market,start,end,next_page,plate,securities FROM disclosure_queries "
                "WHERE complete=0 AND failure<>'' ORDER BY start DESC LIMIT 3"
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


def AuditCompanyStorage(path: Path, *, now: datetime, budget_seconds: float) -> dict[str, object]:
    """Count local records and persisted retry reasons without reading bodies into the report."""
    deadline = time.monotonic() + budget_seconds
    cutoff = (now.date() - timedelta(days=365)).isoformat()
    pending = cooling = ready = 0
    failures: dict[str, int] = {}
    with (
        SqliteSession(
            path,
            SqliteConnectionOptions(read_only=True, timeout_seconds=5),
            cancel_requested=lambda: time.monotonic() >= deadline,
        ) as connection,
        SqliteTransaction(connection),
    ):
        totals = connection.run(
            "SELECT COUNT(*),SUM(body_checked=1),COALESCE(SUM(length(body)),0) FROM disclosures WHERE kind='announcement'"
        ).require_one()
        publication = connection.run("SELECT published_at FROM publication WHERE singleton=1").fetchone()
        stocks = connection.run("SELECT COUNT(*) FROM published").require_one()[0]
        partitions = connection.run(
            "SELECT COUNT(*) FROM partitions WHERE complete=0 AND end>=?", (cutoff,)
        ).require_one()[0]
        partition_failures = [
            {"failure": row[0], "ranges": row[1]}
            for row in connection.run(
                "SELECT failure,COUNT(*) FROM partitions WHERE complete=0 AND end>=? AND failure<>'' GROUP BY failure",
                (cutoff,),
            )
        ]
        for row in connection.run(
            "SELECT d.title,r.failure,r.retry_after FROM disclosures d LEFT JOIN document_retries r "
            "ON r.disclosure_id=d.id AND r.digest=d.digest WHERE d.kind='announcement' AND d.body_checked=0 "
            "AND d.published_at>=? AND d.published_at<=? AND EXISTS(SELECT 1 FROM disclosure_codes c "
            "JOIN published p ON p.code=c.code WHERE c.disclosure_id=d.id)",
            (cutoff, now.isoformat()),
        ):
            if not CompanyDocumentNeedsReview(row[0]):
                continue
            pending += 1
            if row[1]:
                failures[row[1]] = failures.get(row[1], 0) + 1
            if row[2] and datetime.fromisoformat(row[2]) > now:
                cooling += 1
            else:
                ready += 1
    return {
        "schema_version": "company-resume",
        "status": "degraded" if partitions or pending else "passed",
        "summary": {
            "local_storage": {
                "read_only": True,
                "pdf_archived": False,
                "database_bytes": path.stat().st_size,
                "announcement_records": totals[0],
                "extracted_bodies": totals[1] or 0,
                "body_characters": totals[2],
                "published_stocks": stocks,
                "published_at": publication[0] if publication else None,
                "pending_ranges": partitions,
                "pending_documents": pending,
                "stored_retry_cooling": cooling,
                "stored_retry_ready": ready,
                "partition_failures": partition_failures,
                "document_failures": [
                    {"failure": failure, "documents": count} for failure, count in sorted(failures.items())
                ],
            }
        },
        "findings": ["company_download_pending"] if partitions or pending else [],
    }


__all__ = ["AuditCompanyQueries", "AuditCompanyStorage"]
