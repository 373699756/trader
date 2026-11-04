"""Prepare one immutable reference cut through explicitly injected supplier ports."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Protocol

from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.history_reference import HistoryStEvidence
from trader.download.domain.prepared_reference import PreparedReference, SelectEligibleSecurities


class IndustryVersionPort(Protocol):
    @property
    def data_version(self) -> str: ...


@dataclass(frozen=True)
class ReferencePreparationDependencies:
    st_evidence: Callable[[tuple[BaoStockSecurity, ...], date], tuple[HistoryStEvidence, ...]]
    calendar: Callable[[], tuple[date, ...]]
    industries: Callable[[datetime], tuple[IndustryVersionPort, ...]]
    publish: Callable[[PreparedReference, tuple[HistoryStEvidence, ...]], object]
    cancelled: Callable[[], bool]


def PrepareReference(
    universe: tuple[BaoStockSecurity, ...],
    observed_at: datetime,
    dependencies: ReferencePreparationDependencies,
) -> PreparedReference:
    if dependencies.cancelled():
        raise RuntimeError("reference_preparation_cancelled")
    source_evidence = dependencies.st_evidence(universe, observed_at.date())
    if dependencies.cancelled():
        raise RuntimeError("reference_preparation_cancelled")
    population = tuple(sorted(universe, key=lambda item: item.code))
    evidence = tuple(
        replace(item, checked_on=observed_at.date()) for item in sorted(source_evidence, key=lambda item: item.code)
    )
    if not SelectEligibleSecurities(population, evidence):
        raise ValueError("sync_population_empty")
    calendar = dependencies.calendar()
    if dependencies.cancelled():
        raise RuntimeError("reference_preparation_cancelled")
    try:
        industries = dependencies.industries(observed_at)
    except (OSError, RuntimeError, ValueError):
        industries = ()
    reference = PreparedReference(
        population,
        evidence,
        observed_at,
        calendar,
        industries[0].data_version if industries else None,
    )
    if dependencies.cancelled():
        raise RuntimeError("reference_preparation_cancelled")
    dependencies.publish(reference, source_evidence)
    return reference


__all__ = ["IndustryVersionPort", "PrepareReference", "ReferencePreparationDependencies"]
