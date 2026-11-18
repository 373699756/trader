"""Read-only whole-window reconciliation through the production source decoder."""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from scripts.core.projection_values import SafeNonnegativeInt, SafeText
from trader.download.application.company_failures import CompanyFailureCode
from trader.download.domain.company_evidence import CompanyInputValidationFailure, CompanySourceFailure, StructuredCompanyQuery
from trader.download.domain.download_failure import DownloadValidationFailure
from trader.download.domain.company_source_records import CompanyFinancialSourcePage, CompanyFinancialSourceValues
from trader.download.infra.company_structured_source import EastmoneyCompanySource
from trader.infra.http_contracts import HttpAttempt, HttpTransport

_PAGE_LIMIT = 256


def BuildCompanyStructuredWindowQuery(
    component: Literal["financial", "pledge", "unlock"], start: date | None, end: date | None,
    *, observed_at: datetime,
) -> StructuredCompanyQuery:
    if observed_at.utcoffset() is None or type(start) is not date or type(end) is not date:
        raise ValueError("company_structured_window_dates_required")
    today = observed_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
    valid = (today <= start <= end <= today + timedelta(days=180)) if component == "unlock" else (
        today - timedelta(days=365) <= start <= end <= today
    )
    if not valid or component == "pledge" and start != end:
        raise ValueError("company_structured_window_dates_invalid")
    return StructuredCompanyQuery(component, start, end)


def AuditCompanyStructuredWindow(
    session: HttpTransport, query: StructuredCompanyQuery, *, observed_at: datetime,
    timeout_seconds: float, deadline: float, http_attempts: Sequence[HttpAttempt],
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    """Reconcile all pages without claiming issuer revisions or absent-stock coverage."""
    bounded = BuildCompanyStructuredWindowQuery(query.component, query.start, query.end, observed_at=observed_at)
    if query != bounded or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30 or not math.isfinite(deadline):
        raise ValueError("company_structured_window_options_invalid")
    started = monotonic()
    report: dict[str, object] = {
        "probe": "structured_window", "status": "skipped", "source": "eastmoney", "read_only": True,
        "component": query.component, "observed_at": observed_at.isoformat(),
        "window_start": query.start.isoformat(), "window_end": query.end.isoformat(),
        "production_adopted": False, "issuer_revision_verified": False, "population_coverage_verified": False,
        "pagination_verified": False, "page_limit": _PAGE_LIMIT, "page_size": 500,
        "fetch_invocations": 0, "received_pages": 0, "raw_rows": 0, "decoded_rows": 0,
        "duplicate_keys": 0, "source_null_fields": 0, "source_updated_at_unknown_rows": 0,
    }
    expected: tuple[int, int] | None = None
    seen: set[tuple[str, date]] = set()
    codes: set[str] = set()
    missing: dict[str, int] = {}
    hashes: list[str] = []
    raw_count = decoded_count = received = duplicates = unknown = calls = 0
    try:
        for number in range(1, _PAGE_LIMIT + 1):
            remaining = deadline - monotonic()
            if remaining <= 0:
                report["status"] = "timed_out"
                break
            source = EastmoneyCompanySource(
                session, monotonic=monotonic, timeout_seconds=min(timeout_seconds, remaining),
                cancel_requested=lambda: monotonic() >= deadline,
            )
            cursor = replace(query, page=number)
            calls += 1
            keys: tuple[tuple[str, date], ...]
            financial: CompanyFinancialSourcePage | None = None
            if query.component == "financial":
                financial = source.financial_observations(cursor, observed_at=observed_at)
                pages, total, rows = financial.pages, financial.total, financial.raw_count
                keys = tuple(item.business_key for item in financial.observations)
            else:
                page = source.fetch(cursor, observed_at=observed_at)
                pages, total, rows = page.pages, page.total, page.row_count
                keys = tuple((item.code, item.day) for item in page.ratios)
            if pages > _PAGE_LIMIT or rows > 500 or expected is not None and expected != (pages, total):
                raise ValueError("company_structured_window_pagination_changed")
            expected = pages, total
            received += 1
            raw_count += rows
            decoded_count += len(keys)
            if financial is not None:
                hashes.append(financial.response_hash)
                for item in financial.observations:
                    unknown += int(item.source_updated_at is None)
                    for field, value in CompanyFinancialSourceValues(item.financial):
                        missing[field] = missing.get(field, 0) + int(value is None)
            for key in keys:
                duplicates += int(key in seen)
                seen.add(key)
                codes.add(key[0])
            if duplicates:
                raise ValueError("company_structured_window_duplicate_key")
            if number < pages and rows != 500 or number == pages and raw_count != total:
                raise ValueError("company_structured_window_count_mismatch")
            if monotonic() >= deadline:
                report["status"] = "timed_out"
                break
            if number == pages:
                report.update(status="passed", pagination_verified=True)
                break
        else:
            raise ValueError("company_structured_window_page_limit")
    except (OSError, RuntimeError, ValueError) as exc:
        reason = CompanyFailureCode(exc) if isinstance(exc, (CompanySourceFailure, CompanyInputValidationFailure, DownloadValidationFailure)) else (
            str(exc) if isinstance(exc, ValueError) and str(exc).startswith("company_structured_window_") else type(exc).__name__
        )
        report.update(status="timed_out" if monotonic() >= deadline else "failed", failure=reason)
    report.update(
        fetch_invocations=calls, received_pages=received, raw_rows=raw_count, decoded_rows=decoded_count,
        reported_pages=expected[0] if expected else None, reported_total=expected[1] if expected else None,
        distinct_codes=len(codes), duplicate_keys=duplicates, source_null_fields=sum(missing.values()),
        source_updated_at_unknown_rows=unknown,
        fields=[{"field": field, "source_null_rows": count} for field, count in sorted(missing.items())],
        response_hashes=hashes, physical_http_attempts=len(http_attempts),
        response_bytes=sum(attempt.response_bytes for attempt in http_attempts),
        elapsed_seconds=max(0.0, monotonic() - started),
    )
    return report


def ProjectCompanyStructuredWindow(source: Mapping[str, object]) -> dict[str, object]:
    """Allow only bounded diagnostic metadata to leave the common tool adapter."""
    projected: dict[str, object] = {key: source.get(key) is True for key in (
        "read_only", "pagination_verified",
    )}
    projected.update({key: False for key in (
        "production_adopted", "issuer_revision_verified", "population_coverage_verified", "pagination_verified",
    ) if key != "pagination_verified"})
    projected.update({key: SafeText(source.get(key)) for key in (
        "probe", "status", "source", "component", "observed_at", "window_start", "window_end", "failure", "runtime_configuration_sha256",
    )})
    projected.update({key: SafeNonnegativeInt(source.get(key)) for key in (
        "page_limit", "page_size", "fetch_invocations", "received_pages", "raw_rows", "decoded_rows", "duplicate_keys",
        "source_null_fields", "source_updated_at_unknown_rows", "reported_pages", "reported_total", "distinct_codes",
        "physical_http_attempts", "response_bytes",
    )})
    elapsed = source.get("elapsed_seconds")
    projected["elapsed_seconds"] = elapsed if isinstance(elapsed, (float, int)) and math.isfinite(elapsed) and elapsed >= 0 else None
    fields = source.get("fields")
    projected["fields"] = [{"field": SafeText(item.get("field")), "source_null_rows": SafeNonnegativeInt(item.get("source_null_rows"))}
                           for item in (fields[:8] if isinstance(fields, list) else []) if isinstance(item, Mapping)]
    hashes = source.get("response_hashes")
    projected["response_hashes"] = [digest for digest in (hashes[:_PAGE_LIMIT] if isinstance(hashes, list) else [])
                                    if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) is not None]
    return projected
