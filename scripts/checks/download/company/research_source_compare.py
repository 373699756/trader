"""Read-only qualification of production company evidence source adapters."""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from trader.bootstrap import OpenExternalHttp
from trader.download.domain.company_evidence import DisclosurePartition, StructuredCompanyQuery
from trader.download.domain.company_annual_income import CompanyAnnualIncomeRequest
from trader.download.infra.company_disclosure_source import CninfoMarketDisclosureSource
from trader.download.infra.company_structured_source import EastmoneyCompanySource
from trader.download.infra.download_settings import LoadCompanySyncConfiguration
from scripts.checks.download.company.company_structured_field_audit import AuditCompanyAnnualIncome, AuditCompanyStructuredSources

_SHANGHAI = ZoneInfo("Asia/Shanghai")


def Collect(args: argparse.Namespace) -> dict[str, object]:
    configuration = LoadCompanySyncConfiguration(Path(__file__).resolve().parents[4])
    started = time.monotonic()
    deadline = started + args.task_seconds
    now = datetime.now(_SHANGHAI)
    day = now.date() - timedelta(days=3)
    samples: list[dict[str, object]] = []
    failures: list[str] = []
    with OpenExternalHttp() as session:
        transport = session
        announcement_deadline = min(deadline, started + args.source_loop_seconds)
        source = CninfoMarketDisclosureSource(
            transport,
            monotonic=time.monotonic,
            sleeper=time.sleep,
            timeout_seconds=args.timeout_seconds,
            document_download_timeout_seconds=configuration.document_download_timeout_seconds,
            document_parse_timeout_seconds=configuration.document_parse_timeout_seconds,
            request_interval_seconds=configuration.request_interval_seconds,
            document_limits=configuration.document_limits,
            extended_document_limits=configuration.extended_document_limits,
            cancel_requested=lambda: time.monotonic() >= announcement_deadline,
        )
        for market in ("sse", "szse", "bse"):
            query = DisclosurePartition(market, day, day)
            received, pages = 0, 0
            codes: set[str] = set()
            seen: set[tuple[str, str]] = set()
            duplicate = 0
            more = True
            total = 0
            try:
                while more and pages < 64:
                    page = source.fetch(query)
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
        structured_deadline = min(deadline, time.monotonic() + args.source_loop_seconds)
        structured = EastmoneyCompanySource(
            transport,
            monotonic=time.monotonic,
            timeout_seconds=args.timeout_seconds,
            cancel_requested=lambda: time.monotonic() >= structured_deadline,
        )
        component_samples: list[dict[str, object]] = []
        labels: tuple[Literal["financial", "pledge", "unlock", "financial_baseline"], ...] = (
            "financial",
            "pledge",
            "unlock",
            "financial_baseline",
        )
        for label in labels:
            component: Literal["financial", "pledge", "unlock"] = (
                "financial" if label == "financial_baseline" else label
            )
            try:
                if component == "financial":
                    begin, end = now.date() - timedelta(days=550 if label == "financial_baseline" else 7), now.date()
                elif component == "pledge":
                    begin = end = structured.pledge_day(now)
                else:
                    begin, end = now.date(), now.date() + timedelta(days=90)
                received = usable = duplicate = pages = 0
                expected: int | None = None
                seen_structured: set[tuple[str, str]] = set()
                while True:
                    structured_page = structured.fetch(
                        StructuredCompanyQuery(component, begin, end, pages + 1),
                        observed_at=now,
                    )
                    if expected is not None and structured_page.total != expected:
                        raise ValueError("structured_count_changed")
                    expected = structured_page.total
                    pages += 1
                    received += structured_page.row_count
                    keys = tuple(
                        (item.code, item.report_date.isoformat()) for item in structured_page.financials
                    ) + tuple((item.code, item.day.isoformat()) for item in structured_page.ratios)
                    usable += len(keys)
                    for key in keys:
                        duplicate += int(key in seen_structured)
                        seen_structured.add(key)
                    if pages == structured_page.pages:
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


def Main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--codes", nargs="+", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=8)
    parser.add_argument("--source-loop-seconds", type=float, default=120)
    parser.add_argument("--task-seconds", type=float, default=180)
    parser.add_argument("--field-sources-only", action="store_true", help="Read one bounded page of structured company field alternatives; no database writes.")
    parser.add_argument("--field-source-rows", type=int, default=5, help="Rows per structured field sample (1..500); requires --field-sources-only.")
    parser.add_argument("--field-source-schema", action="store_true", help="Discover column names from exactly one row per source; requires --field-sources-only and --field-source-rows 1. Values remain projected.")
    parser.add_argument("--annual-income-period", type=date.fromisoformat, help="Read one explicit annual period with the production F10 decoder; requires exactly one --codes value.")
    args = parser.parse_args()
    if not (0 < args.timeout_seconds <= 30 and 0 < args.source_loop_seconds <= 120 and 0 < args.task_seconds <= 600):
        parser.error("diagnostic budgets exceed the bounded source/task limits")
    if not 1 <= args.field_source_rows <= 500 or args.field_source_rows != 5 and not args.field_sources_only:
        parser.error("--field-source-rows requires --field-sources-only and a value within 1..500")
    if args.field_source_schema and (not args.field_sources_only or args.field_source_rows != 1):
        parser.error("--field-source-schema requires --field-sources-only and --field-source-rows 1")
    if args.annual_income_period is not None and (args.field_sources_only or len(args.codes) != 1):
        parser.error("--annual-income-period requires one code and cannot be combined with --field-sources-only")
    if args.annual_income_period is not None:
        try:
            request = CompanyAnnualIncomeRequest(args.codes[0], (args.annual_income_period,))
        except ValueError:
            parser.error("annual income requires an A-share code and a December 31 period")
        with OpenExternalHttp() as session:
            report = AuditCompanyAnnualIncome(
                session, request, observed_at=datetime.now(_SHANGHAI), timeout_seconds=args.timeout_seconds,
                deadline=time.monotonic() + args.task_seconds,
            )
    elif args.field_sources_only:
        with OpenExternalHttp() as session:
            report = AuditCompanyStructuredSources(
                session,
                observed_at=datetime.now(_SHANGHAI),
                timeout_seconds=args.timeout_seconds,
                deadline=time.monotonic() + args.task_seconds,
                sample_rows=args.field_source_rows,
                discover_schema=args.field_source_schema,
            )
    else:
        report = Collect(args)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0
