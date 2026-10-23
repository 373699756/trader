"""Pure market-session alignment shared by training and online model inputs."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Literal

HISTORY_ALIGNMENT_CONTRACT = "market_calendar_forward_valuation_independent_targets"
SessionKind = Literal["trading", "suspended", "estimated", "invalid"]
HISTORY_INPUT_FAILURES = frozenset(
    {
        "history_snapshot_unavailable",
        "history_window_insufficient",
        "history_model_date_mismatch",
        "history_invalid_prices",
    }
)


@dataclass(frozen=True, slots=True)
class ModelHistoryQuality:
    failure: str | None = None
    estimated_sessions: int = 0
    suspended_sessions: int = 0

    def __post_init__(self) -> None:
        if self.failure is not None and self.failure not in HISTORY_INPUT_FAILURES:
            raise ValueError("model history failure is invalid")
        if min(self.estimated_sessions, self.suspended_sessions) < 0:
            raise ValueError("model history counts are invalid")


@dataclass(frozen=True, slots=True)
class MarketDailyObservation:
    trade_date: date
    close: float | None
    amount: float | None
    open_price: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None
    kind: SessionKind = "trading"


@dataclass(frozen=True, slots=True)
class AlignedMarketSession:
    trade_date: date
    close: float
    amount: float
    open_price: float | None
    high: float | None
    low: float | None
    volume: float | None
    kind: SessionKind

    @property
    def label_available(self) -> bool:
        return self.kind in {"trading", "suspended"}


def AlignMarketSession(
    trade_date: date,
    observation: MarketDailyObservation | None,
    previous: AlignedMarketSession | None,
) -> AlignedMarketSession | None:
    """Preserve calendar positions; estimated prices never become observed labels.

    Callers enforce listing boundaries and a verified source snapshot. An invalid
    observed row is not a no-trade day and cannot be repaired by this function.
    """
    if previous is not None and previous.trade_date >= trade_date:
        raise ValueError("history_alignment_order_invalid")
    if observation is not None and observation.trade_date != trade_date:
        raise ValueError("history_alignment_date_mismatch")
    kind: SessionKind = observation.kind if observation is not None else "estimated"
    if kind == "invalid":
        return None
    if kind == "trading":
        assert observation is not None
        if not _positive(observation.close) or not _nonnegative(observation.amount):
            return None
        if any(
            value is not None and not _positive(value)
            for value in (observation.open_price, observation.high, observation.low)
        ):
            return None
        if observation.volume is not None and not _nonnegative(observation.volume):
            return None
        assert observation.close is not None and observation.amount is not None
        return AlignedMarketSession(
            trade_date,
            observation.close,
            observation.amount,
            observation.open_price,
            observation.high,
            observation.low,
            observation.volume,
            kind,
        )
    # A supplier-marked suspension may carry a trustworthy daily valuation.
    close = (
        observation.close if kind == "suspended" and observation is not None and _positive(observation.close) else None
    )
    if close is None:
        if kind == "suspended" and previous is not None and not previous.label_available:
            kind = "estimated"
        close = previous.close if previous is not None else None
    if close is None:
        return None
    return AlignedMarketSession(trade_date, close, 0.0, close, close, close, 0.0, kind)


def _positive(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0.0


def _nonnegative(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value >= 0.0


__all__ = [
    "HISTORY_ALIGNMENT_CONTRACT",
    "SessionKind",
    "MarketDailyObservation",
    "AlignedMarketSession",
    "AlignMarketSession",
    "ModelHistoryQuality",
    "HISTORY_INPUT_FAILURES",
]
