"""Typed reports and coverage summaries for structured market research."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from trader.recommendation.domain.market.research import (
    CorporateRiskCategory,
    ResearchComponentStatus,
    ResearchObservation,
    research_components_from_observation,
)

RESEARCH_COMPONENT_IDS: Final[tuple[str, ...]] = (
    "financial",
    "announcements",
    "pledge",
    "unlock",
    "penalty",
    "lawsuit_restructuring",
    "forced_delisting",
    "suspension",
)


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
    corporate_risk_evidence_versions: tuple[str, ...]
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


def research_component_statuses(observation: ResearchObservation) -> tuple[ResearchComponentStatus, ...]:
    components = research_components_from_observation(observation)
    penalty_status: ResearchComponentStatus = (
        "known_risk" if any(fact.category is not None for fact in observation.corporate_risk_facts) else "unknown"
    )
    lawsuit_restructuring_status: ResearchComponentStatus = (
        "known_risk"
        if any(
            fact.category is CorporateRiskCategory.MAJOR_SHAREHOLDER_REDUCTION
            for fact in observation.corporate_risk_facts
        )
        else "unknown"
    )
    delisting_status: ResearchComponentStatus = (
        "known_risk"
        if any(fact.category is CorporateRiskCategory.FORCED_DELISTING for fact in observation.corporate_risk_facts)
        else "unknown"
    )
    suspension_status: ResearchComponentStatus = "unknown"

    return (
        components.financial,
        components.announcements,
        components.pledge,
        components.unlock,
        penalty_status,
        lawsuit_restructuring_status,
        delisting_status,
        suspension_status,
    )


def research_component_coverage(
    observation: ResearchObservation,
) -> tuple[bool, bool, bool, bool]:
    coverage = research_component_statuses(observation)
    return (
        coverage[0] in {"known_clear", "known_risk"},
        coverage[1] in {"known_clear", "known_risk"},
        coverage[2] in {"known_clear", "known_risk"},
        coverage[3] in {"known_clear", "known_risk"},
    )


__all__ = [
    "ResearchLoaderStatus",
    "ResearchLoadReport",
    "research_component_coverage",
]
