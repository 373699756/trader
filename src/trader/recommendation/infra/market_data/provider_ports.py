"""Supplier-facing protocols for recommendation market-data orchestration.

Concrete vendor clients are composed in ``bootstrap``.  Recommendation
orchestrators depend on these narrow protocols so vendor classes and SDK
details do not cross the market-data boundary.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from datetime import date, datetime
from typing import Protocol

from trader.infra.market_data.observations import SourceObservation
from trader.infra.market_data.providers.baostock_industry import BaoStockIndustryHealthStatus
from trader.infra.market_data.providers.exchange_security_master import ExchangeSecurityMasterHealthStatus
from trader.infra.market_data.providers.tushare import TushareHealthStatus
from trader.recommendation.domain.market.models import Evidence, MarketQuote
from trader.recommendation.domain.market.research import ResearchObservation
from trader.recommendation.domain.market.tail import MinuteBar


class FullMarketSource(Protocol):
    def fetch_market(self) -> Sequence[MarketQuote]: ...


class CandidateQuoteSource(Protocol):
    def fetch_quotes(
        self,
        codes: Sequence[str],
        *,
        timeout_seconds: float | None = None,
    ) -> Sequence[MarketQuote]: ...


class IntradaySource(Protocol):
    def fetch_intraday_minutes(self, code: str, *, now: datetime | None = None) -> tuple[MinuteBar, ...]: ...


FullMarketFetcher = Callable[[datetime | None, threading.Event], Sequence[MarketQuote]]


class ResearchSource(Protocol):
    def fetch_snapshot(self, code: str, *, observed_at: datetime) -> ResearchObservation: ...

    def fetch_news(self, code: str, *, observed_at: datetime, limit: int = 5) -> Sequence[Evidence]: ...


class IndustryReferenceSource(Protocol):
    def fetch(self, observed_at: datetime) -> Sequence[SourceObservation]: ...


class SecurityMasterSource(Protocol):
    def fetch(self, observed_at: datetime) -> Sequence[SourceObservation]: ...

    def health(self) -> ExchangeSecurityMasterHealthStatus: ...


class ModelIndustrySource(IndustryReferenceSource, Protocol):
    def health(self) -> BaoStockIndustryHealthStatus: ...


class ReferenceSource(Protocol):
    def supports(self, dataset: str) -> bool: ...

    def fetch_security_master(self, observed_at: datetime) -> Sequence[SourceObservation]: ...

    def fetch_trading_calendar(
        self,
        start_date: date,
        end_date: date,
        observed_at: datetime,
    ) -> Sequence[SourceObservation]: ...

    def fetch_daily_valuations(
        self,
        codes: Sequence[str],
        trade_date: date,
        observed_at: datetime,
    ) -> Sequence[SourceObservation]: ...

    def fetch_financial_indicators(
        self,
        codes: Sequence[str],
        observed_at: datetime,
    ) -> Sequence[SourceObservation]: ...

    def health(self) -> TushareHealthStatus: ...


__all__ = [
    "CandidateQuoteSource",
    "FullMarketFetcher",
    "FullMarketSource",
    "IndustryReferenceSource",
    "IntradaySource",
    "ModelIndustrySource",
    "ReferenceSource",
    "ResearchSource",
    "SecurityMasterSource",
    "TushareHealthStatus",
]
