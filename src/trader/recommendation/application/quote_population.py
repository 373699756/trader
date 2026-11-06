"""Read the current ST-qualified population before an external quote request."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from trader.recommendation.application.ports.static_reference import StaticReferenceRead
from trader.recommendation.domain.market.eligibility import (
    HistoricalStEligibilitySnapshot,
    IssuerEligibilityFreeze,
    SelectQuotePopulation,
)


@dataclass(frozen=True)
class ReadQuotePopulation:
    reference: Callable[[], StaticReferenceRead]
    exclusions: Callable[[datetime], tuple[IssuerEligibilityFreeze, ...]]
    historical_st: Callable[[], HistoricalStEligibilitySnapshot]
    clock: Callable[[], datetime]

    def execute(self) -> tuple[str, ...]:
        return SelectQuotePopulation(self.reference().reference, self.exclusions(self.clock()), self.historical_st())
