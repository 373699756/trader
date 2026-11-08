"""Field gaps select local recovery without treating every null as a download."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from trader.download.domain.company_events import (
    CompanyDataState,
    CompanyEventStatus,
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
from trader.download.domain.company_facts import CompanyFacts
from trader.download.domain.company_field_plan import (
    BuildCompanyFieldGaps,
    BuildCompanyFieldResults,
    CompanyFieldGap,
    CompanyFieldMaterial,
    CompanyFieldIsAvailable,
    CompanyFieldRecovery,
    PlanCompanyFieldRecovery,
)

AT = datetime(2026, 11, 4, 15, tzinfo=ZoneInfo("Asia/Shanghai"))


def _facts(*, risks=(), positives=()):
    return CompanyFacts(
        code="600001",
        financial=None,
        financial_state=CompanyDataState.UNAVAILABLE,
        pledge=None,
        pledge_state=CompanyDataState.UNAVAILABLE,
        unlock_schedule=(),
        unlock_checked_at=None,
        unlock_coverage_end=None,
        unlock_state=CompanyDataState.UNAVAILABLE,
        risk_events=risks,
        positive_events=positives,
        review_reasons=(),
        pending_document_count=0,
        announcement_coverage_start=None,
        announcement_coverage_end=None,
        announcement_state=CompanyDataState.PARTIAL,
        checked_at=AT,
        published_at=AT,
        latest_financial=None,
    )


def _risk(identity="reduction"):
    return CompanyRiskEvent(
        event_id=identity,
        risk_type=CompanyRiskType.MAJOR_SHAREHOLDER_REDUCTION,
        risk_level=4,
        subject_type=CompanySubject.MAJOR_SHAREHOLDER,
        severity_basis=CompanySeverityBasis.HARD,
        case_id=None,
        first_disclosed_at=AT - timedelta(days=10),
        last_disclosed_at=AT,
        effective_at=AT - timedelta(days=10),
        status=CompanyEventStatus.ONGOING,
        resolved_at=None,
        expires_at=None,
        summary="大股东减持",
        evidence_ids=(identity,),
        reduction_plan=ReductionPlan(),
    )


def _positive(identity="increase"):
    return CompanyPositiveEvent(
        event_id=identity,
        event_type=CompanyPositiveType.SHAREHOLDER_INCREASE,
        subject_type=CompanySubject.MAJOR_SHAREHOLDER,
        status=CompanyPositiveStatus.IMPLEMENTING,
        first_disclosed_at=AT - timedelta(days=10),
        last_disclosed_at=AT,
        cancelled_at=None,
        positive_level=0,
        summary="大股东增持",
        evidence_ids=(identity,),
        measurements=IncreaseMeasurements(None),
        level_milestones=(),
    )


def _gap(reason="measurement_missing"):
    return CompanyFieldGap("600001", "increase", "notice", "actual_pct", reason)


def _material(**changes):
    return replace(CompanyFieldMaterial("notice", AT, False, False), **changes)


@pytest.mark.parametrize(
    ("changes", "action", "reason"),
    [
        ({"result_available": True, "text_available": True, "pdf_available": True}, "publish", "verified_candidate_available"),
        ({"text_available": True, "pdf_available": True, "ocr_pending": True}, "extract", "local_text_available"),
        ({"pdf_available": True}, "extract", "local_pdf_available"),
        ({"pdf_available": True, "ocr_pending": True}, "ocr", "local_ocr_pending"),
        ({}, "download", "local_source_missing"),
    ],
)
def test_shortest_recovery_uses_verified_material(changes, action, reason):
    result = PlanCompanyFieldRecovery((_gap(),), (_material(**changes),), AT)[0]
    assert (result.action, result.reason) == (action, reason)


@pytest.mark.parametrize("days,expected", [(365, "download"), (366, "out_of_range")])
def test_network_window_is_inclusive_and_uses_source_date(days, expected):
    material = _material(published_at=AT - timedelta(days=days))
    assert PlanCompanyFieldRecovery((_gap(),), (material,), AT)[0].action == expected


def test_window_compares_disclosure_dates_in_the_observation_timezone():
    midnight = AT.replace(hour=0)
    source = (midnight - timedelta(days=365)).astimezone(timezone.utc)
    material = _material(published_at=source)
    assert PlanCompanyFieldRecovery((_gap(),), (material,), midnight)[0].action == "download"


@pytest.mark.parametrize("changes", [{"text_available": True}, {"pdf_available": True}, {"result_available": True}])
def test_existing_local_evidence_is_usable_beyond_network_window(changes):
    material = _material(published_at=AT - timedelta(days=500), **changes)
    result = PlanCompanyFieldRecovery((_gap(),), (material,), AT)[0]
    assert result.action in {"extract", "publish"}


@pytest.mark.parametrize(
    "reason,action",
    [
        ("available", "reuse"),
        ("not_applicable", "finished"),
        ("not_disclosed", "finished"),
        ("implementation_missing", "unsupported"),
        ("ambiguous", "verify"),
        ("dependency_missing", "verify"),
    ],
)
def test_field_conclusions_do_not_trigger_unhelpful_downloads(reason, action):
    result = PlanCompanyFieldRecovery((_gap(reason),), (_material(),), AT)[0]
    assert result.action == action


def test_unknown_or_future_source_metadata_requires_verification():
    assert PlanCompanyFieldRecovery((_gap(),), (), AT)[0].reason == "source_metadata_missing"
    future = _material(published_at=AT + timedelta(seconds=1), text_available=True)
    assert PlanCompanyFieldRecovery((_gap(),), (future,), AT)[0].reason == "source_after_cutoff"


def test_ambiguous_versions_and_naive_times_are_rejected():
    with pytest.raises(ValueError, match="version_ambiguous"):
        PlanCompanyFieldRecovery((_gap(),), (_material(), _material()), AT)
    with pytest.raises(ValueError, match="time_invalid"):
        PlanCompanyFieldRecovery((), (), AT.replace(tzinfo=None))
    with pytest.raises(ValueError, match="material_identity_invalid"):
        _material(published_at=AT.replace(tzinfo=None))


def test_reduction_gaps_keep_distinct_events_and_known_values():
    first = replace(_risk("first"), reduction_plan=ReductionPlan(AT.date(), None, 1.2))
    second = _risk("second")
    gaps = BuildCompanyFieldGaps(_facts(risks=(first, second)))
    assert [(gap.event_id, gap.field) for gap in gaps] == [
        ("first", "end_date"),
        ("second", "start_date"),
        ("second", "end_date"),
        ("second", "planned_pct"),
    ]
    assert all(gap.reason == "measurement_missing" for gap in gaps)


def test_expired_reduction_needs_no_current_recovery_but_unresolved_old_risk_does():
    expired = replace(_risk("expired"), expires_at=AT)
    ongoing = replace(_risk("ongoing"), first_disclosed_at=AT - timedelta(days=500))
    assert {gap.event_id for gap in BuildCompanyFieldGaps(_facts(risks=(expired, ongoing)))} == {"ongoing"}


def test_reduction_plan_gaps_use_original_plan_and_completion_uses_latest_evidence():
    event = replace(
        _risk(),
        status=CompanyEventStatus.RESOLVED,
        resolved_at=AT,
        evidence_ids=("original-plan", "progress", "completion"),
    )
    gaps = BuildCompanyFieldGaps(_facts(risks=(event,)))
    assert [(gap.field, gap.evidence_id) for gap in gaps] == [
        ("start_date", "original-plan"),
        ("end_date", "original-plan"),
        ("planned_pct", "original-plan"),
        ("completion_disclosed_at", "completion"),
    ]


def test_actual_ratio_only_applies_to_implemented_share_actions():
    planned = replace(_positive("planned"), status=CompanyPositiveStatus.PLANNED)
    cancelled = replace(_positive("cancelled"), status=CompanyPositiveStatus.CANCELLED, cancelled_at=AT)
    implemented = _positive("implemented")
    gaps = BuildCompanyFieldGaps(_facts(positives=(planned, cancelled, implemented)))
    assert [(gap.event_id, gap.field) for gap in gaps] == [("implemented", "actual_pct")]


def test_nonqualifying_repurchase_still_keeps_known_actual_measurement():
    event = replace(
        _positive(),
        event_type=CompanyPositiveType.SHARE_REPURCHASE,
        measurements=RepurchaseMeasurements(0.3, RepurchasePurpose.EMPLOYEE_PLAN),
        not_eligible_reason="implementation_or_measurement_unverified",
    )
    assert BuildCompanyFieldGaps(_facts(positives=(event,))) == ()


def test_contract_known_amount_is_preserved_and_missing_dependencies_require_verification():
    event = replace(
        _positive(),
        event_type=CompanyPositiveType.OPERATING_CONTRACT,
        measurements=ContractMeasurements(100_000_000, None, None, None, None, True, "unknown"),
    )
    gaps = BuildCompanyFieldGaps(_facts(positives=(event,)))
    assert {gap.field for gap in gaps} == {
        "annual_revenue", "revenue_report_date", "revenue_disclosed_at", "contract_revenue_pct", "amount_basis"
    }
    assert all(recovery.action == "verify" for recovery in PlanCompanyFieldRecovery(gaps, (), AT))


def test_field_availability_requires_value_on_the_same_event_including_zero():
    measured = replace(_positive(), measurements=IncreaseMeasurements(0))
    facts = _facts(positives=(measured,))
    assert CompanyFieldIsAvailable(facts, measured.event_id, "actual_pct")
    assert not CompanyFieldIsAvailable(facts, "other-event", "actual_pct")
    assert not CompanyFieldIsAvailable(facts, measured.event_id, "repurchase_purpose")
    assert not CompanyFieldIsAvailable(facts, measured.event_id, "unsupported-field")
    cancelled = replace(_positive(), status=CompanyPositiveStatus.CANCELLED, cancelled_at=AT)
    assert BuildCompanyFieldGaps(_facts(positives=(cancelled,))) == ()
    assert not CompanyFieldIsAvailable(_facts(positives=(cancelled,)), cancelled.event_id, "actual_pct")


def test_available_reduction_fields_do_not_imply_missing_completion_or_another_event():
    risk = replace(_risk(), reduction_plan=ReductionPlan(AT.date(), AT.date(), 2))
    facts = _facts(risks=(risk,))
    assert all(CompanyFieldIsAvailable(facts, risk.event_id, field) for field in (
        "start_date", "end_date", "planned_pct",
    ))
    assert not CompanyFieldIsAvailable(facts, risk.event_id, "completion_disclosed_at")
    assert not CompanyFieldIsAvailable(facts, "other-event", "planned_pct")


def test_contract_amount_does_not_complete_revenue_or_measurement_basis():
    event = replace(
        _positive(), event_type=CompanyPositiveType.OPERATING_CONTRACT,
        measurements=ContractMeasurements(100_000_000, None, None, None, None, True, "unknown"),
    )
    facts = _facts(positives=(event,))
    assert CompanyFieldIsAvailable(facts, event.event_id, "contract_amount")
    assert all(not CompanyFieldIsAvailable(facts, event.event_id, field) for field in (
        "annual_revenue", "revenue_report_date", "revenue_disclosed_at", "contract_revenue_pct", "amount_basis",
    ))


def test_repurchase_actual_ratio_and_purpose_are_independent_fields():
    event = replace(
        _positive(), event_type=CompanyPositiveType.SHARE_REPURCHASE,
        measurements=RepurchaseMeasurements(0.3, RepurchasePurpose.UNKNOWN),
    )
    facts = _facts(positives=(event,))
    assert CompanyFieldIsAvailable(facts, event.event_id, "actual_pct")
    assert not CompanyFieldIsAvailable(facts, event.event_id, "repurchase_purpose")


def test_field_results_keep_old_nonnull_values_unverified_including_zero_and_original_dates():
    risk = replace(_risk(), reduction_plan=ReductionPlan(AT.date(), AT.date(), 0))
    event = replace(_positive(), measurements=IncreaseMeasurements(0))
    results = BuildCompanyFieldResults(_facts(risks=(risk,), positives=(event,)))
    present = tuple(result for result in results if result.state == "present_unverified")
    assert {(result.field, result.value) for result in present} == {
        ("start_date", AT.date()), ("end_date", AT.date()), ("planned_pct", 0), ("actual_pct", 0),
    }
    assert all(result.applicable and result.reason == "field_level_source_proof_missing" for result in present)
    completion = next(result for result in results if result.field == "completion_disclosed_at")
    assert completion.state == "not_applicable" and not completion.applicable


def test_field_results_use_actual_event_applicability_for_the_denominator():
    planned = replace(_positive("planned"), status=CompanyPositiveStatus.PLANNED)
    cancelled = replace(_positive("cancelled"), status=CompanyPositiveStatus.CANCELLED, cancelled_at=AT)
    current = _positive("current")
    expired = replace(_risk("expired"), expires_at=AT)
    results = BuildCompanyFieldResults(_facts(risks=(expired,), positives=(planned, cancelled, current)))
    assert {(result.event_id, result.field) for result in results if result.applicable} == {("current", "actual_pct")}
    assert all(result.state == "not_applicable" for result in results if not result.applicable)


@pytest.mark.parametrize("action,state", (
    ("download", "pending_download"),
    ("extract", "pending_extraction"),
    ("ocr", "pending_extraction"),
    ("publish", "pending_publication"),
    ("verify", "verification_required"),
    ("unsupported", "unsupported"),
    ("out_of_range", "out_of_range"),
))
def test_field_result_missing_value_uses_exact_matching_recovery(action, state):
    gap = CompanyFieldGap("600001", "increase", "increase", "actual_pct", "measurement_missing")
    results = BuildCompanyFieldResults(
        _facts(positives=(_positive(),)), (CompanyFieldRecovery(gap, action, "diagnosed_reason"),)
    )
    assert len(results) == 1
    assert results[0].state == state and results[0].reason == "diagnosed_reason"
    assert results[0].recovery_action == action and results[0].value is None and results[0].applicable


@pytest.mark.parametrize("change", (
    {"code": "600002"}, {"event_id": "other-event"}, {"evidence_id": "other-source"}, {"field": "planned_pct"},
))
def test_field_result_does_not_reuse_another_field_or_source_recovery(change):
    gap = replace(CompanyFieldGap("600001", "increase", "increase", "actual_pct", "measurement_missing"), **change)
    recovery = CompanyFieldRecovery(gap, "download", "local_source_missing")
    result = BuildCompanyFieldResults(_facts(positives=(_positive(),)), (recovery,))[0]
    assert result.state == "verification_required" and result.recovery_action is None


@pytest.mark.parametrize("reason,action", (("not_disclosed", "finished"), ("available", "reuse")))
def test_field_result_does_not_upgrade_an_unproven_recovery_conclusion(reason, action):
    gap = CompanyFieldGap("600001", "increase", "increase", "actual_pct", reason)
    recovery = CompanyFieldRecovery(gap, action, "unproven")
    result = BuildCompanyFieldResults(_facts(positives=(_positive(),)), (recovery,))[0]
    assert result.state == "verification_required" and result.applicable
    assert result.reason == "field_conclusion_without_source_proof"


def test_progress_recovery_can_identify_an_actual_ratio_obligation_left_in_planned_state():
    event = replace(_positive(), status=CompanyPositiveStatus.PLANNED)
    gap = CompanyFieldGap("600001", "increase", "increase", "actual_pct", "measurement_missing")
    result = BuildCompanyFieldResults(
        _facts(positives=(event,)), (CompanyFieldRecovery(gap, "extract", "local_text_available"),)
    )[0]
    assert result.applicable and result.state == "pending_extraction"
    cancelled = replace(event, status=CompanyPositiveStatus.CANCELLED, cancelled_at=AT)
    result = BuildCompanyFieldResults(
        _facts(positives=(cancelled,)), (CompanyFieldRecovery(gap, "extract", "local_text_available"),)
    )[0]
    assert not result.applicable and result.state == "not_applicable"


def test_contract_results_keep_unknown_sentinels_as_missing_and_cancelled_fields_outside_denominator():
    event = replace(
        _positive(), event_type=CompanyPositiveType.OPERATING_CONTRACT,
        measurements=ContractMeasurements(100_000_000, None, None, None, None, True, "unknown"),
    )
    results = BuildCompanyFieldResults(_facts(positives=(event,)))
    assert len(results) == 6 and all(result.applicable for result in results)
    assert next(result for result in results if result.field == "contract_amount").state == "present_unverified"
    assert next(result for result in results if result.field == "amount_basis").value is None
    cancelled = replace(event, status=CompanyPositiveStatus.CANCELLED, cancelled_at=AT)
    assert all(not result.applicable for result in BuildCompanyFieldResults(_facts(positives=(cancelled,))))


def test_reviews_and_orphan_recoveries_do_not_create_fake_event_fields():
    facts = replace(_facts(), review_reasons=(CompanyReviewReason("positive", "progress_unlinked", "notice", AT, "待关联"),))
    recovery = CompanyFieldRecovery(_gap(), "download", "local_source_missing")
    assert BuildCompanyFieldResults(facts, (recovery,)) == ()


def test_field_result_rejects_conflicting_recovery_rows_instead_of_selecting_an_action():
    gap = CompanyFieldGap("600001", "increase", "increase", "actual_pct", "measurement_missing")
    recoveries = (
        CompanyFieldRecovery(gap, "extract", "local_text_available"),
        CompanyFieldRecovery(gap, "download", "local_source_missing"),
    )
    with pytest.raises(ValueError, match="company_field_recovery_ambiguous"):
        BuildCompanyFieldResults(_facts(positives=(_positive(),)), recoveries)
