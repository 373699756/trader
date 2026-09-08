"""Immutable recommendation outcome values."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Literal

from trader.domain.recommendation.models import Strategy


def outcome_horizons(strategy: Strategy) -> tuple[int, ...]:
    if strategy in {Strategy.TODAY, Strategy.TOMORROW}:
        return (1,)
    if strategy is Strategy.D25:
        return (2, 3, 4, 5)
    raise ValueError("outcome strategy is unsupported")


@dataclass(frozen=True)
class OutcomePrice:
    open_price: float
    high: float
    low: float
    close: float

    def __post_init__(self) -> None:
        prices = (self.open_price, self.high, self.low, self.close)
        if any(not math.isfinite(value) or value <= 0.0 for value in prices):
            raise ValueError("outcome prices must be finite and positive")
        if self.high < max(self.open_price, self.close, self.low) or self.low > min(
            self.open_price,
            self.close,
            self.high,
        ):
            raise ValueError("outcome OHLC prices are inconsistent")


class OutcomeTradingStatus(str, Enum):
    TRADABLE = "tradable"
    SUSPENDED = "suspended"
    ONE_PRICE_LIMIT_DOWN = "one_price_limit_down"
    UNKNOWN = "unknown"


class OutcomeExitStatus(str, Enum):
    TRADABLE = "tradable"
    SUSPENDED = "suspended"
    ONE_PRICE_LIMIT_DOWN = "one_price_limit_down"
    MISSING_CARRIED_FORWARD = "missing_carried_forward"


@dataclass(frozen=True)
class OutcomeBar:
    trade_date: str
    qfq: OutcomePrice
    raw: OutcomePrice
    trading_status: OutcomeTradingStatus
    source: str

    def __post_init__(self) -> None:
        try:
            date.fromisoformat(self.trade_date)
        except ValueError as exc:
            raise ValueError("outcome bar trade date must use ISO format") from exc
        if not isinstance(self.trading_status, OutcomeTradingStatus):
            raise TypeError("outcome bar trading status must be typed")
        if not self.source.strip():
            raise ValueError("outcome bar source is required")


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
    anchor_raw_price: float
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
    anchor_raw_price: float
    anchor_qfq_price: float | None
    atr20_pct: float
    minimum_qfq_low: float | None = None
    end_qfq_close: float | None = None
    exit_status: OutcomeExitStatus | None = None
    untradable_dates: tuple[str, ...] = ()
    gross_return_pct: float | None = None
    benchmark_return_pct: float | None = None
    net_excess_return_pct: float | None = None
    mae_pct: float | None = None
    mae_atr: float | None = None
    severe_drawdown: bool | None = None
    quality_reason: str = ""
    schema_identity: str = "recommendation_outcome_qfq_tradability_cost20bp"

    def __post_init__(self) -> None:
        if self.horizon not in outcome_horizons(self.strategy):
            raise ValueError("recommendation outcome horizon is incompatible with strategy")
        if self.status not in {"complete", "benchmark_missing", "insufficient_data"}:
            raise ValueError("recommendation outcome status is invalid")
        if self.exit_status is not None and not isinstance(self.exit_status, OutcomeExitStatus):
            raise TypeError("recommendation outcome exit status must be typed")
        if tuple(sorted(set(self.untradable_dates))) != self.untradable_dates:
            raise ValueError("untradable outcome dates must be sorted and unique")
        for trade_date in self.untradable_dates:
            try:
                date.fromisoformat(trade_date)
            except ValueError as exc:
                raise ValueError("untradable outcome dates must use ISO format") from exc
        if self.status != "insufficient_data":
            required = (
                self.anchor_raw_price,
                self.anchor_qfq_price,
                self.minimum_qfq_low,
                self.end_qfq_close,
                self.gross_return_pct,
                self.mae_pct,
                self.mae_atr,
            )
            if self.exit_status is None or any(value is None or not math.isfinite(value) for value in required):
                raise ValueError("settled recommendation outcome is incomplete")
        if self.status == "complete" and (self.benchmark_return_pct is None or self.net_excess_return_pct is None):
            raise ValueError("complete recommendation outcome requires benchmark and net excess return")

    @property
    def exit_untradable(self) -> bool | None:
        if self.exit_status is None:
            return None
        return self.exit_status is not OutcomeExitStatus.TRADABLE


__all__ = [
    "BenchmarkConstituentReturn",
    "BenchmarkReturn",
    "OutcomeBar",
    "OutcomeExitStatus",
    "OutcomePrice",
    "OutcomeTarget",
    "OutcomeTradingStatus",
    "RecommendationOutcome",
    "outcome_horizons",
]
