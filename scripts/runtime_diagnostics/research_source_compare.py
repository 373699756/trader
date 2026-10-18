"""Read-only qualification of production company evidence source adapters."""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

import requests

from trader.download.domain.company_evidence import DisclosurePartition, Market, StructuredCompanyQuery
from trader.download.infra.company_disclosure_source import CninfoMarketDisclosureSource, DisclosureSession
from trader.download.infra.company_structured_source import EastmoneyCompanySource

_SHANGHAI = ZoneInfo("Asia/Shanghai")


def Collect(args: argparse.Namespace) -> dict[str, object]:
    started = time.monotonic()
    deadline = started + args.task_seconds
    now = datetime.now(_SHANGHAI)
    day = now.date() - timedelta(days=3)
    samples: list[dict[str, object]] = []
    failures: list[str] = []
    with requests.Session() as session:
        session.trust_env = False
        transport = cast(DisclosureSession, session)
        source = CninfoMarketDisclosureSource(
            transport, monotonic=time.monotonic, sleeper=time.sleep, timeout_seconds=args.timeout_seconds
        )
        for market in ("sse", "szse", "bse"):
            query = DisclosurePartition(cast(Market, market), day, day)
            received, pages = 0, 0
            codes: set[str] = set()
            seen: set[tuple[str, str]] = set()
            duplicate = 0
            more = True
            total = 0
            try:
                while more and pages < 64:
                    page = source.fetch(query, deadline=min(deadline, started + args.source_loop_seconds))
                    received += len(page.rows)
                    pages += 1
                    total, more = page.total, page.has_more
                    for item in page.rows:
                        for code in item.codes:
                            duplicate += int((item.identity, code) in seen)
                            seen.add((item.identity, code))
                            codes.add(code)
                    query = replace(query, page=query.page + 1)
                status = "complete" if not more and received == total and not duplicate else "partial"
                if status != "complete":
                    failures.append("cninfo:pagination_incomplete")
                samples.append(
                    {
                        "market": market,
                        "pages": pages,
                        "received_rows": received,
                        "reported_rows": total,
                        "distinct_codes": len(codes),
                        "duplicate_links": duplicate,
                        "interface_status": status,
                    }
                )
            except (OSError, ValueError, RuntimeError) as exc:
                failures.append(f"cninfo:{type(exc).__name__}")
                samples.append({"market": market, "pages": pages, "error": type(exc).__name__})
                break
        structured = EastmoneyCompanySource(transport, monotonic=time.monotonic, timeout_seconds=args.timeout_seconds)
        component_samples: list[dict[str, object]] = []
        structured_deadline = min(deadline, time.monotonic() + args.source_loop_seconds)
        for label in ("financial", "financial_baseline", "pledge", "unlock"):
            component = "financial" if label == "financial_baseline" else label
            try:
                if component == "financial":
                    begin, end = now.date() - timedelta(days=550 if label == "financial_baseline" else 7), now.date()
                elif component == "pledge":
                    begin = end = structured.pledge_day(now, deadline=structured_deadline)
                else:
                    begin, end = now.date(), now.date() + timedelta(days=90)
                received = usable = duplicate = pages = 0
                expected: int | None = None
                seen_structured: set[tuple[str, str]] = set()
                while True:
                    page = structured.fetch(
                        StructuredCompanyQuery(component, begin, end, pages + 1),
                        observed_at=now,
                        deadline=structured_deadline,
                    )
                    if expected is not None and page.total != expected:
                        raise ValueError("structured_count_changed")
                    expected = page.total
                    pages += 1
                    received += page.row_count
                    keys = tuple((item.code, item.report_date.isoformat()) for item in page.financials) + tuple(
                        (item.code, item.day.isoformat()) for item in page.ratios
                    )
                    usable += len(keys)
                    for key in keys:
                        duplicate += int(key in seen_structured)
                        seen_structured.add(key)
                    if pages == page.pages:
                        break
                status = "complete" if received == expected and not duplicate else "partial"
                if status != "complete":
                    failures.append(f"{component}:pagination_incomplete")
                component_samples.append(
                    {
                        "component": label,
                        "rows": usable,
                        "raw_rows": received,
                        "total": expected,
                        "pages": pages,
                        "duplicate_keys": duplicate,
                        "interface_status": status,
                    }
                )
            except (OSError, ValueError, RuntimeError) as exc:
                failures.append(f"{component}:{type(exc).__name__}")
                component_samples.append(
                    {
                        "component": label,
                        "error": type(exc).__name__,
                        "reason": str(exc) if isinstance(exc, ValueError) else "transport_failed",
                    }
                )
    return {
        "schema_version": "research-source-comparison",
        "status": "degraded" if failures else "passed",
        "observed_at": now.isoformat(),
        "required_fields": ["announcements", "financial", "pledge", "unlock"],
        "market_pagination": {"day": day.isoformat(), "samples": samples},
        "sources": [
            {
                "source": "cninfo",
                "tested": True,
                "sample_count": len(samples),
                "samples": samples,
                "interface_status": "partial" if failures else "complete",
                "production_role": "market_date_disclosures",
            },
            {
                "source": "eastmoney",
                "tested": True,
                "sample_count": len(component_samples),
                "samples": component_samples,
                "interface_status": "sampled",
                "production_role": "structured_company_updates",
            },
        ],
        "structured_samples": component_samples,
        "failures": failures,
        "budget": {
            "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            "task_limit_seconds": args.task_seconds,
        },
        "selected_route": {
            "primary": "cninfo_market_dates_and_eastmoney_structured",
            "full_year_coverage_verified": False,
            "performance_acceptance": "not_a_full_market_incremental_run",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--codes", nargs="+", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=8)
    parser.add_argument("--source-loop-seconds", type=float, default=120)
    parser.add_argument("--task-seconds", type=float, default=180)
    args = parser.parse_args()
    if not (0 < args.timeout_seconds <= 30 and 0 < args.source_loop_seconds <= 120 and 0 < args.task_seconds <= 600):
        parser.error("diagnostic budgets exceed the bounded source/task limits")
    print(json.dumps(Collect(args), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
