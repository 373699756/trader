"""Resumable company-evidence preparation, independent of recommendation execution."""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, wait
from dataclasses import dataclass, replace
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
    CompanyEvidenceRequest,
    CompanyEvidenceSyncResult,
    CompanySourceFailure,
    CompanyStockProgress,
    CompanySyncConfiguration,
    Disclosure,
    DisclosureCoverageMismatch,
    DisclosurePage,
    DisclosurePartition,
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
from trader.infra.workers import SubmitOrReject, WorkerExecutor, WorkerResourceRejectedError


class CompanyEvidenceIndex(Protocol):
    def initialize(self) -> None: ...
    def partitions(self, markets: Sequence[Market], observed_at: datetime) -> tuple[DisclosurePartition, ...]: ...
    def reusable_partitions(self, markets: Sequence[Market], observed_at: datetime) -> int: ...
    def split_partition(
        self, partition: DisclosurePartition, observed_at: datetime
    ) -> tuple[DisclosurePartition, ...]: ...
    def commit_page(self, partition: DisclosurePartition, page: DisclosurePage, observed_at: datetime) -> int: ...
    def partition_failure(self, partition: DisclosurePartition, failure: str) -> None: ...
    def disclosure_queries(self, partition: DisclosurePartition) -> tuple[DisclosurePartition, ...] | None: ...
    def disclosure_members(self, partition: DisclosurePartition) -> tuple[DisclosureSecurity, ...]: ...
    def split_query(
        self, partition: DisclosurePartition, queries: tuple[DisclosureQuery, ...]
    ) -> tuple[DisclosurePartition, ...]: ...
    def commit_query_coverage(
        self, partition: DisclosurePartition, page: DisclosurePage, observed_at: datetime
    ) -> None: ...
    def partition_retry_after(self, partition: DisclosurePartition) -> datetime | None: ...
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
    ) -> None: ...
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
    def fetch(self, partition: DisclosurePartition) -> DisclosurePage: ...
    def document(
        self, item: Disclosure, *, route: Literal["standard", "ocr", "limits"] = "standard"
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
    ocr_identity: str = "unavailable"


@dataclass
class _SyncState:
    pages: int = 0
    changes: int = 0
    reused: int = 0
    completed: int = 0
    current_stock: CompanyStockProgress | None = None
    failures: tuple[str, ...] = ()
    failed_documents: int = 0
    batch: int = 0


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
    state = _SyncState()
    population = _validated_population(codes)
    context = _CompanyRunContext(observed_at, started, configuration)
    documents = _DocumentRun(dependencies, population, state, context, request.document_repair)
    if not _cancelled(dependencies):
        index.initialize()
        index.prepare_publication(population, observed_at)
    news_population = tuple(sorted(set(news_codes).intersection(population))) if request.document_repair is None else ()
    if request.document_repair is not None:
        pending = 0
        while documents.has_due():
            state.batch += 1
            documents.run_batch(discovering=False)
            if documents.stopped or _cancelled(dependencies):
                break
        published = 0 if _cancelled(dependencies) else index.published_count(population)
    else:
        components: tuple[Literal["financial", "pledge", "unlock"], ...] = ("financial", "pledge", "unlock")
        for component in components:
            if _cancelled(dependencies):
                break
            if any(item.endswith((":access_denied", ":rate_limited")) for item in state.failures):
                _report(dependencies, component, state, None, completed=0, status="deferred")
                continue
            _structured(dependencies, component, observed_at, state)
        pending, published = (
            (0, 0) if _cancelled(dependencies) else _run_batches(dependencies, population, state, context, documents)
        )
        news_updates = () if _cancelled(dependencies) else _news(dependencies, news_population, observed_at, state)
        if news_updates and not _cancelled(dependencies):
            _publish_batch(dependencies, population, observed_at, started, state)
    if _cancelled(dependencies):
        state.failures += ("task:cancelled",)
    document_workload = (
        CompanyDocumentWorkload(0, 0) if _cancelled(dependencies) else documents.workload(include_attempted=True)
    )
    pending_documents = document_workload.due + document_workload.cooling
    if not _cancelled(dependencies):
        index.finish(observed_at + timedelta(seconds=max(0, dependencies.monotonic() - started)))
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
        deferred_documents=document_workload.cooling,
        ocr_documents=document_workload.ocr,
        unextractable_documents=document_workload.unextractable,
        next_document_retry_at=document_workload.next_retry_at,
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
        document_workload.ocr,
        document_workload.unextractable,
        document_workload.next_retry_at,
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
    while not _cancelled(dependencies):
        state.batch += 1
        pending = announcements.run_batch(context.configuration.directory_batch_pages)
        if not _cancelled(dependencies):
            documents.run_batch(discovering=announcements.can_continue)
        if not _cancelled(dependencies):
            count = _publish_batch(dependencies, population, context.observed_at, context.started, state)
            if count is not None:
                published = count
        if not announcements.can_continue and (documents.stopped or not documents.has_due()):
            break
    if not _cancelled(dependencies):
        published = dependencies.index.published_count(population)
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
        for attempt in range(3):
            try:
                updated = dependencies.index.publish(
                    population,
                    observed_at + timedelta(seconds=max(0, dependencies.monotonic() - started)),
                    changed_only=True,
                )
                break
            except RuntimeError as exc:
                if attempt == 2 or str(exc) not in {
                    "company_publication_source_changed",
                    "company_publication_parent_changed",
                }:
                    raise
        published = dependencies.index.published_count(population)
        _report(dependencies, "publish", state, 0, completed=updated, status="complete")
        return published
    return None


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

    def prepare(self, population: tuple[str, ...]) -> None:
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
        return bool(self._queue) and not self._stopped

    def run_batch(self, max_pages: int) -> int:
        dependencies, state = self._dependencies, self._state
        initial_pages = state.pages
        while self.can_continue and not _cancelled(dependencies) and state.pages - initial_pages < max_pages:
            partition = self._queue.popleft()
            self._report(partition, include_current=True)
            if _cancelled(dependencies):
                self._queue.appendleft(partition)
                break
            try:
                self._consume(partition)
            except (OSError, RuntimeError, ValueError) as exc:
                if self._failed(partition, exc):
                    self._stopped = True
                    break
        pending = _pending_count(self._queue, self._deferred)
        self._report(status=("deferred" if not self.can_continue else "running") if pending else "complete")
        return pending

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
        current = partition if include_current else None
        queued = (*self._queue, *((current,) if current else ()))
        _report(
            self._dependencies,
            "announcements",
            self._state,
            _pending_count(self._queue, self._deferred, current),
            scope=partition,
            dates=_date_progress(self._context.observed_at.date(), self._queue, self._deferred, current),
            status=status,
            activity=activity,
            known_pages=sum(
                max(0, (item.expected_rows + item.page_size - 1) // item.page_size - item.page + 1)
                for item in queued
                if item.expected_rows is not None
            ),
            unknown_queries=sum(item.expected_rows is None for item in queued),
        )

    def _consume(self, partition: DisclosurePartition) -> None:
        retry_after = self._index.partition_retry_after(partition)
        if retry_after is not None and retry_after > self._now():
            self._deferred.add(_query_key(partition))
            return
        children = self._index.disclosure_queries(partition) if partition.query is None else None
        if children:
            self._queue.extendleft(reversed([child for child in children if _query_key(child) not in self._deferred]))
            return
        verifying = children == ()
        if verifying:
            partition = replace(partition, page=1, expected_rows=None, received_rows=0)
        page = self._dependencies.disclosures.fetch(partition)
        self._state.pages += 1
        if verifying:
            try:
                self._index.commit_query_coverage(partition, page, self._context.observed_at)
            except DisclosureCoverageMismatch as exc:
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
            self._state.changes += self._index.commit_page(partition, page, self._now())
            self._circuit.success()
            self._advance(partition, page)
        self._report(replace(partition, expected_rows=page.total))

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
        if _failure(exc) in {"cninfo_duplicate_page_links", "cninfo_page_limit"}:
            try:
                self._split(partition)
                return False
            except (OSError, RuntimeError, ValueError) as split_exc:
                exc = split_exc
        if _cancelled(self._dependencies):
            self._queue.appendleft(partition)
            return True
        failure = _failure(exc)
        self._index.partition_failure(partition, failure)
        self._state.failures += (f"announcements:{failure}",)
        self._deferred.add(_query_key(partition))
        configuration = self._context.configuration
        delay = (
            configuration.document_transient_retry_seconds
            if _transient(exc)
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
) -> CompanyDateProgress:
    start = end - timedelta(days=365)
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
        repair: Literal["ocr", "limits"] | None,
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
        self.policies = BuildCompanyDocumentPolicies(configuration, dependencies.ocr_identity)
        self.repair = repair
        self.circuit = _SourceCircuit()
        self.stopped = False
        self.completed = self.attempted = 0
        self.elapsed = 0.0
        self.batch_started: float | None = None

    def now(self) -> datetime:
        return self.observed_at + timedelta(seconds=max(0, self.dependencies.monotonic() - self.started))

    def workload(self, *, include_attempted: bool = False) -> CompanyDocumentWorkload:
        return self.dependencies.index.document_workload(
            self.population,
            self.now(),
            self.policies,
            excluded=() if include_attempted else tuple(self.failed_identities),
            repair=self.repair,
        )

    def has_due(self) -> bool:
        return not _cancelled(self.dependencies) and self.workload().due > 0

    def run_batch(self, *, discovering: bool = False) -> None:
        dependencies, index = self.dependencies, self.dependencies.index
        if self.stopped or _cancelled(dependencies):
            return
        self.state.current_stock = None
        workload = self.workload()
        queue = deque(
            index.pending_documents(
                self.population,
                self.now(),
                limit=self.configuration.document_batch_size,
                policies=self.policies,
                excluded=tuple(self.failed_identities),
                repair=self.repair,
            )
        )
        pending_documents: dict[Future[CompanyDocumentExtraction], tuple[int, CompanyDocumentTask]] = {}
        available = deque(range(len(self.sources)))
        completed_before = self.completed
        self.batch_started = dependencies.monotonic()
        self.report(workload, completed_before)
        while queue or pending_documents:
            while queue and available and not self.stopped and not _cancelled(dependencies):
                slot = available.popleft()
                task = queue.popleft()
                item = task.disclosure
                _prepare_stock_progress(dependencies, self.state, item.codes[0])
                self.report(workload, completed_before, discovering=discovering)
                if _cancelled(dependencies):
                    break
                pending_documents[self._submit(self.sources[slot], task)] = (slot, task)
                self.attempted += 1
            if self.stopped or _cancelled(dependencies):
                queue.clear()
                for future in pending_documents:
                    future.cancel()
            if not pending_documents:
                break
            done, _ = wait(pending_documents, timeout=0.1, return_when=FIRST_COMPLETED)
            for future in done:
                slot, task = pending_documents.pop(future)
                available.append(slot)
                self._accept(future, task)
                self.report(workload, completed_before, discovering=discovering)
        self.elapsed += max(0.0, dependencies.monotonic() - self.batch_started)
        self.batch_started = None
        self.report(
            workload,
            completed_before,
            status="deferred" if self.stopped else "complete",
            discovering=discovering,
        )

    def _accept(self, future: Future[CompanyDocumentExtraction], task: CompanyDocumentTask) -> None:
        if future.cancelled():
            return
        item = task.disclosure
        _prepare_stock_progress(self.dependencies, self.state, item.codes[0])
        try:
            extraction = future.result()
        except WorkerResourceRejectedError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            if not _cancelled(self.dependencies):
                self.failed(item, exc)
        else:
            self.circuit.success()
            if self.dependencies.index.commit_document(item, extraction, self.now()):
                self.completed += 1
                self.state.changes += 1

    def _submit(
        self, source: CompanyDisclosureSource, task: CompanyDocumentTask
    ) -> Future[CompanyDocumentExtraction]:
        function = source.document if task.route == "standard" else partial(source.document, route=task.route)
        if self.executor is not None:
            return SubmitOrReject(self.executor, function, task.disclosure)
        # Explicit single-worker configuration also supports caller-thread execution.
        completed: Future[CompanyDocumentExtraction] = Future()
        try:
            completed.set_result(function(task.disclosure))
        except (OSError, RuntimeError, ValueError) as exc:
            completed.set_exception(exc)
        return completed

    def failed(self, item: Disclosure, exc: BaseException) -> None:
        self.failed_identities.add((item.source, item.identity, item.digest))
        failure = _failure(exc)
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
        )
        if self.circuit.failed(exc):
            self.stopped = True
            self.state.failures += ("risk_documents:source_stopped",)

    def report(
        self,
        workload: CompanyDocumentWorkload,
        completed_before: int,
        *,
        status: EvidenceProgressState = "running",
        discovering: bool = False,
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
            rows = dependencies.structured.news(code, observed_at=observed_at)
            state.changes += index.commit_news(code, rows, observed_at)
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
        while not _cancelled(dependencies):
            completed = query.page - 1
            _report(dependencies, component, state, pending, completed=completed, scope=query)
            if _cancelled(dependencies):
                break
            page = dependencies.structured.fetch(query, observed_at=now)
            state.pages += 1
            state.changes += index.commit_structured(query, page, now)
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
    return failure in {"timeout", "HttpConnectionError"} or failure.startswith("http_")


def _failure_requires_retry(failure: str) -> bool:
    scope, _, reason = failure.partition(":")
    return scope != "risk_documents" or reason in {"pending", "source_stopped"} or IsTransientCompanyDocumentFailure(reason)


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
    pending_documents: int = 0,
    attempted_documents: int = 0,
    deferred_documents: int = 0,
    activity: Literal["download", "repair"] = "download",
    dates: CompanyDateProgress | None = None,
    known_pages: int = 0,
    unknown_queries: int = 0,
    document_rate: float | None = None,
    document_eta_seconds: float | None = None,
    ocr_documents: int = 0,
    unextractable_documents: int = 0,
    next_document_retry_at: datetime | None = None,
    discovering_documents: bool = False,
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
                state.current_stock if component in {"risk_documents", "news"} else None,
                dates=dates,
                failed_documents=state.failed_documents if component == "risk_documents" else 0,
                batch=state.batch,
                known_pages=known_pages,
                unknown_queries=unknown_queries,
                document_rate=document_rate,
                document_eta_seconds=document_eta_seconds,
                ocr_documents=ocr_documents,
                unextractable_documents=unextractable_documents,
                next_document_retry_at=next_document_retry_at,
                discovering_documents=discovering_documents,
            )
        )


__all__ = [
    "CompanyEvidenceDependencies",
    "CompanyEvidenceIndex",
    "CompanyDisclosureSource",
    "CompanyStructuredSource",
    "SyncCompanyEvidence",
]
