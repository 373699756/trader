"""Pure field recovery decisions; a document receipt never proves field completion."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import date, datetime, timedelta
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
CompanyFieldValue = float | str | date | datetime | None
CompanyFieldState = Literal[
    "present_unverified",
    "not_applicable",
    "pending_download",
    "pending_extraction",
    "pending_publication",
    "verification_required",
    "unsupported",
    "out_of_range",
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


@dataclass(frozen=True, slots=True)
class CompanyFieldResult:
    """Diagnostic projection, never a substitute for field-level source proof.

    ``evidence_id`` identifies the event/recovery context, not proven provenance
    for the projected value. Annual income can have a separate input source.
    """

    code: str
    event_id: str
    evidence_id: str
    field: str
    value: CompanyFieldValue
    state: CompanyFieldState
    applicable: bool
    reason: str
    recovery_action: CompanyFieldAction | None


@dataclass(frozen=True, slots=True)
class _ProjectedField:
    event_id: str
    evidence_id: str
    field: str
    value: CompanyFieldValue
    applicable: bool
    inapplicable_reason: str


def BuildCompanyFieldResults(
    facts: CompanyFacts, recoveries: tuple[CompanyFieldRecovery, ...] = ()
) -> tuple[CompanyFieldResult, ...]:
    """Show current values and applicable obligations without certifying them.

    Existing facts have no page/paragraph and version proof for each field.
    Non-null values therefore remain ``present_unverified``. Recovery plans
    can explain missing values, but cannot prove non-disclosure or add events.
    """
    relevant = tuple(sorted(
        (item for item in recoveries if item.gap.code == facts.code),
        key=lambda item: (item.gap.event_id, item.gap.field, item.gap.evidence_id),
    ))
    keys = tuple((item.gap.event_id, item.gap.field, item.gap.evidence_id) for item in relevant)
    if len(set(keys)) != len(keys):
        raise ValueError("company_field_recovery_ambiguous")
    gaps = BuildCompanyFieldGaps(facts)
    results: list[CompanyFieldResult] = []
    for projected in _projected_fields(facts):
        key = (projected.event_id, projected.field, projected.evidence_id)
        position = bisect_left(keys, key)
        recovery = relevant[position] if position < len(keys) and keys[position] == key else None
        applicable = projected.applicable
        if (not applicable and projected.inapplicable_reason == "action_not_implemented" and recovery is not None
            and recovery.gap.reason == "measurement_missing"):
            # The adapter may have a verified progress/result title while the
            # old missing measurement still leaves the fact in planned state.
            applicable = True
        state: CompanyFieldState
        if not applicable:
            state, reason = "not_applicable", projected.inapplicable_reason
        elif projected.value is not None:
            state, reason = "present_unverified", "field_level_source_proof_missing"
        elif recovery is not None:
            state, reason = _field_result_recovery(recovery)
        else:
            state = "verification_required"
            reason = next((gap.reason for gap in gaps if (
                gap.event_id == projected.event_id and gap.field == projected.field
                and gap.evidence_id == projected.evidence_id
            )), "field_evidence_required")
        results.append(CompanyFieldResult(
            facts.code, projected.event_id, projected.evidence_id, projected.field, projected.value,
            state, applicable, reason, recovery.action if recovery is not None else None,
        ))
    return tuple(results)


def _projected_fields(facts: CompanyFacts) -> tuple[_ProjectedField, ...]:
    fields: list[_ProjectedField] = []
    value: CompanyFieldValue
    for risk in facts.risk_events:
        if risk.risk_type is not CompanyRiskType.MAJOR_SHAREHOLDER_REDUCTION:
            continue
        applicable = risk.expires_at is None or risk.expires_at > facts.checked_at
        plan = risk.reduction_plan
        for field, value in (
            ("start_date", plan.planned_start_date if plan is not None else None),
            ("end_date", plan.planned_end_date if plan is not None else None),
            ("planned_pct", plan.planned_ratio_pct if plan is not None else None),
        ):
            fields.append(_ProjectedField(risk.event_id, risk.evidence_ids[0], field, value, applicable, "risk_expired"))
        completion_applies = applicable and risk.status is CompanyEventStatus.RESOLVED
        fields.append(_ProjectedField(
            risk.event_id, risk.evidence_ids[-1], "completion_disclosed_at",
            plan.completion_disclosed_at if plan is not None else None, completion_applies,
            "risk_expired" if not applicable else "reduction_not_resolved",
        ))
    for event in facts.positive_events:
        applicable = event.status is not CompanyPositiveStatus.CANCELLED
        measurements = event.measurements
        if isinstance(measurements, (RepurchaseMeasurements, IncreaseMeasurements)):
            actual_applies = applicable and event.status is not CompanyPositiveStatus.PLANNED
            fields.append(_ProjectedField(
                event.event_id, event.evidence_ids[-1], "actual_pct", measurements.actual_ratio_pct, actual_applies,
                "event_cancelled" if not applicable else "action_not_implemented",
            ))
            if isinstance(measurements, RepurchaseMeasurements):
                purpose = measurements.purpose.value if measurements.purpose is not RepurchasePurpose.UNKNOWN else None
                fields.append(_ProjectedField(
                    event.event_id, event.evidence_ids[-1], "repurchase_purpose", purpose, applicable, "event_cancelled"
                ))
        elif isinstance(measurements, ContractMeasurements):
            values: tuple[tuple[str, CompanyFieldValue], ...] = (
                ("contract_amount", measurements.contract_amount_cny),
                ("annual_revenue", measurements.reference_annual_revenue_cny),
                ("revenue_report_date", measurements.reference_report_date),
                ("revenue_disclosed_at", measurements.reference_disclosed_at),
                ("contract_revenue_pct", measurements.amount_to_revenue_pct),
                ("amount_basis", measurements.amount_basis if measurements.amount_basis != "unknown" else None),
            )
            for field, value in values:
                fields.append(_ProjectedField(
                    event.event_id, event.evidence_ids[-1], field, value, applicable, "event_cancelled"
                ))
    return tuple(fields)


def _field_result_recovery(recovery: CompanyFieldRecovery) -> tuple[CompanyFieldState, str]:
    if recovery.action == "download":
        return "pending_download", recovery.reason
    if recovery.action in {"extract", "ocr"}:
        return "pending_extraction", recovery.reason
    if recovery.action == "publish":
        return "pending_publication", recovery.reason
    if recovery.action == "unsupported":
        return "unsupported", recovery.reason
    if recovery.action == "out_of_range":
        return "out_of_range", recovery.reason
    if recovery.action in {"reuse", "finished"}:
        return "verification_required", "field_conclusion_without_source_proof"
    return "verification_required", recovery.reason


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
    "CompanyFieldValue",
    "CompanyFieldState",
    "CompanyFieldResult",
    "BuildCompanyFieldResults",
    "BuildCompanyFieldGaps",
    "CompanyFieldIsAvailable",
    "PlanCompanyFieldRecovery",
]
