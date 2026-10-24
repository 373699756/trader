"""Bounded real CNInfo count/filter audit; local databases are strictly read-only."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Literal, cast
from urllib.parse import parse_qs

from trader.bootstrap import OpenExternalHttp
from trader.download.domain.company_evidence import DisclosurePartition, DisclosureQuery, DisclosureSecurity, Market
from trader.download.infra.company_disclosure_source import CninfoMarketDisclosureSource
from trader.infra.http_contracts import HttpRequest, HttpResponse
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession


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
        source = CninfoMarketDisclosureSource(session, monotonic=time.monotonic, sleeper=time.sleep)

        def fetch(partition: DisclosurePartition, *, order: Literal["", "asc", "desc"] = "") -> int | None:
            if time.monotonic() >= deadline or len(pages) >= 192:
                return None
            capture.total = None
            capture.rows = capture.outside_codes = capture.same_issuer = capture.b_shares = 0
            failure = ""
            try:
                source.fetch(partition, deadline=deadline, order=order)
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
            for query in source.split_queries(partition, deadline=deadline):
                for order in ("asc", "desc"):
                    fetch(replace(partition, query=query), order=order)
        for partition in targets:
            parent_total = fetch(partition)
            if parent_total is None:
                break
            board_total = 0
            group_total = 0
            complete = True
            for query in source.split_queries(partition, deadline=deadline):
                board = replace(partition, query=query)
                total = fetch(board)
                if total is None:
                    complete = False
                    break
                board_total += total
                if total <= 3000:
                    group_total += total
                    continue
                for group in source.split_queries(board, deadline=deadline):
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


__all__ = ["AuditCompanyQueries"]
