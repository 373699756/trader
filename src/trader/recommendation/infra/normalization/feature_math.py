"""Pure feature math and structured evidence functions."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime

from trader.recommendation.domain.market.factors import BandScore, Clamp
from trader.recommendation.domain.market.models import (
    Evidence,
    MarketQuote,
)


def _ma_position(price: float | None, ma20: float | None, ma60: float | None) -> float | None:
    if not _all_finite(price, ma20) or price is None or ma20 is None:
        return None
    if ma60 is None:
        return 70.0 if price >= ma20 else 30.0
    if price >= ma20 >= ma60:
        return 100.0
    if price >= ma20:
        return 75.0
    if price >= ma60:
        return 45.0
    return 15.0


def _slope_score(ma5: float | None, ma20: float | None) -> float | None:
    if not _all_finite(ma5, ma20) or ma5 is None or ma20 is None or ma20 <= 0:
        return None
    return Clamp(50.0 + (ma5 / ma20 - 1.0) * 1000.0)


def _breakout_score(price: float | None, high: float | None) -> float | None:
    if not _all_finite(price) or price is None or high is None:
        return None
    if high <= 0:
        return None
    return Clamp((price / high - 0.90) * 1000.0)


def _price_volume_confirmation(
    return_5d: float | None,
    amount: float | None,
    median: float | None,
) -> float | None:
    if (
        not _all_finite(return_5d, amount, median)
        or return_5d is None
        or amount is None
        or median is None
        or median <= 0
    ):
        return None
    volume_signal = Clamp(amount / median * 50.0)
    if return_5d == 0.0:
        return 50.0
    direction = 1.0 if return_5d > 0.0 else -1.0
    return Clamp(50.0 + direction * volume_signal / 2.0)


def _close_location(price: float | None, high: float | None, low: float | None) -> float | None:
    if not _all_finite(price, high, low) or price is None or high is None or low is None or high <= low:
        return None
    return Clamp((price - low) / (high - low) * 100.0)


def _ma_deviation_inverse(price: float | None, ma20: float | None) -> float | None:
    if not _all_finite(price, ma20) or price is None or ma20 is None or ma20 <= 0:
        return None
    deviation = abs(price / ma20 - 1.0) * 100.0
    return Clamp(100.0 - deviation * 10.0)


def _optional_band_score(
    value: float | None, lower: float, optimal_low: float, optimal_high: float, upper: float
) -> float | None:
    return BandScore(value, lower, optimal_low, optimal_high, upper) if _all_finite(value) else None


def _all_finite(*values: float | None) -> bool:
    return all(value is not None and math.isfinite(value) for value in values)


def _missing_quote_fields(quote: MarketQuote) -> tuple[str, ...]:
    return tuple(
        name
        for name in (
            "price",
            "previous_close",
            "open_price",
            "high",
            "low",
            "pct_change",
            "change_5m",
            "speed",
            "volume_ratio",
            "turnover_rate",
            "amount",
            "volume",
            "amplitude",
            "market_cap",
        )
        if not _all_finite(getattr(quote, name))
    )


def _structured_evidence(
    quote: MarketQuote,
    values: Mapping[str, float | None],
    observed_at: datetime,
) -> Evidence:
    available_count = sum(raw is not None and math.isfinite(float(raw)) for raw in values.values())
    summary = (
        f"结构化点时数据：价格={quote.price}，涨幅={quote.pct_change}，量比={quote.volume_ratio}，"
        f"换手={quote.turnover_rate}，可用历史因子={available_count}项"
    )
    return Evidence(
        evidence_id=f"structured:{quote.code}:{observed_at.date().isoformat()}",
        evidence_type="structured_point_in_time",
        title=summary[:240],
        source=quote.source,
        published_at=observed_at,
        received_at=observed_at,
        data_version=quote.data_version,
    )
