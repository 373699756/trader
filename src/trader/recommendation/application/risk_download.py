"""Bounded offline preparation of one trade-date risk snapshot."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Literal, Protocol, TypeAlias, TypedDict

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
        codes: Sequence[str] | None = None,
        research_only: bool = False,
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
    def attempted_codes(self) -> tuple[str, ...]: ...

    @property
    def deferred_codes(self) -> tuple[str, ...]: ...

    @property
    def failed_codes(self) -> tuple[str, ...]: ...

    @property
    def pages_fetched(self) -> int: ...

    @property
    def pages_checkpointed(self) -> int: ...

    @property
    def saved_announcements(self) -> int: ...

    @property
    def resumed_codes(self) -> tuple[str, ...]: ...

    @property
    def deadline_reached(self) -> bool: ...

    @property
    def request_seconds(self) -> float: ...

    @property
    def parse_seconds(self) -> float: ...

    @property
    def evidence_write_seconds(self) -> float: ...

    @property
    def cursor_write_seconds(self) -> float: ...

    @property
    def persistence_error(self) -> Literal["busy", "unavailable"] | None: ...

    @property
    def source_error(self) -> Literal["access_denied", "rate_limited"] | None: ...


RiskAnnouncementProgress: TypeAlias = tuple[int, int, int, int, int, int]


class RiskAnnouncementSynchronizer(Protocol):
    def pending_codes(self, codes: Sequence[str], observed_at: datetime) -> tuple[str, ...]: ...

    def sync_report(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        deadline: datetime,
        progress: Callable[[RiskAnnouncementProgress], None] | None = None,
    ) -> RiskAnnouncementSyncReport: ...


class _RiskLoadRequiredOptions(TypedDict):
    include_structured: bool


class _RiskLoadOptionalOptions(TypedDict, total=False):
    force: bool
    deadline: datetime | None
    action_restrictions: dict[str, set[str]] | None
    progress: Callable[[int, int], None]


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
    official_pages_checkpointed: int = 0
    official_announcements_saved: int = 0
    official_resumed_codes: int = 0
    official_failed_codes: int = 0
    official_persistence_error: Literal["busy", "unavailable"] | None = None
    official_source_error: Literal["access_denied", "rate_limited"] | None = None
    research_coverage_required: bool = True

    @property
    def needs_retry(self) -> bool:
        return (
            self.stop_reason != "complete"
            or (self.research_coverage_required and self.covered_codes < self.requested_codes)
            or self.deadline_reached
        )


@dataclass(frozen=True)
class RiskDownloadContext:
    target_date: date
    observed_at: datetime
    monotonic: Callable[[], float]
    wall_clock: Callable[[], datetime]
    progress: RiskDownloadProgressPort | None = None


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


RiskDownloadProgressStage = Literal[
    "local_snapshot",
    "research_initial",
    "research_retry",
    "official_announcements",
    "wave_summary",
    "finished",
]
RiskDownloadProgressState = Literal["started", "running", "completed", "deferred", "waiting", "stopped"]


@dataclass(frozen=True)
class RiskDownloadProgress:
    stage: RiskDownloadProgressStage
    state: RiskDownloadProgressState
    wave: int
    completed_units: int
    total_units: int
    requested_codes: int
    research_pending_codes: int | None = None
    official_pending_codes: int | None = None
    available_codes: int | None = None
    covered_codes: int | None = None
    deferred_codes: int | None = None
    changed_codes: int | None = None
    official_pages_fetched: int = 0
    official_pages_checkpointed: int = 0
    official_announcements_saved: int = 0
    official_resumed_codes: int = 0
    official_sync_seconds: float = 0.0
    official_recovery_seconds: float = 0.0
    official_request_seconds: float = 0.0
    official_parse_seconds: float = 0.0
    official_evidence_write_seconds: float = 0.0
    official_cursor_write_seconds: float = 0.0
    official_persistence_error: Literal["busy", "unavailable"] | None = None
    official_source_error: Literal["access_denied", "rate_limited"] | None = None
    stop_reason: str | None = None


class RiskDownloadProgressPort(Protocol):
    def publish(self, progress: RiskDownloadProgress) -> None: ...


@dataclass(frozen=True)
class _RiskObservationProgress:
    code: str
    news_queried: bool
    evidence_ids: tuple[str, ...]
    announcement_ids: tuple[str, ...]
    financial_reports: tuple[tuple[date, datetime], ...]
    financial_history_complete: bool
    financial_available: bool
    announcements_available: bool
    pledge_available: bool
    unlock_available: bool
    regulatory_facts: tuple[tuple[str, str], ...]
    regulatory_history_complete: bool


@dataclass(frozen=True)
class _RiskProgressIdentity:
    research_pending: tuple[str, ...]
    official_pending: tuple[str, ...]
    research_advances: int
    official_checkpoint_advances: int


@dataclass
class _RiskDownloadState:
    normalized: tuple[str, ...]
    started: float
    observations: dict[str, ResearchObservation]
    changed_codes: set[str]
    official_terminal_codes: set[str]
    progress: RiskDownloadProgressPort | None = None
    official_enabled: bool = False
    require_current_window: bool = False
    research_enabled: bool = True
    research_retry_order: tuple[str, ...] = ()
    official_order: tuple[str, ...] = ()
    research_pending_codes: int | None = None
    official_pending_codes: int | None = None
    official_pages_fetched: int = 0
    official_pages_checkpointed: int = 0
    official_announcements_saved: int = 0
    official_resumed_codes: set[str] = field(default_factory=set)
    official_checkpoint_advances: int = 0
    research_advances: int = 0
    official_sync_seconds: float = 0.0
    official_recovery_seconds: float = 0.0
    official_request_seconds: float = 0.0
    official_parse_seconds: float = 0.0
    official_evidence_write_seconds: float = 0.0
    official_cursor_write_seconds: float = 0.0
    official_persistence_error: Literal["busy", "unavailable"] | None = None
    official_source_error: Literal["access_denied", "rate_limited"] | None = None
    official_wave_initial_pending: int = 0
    wave_count: int = 0
    any_deadline: bool = False
    stop_reason: str = "complete"


@dataclass(frozen=True)
class _RiskPendingCodes:
    missing: tuple[str, ...]
    retryable: tuple[str, ...]
    official: tuple[str, ...]


@dataclass(frozen=True)
class _RiskStageUpdate:
    stage: RiskDownloadProgressStage
    state: RiskDownloadProgressState
    completed_units: int
    total_units: int
    official_pages_fetched: int | None = None
    official_pages_checkpointed: int | None = None
    official_announcements_saved: int | None = None
    official_resumed_codes: int | None = None


def DownloadRiskSnapshot(
    loader: RiskResearchLoader,
    codes: Sequence[str],
    context: RiskDownloadContext,
    budget: RiskDownloadBudget = _DEFAULT_RISK_DOWNLOAD_BUDGET,
    announcement_sync: RiskAnnouncementSynchronizer | None = None,
    require_current_window: bool = False,
    research_enabled: bool = True,
) -> RiskDownloadResult:
    if budget.source_seconds <= 0.0 or (budget.task_seconds is not None and budget.task_seconds <= 0.0):
        raise ValueError("risk download budgets must be positive")
    normalized = tuple(sorted(set(codes)))
    started = context.monotonic()
    if context.progress is not None:
        context.progress.publish(
            RiskDownloadProgress(
                stage="local_snapshot",
                state="started",
                wave=0,
                completed_units=0,
                total_units=len(normalized),
                requested_codes=len(normalized),
            )
        )
    loader.recover_from_data_plane(context.observed_at, retain_stale=True, research_only=announcement_sync is None)
    state = _RiskDownloadState(
        normalized=normalized,
        started=started,
        observations=dict(loader.cached(normalized, include_structured=True, fresh_only=False)),
        changed_codes=set(),
        official_terminal_codes=set(),
        progress=context.progress,
        official_enabled=announcement_sync is not None,
        require_current_window=require_current_window,
        research_enabled=research_enabled,
    )
    initial_pending = _pending_codes(state, context, announcement_sync)
    _remember_pending_counts(state, initial_pending)
    _publish_summary_progress(
        state,
        initial_pending,
        stage="local_snapshot",
        state_name="completed",
        completed_units=len(state.observations),
    )
    _run_download_waves(loader, context, budget, announcement_sync, state)
    result = _build_download_result(context, state, announcement_sync)
    _publish_result_progress(context.progress, result, state)
    return result


def _run_download_waves(
    loader: RiskResearchLoader,
    context: RiskDownloadContext,
    budget: RiskDownloadBudget,
    announcement_sync: RiskAnnouncementSynchronizer | None,
    state: _RiskDownloadState,
) -> None:
    while True:
        pending = _pending_codes(state, context, announcement_sync)
        _remember_pending_counts(state, pending)
        if not pending.missing and not pending.retryable and not pending.official:
            state.stop_reason = (
                "complete"
                if not state.official_terminal_codes
                and (not state.research_enabled or _all_components_covered(state.normalized, state.observations))
                else "source_exhausted"
            )
            break
        remaining = _remaining_task_seconds(state.started, context.monotonic(), budget.task_seconds)
        if remaining is not None and remaining <= 0.0:
            state.stop_reason = "task_deadline"
            break

        state.wave_count += 1
        before = _progress_identity(
            pending,
            state.research_advances,
            state.official_checkpoint_advances,
        )
        if not _run_research_wave(loader, context, budget, state, pending):
            state.stop_reason = "task_deadline"
            break
        if not _run_official_wave(loader, context, budget, announcement_sync, state):
            state.stop_reason = "task_deadline"
            break
        after_pending = _pending_codes(state, context, announcement_sync)
        _remember_pending_counts(state, after_pending)
        unresolved = set((*after_pending.missing, *after_pending.retryable, *after_pending.official))
        _publish_summary_progress(
            state,
            after_pending,
            stage="wave_summary",
            state_name="waiting" if state.official_persistence_error == "busy" else "completed",
            completed_units=len(state.normalized) - len(unresolved),
        )
        after = _progress_identity(
            after_pending,
            state.research_advances,
            state.official_checkpoint_advances,
        )
        if not _made_durable_progress(before, after):
            if state.official_source_error is not None:
                state.stop_reason = "source_blocked"
                break
            if state.official_persistence_error == "busy":
                continue
            state.stop_reason = (
                "persistence_unavailable" if state.official_persistence_error == "unavailable" else "no_progress"
            )
            break
        if state.official_source_error is not None:
            state.stop_reason = "source_blocked"
            break


def _pending_codes(
    state: _RiskDownloadState,
    context: RiskDownloadContext,
    announcement_sync: RiskAnnouncementSynchronizer | None,
) -> _RiskPendingCodes:
    missing = (
        tuple(code for code in state.normalized if code not in state.observations) if state.research_enabled else ()
    )
    retryable = _prioritize_pending(
        tuple(
            code
            for code in state.normalized
            if state.research_enabled
            and code in state.observations
            and _needs_research_refresh(state, state.observations[code], context.observed_at.date())
        ),
        state.research_retry_order,
    )
    official = (
        _prioritize_pending(
            tuple(
                code
                for code in announcement_sync.pending_codes(state.normalized, context.observed_at)
                if code not in state.official_terminal_codes
            ),
            state.official_order,
        )
        if announcement_sync is not None
        else ()
    )
    return _RiskPendingCodes(missing, retryable, official)


def _prioritize_pending(codes: tuple[str, ...], priority: Sequence[str]) -> tuple[str, ...]:
    pending = set(codes)
    prioritized = tuple(code for code in priority if code in pending)
    included = set(prioritized)
    return (*prioritized, *(code for code in codes if code not in included))


def _remember_pending_counts(state: _RiskDownloadState, pending: _RiskPendingCodes) -> None:
    state.research_pending_codes = len(set((*pending.missing, *pending.retryable))) if state.research_enabled else None
    state.official_pending_codes = len(pending.official) if state.official_enabled else None


def _needs_research_refresh(state: _RiskDownloadState, observation: ResearchObservation, observed_on: date) -> bool:
    return _has_retryable_source_error(observation) or (
        state.require_current_window
        and (
            observation.queried_on != observed_on
            or observation.pledge_checked_on != observed_on
            or not observation.news_queried
        )
    )


def _run_research_wave(
    loader: RiskResearchLoader,
    context: RiskDownloadContext,
    budget: RiskDownloadBudget,
    state: _RiskDownloadState,
    pending: _RiskPendingCodes,
) -> bool:
    group_candidates: tuple[
        tuple[tuple[str, ...], bool, RiskDownloadProgressStage],
        ...,
    ] = (
        (pending.missing, False, "research_initial"),
        (pending.retryable, True, "research_retry"),
    )
    groups = tuple((group, force, stage) for group, force, stage in group_candidates if group)
    research_seconds = budget.source_seconds / max(1, len(groups))
    for group, force, stage in groups:
        source_seconds = _source_budget_seconds(
            research_seconds,
            started=state.started,
            now=context.monotonic(),
            task_seconds=budget.task_seconds,
        )
        if source_seconds <= 0.0:
            return False
        _publish_stage_progress(state, _RiskStageUpdate(stage, "started", 0, len(group)))
        report = loader.load_report(
            group,
            context.observed_at,
            include_structured=True,
            force=force,
            deadline=context.wall_clock() + timedelta(seconds=source_seconds),
            progress=partial(_publish_research_progress, state, stage),
        )
        previous = {code: state.observations.get(code) for code in report.changed_codes}
        state.observations.update(report.observations)
        for code in report.changed_codes:
            current = state.observations.get(code)
            if current is not None and _research_observation_advanced(code, previous[code], current):
                state.changed_codes.add(code)
                state.research_advances += 1
        state.any_deadline = state.any_deadline or report.deadline_reached
        research_pending = _pending_codes(state, context, None)
        state.research_pending_codes = len(set((*research_pending.missing, *research_pending.retryable)))
        if stage == "research_retry":
            deferred = set(report.deferred_codes)
            state.research_retry_order = (
                *report.deferred_codes,
                *(code for code in group if code not in deferred),
            )
        unresolved_group = sum(
            code not in state.observations
            or _needs_research_refresh(state, state.observations[code], context.observed_at.date())
            for code in group
        )
        _publish_stage_progress(
            state,
            _RiskStageUpdate(
                stage,
                "deferred" if unresolved_group else "completed",
                len(group) - len(report.deferred_codes),
                len(group),
            ),
        )
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
        state.official_persistence_error = None
        return True
    source_seconds = _source_budget_seconds(
        budget.source_seconds,
        started=state.started,
        now=context.monotonic(),
        task_seconds=budget.task_seconds,
    )
    if source_seconds <= 0.0:
        return False
    state.official_wave_initial_pending = len(official_pending)
    _publish_stage_progress(
        state,
        _RiskStageUpdate("official_announcements", "started", 0, len(official_pending)),
    )
    sync_started = context.monotonic()
    report = announcement_sync.sync_report(
        official_pending,
        context.observed_at,
        deadline=context.wall_clock() + timedelta(seconds=source_seconds),
        progress=partial(_publish_official_progress, state),
    )
    state.official_sync_seconds = max(0.0, context.monotonic() - sync_started)
    state.official_request_seconds = report.request_seconds
    state.official_parse_seconds = report.parse_seconds
    state.official_evidence_write_seconds = report.evidence_write_seconds
    state.official_cursor_write_seconds = report.cursor_write_seconds
    state.official_persistence_error = report.persistence_error
    state.official_source_error = report.source_error
    state.changed_codes.update(report.changed_codes)
    state.official_terminal_codes.update(report.failed_codes)
    state.official_pages_fetched += report.pages_fetched
    state.official_pages_checkpointed += report.pages_checkpointed
    state.official_announcements_saved += report.saved_announcements
    state.official_resumed_codes.update(report.resumed_codes)
    state.official_checkpoint_advances += report.pages_checkpointed
    state.any_deadline = state.any_deadline or report.deadline_reached
    attempted = set(report.attempted_codes)
    deferred = set(report.deferred_codes)
    state.official_order = (
        *(code for code in official_pending if code in deferred and code not in attempted),
        *(code for code in official_pending if code in deferred and code in attempted),
    )
    state.official_pending_codes = len(report.deferred_codes)
    refreshed_codes = tuple(sorted(set((*report.changed_codes, *report.completed_codes))))
    recovery_started = context.monotonic()
    if refreshed_codes:
        loader.recover_from_data_plane(context.observed_at, retain_stale=True, codes=refreshed_codes)
        state.observations.update(loader.cached(refreshed_codes, include_structured=True, fresh_only=False))
    state.official_recovery_seconds = max(0.0, context.monotonic() - recovery_started)
    _publish_stage_progress(
        state,
        _RiskStageUpdate(
            "official_announcements",
            "waiting" if report.persistence_error == "busy" else "deferred" if report.deferred_codes else "completed",
            len(report.completed_codes) + len(report.failed_codes),
            len(official_pending),
            official_pages_fetched=state.official_pages_fetched,
            official_pages_checkpointed=state.official_pages_checkpointed,
            official_announcements_saved=state.official_announcements_saved,
            official_resumed_codes=len(state.official_resumed_codes),
        ),
    )
    return True


def _publish_research_progress(
    state: _RiskDownloadState,
    stage: RiskDownloadProgressStage,
    completed_units: int,
    total_units: int,
) -> None:
    _publish_stage_progress(state, _RiskStageUpdate(stage, "running", completed_units, total_units))


def _publish_official_progress(
    state: _RiskDownloadState,
    update: RiskAnnouncementProgress,
) -> None:
    completed_units, total_units, pages_fetched, pages_checkpointed, announcements_saved, completed_history = update
    state.official_pending_codes = max(0, state.official_wave_initial_pending - completed_history)
    _publish_stage_progress(
        state,
        _RiskStageUpdate(
            "official_announcements",
            "running",
            completed_units,
            total_units,
            state.official_pages_fetched + pages_fetched,
            state.official_pages_checkpointed + pages_checkpointed,
            state.official_announcements_saved + announcements_saved,
            len(state.official_resumed_codes),
        ),
    )


def _publish_stage_progress(state: _RiskDownloadState, update: _RiskStageUpdate) -> None:
    progress = state.progress
    if progress is None:
        return
    progress.publish(
        RiskDownloadProgress(
            stage=update.stage,
            state=update.state,
            wave=state.wave_count,
            completed_units=update.completed_units,
            total_units=update.total_units,
            requested_codes=len(state.normalized),
            research_pending_codes=state.research_pending_codes,
            official_pending_codes=state.official_pending_codes,
            changed_codes=len(state.changed_codes),
            official_pages_fetched=(
                state.official_pages_fetched if update.official_pages_fetched is None else update.official_pages_fetched
            ),
            official_pages_checkpointed=(
                state.official_pages_checkpointed
                if update.official_pages_checkpointed is None
                else update.official_pages_checkpointed
            ),
            official_announcements_saved=(
                state.official_announcements_saved
                if update.official_announcements_saved is None
                else update.official_announcements_saved
            ),
            official_resumed_codes=(
                len(state.official_resumed_codes)
                if update.official_resumed_codes is None
                else update.official_resumed_codes
            ),
            official_sync_seconds=state.official_sync_seconds,
            official_recovery_seconds=state.official_recovery_seconds,
            official_request_seconds=state.official_request_seconds,
            official_parse_seconds=state.official_parse_seconds,
            official_evidence_write_seconds=state.official_evidence_write_seconds,
            official_cursor_write_seconds=state.official_cursor_write_seconds,
            official_persistence_error=state.official_persistence_error,
            official_source_error=state.official_source_error,
        )
    )


def _publish_summary_progress(
    state: _RiskDownloadState,
    pending: _RiskPendingCodes,
    *,
    stage: RiskDownloadProgressStage,
    state_name: RiskDownloadProgressState,
    completed_units: int,
) -> None:
    progress = state.progress
    if progress is None:
        return
    available, covered = _coverage_counts(state.normalized, state.observations)
    deferred = set((*pending.missing, *pending.retryable, *pending.official))
    progress.publish(
        RiskDownloadProgress(
            stage=stage,
            state=state_name,
            wave=state.wave_count,
            completed_units=completed_units,
            total_units=len(state.normalized),
            requested_codes=len(state.normalized),
            research_pending_codes=state.research_pending_codes,
            official_pending_codes=state.official_pending_codes,
            available_codes=available if state.research_enabled else None,
            covered_codes=covered if state.research_enabled else None,
            deferred_codes=len(deferred),
            changed_codes=len(state.changed_codes),
            official_pages_fetched=state.official_pages_fetched,
            official_pages_checkpointed=state.official_pages_checkpointed,
            official_announcements_saved=state.official_announcements_saved,
            official_resumed_codes=len(state.official_resumed_codes),
            official_persistence_error=state.official_persistence_error,
            official_source_error=state.official_source_error,
        )
    )


def _publish_result_progress(
    progress: RiskDownloadProgressPort | None,
    result: RiskDownloadResult,
    state: _RiskDownloadState,
) -> None:
    if progress is None:
        return
    progress.publish(
        RiskDownloadProgress(
            stage="finished",
            state="completed" if not result.needs_retry else "stopped",
            wave=result.wave_count,
            completed_units=result.requested_codes - result.deferred_codes - result.failed_codes,
            total_units=result.requested_codes,
            requested_codes=result.requested_codes,
            research_pending_codes=state.research_pending_codes,
            official_pending_codes=state.official_pending_codes,
            available_codes=result.available_codes if state.research_enabled else None,
            covered_codes=result.covered_codes if state.research_enabled else None,
            deferred_codes=result.deferred_codes,
            changed_codes=result.changed_codes,
            official_pages_fetched=result.official_pages_fetched,
            official_pages_checkpointed=result.official_pages_checkpointed,
            official_announcements_saved=result.official_announcements_saved,
            official_resumed_codes=result.official_resumed_codes,
            official_persistence_error=result.official_persistence_error,
            official_source_error=result.official_source_error,
            stop_reason=result.stop_reason,
        )
    )


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
        failed_codes=(
            max(0, len(state.normalized) - len(available) - len(deferred))
            if state.research_enabled
            else len(state.official_terminal_codes)
        ),
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
        official_pages_checkpointed=state.official_pages_checkpointed,
        official_announcements_saved=state.official_announcements_saved,
        official_resumed_codes=len(state.official_resumed_codes),
        official_failed_codes=len(state.official_terminal_codes),
        official_persistence_error=state.official_persistence_error,
        official_source_error=state.official_source_error,
        research_coverage_required=state.research_enabled,
    )


def _coverage_counts(
    codes: Sequence[str],
    observations: Mapping[str, ResearchObservation],
) -> tuple[int, int]:
    coverage = tuple(
        _research_component_coverage(observation)
        for code in codes
        if (observation := observations.get(code)) is not None
    )
    return sum(any(status) for status in coverage), sum(all(status) for status in coverage)


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
    pending: _RiskPendingCodes,
    research_advances: int,
    official_checkpoint_advances: int,
) -> _RiskProgressIdentity:
    return _RiskProgressIdentity(
        research_pending=tuple(sorted(set((*pending.missing, *pending.retryable)))),
        official_pending=tuple(sorted(pending.official)),
        research_advances=research_advances,
        official_checkpoint_advances=official_checkpoint_advances,
    )


def _observation_progress(code: str, observation: ResearchObservation) -> _RiskObservationProgress:
    return _RiskObservationProgress(
        code=code,
        news_queried=observation.news_queried,
        evidence_ids=tuple(item.evidence_id for item in observation.evidence),
        announcement_ids=tuple(item.announcement_id for item in observation.announcements),
        financial_reports=tuple((item.report_date, item.published_at) for item in observation.financial_history),
        financial_history_complete=observation.financial_history_complete,
        financial_available=observation.financial is not None,
        announcements_available=observation.announcements_available,
        pledge_available=observation.pledge_ratio_pct is not None,
        unlock_available=observation.unlock_ratio_pct is not None,
        regulatory_facts=tuple((item.category.value, item.evidence_id) for item in observation.corporate_risk_facts),
        regulatory_history_complete=observation.corporate_risk_history_complete,
    )


def _research_observation_advanced(
    code: str,
    previous: ResearchObservation | None,
    current: ResearchObservation,
) -> bool:
    if previous is not None and _has_retryable_source_error(previous) and not _has_retryable_source_error(current):
        return True
    return _observation_progressed(
        None if previous is None else _observation_progress(code, previous),
        _observation_progress(code, current),
    )


def _made_durable_progress(before: _RiskProgressIdentity, after: _RiskProgressIdentity) -> bool:
    if set(after.research_pending) < set(before.research_pending):
        return True
    if set(after.official_pending) < set(before.official_pending):
        return True
    if after.official_checkpoint_advances > before.official_checkpoint_advances:
        return True
    return after.research_advances > before.research_advances


def _observation_progressed(
    previous: _RiskObservationProgress | None,
    current: _RiskObservationProgress,
) -> bool:
    if previous is None:
        return _observation_has_durable_progress(current)
    if (
        (current.news_queried and not previous.news_queried)
        or (current.financial_history_complete and not previous.financial_history_complete)
        or (current.financial_available and not previous.financial_available)
        or (current.announcements_available and not previous.announcements_available)
        or (current.pledge_available and not previous.pledge_available)
        or (current.unlock_available and not previous.unlock_available)
        or (current.regulatory_history_complete and not previous.regulatory_history_complete)
    ):
        return True
    return any(
        set(current_values) - set(previous_values)
        for current_values, previous_values in (
            (current.evidence_ids, previous.evidence_ids),
            (current.announcement_ids, previous.announcement_ids),
            (current.financial_reports, previous.financial_reports),
            (current.regulatory_facts, previous.regulatory_facts),
        )
    )


def _observation_has_durable_progress(observation: _RiskObservationProgress) -> bool:
    return any(
        (
            observation.news_queried,
            observation.financial_history_complete,
            observation.financial_available,
            observation.announcements_available,
            observation.pledge_available,
            observation.unlock_available,
            observation.regulatory_history_complete,
            bool(observation.evidence_ids),
            bool(observation.announcement_ids),
            bool(observation.financial_reports),
            bool(observation.regulatory_facts),
        )
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
    "RiskDownloadProgress",
    "RiskDownloadProgressPort",
    "RiskDownloadResult",
    "RiskAnnouncementSynchronizer",
    "RiskResearchLoadReport",
]
