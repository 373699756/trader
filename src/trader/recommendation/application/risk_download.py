"""Bounded offline preparation of one trade-date risk snapshot."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Protocol

from trader.recommendation.domain.market.research import ResearchObservation
from trader.recommendation.infra.market_data.research_load_status import (
    ResearchComponentCoverage,
    ResearchLoadReport,
)


class RiskResearchLoader(Protocol):
    def recover_from_data_plane(self, observed_at: datetime | None = None) -> None: ...

    def load_report(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        include_structured: bool,
        force: bool = False,
        deadline: datetime | None = None,
        action_restrictions: dict[str, set[str]] | None = None,
    ) -> ResearchLoadReport: ...


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


def DownloadRiskSnapshot(
    loader: RiskResearchLoader,
    codes: Sequence[str],
    *,
    target_date: date,
    observed_at: datetime,
    monotonic: Callable[[], float],
    source_budget_seconds: float = 120.0,
    task_budget_seconds: float = 600.0,
) -> RiskDownloadResult:
    normalized = tuple(sorted(set(codes)))
    started = monotonic()
    loader.recover_from_data_plane(observed_at)
    deadline_seconds = min(source_budget_seconds, task_budget_seconds)
    report = loader.load_report(
        normalized,
        observed_at,
        include_structured=True,
        force=False,
        deadline=observed_at + timedelta(seconds=deadline_seconds),
    )
    observations: dict[str, ResearchObservation] = dict(report.observations)
    deferred = set(report.deferred_codes)
    coverage = {
        code: ResearchComponentCoverage(observation)
        for code, observation in observations.items()
        if code not in deferred
    }
    available = {code for code, status in coverage.items() if any(status)}
    covered = {code for code, status in coverage.items() if all(status)}
    return RiskDownloadResult(
        target_date=target_date,
        requested_codes=len(normalized),
        available_codes=len(available),
        covered_codes=len(covered),
        partial_codes=len(available - covered),
        changed_codes=len(report.changed_codes),
        deferred_codes=len(report.deferred_codes),
        failed_codes=max(0, len(normalized) - len(available) - len(deferred)),
        deadline_reached=report.deadline_reached,
        elapsed_seconds=max(0.0, monotonic() - started),
    )


__all__ = ["DownloadRiskSnapshot", "RiskDownloadResult"]
