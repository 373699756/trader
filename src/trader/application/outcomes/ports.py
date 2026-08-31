"""Typed protocols shared by outcome and supplemental research settlement."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from trader.application.ports.market import OutcomePriceReaderPort
from trader.domain.market.models import FeatureSnapshot


class OutcomeSettlementMarketData(OutcomePriceReaderPort, Protocol):
    def fetch_market_features(
        self,
        observed_at: datetime,
        *,
        force: bool = False,
    ) -> Sequence[FeatureSnapshot]: ...


class SupplementalOutcomeSettlementPort(Protocol):
    def settle(self, now: datetime, market_features: Sequence[FeatureSnapshot]) -> object: ...


__all__ = ["OutcomeSettlementMarketData", "SupplementalOutcomeSettlementPort"]
