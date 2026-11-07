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
from trader.download.domain.company_evidence import EvidenceMarket
from trader.download.infra.company_structured_source import EastmoneyCompanySource
from trader.download.domain.company_structured_fields import CompanyStructuredDataset, ResolveCompanyStructuredFields
from trader.download.infra.company_structured_fields import DecodeCompanyStructuredFields
from trader.infra.http_contracts import HttpTransport

_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_SAMPLE_ROWS = 5
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_FINANCIAL_URL = "https://datacenter.eastmoney.com/securities/api/data/get"
_FINANCIAL_REPORT = "RPT_F10_FINANCE_MAINFINADATA"
_FINANCIAL_FIELDS = (
    ("eps", "EPSJB"),
    ("bps", "BPS"),
    ("revenue_growth_pct", "TOTALOPERATEREVETZ"),
    ("profit_growth_pct", "PARENTNETPROFITTZ"),
    ("core_profit_growth_pct", "KCFJCXSYJLRTZ"),
    ("roe_pct", "ROEJQ"),
    ("parent_profit", "PARENTNETPROFIT"),
    ("core_profit", "KCFJCXSYJLR"),
)


@dataclass(frozen=True, slots=True)
class CompanyFinancialRevisionRequest:
    code: str
    report_date: date

    def __post_init__(self) -> None:
        if (
            not isinstance(self.code, str) or re.fullmatch(r"[0-9]{6}", self.code) is None
            or not self.code.startswith(("0", "3", "6", "4", "8", "92"))
            or type(self.report_date) is not date
            or (self.report_date.month, self.report_date.day) not in ((3, 31), (6, 30), (9, 30), (12, 31))
        ):
            raise ValueError("company_financial_revision_request_invalid")


@dataclass(frozen=True, slots=True)
class _FinancialRevisionRecord:
    identity: str
    report_date: date
    notice_at: datetime
    updated_at: datetime
    values: tuple[tuple[str, float | None], ...]


@dataclass(frozen=True, slots=True)
class _FinancialRevisionProductionFields:
    identity: str
    report_date: date
    notice_at: datetime
    values: tuple[tuple[str, float | None], ...]


class _FinancialRevisionProbeError(ValueError):
    """Only this module's fixed protocol reasons may reach the public report."""


def AuditCompanyFinancialRevision(
    session: HttpTransport,
    request: CompanyFinancialRevisionRequest,
    *,
    observed_at: datetime,
    timeout_seconds: float,
    deadline: float,
    clock: Callable[[], datetime] = lambda: datetime.now(_SHANGHAI),
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    """Compare one identity with positive and excluding UPDATE_DATE predicates."""
    if (
        observed_at.utcoffset() is None or request.report_date > observed_at.astimezone(_SHANGHAI).date()
        or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30
        or not math.isfinite(deadline)
    ):
        raise ValueError("company_financial_revision_options_invalid")
    observed_at = observed_at.astimezone(_SHANGHAI)
    started = monotonic()
    identity = request.code + "." + {"sse": "SH", "szse": "SZ", "bse": "BJ"}[EvidenceMarket(request.code)]
    columns = ("SECUCODE", "REPORT_DATE", "NOTICE_DATE", "UPDATE_DATE", *(field for _name, field in _FINANCIAL_FIELDS))
    base_filter = f"(SECUCODE='{identity}')(REPORT_DATE='{request.report_date.isoformat()}')"
    queries: list[dict[str, object]] = []
    projection: dict[str, object] = {
        "source": "eastmoney", "report": _FINANCIAL_REPORT, "code": request.code,
        "report_date": request.report_date.isoformat(), "observed_at": observed_at.isoformat(),
        "read_only": True, "production_adopted": False, "field_correctness_verified": False,
        "full_window_coverage_verified": False, "update_filter_verified": False, "sort_verified": False,
        "support_status": "unverified", "request_limit": 3, "max_response_bytes": 1024 * 1024,
        "request_count": 0, "queries": queries,
    }

    def check() -> None:
        if monotonic() >= deadline:
            raise _FinancialRevisionProbeError("company_financial_revision_deadline_exhausted")

    def fetch(
        label: str, predicate: str = "", *, include_update: bool = True,
    ) -> _FinancialRevisionRecord | _FinancialRevisionProductionFields | None:
        check()
        requested_columns = columns if include_update else tuple(field for field in columns if field != "UPDATE_DATE")
        evidence: dict[str, object] = {"query": label, "status": "failed"}
        queries.append(evidence)
        projection["request_count"] = len(queries)
        response = session.get(
            _FINANCIAL_URL,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"},
            params={
                "type": _FINANCIAL_REPORT, "sty": ",".join(requested_columns), "quoteColumns": "",
                "filter": base_filter + predicate, "p": "1", "ps": "1",
                "sr": "-1,1,-1", "st": "NOTICE_DATE,SECUCODE,REPORT_DATE",
                "source": "HSF10", "client": "PC",
            },
            timeout=min(timeout_seconds, max(0.001, deadline - monotonic())),
            deadline=deadline,
            max_response_bytes=1024 * 1024,
            cancel_requested=lambda: monotonic() >= deadline,
        )
        received_at = clock()
        if received_at.utcoffset() is None:
            raise _FinancialRevisionProbeError("company_financial_revision_clock_invalid")
        evidence.update({
            "http_status": response.status_code, "received_at": received_at.astimezone(_SHANGHAI).isoformat(),
            "response_sha256": hashlib.sha256(response.content).hexdigest(), "response_bytes": len(response.content),
        })
        check()
        if not response.content or len(response.content) > 1024 * 1024:
            raise _FinancialRevisionProbeError("company_financial_revision_response_size_invalid")
        CheckCompanyResponse(response)
        payload = response.json()
        if isinstance(payload, Mapping):
            evidence["provider_code"] = payload.get("code") if type(payload.get("code")) is int else None
            evidence["provider_success"] = payload.get("success") if type(payload.get("success")) is bool else None
        if (
            not isinstance(payload, Mapping) or payload.get("success") is not True
            or type(payload.get("code")) is not int or payload.get("code") != 0
        ):
            raise _FinancialRevisionProbeError("company_financial_revision_provider_rejected")
        result = payload.get("result")
        if not isinstance(result, Mapping):
            raise _FinancialRevisionProbeError("company_financial_revision_result_invalid")
        count, pages, rows = result.get("count"), result.get("pages"), result.get("data")
        if count == 0 and rows is None:
            rows = []
        evidence.update({"reported_count": count if type(count) is int else None, "reported_pages": pages if type(pages) is int else None})
        if (
            type(count) is not int or count not in (0, 1) or type(pages) is not int
            or pages not in (0, 1) or count == 1 and pages != 1
            or not isinstance(rows, list) or len(rows) != count
        ):
            raise _FinancialRevisionProbeError("company_financial_revision_identity_count_invalid")
        evidence["row_count"] = len(rows)
        if not rows:
            evidence["status"] = "empty"
            return None
        row = rows[0]
        if not isinstance(row, Mapping):
            raise _FinancialRevisionProbeError("company_financial_revision_row_invalid")
        missing = tuple(field for field in requested_columns if field not in row)
        if missing:
            evidence["missing_fields"] = missing
            raise _FinancialRevisionProbeError("company_financial_revision_required_fields_missing")
        record = _financial_revision_record(row, request, identity, observed_at) if include_update else _financial_revision_production_fields(row, request, identity, observed_at)
        evidence["status"] = "decoded"
        check()
        return record

    status = "degraded"
    try:
        baseline = fetch("baseline")
        if not isinstance(baseline, _FinancialRevisionRecord):
            raise _FinancialRevisionProbeError("company_financial_revision_record_absent")
        projection["record"] = {
            "identity": baseline.identity, "report_date": baseline.report_date.isoformat(),
            "notice_at": baseline.notice_at.isoformat(), "updated_at": baseline.updated_at.isoformat(),
            "values": dict(baseline.values),
            "missing_value_fields": tuple(name for name, value in baseline.values if value is None),
        }
        boundary = baseline.updated_at.date().isoformat()
        included = fetch("include_update_boundary", f"(UPDATE_DATE>='{boundary}')")
        if included != baseline:
            raise _FinancialRevisionProbeError("company_financial_revision_inclusion_not_reproduced")
        excluded = fetch("exclude_update_boundary", f"(UPDATE_DATE<'{boundary}')")
        if excluded is not None:
            raise _FinancialRevisionProbeError("company_financial_revision_filter_ignored")
        check()
        projection.update({"support_status": "sample_verified", "update_filter_verified": True})
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        status = "failed"
        projection["failure"] = str(exc) if isinstance(exc, _FinancialRevisionProbeError) else type(exc).__name__
        projection["support_status"] = "not_verified"
        if isinstance(exc, _FinancialRevisionProbeError) and str(exc) == "company_financial_revision_provider_rejected" and len(queries) == 1:
            control: dict[str, object] = {"status": "failed", "update_date_requested": False, "revision_verified": False}
            projection["production_fields_control"] = control
            try:
                original = fetch("production_fields_control", include_update=False)
                if isinstance(original, _FinancialRevisionProductionFields):
                    control.update({
                        "status": "decoded", "identity": original.identity, "report_date": original.report_date.isoformat(),
                        "notice_at": original.notice_at.isoformat(), "values": dict(original.values),
                        "missing_value_fields": tuple(name for name, value in original.values if value is None),
                    })
                elif original is None:
                    control["status"] = "empty"
                else:
                    raise _FinancialRevisionProbeError("company_financial_revision_row_invalid")
            except (OSError, RuntimeError, TypeError, ValueError) as control_exc:
                control["failure"] = str(control_exc) if isinstance(control_exc, _FinancialRevisionProbeError) else type(control_exc).__name__
    projection["elapsed_seconds"] = max(0.0, monotonic() - started)
    return {"status": status, "financial_revision_probe": projection}


def _financial_revision_record(
    row: Mapping[str, object],
    request: CompanyFinancialRevisionRequest,
    identity: str,
    observed_at: datetime,
) -> _FinancialRevisionRecord:
    original = _financial_revision_production_fields(row, request, identity, observed_at)
    updated_at = _revision_time(row["UPDATE_DATE"])
    if not original.notice_at <= updated_at <= observed_at:
        raise _FinancialRevisionProbeError("company_financial_revision_time_invalid")
    return _FinancialRevisionRecord(identity, request.report_date, original.notice_at, updated_at, original.values)


def _financial_revision_production_fields(
    row: Mapping[str, object],
    request: CompanyFinancialRevisionRequest,
    identity: str,
    observed_at: datetime,
) -> _FinancialRevisionProductionFields:
    if row["SECUCODE"] != identity or _revision_time(row["REPORT_DATE"]).date() != request.report_date:
        raise _FinancialRevisionProbeError("company_financial_revision_identity_mismatch")
    notice_at = _revision_time(row["NOTICE_DATE"])
    if not request.report_date <= notice_at.date() or notice_at > observed_at:
        raise _FinancialRevisionProbeError("company_financial_revision_time_invalid")
    values = []
    for name, field in _FINANCIAL_FIELDS:
        raw = row[field]
        if raw is None or raw in ("", "-", "--"):
            number = None
        else:
            if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
                raise _FinancialRevisionProbeError("company_financial_revision_number_invalid")
            try:
                number = float(raw)
            except (OverflowError, ValueError) as exc:
                raise _FinancialRevisionProbeError("company_financial_revision_number_invalid") from exc
            if not math.isfinite(number):
                raise _FinancialRevisionProbeError("company_financial_revision_number_invalid")
        values.append((name, number))
    return _FinancialRevisionProductionFields(identity, request.report_date, notice_at, tuple(values))


def _revision_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise _FinancialRevisionProbeError("company_financial_revision_time_invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise _FinancialRevisionProbeError("company_financial_revision_time_invalid") from exc
    return parsed.replace(tzinfo=_SHANGHAI) if parsed.utcoffset() is None else parsed.astimezone(_SHANGHAI)


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


__all__ = ["AuditCompanyAnnualIncome", "AuditCompanyFinancialRevision", "AuditCompanyStructuredSources", "CompanyFinancialRevisionRequest"]
