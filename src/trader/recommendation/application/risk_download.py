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

    @property
    def needs_retry(self) -> bool:
        return self.covered_codes < self.requested_codes or self.deadline_reached


@dataclass(frozen=True)
class RiskDownloadContext:
    target_date: date
    observed_at: datetime
    monotonic: Callable[[], float]


@dataclass(frozen=True)
class RiskDownloadBudget:
    source_seconds: float = 120.0
    task_seconds: float = 600.0


_DEFAULT_RISK_DOWNLOAD_BUDGET = RiskDownloadBudget()


@dataclass(frozen=True)
class _EmptyRiskResearchLoadReport:
    observations: Mapping[str, ResearchObservation]
    changed_codes: tuple[str, ...] = ()
    deferred_codes: tuple[str, ...] = ()
    deadline_reached: bool = False


def DownloadRiskSnapshot(
    loader: RiskResearchLoader,
    codes: Sequence[str],
    context: RiskDownloadContext,
    budget: RiskDownloadBudget = _DEFAULT_RISK_DOWNLOAD_BUDGET,
) -> RiskDownloadResult:
    normalized = tuple(sorted(set(codes)))
    started = context.monotonic()
    loader.recover_from_data_plane(context.observed_at, retain_stale=True)
    observations = dict(loader.cached(normalized, include_structured=True, fresh_only=False))
    missing = tuple(code for code in normalized if code not in observations)
    refresh_codes = missing or tuple(code for code in normalized if _has_retryable_source_error(observations[code]))
    deadline_seconds = min(budget.source_seconds, budget.task_seconds)
    report = (
        loader.load_report(
            refresh_codes,
            context.observed_at,
            include_structured=True,
            force=not missing,
            deadline=context.observed_at + timedelta(seconds=deadline_seconds),
        )
        if refresh_codes
        else _EmptyRiskResearchLoadReport({})
    )
    observations.update(report.observations)
    coverage = {code: _research_component_coverage(observation) for code, observation in observations.items()}
    available = {code for code, status in coverage.items() if any(status)}
    covered = {code for code, status in coverage.items() if all(status)}
    return RiskDownloadResult(
        target_date=context.target_date,
        requested_codes=len(normalized),
        available_codes=len(available),
        covered_codes=len(covered),
        partial_codes=len(available - covered),
        changed_codes=len(report.changed_codes),
        deferred_codes=len(report.deferred_codes),
        failed_codes=max(0, len(normalized) - len(available) - len(report.deferred_codes)),
        deadline_reached=report.deadline_reached,
        elapsed_seconds=max(0.0, context.monotonic() - started),
    )


_RETRYABLE_SOURCE_FAILURES = frozenset(
    {
        "timeout",
        "deadline",
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
    "RiskDownloadResult",
    "RiskResearchLoadReport",
]
