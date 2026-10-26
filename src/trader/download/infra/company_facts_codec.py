"""Explicit codecs for the compact publication; JSON exists only at this boundary."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import asdict
from datetime import date, datetime
from enum import Enum
from zoneinfo import ZoneInfo

from trader.download.domain.company_events import (
    CompanyDataState,
    CompanyEventPolicy,
    CompanyEventStatus,
    CompanyLevelMilestone,
    CompanyPositiveEvent,
    CompanyPositiveStatus,
    CompanyPositiveType,
    CompanyReviewReason,
    CompanyRiskEvent,
    CompanyRiskType,
    CompanySeverityBasis,
    CompanySubject,
    ContractMeasurements,
    IncreaseMeasurements,
    ReductionPlan,
    RepurchaseMeasurements,
    RepurchasePurpose,
)
from trader.download.domain.company_evidence import CompanyFinancial, CompanyRatio
from trader.download.domain.company_facts import CompanyFacts

PUBLICATION_COLUMNS = (
    "code",
    "financial_report_date",
    "financial_disclosed_at",
    "financial_state",
    "basic_eps",
    "book_value_per_share",
    "revenue_growth_pct",
    "net_profit_growth_pct",
    "core_profit_growth_pct",
    "roe_pct",
    "parent_net_profit",
    "core_net_profit",
    "pledge_ratio_pct",
    "pledge_as_of",
    "pledge_state",
    "unlock_schedule_json",
    "unlock_checked_at",
    "unlock_coverage_end",
    "unlock_state",
    "risk_events_json",
    "positive_events_json",
    "review_reasons_json",
    "pending_document_count",
    "announcement_coverage_start",
    "announcement_coverage_end",
    "announcement_state",
    "checked_at",
    "published_at",
)
COMPANY_FACTS_SCHEMA = "company_facts"
COMPANY_EXTRACTION_IDENTITY = "official_company_events"

SqlValue = str | float | int | None


def CompanyExtractionIdentity(policy: CompanyEventPolicy) -> str:
    payload = json.dumps(asdict(policy), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return COMPANY_EXTRACTION_IDENTITY + ":" + hashlib.sha256(payload.encode()).hexdigest()


def EncodeCompanyPublication(facts: CompanyFacts) -> tuple[SqlValue, ...]:
    financial = facts.financial
    return (
        facts.code,
        _iso(financial.report_date) if financial else None,
        _iso(financial.published_at) if financial else None,
        facts.financial_state.value,
        financial.basic_eps if financial else None,
        financial.book_value_per_share if financial else None,
        financial.revenue_growth_pct if financial else None,
        financial.net_profit_growth_pct if financial else None,
        financial.core_profit_growth_pct if financial else None,
        financial.roe_pct if financial else None,
        financial.parent_net_profit if financial else None,
        financial.core_net_profit if financial else None,
        facts.pledge.ratio_pct if facts.pledge else None,
        _iso(facts.pledge.day) if facts.pledge else None,
        facts.pledge_state.value,
        _json([{"day": item.day.isoformat(), "ratio_pct": item.ratio_pct} for item in facts.unlock_schedule]),
        _iso(facts.unlock_checked_at),
        _iso(facts.unlock_coverage_end),
        facts.unlock_state.value,
        _json([asdict(item) for item in facts.risk_events]),
        _json([asdict(item) for item in facts.positive_events]),
        _json([asdict(item) for item in facts.review_reasons]),
        facts.pending_document_count,
        _iso(facts.announcement_coverage_start),
        _iso(facts.announcement_coverage_end),
        facts.announcement_state.value,
        facts.checked_at.isoformat(),
        facts.published_at.isoformat(),
    )


def DecodeCompanyPublication(row: Mapping[str, object]) -> CompanyFacts:
    try:
        code = _text(row, "code")
        report_date = _date(row, "financial_report_date")
        financial_at = _datetime(row, "financial_disclosed_at")
        if (report_date is None) != (financial_at is None):
            raise ValueError("company_financial_identity_incomplete")
        financial = (
            CompanyFinancial(
                code,
                report_date,
                financial_at,
                *(
                    _number(row, key)
                    for key in (
                        "basic_eps",
                        "book_value_per_share",
                        "revenue_growth_pct",
                        "net_profit_growth_pct",
                        "core_profit_growth_pct",
                        "roe_pct",
                        "parent_net_profit",
                        "core_net_profit",
                    )
                ),
            )
            if report_date is not None and financial_at is not None
            else None
        )
        pledge_day, pledge_ratio = _date(row, "pledge_as_of"), _number(row, "pledge_ratio_pct")
        if (pledge_day is None) != (pledge_ratio is None):
            raise ValueError("company_pledge_identity_incomplete")
        pledge = (
            CompanyRatio(code, pledge_day, pledge_ratio)
            if pledge_day is not None and pledge_ratio is not None
            else None
        )
        return CompanyFacts(
            code,
            financial,
            CompanyDataState(_text(row, "financial_state")),
            pledge,
            CompanyDataState(_text(row, "pledge_state")),
            tuple(
                CompanyRatio(code, date.fromisoformat(_text(item, "day")), _required_number(item, "ratio_pct"))
                for item in _array(row, "unlock_schedule_json")
            ),
            _datetime(row, "unlock_checked_at"),
            _date(row, "unlock_coverage_end"),
            CompanyDataState(_text(row, "unlock_state")),
            tuple(_risk(item) for item in _array(row, "risk_events_json")),
            tuple(_positive(item) for item in _array(row, "positive_events_json")),
            tuple(
                CompanyReviewReason(
                    _text(item, "scope"),
                    _text(item, "reason_code"),
                    _optional_text(item, "evidence_id"),
                    _required_datetime(item, "detected_at"),
                    _text(item, "summary"),
                )
                for item in _array(row, "review_reasons_json")
            ),
            _integer(row, "pending_document_count"),
            _date(row, "announcement_coverage_start"),
            _date(row, "announcement_coverage_end"),
            CompanyDataState(_text(row, "announcement_state")),
            _required_datetime(row, "checked_at"),
            _required_datetime(row, "published_at"),
        )
    except (KeyError, TypeError, OverflowError) as exc:
        raise ValueError("company_facts_schema_invalid") from exc


def _risk(raw: Mapping[str, object]) -> CompanyRiskEvent:
    plan_raw = raw["reduction_plan"]
    plan = _object(plan_raw) if plan_raw is not None else None
    return CompanyRiskEvent(
        _text(raw, "event_id"),
        CompanyRiskType(_text(raw, "risk_type")),
        _integer(raw, "risk_level"),
        CompanySubject(_text(raw, "subject_type")),
        CompanySeverityBasis(_text(raw, "severity_basis")),
        _optional_text(raw, "case_id"),
        _required_datetime(raw, "first_disclosed_at"),
        _required_datetime(raw, "last_disclosed_at"),
        _required_datetime(raw, "effective_at"),
        CompanyEventStatus(_text(raw, "status")),
        _datetime(raw, "resolved_at"),
        _datetime(raw, "expires_at"),
        _text(raw, "summary"),
        _strings(raw["evidence_ids"]),
        ReductionPlan(
            _date(plan, "planned_start_date"),
            _date(plan, "planned_end_date"),
            _number(plan, "planned_ratio_pct"),
            _datetime(plan, "completion_disclosed_at"),
        )
        if plan is not None
        else None,
    )


def _positive(raw: Mapping[str, object]) -> CompanyPositiveEvent:
    kind = CompanyPositiveType(_text(raw, "event_type"))
    values = _object(raw["measurements"])
    measurements: RepurchaseMeasurements | IncreaseMeasurements | ContractMeasurements
    if kind is CompanyPositiveType.SHARE_REPURCHASE:
        measurements = RepurchaseMeasurements(
            _number(values, "actual_ratio_pct"), RepurchasePurpose(_text(values, "purpose"))
        )
    elif kind is CompanyPositiveType.SHAREHOLDER_INCREASE:
        measurements = IncreaseMeasurements(_number(values, "actual_ratio_pct"))
    else:
        binding = values["binding_confirmed"]
        if type(binding) is not bool:
            raise ValueError("company_contract_boolean_invalid")
        measurements = ContractMeasurements(
            _number(values, "contract_amount_cny"),
            _number(values, "reference_annual_revenue_cny"),
            _date(values, "reference_report_date"),
            _datetime(values, "reference_disclosed_at"),
            _number(values, "amount_to_revenue_pct"),
            binding,
            _text(values, "amount_basis"),
        )
    milestones = raw["level_milestones"]
    if not isinstance(milestones, list):
        raise ValueError("company_milestones_array_required")
    return CompanyPositiveEvent(
        _text(raw, "event_id"),
        kind,
        CompanySubject(_text(raw, "subject_type")),
        CompanyPositiveStatus(_text(raw, "status")),
        _required_datetime(raw, "first_disclosed_at"),
        _required_datetime(raw, "last_disclosed_at"),
        _datetime(raw, "cancelled_at"),
        _integer(raw, "positive_level"),
        _text(raw, "summary"),
        _strings(raw["evidence_ids"]),
        measurements,
        tuple(_milestone(_object(item)) for item in milestones),
        _optional_text(raw, "not_eligible_reason"),
    )


def _milestone(raw: Mapping[str, object]) -> CompanyLevelMilestone:
    return CompanyLevelMilestone(
        _integer(raw, "level"),
        _required_datetime(raw, "qualified_at"),
        _required_number(raw, "measurement_value_pct"),
        _text(raw, "evidence_id"),
    )


def _array(row: Mapping[str, object], key: str) -> tuple[Mapping[str, object], ...]:
    raw = json.loads(_text(row, key))
    if not isinstance(raw, list):
        raise ValueError("company_array_required")
    return tuple(_object(item) for item in raw)


def _object(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("company_object_required")
    return value


def _text(row: Mapping[str, object], key: str) -> str:
    value = row[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"company_text_required:{key}")
    return value


def _optional_text(row: Mapping[str, object], key: str) -> str | None:
    return None if row[key] is None else _text(row, key)


def _number(row: Mapping[str, object], key: str) -> float | None:
    value = row[key]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"company_finite_number_required:{key}")
    return float(value)


def _required_number(row: Mapping[str, object], key: str) -> float:
    value = _number(row, key)
    if value is None:
        raise ValueError(f"company_number_required:{key}")
    return value


def _integer(row: Mapping[str, object], key: str) -> int:
    value = row[key]
    if type(value) is not int:
        raise ValueError(f"company_integer_required:{key}")
    return value


def _date(row: Mapping[str, object], key: str) -> date | None:
    return date.fromisoformat(_text(row, key)) if row[key] is not None else None


def _datetime(row: Mapping[str, object], key: str) -> datetime | None:
    return _aware_datetime(_text(row, key)) if row[key] is not None else None


def _required_datetime(row: Mapping[str, object], key: str) -> datetime:
    value = _datetime(row, key)
    if value is None or value.utcoffset() is None:
        raise ValueError(f"company_aware_time_required:{key}")
    return value


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError("company_string_array_required")
    return tuple(value)


def _aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("company_time_requires_timezone")
    return parsed.astimezone(ZoneInfo("Asia/Shanghai"))


def _iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False, default=_json_scalar)


def _json_scalar(value: object) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return str(value.value)
    raise TypeError("unsupported company codec value")
