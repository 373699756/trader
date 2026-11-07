"""Resumable company-evidence preparation, independent of recommendation execution."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, wait
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from functools import partial
from typing import Literal, Protocol

from trader.download.domain.company_evidence import (
    CompanyDateProgress,
    CompanyDocumentExtraction,
    CompanyDocumentPolicies,
    CompanyDocumentTask,
    CompanyDocumentWorkload,
    CompanyOcrDiagnostic,
    CompanyEvidenceRequest,
    CompanyEvidenceSyncResult,
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
    Market,
    StructuredCompanyPage,
    StructuredCompanyQuery,
)
from trader.infra.workers import WorkerExecutor, WorkerResourceRejectedError
from trader.download.application.download_failure import DownloadFailurePort
from trader.download.domain.download_failure import DownloadFailureContext
from trader.download.domain.company_annual_income import CompanyAnnualIncome, CompanyAnnualIncomeRequest
from trader.download.application.company_operations import CompanyOperations, CompanyOperationTiming
from trader.download.application.company_documents import CompanyDocumentRun
from trader.download.application.company_structured import CollectCompanyStructured
from trader.download.application.company_failures import (
    CompanySourceCircuit, CompanyFailureCode, IsTransientCompanyFailure,
    CompanyFailureRequiresRetry,
)
from trader.download.application.company_progress import (
    ReportCompanyProgress, MarkCompanyPublicationPending, PrepareCompanyStockProgress, CompanyCancelled,
)


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
    def request_endpoint(self, component: str) -> str: ...
    @property
    def source(self) -> str: ...
    def fetch(self, partition: DisclosurePartition) -> DisclosurePage: ...
    def document_ready(self, item: Disclosure) -> bool: ...
    def download_document(self, item: Disclosure) -> None: ...
    def extract_document(
        self, item: Disclosure, *, route: Literal["standard", "ocr", "limits"] = "standard",
        page_progress: Callable[[int, int], None] | None = None,
    ) -> CompanyDocumentExtraction: ...
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
    def request_endpoint(self, component: str) -> str: ...
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
    run_id: str = ""


@dataclass
class _SyncState:
    operations: CompanyOperations
    population: int
    population_codes: frozenset[str] = frozenset()
    existing_published_codes: int | None = None
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
    annual_income_circuit: CompanySourceCircuit = field(default_factory=lambda: CompanySourceCircuit())
    annual_income_source_stopped: bool = False
    unpublished_documents: int = 0
    publication_pending_since: float | None = None
    publication_required: bool = True


def SyncCompanyEvidence(
    dependencies: CompanyEvidenceDependencies,
    request: CompanyEvidenceRequest,
) -> CompanyEvidenceSyncResult:
    codes, observed_at = request.codes, request.observed_at
    if observed_at.utcoffset() is None:
        raise ValueError("company evidence requires an aware time")
    started = dependencies.monotonic()
    population = _validated_population(codes)
    state = _SyncState(
        CompanyOperations(dependencies.progress, dependencies.monotonic, dependencies.stock_names,
                          population=population, failures=dependencies.failures, run_id=dependencies.run_id,
                          cancel_requested=dependencies.cancel_requested), len(population),
        population_codes=frozenset(population),
    )
    try:
        return _execute_company(dependencies, request, population, state, started)
    except Exception as exc:
        if CompanyCancelled(dependencies):
            state.failures += ("task:cancelled",)
        else:
            state.operations.record_failure(exc, fallback=DownloadFailureContext("company_sync"))
            state.failures += (f"task:{CompanyFailureCode(exc)}",)
        try:
            ReportCompanyProgress(dependencies, "complete", state, None, completed=state.published,
                                  status="deferred", pending_documents=None, deferred_documents=None,
                                  ocr_documents=None, unextractable_documents=None)
        except Exception:
            # Output failure must not replace the business failure being propagated.
            pass
        raise


def _execute_company(
    dependencies: CompanyEvidenceDependencies, request: CompanyEvidenceRequest,
    population: tuple[str, ...], state: _SyncState, started: float,
) -> CompanyEvidenceSyncResult:
    observed_at, configuration, news_codes = request.observed_at, request.configuration, request.news_codes
    index = dependencies.index
    start_date = observed_at.date() - timedelta(days=365)
    context = _CompanyRunContext(observed_at, started, configuration, start_date)
    documents = CompanyDocumentRun(
        dependencies, population, state, context,
        publish=partial(_publish_batch, dependencies, population, observed_at, started, state),
    )
    if not CompanyCancelled(dependencies):
        ReportCompanyProgress(dependencies, "prepare", state, 1, completed=0)
        state.operations.call("prepare", index.initialize, component="prepare", endpoint="sqlite:company_indexes")
        if not CompanyCancelled(dependencies):
            state.operations.call("prepare", partial(index.prepare_publication, population, observed_at),
                                  component="prepare", endpoint="sqlite:company_publication")
        if not CompanyCancelled(dependencies):
            state.existing_published_codes = state.operations.call("inventory", partial(index.published_count, population),
                                                                  component="prepare", endpoint="sqlite:company_facts")
        if not CompanyCancelled(dependencies):
            workload = documents.workload()
            if not CompanyCancelled(dependencies):
                ReportCompanyProgress(dependencies, "prepare", state, 0, completed=1, status="complete")
                ReportCompanyProgress(dependencies, "resume", state, None, workload=workload, status="complete")
    news_population = tuple(sorted(set(news_codes).intersection(population))) if not request.fill_missing else ()
    components: tuple[Literal["financial", "pledge", "unlock"], ...] = ("financial", "pledge", "unlock")
    for component in components:
        if CompanyCancelled(dependencies):
            break
        if any(item.endswith((":access_denied", ":rate_limited")) for item in state.failures):
            ReportCompanyProgress(dependencies, component, state, None, completed=0, status="deferred")
            continue
        CollectCompanyStructured(dependencies, component, observed_at, state,
                                 timeout_seconds=configuration.source_timeout_seconds)
    if not CompanyCancelled(dependencies):
        _publish_batch(dependencies, population, observed_at, started, state)
    if not CompanyCancelled(dependencies) and documents.workload().local_pending:
        # Recover one bounded local batch; directory discovery must not wait for
        # the complete backlog, and later batches still reuse their local files.
        completed = documents.completed
        documents.run_batch(local_only=True)
        if documents.completed != completed:
            _publish_batch(dependencies, population, observed_at, started, state)
    pending, published = (
        (None, 0) if CompanyCancelled(dependencies) else _run_batches(dependencies, population, state, context, documents)
    )
    published = max(published, state.published)
    news_updates = () if CompanyCancelled(dependencies) else _news(dependencies, news_population, observed_at, state)
    if news_updates and not CompanyCancelled(dependencies):
        _publish_batch(dependencies, population, observed_at, started, state)
    if dependencies.coalesce_publications and not CompanyCancelled(dependencies):
        # Final verification remains mandatory even if the last bounded
        # batch was already published. Scheduling is not a file seal.
        _publish_batch(dependencies, population, observed_at, started, state)
    published = max(published, state.published)
    document_workload: CompanyDocumentWorkload | None = None
    if not CompanyCancelled(dependencies):
        try:
            document_workload = documents.workload(include_attempted=True)
        except InterruptedError:
            if not CompanyCancelled(dependencies):
                raise
    if CompanyCancelled(dependencies):
        state.failures += ("task:cancelled",)
        pending = None
        document_workload = None
    pending_documents = document_workload.due + document_workload.cooling if document_workload is not None else None
    if not CompanyCancelled(dependencies):
        ReportCompanyProgress(dependencies, "finish", state, 1, completed=0)
        state.operations.call("finish", partial(index.finish, observed_at), component="finish", endpoint="sqlite:company_collection")
        if not CompanyCancelled(dependencies):
            ReportCompanyProgress(dependencies, "finish", state, 0, completed=1, status="complete")
    if pending_documents:
        state.failures += ("risk_documents:pending",)
    result_failures = tuple(sorted({failure for failure in state.failures if CompanyFailureRequiresRetry(failure)}))
    ReportCompanyProgress(
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
    documents: CompanyDocumentRun,
) -> tuple[int, int]:
    pending = published = 0
    announcements = _AnnouncementRun(dependencies, state, context)
    announcements.prepare(population)
    try:
        while not CompanyCancelled(dependencies):
            state.batch += 1
            pending = announcements.run_batch(context.configuration.directory_batch_pages)
            if not CompanyCancelled(dependencies):
                documents.run_batch(
                    discovering=announcements.can_continue,
                    discover=lambda: announcements.run_batch(
                        1, wait_for_page=dependencies.directory_executor is None
                    ) if announcements.can_continue else None,
                )
                pending = announcements.run_batch(0)
            if not CompanyCancelled(dependencies) and (
                not dependencies.coalesce_publications or state.publication_required
            ):
                count = _publish_batch(dependencies, population, context.observed_at, context.started, state)
                if count is not None:
                    published = count
            if not announcements.can_continue and not documents.has_due():
                break
    finally:
        announcements.close()
    if not CompanyCancelled(dependencies):
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
    ReportCompanyProgress(dependencies, "facts", state, len(population), completed=0)
    if not CompanyCancelled(dependencies):
        _publish_company_facts(dependencies.index, population, observed_at, state.operations)
        if CompanyCancelled(dependencies):
            return state.published
        state.published = dependencies.index.published_count(population)
        changes = _annual_income(dependencies, population, observed_at, state)
        if CompanyCancelled(dependencies):
            return state.published
        if changes and not CompanyCancelled(dependencies):
            state.changes += changes
            _publish_company_facts(dependencies.index, population, observed_at, state.operations)
            if CompanyCancelled(dependencies):
                return state.published
            state.published = dependencies.index.published_count(population)
        ReportCompanyProgress(dependencies, "publish", state, len(population) - state.published, completed=state.published, status="complete")
        state.unpublished_documents = 0
        state.publication_pending_since = None
        state.publication_required = False
        return state.published
    return None


def _publish_company_facts(index: CompanyEvidenceIndex, population: tuple[str, ...], observed_at: datetime, operations: CompanyOperations) -> int:
    for attempt in range(3):
        try:
            published, timing = operations.call_observed("publication", partial(index.publish, population, observed_at, changed_only=True),
                                                       object_id=f"publication:{observed_at.isoformat()}", component="publish", endpoint="sqlite:company_publication")
            operations.result(timing, "accepted", rows=published, component="publish")
            return published
        except RuntimeError as exc:
            if attempt == 2 or str(exc) not in {"company_publication_source_changed", "company_publication_parent_changed"}:
                raise
            operations.recover(exc, "retry", str(exc))
    raise RuntimeError("company_publication_retry_exhausted")


def _annual_income(
    dependencies: CompanyEvidenceDependencies,
    population: tuple[str, ...],
    observed_at: datetime,
    state: _SyncState,
) -> int:
    if CompanyCancelled(dependencies) or state.annual_income_source_stopped:
        return 0
    requests = tuple(
        request for request in dependencies.index.annual_income_requests(population, observed_at)
        if request not in state.annual_income_attempts
    )
    changes = 0
    for completed, request in enumerate(requests):
        if CompanyCancelled(dependencies) or state.annual_income_source_stopped:
            break
        state.current_stock = None
        ReportCompanyProgress(dependencies, "annual_income", state, len(requests) - completed, completed=completed)
        if CompanyCancelled(dependencies):
            break
        state.annual_income_attempts.add(request)
        try:
            identity = f"annual_income:{request.code}:" + ",".join(day.isoformat() for day in request.report_dates)
            incomes, fetched = state.operations.call_observed(
                "annual_income", partial(dependencies.structured.annual_income, request, observed_at=observed_at),
                code=request.code, source=dependencies.structured.source, object_id=identity,
                component="annual_income", endpoint=dependencies.structured.request_endpoint("annual_income"),
            )
            if CompanyCancelled(dependencies):
                break
            changed, committed = state.operations.call_observed(
                "collection_commit", partial(dependencies.index.commit_annual_income, request, incomes, observed_at),
                code=request.code, source=dependencies.structured.source, object_id=identity,
                component="annual_income", endpoint="sqlite:company_collection",
            )
            changes += changed
            state.operations.result(fetched, "accepted" if incomes else "empty", rows=len(incomes),
                                    commit=committed, changed_rows=changed, component="annual_income")
            state.pages += 1
            state.annual_income_circuit.success()
            remaining = len(requests) - completed - 1
            ReportCompanyProgress(dependencies, "annual_income", state, remaining, completed=completed + 1,
                    status="running" if remaining else "complete")
        except (OSError, RuntimeError, ValueError) as exc:
            if CompanyCancelled(dependencies):
                break
            failure = CompanyFailureCode(exc)
            state.failures += (f"annual_income:{failure}",)
            state.annual_income_source_stopped = state.annual_income_circuit.failed(exc, validation=isinstance(exc, ValueError))
            state.operations.recover(exc, "source_deferred", failure,
                                     fallback=DownloadFailureContext("company_annual_income", supplier=dependencies.structured.source))
            ReportCompanyProgress(dependencies, "annual_income", state, len(requests) - completed, completed=completed, status="deferred")
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
        self._circuit = CompanySourceCircuit()
        self._stopped = False
        self._prefetch: tuple[DisclosurePartition, bool, Future[DisclosurePage]] | None = None
        self._markets: tuple[Market, ...] = ()
        self._fetch_timing: CompanyOperationTiming | None = None

    def prepare(self, population: tuple[str, ...]) -> None:
        self._population = population
        dependencies, state = self._dependencies, self._state
        ReportCompanyProgress(dependencies, "announcements", state, None)
        if CompanyCancelled(dependencies):
            return
        markets = tuple(sorted({EvidenceMarket(code) for code in population}))
        self._markets = markets
        self._queue = deque(self._index.partitions(markets, self._context.observed_at))
        state.reused += self._index.reusable_partitions(markets, self._context.observed_at)
        self._report()

    @property
    def can_continue(self) -> bool:
        return bool(self._queue or self._prefetch is not None) and not self._stopped

    def run_batch(self, max_pages: int, *, wait_for_page: bool = True) -> int:
        dependencies, state = self._dependencies, self._state
        initial_pages = state.pages
        while self.can_continue and not CompanyCancelled(dependencies) and state.pages - initial_pages < max_pages:
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
                    self._fetch_timing = None
                    self._queue.remove(partition)
                    self._report(partition, include_current=True)
                    if CompanyCancelled(dependencies):
                        self._queue.appendleft(partition)
                        break
                    request = self._request(partition)
                    if request is None:
                        continue
                    partition, verifying = request
                    if dependencies.directory_executor is None:
                        page, self._fetch_timing = state.operations.call_observed(
                            "directory", partial(dependencies.disclosures.fetch, partition),
                            source=dependencies.disclosures.source, scope=partition,
                            endpoint=dependencies.disclosures.request_endpoint("directory"),
                            timeout_seconds=self._context.configuration.source_timeout_seconds,
                        )
                        if CompanyCancelled(dependencies):
                            self._queue.appendleft(partition)
                            break
                        self._accept(partition, page, verifying=verifying)
                        continue
                    self._prefetch = (
                        partition,
                        verifying,
                        state.operations.submit("directory", partial(dependencies.disclosures.fetch, partition), dependencies.directory_executor,
                                                source=dependencies.disclosures.source, scope=partition,
                                                endpoint=dependencies.disclosures.request_endpoint("directory"),
                                                timeout_seconds=self._context.configuration.source_timeout_seconds),
                    )
                partition, verifying, future = self._prefetch
                if not future.done():
                    if not wait_for_page:
                        break
                    wait((future,), timeout=0.1, return_when=FIRST_COMPLETED)
                    continue
                self._prefetch = None
                if future.cancelled():
                    state.operations.discard_future(future)
                    if not CompanyCancelled(dependencies):
                        raise WorkerResourceRejectedError("company_directory_request_cancelled")
                    self._queue.appendleft(partition)
                    break
                self._fetch_timing = state.operations.take_future(future)
                page = future.result()
                if CompanyCancelled(dependencies):
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
            self._state.operations.discard_future(future)
            self._prefetch = None

    def _publish_due(self) -> None:
        state, dependencies = self._state, self._dependencies
        pending_since = state.publication_pending_since
        if (
            dependencies.coalesce_publications
            and pending_since is not None
            and not CompanyCancelled(dependencies)
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
        ReportCompanyProgress(
            self._dependencies,
            "announcements",
            self._state,
            _pending_count(self._queue, self._deferred, current),
            scope=partition,
            dates=_date_progress(self._context.observed_at.date(), self._queue, self._deferred, current, self._context.start_date),
            market_dates=tuple((market, _date_progress(
                self._context.observed_at.date(),
                deque(item for item in self._queue if item.market == market),
                {item for item in self._deferred if item[0] == market},
                current if current is not None and current.market == market else None,
                self._context.start_date,
            )) for market in self._markets),
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
        MarkCompanyPublicationPending(self._dependencies, self._state)
        if verifying:
            try:
                _, committed = self._state.operations.call_observed(
                    "collection_commit", partial(self._index.commit_query_coverage, partition, page, self._context.observed_at),
                    source=self._dependencies.disclosures.source, scope=partition,
                    endpoint="sqlite:company_collection",
                )
            except DisclosureCoverageMismatch as exc:
                if exc.missing_rows and self._refresh_queries(partition):
                    self._state.operations.recover(exc, "retry", CompanyFailureCode(exc))
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
                self._state.operations.recover(exc, "retry", CompanyFailureCode(exc))
                self._report(partition)
                return
            self._state.completed += 1
            self._circuit.success()
            if self._fetch_timing is not None:
                self._state.operations.result(self._fetch_timing, "empty" if not page.rows else "accepted",
                                              rows=len(page.rows), commit=committed, component="announcements")
        elif page.total > 3000:
            self._circuit.success()
            self._split(partition)
            if self._fetch_timing is not None:
                self._state.operations.result(self._fetch_timing, "split", rows=len(page.rows), component="announcements")
        else:
            changes, committed = self._state.operations.call_observed(
                "collection_commit", partial(self._index.commit_page, partition, page, self._context.observed_at),
                source=self._dependencies.disclosures.source, scope=partition,
                endpoint="sqlite:company_collection",
            )
            self._state.changes += changes
            if self._fetch_timing is not None:
                self._state.operations.result(self._fetch_timing, "empty" if not page.rows else "accepted",
                                              rows=len(page.rows), commit=committed, changed_rows=changes, component="announcements")
            self._circuit.success()
            self._advance(partition, page)
        self._report(replace(partition, expected_rows=page.total))

    def _refresh_queries(self, partition: DisclosurePartition) -> bool:
        refreshed = False
        for saved in self._index.disclosure_queries(partition, include_completed=True) or ():
            if CompanyCancelled(self._dependencies):
                return False
            probe = replace(saved, page=1, received_rows=0)
            page, fetched = self._state.operations.call_observed("directory", partial(self._dependencies.disclosures.fetch, probe),
                                                                 source=self._dependencies.disclosures.source, scope=probe,
                                                                 endpoint=self._dependencies.disclosures.request_endpoint("directory"),
                                                                 timeout_seconds=self._context.configuration.source_timeout_seconds)
            self._state.pages += 1
            changes, committed = self._state.operations.call_observed("collection_commit", partial(self._index.refresh_disclosure_query, probe, page),
                                                                      source=self._dependencies.disclosures.source, scope=probe,
                                                                      endpoint="sqlite:company_collection")
            if changes is None:
                self._state.operations.result(fetched, "unchanged", rows=len(page.rows), component="announcements")
            else:
                self._state.operations.result(fetched, "empty" if not page.rows else "accepted",
                                              rows=len(page.rows), commit=committed, changed_rows=changes, component="announcements")
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
        if CompanyCancelled(self._dependencies):
            self._queue.appendleft(partition)
            return True
        if str(exc) == "company_evidence_persistence_unavailable":
            raise exc
        if CompanyFailureCode(exc) in {"cninfo_duplicate_page_links", "cninfo_page_limit"} or (
            CompanyFailureCode(exc) == "cninfo_partition_changed" and partition.start < partition.end
        ):
            try:
                self._split(partition)
                self._state.operations.recover(exc, "retry", CompanyFailureCode(exc))
                return False
            except (OSError, RuntimeError, ValueError) as split_exc:
                exc = split_exc
        if CompanyCancelled(self._dependencies):
            self._queue.appendleft(partition)
            return True
        failure = CompanyFailureCode(exc)
        fallback = DownloadFailureContext(
            "company_directory", supplier=self._dependencies.disclosures.source, window_start=partition.start,
            window_end=partition.end, market=partition.market, page=partition.page, page_size=partition.page_size,
            component="announcements", object_id=f"{partition.market}:{partition.start}:{partition.end}:{partition.page}",
        )
        self._state.operations.record_failure(exc, fallback=fallback)
        self._state.operations.register_failure(exc, partial(self._index.partition_failure, partition, failure))
        MarkCompanyPublicationPending(self._dependencies, self._state)
        self._state.failures += (f"announcements:{failure}",)
        self._deferred.add(_query_key(partition))
        configuration = self._context.configuration
        delay = (
            configuration.document_transient_retry_seconds
            if IsTransientCompanyFailure(exc) or failure in {"cninfo_partition_changed", "cninfo_query_coverage_mismatch"}
            else configuration.document_failure_retry_seconds
        )
        retry_at = self._now() + timedelta(seconds=delay)
        self._state.operations.register_failure(exc, partial(self._index.defer_partition, partition, retry_at))
        if CompanyCancelled(self._dependencies):
            return True
        stopped = self._circuit.failed(
            exc, validation=failure not in {"cninfo_query_unsplittable", "cninfo_single_day_page_limit"}
        )
        if stopped:
            self._state.failures += ("announcements:source_stopped",)
        self._state.operations.recover(exc, "source_deferred" if stopped else "cooldown", failure,
                                       retry_at=retry_at, record=False, fallback=fallback)
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
        ReportCompanyProgress(dependencies, "news", state, 0, completed=0, status="complete")
        return ()
    ReportCompanyProgress(dependencies, "news", state, total, completed=0)
    for code in () if structured_blocked else candidates:
        if CompanyCancelled(dependencies):
            break
        PrepareCompanyStockProgress(dependencies, state, code)
        due, checked = state.operations.call_observed("news", partial(index.component_due, "news", code, observed_at),
                                                      code=code, source="local", object_id=f"news:{code}",
                                                      component="news", endpoint="sqlite:company_component_checks")
        if not due:
            state.reused += 1
            completed += 1
            state.operations.result(checked, "reused", component="news", original_source=dependencies.structured.news_source)
            continue
        attempted.append(code)
        ReportCompanyProgress(dependencies, "news", state, total - completed, completed=completed)
        try:
            rows, fetched = state.operations.call_observed("news", partial(dependencies.structured.news, code, observed_at=observed_at),
                                                           code=code, source=dependencies.structured.news_source, object_id=f"news:{code}",
                                                           component="news", endpoint=dependencies.structured.request_endpoint("news"))
            changed, committed = state.operations.call_observed("collection_commit", partial(index.commit_news, code, rows, observed_at),
                                                               code=code, source=dependencies.structured.news_source, object_id=f"news:{code}",
                                                               component="news", endpoint="sqlite:company_collection")
            state.changes += changed
            state.operations.result(fetched, "accepted" if rows else "empty", rows=len(rows), commit=committed,
                                    changed_rows=changed, component="news")
            MarkCompanyPublicationPending(dependencies, state)
            completed += 1
        except (OSError, RuntimeError, ValueError) as exc:
            if CompanyCancelled(dependencies):
                break
            failure = CompanyFailureCode(exc)
            state.operations.register_failure(exc, partial(index.component_failure, "news", code, failure))
            state.failures += (f"news:{failure}",)
            state.operations.recover(exc, "source_deferred", failure)
            break
    ReportCompanyProgress(
        dependencies,
        "news",
        state,
        total - completed,
        completed=completed,
        status="complete" if completed == total else "deferred",
    )
    return tuple(attempted)



__all__ = [
    "CompanyEvidenceDependencies",
    "CompanyEvidenceIndex",
    "CompanyDisclosureSource",
    "CompanyStructuredSource",
    "SyncCompanyEvidence",
]
