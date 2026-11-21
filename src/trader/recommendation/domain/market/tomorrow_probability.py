"""Feature and prediction contracts for the next-close models."""

from __future__ import annotations

import math
from datetime import date, datetime, time
from typing import Protocol
from zoneinfo import ZoneInfo

from trader.recommendation.domain.market.models import Board, MarketQuote
from trader.recommendation.domain.market.v5.features import SEQUENCE_CHANNELS, SEQUENCE_STEPS, ModelPriceVolumeSequence

TOMORROW_FEATURE_NAMES = (
    "close_to_open_return",
    "high_to_low_log_range",
    "close_in_range",
    "log_volume_shares",
    "is_chinext",
    "is_star",
)
TOMORROW_SEQUENCE_FEATURE_NAMES = (
    *TOMORROW_FEATURE_NAMES,
    *(f"sequence_{step}_{channel}" for step in range(SEQUENCE_STEPS) for channel in SEQUENCE_CHANNELS),
)
SHANGHAI = ZoneInfo("Asia/Shanghai")


def IsTomorrowClosingQuote(quote: MarketQuote, trade_date: date, evaluated_at: datetime) -> bool:
    """Require a last-session quote observed at or after the market close."""

    if any(value.utcoffset() is None for value in (quote.source_time, quote.received_time, evaluated_at)):
        return False
    close = datetime.combine(trade_date, time(15), SHANGHAI)
    return (
        close <= quote.source_time <= quote.received_time <= evaluated_at
        and quote.source_time.astimezone(SHANGHAI).date() == trade_date
        and quote.received_time.astimezone(SHANGHAI).date() == trade_date
        and bool(quote.source)
        and bool(quote.data_version)
        and quote.price is not None
        and math.isfinite(quote.price)
        and quote.price > 0.0
    )


def BuildTomorrowFeatures(
    open_price: float | None,
    high: float | None,
    low: float | None,
    close: float | None,
    volume: float | None,
    board: Board,
) -> tuple[float, ...] | None:
    """Construct features only from the same session's genuine raw bar."""

    if (
        board not in {Board.MAIN, Board.CHINEXT, Board.STAR}
        or open_price is None or high is None or low is None or close is None or volume is None
        or any(not math.isfinite(value) or value <= 0 for value in (open_price, high, low, close, volume))
        or low > min(open_price, close) or high < max(open_price, close)
    ):
        return None
    features = (
        close / open_price - 1.0,
        math.log(high) - math.log(low),
        (close - low) / (high - low) if high > low else 0.5,
        math.log1p(volume),
        float(board is Board.CHINEXT),
        float(board is Board.STAR),
    )
    return features if all(math.isfinite(value) for value in features) else None


def BuildTomorrowSequenceFeatures(
    base: tuple[float, ...], sequence: ModelPriceVolumeSequence, *, signal_date: date,
) -> tuple[float, ...] | None:
    """Append the pre-signal sequence to the six same-day raw features."""
    if (
        len(base) != len(TOMORROW_FEATURE_NAMES)
        or any(not math.isfinite(value) for value in base)
        or sequence.dates[-1] >= signal_date
    ):
        return None
    return (*base, *sequence.values)


class TomorrowProbabilityPredictor(Protocol):
    @property
    def identity(self) -> str: ...

    def predict_probability(self, features: tuple[float, ...]) -> float: ...

    def predict_probabilities(self, rows: tuple[tuple[float, ...], ...]) -> tuple[float, ...]: ...
