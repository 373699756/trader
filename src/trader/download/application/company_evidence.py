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
    Disclosure,
    DisclosurePage,
    DisclosurePartition,
    EvidenceMarket,
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
    def candidate_codes(self, observed_at: datetime) -> tuple[str, ...]: ...
    def record_candidates(self, codes: Sequence[str], observed_at: datetime) -> None: ...
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
    budget_seconds: float = 3480,
    news_codes: Sequence[str] = (),
) -> CompanyEvidenceSyncResult:
    if observed_at.utcoffset() is None or not math.isfinite(budget_seconds) or budget_seconds <= 0:
        raise ValueError("company evidence requires an aware time and positive budget")
    started = dependencies.monotonic()
    deadline = started + budget_seconds
    index = dependencies.index
    state = _SyncState()
    population = _validated_population(codes)
    index.initialize()
    if news_codes:
        index.record_candidates(tuple(code for code in news_codes if code in population), observed_at)
    components: tuple[Literal["financial", "pledge", "unlock"], ...] = ("financial", "pledge", "unlock")
    for component in components:
        _structured(dependencies, component, observed_at, min(deadline, dependencies.monotonic() + 600), state)
        if any(item.endswith((":access_denied", ":rate_limited")) for item in state.failures):
            break
    pending = _announcements(dependencies, population, observed_at, started + budget_seconds * 0.7, state)
    _documents(dependencies, population, observed_at, started + budget_seconds * 0.95, state)
    published = index.publish(population, observed_at)
    news_updates = _news(dependencies, population, observed_at, deadline, state)
    if news_updates:
        index.publish(news_updates, observed_at)
    if dependencies.monotonic() >= deadline:
        state.failures += ("task:deadline",)
    if dependencies.cancel_requested is not None and dependencies.cancel_requested():
        state.failures += ("task:cancelled",)
    if index.pending_documents(population, observed_at):
        state.failures += ("risk_documents:pending",)
    _report(dependencies, "complete", state, pending, completed=published)
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
    partitions = deque(index.partitions(markets, observed_at))
    while partitions and _running(dependencies, deadline):
        partition = partitions.popleft()
        _report(dependencies, "announcements", state, len(partitions) + 1)
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
        except (OSError, RuntimeError, ValueError) as exc:
            failure = _failure(exc)
            index.partition_failure(partition, failure)
            state.failures += (f"announcements:{failure}",)
            partitions.appendleft(partition)
            break  # Do not hammer another partition after a source/transport rejection.
    return len(partitions)


def _documents(
    dependencies: CompanyEvidenceDependencies,
    population: tuple[str, ...],
    observed_at: datetime,
    deadline: float,
    state: _SyncState,
) -> None:
    if any(item in {"announcements:access_denied", "announcements:rate_limited"} for item in state.failures):
        return
    index = dependencies.index
    pending_documents = index.pending_documents(population, observed_at)
    seen: set[tuple[str, str]] = set()
    completed = 0
    for item in pending_documents:
        if not _running(dependencies, deadline):
            break
        if (item.source, item.identity) in seen:
            continue
        seen.add((item.source, item.identity))
        _report(dependencies, "risk_documents", state, len(pending_documents) - len(seen) + 1, completed=completed)
        try:
            text = dependencies.disclosures.document(item, deadline=deadline)
            index.commit_document(item, text)
            completed += 1
            state.changes += 1
        except (OSError, RuntimeError, ValueError) as exc:
            state.failures += (f"risk_documents:{_failure(exc)}",)
            if isinstance(exc, ValueError):
                continue  # An unreadable/oversized PDF must not starve all other documents.
            break


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
    for code in () if structured_blocked else index.candidate_codes(observed_at):
        if not _running(dependencies, deadline):
            break
        if code not in population:
            continue
        if not index.component_due("news", code, observed_at):
            state.reused += 1
            continue
        attempted.append(code)
        _report(dependencies, "news", state, 1, completed=completed)
        try:
            rows = dependencies.structured.news(code, observed_at=observed_at, deadline=deadline)
            state.changes += index.commit_news(code, rows, observed_at)
            completed += 1
        except (OSError, RuntimeError, ValueError) as exc:
            failure = _failure(exc)
            index.component_failure("news", code, failure)
            state.failures += (f"news:{failure}",)
            break
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
        return
    if not _running(dependencies, deadline):
        return
    _report(dependencies, component, state, 1, completed=0)
    completed = 0
    try:
        if component == "financial":
            start, end = index.component_since(component, "market", now), now.date()
        elif component == "pledge":
            start = end = dependencies.structured.pledge_day(now, deadline=deadline)
        else:
            start, end = now.date(), now.date() + timedelta(days=90)
        requested = StructuredCompanyQuery(component, start, end)
        query = index.structured_cursor(requested)
        while _running(dependencies, deadline):
            page = dependencies.structured.fetch(query, observed_at=now, deadline=deadline)
            state.pages += 1
            state.changes += index.commit_structured(query, page, now)
            completed += 1
            _report(dependencies, component, state, page.pages - page.page, completed=completed)
            if page.page == page.pages:
                remaining = _remaining_query(index, query, requested, now)
                if remaining is None:
                    return
                query = remaining
                continue
            query = replace(
                query, received_rows=query.received_rows + page.row_count, expected_rows=page.total, page=page.page + 1
            )
        raise TimeoutError("company_source_deadline")
    except (OSError, RuntimeError, ValueError) as exc:
        failure = _failure(exc)
        index.component_failure(component, "market", failure)
        state.failures += (f"{component}:{failure}",)


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
        return exc.category
    if isinstance(exc, (ValueError, TimeoutError, RuntimeError)) and re.fullmatch(
        r"(?:cninfo|company|disclosure|structured|financial|pledge|news)_[a-z_]{1,60}", str(exc)
    ):
        return str(exc)
    return type(exc).__name__


def _running(dependencies: CompanyEvidenceDependencies, deadline: float) -> bool:
    return dependencies.monotonic() < deadline and (
        dependencies.cancel_requested is None or not dependencies.cancel_requested()
    )


def _report(
    dependencies: CompanyEvidenceDependencies,
    component: str,
    state: _SyncState,
    pending: int,
    *,
    completed: int | None = None,
) -> None:
    if dependencies.progress is not None:
        dependencies.progress(
            EvidenceSyncProgress(
                component,
                state.completed if completed is None else completed,
                pending,
                state.pages,
                state.changes,
                state.reused,
                state.failures[-1] if state.failures else "",
            )
        )


__all__ = [
    "CompanyEvidenceDependencies",
    "CompanyEvidenceIndex",
    "CompanyDisclosureSource",
    "CompanyStructuredSource",
    "SyncCompanyEvidence",
]
