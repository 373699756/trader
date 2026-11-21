"""Project one published history point to the shared V5 sequence contract."""

from typing import cast

from trader.download.domain.history_revision import HistoryTrainingPoint
from trader.recommendation.domain.market.v5.features import PriceVolumePoint


def ProjectSequencePoint(point: HistoryTrainingPoint) -> PriceVolumePoint | None:
    values = (
        point.qfq_open, point.qfq_high, point.qfq_low,
        point.qfq_close_price, point.raw_volume, point.qfq_amount,
    )
    if any(value is None for value in values):
        return None
    try:
        return PriceVolumePoint(point.trade_date, *(cast(float, value) for value in values))
    except ValueError:
        return None
