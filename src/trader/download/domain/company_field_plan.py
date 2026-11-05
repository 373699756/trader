"""Pure field recovery decisions; a document receipt never proves field completion."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from trader.download.domain.company_events import (
    CompanyEventStatus,
    CompanyPositiveStatus,
    CompanyRiskType,
    ContractMeasurements,
    IncreaseMeasurements,
    RepurchaseMeasurements,
    RepurchasePurpose,
)
from trader.download.domain.company_evidence import EvidenceMarket
from trader.download.domain.company_facts import CompanyFacts

CompanyFieldReason = Literal[
    "available",
    "not_applicable",
    "not_disclosed",
    "implementation_missing",
    "measurement_missing",
    "dependency_missing",
    "ambiguous",
]
CompanyFieldAction = Literal[
    "reuse", "finished", "publish", "extract", "ocr", "download", "verify", "unsupported", "out_of_range"
]


@dataclass(frozen=True, slots=True)
class CompanyFieldGap:
    """A field conclusion supplied by an adapter or derived from an existing fact."""

    code: str
    event_id: str
    evidence_id: str
    field: str
    reason: CompanyFieldReason

    def __post_init__(self) -> None:
        EvidenceMarket(self.code)
        if not self.event_id or not self.evidence_id or not self.field:
            raise ValueError("company_field_identity_missing")


@dataclass(frozen=True, slots=True)
class CompanyFieldMaterial:
    """Current-version material whose physical availability the adapter has checked.

    ``text_available`` means usable complete text, not a hash or partial OCR.
    ``result_available`` requires validated candidates for the requested fields;
    neither a document's ``valid`` receipt nor its checked flag establishes it.
    """

    evidence_id: str
    published_at: datetime
    text_available: bool
    pdf_available: bool
    ocr_pending: bool = False
    result_available: bool = False

    def __post_init__(self) -> None:
        if not self.evidence_id or self.published_at.utcoffset() is None:
            raise ValueError("company_field_material_identity_invalid")
        if any(type(value) is not bool for value in (
            self.text_available, self.pdf_available, self.ocr_pending, self.result_available
        )):
            raise ValueError("company_field_material_availability_invalid")


@dataclass(frozen=True, slots=True)
class CompanyFieldRecovery:
    gap: CompanyFieldGap
    action: CompanyFieldAction
    reason: str


def BuildCompanyFieldGaps(facts: CompanyFacts) -> tuple[CompanyFieldGap, ...]:
    """Identify applicable missing event values without inferring source disclosure.

    Existing facts lack field-level evidence of non-disclosure. Consequently a
    null value here is only a measurement/dependency gap, never proof that its
    source omitted the value. Planned actions do not require an actual ratio.
    """
    gaps: list[CompanyFieldGap] = []
    for risk in facts.risk_events:
        if risk.risk_type is not CompanyRiskType.MAJOR_SHAREHOLDER_REDUCTION:
            continue
        if risk.expires_at is not None and risk.expires_at <= facts.checked_at:
            continue
        plan = risk.reduction_plan
        for field, missing in (
            ("start_date", plan is None or plan.planned_start_date is None),
            ("end_date", plan is None or plan.planned_end_date is None),
            ("planned_pct", plan is None or plan.planned_ratio_pct is None),
        ):
            if missing:
                gaps.append(CompanyFieldGap(
                    facts.code, risk.event_id, risk.evidence_ids[0], field, "measurement_missing"
                ))
        if risk.status is CompanyEventStatus.RESOLVED and (plan is None or plan.completion_disclosed_at is None):
            gaps.append(CompanyFieldGap(
                facts.code, risk.event_id, risk.evidence_ids[-1], "completion_disclosed_at", "dependency_missing"
            ))
    for event in facts.positive_events:
        if event.status is CompanyPositiveStatus.CANCELLED:
            continue
        measurements = event.measurements
        if isinstance(measurements, (RepurchaseMeasurements, IncreaseMeasurements)):
            if event.status is not CompanyPositiveStatus.PLANNED and measurements.actual_ratio_pct is None:
                gaps.append(CompanyFieldGap(
                    facts.code, event.event_id, event.evidence_ids[-1], "actual_pct", "measurement_missing"
                ))
            if isinstance(measurements, RepurchaseMeasurements) and measurements.purpose is RepurchasePurpose.UNKNOWN:
                gaps.append(CompanyFieldGap(
                    facts.code, event.event_id, event.evidence_ids[-1], "repurchase_purpose", "measurement_missing"
                ))
        elif isinstance(measurements, ContractMeasurements):
            contract_fields: tuple[tuple[str, bool, CompanyFieldReason], ...] = (
                ("contract_amount", measurements.contract_amount_cny is None, "measurement_missing"),
                ("annual_revenue", measurements.reference_annual_revenue_cny is None, "dependency_missing"),
                ("revenue_report_date", measurements.reference_report_date is None, "measurement_missing"),
                ("revenue_disclosed_at", measurements.reference_disclosed_at is None, "dependency_missing"),
                ("contract_revenue_pct", measurements.amount_to_revenue_pct is None, "dependency_missing"),
                ("amount_basis", measurements.amount_basis == "unknown", "ambiguous"),
            )
            for field, missing, reason in contract_fields:
                if missing:
                    gaps.append(CompanyFieldGap(
                        facts.code, event.event_id, event.evidence_ids[-1], field, reason
                    ))
    return tuple(gaps)


def CompanyFieldIsAvailable(facts: CompanyFacts, event_id: str, field: str) -> bool:
    """Require an actual value on the same event before retiring an older task.

    An absent gap can also mean an expired, cancelled or unlinked event. None
    of those conditions proves that a newer input supplied the requested value.
    """
    for risk in facts.risk_events:
        if risk.event_id != event_id or risk.reduction_plan is None:
            continue
        plan = risk.reduction_plan
        return any(name == field and available for name, available in (
            ("start_date", plan.planned_start_date is not None),
            ("end_date", plan.planned_end_date is not None),
            ("planned_pct", plan.planned_ratio_pct is not None),
            ("completion_disclosed_at", plan.completion_disclosed_at is not None),
        ))
    for event in facts.positive_events:
        if event.event_id != event_id:
            continue
        measurements = event.measurements
        if isinstance(measurements, (RepurchaseMeasurements, IncreaseMeasurements)):
            if field == "actual_pct":
                return measurements.actual_ratio_pct is not None
            return (
                field == "repurchase_purpose" and isinstance(measurements, RepurchaseMeasurements)
                and measurements.purpose is not RepurchasePurpose.UNKNOWN
            )
        if isinstance(measurements, ContractMeasurements):
            return any(name == field and available for name, available in (
                ("contract_amount", measurements.contract_amount_cny is not None),
                ("annual_revenue", measurements.reference_annual_revenue_cny is not None),
                ("revenue_report_date", measurements.reference_report_date is not None),
                ("revenue_disclosed_at", measurements.reference_disclosed_at is not None),
                ("contract_revenue_pct", measurements.amount_to_revenue_pct is not None),
                ("amount_basis", measurements.amount_basis == "comparable"),
            ))
    return False


def PlanCompanyFieldRecovery(
    gaps: tuple[CompanyFieldGap, ...],
    materials: tuple[CompanyFieldMaterial, ...],
    observed_at: datetime,
) -> tuple[CompanyFieldRecovery, ...]:
    """Choose the shortest justified local step before any in-window download.

    The input holds only current source versions. Duplicate identities are
    rejected instead of silently selecting a document version. Missing source
    metadata cannot authorize a request; it requires verification first.
    """
    if observed_at.utcoffset() is None:
        raise ValueError("company_field_plan_time_invalid")
    ordered_materials = tuple(sorted(materials, key=lambda item: item.evidence_id))
    evidence_ids = tuple(material.evidence_id for material in ordered_materials)
    if len(set(evidence_ids)) != len(evidence_ids):
        raise ValueError("company_field_material_version_ambiguous")
    recoveries: list[CompanyFieldRecovery] = []
    for gap in gaps:
        position = bisect_left(evidence_ids, gap.evidence_id)
        material = (
            ordered_materials[position]
            if position < len(evidence_ids) and evidence_ids[position] == gap.evidence_id
            else None
        )
        action, reason = _recovery_step(gap, material, observed_at)
        recoveries.append(CompanyFieldRecovery(gap, action, reason))
    return tuple(recoveries)


def _recovery_step(
    gap: CompanyFieldGap,
    material: CompanyFieldMaterial | None,
    observed_at: datetime,
) -> tuple[CompanyFieldAction, str]:
    if gap.reason == "available":
        return "reuse", "verified_value_available"
    if gap.reason in {"not_applicable", "not_disclosed"}:
        return "finished", gap.reason
    if gap.reason == "implementation_missing":
        return "unsupported", "implementation_missing"
    if gap.reason == "ambiguous":
        return "verify", "field_verification_required"
    if material is None:
        return "verify", "source_metadata_missing"
    if material.published_at > observed_at:
        return "verify", "source_after_cutoff"
    if material.result_available:
        return "publish", "verified_candidate_available"
    if gap.reason == "dependency_missing":
        return "verify", "dependency_evidence_required"
    if material.text_available:
        return "extract", "local_text_available"
    if material.pdf_available:
        return ("ocr", "local_ocr_pending") if material.ocr_pending else ("extract", "local_pdf_available")
    if material.published_at.astimezone(observed_at.tzinfo).date() < observed_at.date() - timedelta(days=365):
        return "out_of_range", "source_outside_collection_window"
    return "download", "local_source_missing"


__all__ = [
    "CompanyFieldReason",
    "CompanyFieldAction",
    "CompanyFieldGap",
    "CompanyFieldMaterial",
    "CompanyFieldRecovery",
    "BuildCompanyFieldGaps",
    "CompanyFieldIsAvailable",
    "PlanCompanyFieldRecovery",
]
