"""Withdraw a disposal document's own mistaken repurchase measurement only."""

from dataclasses import replace
from datetime import datetime

from trader.download.domain.company_event_extraction import (
    ExtractCompanyLinkCandidate, IsCompanyRepurchaseCancellationOnly,
)
from trader.download.domain.company_events import (
    CompanyEventPolicy, CompanyPositiveType, CompanySubject, RepurchaseMeasurements, IncreaseMeasurements, ContractMeasurements,
)
from trader.download.domain.company_evidence import CompanyDocument
from trader.download.domain.company_facts import CompanyFacts


def CorrectCompanyRepurchaseDisposalMeasurements(
    previous: CompanyFacts, documents: tuple[CompanyDocument, ...], cutoff: datetime, policy: CompanyEventPolicy,
) -> CompanyFacts:
    """Consume exact, valid-backed documents supplied by the material boundary.

    A linked historical execution, shared reference, multi-issuer document or
    independent milestone is never erased. The event remains under its original
    identity and dates; only the measurement incorrectly ascribed to this sole
    disposal announcement is replaced by the current single parser's result.
    """
    if cutoff.utcoffset() is None:
        raise ValueError("company_measurement_correction_cutoff_invalid")
    positives = list(previous.positive_events)
    for position, event in enumerate(positives):
        if (event.event_type is not CompanyPositiveType.SHARE_REPURCHASE or event.subject_type is not CompanySubject.ISSUER
            or event.cancelled_at is not None or not isinstance(event.measurements, RepurchaseMeasurements)
            or event.measurements.actual_ratio_pct is None or len(event.evidence_ids) != 1):
            continue
        identity = event.evidence_ids[0]
        matches = tuple(document for document in documents if document.disclosure.identity == identity)
        if len(matches) != 1:
            continue
        document = matches[0]
        item = document.disclosure
        if (not document.checked or document.terminal_failure or item.codes != (previous.code,)
            or item.kind != "announcement" or item.published_at > cutoff
            or event.event_id != f"{identity}:share_repurchase"
            or event.first_disclosed_at != event.last_disclosed_at or event.last_disclosed_at != item.published_at
            or not IsCompanyRepurchaseCancellationOnly(item.title)
            or any(identity in other.evidence_ids for other in previous.risk_events + previous.positive_events if other.event_id != event.event_id)
            or any(review.evidence_id == identity for review in previous.review_reasons
                if (review.scope, review.reason_code) != ("positive", "implementation_or_measurement_unverified"))
            or any(milestone.evidence_id != identity for milestone in event.level_milestones)):
            continue
        candidate = ExtractCompanyLinkCandidate(document, cutoff, policy)
        if (candidate is None or candidate.subject is not CompanySubject.ISSUER or candidate.positive is None
            or not isinstance(candidate.positive.measurements, RepurchaseMeasurements)
            or candidate.positive.measurements.actual_ratio_pct is not None or candidate.positive.positive_level):
            continue
        positives[position] = replace(event, measurements=candidate.positive.measurements,
            status=candidate.positive.status, positive_level=0, level_milestones=(),
            not_eligible_reason=candidate.positive.not_eligible_reason)
    updated = tuple(positives)
    return previous if updated == previous.positive_events else replace(previous, positive_events=updated, published_at=cutoff)


def RefineCompanyIsolatedPositiveMeasurements(
    previous: CompanyFacts, documents: tuple[CompanyDocument, ...], cutoff: datetime, policy: CompanyEventPolicy,
) -> CompanyFacts:
    """Consume a newly restored exact original through the single current parser.

    Only a missing measurement on its own isolated event can be filled. Known
    values, independent history, shared references and every risk stay intact.
    The ordinary field proofer must still certify each staged value separately.
    """
    positives = list(previous.positive_events)
    for position, event in enumerate(positives):
        if len(event.evidence_ids) != 1 or event.first_disclosed_at != event.last_disclosed_at or event.cancelled_at is not None:
            continue
        matches = tuple(document for document in documents if document.disclosure.identity == event.evidence_ids[0])
        if len(matches) != 1:
            continue
        document = matches[0]
        if (not document.checked or document.terminal_failure or document.disclosure.codes != (previous.code,)
            or document.disclosure.published_at > cutoff or document.disclosure.published_at != event.last_disclosed_at
            or any(event.evidence_ids[0] in other.evidence_ids for other in previous.risk_events + previous.positive_events if other.event_id != event.event_id)
            or any(milestone.evidence_id != event.evidence_ids[0] for milestone in event.level_milestones)):
            continue
        candidate = ExtractCompanyLinkCandidate(document, cutoff, policy)
        if candidate is None or candidate.positive is None or candidate.subject is not event.subject_type:
            continue
        current = candidate.positive
        if current.event_id != event.event_id or current.event_type is not event.event_type:
            continue
        before, after = event.measurements, current.measurements
        fill = (isinstance(before, (RepurchaseMeasurements, IncreaseMeasurements))
            and isinstance(after, type(before)) and before.actual_ratio_pct is None and after.actual_ratio_pct is not None)
        fill = fill or (isinstance(before, ContractMeasurements) and isinstance(after, ContractMeasurements)
            and before.contract_amount_cny is None and after.contract_amount_cny is not None
            and before.reference_annual_revenue_cny is None and before.reference_report_date is None and before.reference_disclosed_at is None)
        if fill:
            positives[position] = replace(event, measurements=current.measurements, status=current.status,
                positive_level=current.positive_level, level_milestones=current.level_milestones,
                not_eligible_reason=current.not_eligible_reason)
    result = tuple(positives)
    return previous if result == previous.positive_events else replace(previous, positive_events=result, published_at=cutoff)


__all__ = ["CorrectCompanyRepurchaseDisposalMeasurements", "RefineCompanyIsolatedPositiveMeasurements"]
