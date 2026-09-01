"""Typed protocol for formal recommendation outcome settlement."""

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


__all__ = ["OutcomeSettlementMarketData"]
