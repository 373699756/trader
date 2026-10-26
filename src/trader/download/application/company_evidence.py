"""Resumable company-evidence preparation, independent of recommendation execution."""

from __future__ import annotations

import math
import re
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Literal, Protocol

from trader.download.domain.company_evidence import (
    CompanyEvidenceSyncResult,
    CompanySourceFailure,
    CompanySyncConfiguration,
    Disclosure,
    DisclosureCoverageMismatch,
    DisclosurePage,
    DisclosurePartition,
    DisclosureQuery,
    DisclosureSecurity,
    DocumentRetry,
    EvidenceMarket,
    EvidenceProgressState,
    EvidenceSyncProgress,
    Market,
    StructuredCompanyPage,
    StructuredCompanyQuery,
)


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
    def pending_documents(self, codes: Sequence[str], observed_at: datetime) -> tuple[Disclosure, ...]: ...
    def commit_document(self, item: Disclosure, text: str) -> None: ...
    def document_retry(self, item: Disclosure, policy: str) -> DocumentRetry | None: ...
    def document_failure(
        self, item: Disclosure, policy: str, failure: str, attempted_at: datetime, retry_after: datetime
    ) -> None: ...
    def component_due(self, component: str, scope: str, observed_at: datetime) -> bool: ...
    def component_since(self, component: str, scope: str, observed_at: datetime) -> date: ...
    def structured_cursor(self, query: StructuredCompanyQuery) -> StructuredCompanyQuery: ...
    def commit_structured(
        self, query: StructuredCompanyQuery, page: StructuredCompanyPage, observed_at: datetime
    ) -> int: ...
    def component_failure(self, component: str, scope: str, failure: str) -> None: ...
    def commit_news(self, code: str, rows: Sequence[Disclosure], observed_at: datetime) -> int: ...
    def publish(self, codes: Sequence[str], observed_at: datetime) -> int: ...


class CompanyDisclosureSource(Protocol):
    def fetch(self, partition: DisclosurePartition, *, deadline: float) -> DisclosurePage: ...
    def document(self, item: Disclosure, *, deadline: float) -> str: ...
    def split_queries(self, partition: DisclosurePartition, *, deadline: float) -> tuple[DisclosureQuery, ...]: ...
    def repair_queries(
        self,
        partition: DisclosurePartition,
        members: tuple[DisclosureSecurity, ...],
        *,
        missing_rows: int,
        deadline: float,
        report: Callable[[DisclosurePartition], None] | None = None,
    ) -> tuple[DisclosureQuery, ...]: ...


class CompanyStructuredSource(Protocol):
    def pledge_day(self, observed_at: datetime, *, deadline: float) -> date: ...
    def fetch(
        self, query: StructuredCompanyQuery, *, observed_at: datetime, deadline: float
    ) -> StructuredCompanyPage: ...
    def news(self, code: str, *, observed_at: datetime, deadline: float) -> tuple[Disclosure, ...]: ...


@dataclass(frozen=True)
class CompanyEvidenceDependencies:
    index: CompanyEvidenceIndex
    disclosures: CompanyDisclosureSource
    structured: CompanyStructuredSource
    monotonic: Callable[[], float]
    progress: Callable[[EvidenceSyncProgress], None] | None = None
    cancel_requested: Callable[[], bool] | None = None


@dataclass
class _SyncState:
    pages: int = 0
    changes: int = 0
    reused: int = 0
    completed: int = 0
    failures: tuple[str, ...] = ()


@dataclass
class _SourceCircuit:
    transport_failures: int = 0
    validation_failures: int = 0

    def success(self) -> None:
        self.transport_failures = self.validation_failures = 0

    def failed(self, exc: BaseException, *, validation: bool = False) -> bool:
        if isinstance(exc, CompanySourceFailure) and exc.category in {"access_denied", "rate_limited"}:
            return True
        if _transient(exc):
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
    codes: Sequence[str],
    observed_at: datetime,
    *,
    configuration: CompanySyncConfiguration,
    news_codes: Sequence[str] = (),
) -> CompanyEvidenceSyncResult:
    budget_seconds = configuration.budget_seconds
    if observed_at.utcoffset() is None or not math.isfinite(budget_seconds) or budget_seconds <= 0:
        raise ValueError("company evidence requires an aware time and positive budget")
    started = dependencies.monotonic()
    deadline = started + budget_seconds
    index = dependencies.index
    state = _SyncState()
    population = _validated_population(codes)
    if not _cancelled(dependencies):
        index.initialize()
    news_population = tuple(sorted(set(news_codes).intersection(population)))
    components: tuple[Literal["financial", "pledge", "unlock"], ...] = ("financial", "pledge", "unlock")
    for component in components:
        if _cancelled(dependencies):
            break
        if any(item.endswith((":access_denied", ":rate_limited")) for item in state.failures):
            _report(dependencies, component, state, None, completed=0, status="deferred")
            continue
        _structured(
            dependencies,
            component,
            observed_at,
            min(deadline, dependencies.monotonic() + configuration.structured_component_seconds),
            state,
        )
    pending = published = 0
    if not _cancelled(dependencies):
        pending = _announcements(
            dependencies,
            population,
            state,
            _AnnouncementSchedule(
                observed_at,
                started,
                started + budget_seconds * configuration.announcements_deadline_fraction,
                configuration,
            ),
        )
    if not _cancelled(dependencies):
        _documents(
            dependencies,
            population,
            observed_at,
            started + budget_seconds * configuration.documents_deadline_fraction,
            state,
            configuration,
            started,
        )
    if not _cancelled(dependencies):
        _report(dependencies, "publish", state, len(population), completed=0)
        if not _cancelled(dependencies):
            published = index.publish(population, observed_at)
            _report(dependencies, "publish", state, len(population) - published, completed=published, status="complete")
    news_updates = (
        () if _cancelled(dependencies) else _news(dependencies, news_population, observed_at, deadline, state)
    )
    if news_updates and not _cancelled(dependencies):
        _report(dependencies, "publish", state, len(news_updates), completed=0)
        if not _cancelled(dependencies):
            updated = index.publish(news_updates, observed_at)
            _report(dependencies, "publish", state, len(news_updates) - updated, completed=updated, status="complete")
    if dependencies.monotonic() >= deadline:
        state.failures += ("task:deadline",)
    if _cancelled(dependencies):
        state.failures += ("task:cancelled",)
    pending_documents = (
        0 if _cancelled(dependencies) else len(_unique_documents(index.pending_documents(population, observed_at)))
    )
    if pending_documents:
        state.failures += ("risk_documents:pending",)
    _report(
        dependencies,
        "complete",
        state,
        pending,
        completed=published,
        status="deferred" if state.failures or pending else "complete",
        pending_documents=pending_documents,
    )
    return CompanyEvidenceSyncResult(
        observed_at,
        len(population),
        published,
        state.pages,
        state.changes,
        state.reused,
        pending,
        tuple(sorted(set(state.failures))),
        max(0.0, dependencies.monotonic() - started),
        pending_documents,
        len(news_population),
        "published_population" if news_population else "uncovered",
    )


def _validated_population(codes: Sequence[str]) -> tuple[str, ...]:
    population = tuple(sorted(set(codes)))
    if not population:
        raise ValueError("company_evidence_empty_population")
    for code in population:
        EvidenceMarket(code)
    return population


@dataclass(frozen=True)
class _AnnouncementSchedule:
    observed_at: datetime
    started: float
    deadline: float
    configuration: CompanySyncConfiguration


class _AnnouncementRun:
    """Own the dispatch queue and failure circuit for one announcement stage."""

    def __init__(
        self, dependencies: CompanyEvidenceDependencies, state: _SyncState, schedule: _AnnouncementSchedule
    ) -> None:
        self._dependencies, self._state, self._schedule = dependencies, state, schedule
        self._index = dependencies.index
        self._queue: deque[DisclosurePartition] = deque()
        self._deferred: set[tuple[Market, date, date, str]] = set()
        self._circuit = _SourceCircuit()

    def run(self, population: tuple[str, ...]) -> int:
        dependencies, state = self._dependencies, self._state
        _report(dependencies, "announcements", state, None)
        if _cancelled(dependencies):
            return 0
        markets = tuple(sorted({EvidenceMarket(code) for code in population}))
        self._queue = deque(self._index.partitions(markets, self._schedule.observed_at))
        state.reused += self._index.reusable_partitions(markets, self._schedule.observed_at)
        while self._queue and _running(dependencies, self._schedule.deadline):
            partition = self._queue.popleft()
            self._report(partition, include_current=True)
            if _cancelled(dependencies):
                self._queue.appendleft(partition)
                break
            try:
                self._consume(partition)
            except (OSError, RuntimeError, ValueError) as exc:
                if self._failed(partition, exc):
                    break
        pending = _pending_count(self._queue, self._deferred)
        _record_deadline(dependencies, state, "announcements", pending, self._schedule.deadline)
        _report(dependencies, "announcements", state, pending, status="deferred" if pending else "complete")
        return pending

    def _now(self) -> datetime:
        return self._schedule.observed_at + timedelta(
            seconds=max(0, self._dependencies.monotonic() - self._schedule.started)
        )

    def _report(self, partition: DisclosurePartition, *, include_current: bool = False) -> None:
        _report(
            self._dependencies,
            "announcements",
            self._state,
            _pending_count(self._queue, self._deferred, partition if include_current else None),
            scope=partition,
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
        page = self._dependencies.disclosures.fetch(partition, deadline=self._schedule.deadline)
        self._state.pages += 1
        if verifying:
            try:
                self._index.commit_query_coverage(partition, page, self._schedule.observed_at)
            except DisclosureCoverageMismatch as exc:
                repairs = (
                    self._dependencies.disclosures.repair_queries(
                        partition,
                        self._index.disclosure_members(partition),
                        missing_rows=exc.missing_rows,
                        deadline=self._schedule.deadline,
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
            self._state.changes += self._index.commit_page(partition, page, self._schedule.observed_at)
            self._circuit.success()
            self._advance(partition, page)
        self._report(replace(partition, expected_rows=page.total))

    def _repair_progress(self, partition: DisclosurePartition) -> None:
        self._state.pages += 1
        _report(
            self._dependencies,
            "announcements",
            self._state,
            _pending_count(self._queue, self._deferred, partition),
            scope=partition,
            activity="repair",
        )

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
            children = self._index.split_partition(partition, self._schedule.observed_at)
            self._queue = _replace_split_queue(self._queue, partition, children, self._deferred)
        else:
            queries = self._dependencies.disclosures.split_queries(partition, deadline=self._schedule.deadline)
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
        failure = _failure(exc)
        self._index.partition_failure(partition, failure)
        self._state.failures += (f"announcements:{failure}",)
        self._deferred.add(_query_key(partition))
        configuration = self._schedule.configuration
        delay = (
            configuration.document_transient_retry_seconds
            if _transient(exc)
            else configuration.document_failure_retry_seconds
        )
        self._index.defer_partition(partition, self._now() + timedelta(seconds=delay))
        if not _running(self._dependencies, self._schedule.deadline):
            return True
        stopped = self._circuit.failed(
            exc, validation=failure not in {"cninfo_query_unsplittable", "cninfo_single_day_page_limit"}
        )
        if stopped:
            self._state.failures += ("announcements:source_stopped",)
        return stopped


def _announcements(
    dependencies: CompanyEvidenceDependencies,
    population: tuple[str, ...],
    state: _SyncState,
    schedule: _AnnouncementSchedule,
) -> int:
    return _AnnouncementRun(dependencies, state, schedule).run(population)


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


def _documents(
    dependencies: CompanyEvidenceDependencies,
    population: tuple[str, ...],
    observed_at: datetime,
    deadline: float,
    state: _SyncState,
    configuration: CompanySyncConfiguration,
    started: float,
) -> None:
    index = dependencies.index
    _report(dependencies, "risk_documents", state, None, completed=0)
    pending_documents = deque(_unique_documents(index.pending_documents(population, observed_at)))
    total = len(pending_documents)
    completed = attempted = deferred = 0
    limits = configuration.document_limits
    policy = f"text_diagnostics_2:{configuration.document_parse_timeout_seconds}:{limits.max_bytes}:{limits.max_pages}:{limits.max_text_characters}"
    circuit = _SourceCircuit()
    _report(dependencies, "risk_documents", state, total, completed=0)
    while pending_documents and _running(dependencies, deadline):
        item = pending_documents.popleft()
        now = observed_at + timedelta(seconds=max(0, dependencies.monotonic() - started))
        retry = index.document_retry(item, policy)
        if retry is not None and retry.retry_after > now:
            deferred += 1
            state.failures += (f"risk_documents:{retry.failure}",)
            _report(
                dependencies,
                "risk_documents",
                state,
                total - completed,
                completed=completed,
                attempted_documents=attempted,
                deferred_documents=deferred,
            )
            continue
        attempted += 1
        _report(
            dependencies,
            "risk_documents",
            state,
            total - completed,
            completed=completed,
            attempted_documents=attempted,
            deferred_documents=deferred,
        )
        if not _running(dependencies, deadline):
            break
        try:
            text = dependencies.disclosures.document(item, deadline=deadline)
            circuit.success()
            index.commit_document(item, text)
            completed += 1
            state.changes += 1
        except (OSError, RuntimeError, ValueError) as exc:
            if _cancelled(dependencies):
                break
            failure = _failure(exc)
            if str(exc) == "company_evidence_persistence_unavailable":
                raise
            state.failures += (f"risk_documents:{failure}",)
            if not _cancelled(dependencies):
                delay = _document_retry_seconds(configuration, exc)
                now = observed_at + timedelta(seconds=max(0, dependencies.monotonic() - started))
                index.document_failure(item, policy, failure, now, now + timedelta(seconds=delay))
            if _running(dependencies, deadline) and not circuit.failed(exc):
                _report(
                    dependencies,
                    "risk_documents",
                    state,
                    total - completed,
                    completed=completed,
                    attempted_documents=attempted,
                    deferred_documents=deferred,
                )
                continue  # One unreadable or slow PDF must not starve the other documents.
            if _running(dependencies, deadline):
                state.failures += ("risk_documents:source_stopped",)
            break
        _report(
            dependencies,
            "risk_documents",
            state,
            total - completed,
            completed=completed,
            attempted_documents=attempted,
            deferred_documents=deferred,
        )
    _record_deadline(dependencies, state, "risk_documents", total - completed, deadline)
    _report(
        dependencies,
        "risk_documents",
        state,
        total - completed,
        completed=completed,
        status="complete" if completed == total else "deferred",
        attempted_documents=attempted,
        deferred_documents=deferred,
    )


def _unique_documents(items: Sequence[Disclosure]) -> tuple[Disclosure, ...]:
    return tuple({(item.source, item.identity): item for item in items}.values())


def _document_retry_seconds(configuration: CompanySyncConfiguration, exc: BaseException) -> float:
    if isinstance(exc, ValueError) or (isinstance(exc, CompanySourceFailure) and exc.status_code == 404):
        return configuration.document_failure_retry_seconds
    return configuration.document_transient_retry_seconds


def _news(
    dependencies: CompanyEvidenceDependencies,
    population: tuple[str, ...],
    observed_at: datetime,
    deadline: float,
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
    if not candidates:
        _report(dependencies, "news", state, 0, completed=0, status="complete")
        return ()
    _report(dependencies, "news", state, total, completed=0)
    for code in () if structured_blocked else candidates:
        if not _running(dependencies, deadline):
            break
        if not index.component_due("news", code, observed_at):
            state.reused += 1
            completed += 1
            continue
        attempted.append(code)
        _report(dependencies, "news", state, total - completed, completed=completed)
        try:
            rows = dependencies.structured.news(code, observed_at=observed_at, deadline=deadline)
            state.changes += index.commit_news(code, rows, observed_at)
            completed += 1
        except (OSError, RuntimeError, ValueError) as exc:
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
    deadline: float,
    state: _SyncState,
) -> None:
    index = dependencies.index
    if not index.component_due(component, "market", now):
        state.reused += 1
        _report(dependencies, component, state, 0, completed=0, status="reused")
        return
    if not _running(dependencies, deadline):
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
            start = end = dependencies.structured.pledge_day(now, deadline=deadline)
        else:
            start, end = now.date(), now.date() + timedelta(days=90)
        requested = StructuredCompanyQuery(component, start, end)
        query = index.structured_cursor(requested)
        while _running(dependencies, deadline):
            completed = query.page - 1
            _report(dependencies, component, state, pending, completed=completed, scope=query)
            if _cancelled(dependencies):
                break
            page = dependencies.structured.fetch(query, observed_at=now, deadline=deadline)
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
        if _cancelled(dependencies):
            _report(dependencies, component, state, pending, completed=completed, status="deferred", scope=query)
            return
        raise TimeoutError("company_source_deadline")
    except (OSError, RuntimeError, ValueError) as exc:
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
    return (
        (isinstance(exc, TimeoutError) and str(exc) != "disclosure_document_parse_timeout")
        or _failure(exc) == "HttpConnectionError"
        or (isinstance(exc, CompanySourceFailure) and exc.status_code in {408, 500, 502, 503, 504})
    )


def _running(dependencies: CompanyEvidenceDependencies, deadline: float) -> bool:
    return dependencies.monotonic() < deadline and not _cancelled(dependencies)


def _deadline_reached(dependencies: CompanyEvidenceDependencies, deadline: float) -> bool:
    return dependencies.monotonic() >= deadline and not _cancelled(dependencies)


def _record_deadline(
    dependencies: CompanyEvidenceDependencies,
    state: _SyncState,
    component: Literal["announcements", "risk_documents"],
    pending: int,
    deadline: float,
) -> None:
    if pending and _deadline_reached(dependencies, deadline):
        state.failures += (f"{component}:deadline",)


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
            )
        )


__all__ = [
    "CompanyEvidenceDependencies",
    "CompanyEvidenceIndex",
    "CompanyDisclosureSource",
    "CompanyStructuredSource",
    "SyncCompanyEvidence",
]
