"""Typed issuer-eligibility boundary shared by market-data adapters."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from trader.domain.market.eligibility import (
    IssuerEligibilityDecision,
    IssuerEligibilityFact,
    IssuerEligibilityRegistryStatus,
)


class IssuerEligibilityPort(Protocol):
    def record(self, facts: Sequence[IssuerEligibilityFact]) -> int: ...

    def filter_codes(self, codes: Sequence[str], observed_at: datetime) -> tuple[str, ...]: ...

    def exclusions(self, observed_at: datetime) -> tuple[IssuerEligibilityDecision, ...]: ...

    def facts(self) -> tuple[IssuerEligibilityFact, ...]: ...

    def status(self) -> IssuerEligibilityRegistryStatus: ...


__all__ = ["IssuerEligibilityPort"]
