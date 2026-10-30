"""Typed issuer-eligibility boundary shared by market-data adapters."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from trader.recommendation.domain.market.eligibility import (
    IssuerEligibilityFact,
    IssuerEligibilityFreeze,
    IssuerEligibilitySnapshot,
    IssuerEligibilityView,
)


class IssuerEligibilityPort(Protocol):
    def view(self, observed_at: datetime) -> IssuerEligibilityView: ...

    def record(self, facts: Sequence[IssuerEligibilityFact]) -> int: ...

    def filter_codes(self, codes: Sequence[str], observed_at: datetime) -> tuple[str, ...]: ...

    def exclusions(self, observed_at: datetime) -> tuple[IssuerEligibilityFreeze, ...]: ...

    def facts(self) -> tuple[IssuerEligibilityFact, ...]: ...

    def status(self) -> IssuerEligibilitySnapshot: ...


__all__ = ["IssuerEligibilityPort"]
