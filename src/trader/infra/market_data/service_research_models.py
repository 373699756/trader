"""Typed reports and coverage summaries for structured market research."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from trader.domain.market.research import ResearchObservation


@dataclass(frozen=True)
class ResearchLoaderStatus:
    entries: int
    success_count: int
    error_count: int
    planned_count: int
    timeout_count: int
    consecutive_failures: int
    circuit_open: bool
    latencies_ms: tuple[float, ...]
    latest_source_time: datetime | None
    last_error: str
    out_of_order_count: int
    corporate_risk_covered_count: int
    corporate_risk_fact_count: int
    corporate_risk_registry_versions: tuple[str, ...]
    verified_count: int
    partial_count: int
    unavailable_count: int
    financial_covered_count: int
    announcements_covered_count: int
    pledge_covered_count: int
    unlock_covered_count: int


@dataclass(frozen=True)
class ResearchLoadReport:
    observations: Mapping[str, ResearchObservation]
    changed_codes: tuple[str, ...] = ()
    deferred_codes: tuple[str, ...] = ()
    deadline_reached: bool = False


def research_component_coverage(
    observation: ResearchObservation,
) -> tuple[bool, bool, bool, bool]:
    return (
        observation.financial is not None,
        observation.announcements_available and observation.corporate_risk_history_complete,
        observation.pledge_ratio_pct is not None,
        observation.unlock_ratio_pct is not None,
    )


__all__ = [
    "ResearchLoaderStatus",
    "ResearchLoadReport",
    "research_component_coverage",
]
