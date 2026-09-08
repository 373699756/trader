"""Immutable recommendation outcome values."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from trader.domain.recommendation.models import Strategy


def outcome_horizons(strategy: Strategy) -> tuple[int, ...]:
    if strategy in {Strategy.TODAY, Strategy.TOMORROW}:
        return (1,)
    if strategy is Strategy.D25:
        return (2, 3, 4, 5)
    raise ValueError("outcome strategy is unsupported")


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
class BenchmarkConstituentReturn:
    stock_code: str
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
    pending_horizons: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        supplied = tuple(self.pending_horizons)
        pending = tuple(sorted(set(supplied)))
        if supplied != pending:
            raise ValueError("pending outcome horizons must be sorted and unique")
        if any(horizon not in outcome_horizons(self.strategy) for horizon in pending):
            raise ValueError("pending outcome horizons are incompatible with strategy")
        object.__setattr__(self, "pending_horizons", pending)


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
    schema_identity: str = "recommendation_outcome_mae_atr_cost20bp"

    def __post_init__(self) -> None:
        if self.horizon not in outcome_horizons(self.strategy):
            raise ValueError("recommendation outcome horizon is incompatible with strategy")


__all__ = [
    "BenchmarkConstituentReturn",
    "BenchmarkReturn",
    "OutcomeBar",
    "OutcomeTarget",
    "RecommendationOutcome",
    "outcome_horizons",
]
