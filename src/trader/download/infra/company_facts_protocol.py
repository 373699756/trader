"""Fixed integer protocol for relational company facts; no persisted prose."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import TypeVar
from zoneinfo import ZoneInfo

from trader.download.domain.company_events import (
    CompanyDataState,
    CompanyEventPolicy,
    CompanyEventStatus,
    CompanyPositiveStatus,
    CompanyPositiveType,
    CompanyRiskType,
    CompanySubject,
    RepurchasePurpose,
)

_EnumValue = TypeVar("_EnumValue", bound=Enum)
_EPOCH_DATE = date(1970, 1, 1)
_EPOCH_TIME = datetime(1970, 1, 1, tzinfo=timezone.utc)
_SHANGHAI = ZoneInfo("Asia/Shanghai")
COMPANY_EXTRACTION_IDENTITY = "relational_company_facts:1:subject_bound"

DATA_STATES = {CompanyDataState.UNAVAILABLE: 0, CompanyDataState.PARTIAL: 1, CompanyDataState.COMPLETE: 2}
RISK_TYPES = {
    CompanyRiskType.REGULATORY_WARNING: 1,
    CompanyRiskType.ADMINISTRATIVE_PENALTY: 2,
    CompanyRiskType.DISCIPLINARY_ACTION: 3,
    CompanyRiskType.FINANCIAL_FRAUD: 4,
    CompanyRiskType.OFFICIAL_INVESTIGATION: 5,
    CompanyRiskType.MAJOR_ILLEGAL: 6,
    CompanyRiskType.FUND_OCCUPATION: 7,
    CompanyRiskType.ILLEGAL_GUARANTEE: 8,
    CompanyRiskType.FORCED_DELISTING: 9,
    CompanyRiskType.MAJOR_SHAREHOLDER_REDUCTION: 10,
}
SUBJECTS = {
    CompanySubject.ISSUER: 1,
    CompanySubject.CONTROLLING_SHAREHOLDER: 2,
    CompanySubject.ACTUAL_CONTROLLER: 3,
    CompanySubject.MAJOR_SHAREHOLDER: 4,
}
RISK_STATUSES = {CompanyEventStatus.ACTIVE: 1, CompanyEventStatus.RESOLVED: 2, CompanyEventStatus.CANCELLED: 3}
POSITIVE_TYPES = {
    CompanyPositiveType.SHARE_REPURCHASE: 1,
    CompanyPositiveType.SHAREHOLDER_INCREASE: 2,
    CompanyPositiveType.OPERATING_CONTRACT: 3,
}
POSITIVE_STATUSES = {
    CompanyPositiveStatus.PLANNED: 1,
    CompanyPositiveStatus.IMPLEMENTING: 2,
    CompanyPositiveStatus.COMPLETED: 3,
    CompanyPositiveStatus.CANCELLED: 4,
}
REPURCHASE_PURPOSES = {
    RepurchasePurpose.UNKNOWN: 0,
    RepurchasePurpose.CANCELLATION: 1,
    RepurchasePurpose.EMPLOYEE_PLAN: 2,
    RepurchasePurpose.INCENTIVE: 3,
    RepurchasePurpose.OTHER: 4,
}
REVIEW_SCOPES = {"risk": 1, "positive": 2, "financial": 3, "pledge": 4, "unlock": 5}
EVIDENCE_SOURCES = {
    "cninfo": 1,
    "sse": 2,
    "szse": 3,
    "bse": 4,
    "issuer_disclosure": 5,
    "exchange_disclosure": 6,
    "regulator_disclosure": 7,
    "court_disclosure": 8,
    "judicial_disclosure": 9,
    "csrc_disclosure": 10,
}
REVIEW_REASONS = {
    "document_pending": 1,
    "official_source_unverified": 2,
    "subject_unverified": 3,
    "conclusion_unverified": 4,
    "case_conclusion_ambiguous": 5,
    "resolution_unlinked": 6,
    "revision_pending": 7,
    "cancellation_unlinked": 8,
    "progress_unlinked": 9,
    "implementation_or_measurement_unverified": 10,
    "necessary_data_missing": 11,
    "not_current": 12,
    "source_stale": 13,
    "window_incomplete": 14,
    "initial_risk_state_unverified": 15,
    "source_task_pending": 16,
    "source_unavailable": 17,
    "access_denied": 18,
    "rate_limited": 19,
    "timeout": 20,
    "schema_invalid": 21,
    "coverage_mismatch": 22,
    "filter_ignored": 23,
    "source_changed": 24,
    "extraction_unsupported": 25,
}


def CompanyExtractionHash(policy: CompanyEventPolicy) -> bytes:
    payload = json.dumps(asdict(policy), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256((COMPANY_EXTRACTION_IDENTITY + ":" + payload).encode()).digest()


def EncodeCompanyDate(value: date | None) -> int | None:
    return (value - _EPOCH_DATE).days if value is not None else None


def DecodeCompanyDate(value: object) -> date | None:
    return _EPOCH_DATE + timedelta(days=CompanyInteger(value)) if value is not None else None


def EncodeCompanyTime(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.utcoffset() is None:
        raise ValueError("company_time_requires_timezone")
    delta = value.astimezone(timezone.utc) - _EPOCH_TIME
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def DecodeCompanyTime(value: object) -> datetime | None:
    return (
        (_EPOCH_TIME + timedelta(microseconds=CompanyInteger(value))).astimezone(_SHANGHAI)
        if value is not None
        else None
    )


def RequireCompanyTime(value: object) -> datetime:
    result = DecodeCompanyTime(value)
    if result is None:
        raise ValueError("company_time_required")
    return result


def CompanyInteger(value: object) -> int:
    if type(value) is not int:
        raise ValueError("company_integer_required")
    return value


def CompanyNumber(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("company_finite_number_required")
    return float(value)


def CompanyIdentifier(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 240 or any(ord(char) < 32 for char in value):
        raise ValueError("company_identifier_invalid")
    return value


def DecodeCompanyEnum(mapping: dict[_EnumValue, int], value: object) -> _EnumValue:
    number = CompanyInteger(value)
    for member, code in mapping.items():
        if code == number:
            return member
    raise ValueError("company_enum_unknown")


def DecodeCompanyLabel(mapping: dict[str, int], value: object) -> str:
    number = CompanyInteger(value)
    for label, code in mapping.items():
        if code == number:
            return label
    raise ValueError("company_enum_unknown")


def EncodeCompanyReason(reason: str) -> int:
    """Classify bounded supplier failures, rejecting unknown business reason names."""
    if reason in REVIEW_REASONS:
        return REVIEW_REASONS[reason]
    aliases = {
        "unavailable": 11,
        "pending": 16,
        "source_stopped": 17,
        "HttpConnectionError": 17,
        "ConnectionError": 17,
        "OSError": 17,
        "RuntimeError": 17,
        "ValueError": 21,
        "TimeoutError": 20,
        "connection_error": 17,
        "network_error": 17,
        "http_error": 17,
        "invalid_json": 21,
        "empty_response": 17,
    }
    if reason in aliases:
        return aliases[reason]
    if reason.startswith("http_") and reason[5:].isdigit():
        status = int(reason[5:])
        if not 400 <= status <= 599:
            raise ValueError("company_http_failure_invalid")
        return 18 if status in (401, 403) else 19 if status == 429 else 20 if status in (408, 504) else 17
    if reason.startswith(("cninfo_", "company_", "disclosure_", "structured_", "financial_", "pledge_", "news_")):
        if reason.endswith("filter_ignored"):
            return 23
        if reason.endswith(("partition_changed", "count_changed", "source_changed")):
            return 24
        if reason.endswith("timeout"):
            return 20
        if reason.endswith(("unavailable", "missing")):
            return 17
        if reason.endswith(("no_text", "oversized", "unreadable", "encrypted", "page_limit", "unsplittable")):
            return 25
        if reason.endswith(("incomplete", "inconsistent", "truncated", "duplicate_page_links", "coverage_mismatch")):
            return 22
        if reason.endswith(("invalid", "nonfinite", "out_of_range", "unverified", "mismatch", "future")):
            return 21
    raise ValueError("company_review_reason_unknown:" + reason)


__all__ = [
    "COMPANY_EXTRACTION_IDENTITY",
    "DATA_STATES",
    "RISK_TYPES",
    "SUBJECTS",
    "RISK_STATUSES",
    "POSITIVE_TYPES",
    "POSITIVE_STATUSES",
    "REPURCHASE_PURPOSES",
    "REVIEW_SCOPES",
    "REVIEW_REASONS",
    "EVIDENCE_SOURCES",
    "CompanyExtractionHash",
    "EncodeCompanyDate",
    "DecodeCompanyDate",
    "EncodeCompanyTime",
    "DecodeCompanyTime",
    "RequireCompanyTime",
    "CompanyInteger",
    "CompanyNumber",
    "CompanyIdentifier",
    "DecodeCompanyEnum",
    "DecodeCompanyLabel",
    "EncodeCompanyReason",
]
