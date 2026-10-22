"""Pure completed-session V1 features shared by fitting and inference."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

LEARNED_RANK_HISTORY_SESSIONS = 251
LEARNED_RANK_FEATURE_IDS = (
    "completed_return_1d",
    "completed_return_3d",
    "completed_return_5d",
    "completed_return_20d",
    "completed_return_60d",
    "completed_return_120d",
    "completed_return_250d",
    "completed_volatility_20d",
    "completed_drawdown_60d",
    "completed_log_average_amount_20d",
)


@dataclass(frozen=True, slots=True)
class LearnedRankDailyPoint:
    trade_date: date
    close_price: float
    amount: float

    def __post_init__(self) -> None:
        if (
            type(self.trade_date) is not date
            or not math.isfinite(self.close_price)
            or self.close_price <= 0.0
            or not math.isfinite(self.amount)
            or self.amount <= 0.0
        ):
            raise ValueError("learned-rank daily point requires a complete positive price/amount pair")


def CalculateLearnedRankFeatures(points: Sequence[LearnedRankDailyPoint]) -> tuple[float, ...] | None:
    """Use exactly 251 complete, ascending sessions ending before the decision day.

    The caller verifies membership and continuity against its market calendar;
    this calculator never skips an invalid session or substitutes a live quote.
    """

    if len(points) != LEARNED_RANK_HISTORY_SESSIONS:
        return None
    if any(left.trade_date >= right.trade_date for left, right in zip(points, points[1:], strict=False)):
        return None
    close = points[-1].close_price
    returns = tuple(close / points[-horizon - 1].close_price - 1.0 for horizon in (1, 3, 5, 20, 60, 120, 250))
    daily = tuple(points[index].close_price / points[index - 1].close_price - 1.0 for index in range(231, 251))
    mean = math.fsum(daily) / len(daily)
    volatility = math.sqrt(math.fsum((value - mean) ** 2 for value in daily) / len(daily))
    drawdown = close / max(point.close_price for point in points[-60:]) - 1.0
    log_amount = math.log(math.fsum(point.amount for point in points[-20:]) / 20.0)
    result = (*returns, volatility, drawdown, log_amount)
    return result if all(math.isfinite(value) for value in result) else None


__all__ = [
    "CalculateLearnedRankFeatures",
    "LEARNED_RANK_FEATURE_IDS",
    "LEARNED_RANK_HISTORY_SESSIONS",
    "LearnedRankDailyPoint",
]
