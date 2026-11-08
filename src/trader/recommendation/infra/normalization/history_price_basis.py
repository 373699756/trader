"""Project frozen adjusted history onto one verified raw quote coordinate."""

from __future__ import annotations

import math
from dataclasses import replace

from trader.recommendation.domain.market.history_metrics import DailyBar, HistoryContext, HistoryProfile


def RebaseHistoryPrices(
    bars: tuple[DailyBar, ...], context: HistoryContext | None, scale: float
) -> tuple[tuple[DailyBar, ...], HistoryContext | None]:
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError("history_price_scale_invalid")
    if scale == 1.0:
        return bars, context
    projected = tuple(replace(
        bar, open_price=bar.open_price * scale, close=bar.close * scale,
        high=bar.high * scale, low=bar.low * scale,
    ) for bar in bars)
    return projected, None if context is None else replace(
        context,
        profile=_profile(context.profile, scale),
        previous_profile=_profile(context.previous_profile, scale) if context.previous_profile is not None else None,
        return_anchors=tuple((offset, price * scale) for offset, price in context.return_anchors),
    )


def _profile(profile: HistoryProfile, scale: float) -> HistoryProfile:
    return replace(
        profile,
        moving_average_5d=_price(profile.moving_average_5d, scale),
        moving_average_10d=_price(profile.moving_average_10d, scale),
        moving_average_20d=_price(profile.moving_average_20d, scale),
        moving_average_60d=_price(profile.moving_average_60d, scale),
        high_20d=_price(profile.high_20d, scale),
    )


def _price(value: float | None, scale: float) -> float | None:
    return None if value is None else value * scale
