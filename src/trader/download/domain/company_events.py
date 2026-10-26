"""Immutable, source-verified company events shared through the publication boundary."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum


class CompanyDataState(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class CompanyEvidenceLink:
    evidence_id: str
    title: str
    url: str
    published_at: datetime
    body_checked: bool


class CompanySubject(str, Enum):
    ISSUER = "issuer"
    CONTROLLING_SHAREHOLDER = "controlling_shareholder"
    ACTUAL_CONTROLLER = "actual_controller"
    MAJOR_SHAREHOLDER = "major_shareholder"


class CompanyRiskType(str, Enum):
    REGULATORY_WARNING = "regulatory_warning"
    ADMINISTRATIVE_PENALTY = "administrative_penalty"
    DISCIPLINARY_ACTION = "disciplinary_action"
    FINANCIAL_FRAUD = "financial_fraud"
    OFFICIAL_INVESTIGATION = "official_investigation"
    MAJOR_ILLEGAL = "major_illegal"
    FUND_OCCUPATION = "fund_occupation"
    ILLEGAL_GUARANTEE = "illegal_guarantee"
    FORCED_DELISTING = "forced_delisting"
    MAJOR_SHAREHOLDER_REDUCTION = "major_shareholder_reduction"


class CompanyEventStatus(str, Enum):
    ACTIVE = "active"
    RESOLVED = "resolved"
    CANCELLED = "cancelled"


class CompanySeverityBasis(str, Enum):
    WARNING = "formal_warning"
    ORDINARY = "ordinary_penalty"
    SERIOUS = "serious_penalty"
    HARD = "hard_risk_fact"


class CompanyPositiveType(str, Enum):
    SHARE_REPURCHASE = "share_repurchase"
    SHAREHOLDER_INCREASE = "shareholder_increase"
    OPERATING_CONTRACT = "operating_contract"


class CompanyPositiveStatus(str, Enum):
    PLANNED = "planned"
    IMPLEMENTING = "implementing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class RepurchasePurpose(str, Enum):
    CANCELLATION = "cancellation"
    EMPLOYEE_PLAN = "employee_plan"
    INCENTIVE = "incentive"
    OTHER = "other"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ReductionPlan:
    planned_start_date: date | None = None
    planned_end_date: date | None = None
    planned_ratio_pct: float | None = None
    completion_disclosed_at: datetime | None = None

    def __post_init__(self) -> None:
        _ratio(self.planned_ratio_pct)
        _time(self.completion_disclosed_at)
        if self.planned_start_date and self.planned_end_date and self.planned_start_date > self.planned_end_date:
            raise ValueError("company_reduction_dates_invalid")


@dataclass(frozen=True, slots=True)
class CompanyRiskEvent:
    event_id: str
    risk_type: CompanyRiskType
    risk_level: int
    subject_type: CompanySubject
    severity_basis: CompanySeverityBasis
    case_id: str | None
    first_disclosed_at: datetime
    last_disclosed_at: datetime
    effective_at: datetime
    status: CompanyEventStatus
    resolved_at: datetime | None
    expires_at: datetime | None
    summary: str
    evidence_ids: tuple[str, ...]
    reduction_plan: ReductionPlan | None = None

    def __post_init__(self) -> None:
        _identity(self.event_id, self.evidence_ids)
        if type(self.risk_level) is not int or not 1 <= self.risk_level <= 4:
            raise ValueError("company_risk_level_invalid")
        if (
            self.severity_basis
            is not (
                CompanySeverityBasis.WARNING,
                CompanySeverityBasis.ORDINARY,
                CompanySeverityBasis.SERIOUS,
                CompanySeverityBasis.HARD,
            )[self.risk_level - 1]
        ):
            raise ValueError("company_risk_severity_mismatch")
        soft = self.risk_type in {
            CompanyRiskType.REGULATORY_WARNING,
            CompanyRiskType.ADMINISTRATIVE_PENALTY,
            CompanyRiskType.DISCIPLINARY_ACTION,
        }
        if (
            soft == (self.risk_level == 4)
            or self.risk_type is CompanyRiskType.REGULATORY_WARNING
            and self.risk_level != 1
        ):
            raise ValueError("company_risk_type_level_mismatch")
        for value in (
            self.first_disclosed_at,
            self.last_disclosed_at,
            self.effective_at,
            self.resolved_at,
            self.expires_at,
        ):
            _time(value)
        if self.last_disclosed_at < self.first_disclosed_at or self.effective_at < self.first_disclosed_at:
            raise ValueError("company_risk_dates_invalid")
        if self.resolved_at is not None and self.resolved_at < self.first_disclosed_at:
            raise ValueError("company_risk_resolution_invalid")
        if self.status is not CompanyEventStatus.ACTIVE and self.resolved_at is None:
            raise ValueError("company_risk_resolution_missing")
        if not self.summary or len(self.summary) > 240:
            raise ValueError("company_event_summary_invalid")


@dataclass(frozen=True, slots=True)
class RepurchaseMeasurements:
    actual_ratio_pct: float | None
    purpose: RepurchasePurpose

    def __post_init__(self) -> None:
        _ratio(self.actual_ratio_pct)


@dataclass(frozen=True, slots=True)
class IncreaseMeasurements:
    actual_ratio_pct: float | None

    def __post_init__(self) -> None:
        _ratio(self.actual_ratio_pct)


@dataclass(frozen=True, slots=True)
class ContractMeasurements:
    contract_amount_cny: float | None
    reference_annual_revenue_cny: float | None
    reference_report_date: date | None
    reference_disclosed_at: datetime | None
    amount_to_revenue_pct: float | None
    binding_confirmed: bool
    amount_basis: str

    def __post_init__(self) -> None:
        for value in (self.contract_amount_cny, self.reference_annual_revenue_cny, self.amount_to_revenue_pct):
            if value is not None and (isinstance(value, bool) or not math.isfinite(value) or value < 0):
                raise ValueError("company_contract_measurement_invalid")
        _time(self.reference_disclosed_at)
        if type(self.binding_confirmed) is not bool or self.amount_basis not in {"comparable", "unknown"}:
            raise ValueError("company_contract_basis_invalid")


CompanyMeasurements = RepurchaseMeasurements | IncreaseMeasurements | ContractMeasurements


@dataclass(frozen=True, slots=True)
class CompanyLevelMilestone:
    level: int
    qualified_at: datetime
    measurement_value_pct: float
    evidence_id: str

    def __post_init__(self) -> None:
        _time(self.qualified_at)
        if (
            type(self.level) is not int
            or not 1 <= self.level <= 3
            or not math.isfinite(self.measurement_value_pct)
            or self.measurement_value_pct < 0
            or not self.evidence_id
        ):
            raise ValueError("company_positive_milestone_invalid")


@dataclass(frozen=True, slots=True)
class CompanyPositiveEvent:
    event_id: str
    event_type: CompanyPositiveType
    subject_type: CompanySubject
    status: CompanyPositiveStatus
    first_disclosed_at: datetime
    last_disclosed_at: datetime
    cancelled_at: datetime | None
    positive_level: int
    summary: str
    evidence_ids: tuple[str, ...]
    measurements: CompanyMeasurements
    level_milestones: tuple[CompanyLevelMilestone, ...]
    not_eligible_reason: str | None = None

    def __post_init__(self) -> None:
        _identity(self.event_id, self.evidence_ids)
        for value in (self.first_disclosed_at, self.last_disclosed_at, self.cancelled_at):
            _time(value)
        if type(self.positive_level) is not int or not 0 <= self.positive_level <= 3:
            raise ValueError("company_positive_level_invalid")
        if self.last_disclosed_at < self.first_disclosed_at:
            raise ValueError("company_positive_dates_invalid")
        expected = {
            CompanyPositiveType.SHARE_REPURCHASE: RepurchaseMeasurements,
            CompanyPositiveType.SHAREHOLDER_INCREASE: IncreaseMeasurements,
            CompanyPositiveType.OPERATING_CONTRACT: ContractMeasurements,
        }[self.event_type]
        if not isinstance(self.measurements, expected):
            raise ValueError("company_positive_measurement_type_invalid")
        levels = tuple(item.level for item in self.level_milestones)
        if len(set(levels)) != len(levels) or any(
            item.qualified_at > self.last_disclosed_at for item in self.level_milestones
        ):
            raise ValueError("company_positive_milestones_invalid")
        if self.positive_level and self.positive_level not in levels:
            raise ValueError("company_positive_milestone_missing")
        if self.status is CompanyPositiveStatus.CANCELLED and self.cancelled_at is None:
            raise ValueError("company_positive_cancellation_missing")
        if not self.summary or len(self.summary) > 240:
            raise ValueError("company_event_summary_invalid")


@dataclass(frozen=True, slots=True)
class CompanyReviewReason:
    scope: str
    reason_code: str
    evidence_id: str | None
    detected_at: datetime
    summary: str

    def __post_init__(self) -> None:
        _time(self.detected_at)
        if self.scope not in {"risk", "positive", "financial", "pledge", "unlock"} or not self.reason_code:
            raise ValueError("company_review_reason_invalid")


@dataclass(frozen=True, slots=True)
class CompanyEventPolicy:
    share_thresholds: tuple[float, float, float] = (0.1, 0.3, 0.5)
    contract_thresholds: tuple[float, float, float] = (5.0, 10.0, 20.0)
    risk_days: int = 365
    reduction_tail_days: int = 90
    unlock_days: int = 180

    def __post_init__(self) -> None:
        for thresholds in (self.share_thresholds, self.contract_thresholds):
            if (
                len(thresholds) != 3
                or not all(math.isfinite(x) and x > 0 for x in thresholds)
                or not thresholds[0] < thresholds[1] < thresholds[2]
            ):
                raise ValueError("company_event_thresholds_invalid")
        if min(self.risk_days, self.reduction_tail_days, self.unlock_days) <= 0:
            raise ValueError("company_event_windows_invalid")


def _time(value: datetime | None) -> None:
    if value is not None and value.utcoffset() is None:
        raise ValueError("company_event_time_requires_timezone")


def _ratio(value: float | None) -> None:
    if value is not None and (isinstance(value, bool) or not math.isfinite(value) or not 0 <= value <= 100):
        raise ValueError("company_event_ratio_invalid")


def _identity(identity: str, evidence: tuple[str, ...]) -> None:
    if not identity or not evidence or any(not item for item in evidence) or len(set(evidence)) != len(evidence):
        raise ValueError("company_event_identity_invalid")
