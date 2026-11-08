"""Compact per-issuer facts; no disclosure text crosses the recommendation boundary."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta

from trader.download.domain.company_descriptions import (
    CompanyPositiveDescription,
    CompanyReviewDescription,
    CompanyRiskDescription,
)
from trader.download.domain.company_event_extraction import CompanyDocumentNeedsReview, ExtractCompanyEvents
from trader.download.domain.company_events import (
    CompanyDataState,
    CompanyEventPolicy,
    CompanyPositiveEvent,
    CompanyReviewReason,
    CompanyRiskEvent,
)
from trader.download.domain.company_evidence import CompanyEvidenceInput, CompanyFinancial, CompanyRatio, EvidenceMarket
from trader.download.domain.company_annual_income import ApplyCompanyAnnualIncome


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
    latest_financial: CompanyFinancial | None = field(kw_only=True)

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
            (self.financial is None) != (self.latest_financial is None)
            or self.latest_financial is not None and (
                self.latest_financial.code != self.code
                or self.latest_financial.published_at > self.published_at
                or self.latest_financial.report_date > self.published_at.date()
                or self.financial is not None and (
                    self.financial.report_date, self.financial.published_at
                ) > (self.latest_financial.report_date, self.latest_financial.published_at)
            )
        ):
            raise ValueError("company_facts_latest_financial_identity_invalid")
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
        previous_reviews=tuple(r for r in previous.review_reasons if r.evidence_id not in source.superseded_evidence_ids) if previous else (),
    )
    extraction = replace(extraction, positives=ApplyCompanyAnnualIncome(
        source.code, extraction.positives, source.annual_incomes, now, financials=source.financials
    ))
    financials = tuple(
        item for item in source.financials if item.published_at <= now and item.report_date <= now.date()
    )
    if (previous is not None and previous.financial is not None
            and previous.financial.published_at <= now and previous.financial.report_date <= now.date()):
        financials += (previous.financial,)
    if (previous is not None and previous.latest_financial is not None
            and previous.latest_financial.published_at <= now and previous.latest_financial.report_date <= now.date()):
        financials += (previous.latest_financial,)
    latest_financial = max(financials, key=lambda item: (item.report_date, item.published_at)) if financials else None
    valid_financials = tuple(item for item in financials if _complete_financial(item))
    financial = max(valid_financials, key=lambda item: (item.report_date, item.published_at)) if valid_financials else latest_financial
    financial_complete = (
        latest_financial is not None and financial is latest_financial and _complete_financial(latest_financial)
        and not any(item.startswith("financial:") for item in source.failures)
    )
    reviews = list(extraction.reviews)
    if latest_financial is not None and not _complete_financial(latest_financial):
        reviews.append(CompanyReviewReason("financial", "necessary_data_missing", None, now, "最新财报字段尚不完整"))
    for failure in source.failures:
        scope, _, reason = failure.partition(":")
        if scope in {"financial", "pledge", "unlock", "announcements"}:
            reviews.append(
                CompanyReviewReason(
                    "risk" if scope == "announcements" else scope,
                    reason or "unavailable",
                    None,
                    now,
                    "持续事项的上市初始状态尚未核实" if reason == "initial_risk_state_unverified" else failure,
                )
            )
    pledges = tuple(item for item in (source.pledge, previous.pledge if previous else None)
                    if item is not None and item.day <= now.date())
    pledge = max(pledges, key=lambda item: item.day) if pledges else None
    pledge_complete = (pledge is source.pledge and source.pledge is not None
                       and not any(item.startswith("pledge:") for item in source.failures))
    unlock_schedule, unlock_checked_at, unlock_coverage_end, unlock_state = _unlock_snapshot(source, policy, previous)
    risk_complete = source.announcement_complete and not any(item.scope == "risk" for item in reviews)
    if previous is not None:
        reviews = [
            replace(item, detected_at=old.detected_at)
            if (
                old := next(
                    (
                        old
                        for old in previous.review_reasons
                        if (old.scope, old.reason_code, old.evidence_id)
                        == (item.scope, item.reason_code, item.evidence_id)
                    ),
                    None,
                )
            )
            else item
            for item in reviews
        ]
    pending = sum(
        not item.checked and not item.terminal_failure and CompanyDocumentNeedsReview(item.disclosure.title) for item in source.announcements
    )
    return CompanyFacts(
        source.code,
        financial,
        CompanyDataState.COMPLETE
        if financial_complete
        else CompanyDataState.PARTIAL
        if financial
        else CompanyDataState.UNAVAILABLE,
        pledge,
        CompanyDataState.COMPLETE
        if pledge_complete
        else CompanyDataState.PARTIAL
        if pledge
        else CompanyDataState.UNAVAILABLE,
        unlock_schedule,
        unlock_checked_at,
        unlock_coverage_end,
        unlock_state,
        tuple(
            replace(item, summary=CompanyRiskDescription(item.risk_type))
            for item in extraction.risks
            if item.expires_at is None or item.expires_at > now
        ),
        tuple(
            replace(item, summary=CompanyPositiveDescription(item.event_type))
            for item in extraction.positives
            if item.last_disclosed_at.date() >= source.window_start
        ),
        tuple(
            sorted(
                {replace(item, summary=CompanyReviewDescription(item.reason_code)) for item in reviews},
                key=lambda item: (item.scope, item.evidence_id or "", item.reason_code),
            )
        ),
        pending,
        source.window_start,
        source.window_end,
        CompanyDataState.COMPLETE if risk_complete else CompanyDataState.PARTIAL,
        source.source_checked_at or (previous.checked_at if previous else now),
        now,
        latest_financial=latest_financial,
    )


def _unlock_snapshot(
    source: CompanyEvidenceInput, policy: CompanyEventPolicy, previous: CompanyFacts | None
) -> tuple[tuple[CompanyRatio, ...], datetime | None, date | None, CompanyDataState]:
    now = source.published_at
    complete = source.unlock_checked and not any(item.startswith("unlock:") for item in source.failures)
    rows, checked_at, coverage_end = source.unlocks, source.unlock_checked_at, source.unlock_coverage_end
    target_end = now.date() + timedelta(days=policy.unlock_days)
    source_valid = _unlock_metadata_valid(checked_at, coverage_end, now)
    complete = complete and source_valid
    previous_valid = (
        previous is not None
        and previous.published_at <= now
        and _unlock_metadata_valid(previous.unlock_checked_at, previous.unlock_coverage_end, now)
    )
    if previous_valid and previous is not None and (
        not source_valid
        or not complete and (checked_at is None or coverage_end is None)
        or (
            checked_at is not None and previous.unlock_checked_at is not None
            and checked_at < previous.unlock_checked_at
        )
        or (
            coverage_end is not None and coverage_end < target_end and previous.unlock_coverage_end is not None
            and coverage_end < previous.unlock_coverage_end
        )
    ):
        # Preserve one verified snapshot, including its dates. A failed or
        # shorter check cannot turn the previous coverage into an empty result.
        rows, checked_at, coverage_end = (
            previous.unlock_schedule, previous.unlock_checked_at, previous.unlock_coverage_end
        )
        complete = False
    elif not source_valid:
        rows, checked_at, coverage_end, complete = (), None, None, False
    schedule = tuple(
        item for item in rows
        if now.date() <= item.day <= target_end and (coverage_end is None or item.day <= coverage_end)
    )
    if complete and (coverage_end is None or coverage_end >= target_end):
        state = CompanyDataState.COMPLETE
    elif schedule or checked_at is not None and coverage_end is not None and coverage_end >= now.date():
        state = CompanyDataState.PARTIAL
    else:
        state = CompanyDataState.UNAVAILABLE
    return schedule, checked_at, coverage_end, state


def _unlock_metadata_valid(checked_at: datetime | None, coverage_end: date | None, now: datetime) -> bool:
    return checked_at is None or (
        checked_at.utcoffset() is not None
        and checked_at <= now
        and (coverage_end is None or coverage_end >= checked_at.date())
    )


def _complete_financial(item: CompanyFinancial) -> bool:
    return all(value is not None for value in (
        item.basic_eps, item.book_value_per_share, item.revenue_growth_pct, item.net_profit_growth_pct,
        item.core_profit_growth_pct, item.roe_pct, item.parent_net_profit, item.core_net_profit,
    ))
