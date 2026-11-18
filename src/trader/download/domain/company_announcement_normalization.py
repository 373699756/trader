"""Compile verified new official documents without claiming a complete risk baseline."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from trader.download.domain.company_announcement_numbers import ExtractCompanyAnnouncementNumbers
from trader.download.domain.company_event_extraction import (
    CompanyDocumentRiskRelevant, CompanyLinkCandidate, ExtractCompanyEvents, ResolveCompanyLinks,
)
from trader.download.domain.company_events import CompanyDataState, CompanyEventPolicy, CompanyReviewReason
from trader.download.domain.company_evidence import CompanyDocument, CompanyEvidenceInput
from trader.download.domain.company_facts import CompanyFacts, CompileCompanyFacts, FinalizeCompanyFacts


@dataclass(frozen=True, slots=True)
class CompanyAnnouncementNormalization:
    facts: CompanyFacts
    verified_evidence_ids: tuple[str, ...]


def NormalizeCompanyAnnouncementFacts(
    previous: CompanyFacts, documents: tuple[CompanyDocument, ...], cutoff: datetime, policy: CompanyEventPolicy,
    *, candidates: tuple[CompanyLinkCandidate, ...], pending_count: int, directory_complete: bool,
    input_identity: str, revision_reviews: tuple[CompanyReviewReason, ...] = (),
    verified_candidate_ids: tuple[str, ...] = (),
) -> CompanyAnnouncementNormalization:
    """Use the event/link owners; preserve financial, ratio and independent risk facts."""
    if cutoff.utcoffset() is None or cutoff < previous.published_at or pending_count < 0:
        raise ValueError("company_announcement_normalization_scope_invalid")
    unresolved = any(review.evidence_id in verified_candidate_ids and review.reason_code in {
        "progress_unlinked", "resolution_unlinked", "cancellation_unlinked",
    } for review in previous.review_reasons)
    if not documents and not revision_reviews and not unresolved and pending_count == previous.pending_document_count:
        return CompanyAnnouncementNormalization(previous, ())
    eligible: list[CompanyDocument] = []
    reviews = list(revision_reviews)
    verified_ids = []
    for document in documents:
        item = document.disclosure
        if item.codes != (previous.code,) or item.published_at > cutoff or item.kind != "announcement":
            raise ValueError("company_announcement_document_scope_invalid")
        if ExtractCompanyAnnouncementNumbers(document.text).issuer_codes != (previous.code,):
            reviews.append(CompanyReviewReason("risk" if CompanyDocumentRiskRelevant(item.title) else "positive",
                "subject_unverified", item.identity, cutoff, "公告原文证券身份未核实"))
            continue
        eligible.append(document)
        if ExtractCompanyEvents((document,), cutoff, policy).checked_documents == 1:
            verified_ids.append(item.identity)
    snapshot = CompanyEvidenceInput(previous.code, cutoff, cutoff.date() - timedelta(days=365), cutoff.date(),
        input_identity, tuple(eligible), False, (), None, (), False, (), False,
        ("announcements:initial_risk_state_unverified",))
    reviewed_previous = replace(previous, review_reasons=(*previous.review_reasons, *revision_reviews))
    compiled = CompileCompanyFacts(snapshot, policy, previous=reviewed_previous)
    # The general compiler removes expired risks from its active projection.
    # Keep the extractor's actual resolution and its evidence in this journal.
    extracted = ExtractCompanyEvents(tuple(eligible), cutoff, policy, previous_risks=previous.risk_events)
    retained_risks = extracted.risks + tuple(event for event in previous.risk_events
        if event.event_id not in {current.event_id for current in extracted.risks})
    linked = ResolveCompanyLinks(tuple(candidate for candidate in candidates
        if candidate.evidence_id in (*verified_ids, *verified_candidate_ids)),
        retained_risks, compiled.positive_events, compiled.review_reasons, policy)
    compiled = FinalizeCompanyFacts(compiled, snapshot, linked, previous)
    old_reviews = tuple(item for item in previous.review_reasons if item.scope not in {"risk", "positive"})
    combined = tuple(item for item in compiled.review_reasons if item.scope in {"risk", "positive"}) + tuple(reviews)
    # Preserve the original detection time rather than refreshing unchanged gaps.
    combined = tuple(replace(item, detected_at=old.detected_at) if (
        old := next((old for old in previous.review_reasons
            if (old.scope, old.reason_code, old.evidence_id) == (item.scope, item.reason_code, item.evidence_id)), None)
    ) is not None else item for item in combined)
    result = replace(previous, risk_events=linked.risks, positive_events=compiled.positive_events,
        review_reasons=tuple(sorted(set((*old_reviews, *combined)), key=lambda item: (item.scope, item.evidence_id or "", item.reason_code))),
        pending_document_count=pending_count, announcement_state=CompanyDataState.PARTIAL,
        announcement_coverage_start=snapshot.window_start if directory_complete else previous.announcement_coverage_start,
        announcement_coverage_end=snapshot.window_end if directory_complete else previous.announcement_coverage_end,
        checked_at=cutoff if verified_ids else previous.checked_at, published_at=cutoff)
    if replace(result, published_at=previous.published_at) == previous:
        result = previous
    return CompanyAnnouncementNormalization(result, tuple(verified_ids))


__all__ = ["CompanyAnnouncementNormalization", "NormalizeCompanyAnnouncementFacts"]
