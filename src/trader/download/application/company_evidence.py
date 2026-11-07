"""Resumable company-evidence preparation, independent of recommendation execution."""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, wait
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from functools import partial
from typing import Literal, Protocol

from trader.download.domain.company_evidence import (
    BuildCompanyDocumentPolicies,
    CompanyDateProgress,
    CompanyDocumentExtraction,
    CompanyDocumentFailure,
    CompanyDocumentPolicies,
    CompanyDocumentRouteForFailure,
    CompanyDocumentTask,
    CompanyDocumentWorkload,
    CompanyOcrDiagnostic,
    CompanyOperationKind,
    CompanyEvidenceRequest,
    CompanyEvidenceSyncResult,
    CompanySourceFailure,
    CompanyStockProgress,
    CompanySyncConfiguration,
    Disclosure,
    DisclosureCoverageMismatch,
    DisclosurePage,
    DisclosurePartition,
    DisclosurePartitionRetry,
    DisclosureQuery,
    DisclosureSecurity,
    EvidenceMarket,
    EvidenceProgressState,
    EvidenceSyncProgress,
    IsTransientCompanyDocumentFailure,
    Market,
    StructuredCompanyPage,
    StructuredCompanyQuery,
)
from trader.infra.workers import WorkerExecutor, WorkerResourceRejectedError
from trader.download.application.download_failure import DownloadFailurePort
from trader.download.domain.download_failure import DownloadFailureContext
from trader.download.domain.company_annual_income import CompanyAnnualIncome, CompanyAnnualIncomeRequest
from trader.download.application.company_operations import CompanyOperations


class CompanyEvidenceIndex(Protocol):
    def document_publication_urgent(self, item: Disclosure) -> bool: ...
    def annual_income_requests(
        self, codes: Sequence[str], observed_at: datetime
    ) -> tuple[CompanyAnnualIncomeRequest, ...]: ...
    def commit_annual_income(
        self,
        request: CompanyAnnualIncomeRequest,
        incomes: tuple[CompanyAnnualIncome, ...],
        observed_at: datetime,
    ) -> int: ...
    def document_stage(self, item: Disclosure, stage: str) -> None: ...
    def document_required(self, item: Disclosure) -> bool: ...
    def initialize(self) -> None: ...
    def partitions(self, markets: Sequence[Market], observed_at: datetime) -> tuple[DisclosurePartition, ...]: ...
    def reusable_partitions(self, markets: Sequence[Market], observed_at: datetime) -> int: ...
    def split_partition(
        self, partition: DisclosurePartition, observed_at: datetime
    ) -> tuple[DisclosurePartition, ...]: ...
    def commit_page(self, partition: DisclosurePartition, page: DisclosurePage, observed_at: datetime) -> int: ...
    def partition_failure(self, partition: DisclosurePartition, failure: str) -> None: ...
    def disclosure_queries(
        self, partition: DisclosurePartition, *, include_completed: bool = False
    ) -> tuple[DisclosurePartition, ...] | None: ...
    def refresh_disclosure_query(self, partition: DisclosurePartition, page: DisclosurePage) -> int | None: ...
    def disclosure_members(self, partition: DisclosurePartition) -> tuple[DisclosureSecurity, ...]: ...
    def split_query(
        self, partition: DisclosurePartition, queries: tuple[DisclosureQuery, ...]
    ) -> tuple[DisclosurePartition, ...]: ...
    def commit_query_coverage(
        self, partition: DisclosurePartition, page: DisclosurePage, observed_at: datetime
    ) -> None: ...
    def partition_retry(self, partition: DisclosurePartition) -> DisclosurePartitionRetry | None: ...
    def defer_partition(self, partition: DisclosurePartition, retry_after: datetime) -> None: ...
    def pending_documents(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        limit: int | None = None,
        policies: CompanyDocumentPolicies,
        excluded: Sequence[tuple[str, str, str]] = (),
        repair: Literal["ocr", "limits"] | None = None,
        local_only: bool = False,
    ) -> tuple[CompanyDocumentTask, ...]: ...
    def document_workload(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        policies: CompanyDocumentPolicies,
        *,
        excluded: Sequence[tuple[str, str, str]] = (),
        repair: Literal["ocr", "limits"] | None = None,
    ) -> CompanyDocumentWorkload: ...
    def commit_document(
        self, item: Disclosure, extraction: CompanyDocumentExtraction, extracted_at: datetime
    ) -> bool: ...
    def document_failure(
        self,
        item: Disclosure,
        policy: str,
        failure: str,
        attempted_at: datetime,
        retry_after: datetime,
        payload_hash: bytes | None = None,
        diagnostic: CompanyOcrDiagnostic | None = None,
        ocr_policy: str = "",
    ) -> None: ...
    def document_ocr_timeouts(self, item: Disclosure, policy: str) -> int: ...
    def component_due(self, component: str, scope: str, observed_at: datetime) -> bool: ...
    def component_since(self, component: str, scope: str, observed_at: datetime) -> date: ...
    def structured_cursor(self, query: StructuredCompanyQuery) -> StructuredCompanyQuery: ...
    def commit_structured(
        self, query: StructuredCompanyQuery, page: StructuredCompanyPage, observed_at: datetime
    ) -> int: ...
    def component_failure(self, component: str, scope: str, failure: str) -> None: ...
    def commit_news(self, code: str, rows: Sequence[Disclosure], observed_at: datetime) -> int: ...
    def prepare_publication(self, codes: Sequence[str], observed_at: datetime) -> None: ...
    def published_count(self, codes: Sequence[str]) -> int: ...
    def publish(self, codes: Sequence[str], observed_at: datetime, *, changed_only: bool = False) -> int: ...
    def finish(self, observed_at: datetime) -> None: ...


class CompanyDisclosureSource(Protocol):
    @property
    def source(self) -> str: ...
    def fetch(self, partition: DisclosurePartition) -> DisclosurePage: ...
    def document_ready(self, item: Disclosure) -> bool: ...
    def download_document(self, item: Disclosure) -> None: ...
    def extract_document(self, item: Disclosure, *, route: Literal["standard", "ocr", "limits"] = "standard") -> CompanyDocumentExtraction: ...
    def split_queries(self, partition: DisclosurePartition) -> tuple[DisclosureQuery, ...]: ...
    def repair_queries(
        self,
        partition: DisclosurePartition,
        members: tuple[DisclosureSecurity, ...],
        *,
        missing_rows: int,
        report: Callable[[DisclosurePartition], None] | None = None,
    ) -> tuple[DisclosureQuery, ...]: ...


class CompanyStructuredSource(Protocol):
    @property
    def source(self) -> str: ...
    @property
    def news_source(self) -> str: ...
    def annual_income(
        self, request: CompanyAnnualIncomeRequest, *, observed_at: datetime
    ) -> tuple[CompanyAnnualIncome, ...]: ...
    def pledge_day(self, observed_at: datetime) -> date: ...
    def fetch(self, query: StructuredCompanyQuery, *, observed_at: datetime) -> StructuredCompanyPage: ...
    def news(self, code: str, *, observed_at: datetime) -> tuple[Disclosure, ...]: ...


@dataclass(frozen=True)
class CompanyEvidenceDependencies:
    index: CompanyEvidenceIndex
    disclosures: CompanyDisclosureSource
    structured: CompanyStructuredSource
    monotonic: Callable[[], float]
    progress: Callable[[EvidenceSyncProgress], None] | None = None
    cancel_requested: Callable[[], bool] | None = None
    stock_names: tuple[tuple[str, str], ...] = ()
    document_sources: tuple[CompanyDisclosureSource, ...] = ()
    document_executor: WorkerExecutor | None = None
    text_executor: WorkerExecutor | None = None
    ocr_executor: WorkerExecutor | None = None
    ocr_identity: str = "unavailable"
    text_identity: str = "poppler-path"
    failures: DownloadFailurePort | None = None
    directory_executor: WorkerExecutor | None = None
    coalesce_publications: bool = False


@dataclass
class _SyncState:
    operations: CompanyOperations
    population: int
    pages: int = 0
    changes: int = 0
    reused: int = 0
    completed: int = 0
    published: int = 0
    current_stock: CompanyStockProgress | None = None
    failures: tuple[str, ...] = ()
    failed_documents: int = 0
    batch: int = 0
    annual_income_attempts: set[CompanyAnnualIncomeRequest] = field(default_factory=set)
    annual_income_circuit: _SourceCircuit = field(default_factory=lambda: _SourceCircuit())
    annual_income_source_stopped: bool = False
    unpublished_documents: int = 0
    publication_pending_since: float | None = None
    publication_required: bool = True


@dataclass
class _SourceCircuit:
    transport_failures: int = 0
    validation_failures: int = 0

    def success(self) -> None:
        self.transport_failures = self.validation_failures = 0

    def failed(self, exc: BaseException, *, validation: bool = False) -> bool:
        if isinstance(exc, CompanySourceFailure) and exc.category in {"access_denied", "rate_limited"}:
            return True
        if _transport_failure(exc):
            self.transport_failures += 1
            self.validation_failures = 0
        elif validation:
            self.transport_failures = 0
            self.validation_failures += 1
        else:
            self.success()
        return max(self.transport_failures, self.validation_failures) >= 3


def SyncCompanyEvidence(
    dependencies: CompanyEvidenceDependencies,
    request: CompanyEvidenceRequest,
) -> CompanyEvidenceSyncResult:
    codes, observed_at, configuration, news_codes = (
        request.codes,
        request.observed_at,
        request.configuration,
        request.news_codes,
    )
    if observed_at.utcoffset() is None:
        raise ValueError("company evidence requires an aware time")
    started = dependencies.monotonic()
    index = dependencies.index
    population = _validated_population(codes)
    state = _SyncState(CompanyOperations(dependencies.progress, dependencies.monotonic, dependencies.stock_names), len(population))
    start_date = observed_at.date() - timedelta(days=365)
    context = _CompanyRunContext(observed_at, started, configuration, start_date)
    documents = _DocumentRun(dependencies, population, state, context)
    if not _cancelled(dependencies):
        _report(dependencies, "prepare", state, 1, completed=0)
        state.operations.call("prepare", index.initialize)
        if not _cancelled(dependencies):
            state.operations.call("prepare", partial(index.prepare_publication, population, observed_at))
        if not _cancelled(dependencies):
            _report(dependencies, "prepare", state, 0, completed=1, status="complete")
            _report(dependencies, "resume", state, None, workload=documents.workload(), status="complete")
    news_population = tuple(sorted(set(news_codes).intersection(population))) if not request.fill_missing else ()
    components: tuple[Literal["financial", "pledge", "unlock"], ...] = ("financial", "pledge", "unlock")
    for component in components:
        if _cancelled(dependencies):
            break
        if any(item.endswith((":access_denied", ":rate_limited")) for item in state.failures):
            _report(dependencies, component, state, None, completed=0, status="deferred")
            continue
        _structured(dependencies, component, observed_at, state)
    if not _cancelled(dependencies):
        _publish_batch(dependencies, population, observed_at, started, state)
    if not _cancelled(dependencies) and documents.workload().local_pending:
        # Recover one bounded local batch; directory discovery must not wait for
        # the complete backlog, and later batches still reuse their local files.
        completed = documents.completed
        documents.run_batch(local_only=True)
        if documents.completed != completed:
            _publish_batch(dependencies, population, observed_at, started, state)
    pending, published = (
        (None, 0) if _cancelled(dependencies) else _run_batches(dependencies, population, state, context, documents)
    )
    published = max(published, state.published)
    news_updates = () if _cancelled(dependencies) else _news(dependencies, news_population, observed_at, state)
    if news_updates and not _cancelled(dependencies):
        _publish_batch(dependencies, population, observed_at, started, state)
    if dependencies.coalesce_publications and not _cancelled(dependencies):
        # Final verification remains mandatory even if the last bounded
        # batch was already published. Scheduling is not a file seal.
        _publish_batch(dependencies, population, observed_at, started, state)
    published = max(published, state.published)
    document_workload: CompanyDocumentWorkload | None = None
    if not _cancelled(dependencies):
        try:
            document_workload = documents.workload(include_attempted=True)
        except InterruptedError:
            if not _cancelled(dependencies):
                raise
    if _cancelled(dependencies):
        state.failures += ("task:cancelled",)
        pending = None
        document_workload = None
    pending_documents = document_workload.due + document_workload.cooling if document_workload is not None else None
    if not _cancelled(dependencies):
        state.operations.call("finish", partial(index.finish, observed_at))
    if pending_documents:
        state.failures += ("risk_documents:pending",)
    result_failures = tuple(sorted({failure for failure in state.failures if _failure_requires_retry(failure)}))
    _report(
        dependencies,
        "complete",
        state,
        pending,
        completed=published,
        status="deferred" if result_failures or pending else "complete",
        pending_documents=pending_documents,
        deferred_documents=document_workload.cooling if document_workload is not None else None,
        ocr_documents=document_workload.ocr if document_workload is not None else None,
        unextractable_documents=document_workload.unextractable if document_workload is not None else None,
        next_document_retry_at=document_workload.next_retry_at if document_workload is not None else None,
        workload=document_workload,
    )
    return CompanyEvidenceSyncResult(
        observed_at,
        len(population),
        published,
        state.pages,
        state.changes,
        state.reused,
        pending,
        result_failures,
        max(0.0, dependencies.monotonic() - started),
        pending_documents,
        len(news_population),
        "published_population" if news_population else "uncovered",
        document_workload.ocr if document_workload is not None else None,
        document_workload.unextractable if document_workload is not None else None,
        document_workload.next_retry_at if document_workload is not None else None,
        (observed_at.date() - context.start_date).days,
    )


def _run_batches(
    dependencies: CompanyEvidenceDependencies,
    population: tuple[str, ...],
    state: _SyncState,
    context: _CompanyRunContext,
    documents: _DocumentRun,
) -> tuple[int, int]:
    pending = published = 0
    announcements = _AnnouncementRun(dependencies, state, context)
    announcements.prepare(population)
    try:
        while not _cancelled(dependencies):
            state.batch += 1
            pending = announcements.run_batch(context.configuration.directory_batch_pages)
            if not _cancelled(dependencies):
                documents.run_batch(
                    discovering=announcements.can_continue,
                    discover=lambda: announcements.run_batch(
                        1, wait_for_page=dependencies.directory_executor is None
                    ) if announcements.can_continue else None,
                )
                pending = announcements.run_batch(0)
            if not _cancelled(dependencies) and (
                not dependencies.coalesce_publications or state.publication_required
            ):
                count = _publish_batch(dependencies, population, context.observed_at, context.started, state)
                if count is not None:
                    published = count
            if not announcements.can_continue and not documents.has_due():
                break
    finally:
        announcements.close()
    if not _cancelled(dependencies):
        published = dependencies.index.published_count(population)
        state.published = published
    return pending, published


def _publish_batch(
    dependencies: CompanyEvidenceDependencies,
    population: tuple[str, ...],
    observed_at: datetime,
    started: float,
    state: _SyncState,
) -> int | None:
    _report(dependencies, "facts", state, len(population), completed=0)
    if not _cancelled(dependencies):
        state.operations.call("publication", partial(_publish_company_facts, dependencies.index, population, observed_at))
        if _cancelled(dependencies):
            return state.published
        state.published = dependencies.index.published_count(population)
        changes = _annual_income(dependencies, population, observed_at, state)
        if _cancelled(dependencies):
            return state.published
        if changes and not _cancelled(dependencies):
            state.changes += changes
            state.operations.call("publication", partial(_publish_company_facts, dependencies.index, population, observed_at))
            if _cancelled(dependencies):
                return state.published
            state.published = dependencies.index.published_count(population)
        _report(dependencies, "publish", state, len(population) - state.published, completed=state.published, status="complete")
        state.unpublished_documents = 0
        state.publication_pending_since = None
        state.publication_required = False
        return state.published
    return None


def _publish_company_facts(index: CompanyEvidenceIndex, population: tuple[str, ...], observed_at: datetime) -> int:
    for attempt in range(3):
        try:
            return index.publish(population, observed_at, changed_only=True)
        except RuntimeError as exc:
            if attempt == 2 or str(exc) not in {"company_publication_source_changed", "company_publication_parent_changed"}:
                raise
    raise RuntimeError("company_publication_retry_exhausted")


def _mark_publication_pending(dependencies: CompanyEvidenceDependencies, state: _SyncState) -> None:
    state.publication_required = True
    if dependencies.coalesce_publications and state.publication_pending_since is None:
        state.publication_pending_since = dependencies.monotonic()


def _annual_income(
    dependencies: CompanyEvidenceDependencies,
    population: tuple[str, ...],
    observed_at: datetime,
    state: _SyncState,
) -> int:
    if _cancelled(dependencies) or state.annual_income_source_stopped:
        return 0
    requests = tuple(
        request for request in dependencies.index.annual_income_requests(population, observed_at)
        if request not in state.annual_income_attempts
    )
    changes = 0
    for completed, request in enumerate(requests):
        if _cancelled(dependencies) or state.annual_income_source_stopped:
            break
        state.current_stock = None
        _report(dependencies, "annual_income", state, len(requests) - completed, completed=completed)
        if _cancelled(dependencies):
            break
        state.annual_income_attempts.add(request)
        try:
            incomes = state.operations.call("annual_income", partial(dependencies.structured.annual_income, request, observed_at=observed_at), code=request.code, source=dependencies.structured.source)
            if _cancelled(dependencies):
                break
            changes += state.operations.call("collection_commit", partial(dependencies.index.commit_annual_income, request, incomes, observed_at), code=request.code)
            state.pages += 1
            state.annual_income_circuit.success()
            remaining = len(requests) - completed - 1
            _report(dependencies, "annual_income", state, remaining, completed=completed + 1,
                    status="running" if remaining else "complete")
        except (OSError, RuntimeError, ValueError) as exc:
            if _cancelled(dependencies):
                break
            failure = _failure(exc)
            state.failures += (f"annual_income:{failure}",)
            state.annual_income_source_stopped = state.annual_income_circuit.failed(exc, validation=isinstance(exc, ValueError))
            if dependencies.failures is not None:
                dependencies.failures(DownloadFailureContext("company_annual_income", supplier="eastmoney"), exc)
            _report(dependencies, "annual_income", state, len(requests) - completed, completed=completed, status="deferred")
    return changes


def _validated_population(codes: Sequence[str]) -> tuple[str, ...]:
    population = tuple(sorted(set(codes)))
    if not population:
        raise ValueError("company_evidence_empty_population")
    for code in population:
        EvidenceMarket(code)
    return population


@dataclass(frozen=True)
class _CompanyRunContext:
    observed_at: datetime
    started: float
    configuration: CompanySyncConfiguration
    start_date: date


class _AnnouncementRun:
    """Own the dispatch queue and failure circuit for one announcement stage."""

    def __init__(
        self, dependencies: CompanyEvidenceDependencies, state: _SyncState, context: _CompanyRunContext
    ) -> None:
        self._dependencies, self._state, self._context = dependencies, state, context
        self._index = dependencies.index
        self._queue: deque[DisclosurePartition] = deque()
        self._deferred: set[tuple[Market, date, date, str]] = set()
        self._circuit = _SourceCircuit()
        self._stopped = False
        self._prefetch: tuple[DisclosurePartition, bool, Future[DisclosurePage]] | None = None

    def prepare(self, population: tuple[str, ...]) -> None:
        self._population = population
        dependencies, state = self._dependencies, self._state
        _report(dependencies, "announcements", state, None)
        if _cancelled(dependencies):
            return
        markets = tuple(sorted({EvidenceMarket(code) for code in population}))
        self._queue = deque(self._index.partitions(markets, self._context.observed_at))
        state.reused += self._index.reusable_partitions(markets, self._context.observed_at)
        self._report()

    @property
    def can_continue(self) -> bool:
        return bool(self._queue or self._prefetch is not None) and not self._stopped

    def run_batch(self, max_pages: int, *, wait_for_page: bool = True) -> int:
        dependencies, state = self._dependencies, self._state
        initial_pages = state.pages
        while self.can_continue and not _cancelled(dependencies) and state.pages - initial_pages < max_pages:
            # Publication failures belong to publication. In particular they
            # must not discard an outstanding HTTP request or defer its cursor.
            self._publish_due()
            # Splits and resumed cursors can put an older range at the head.
            # Keep the newest date ahead of every market's older backlog;
            # equal-date query leaves retain their durable dispatch order.
            partition = self._prefetch[0] if self._prefetch is not None else max(
                self._queue, key=lambda item: item.end
            )
            try:
                if self._prefetch is None:
                    self._queue.remove(partition)
                    self._report(partition, include_current=True)
                    if _cancelled(dependencies):
                        self._queue.appendleft(partition)
                        break
                    request = self._request(partition)
                    if request is None:
                        continue
                    partition, verifying = request
                    if dependencies.directory_executor is None:
                        page = state.operations.call("directory", partial(dependencies.disclosures.fetch, partition), source=dependencies.disclosures.source)
                        if _cancelled(dependencies):
                            self._queue.appendleft(partition)
                            break
                        self._accept(partition, page, verifying=verifying)
                        continue
                    self._prefetch = (
                        partition,
                        verifying,
                        state.operations.submit("directory", partial(dependencies.disclosures.fetch, partition), dependencies.directory_executor, source=dependencies.disclosures.source),
                    )
                partition, verifying, future = self._prefetch
                if not future.done():
                    if not wait_for_page:
                        break
                    wait((future,), timeout=0.1, return_when=FIRST_COMPLETED)
                    continue
                self._prefetch = None
                if future.cancelled():
                    if not _cancelled(dependencies):
                        raise WorkerResourceRejectedError("company_directory_request_cancelled")
                    self._queue.appendleft(partition)
                    break
                page = future.result()
                if _cancelled(dependencies):
                    self._queue.appendleft(partition)
                    break
                self._accept(partition, page, verifying=verifying)
            except WorkerResourceRejectedError:
                self._queue.appendleft(partition)
                raise
            except (OSError, RuntimeError, ValueError) as exc:
                self._prefetch = None
                if self._failed(partition, exc):
                    self._stopped = True
                    break
        current = self._prefetch[0] if self._prefetch is not None else None
        pending = _pending_count(self._queue, self._deferred, current)
        self._report(status=("deferred" if not self.can_continue else "running") if pending else "complete")
        return pending

    def close(self) -> None:
        """Drain only the owned request before its caller closes the HTTP client.

        The cursor is never advanced on cancellation; a late response is not
        accepted. Source timeout and cancellation remain the request's bound.
        """
        if self._prefetch is not None:
            _partition, _verifying, future = self._prefetch
            future.cancel()
            while not future.done():
                wait((future,), timeout=0.1, return_when=FIRST_COMPLETED)
            self._prefetch = None

    def _publish_due(self) -> None:
        state, dependencies = self._state, self._dependencies
        pending_since = state.publication_pending_since
        if (
            dependencies.coalesce_publications
            and pending_since is not None
            and not _cancelled(dependencies)
            and dependencies.monotonic() - pending_since >= self._context.configuration.publication_interval_seconds
        ):
            _publish_batch(
                dependencies,
                self._population,
                self._context.observed_at,
                self._context.started,
                state,
            )

    def _now(self) -> datetime:
        return self._context.observed_at + timedelta(
            seconds=max(0, self._dependencies.monotonic() - self._context.started)
        )

    def _report(
        self,
        partition: DisclosurePartition | None = None,
        *,
        include_current: bool = False,
        status: EvidenceProgressState = "running",
        activity: Literal["download", "repair"] = "download",
    ) -> None:
        current = partition if include_current else self._prefetch[0] if self._prefetch is not None else None
        queued = (*self._queue, *((current,) if current else ()))
        _report(
            self._dependencies,
            "announcements",
            self._state,
            _pending_count(self._queue, self._deferred, current),
            scope=partition,
            dates=_date_progress(self._context.observed_at.date(), self._queue, self._deferred, current, self._context.start_date),
            status=status,
            activity=activity,
            known_pages=sum(
                max(0, (item.expected_rows + item.page_size - 1) // item.page_size - item.page + 1)
                for item in queued
                if item.expected_rows is not None
            ),
            unknown_queries=sum(item.expected_rows is None for item in queued),
        )

    def _request(self, partition: DisclosurePartition) -> tuple[DisclosurePartition, bool] | None:
        retry = self._index.partition_retry(partition)
        if retry is not None and retry.retry_at > self._now():
            self._deferred.add(_query_key(partition))
            self._state.failures += (f"announcements:{retry.failure or 'pending'}",)
            return None
        children = self._index.disclosure_queries(partition) if partition.query is None else None
        if children:
            self._queue.extendleft(reversed([child for child in children if _query_key(child) not in self._deferred]))
            return None
        verifying = children == ()
        if verifying:
            partition = replace(partition, page=1, expected_rows=None, received_rows=0)
        return partition, verifying

    def _accept(self, partition: DisclosurePartition, page: DisclosurePage, *, verifying: bool) -> None:
        self._state.pages += 1
        _mark_publication_pending(self._dependencies, self._state)
        if verifying:
            try:
                self._index.commit_query_coverage(partition, page, self._context.observed_at)
            except DisclosureCoverageMismatch as exc:
                if exc.missing_rows and self._refresh_queries(partition):
                    return
                repairs = (
                    self._dependencies.disclosures.repair_queries(
                        partition,
                        self._index.disclosure_members(partition),
                        missing_rows=exc.missing_rows,
                        report=self._repair_progress,
                    )
                    if exc.missing_rows
                    else ()
                )
                if not repairs:
                    raise
                children = self._index.split_query(partition, repairs)
                self._queue = _replace_query_queue(self._queue, partition, children, self._deferred)
                self._circuit.success()
                self._report(partition)
                return
            self._state.completed += 1
            self._circuit.success()
        elif page.total > 3000:
            self._circuit.success()
            self._split(partition)
        else:
            self._state.changes += self._state.operations.call("collection_commit", partial(self._index.commit_page, partition, page, self._context.observed_at))
            self._circuit.success()
            self._advance(partition, page)
        self._report(replace(partition, expected_rows=page.total))

    def _refresh_queries(self, partition: DisclosurePartition) -> bool:
        refreshed = False
        for saved in self._index.disclosure_queries(partition, include_completed=True) or ():
            if _cancelled(self._dependencies):
                return False
            probe = replace(saved, page=1, received_rows=0)
            page = self._state.operations.call("directory", partial(self._dependencies.disclosures.fetch, probe), source=self._dependencies.disclosures.source)
            self._state.pages += 1
            changes = self._index.refresh_disclosure_query(probe, page)
            if changes is not None:
                self._state.changes += changes
                refreshed = True
            self._report(probe, include_current=True, activity="repair")
        if refreshed:
            pending = self._index.disclosure_queries(partition) or ()
            self._queue = _replace_query_queue(self._queue, partition, pending, self._deferred)
            self._circuit.success()
        return refreshed

    def _repair_progress(self, partition: DisclosurePartition) -> None:
        self._state.pages += 1
        self._report(partition, include_current=True, activity="repair")

    def _advance(self, partition: DisclosurePartition, page: DisclosurePage) -> None:
        if page.has_more:
            self._queue.appendleft(
                replace(
                    partition,
                    page=partition.page + 1,
                    expected_rows=page.total,
                    received_rows=partition.received_rows + len(page.rows),
                )
            )
        elif partition.query is None:
            self._state.completed += 1
        elif self._index.disclosure_queries(partition) == ():
            self._queue.appendleft(replace(partition, query=None, page=1, expected_rows=None, received_rows=0))

    def _split(self, partition: DisclosurePartition) -> None:
        if partition.start < partition.end:
            children = self._index.split_partition(partition, self._context.observed_at)
            self._queue = _replace_split_queue(self._queue, partition, children, self._deferred)
        else:
            queries = self._dependencies.disclosures.split_queries(partition)
            children = self._index.split_query(partition, queries)
            self._queue = _replace_query_queue(self._queue, partition, children, self._deferred)

    def _failed(self, partition: DisclosurePartition, exc: BaseException) -> bool:
        if _cancelled(self._dependencies):
            self._queue.appendleft(partition)
            return True
        if str(exc) == "company_evidence_persistence_unavailable":
            raise exc
        if _failure(exc) in {"cninfo_duplicate_page_links", "cninfo_page_limit"} or (
            _failure(exc) == "cninfo_partition_changed" and partition.start < partition.end
        ):
            try:
                self._split(partition)
                return False
            except (OSError, RuntimeError, ValueError) as split_exc:
                exc = split_exc
        if _cancelled(self._dependencies):
            self._queue.appendleft(partition)
            return True
        failure = _failure(exc)
        if self._dependencies.failures is not None:
            self._dependencies.failures(DownloadFailureContext("company_directory", supplier="cninfo", window_start=partition.start, window_end=partition.end), exc)
        self._index.partition_failure(partition, failure)
        _mark_publication_pending(self._dependencies, self._state)
        self._state.failures += (f"announcements:{failure}",)
        self._deferred.add(_query_key(partition))
        configuration = self._context.configuration
        delay = (
            configuration.document_transient_retry_seconds
            if _transient(exc) or failure in {"cninfo_partition_changed", "cninfo_query_coverage_mismatch"}
            else configuration.document_failure_retry_seconds
        )
        self._index.defer_partition(partition, self._now() + timedelta(seconds=delay))
        if _cancelled(self._dependencies):
            return True
        stopped = self._circuit.failed(
            exc, validation=failure not in {"cninfo_query_unsplittable", "cninfo_single_day_page_limit"}
        )
        if stopped:
            self._state.failures += ("announcements:source_stopped",)
        return stopped


def _replace_split_queue(
    queue: deque[DisclosurePartition],
    parent: DisclosurePartition,
    children: tuple[DisclosurePartition, ...],
    deferred: set[tuple[Market, date, date, str]],
) -> deque[DisclosurePartition]:
    # The index returns current durable descendants; discard every stale queued copy,
    # including children already completed by a previous split in this run.
    return deque(
        (
            *(item for item in children if _query_key(item) not in deferred),
            *(
                item
                for item in queue
                if not (item.market == parent.market and parent.start <= item.start <= item.end <= parent.end)
            ),
        )
    )


def _query_key(partition: DisclosurePartition) -> tuple[Market, date, date, str]:
    return partition.market, partition.start, partition.end, partition.query.key if partition.query else ""


def _pending_count(
    queue: deque[DisclosurePartition],
    deferred: set[tuple[Market, date, date, str]],
    current: DisclosurePartition | None = None,
) -> int:
    keys = {(market, start, end) for market, start, end, _ in deferred}
    keys.update((item.market, item.start, item.end) for item in queue)
    if current is not None:
        keys.add((current.market, current.start, current.end))
    return len(keys)


def _date_progress(
    end: date,
    queue: deque[DisclosurePartition],
    deferred: set[tuple[Market, date, date, str]],
    current: DisclosurePartition | None,
    start: date,
) -> CompanyDateProgress:
    ranges = {(item.start, item.end) for item in queue} | {(first, last) for _, first, last, _ in deferred}
    if current is not None:
        ranges.add((current.start, current.end))
    pending_days: set[date] = set()
    for first, last in ranges:
        first, last = max(start, first), min(end, last)
        pending_days.update(first + timedelta(days=offset) for offset in range(max(0, (last - first).days + 1)))
    return CompanyDateProgress(start, end, (end - start).days + 1 - len(pending_days))


def _replace_query_queue(
    queue: deque[DisclosurePartition],
    parent: DisclosurePartition,
    children: tuple[DisclosurePartition, ...],
    deferred: set[tuple[Market, date, date, str]],
) -> deque[DisclosurePartition]:
    key = (parent.market, parent.start, parent.end)
    if not children:
        children = (replace(parent, query=None, page=1, expected_rows=None, received_rows=0),)
    return deque(
        (
            *(item for item in children if _query_key(item) not in deferred),
            *(item for item in queue if (item.market, item.start, item.end) != key),
        )
    )


class _DocumentRun:
    """Bounded network/parse work; only this caller commits results and retry state."""

    def __init__(
        self,
        dependencies: CompanyEvidenceDependencies,
        population: tuple[str, ...],
        state: _SyncState,
        context: _CompanyRunContext,
    ) -> None:
        configuration = context.configuration
        self.dependencies, self.population, self.observed_at = dependencies, population, context.observed_at
        self.state, self.configuration, self.started = state, configuration, context.started
        self.sources = dependencies.document_sources
        if configuration.document_workers == 1 and not self.sources:
            self.sources = (dependencies.disclosures,)
        if len(self.sources) != configuration.document_workers or len({id(source) for source in self.sources}) != len(
            self.sources
        ):
            raise ValueError("company_document_sources_invalid")
        if configuration.document_workers > 1 and dependencies.document_executor is None:
            raise ValueError("company_document_executor_required")
        self.executor = dependencies.document_executor
        self.failed_identities: set[tuple[str, str, str]] = set()
        self.policies = BuildCompanyDocumentPolicies(
            configuration,
            dependencies.ocr_identity,
            text_identity=dependencies.text_identity,
        )
        self.circuit = _SourceCircuit()
        self.stopped = False
        self.capacity_blocked = False
        self.completed = self.attempted = 0
        self.attempted_identities: set[tuple[str, str, str]] = set()
        self.elapsed = 0.0
        self.batch_started: float | None = None
        self.batch_sources: tuple[str, ...] = ()

    def now(self) -> datetime:
        return self.observed_at + timedelta(seconds=max(0, self.dependencies.monotonic() - self.started))

    def workload(self, *, include_attempted: bool = False) -> CompanyDocumentWorkload:
        return self.state.operations.call("inventory", partial(self.dependencies.index.document_workload,
            self.population,
            self.now(),
            self.policies,
            excluded=() if include_attempted else tuple(self.failed_identities),
        ))

    def has_due(self) -> bool:
        if _cancelled(self.dependencies) or self.capacity_blocked or not self.workload().due:
            return False
        if not self.stopped:
            return True
        tasks = self.dependencies.index.pending_documents(self.population, self.now(), limit=1, policies=self.policies,
                                                          excluded=tuple(self.failed_identities))
        return bool(tasks and self.sources[0].document_ready(tasks[0].disclosure))

    def run_batch(self, *, discovering: bool = False, discover: Callable[[], int | None] | None = None, local_only: bool = False) -> None:
        dependencies, index = self.dependencies, self.dependencies.index
        if self.capacity_blocked or _cancelled(dependencies):
            return
        self.state.current_stock = None
        workload = self.workload()
        if _cancelled(dependencies):
            return
        queue = deque(
            index.pending_documents(
                self.population,
                self.now(),
                limit=self.configuration.document_batch_size,
                policies=self.policies,
                excluded=tuple(self.failed_identities),
                local_only=local_only,
            )
        )
        self.batch_sources = tuple(sorted({task.disclosure.source for task in queue}))
        self.attempted_identities.clear()
        text_queue: deque[CompanyDocumentTask] = deque()
        ocr_queue: deque[CompanyDocumentTask] = deque()
        for task in tuple(queue):
            if self.sources[0].document_ready(task.disclosure):
                queue.remove(task)
                (ocr_queue if task.route == "ocr" else text_queue).append(task)
        if local_only or self.stopped:
            queue.clear()
        pending_documents: dict[Future[CompanyDocumentExtraction | None], tuple[int, Literal["download", "text", "ocr"], CompanyDocumentTask]] = {}
        available = deque(range(len(self.sources)))
        completed_before = self.completed
        self.batch_started = dependencies.monotonic()
        self.report(workload, completed_before)
        last_publish = dependencies.monotonic()
        unpublished = 0
        capacity_blocked = False
        while queue or text_queue or ocr_queue or pending_documents:
            urgent_publish = False
            stages: tuple[tuple[Literal["text", "ocr"], deque[CompanyDocumentTask], WorkerExecutor | None, int], ...] = (
                ("ocr", ocr_queue, dependencies.ocr_executor, self.configuration.ocr_workers),
                ("text", text_queue, dependencies.text_executor, self.configuration.text_workers),
            )
            for stage, ready, executor, capacity in stages:
                inflight = sum(value[1] == stage for value in pending_documents.values())
                while ready and inflight < capacity and not _cancelled(dependencies):
                    task = ready.popleft()
                    if not index.document_required(task.disclosure):
                        continue
                    index.document_stage(task.disclosure, "analyzing")
                    pending_documents[self._submit(self.sources[0], task, stage, executor)] = (-1, stage, task)
                    inflight += 1
                    self._attempt(task.disclosure)
            while queue and available and not self.stopped and not capacity_blocked and not _cancelled(dependencies):
                slot = available.popleft()
                task = queue.popleft()
                item = task.disclosure
                if not index.document_required(item):
                    available.append(slot)
                    continue
                _prepare_stock_progress(dependencies, self.state, item.codes[0])
                self.report(workload, completed_before, discovering=discovering)
                if _cancelled(dependencies):
                    break
                index.document_stage(item, "downloading")
                pending_documents[self._submit(self.sources[slot], task, "download", self.executor)] = (slot, "download", task)
                self._attempt(item)
            if self.stopped or _cancelled(dependencies):
                queue.clear()
            if _cancelled(dependencies):
                text_queue.clear()
                ocr_queue.clear()
                for future in pending_documents:
                    future.cancel()
            if not pending_documents:
                break
            if discover is not None and any(not future.done() for future in pending_documents) and not _cancelled(dependencies):
                discover()
            done, _ = wait(pending_documents, timeout=0.1, return_when=FIRST_COMPLETED)
            for future in done:
                slot, completed_stage, task = pending_documents.pop(future)
                if slot >= 0:
                    available.append(slot)
                if future.cancelled():
                    continue
                try:
                    extraction = future.result()
                except WorkerResourceRejectedError:
                    raise
                except (OSError, RuntimeError, ValueError) as exc:
                    if isinstance(exc, CompanyDocumentFailure) and exc.reason == "disclosure_document_ocr_required":
                        ocr_queue.append(replace(task, route="ocr"))
                        index.document_stage(task.disclosure, "ocr_pending")
                    elif str(exc) == "company_raw_capacity_exhausted":
                        capacity_blocked = True
                        queue.clear()
                        self.report(workload, completed_before, discovering=discovering,
                                    failure_document=task.disclosure, failure_stage=completed_stage,
                                    failure_reason="company_raw_capacity_exhausted")
                    elif not _cancelled(dependencies):
                        reason = self.failed(task.disclosure, exc, stage=completed_stage)
                        self.report(workload, completed_before, discovering=discovering,
                                    failure_document=task.disclosure, failure_stage=completed_stage, failure_reason=reason)
                else:
                    if completed_stage == "download":
                        self.circuit.success()
                        index.document_stage(task.disclosure, "downloaded")
                        (ocr_queue if task.route == "ocr" else text_queue).append(task)
                    elif extraction is not None and self.state.operations.call("document_commit", partial(index.commit_document, task.disclosure, extraction, self.now()), document=task.disclosure):
                        self.completed += 1
                        unpublished += 1
                        self.state.changes += 1
                        _mark_publication_pending(dependencies, self.state)
                        self.state.unpublished_documents += 1
                        if dependencies.coalesce_publications and not _cancelled(dependencies):
                            urgent_publish = urgent_publish or index.document_publication_urgent(task.disclosure)
                self.report(workload, completed_before, discovering=discovering)
            pending_count = self.state.unpublished_documents if dependencies.coalesce_publications else unpublished
            pending_since = self.state.publication_pending_since if dependencies.coalesce_publications else last_publish
            publication_waiting = self.state.publication_required if dependencies.coalesce_publications else bool(unpublished)
            if publication_waiting and not _cancelled(dependencies) and (
                urgent_publish
                or pending_count >= self.configuration.publication_batch_size
                or pending_since is not None and dependencies.monotonic() - pending_since >= self.configuration.publication_interval_seconds
            ):
                _publish_batch(dependencies, self.population, self.observed_at, self.started, self.state)
                unpublished = 0
                last_publish = dependencies.monotonic()
        if capacity_blocked and self.completed == completed_before:
            self.capacity_blocked = True
            self.state.failures += ("risk_documents:company_raw_capacity_exhausted",)
        self.elapsed += max(0.0, dependencies.monotonic() - self.batch_started)
        self.batch_started = None
        if _cancelled(dependencies):
            return
        try:
            final_workload = self.workload()
        except InterruptedError:
            if _cancelled(dependencies):
                return
            raise
        self.report(
            final_workload,
            self.completed,
            status="deferred" if self.stopped else "complete",
            discovering=discovering,
        )

    def _attempt(self, item: Disclosure) -> None:
        key = item.source, item.identity, item.digest
        if key not in self.attempted_identities:
            self.attempted_identities.add(key)
            self.attempted += 1

    def _submit(
        self, source: CompanyDisclosureSource, task: CompanyDocumentTask, stage: Literal["download", "text", "ocr"], executor: WorkerExecutor | None
    ) -> Future[CompanyDocumentExtraction | None]:
        function: Callable[[Disclosure], CompanyDocumentExtraction | None] = (
            source.download_document if stage == "download" else partial(source.extract_document, route=task.route)
        )
        return self.state.operations.submit(stage, partial(function, task.disclosure), executor, document=task.disclosure)

    def failed(self, item: Disclosure, exc: BaseException, *, stage: str = "analysis") -> str:
        if self.dependencies.failures is not None:
            self.dependencies.failures(DownloadFailureContext("company_document", code=item.codes[0], supplier=item.source,
                detail=f"stage={stage};record={item.identity};version={item.digest}"), exc)
        self.failed_identities.add((item.source, item.identity, item.digest))
        failure = _failure(exc)
        if failure == "disclosure_document_ocr_timeout" and self.dependencies.index.document_ocr_timeouts(
            item, self.policies.ocr
        ) >= 1:
            failure = "disclosure_document_ocr_timeout_exhausted"
        self.state.failed_documents += 1
        self.state.failures = tuple(dict.fromkeys((*self.state.failures, f"risk_documents:{failure}")))
        now = self.now()
        route = CompanyDocumentRouteForFailure(failure)
        policy = self.policies.for_route(route)
        payload_hash = exc.payload_hash if isinstance(exc, CompanyDocumentFailure) else None
        retry_seconds = _document_retry_seconds(self.configuration, failure)
        self.dependencies.index.document_failure(
            item,
            policy,
            failure,
            now,
            now + timedelta(seconds=retry_seconds),
            payload_hash,
            exc.diagnostic if isinstance(exc, CompanyDocumentFailure) else None,
            self.policies.ocr,
        )
        _mark_publication_pending(self.dependencies, self.state)
        if stage == "download" and self.circuit.failed(exc):
            self.stopped = True
            self.state.failures += ("risk_documents:source_stopped",)
        return failure

    def report(
        self,
        workload: CompanyDocumentWorkload,
        completed_before: int,
        *,
        status: EvidenceProgressState = "running",
        discovering: bool = False,
        failure_document: Disclosure | None = None,
        failure_stage: CompanyOperationKind | None = None,
        failure_reason: str | None = None,
    ) -> None:
        elapsed = self.elapsed + (
            max(0.0, self.dependencies.monotonic() - self.batch_started) if self.batch_started is not None else 0.0
        )
        rate = self.completed / elapsed if self.completed >= 5 and elapsed >= 1 else None
        due = max(0, workload.due - self.completed + completed_before)
        _report(
            self.dependencies,
            "risk_documents",
            self.state,
            max(0, workload.pending - self.completed + completed_before),
            completed=self.completed,
            attempted_documents=self.attempted,
            deferred_documents=workload.cooling,
            status=status,
            document_rate=rate,
            document_eta_seconds=due / rate if rate and due else None,
            ocr_documents=workload.ocr,
            unextractable_documents=workload.unextractable,
            next_document_retry_at=workload.next_retry_at,
            discovering_documents=discovering,
            unresolved_document_failures=workload.unresolved_failures,
            unpublished_documents=self.state.unpublished_documents,
            failure_document=failure_document,
            failure_stage=failure_stage,
            failure_reason=failure_reason,
            sources=self.batch_sources,
        )


def _prepare_stock_progress(dependencies: CompanyEvidenceDependencies, state: _SyncState, code: str) -> None:
    state.current_stock = CompanyStockProgress(
        code, next((name for candidate, name in dependencies.stock_names if candidate == code), "")
    )


def _document_retry_seconds(configuration: CompanySyncConfiguration, failure: str) -> float:
    return (
        configuration.document_transient_retry_seconds
        if IsTransientCompanyDocumentFailure(failure)
        else configuration.document_failure_retry_seconds
    )


def _news(
    dependencies: CompanyEvidenceDependencies,
    population: tuple[str, ...],
    observed_at: datetime,
    state: _SyncState,
) -> tuple[str, ...]:
    index = dependencies.index
    attempted: list[str] = []
    completed = 0
    structured_blocked = any(
        item.startswith(("financial:", "pledge:", "unlock:")) and item.endswith((":access_denied", ":rate_limited"))
        for item in state.failures
    )
    candidates = population
    total = len(candidates)
    state.current_stock = None
    if not candidates:
        _report(dependencies, "news", state, 0, completed=0, status="complete")
        return ()
    _report(dependencies, "news", state, total, completed=0)
    for code in () if structured_blocked else candidates:
        if _cancelled(dependencies):
            break
        _prepare_stock_progress(dependencies, state, code)
        if not index.component_due("news", code, observed_at):
            state.reused += 1
            completed += 1
            continue
        attempted.append(code)
        _report(dependencies, "news", state, total - completed, completed=completed)
        try:
            rows = state.operations.call("news", partial(dependencies.structured.news, code, observed_at=observed_at), code=code, source=dependencies.structured.news_source)
            state.changes += state.operations.call("collection_commit", partial(index.commit_news, code, rows, observed_at), code=code)
            _mark_publication_pending(dependencies, state)
            completed += 1
        except (OSError, RuntimeError, ValueError) as exc:
            if _cancelled(dependencies):
                break
            failure = _failure(exc)
            index.component_failure("news", code, failure)
            state.failures += (f"news:{failure}",)
            break
    _report(
        dependencies,
        "news",
        state,
        total - completed,
        completed=completed,
        status="complete" if completed == total else "deferred",
    )
    return tuple(attempted)


def _structured(
    dependencies: CompanyEvidenceDependencies,
    component: Literal["financial", "pledge", "unlock"],
    now: datetime,
    state: _SyncState,
) -> None:
    index = dependencies.index
    if not index.component_due(component, "market", now):
        state.reused += 1
        _report(dependencies, component, state, 0, completed=0, status="reused")
        return
    if _cancelled(dependencies):
        _report(dependencies, component, state, None, completed=0, status="deferred")
        return
    _report(dependencies, component, state, None, completed=0)
    completed = 0
    pending: int | None = None
    query: StructuredCompanyQuery | None = None
    try:
        if _cancelled(dependencies):
            return
        if component == "financial":
            start, end = index.component_since(component, "market", now), now.date()
        elif component == "pledge":
            start = end = dependencies.structured.pledge_day(now)
        else:
            start, end = now.date(), now.date() + timedelta(days=180)
        requested = StructuredCompanyQuery(component, start, end)
        query = index.structured_cursor(requested)
        if component == "financial" and query.start > requested.start:
            query = requested
        while not _cancelled(dependencies):
            completed = query.page - 1
            _report(dependencies, component, state, pending, completed=completed, scope=query)
            if _cancelled(dependencies):
                break
            page = state.operations.call("structured", partial(dependencies.structured.fetch, query, observed_at=now), source=dependencies.structured.source)
            state.pages += 1
            state.changes += state.operations.call("collection_commit", partial(index.commit_structured, query, page, now))
            _mark_publication_pending(dependencies, state)
            completed, pending = page.page, page.pages - page.page
            _report(dependencies, component, state, pending, completed=completed, scope=query)
            if page.page == page.pages:
                remaining = _remaining_query(index, query, requested, now)
                if remaining is None:
                    _report(dependencies, component, state, 0, completed=completed, status="complete", scope=query)
                    return
                query = remaining
                pending = None
                continue
            query = replace(
                query, received_rows=query.received_rows + page.row_count, expected_rows=page.total, page=page.page + 1
            )
        _report(dependencies, component, state, pending, completed=completed, status="deferred", scope=query)
    except (OSError, RuntimeError, ValueError) as exc:
        if _cancelled(dependencies):
            _report(dependencies, component, state, pending, completed=completed, status="deferred", scope=query)
            return
        failure = _failure(exc)
        index.component_failure(component, "market", failure)
        if dependencies.failures is not None:
            dependencies.failures(DownloadFailureContext(f"company_{component}", supplier="eastmoney"), exc)
        state.failures += (f"{component}:{failure}",)
        _report(dependencies, component, state, pending, completed=completed, status="deferred", scope=query)


def _remaining_query(
    index: CompanyEvidenceIndex, query: StructuredCompanyQuery, requested: StructuredCompanyQuery, now: datetime
) -> StructuredCompanyQuery | None:
    if query.component == "financial" and query.end < requested.end:
        return replace(requested, start=index.component_since("financial", "market", now))
    if query.component != "financial" and (query.start, query.end) != (requested.start, requested.end):
        return requested
    return None


def _failure(exc: BaseException) -> str:
    # Suppliers expose bounded categories, never raw URLs, documents or transport error text.
    if isinstance(exc, CompanyDocumentFailure):
        return exc.reason
    if isinstance(exc, CompanySourceFailure):
        if exc.category == "http_error" and exc.status_code is not None:
            return f"http_{exc.status_code}"
        return exc.category
    if isinstance(exc, OSError) and str(exc) in {
        "http_transport_failed", "http_compressed_body_invalid", "http_content_encoding_unsupported",
    }:
        return str(exc)
    if isinstance(exc, (ValueError, TimeoutError, RuntimeError)) and re.fullmatch(
        r"(?:cninfo|company|disclosure|structured|financial|pledge|news)_[a-z_]{1,60}", str(exc)
    ):
        return str(exc)
    if isinstance(exc, TimeoutError):
        return "timeout"
    return type(exc).__name__


def _transient(exc: BaseException) -> bool:
    return IsTransientCompanyDocumentFailure(_failure(exc))


def _transport_failure(exc: BaseException) -> bool:
    failure = _failure(exc)
    return failure in {"timeout", "HttpConnectionError", "HttpError"} or failure.startswith("http_")


def _failure_requires_retry(failure: str) -> bool:
    scope, _, reason = failure.partition(":")
    return scope != "risk_documents" or reason in {"pending", "source_stopped", "company_raw_capacity_exhausted"} or IsTransientCompanyDocumentFailure(reason)


def _cancelled(dependencies: CompanyEvidenceDependencies) -> bool:
    return dependencies.cancel_requested is not None and dependencies.cancel_requested()


def _report(
    dependencies: CompanyEvidenceDependencies,
    component: str,
    state: _SyncState,
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
                sources=(sources if sources is not None else (dependencies.disclosures.source,)
                         if component == "announcements" else (dependencies.structured.source,)
                         if component in {"financial", "pledge", "unlock", "annual_income"}
                         else (dependencies.structured.news_source,) if component == "news" else ("local",)),
            )
        )


__all__ = [
    "CompanyEvidenceDependencies",
    "CompanyEvidenceIndex",
    "CompanyDisclosureSource",
    "CompanyStructuredSource",
    "SyncCompanyEvidence",
]
