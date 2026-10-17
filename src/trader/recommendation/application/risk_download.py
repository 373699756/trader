"""Bounded offline preparation of one trade-date risk snapshot."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Protocol, TypedDict

if TYPE_CHECKING:
    from typing_extensions import Unpack

from trader.recommendation.domain.market.research import (
    ResearchComponentsFromObservation,
    ResearchObservation,
)


class RiskResearchLoadReport(Protocol):
    @property
    def observations(self) -> Mapping[str, ResearchObservation]: ...

    @property
    def changed_codes(self) -> tuple[str, ...]: ...

    @property
    def deferred_codes(self) -> tuple[str, ...]: ...

    @property
    def deadline_reached(self) -> bool: ...


class RiskResearchLoader(Protocol):
    def recover_from_data_plane(
        self,
        observed_at: datetime | None = None,
        *,
        retain_stale: bool = False,
    ) -> None: ...

    def cached(
        self,
        codes: Sequence[str],
        *,
        include_structured: bool,
        fresh_only: bool = False,
        action_restrictions: dict[str, set[str]] | None = None,
    ) -> Mapping[str, ResearchObservation]: ...

    def load_report(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        **options: Unpack[_RiskLoadOptions],
    ) -> RiskResearchLoadReport: ...


class RiskAnnouncementSyncReport(Protocol):
    @property
    def changed_codes(self) -> tuple[str, ...]: ...

    @property
    def completed_codes(self) -> tuple[str, ...]: ...

    @property
    def deferred_codes(self) -> tuple[str, ...]: ...

    @property
    def failed_codes(self) -> tuple[str, ...]: ...

    @property
    def pages_fetched(self) -> int: ...

    @property
    def saved_announcements(self) -> int: ...

    @property
    def deadline_reached(self) -> bool: ...


class RiskAnnouncementSynchronizer(Protocol):
    def pending_codes(self, codes: Sequence[str], observed_at: datetime) -> tuple[str, ...]: ...

    def sync_report(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        deadline: datetime,
    ) -> RiskAnnouncementSyncReport: ...


class _RiskLoadRequiredOptions(TypedDict):
    include_structured: bool


class _RiskLoadOptionalOptions(TypedDict, total=False):
    force: bool
    deadline: datetime | None
    action_restrictions: dict[str, set[str]] | None


class _RiskLoadOptions(_RiskLoadRequiredOptions, _RiskLoadOptionalOptions):
    pass


@dataclass(frozen=True)
class RiskDownloadResult:
    target_date: date
    requested_codes: int
    available_codes: int
    covered_codes: int
    partial_codes: int
    changed_codes: int
    deferred_codes: int
    failed_codes: int
    deadline_reached: bool
    elapsed_seconds: float
    inventory: RiskDownloadInventory
    wave_count: int = 0
    stop_reason: str = "complete"
    official_pages_fetched: int = 0
    official_announcements_saved: int = 0
    official_failed_codes: int = 0

    @property
    def needs_retry(self) -> bool:
        return self.stop_reason != "complete" or self.covered_codes < self.requested_codes or self.deadline_reached


@dataclass(frozen=True)
class RiskDownloadContext:
    target_date: date
    observed_at: datetime
    monotonic: Callable[[], float]
    wall_clock: Callable[[], datetime]


@dataclass(frozen=True)
class RiskDownloadInventory:
    financial_codes: int
    announcement_codes: int
    pledge_codes: int
    unlock_codes: int
    regulatory_history_codes: int
    regulatory_evidence_codes: int
    news_queried_codes: int
    news_items: int
    announcement_items: int
    financial_reports: int


@dataclass(frozen=True)
class RiskDownloadBudget:
    source_seconds: float = 120.0
    task_seconds: float | None = None


_DEFAULT_RISK_DOWNLOAD_BUDGET = RiskDownloadBudget()


@dataclass(frozen=True)
class _RiskObservationProgress:
    code: str
    news_queried: bool
    evidence_ids: tuple[str, ...]
    announcement_ids: tuple[str, ...]
    financial_reports: tuple[tuple[date, datetime], ...]
    financial_available: bool
    announcements_available: bool
    pledge_available: bool
    unlock_available: bool
    regulatory_facts: tuple[tuple[str, str], ...]
    regulatory_history_complete: bool
    source_errors: tuple[str, ...]


@dataclass(frozen=True)
class _RiskProgressIdentity:
    official_pending: tuple[str, ...]
    observations: tuple[_RiskObservationProgress, ...]


@dataclass
class _RiskDownloadState:
    normalized: tuple[str, ...]
    started: float
    observations: dict[str, ResearchObservation]
    changed_codes: set[str]
    official_terminal_codes: set[str]
    official_pages_fetched: int = 0
    official_announcements_saved: int = 0
    wave_count: int = 0
    any_deadline: bool = False
    stop_reason: str = "complete"


@dataclass(frozen=True)
class _RiskPendingCodes:
    missing: tuple[str, ...]
    retryable: tuple[str, ...]
    official: tuple[str, ...]


def DownloadRiskSnapshot(
    loader: RiskResearchLoader,
    codes: Sequence[str],
    context: RiskDownloadContext,
    budget: RiskDownloadBudget = _DEFAULT_RISK_DOWNLOAD_BUDGET,
    announcement_sync: RiskAnnouncementSynchronizer | None = None,
) -> RiskDownloadResult:
    if budget.source_seconds <= 0.0 or (budget.task_seconds is not None and budget.task_seconds <= 0.0):
        raise ValueError("risk download budgets must be positive")
    normalized = tuple(sorted(set(codes)))
    started = context.monotonic()
    loader.recover_from_data_plane(context.observed_at, retain_stale=True)
    state = _RiskDownloadState(
        normalized=normalized,
        started=started,
        observations=dict(loader.cached(normalized, include_structured=True, fresh_only=False)),
        changed_codes=set(),
        official_terminal_codes=set(),
    )
    _run_download_waves(loader, context, budget, announcement_sync, state)
    return _build_download_result(context, state, announcement_sync)


def _run_download_waves(
    loader: RiskResearchLoader,
    context: RiskDownloadContext,
    budget: RiskDownloadBudget,
    announcement_sync: RiskAnnouncementSynchronizer | None,
    state: _RiskDownloadState,
) -> None:
    while True:
        pending = _pending_codes(state, context, announcement_sync)
        if not pending.missing and not pending.retryable and not pending.official:
            state.stop_reason = (
                "complete"
                if not state.official_terminal_codes and _all_components_covered(state.normalized, state.observations)
                else "source_exhausted"
            )
            break
        remaining = _remaining_task_seconds(state.started, context.monotonic(), budget.task_seconds)
        if remaining is not None and remaining <= 0.0:
            state.stop_reason = "task_deadline"
            break

        state.wave_count += 1
        before = _progress_identity(state.normalized, state.observations, pending.official)
        if not _run_research_wave(loader, context, budget, state, pending):
            state.stop_reason = "task_deadline"
            break
        if not _run_official_wave(loader, context, budget, announcement_sync, state):
            state.stop_reason = "task_deadline"
            break
        after_pending = _pending_codes(state, context, announcement_sync)
        after = _progress_identity(state.normalized, state.observations, after_pending.official)
        if after == before:
            state.stop_reason = "no_progress"
            break


def _pending_codes(
    state: _RiskDownloadState,
    context: RiskDownloadContext,
    announcement_sync: RiskAnnouncementSynchronizer | None,
) -> _RiskPendingCodes:
    missing = tuple(code for code in state.normalized if code not in state.observations)
    retryable = tuple(
        code
        for code in state.normalized
        if code in state.observations and _has_retryable_source_error(state.observations[code])
    )
    official = (
        tuple(
            code
            for code in announcement_sync.pending_codes(state.normalized, context.observed_at)
            if code not in state.official_terminal_codes
        )
        if announcement_sync is not None
        else ()
    )
    return _RiskPendingCodes(missing, retryable, official)


def _run_research_wave(
    loader: RiskResearchLoader,
    context: RiskDownloadContext,
    budget: RiskDownloadBudget,
    state: _RiskDownloadState,
    pending: _RiskPendingCodes,
) -> bool:
    groups = tuple((group, force) for group, force in ((pending.missing, False), (pending.retryable, True)) if group)
    research_seconds = budget.source_seconds / max(1, len(groups))
    for group, force in groups:
        source_seconds = _source_budget_seconds(
            research_seconds,
            started=state.started,
            now=context.monotonic(),
            task_seconds=budget.task_seconds,
        )
        if source_seconds <= 0.0:
            return False
        report = loader.load_report(
            group,
            context.observed_at,
            include_structured=True,
            force=force,
            deadline=context.wall_clock() + timedelta(seconds=source_seconds),
        )
        state.observations.update(report.observations)
        state.changed_codes.update(report.changed_codes)
        state.any_deadline = state.any_deadline or report.deadline_reached
    return True


def _run_official_wave(
    loader: RiskResearchLoader,
    context: RiskDownloadContext,
    budget: RiskDownloadBudget,
    announcement_sync: RiskAnnouncementSynchronizer | None,
    state: _RiskDownloadState,
) -> bool:
    official_pending = _pending_codes(state, context, announcement_sync).official
    if announcement_sync is None or not official_pending:
        return True
    source_seconds = _source_budget_seconds(
        budget.source_seconds,
        started=state.started,
        now=context.monotonic(),
        task_seconds=budget.task_seconds,
    )
    if source_seconds <= 0.0:
        return False
    report = announcement_sync.sync_report(
        official_pending,
        context.observed_at,
        deadline=context.wall_clock() + timedelta(seconds=source_seconds),
    )
    state.changed_codes.update(report.changed_codes)
    state.official_terminal_codes.update(report.failed_codes)
    state.official_pages_fetched += report.pages_fetched
    state.official_announcements_saved += report.saved_announcements
    state.any_deadline = state.any_deadline or report.deadline_reached
    loader.recover_from_data_plane(context.observed_at, retain_stale=True)
    state.observations.update(loader.cached(state.normalized, include_structured=True, fresh_only=False))
    return True


def _build_download_result(
    context: RiskDownloadContext,
    state: _RiskDownloadState,
    announcement_sync: RiskAnnouncementSynchronizer | None,
) -> RiskDownloadResult:
    pending = _pending_codes(state, context, announcement_sync)
    deferred = set((*pending.missing, *pending.retryable, *pending.official))
    coverage = {code: _research_component_coverage(observation) for code, observation in state.observations.items()}
    available = {code for code, status in coverage.items() if any(status)}
    covered = {code for code, status in coverage.items() if all(status)}
    retained = tuple(state.observations[code] for code in state.normalized if code in state.observations)
    return RiskDownloadResult(
        target_date=context.target_date,
        requested_codes=len(state.normalized),
        available_codes=len(available),
        covered_codes=len(covered),
        partial_codes=len(available - covered),
        changed_codes=len(state.changed_codes),
        deferred_codes=len(deferred),
        failed_codes=max(0, len(state.normalized) - len(available) - len(deferred)),
        deadline_reached=state.any_deadline and bool(deferred),
        elapsed_seconds=max(0.0, context.monotonic() - state.started),
        inventory=RiskDownloadInventory(
            financial_codes=sum(item.financial is not None for item in retained),
            announcement_codes=sum(item.announcements_available for item in retained),
            pledge_codes=sum(item.pledge_ratio_pct is not None for item in retained),
            unlock_codes=sum(item.unlock_ratio_pct is not None for item in retained),
            regulatory_history_codes=sum(item.corporate_risk_history_complete for item in retained),
            regulatory_evidence_codes=sum(bool(item.corporate_risk_facts) for item in retained),
            news_queried_codes=sum(item.news_queried for item in retained),
            news_items=sum(sum(evidence.evidence_type == "news" for evidence in item.evidence) for item in retained),
            announcement_items=sum(len(item.announcements) for item in retained),
            financial_reports=sum(len(item.financial_history) for item in retained),
        ),
        wave_count=state.wave_count,
        stop_reason=state.stop_reason,
        official_pages_fetched=state.official_pages_fetched,
        official_announcements_saved=state.official_announcements_saved,
        official_failed_codes=len(state.official_terminal_codes),
    )


def _remaining_task_seconds(started: float, now: float, task_seconds: float | None) -> float | None:
    if task_seconds is None:
        return None
    return max(0.0, task_seconds - max(0.0, now - started))


def _source_budget_seconds(
    source_seconds: float,
    *,
    started: float,
    now: float,
    task_seconds: float | None,
) -> float:
    remaining = _remaining_task_seconds(started, now, task_seconds)
    return source_seconds if remaining is None else min(source_seconds, remaining)


def _progress_identity(
    codes: Sequence[str],
    observations: Mapping[str, ResearchObservation],
    official_pending: Sequence[str],
) -> _RiskProgressIdentity:
    return _RiskProgressIdentity(
        official_pending=tuple(official_pending),
        observations=tuple(
            _RiskObservationProgress(
                code=code,
                news_queried=observation.news_queried,
                evidence_ids=tuple(item.evidence_id for item in observation.evidence),
                announcement_ids=tuple(item.announcement_id for item in observation.announcements),
                financial_reports=tuple(
                    (item.report_date, item.published_at) for item in observation.financial_history
                ),
                financial_available=observation.financial is not None,
                announcements_available=observation.announcements_available,
                pledge_available=observation.pledge_ratio_pct is not None,
                unlock_available=observation.unlock_ratio_pct is not None,
                regulatory_facts=tuple(
                    (item.category.value, item.evidence_id) for item in observation.corporate_risk_facts
                ),
                regulatory_history_complete=observation.corporate_risk_history_complete,
                source_errors=observation.source_errors,
            )
            for code in codes
            if (observation := observations.get(code)) is not None
        ),
    )


def _all_components_covered(codes: Sequence[str], observations: Mapping[str, ResearchObservation]) -> bool:
    return all(
        code in observations
        and observations[code].news_queried
        and all(_research_component_coverage(observations[code]))
        for code in codes
    )


_RETRYABLE_SOURCE_FAILURES = frozenset(
    {
        "timeout",
        "deadline",
        "deferred",
        "circuit_open",
        "negative_cache",
        "superseded",
        "no_data",
        "resource_rejected",
        "rate_limited",
        "source_failed",
    }
)


def _has_retryable_source_error(observation: ResearchObservation) -> bool:
    return any(error.partition(":")[2] in _RETRYABLE_SOURCE_FAILURES for error in observation.source_errors)


def _research_component_coverage(observation: ResearchObservation) -> tuple[bool, bool, bool, bool]:
    components = ResearchComponentsFromObservation(observation)
    covered = {"known_clear", "known_risk"}
    return (
        components.financial in covered,
        components.announcements in covered,
        components.pledge in covered,
        components.unlock in covered,
    )


__all__ = [
    "DownloadRiskSnapshot",
    "RiskDownloadBudget",
    "RiskDownloadContext",
    "RiskDownloadInventory",
    "RiskDownloadResult",
    "RiskAnnouncementSynchronizer",
    "RiskResearchLoadReport",
]
