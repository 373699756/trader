"""Collection-only structured linkage candidates; no PDF or body text is stored."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime
from enum import Enum

from trader.download.domain.company_event_extraction import CompanyLinkCandidate
from trader.download.domain.company_events import (
    CompanyLevelMilestone, CompanyPositiveEvent, CompanyPositiveStatus, CompanyPositiveType,
    CompanyRiskType, CompanySubject, ContractMeasurements, IncreaseMeasurements, RepurchaseMeasurements, RepurchasePurpose,
)


def EncodeCompanyLink(candidate: CompanyLinkCandidate) -> str:
    def encode(value: object) -> str:
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, Enum):
            return str(value.value)
        raise TypeError("company_link_value_invalid")
    return json.dumps(asdict(candidate), default=encode, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def DecodeCompanyLink(payload: str) -> CompanyLinkCandidate:
    # JSON is restricted to this persistence boundary; constructors validate the
    # numeric and temporal domain contracts after decoding.
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("company_link_candidate_invalid")
    positive = value["positive"]
    event = None
    if positive is not None:
        if not isinstance(positive, dict):
            raise ValueError("company_link_positive_invalid")
        kind = CompanyPositiveType(positive["event_type"])
        fields = positive["measurements"]
        measurement: RepurchaseMeasurements | IncreaseMeasurements | ContractMeasurements
        if kind is CompanyPositiveType.SHARE_REPURCHASE:
            measurement = RepurchaseMeasurements(fields["actual_ratio_pct"], RepurchasePurpose(fields["purpose"]))
        elif kind is CompanyPositiveType.SHAREHOLDER_INCREASE:
            measurement = IncreaseMeasurements(fields["actual_ratio_pct"])
        else:
            measurement = ContractMeasurements(
                fields["contract_amount_cny"], fields["reference_annual_revenue_cny"],
                date.fromisoformat(fields["reference_report_date"]) if fields["reference_report_date"] else None,
                datetime.fromisoformat(fields["reference_disclosed_at"]) if fields["reference_disclosed_at"] else None,
                fields["amount_to_revenue_pct"], fields["binding_confirmed"], fields["amount_basis"],
            )
        event = CompanyPositiveEvent(
            positive["event_id"], kind, CompanySubject(positive["subject_type"]), CompanyPositiveStatus(positive["status"]),
            datetime.fromisoformat(positive["first_disclosed_at"]), datetime.fromisoformat(positive["last_disclosed_at"]),
            datetime.fromisoformat(positive["cancelled_at"]) if positive["cancelled_at"] else None,
            positive["positive_level"], positive["summary"], tuple(positive["evidence_ids"]), measurement,
            tuple(CompanyLevelMilestone(m["level"], datetime.fromisoformat(m["qualified_at"]), m["measurement_value_pct"], m["evidence_id"])
                  for m in positive["level_milestones"]), positive["not_eligible_reason"],
        )
    return CompanyLinkCandidate(
        value["evidence_id"], datetime.fromisoformat(value["published_at"]), CompanySubject(value["subject"]),
        tuple(value["references"]), tuple(value["cases"]), tuple(CompanyRiskType(v) for v in value["closures"]), event, value["cancellation"], tuple(value["supersedes"]),
    )


__all__ = ["DecodeCompanyLink", "EncodeCompanyLink"]
