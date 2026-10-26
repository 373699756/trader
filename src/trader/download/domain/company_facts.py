"""Compact per-issuer facts; no disclosure text crosses the recommendation boundary."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from trader.download.domain.company_event_extraction import CompanyDocumentNeedsReview, ExtractCompanyEvents
from trader.download.domain.company_events import (
    CompanyDataState,
    CompanyEventPolicy,
    CompanyPositiveEvent,
    CompanyReviewReason,
    CompanyRiskEvent,
)
from trader.download.domain.company_evidence import CompanyEvidenceInput, CompanyFinancial, CompanyRatio, EvidenceMarket


@dataclass(frozen=True, slots=True)
class CompanyFacts:
    code: str
    financial: CompanyFinancial | None
    financial_state: CompanyDataState
    pledge: CompanyRatio | None
    pledge_state: CompanyDataState
    unlock_schedule: tuple[CompanyRatio, ...]
    unlock_checked_at: datetime | None
    unlock_coverage_end: date | None
    unlock_state: CompanyDataState
    risk_events: tuple[CompanyRiskEvent, ...]
    positive_events: tuple[CompanyPositiveEvent, ...]
    review_reasons: tuple[CompanyReviewReason, ...]
    pending_document_count: int
    announcement_coverage_start: date | None
    announcement_coverage_end: date | None
    announcement_state: CompanyDataState
    checked_at: datetime
    published_at: datetime

    def __post_init__(self) -> None:
        EvidenceMarket(self.code)
        if (
            self.checked_at.utcoffset() is None
            or self.published_at.utcoffset() is None
            or self.pending_document_count < 0
        ):
            raise ValueError("company_facts_time_or_count_invalid")
        if self.financial is not None and self.financial.code != self.code:
            raise ValueError("company_facts_financial_identity_invalid")
        if (
            any(item.code != self.code for item in self.unlock_schedule)
            or self.pledge
            and self.pledge.code != self.code
        ):
            raise ValueError("company_facts_ratio_identity_invalid")
        if len({item.event_id for item in self.risk_events}) != len(self.risk_events) or len(
            {item.event_id for item in self.positive_events}
        ) != len(self.positive_events):
            raise ValueError("company_facts_duplicate_events")


def CompileCompanyFacts(
    source: CompanyEvidenceInput,
    policy: CompanyEventPolicy,
    *,
    previous: CompanyFacts | None = None,
) -> CompanyFacts:
    now = source.published_at
    extraction = ExtractCompanyEvents(
        source.announcements,
        now,
        policy,
        previous_risks=previous.risk_events if previous else (),
        previous_positives=previous.positive_events if previous else (),
    )
    financials = tuple(
        item for item in source.financials if item.published_at <= now and item.report_date <= now.date()
    )
    financial = max(financials, key=lambda item: (item.report_date, item.published_at)) if financials else None
    financial_complete = (
        financial is not None
        and all(
            value is not None
            for value in (
                financial.basic_eps,
                financial.book_value_per_share,
                financial.revenue_growth_pct,
                financial.net_profit_growth_pct,
                financial.core_profit_growth_pct,
                financial.roe_pct,
                financial.parent_net_profit,
                financial.core_net_profit,
            )
        )
        and not any(item.startswith("financial:") for item in source.failures)
    )
    reviews = list(extraction.reviews)
    for failure in source.failures:
        scope, _, reason = failure.partition(":")
        if scope in {"financial", "pledge", "unlock", "announcements"}:
            reviews.append(
                CompanyReviewReason(
                    "risk" if scope == "announcements" else scope, reason or "unavailable", None, now, failure
                )
            )
    pledge_complete = source.pledge is not None and not any(item.startswith("pledge:") for item in source.failures)
    unlock_complete = source.unlock_checked and not any(item.startswith("unlock:") for item in source.failures)
    risk_complete = source.announcement_complete and not any(item.scope == "risk" for item in reviews)
    if previous is not None:
        reviews = [
            replace(item, detected_at=old.detected_at)
            if (
                old := next(
                    (
                        old
                        for old in previous.review_reasons
                        if (old.scope, old.reason_code, old.evidence_id, old.summary)
                        == (item.scope, item.reason_code, item.evidence_id, item.summary)
                    ),
                    None,
                )
            )
            else item
            for item in reviews
        ]
    pending = sum(
        not item.checked and CompanyDocumentNeedsReview(item.disclosure.title) for item in source.announcements
    )
    return CompanyFacts(
        source.code,
        financial,
        CompanyDataState.COMPLETE
        if financial_complete
        else CompanyDataState.PARTIAL
        if financial
        else CompanyDataState.UNAVAILABLE,
        source.pledge,
        CompanyDataState.COMPLETE
        if pledge_complete
        else CompanyDataState.PARTIAL
        if source.pledge
        else CompanyDataState.UNAVAILABLE,
        tuple(
            item for item in source.unlocks if now.date() <= item.day <= now.date() + timedelta(days=policy.unlock_days)
        ),
        source.unlock_checked_at if unlock_complete else previous.unlock_checked_at if previous else None,
        source.unlock_coverage_end if unlock_complete else previous.unlock_coverage_end if previous else None,
        CompanyDataState.COMPLETE
        if unlock_complete
        else CompanyDataState.PARTIAL
        if source.unlocks
        else CompanyDataState.UNAVAILABLE,
        tuple(item for item in extraction.risks if item.expires_at is None or item.expires_at > now),
        tuple(item for item in extraction.positives if item.last_disclosed_at.date() >= source.window_start),
        tuple(reviews),
        pending,
        source.window_start,
        source.window_end,
        CompanyDataState.COMPLETE if risk_complete else CompanyDataState.PARTIAL,
        source.source_checked_at or (previous.checked_at if previous else now),
        now,
    )
