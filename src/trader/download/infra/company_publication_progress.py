"""Project compilation facts without claiming that the formal transaction committed."""

from __future__ import annotations

from collections.abc import Callable

from trader.download.domain.company_evidence import CompanyPublicationProgress, CompanyStockProgress
from trader.download.domain.company_events import CompanyDataState
from trader.download.domain.company_facts import CompanyFacts


def ReportCompanyVerification(
    report: Callable[[CompanyStockProgress | CompanyPublicationProgress], None] | None,
    facts: CompanyFacts,
    processed: int,
    total: int,
    announcements: int,
    analyzed: int,
    reused: bool,
) -> None:
    if report is not None:
        report(CompanyStockProgress(
            facts.code,
            processed=processed,
            total=total,
            announcements=announcements,
            analyzed=analyzed,
            pending=facts.pending_document_count,
            risks=len(facts.risk_events),
            positives=len(facts.positive_events),
            coverage_complete=all(state is CompanyDataState.COMPLETE for state in (
                facts.financial_state, facts.pledge_state, facts.unlock_state, facts.announcement_state,
            )),
            reused=reused,
            review_count=len(facts.review_reasons),
            financial_state=facts.financial_state,
            pledge_state=facts.pledge_state,
            unlock_state=facts.unlock_state,
            announcement_state=facts.announcement_state,
            financial_report_date=facts.financial.report_date if facts.financial is not None else None,
            financial_disclosed_at=facts.financial.published_at if facts.financial is not None else None,
            pledge_date=facts.pledge.day if facts.pledge is not None else None,
            unlock_coverage_end=facts.unlock_coverage_end,
        ))


__all__ = ["ReportCompanyVerification"]
