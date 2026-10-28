"""Compare complete CNInfo record sets at different page sizes without database writes."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

from trader.bootstrap import OpenExternalHttp
from trader.download.domain.company_evidence import DisclosurePartition, Market
from trader.download.infra.company_disclosure_source import CninfoMarketDisclosureSource
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession


def AuditCompanyPagination(path: Path, *, budget_seconds: float) -> dict[str, object]:
    deadline = time.monotonic() + budget_seconds
    yesterday = datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)
    with SqliteSession(path, SqliteConnectionOptions(read_only=True, timeout_seconds=3)) as connection:
        targets = tuple(
            DisclosurePartition(cast(Market, row[0]), date.fromisoformat(row[1]), date.fromisoformat(row[2]))
            for row in connection.run(
                "SELECT market,start_date,end_date FROM partitions WHERE is_complete=1 AND start_date=end_date AND end_date<=? "
                "AND expected_rows BETWEEN 31 AND 600 ORDER BY end_date DESC,market LIMIT 3",
                (yesterday.isoformat(),),
            )
        )
    comparisons: list[dict[str, object]] = []
    with OpenExternalHttp() as session:
        source = CninfoMarketDisclosureSource(
            session,
            monotonic=time.monotonic,
            sleeper=time.sleep,
            cancel_requested=lambda: time.monotonic() >= deadline,
        )
        for target in targets:
            baseline: frozenset[tuple[str, tuple[str, ...]]] | None = None
            for size in (30, 50, 100):
                began = time.monotonic()
                identities: set[tuple[str, tuple[str, ...]]] = set()
                page_counts: list[int] = []
                total: int | None = None
                failure = ""
                try:
                    for number in range(1, 22):
                        page = source.fetch(replace(target, page=number, page_size=size))
                        if total is not None and total != page.total:
                            raise ValueError("cninfo_total_changed")
                        total = page.total
                        page_counts.append(len(page.rows))
                        keys = {(item.identity, tuple(sorted(item.codes))) for item in page.rows}
                        if identities.intersection(keys) or len(keys) != len(page.rows):
                            raise ValueError("cninfo_duplicate_page_links")
                        identities.update(keys)
                        if not page.has_more:
                            break
                        if len(page.rows) != size:
                            raise ValueError("cninfo_requested_page_size_ignored")
                    else:
                        raise ValueError("cninfo_audit_page_limit")
                    if len(identities) != total:
                        raise ValueError("cninfo_query_coverage_mismatch")
                except (OSError, RuntimeError, ValueError) as exc:
                    failure = (
                        str(exc)
                        if isinstance(exc, ValueError) and str(exc).startswith("cninfo_")
                        else type(exc).__name__
                    )
                if size == 30 and not failure:
                    baseline = frozenset(identities)
                equivalent = baseline is not None and frozenset(identities) == baseline and not failure
                comparisons.append(
                    {
                        "market": target.market,
                        "date": str(target.start),
                        "page_size": size,
                        "total": total,
                        "page_rows": page_counts,
                        "equivalent": bool(equivalent),
                        "failure": failure,
                        "seconds": round(time.monotonic() - began, 3),
                    }
                )
                if time.monotonic() >= deadline:
                    break
            if time.monotonic() >= deadline:
                break
    passed = bool(targets) and len(comparisons) == 3 * len(targets) and all(item["equivalent"] for item in comparisons)
    return {
        "schema_version": "company-resume",
        "status": "passed" if passed else "degraded",
        "summary": {
            "query_audit": {
                "read_only": True,
                "pagination": comparisons,
                "category_filter": "unverified_full_directory_retained",
            }
        },
        "findings": [] if passed else ["company_pagination_not_verified"],
    }
