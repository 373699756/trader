"""Pure, dated OHLCV/amount sequences; no clock, configuration or I/O."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

from trader.recommendation.domain.market.history_alignment import HISTORY_ALIGNMENT_CONTRACT

SEQUENCE_STEPS = 60
SEQUENCE_CHANNELS = ("open_return", "high_return", "low_return", "close_return", "log_volume", "log_amount")
SEQUENCE_CONTRACT = "completed_daily_61_ohlcv_amount_before_signal:" + HISTORY_ALIGNMENT_CONTRACT


@dataclass(frozen=True, slots=True)
class PriceVolumePoint:
    trade_date: date
    open_price: float
    high: float
    low: float
    close: float
    volume: float
    amount: float

    def __post_init__(self) -> None:
        if type(self.trade_date) is not date:
            raise ValueError("V5 sequence point requires a market date")
        if any(not math.isfinite(v) or v <= 0 for v in (self.open_price, self.high, self.low, self.close)):
            raise ValueError("V5 requires positive complete OHLC")
        if any(not math.isfinite(v) or v < 0 for v in (self.volume, self.amount)):
            raise ValueError("V5 requires nonnegative complete volume and amount")
        if not self.low <= min(self.open_price, self.close) <= max(self.open_price, self.close) <= self.high:
            raise ValueError("V5 OHLC order is invalid")


@dataclass(frozen=True, slots=True)
class ModelPriceVolumeSequence:
    code: str
    dates: tuple[date, ...]
    values: tuple[float, ...]
    source_identity: str
    contract: str = SEQUENCE_CONTRACT

    def __post_init__(self) -> None:
        if (
            len(self.code) != 6
            or not self.code.isdigit()
            or not self.source_identity
            or len(self.dates) != 61
            or any(type(day) is not date for day in self.dates)
            or self.dates != tuple(sorted(set(self.dates)))
            or len(self.values) != SEQUENCE_STEPS * len(SEQUENCE_CHANNELS)
            or any(not math.isfinite(v) for v in self.values)
            or self.contract != SEQUENCE_CONTRACT
        ):
            raise ValueError("V5 sequence identity or dimensions are invalid")


def CalculatePriceVolumeSequence(
    code: str,
    points: tuple[PriceVolumePoint, ...],
    *,
    expected_dates: tuple[date, ...],
    source_identity: str,
) -> ModelPriceVolumeSequence:
    if len(points) != 61 or tuple(p.trade_date for p in points) != expected_dates:
        raise ValueError("V5 requires 61 contiguous market dates")
    values: list[float] = []
    for previous, current in zip(points[:-1], points[1:], strict=True):
        values.extend(
            (
                current.open_price / previous.close - 1,
                current.high / previous.close - 1,
                current.low / previous.close - 1,
                current.close / previous.close - 1,
                math.log1p(current.volume),
                math.log1p(current.amount),
            )
        )
    return ModelPriceVolumeSequence(code, expected_dates, tuple(values), source_identity)


__all__ = [
    "SEQUENCE_STEPS",
    "SEQUENCE_CHANNELS",
    "SEQUENCE_CONTRACT",
    "PriceVolumePoint",
    "ModelPriceVolumeSequence",
    "CalculatePriceVolumeSequence",
]
