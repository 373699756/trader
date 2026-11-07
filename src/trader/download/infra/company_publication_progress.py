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
        ))


__all__ = ["ReportCompanyVerification"]
