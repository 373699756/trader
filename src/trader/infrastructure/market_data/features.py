"""Convert normalized quotes and cached history into domain feature snapshots."""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime

from trader.domain.factors import band_score, clamp, percentile_scores
from trader.domain.models import Evidence, FeatureSnapshot, MarketQuote
from trader.infrastructure.market_data.history import (
    DailyBar,
    maximum_drawdown_pct,
    median_amount,
    moving_average,
    return_pct,
    upward_consistency,
    volatility_pct,
)


class FeatureBuilder:
    def build(
        self,
        quotes: Sequence[MarketQuote],
        histories: Mapping[str, tuple[DailyBar, ...]],
        observed_at: datetime,
        *,
        cross_section_reference: Mapping[str, Mapping[str, float | None]] | None = None,
        research_evidence: Mapping[str, Sequence[Evidence]] | None = None,
    ) -> tuple[FeatureSnapshot, ...]:
        raw = {quote.code: self._raw_features(quote, histories.get(quote.code, ())) for quote in quotes}
        amount_percentiles = percentile_scores({code: values.get("amount_median_20d") for code, values in raw.items()})
        speed_percentiles = percentile_scores({quote.code: quote.speed for quote in quotes})
        strength_percentiles = {
            days: percentile_scores({code: values.get(f"return_{days}d") for code, values in raw.items()})
            for days in (3, 5, 10, 20)
        }
        volatility_scores = percentile_scores(
            {code: values.get("volatility_20d") for code, values in raw.items()},
            inverse=True,
        )
        drawdown_scores = percentile_scores({code: values.get("max_drawdown_20d") for code, values in raw.items()})
        industry_strength, industry_breadth = _industry_scores(quotes)
        market_breadth = 100.0 * sum((quote.pct_change or 0.0) > 0 for quote in quotes) / max(1, len(quotes))

        snapshots: list[FeatureSnapshot] = []
        for quote in quotes:
            values = raw[quote.code]
            values.update(
                {
                    "amount_percentile_20d": _if_present(
                        values.get("amount_median_20d"), amount_percentiles[quote.code]
                    ),
                    "speed_percentile": _if_present(quote.speed, speed_percentiles[quote.code]),
                    "relative_strength_3d": _if_present(values.get("return_3d"), strength_percentiles[3][quote.code]),
                    "relative_strength_5d": _if_present(values.get("return_5d"), strength_percentiles[5][quote.code]),
                    "relative_strength_10d": _if_present(
                        values.get("return_10d"), strength_percentiles[10][quote.code]
                    ),
                    "relative_strength_20d": _if_present(
                        values.get("return_20d"), strength_percentiles[20][quote.code]
                    ),
                    "industry_strength": industry_strength.get(quote.industry, 50.0),
                    "industry_breadth": industry_breadth.get(quote.industry, 50.0),
                    "industry_trend": industry_strength.get(quote.industry, 50.0),
                    "market_breadth": market_breadth,
                    "low_volatility_score": _if_present(values.get("volatility_20d"), volatility_scores[quote.code]),
                    "low_drawdown_score": _if_present(values.get("max_drawdown_20d"), drawdown_scores[quote.code]),
                }
            )
            reference = (cross_section_reference or {}).get(quote.code, {})
            for name in _CROSS_SECTION_FIELDS:
                if name in reference:
                    values[name] = reference[name]
            missing = tuple(sorted(name for name, value in values.items() if value is None))
            snapshots.append(
                FeatureSnapshot(
                    quote=quote,
                    values=values,
                    observed_at=observed_at,
                    history_days=len(histories.get(quote.code, ())),
                    market_regime=_market_regime(market_breadth),
                    missing_fields=missing,
                    evidence=(
                        _structured_evidence(quote, values, observed_at),
                        *tuple((research_evidence or {}).get(quote.code, ()))[:15],
                    ),
                )
            )
        return tuple(snapshots)

    def _raw_features(self, quote: MarketQuote, bars: tuple[DailyBar, ...]) -> dict[str, float | None]:
        returns = {days: return_pct(bars, days, quote.price) for days in (3, 5, 10, 20, 60)}
        ma5 = moving_average(bars, 5)
        ma20 = moving_average(bars, 20)
        ma60 = moving_average(bars, 60)
        volatility = volatility_pct(bars)
        drawdown = maximum_drawdown_pct(bars)
        amount_median = median_amount(bars)
        ma_position = _ma_position(quote.price, ma20, ma60)
        breakout = _breakout_score(quote.price, bars)
        slope = _slope_score(ma5, ma20)
        capacity = (
            None if quote.amount is None or amount_median is None else clamp(50.0 + 25.0 * quote.amount / amount_median)
        )
        limit = 20.0 if quote.code.startswith(("300", "301", "688", "689")) else 10.0
        limit_proximity = min(1.0, abs(quote.pct_change or 0.0) / limit)
        risk_adjusted = None
        if returns[20] is not None and volatility is not None and volatility > 0:
            risk_adjusted = clamp(50.0 + returns[20] / volatility * 5.0)
        close_location = _close_location(quote)
        trend_score = None if ma_position is None else clamp(0.6 * ma_position + 0.4 * (slope or 50.0))
        return {
            "amount_median_20d": amount_median,
            "return_3d": returns[3],
            "return_5d": returns[5],
            "return_10d": returns[10],
            "return_20d": returns[20],
            "return_60d": returns[60],
            "volatility_20d": volatility,
            "max_drawdown_20d": drawdown,
            "price_volume_confirmation": _price_volume_confirmation(returns[5], quote.amount, amount_median),
            "moderate_daily_return": band_score(quote.pct_change, -2.0, 0.5, 5.0, 8.0),
            "ma20_60_position": ma_position,
            "ma20_60_structure": ma_position,
            "ma_slope": slope,
            "breakout_20d": breakout,
            "risk_adjusted_return_20d": risk_adjusted,
            "upward_consistency": upward_consistency(bars),
            "capacity_score": capacity,
            "moderate_amplitude": band_score(quote.amplitude, 0.0, 1.0, 5.0, 12.0),
            "limit_distance_safety": 100.0 * (1.0 - limit_proximity),
            "tail_return_30m": None,
            "tail_volume_ratio": None,
            "close_location": close_location,
            "price_executability": band_score(quote.price, 1.0, 5.0, 100.0, 300.0),
            "ma20_deviation_inverse": _ma_deviation_inverse(quote.price, ma20),
            "return_20d_not_overheated": _not_overheated(returns[20]),
            "trend_score": trend_score,
            "low_crowding_score": 100.0 * (1.0 - limit_proximity),
            "limit_proximity": limit_proximity,
            "price_volume_divergence": 0.0,
            "financial_deterioration": 0.0,
            "reduction_or_unlock": 0.0,
            "pledge_risk": 0.0,
            "negative_announcement_level": 0.0,
            "news_sentiment": None,
            "evidence_freshness": None,
            "value_score": None,
            "growth_score": None,
            "quality_score": None,
            "industry_policy_score": None,
            "risk_protection_score": None,
        }


def _industry_scores(quotes: Sequence[MarketQuote]) -> tuple[dict[str, float], dict[str, float]]:
    changes: dict[str, list[float]] = defaultdict(list)
    for quote in quotes:
        if quote.industry and quote.pct_change is not None:
            changes[quote.industry].append(quote.pct_change)
    averages = {industry: sum(values) / len(values) for industry, values in changes.items() if values}
    strength_by_industry = percentile_scores(averages)
    breadth = {
        industry: 100.0 * sum(value > 0 for value in values) / len(values)
        for industry, values in changes.items()
        if values
    }
    return strength_by_industry, breadth


def _ma_position(price: float | None, ma20: float | None, ma60: float | None) -> float | None:
    if price is None or ma20 is None:
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
    if ma5 is None or ma20 is None or ma20 <= 0:
        return None
    return clamp(50.0 + (ma5 / ma20 - 1.0) * 1000.0)


def _breakout_score(price: float | None, bars: tuple[DailyBar, ...]) -> float | None:
    if price is None or len(bars) < 20:
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
    if return_5d is None or amount is None or median is None or median <= 0:
        return None
    volume_signal = clamp(amount / median * 50.0)
    return clamp(50.0 + math.copysign(volume_signal / 2.0, return_5d))


def _close_location(quote: MarketQuote) -> float | None:
    if quote.price is None or quote.high is None or quote.low is None or quote.high <= quote.low:
        return None
    return clamp((quote.price - quote.low) / (quote.high - quote.low) * 100.0)


def _ma_deviation_inverse(price: float | None, ma20: float | None) -> float | None:
    if price is None or ma20 is None or ma20 <= 0:
        return None
    deviation = abs(price / ma20 - 1.0) * 100.0
    return clamp(100.0 - deviation * 10.0)


def _not_overheated(return_20d: float | None) -> float | None:
    if return_20d is None:
        return None
    if return_20d <= 15.0:
        return 100.0
    if return_20d >= 30.0:
        return 0.0
    return 100.0 * (30.0 - return_20d) / 15.0


def _if_present(raw: float | None, score: float) -> float | None:
    return score if raw is not None and math.isfinite(raw) else None


def _market_regime(market_breadth: float) -> str:
    if market_breadth >= 60.0:
        return "risk_on"
    if market_breadth <= 40.0:
        return "risk_off"
    return "neutral"


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
                _relative_bucket(quote.price, 0.01),
                _absolute_bucket(quote.volume_ratio, 0.3),
                tuple(
                    sorted(
                        (name, None if value is None else round(float(value), 4))
                        for name, value in values.items()
                        if name not in _QUOTE_SENSITIVE_FEATURES
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
    )


def _relative_bucket(value: float | None, threshold: float) -> int | None:
    if value is None or value <= 0 or not math.isfinite(value):
        return None
    return math.floor(math.log(value) / math.log1p(threshold))


def _absolute_bucket(value: float | None, step: float) -> int | None:
    if value is None or not math.isfinite(value):
        return None
    return math.floor(value / step)


_QUOTE_SENSITIVE_FEATURES = frozenset(
    {
        "price_executability",
        "moderate_daily_return",
        "moderate_amplitude",
        "limit_distance_safety",
        "limit_proximity",
    }
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


__all__ = ["FeatureBuilder"]
