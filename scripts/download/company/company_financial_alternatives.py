"""Bounded Sina/THS report comparisons, without production source admission."""

from __future__ import annotations

import hashlib
import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from scripts.core.projection_values import SafeNonnegativeInt, SafeText
from trader.download.application.company_failures import CompanyFailureCode
from trader.download.domain.company_evidence import CompanySourceFailure, EvidenceMarket
from trader.download.domain.company_source_records import CompanyFinancialField
from trader.download.infra.company_http import CheckCompanyResponse
from trader.infra.http_contracts import HttpAttempt, HttpError, HttpTransport

_FIELDS: tuple[CompanyFinancialField, ...] = (
    "basic_eps", "book_value_per_share", "revenue_growth_pct", "net_profit_growth_pct",
    "core_profit_growth_pct", "roe_pct", "parent_net_profit", "core_net_profit",
)
_THS_FIELDS = (
    "basic_eps", "calc_per_net_assets", "calculate_operating_income_total_yoy_growth_ratio",
    "calculate_parent_holder_net_profit_yoy_growth_ratio", "deduct_net_profit_yoy_growth_ratio",
    "index_weighted_avg_roe", "parent_holder_net_profit", "index_deduct_holder_net_profit",
)


@dataclass(frozen=True, slots=True)
class _CandidateReport:
    code: str
    source: Literal["sina", "ths"]
    report_date: date
    disclosed_on: date | None
    currency: Literal["CNY"] | None
    metrics: tuple[float | None, ...]


def ValidateCompanyFinancialAlternativeCodes(codes: tuple[str, ...]) -> None:
    if not 1 <= len(codes) <= 8 or len(set(codes)) != len(codes):
        raise ValueError("company_financial_alternative_codes_invalid")
    for code in codes:
        EvidenceMarket(code)


def _number(value: object) -> float | None:
    if value is None or value in ("", "--"):
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("company_financial_alternative_number_invalid")
    try:
        result = float(value)
    except ValueError as exc:
        raise ValueError("company_financial_alternative_number_invalid") from exc
    if not math.isfinite(result):
        raise ValueError("company_financial_alternative_number_invalid")
    return result


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("company_financial_alternative_schema_invalid")
    return value


def _period(value: object) -> date:
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
        raise ValueError("company_financial_alternative_period_invalid")
    result = date.fromisoformat(value)
    if (result.month, result.day) not in {(3, 31), (6, 30), (9, 30), (12, 31)}:
        raise ValueError("company_financial_alternative_period_invalid")
    return result


def _sina_reports(payload: Mapping[str, object], code: str) -> tuple[_CandidateReport, ...]:
    root = _mapping(payload.get("result"))
    status = _mapping(root.get("status")).get("code")
    if type(status) is not int or status != 0:
        raise ValueError("company_financial_alternative_provider_rejected")
    reports = _mapping(_mapping(root.get("data")).get("report_list"))
    if not 1 <= len(reports) <= 2:
        raise ValueError("company_financial_alternative_row_limit")
    result = []
    for period, entry in reports.items():
        if re.fullmatch(r"[0-9]{8}", period) is None:
            raise ValueError("company_financial_alternative_period_invalid")
        report_date = _period(f"{period[:4]}-{period[4:6]}-{period[6:]}")
        report = _mapping(entry)
        if report.get("rType") != "合并期末" or report.get("rCurrency") != "CNY":
            raise ValueError("company_financial_alternative_basis_unverified")
        items = report.get("data")
        if not isinstance(items, list) or len(items) > 256:
            raise ValueError("company_financial_alternative_row_limit")
        by_field: dict[tuple[int, str], Mapping[str, object]] = {}
        row: Mapping[str, object] | None
        for item in items:
            row = _mapping(item)
            group, field = row.get("item_group_no"), row.get("item_field")
            if type(group) is not int or not isinstance(field, str) or not field:
                continue
            if (group, field) in by_field:
                raise ValueError("company_financial_alternative_duplicate_metric")
            by_field[group, field] = row
        metrics: list[float | None] = []
        for key in ((1, "EPSBASIC"), (1, "NAPS"), (4, "TAGRT"), (4, "NPGRT")):
            row = by_field.get(key)
            metrics.append(None if row is None else _number(row.get("item_value")))
        core = by_field.get((1, "NPCUT"))
        growth = None if core is None else _number(core.get("item_tongbi"))
        metrics.append(None if growth is None else growth * 100)
        for key in ((1, "ROEWEIGHTED"), (1, "PARENETP"), (1, "NPCUT")):
            if key == (1, "PARENETP"):
                parents = [by_field[1, field] for field in ("PARENETP", "NETPARECOMPPROF") if (1, field) in by_field]
                if len(parents) > 1:
                    raise ValueError("company_financial_alternative_duplicate_metric")
                row = parents[0] if parents else None
                if row is not None and (row.get("item_number"), row.get("item_title"), row.get("item_source")) != ("GJ1", "归母净利润", "lrb"):
                    raise ValueError("company_financial_alternative_basis_unverified")
                metrics.append(None if row is None else _number(row.get("item_value")))
                continue
            row = by_field.get(key)
            metrics.append(None if row is None else _number(row.get("item_value")))
        published = report.get("publish_date")
        if not isinstance(published, str) or re.fullmatch(r"[0-9]{8}", published) is None:
            raise ValueError("company_financial_alternative_notice_invalid")
        disclosed = date.fromisoformat(f"{published[:4]}-{published[4:6]}-{published[6:]}")
        if disclosed < report_date:
            raise ValueError("company_financial_alternative_notice_invalid")
        result.append(_CandidateReport(code, "sina", report_date, disclosed, "CNY", tuple(metrics)))
    return tuple(sorted(result, key=lambda item: item.report_date, reverse=True))


def _ths_reports(payload: Mapping[str, object], code: str) -> tuple[_CandidateReport, ...]:
    status = payload.get("status_code")
    if type(status) is not int or status != 0:
        raise ValueError("company_financial_alternative_provider_rejected")
    data = _mapping(payload.get("data"))
    rows = data.get("data")
    if data.get("page") != 1 or data.get("size") != 2 or not isinstance(rows, list) or not 1 <= len(rows) <= 2:
        raise ValueError("company_financial_alternative_row_limit")
    result = []
    seen: set[date] = set()
    for entry in rows:
        row = _mapping(entry)
        report_date = _period(row.get("date"))
        if report_date in seen or row.get("report") != f"{report_date.year}-{report_date.month // 3}":
            raise ValueError("company_financial_alternative_period_invalid")
        seen.add(report_date)
        indices = _mapping(row.get("index_list"))
        values = tuple(None if field not in indices else _number(_mapping(indices[field]).get("value")) for field in _THS_FIELDS)
        # Only cumulative `value` is comparable. `single` and its growth are not.
        result.append(_CandidateReport(code, "ths", report_date, None, None, values))
    return tuple(sorted(result, key=lambda item: item.report_date, reverse=True))


def AuditCompanyFinancialAlternatives(
    session: HttpTransport, codes: tuple[str, ...], *, observed_at: datetime, timeout_seconds: float,
    deadline: float, http_attempts: Sequence[HttpAttempt], monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    ValidateCompanyFinancialAlternativeCodes(codes)
    if observed_at.utcoffset() is None or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30 or not math.isfinite(deadline):
        raise ValueError("company_financial_alternative_options_invalid")
    started = monotonic()
    samples: list[dict[str, object]] = []
    decoded: list[_CandidateReport] = []
    for code in codes:
        market = EvidenceMarket(code)
        for source in ("sina", "ths"):
            if monotonic() >= deadline:
                break
            sample: dict[str, object] = {"source": source, "code": code, "status": "failed", "report_limit": 2}
            try:
                if source == "sina":
                    url = "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"
                    params = {"paperCode": {"sse": "sh", "szse": "sz", "bse": "bj"}[market] + code,
                              "source": "gjzb", "type": "0", "page": "1", "num": "2"}
                else:
                    url = "https://basic.10jqka.com.cn/basicapi/finance/index/v1/app_data/"
                    params = {"code": code, "id": "client_stock_importance", "market": {"sse": "17", "szse": "33", "bse": "151"}[market],
                              "type": "stock", "page": "1", "size": "2", "period": "0"}
                response = session.get(
                    url, params=params, headers={"User-Agent": "Mozilla/5.0"},
                    timeout=min(timeout_seconds, deadline - monotonic()), deadline=deadline,
                    max_response_bytes=1024 * 1024, cancel_requested=lambda: monotonic() >= deadline,
                )
                CheckCompanyResponse(response)
                payload = _mapping(response.json())
                reports = _sina_reports(payload, code) if source == "sina" else _ths_reports(payload, code)
                if any(item.report_date > observed_at.date() or item.disclosed_on is not None and item.disclosed_on > observed_at.date() for item in reports):
                    raise ValueError("company_financial_alternative_future_report")
                decoded.extend(reports)
                sample.update(status="decoded", response_sha256=hashlib.sha256(response.content).hexdigest(),
                              reports=[{"report_date": item.report_date.isoformat(),
                                        "disclosed_on": item.disclosed_on.isoformat() if item.disclosed_on else None,
                                        "currency": item.currency, "metrics": dict(zip(_FIELDS, item.metrics, strict=True))}
                                       for item in reports])
            except (HttpError, CompanySourceFailure, ValueError) as exc:
                sample["failure"] = CompanyFailureCode(exc) if isinstance(exc, CompanySourceFailure) else (
                    str(exc) if isinstance(exc, ValueError) and str(exc).startswith("company_financial_alternative_") else type(exc).__name__
                )
            samples.append(sample)
    comparisons = []
    for first in decoded:
        if first.source != "sina":
            continue
        other = next((item for item in decoded if item.source == "ths" and (item.code, item.report_date) == (first.code, first.report_date)), None)
        if other is None:
            continue
        comparable = mismatched = 0
        differences = []
        for field, left, right in zip(_FIELDS, first.metrics, other.metrics, strict=True):
            if left is None or right is None:
                continue
            comparable += 1
            tolerance = 0.01 if field in {"parent_net_profit", "core_net_profit"} else 0.005 if field.endswith("_pct") or field == "book_value_per_share" else 0.0001
            if abs(left - right) > tolerance:
                mismatched += 1
                differences.append(field)
        comparisons.append({"code": first.code, "report_date": first.report_date.isoformat(),
                            "comparable_fields": comparable, "mismatched_fields": mismatched, "differences": differences})
    return {
        "probe": "financial_alternatives", "status": "timed_out" if monotonic() >= deadline else (
            "passed" if len(samples) == len(codes) * 2 and all(item["status"] == "decoded" for item in samples) else "degraded"
        ), "read_only": True, "production_adopted": False, "issuer_revision_verified": False,
        "population_coverage_verified": False, "field_correctness_verified": False,
        "security_identity": "request_bound_only", "report_basis": "cumulative",
        "sina_core_growth_precision": "source_fraction_rounded_to_five_decimals",
        "observed_at": observed_at.isoformat(), "request_limit": len(codes) * 2, "report_limit_per_request": 2,
        "physical_http_attempts": len(http_attempts), "response_bytes": sum(item.response_bytes for item in http_attempts),
        "elapsed_seconds": max(0.0, monotonic() - started), "samples": samples, "comparisons": comparisons,
    }


def ProjectCompanyFinancialAlternatives(source: Mapping[str, object]) -> dict[str, object]:
    projected: dict[str, object] = {
        "probe": "financial_alternatives", "read_only": source.get("read_only") is True,
        **{key: False for key in ("production_adopted", "issuer_revision_verified", "population_coverage_verified", "field_correctness_verified")},
        **{key: SafeText(source.get(key)) for key in (
            "status", "observed_at", "runtime_configuration_sha256", "security_identity", "report_basis", "sina_core_growth_precision",
        )},
        **{key: SafeNonnegativeInt(source.get(key)) for key in (
            "request_limit", "report_limit_per_request", "physical_http_attempts", "response_bytes",
        )},
    }
    elapsed = source.get("elapsed_seconds")
    projected["elapsed_seconds"] = elapsed if isinstance(elapsed, (int, float)) and math.isfinite(elapsed) and elapsed >= 0 else None
    samples = source.get("samples")
    safe_samples = []
    for sample in samples[:16] if isinstance(samples, list) else []:
        if not isinstance(sample, Mapping) or sample.get("source") not in ("sina", "ths"):
            continue
        reports = sample.get("reports")
        safe_reports = []
        for report in reports[:2] if isinstance(reports, list) else []:
            if not isinstance(report, Mapping):
                continue
            metrics = report.get("metrics")
            safe_metrics: dict[str, float | None] = {}
            if isinstance(metrics, Mapping):
                for field in _FIELDS:
                    value = metrics.get(field)
                    safe_metrics[field] = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None
            safe_reports.append({**{key: SafeText(report.get(key)) for key in ("report_date", "disclosed_on", "currency")}, "metrics": safe_metrics})
        safe_samples.append({**{key: SafeText(sample.get(key)) for key in ("source", "code", "status", "failure", "response_sha256")}, "reports": safe_reports})
    projected["samples"] = safe_samples
    comparisons = source.get("comparisons")
    safe_comparisons = []
    for comparison in comparisons[:16] if isinstance(comparisons, list) else []:
        if not isinstance(comparison, Mapping):
            continue
        differences = comparison.get("differences")
        safe_comparisons.append({
            **{key: SafeText(comparison.get(key)) for key in ("code", "report_date")},
            **{key: SafeNonnegativeInt(comparison.get(key)) for key in ("comparable_fields", "mismatched_fields")},
            "differences": [field for field in differences[:8] if field in _FIELDS] if isinstance(differences, list) else [],
        })
    projected["comparisons"] = safe_comparisons
    return projected
