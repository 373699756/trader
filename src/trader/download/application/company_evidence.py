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
    DisclosurePage,
    DisclosurePartition,
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
    def split_partition(
        self, partition: DisclosurePartition, observed_at: datetime
    ) -> tuple[DisclosurePartition, ...]: ...
    def commit_page(self, partition: DisclosurePartition, page: DisclosurePage, observed_at: datetime) -> int: ...
    def partition_failure(self, partition: DisclosurePartition, failure: str) -> None: ...
    def pending_documents(self, codes: Sequence[str], observed_at: datetime) -> tuple[Disclosure, ...]: ...
    def commit_document(self, item: Disclosure, text: str) -> None: ...
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
            observed_at,
            started + budget_seconds * configuration.announcements_deadline_fraction,
            state,
        )
    if not _cancelled(dependencies):
        _documents(
            dependencies,
            population,
            observed_at,
            started + budget_seconds * configuration.documents_deadline_fraction,
            state,
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


def _announcements(
    dependencies: CompanyEvidenceDependencies,
    population: tuple[str, ...],
    observed_at: datetime,
    deadline: float,
    state: _SyncState,
) -> int:
    index = dependencies.index
    markets = tuple(sorted({EvidenceMarket(code) for code in population}))
    _report(dependencies, "announcements", state, None)
    if _cancelled(dependencies):
        return 0
    partitions = deque(index.partitions(markets, observed_at))
    while partitions and _running(dependencies, deadline):
        partition = partitions.popleft()
        _report(dependencies, "announcements", state, len(partitions) + 1, scope=partition)
        if _cancelled(dependencies):
            partitions.appendleft(partition)
            break
        try:
            page = dependencies.disclosures.fetch(partition, deadline=deadline)
            state.pages += 1
            # CNInfo has bounded result sets. Split dates before trusting an oversized range.
            if page.total > 3000 and partition.start != partition.end:
                partitions.extendleft(reversed(index.split_partition(partition, observed_at)))
                continue
            state.changes += index.commit_page(partition, page, observed_at)
            if page.has_more:
                partitions.appendleft(
                    replace(
                        partition,
                        page=partition.page + 1,
                        expected_rows=page.total,
                        received_rows=partition.received_rows + len(page.rows),
                    )
                )
            else:
                state.completed += 1
            _report(dependencies, "announcements", state, len(partitions), scope=partition)
        except (OSError, RuntimeError, ValueError) as exc:
            failure = _failure(exc)
            index.partition_failure(partition, failure)
            state.failures += (f"announcements:{failure}",)
            partitions.appendleft(partition)
            break  # Do not hammer another partition after a source/transport rejection.
    _report(dependencies, "announcements", state, len(partitions), status="deferred" if partitions else "complete")
    return len(partitions)


def _documents(
    dependencies: CompanyEvidenceDependencies,
    population: tuple[str, ...],
    observed_at: datetime,
    deadline: float,
    state: _SyncState,
) -> None:
    index = dependencies.index
    pending_documents = _unique_documents(index.pending_documents(population, observed_at))
    total = len(pending_documents)
    if any(item in {"announcements:access_denied", "announcements:rate_limited"} for item in state.failures):
        _report(dependencies, "risk_documents", state, total, completed=0, status="deferred")
        return
    completed = 0
    _report(dependencies, "risk_documents", state, total, completed=0)
    for item in pending_documents:
        if not _running(dependencies, deadline):
            break
        try:
            text = dependencies.disclosures.document(item, deadline=deadline)
            index.commit_document(item, text)
            completed += 1
            state.changes += 1
        except (OSError, RuntimeError, ValueError) as exc:
            state.failures += (f"risk_documents:{_failure(exc)}",)
            if isinstance(exc, ValueError) or (
                isinstance(exc, TimeoutError)
                and str(exc) == "disclosure_document_parse_timeout"
                and _running(dependencies, deadline)
            ):
                _report(dependencies, "risk_documents", state, total - completed, completed=completed)
                continue  # One unreadable or slow PDF must not starve the other documents.
            break
        _report(dependencies, "risk_documents", state, total - completed, completed=completed)
    _report(
        dependencies,
        "risk_documents",
        state,
        total - completed,
        completed=completed,
        status="complete" if completed == total else "deferred",
    )


def _unique_documents(items: Sequence[Disclosure]) -> tuple[Disclosure, ...]:
    return tuple({(item.source, item.identity): item for item in items}.values())


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


def _running(dependencies: CompanyEvidenceDependencies, deadline: float) -> bool:
    return dependencies.monotonic() < deadline and not _cancelled(dependencies)


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
            )
        )


__all__ = [
    "CompanyEvidenceDependencies",
    "CompanyEvidenceIndex",
    "CompanyDisclosureSource",
    "CompanyStructuredSource",
    "SyncCompanyEvidence",
]
