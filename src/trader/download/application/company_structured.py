"""Market-level structured collection and its durable query progress."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Literal

from trader.download.application.company_progress import (
    CompanyProgressState, ReportCompanyProgress, MarkCompanyPublicationPending, CompanyCancelled,
)
from trader.download.application.company_failures import CompanyFailureCode
from trader.download.application.company_operations import CompanyOperations
from trader.download.domain.company_evidence import (
    CompanyStructuredProgress, StructuredCompanyQuery,
)

if TYPE_CHECKING:
    from trader.download.application.company_evidence import CompanyEvidenceDependencies, CompanyEvidenceIndex

def CollectCompanyStructured(
    dependencies: CompanyEvidenceDependencies,
    component: Literal["financial", "pledge", "unlock"],
    now: datetime,
    state: CompanyProgressState,
    *, timeout_seconds: float | None = None,
) -> None:
    index = dependencies.index
    due, checked = state.operations.call_observed("inventory", partial(index.component_due, component, "market", now),
                                                  object_id=f"{component}:market", component=component,
                                                  endpoint="sqlite:company_component_checks")
    if not due:
        state.reused += 1
        state.operations.result(checked, "reused", component=component, original_source=dependencies.structured.source)
        ReportCompanyProgress(dependencies, component, state, 0, completed=0, status="reused")
        return
    if CompanyCancelled(dependencies):
        ReportCompanyProgress(dependencies, component, state, None, completed=0, status="deferred")
        return
    ReportCompanyProgress(dependencies, component, state, None, completed=0)
    completed = 0
    pending: int | None = None
    query: StructuredCompanyQuery | None = None
    detail: CompanyStructuredProgress | None = None
    try:
        if CompanyCancelled(dependencies):
            return
        if component == "financial":
            start = state.operations.call("inventory", partial(index.component_since, component, "market", now),
                                          component=component, object_id=f"{component}:market",
                                          endpoint="sqlite:company_structured_coverage")
            end = now.date()
        elif component == "pledge":
            start = end = state.operations.call("structured", partial(dependencies.structured.pledge_day, now),
                                                source=dependencies.structured.source, component="pledge",
                                                endpoint=dependencies.structured.request_endpoint("pledge"))
        else:
            start, end = now.date(), now.date() + timedelta(days=180)
        requested = StructuredCompanyQuery(component, start, end)
        query = state.operations.call("inventory", partial(index.structured_cursor, requested), scope=requested,
                                      component=component, endpoint="sqlite:company_structured_cursor")
        resumed, restarted = query.page > 1, False
        if component == "financial" and query.start > requested.start:
            query = requested
            resumed, restarted = False, True
        while not CompanyCancelled(dependencies):
            completed = query.page - 1
            detail = CompanyStructuredProgress(query, query.received_rows, resumed=resumed, restarted=restarted)
            ReportCompanyProgress(dependencies, component, state, pending, completed=completed, scope=query, structured=detail)
            restarted = False
            if CompanyCancelled(dependencies):
                break
            page, fetched = state.operations.call_observed(
                "structured", partial(dependencies.structured.fetch, query, observed_at=now),
                source=dependencies.structured.source, scope=query, timeout_seconds=timeout_seconds,
                endpoint=dependencies.structured.request_endpoint(component),
            )
            state.pages += 1
            changes, committed = state.operations.call_observed(
                "collection_commit", partial(index.commit_structured, query, page, now),
                scope=query, source=dependencies.structured.source,
                endpoint="sqlite:company_collection",
            )
            state.changes += changes
            state.operations.result(fetched, "empty" if not page.row_count else "accepted",
                                    rows=page.row_count, commit=committed, changed_rows=changes, component=component)
            MarkCompanyPublicationPending(dependencies, state)
            completed, pending = page.page, page.pages - page.page
            page_codes = tuple(sorted(
                ({item.code for item in page.financials} | {item.code for item in page.ratios}) & state.population_codes
            ))
            page_dates = tuple(item.published_at.date() for item in page.financials) + tuple(item.day for item in page.ratios)
            detail = CompanyStructuredProgress(
                query,
                query.received_rows + page.row_count,
                page.row_count,
                page_codes,
                min(page_dates) if page_dates else None,
                max(page_dates) if page_dates else None,
                resumed=resumed,
                restarted=restarted,
            )
            ReportCompanyProgress(dependencies, component, state, pending, completed=completed, scope=query, structured=detail)
            if page.page == page.pages:
                remaining = _remaining_query(index, query, requested, now, state.operations)
                if remaining is None:
                    ReportCompanyProgress(dependencies, component, state, 0, completed=completed, status="complete", scope=query, structured=detail)
                    return
                query = remaining
                resumed, restarted = False, True
                pending = None
                continue
            query = replace(
                query, received_rows=query.received_rows + page.row_count, expected_rows=page.total, page=page.page + 1
            )
        ReportCompanyProgress(dependencies, component, state, pending, completed=completed, status="deferred", scope=query, structured=detail)
    except (OSError, RuntimeError, ValueError) as exc:
        if CompanyCancelled(dependencies):
            ReportCompanyProgress(dependencies, component, state, pending, completed=completed, status="deferred", scope=query, structured=detail)
            return
        failure = CompanyFailureCode(exc)
        state.operations.register_failure(exc, partial(index.component_failure, component, "market", failure))
        state.failures += (f"{component}:{failure}",)
        state.operations.recover(exc, "source_deferred", failure)
        if detail is not None and failure in {
            "structured_count_changed", "structured_pages_incomplete", "structured_pages_inconsistent",
            "structured_duplicate_page_rows",
        }:
            query = replace(detail.query, page=1, received_rows=0, expected_rows=None)
            detail = CompanyStructuredProgress(query, 0, restarted=True)
            completed, pending = 0, None
        ReportCompanyProgress(dependencies, component, state, pending, completed=completed, status="deferred", scope=query, structured=detail)


def _remaining_query(
    index: CompanyEvidenceIndex, query: StructuredCompanyQuery, requested: StructuredCompanyQuery, now: datetime,
    operations: CompanyOperations,
) -> StructuredCompanyQuery | None:
    if query.component == "financial" and query.end < requested.end:
        start = operations.call("inventory", partial(index.component_since, "financial", "market", now),
                                scope=requested, component="financial", endpoint="sqlite:company_structured_coverage")
        return replace(requested, start=start)
    if query.component != "financial" and (query.start, query.end) != (requested.start, requested.end):
        return requested
    return None
