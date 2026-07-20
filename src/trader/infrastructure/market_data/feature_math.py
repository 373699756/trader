"""Pure feature math and structured evidence helpers."""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime

from trader.domain.factors import band_score, clamp, percentile_scores_with_metadata
from trader.domain.fusion import STRUCTURED_REVIEW_FEATURES
from trader.domain.models import CrossSectionStats, Evidence, MarketQuote
from trader.infrastructure.market_data.history import (
    DailyBar,
)


def _industry_scores(
    quotes: Sequence[MarketQuote], data_version: str
) -> tuple[dict[str, float], CrossSectionStats, dict[str, float]]:
    changes: dict[str, list[float]] = defaultdict(list)
    for quote in quotes:
        if quote.industry and quote.pct_change is not None and math.isfinite(quote.pct_change):
            changes[quote.industry].append(quote.pct_change)
    averages = {industry: sum(values) / len(values) for industry, values in changes.items() if values}
    strength_by_industry, stats = percentile_scores_with_metadata(averages, population_data_version=data_version)
    breadth = {
        industry: 100.0 * sum(value > 0 for value in values) / len(values)
        for industry, values in changes.items()
        if values
    }
    return strength_by_industry, stats, breadth


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
    return clamp(50.0 + (ma5 / ma20 - 1.0) * 1000.0)


def _breakout_score(price: float | None, bars: tuple[DailyBar, ...]) -> float | None:
    if not _all_finite(price) or price is None or len(bars) < 20:
        return None
    high = max(bar.high for bar in bars[-20:])
    if high <= 0:
        return None
    return clamp((price / high - 0.90) * 1000.0)


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
    volume_signal = clamp(amount / median * 50.0)
    if return_5d == 0.0:
        return 50.0
    direction = 1.0 if return_5d > 0.0 else -1.0
    return clamp(50.0 + direction * volume_signal / 2.0)


def _close_location(quote: MarketQuote) -> float | None:
    if (
        not _all_finite(quote.price, quote.high, quote.low)
        or quote.price is None
        or quote.high is None
        or quote.low is None
        or quote.high <= quote.low
    ):
        return None
    return clamp((quote.price - quote.low) / (quote.high - quote.low) * 100.0)


def _ma_deviation_inverse(price: float | None, ma20: float | None) -> float | None:
    if not _all_finite(price, ma20) or price is None or ma20 is None or ma20 <= 0:
        return None
    deviation = abs(price / ma20 - 1.0) * 100.0
    return clamp(100.0 - deviation * 10.0)


def _if_present(raw: float | None, score: float) -> float | None:
    return score if raw is not None and math.isfinite(raw) else None


def _optional_band_score(
    value: float | None, lower: float, optimal_low: float, optimal_high: float, upper: float
) -> float | None:
    return band_score(value, lower, optimal_low, optimal_high, upper) if _all_finite(value) else None


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
    available = {
        name: round(value, 4) for name, raw in values.items() if raw is not None and math.isfinite(value := float(raw))
    }
    summary = (
        f"结构化点时数据：价格={quote.price}，涨幅={quote.pct_change}，量比={quote.volume_ratio}，"
        f"换手={quote.turnover_rate}，可用历史因子={len(available)}项"
    )
    signature = hashlib.sha256(
        repr(
            (
                quote.code,
                tuple(
                    sorted(
                        (name, None if value is None else round(float(value), 4))
                        for name, value in values.items()
                        if name in STRUCTURED_REVIEW_FEATURES
                    )
                ),
            )
        ).encode("utf-8")
    ).hexdigest()[:32]
    return Evidence(
        evidence_id=f"structured:{quote.code}:{signature}",
        evidence_type="structured_point_in_time",
        title=summary[:240],
        source=quote.source,
        published_at=observed_at,
        received_at=observed_at,
        data_version=quote.data_version,
    )


_CROSS_SECTION_FIELDS = frozenset(
    {
        "amount_percentile_20d",
        "speed_percentile",
        "relative_strength_3d",
        "relative_strength_5d",
        "relative_strength_10d",
        "relative_strength_20d",
        "industry_strength",
        "industry_breadth",
        "industry_trend",
        "market_breadth",
        "low_volatility_score",
        "low_drawdown_score",
    }
)
