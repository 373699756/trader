"""Typed command progress, with counters owned by the running company use case."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol
from datetime import datetime

from trader.download.application.company_operations import CompanyOperations
from trader.download.domain.company_evidence import (
    CompanyDateProgress, CompanyDocumentWorkload, CompanyOperationKind, CompanyStockProgress,
    CompanyStructuredProgress, Disclosure, DisclosurePartition, EvidenceProgressState,
    EvidenceSyncProgress, Market, StructuredCompanyQuery,
)

if TYPE_CHECKING:
    from trader.download.application.company_evidence import CompanyEvidenceDependencies


class CompanyProgressState(Protocol):
    operations: CompanyOperations
    population: int
    population_codes: frozenset[str]
    existing_published_codes: int | None
    pages: int
    changes: int
    reused: int
    completed: int
    published: int
    current_stock: CompanyStockProgress | None
    failures: tuple[str, ...]
    failed_documents: int
    batch: int
    unpublished_documents: int
    publication_pending_since: float | None
    publication_required: bool


def MarkCompanyPublicationPending(dependencies: CompanyEvidenceDependencies, state: CompanyProgressState) -> None:
    state.publication_required = True
    if dependencies.coalesce_publications and state.publication_pending_since is None:
        state.publication_pending_since = dependencies.monotonic()


def PrepareCompanyStockProgress(dependencies: CompanyEvidenceDependencies, state: CompanyProgressState, code: str) -> None:
    state.current_stock = CompanyStockProgress(
        code, next((name for candidate, name in dependencies.stock_names if candidate == code), "")
    )


def CompanyCancelled(dependencies: CompanyEvidenceDependencies) -> bool:
    return dependencies.cancel_requested is not None and dependencies.cancel_requested()


def ReportCompanyProgress(
    dependencies: CompanyEvidenceDependencies,
    component: str,
    state: CompanyProgressState,
    pending: int | None,
    *,
    completed: int | None = None,
    status: EvidenceProgressState = "running",
    scope: DisclosurePartition | StructuredCompanyQuery | None = None,
    pending_documents: int | None = 0,
    attempted_documents: int = 0,
    deferred_documents: int | None = 0,
    activity: Literal["download", "repair"] = "download",
    dates: CompanyDateProgress | None = None,
    known_pages: int = 0,
    unknown_queries: int = 0,
    document_rate: float | None = None,
    document_eta_seconds: float | None = None,
    ocr_documents: int | None = 0,
    unextractable_documents: int | None = 0,
    next_document_retry_at: datetime | None = None,
    discovering_documents: bool = False,
    workload: CompanyDocumentWorkload | None = None,
    unresolved_document_failures: int = 0,
    unpublished_documents: int | None = None,
    failure_document: Disclosure | None = None,
    failure_stage: CompanyOperationKind | None = None,
    failure_reason: str | None = None,
    sources: tuple[str, ...] | None = None,
    completion_scope: Literal["component", "batch"] | None = None,
    structured: CompanyStructuredProgress | None = None,
    market_dates: tuple[tuple[Market, CompanyDateProgress], ...] = (),
) -> None:
    if dependencies.progress is not None:
        failures = tuple(
            dict.fromkeys(
                item for item in state.failures if component == "complete" or item.startswith(f"{component}:")
            )
        )
        dependencies.progress(
            EvidenceSyncProgress(
                component,
                state.completed if completed is None else completed,
                pending,
                state.pages,
                state.changes,
                state.reused,
                ",".join(failures),
                status,
                scope,
                pending_documents,
                attempted_documents,
                deferred_documents,
                activity,
                (CompanyStockProgress(failure_document.codes[0], next((name for code, name in dependencies.stock_names if code == failure_document.codes[0]), ""))
                 if failure_document is not None else state.current_stock) if component in {"risk_documents", "news"} else None,
                dates=dates,
                failed_documents=state.failed_documents if component == "risk_documents" else 0,
                unresolved_document_failures=unresolved_document_failures,
                batch=state.batch,
                known_pages=known_pages,
                unknown_queries=unknown_queries,
                document_rate=document_rate,
                document_eta_seconds=document_eta_seconds,
                ocr_documents=ocr_documents,
                unextractable_documents=unextractable_documents,
                next_document_retry_at=next_document_retry_at,
                discovering_documents=discovering_documents,
                workload=workload,
                population=state.population,
                unpublished_documents=unpublished_documents,
                failure_document=failure_document,
                failure_stage=failure_stage,
                failure_reason=failure_reason,
                completion_scope=completion_scope or (
                    "batch" if component in {"risk_documents", "facts", "publish", "annual_income"} else "component"
                ),
                structured=structured,
                market_dates=market_dates,
                existing_published_codes=state.existing_published_codes if component in {"resume", "complete"} else None,
                sources=(sources if sources is not None else (dependencies.disclosures.source,)
                         if component == "announcements" else (dependencies.structured.source,)
                         if component in {"financial", "pledge", "unlock", "annual_income"}
                         else (dependencies.structured.news_source,) if component == "news" else ("local",)),
                run_id=state.operations.run_id,
            )
        )
