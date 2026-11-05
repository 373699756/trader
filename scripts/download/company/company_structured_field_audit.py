"""Bounded, read-only samples of structured alternatives to company PDF extraction."""

from __future__ import annotations

import hashlib
import math
import re
import time
from collections.abc import Callable, Mapping
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from trader.download.infra.company_http import CheckCompanyResponse
from trader.download.domain.company_annual_income import CompanyAnnualIncomeRequest
from trader.download.infra.company_structured_source import EastmoneyCompanySource
from trader.download.domain.company_structured_fields import CompanyStructuredDataset, ResolveCompanyStructuredFields
from trader.download.infra.company_structured_fields import DecodeCompanyStructuredFields
from trader.infra.http_contracts import HttpTransport

_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_SAMPLE_ROWS = 5
_SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class _FieldSource:
    name: str
    report: str
    code_column: str
    date_column: str
    numeric_fields: tuple[str, ...]
    evidence_fields: tuple[str, ...]
    unresolved: tuple[str, ...]


_SOURCES = (
    _FieldSource(
        "repurchase",
        "RPTA_WEB_GETHGLIST_NEW",
        "DIM_SCODE",
        "UPDATEDATE",
        ("REPURNUM", "REPURAMOUNT", "REPURNUMLOWER", "REPURNUMCAP", "ZSZXX", "ZSZSX"),
        ("REPUROBJECTIVE", "REPURPROGRESS", "REPURCODE", "DIM_TRADEDATE"),
        ("actual_total_capital_ratio", "purpose_enum", "plan_identity", "historical_revisions"),
    ),
    _FieldSource(
        "holder_trade",
        "RPT_SHARE_HOLDER_INCREASE",
        "SECURITY_CODE",
        "NOTICE_DATE",
        ("CHANGE_NUM", "CHANGE_NUM_SYMBOL", "AFTER_CHANGE_RATE", "CHANGE_FREE_RATIO", "HOLD_RATIO"),
        ("HOLDER_NAME", "DIRECTION", "START_DATE", "END_DATE"),
        ("holder_qualification", "cumulative_event_identity", "quantity_unit", "historical_revisions"),
    ),
    _FieldSource(
        "annual_income",
        "RPT_DMSK_FN_INCOME",
        "SECURITY_CODE",
        "NOTICE_DATE",
        ("OPERATE_INCOME", "TOTAL_OPERATE_INCOME"),
        ("REPORT_DATE", "REPORT_TYPE_CODE", "DATA_STATE"),
        ("original_annual_revision", "actual_disclosure_time", "currency", "tax_basis"),
    ),
    _FieldSource(
        "contract",
        "RPTA_WEB_ZDHT_LIST",
        "SECURITYCODE",
        "DIM_RDATE",
        ("AMOUNTS", "SNDYYSR", "ZSNDYYSRBL", "OPERATEREVE"),
        ("SIGNATORY", "SIGNATORYRELNAME", "SIGNDATE", "SIGNEFFECT", "ISABOLISHED", "YEAR", "CONTRACTNAME", "COUNTERPARTY", "UPDATEDATE"),
        ("contract_identity", "binding_effect", "revenue_original_year", "currency", "tax_basis", "historical_revisions"),
    ),
)


def AuditCompanyAnnualIncome(
    session: HttpTransport,
    request: CompanyAnnualIncomeRequest,
    *,
    observed_at: datetime,
    timeout_seconds: float,
    deadline: float,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    """Exercise the production decoder for explicit periods without opening a database."""
    if observed_at.utcoffset() is None or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30:
        raise ValueError("company_annual_probe_options_invalid")
    if not math.isfinite(deadline) or monotonic() >= deadline:
        raise ValueError("company_annual_probe_deadline_invalid")
    started = monotonic()
    result: dict[str, object] = {
        "schema_version": "company-annual-income-source", "status": "degraded", "read_only": True,
        "source": "eastmoney_f10", "code": request.code,
        "requested_periods": [day.isoformat() for day in request.report_dates],
        "field_correctness_verified": False, "full_window_coverage_verified": False,
    }
    source = EastmoneyCompanySource(
        session, monotonic=monotonic, timeout_seconds=min(timeout_seconds, deadline - started),
        cancel_requested=lambda: monotonic() >= deadline,
    )
    try:
        incomes = source.annual_income(request, observed_at=observed_at)
        result.update({"status": "decoded", "records": [
            {
                "report_date": row.report_date.isoformat(), "disclosed_at": row.disclosed_at.isoformat(),
                "updated_at": row.updated_at.isoformat(), "currency": row.currency,
                "operating_revenue": row.operating_revenue, "record_hash": row.record_hash,
            } for row in incomes
        ]})
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        result["failure"] = type(exc).__name__
    result["elapsed_seconds"] = max(0.0, monotonic() - started)
    return result


def AuditCompanyStructuredSources(
    session: HttpTransport,
    *,
    observed_at: datetime,
    timeout_seconds: float,
    deadline: float,
    sample_rows: int = _SAMPLE_ROWS,
    discover_schema: bool = False,
    include_identities: bool = False,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    """Sample one page per source; report schemas and counts, without production writes.

    A successful sample proves only that the requested schema was returned in
    this bounded request. It does not qualify values for scoring, certify the
    source's complete coverage, or establish a historical point-in-time view.
    """
    if observed_at.utcoffset() is None or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30:
        raise ValueError("company_field_probe_options_invalid")
    if not math.isfinite(deadline):
        raise ValueError("company_field_probe_deadline_invalid")
    if type(sample_rows) is not int or not 1 <= sample_rows <= 500:
        raise ValueError("company_field_probe_sample_limit_invalid")
    if type(discover_schema) is not bool or discover_schema and sample_rows != 1:
        raise ValueError("company_field_probe_schema_limit_invalid")
    if type(include_identities) is not bool or include_identities and sample_rows > 5:
        raise ValueError("company_field_probe_identity_limit_invalid")
    observed_at = observed_at.astimezone(_SHANGHAI)
    started = monotonic()
    begin, end = observed_at.date() - timedelta(days=365), observed_at.date()
    samples: list[dict[str, object]] = []
    for source in _SOURCES:
        sample: dict[str, object] = {
            "source": "eastmoney", "dataset": source.name, "report": source.report,
            "status": "skipped", "requests": 0, "sample_limit": sample_rows,
            "unresolved_semantics": source.unresolved,
        }
        samples.append(sample)
        if monotonic() >= deadline:
            sample["reason"] = "task_deadline"
            continue
        filters = f"({source.date_column}>='{begin.isoformat()}')({source.date_column}<='{end.isoformat()}')"
        if source.name == "annual_income":
            filters += f"(REPORT_DATE='{end.year - 1}-12-31')"
        before = monotonic()
        try:
            sample["requests"] = 1
            columns = (source.code_column, source.date_column, *source.numeric_fields, *source.evidence_fields)
            response = session.get(
                _URL,
                params={
                    "reportName": source.report, "columns": "ALL" if discover_schema else ",".join(columns), "pageNumber": "1",
                    "pageSize": str(sample_rows), "sortColumns": source.date_column,
                    "sortTypes": "-1", "filter": filters, "source": "WEB", "client": "WEB",
                },
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"},
                timeout=min(timeout_seconds, max(0.001, deadline - before)),
                deadline=deadline,
                max_response_bytes=1024 * 1024,
                cancel_requested=lambda: monotonic() >= deadline,
            )
            CheckCompanyResponse(response)
            payload = response.json()
            sample.update(_sample_summary(
                source,
                payload,
                begin,
                end,
                limit=sample_rows,
            ))
            result = payload.get("result") if isinstance(payload, Mapping) else None
            if include_identities and isinstance(result, Mapping):
                sample["identity_samples"] = _identity_samples(result.get("data"), columns, sample_rows)
            if discover_schema and isinstance(result, Mapping):
                rows = result.get("data")
                if isinstance(rows, list) and len(rows) <= 1:
                    keys = {key for row in rows if isinstance(row, Mapping) for key in row}
                    if len(keys) > 256 or any(not isinstance(key, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", key) is None for key in keys):
                        raise ValueError("company_field_probe_schema_invalid")
                    sample["returned_fields"] = sorted(keys)
            sample["response_bytes"] = len(response.content)
            sample["response_sha256"] = hashlib.sha256(response.content).hexdigest()
            if source.name != "annual_income":
                sample["typed_fields"] = _typed_fields(
                    source.name,
                    payload,
                    observed_at,
                    begin,
                    end,
                    sample_rows,
                    hashlib.sha256(response.content).hexdigest(),
                )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            # Provider error messages may contain payloads; expose categories only.
            sample.update({"status": "failed", "failure": type(exc).__name__})
        sample["seconds"] = max(0.0, monotonic() - before)
    return {
        "schema_version": "company-structured-field-sampling",
        "status": "passed" if all(row["status"] == "sampled" for row in samples) else "degraded",
        "observed_at": observed_at.isoformat(), "read_only": True, "production_adopted": False,
        "field_correctness_verified": False, "full_window_coverage_verified": False,
        "window_start": begin.isoformat(), "window_end": end.isoformat(),
        "request_count_basis": "adapter invocations; transport retries may add physical attempts",
        "schema_discovery_only": discover_schema,
        "sources": samples, "elapsed_seconds": max(0.0, monotonic() - started),
    }


def _typed_fields(
    dataset: str,
    payload: object,
    observed_at: datetime,
    begin: date,
    end: date,
    sample_rows: int,
    response_hash: str,
) -> dict[str, object]:
    """Keep schema sampling separate from adoption and field correctness.

    No event proof is invented from a matching security, date or supplier key.
    Existing HTTP owns the request; decoding never starts another market scan.
    """
    try:
        page = DecodeCompanyStructuredFields(
            CompanyStructuredDataset(dataset),
            payload,
            observed_at=observed_at,
            window_start=begin,
            window_end=end,
            page_size=sample_rows,
            response_hash=response_hash,
        )
        resolutions = ResolveCompanyStructuredFields(page.observations, observed_at)
    except (TypeError, ValueError) as exc:
        return {"status": "rejected", "failure": type(exc).__name__, "production_adopted": False}
    return {
        "status": "decoded",
        "observations": len(page.observations),
        "reasons": dict(Counter(result.reason.value for result in resolutions)),
        "production_adopted": False,
        "field_correctness_verified": False,
    }


def _sample_summary(
    source: _FieldSource,
    payload: object,
    begin: date,
    end: date,
    *,
    limit: int,
) -> dict[str, object]:
    if (
        not isinstance(payload, Mapping) or type(payload.get("code")) is not int
        or payload.get("code") != 0 or payload.get("success") is not True
    ):
        raise ValueError("company_field_probe_provider_rejected")
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise ValueError("company_field_probe_result_invalid")
    count, pages, rows = result.get("count"), result.get("pages"), result.get("data")
    if (
        isinstance(count, bool) or not isinstance(count, int) or count < 0
        or isinstance(pages, bool) or not isinstance(pages, int) or pages < 0
        or not isinstance(rows, list) or len(rows) > limit
        or len(rows) > count or count > 0 and pages < 1
        or any(not isinstance(row, Mapping) for row in rows)
    ):
        raise ValueError("company_field_probe_pagination_invalid")
    fields = source.numeric_fields + source.evidence_fields
    required = (source.code_column, source.date_column, *source.numeric_fields)
    missing = tuple(field for field in required if any(field not in row for row in rows))
    outside = 0
    for row in rows:
        raw_date = row.get(source.date_column)
        if not isinstance(raw_date, str):
            raise ValueError("company_field_probe_date_invalid")
        day = datetime.fromisoformat(raw_date).date()
        outside += int(not begin <= day <= end)
        if source.name == "annual_income":
            report_date = row.get("REPORT_DATE")
            if not isinstance(report_date, str):
                raise ValueError("company_field_probe_report_date_invalid")
            outside += int(report_date[:10] != f"{end.year - 1}-12-31")
    return {
        "status": "empty" if not rows else "sampled" if not missing and not outside else "invalid",
        "rows": len(rows), "reported_count": count, "reported_pages": pages,
        "missing_required_fields": missing, "outside_requested_window": outside,
        "fields": [
            {
                "field": field, "present_rows": sum(field in row for row in rows),
                "non_null_rows": sum(row.get(field) is not None for row in rows),
                "finite_numeric_rows": sum(_finite_number(row.get(field)) for row in rows)
                if field in source.numeric_fields else None,
            }
            for field in fields
        ],
    }


def _identity_samples(value: object, columns: tuple[str, ...], limit: int) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    samples: list[dict[str, object]] = []
    for row in value[:limit]:
        if not isinstance(row, Mapping):
            continue
        projected: dict[str, object] = {}
        for key in columns:
            field = row.get(key)
            if isinstance(field, str):
                projected[key] = field[:160]
            elif field is None or _finite_number(field):
                projected[key] = field
        samples.append(projected)
    return samples


def _finite_number(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


__all__ = ["AuditCompanyAnnualIncome", "AuditCompanyStructuredSources"]
