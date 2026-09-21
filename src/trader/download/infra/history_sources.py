"""Adapters used only by the explicit downloadable historical screening command."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Protocol

from trader.infra.market_data.history.history_seed import DailyHistoryClient
from trader.recommendation.domain.candidate.filters import board_for_code
from trader.training.application.historical_screening import HistoricalSecurity
from trader.training.domain.evaluation.historical_screening import HistoricalPriceBar


class HistoricalQuote(Protocol):
    code: str
    name: str
    is_st: bool
    is_suspended: bool


class HistoricalUniverseSource(Protocol):
    def fetch_market(self) -> Sequence[HistoricalQuote]: ...


class SinaHistoricalUniverseProvider:
    def __init__(self, client: HistoricalUniverseSource) -> None:
        self._client = client

    def fetch(self) -> tuple[HistoricalSecurity, ...]:
        return tuple(
            HistoricalSecurity(
                quote.code,
                board_for_code(quote.code).value,
                quote.name,
                quote.is_st,
                quote.is_suspended,
            )
            for quote in self._client.fetch_market()
        )


class HistoricalPriceProviderAdapter:
    def __init__(self, client: DailyHistoryClient) -> None:
        self._client = client

    def fetch_history(self, code: str, *, days: int) -> tuple[HistoricalPriceBar, ...]:
        return tuple(
            HistoricalPriceBar(
                trade_date=date.fromisoformat(item.trade_date),
                open_price=item.open_price,
                close=item.close,
                high=item.high,
                low=item.low,
                volume=item.volume,
                amount=item.amount,
                pct_change=item.pct_change,
                turnover_rate=item.turnover_rate,
                adjustment=item.adjustment.value,
                source=item.source,
            )
            for item in self._client.fetch_history(code, days=days)
        )


__all__ = ["HistoricalPriceProviderAdapter", "SinaHistoricalUniverseProvider"]
