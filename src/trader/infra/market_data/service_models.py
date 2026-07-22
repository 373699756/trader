"""Private cache entry models for the market feature service."""

from __future__ import annotations

from dataclasses import dataclass

from trader.domain.market.research import ResearchObservation
from trader.domain.market.tail import MinuteBar
from trader.infra.market_data.history import DailyBar


@dataclass(frozen=True)
class _HistoryEntry:
    bars: tuple[DailyBar, ...]
    expires_at: float
    source: str = "eastmoney"


@dataclass(frozen=True)
class _ResearchEntry:
    observation: ResearchObservation
    expires_at: float


@dataclass(frozen=True)
class _IntradayEntry:
    bars: tuple[MinuteBar, ...]
    expires_at: float
