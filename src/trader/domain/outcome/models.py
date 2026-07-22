"""Immutable recommendation outcome values."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from trader.domain.recommendation.models import Strategy


@dataclass(frozen=True)
class OutcomeBar:
    trade_date: str
    open_price: float
    high: float
    low: float
    close: float
    pct_change: float


@dataclass(frozen=True)
class BenchmarkReturn:
    trade_date: str
    return_pct: float


@dataclass(frozen=True)
class OutcomeTarget:
    snapshot_id: str
    strategy: Strategy
    recommend_date: str
    stock_code: str
    anchor_price: float
    atr20_pct: float


@dataclass(frozen=True)
class RecommendationOutcome:
    snapshot_id: str
    strategy: Strategy
    recommend_date: str
    stock_code: str
    horizon: int
    status: Literal["complete", "benchmark_missing", "insufficient_data"]
    settled_at: datetime
    anchor_price: float
    atr20_pct: float
    minimum_low: float | None = None
    end_close: float | None = None
    gross_return_pct: float | None = None
    benchmark_return_pct: float | None = None
    net_excess_return_pct: float | None = None
    mae_pct: float | None = None
    mae_atr: float | None = None
    severe_drawdown: bool | None = None
    quality_reason: str = ""
    version: str = "outcome_v1_mae_atr_cost20bp"


__all__ = ["BenchmarkReturn", "OutcomeBar", "OutcomeTarget", "RecommendationOutcome"]
