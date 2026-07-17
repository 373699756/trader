"""Convert normalized quotes and cached history into domain feature snapshots."""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime

from trader.domain.factors import band_score, clamp, percentile_scores_with_metadata
from trader.domain.models import CrossSectionStats, Evidence, FeatureSnapshot, MarketQuote
from trader.domain.news import NewsSignalPolicy, derive_news_signals
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
    def __init__(self, news_signal_policy: NewsSignalPolicy) -> None:
        self._news_signal_policy = news_signal_policy

    def build(
        self,
        quotes: Sequence[MarketQuote],
        histories: Mapping[str, tuple[DailyBar, ...]],
        observed_at: datetime,
        *,
        cross_section_reference: Mapping[str, Mapping[str, float | None]] | None = None,
        cross_section_normalization_reference: Mapping[str, Mapping[str, CrossSectionStats]] | None = None,
        research_evidence: Mapping[str, Sequence[Evidence]] | None = None,
    ) -> tuple[FeatureSnapshot, ...]:
        grouped: dict[str, list[MarketQuote]] = defaultdict(list)
        for quote in quotes:
            grouped[quote.data_version].append(quote)
        built: dict[str, FeatureSnapshot] = {}
        for data_version, group_quotes in grouped.items():
            for snapshot in self._build_group(
                tuple(group_quotes),
                histories,
                observed_at,
                data_version=data_version,
                cross_section_reference=cross_section_reference,
                cross_section_normalization_reference=cross_section_normalization_reference,
                research_evidence=research_evidence,
            ):
                built[snapshot.quote.code] = snapshot
        return tuple(built[quote.code] for quote in quotes)

    def _build_group(
        self,
        quotes: Sequence[MarketQuote],
        histories: Mapping[str, tuple[DailyBar, ...]],
        observed_at: datetime,
        *,
        data_version: str,
        cross_section_reference: Mapping[str, Mapping[str, float | None]] | None,
        cross_section_normalization_reference: Mapping[str, Mapping[str, CrossSectionStats]] | None,
        research_evidence: Mapping[str, Sequence[Evidence]] | None,
    ) -> tuple[FeatureSnapshot, ...]:
        raw = {quote.code: self._raw_features(quote, histories.get(quote.code, ())) for quote in quotes}
        amount_percentiles, amount_stats = percentile_scores_with_metadata(
            {code: values.get("amount_median_20d") for code, values in raw.items()},
            population_data_version=data_version,
        )
        speed_percentiles, speed_stats = percentile_scores_with_metadata(
            {quote.code: quote.speed for quote in quotes}, population_data_version=data_version
        )
        strength_results = {
            days: percentile_scores_with_metadata(
                {code: values.get(f"return_{days}d") for code, values in raw.items()},
                population_data_version=data_version,
            )
            for days in (3, 5, 10, 20)
        }
        volatility_scores, volatility_stats = percentile_scores_with_metadata(
            {code: values.get("volatility_20d") for code, values in raw.items()},
            inverse=True,
            population_data_version=data_version,
        )
        drawdown_scores, drawdown_stats = percentile_scores_with_metadata(
            {code: values.get("max_drawdown_20d") for code, values in raw.items()},
            population_data_version=data_version,
        )
        industry_strength, industry_strength_stats, industry_breadth = _industry_scores(quotes, data_version)
        valid_changes = [
            quote.pct_change for quote in quotes if quote.pct_change is not None and math.isfinite(quote.pct_change)
        ]
        market_breadth = (
            100.0 * sum(value > 0 for value in valid_changes) / len(valid_changes) if valid_changes else None
        )
        market_breadth_stats = CrossSectionStats(
            None,
            None,
            len(valid_changes),
            len(quotes) - len(valid_changes),
            0.025,
            0.975,
            data_version,
        )
        shared_normalization = {
            "amount_percentile_20d": amount_stats,
            "speed_percentile": speed_stats,
            "relative_strength_3d": strength_results[3][1],
            "relative_strength_5d": strength_results[5][1],
            "relative_strength_10d": strength_results[10][1],
            "relative_strength_20d": strength_results[20][1],
            "industry_strength": industry_strength_stats,
            "market_breadth": market_breadth_stats,
            "low_volatility_score": volatility_stats,
            "low_drawdown_score": drawdown_stats,
        }

        snapshots: list[FeatureSnapshot] = []
        for quote in quotes:
            values = raw[quote.code]
            candidate_evidence = tuple((research_evidence or {}).get(quote.code, ()))[:15]
            news_signals = derive_news_signals(
                candidate_evidence,
                observed_at=observed_at,
                policy=self._news_signal_policy,
            )
            values.update(
                {
                    "amount_percentile_20d": _if_present(
                        values.get("amount_median_20d"), amount_percentiles[quote.code]
                    ),
                    "speed_percentile": _if_present(quote.speed, speed_percentiles[quote.code]),
                    "relative_strength_3d": _if_present(values.get("return_3d"), strength_results[3][0][quote.code]),
                    "relative_strength_5d": _if_present(values.get("return_5d"), strength_results[5][0][quote.code]),
                    "relative_strength_10d": _if_present(values.get("return_10d"), strength_results[10][0][quote.code]),
                    "relative_strength_20d": _if_present(values.get("return_20d"), strength_results[20][0][quote.code]),
                    "industry_strength": industry_strength.get(quote.industry),
                    "industry_breadth": industry_breadth.get(quote.industry),
                    "industry_trend": industry_strength.get(quote.industry),
                    "market_breadth": market_breadth,
                    "low_volatility_score": _if_present(values.get("volatility_20d"), volatility_scores[quote.code]),
                    "low_drawdown_score": _if_present(values.get("max_drawdown_20d"), drawdown_scores[quote.code]),
                    "news_sentiment": news_signals.sentiment_score,
                    "evidence_freshness": news_signals.freshness_score,
                }
            )
            reference = (cross_section_reference or {}).get(quote.code, {})
            for name in _CROSS_SECTION_FIELDS:
                if name in reference:
                    values[name] = reference[name]
            normalization = dict(shared_normalization)
            normalization.update((cross_section_normalization_reference or {}).get(quote.code, {}))
            missing = tuple(
                sorted(
                    {
                        *(name for name, value in values.items() if value is None),
                        *_missing_quote_fields(quote),
                    }
                )
            )
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
                        *candidate_evidence,
                    ),
                    normalization=normalization,
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
            None
            if quote.amount is None
            or not math.isfinite(quote.amount)
            or amount_median is None
            or not math.isfinite(amount_median)
            or amount_median <= 0
            else clamp(50.0 + 25.0 * quote.amount / amount_median)
        )
        limit = 20.0 if quote.code.startswith(("300", "301", "688", "689")) else 10.0
        limit_proximity = (
            min(1.0, abs(quote.pct_change) / limit)
            if quote.pct_change is not None and math.isfinite(quote.pct_change)
            else None
        )
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
            "moderate_daily_return": _optional_band_score(quote.pct_change, -2.0, 0.5, 5.0, 8.0),
            "ma20_60_position": ma_position,
            "ma20_60_structure": ma_position,
            "ma_slope": slope,
            "breakout_20d": breakout,
            "risk_adjusted_return_20d": risk_adjusted,
            "upward_consistency": upward_consistency(bars),
            "capacity_score": capacity,
            "moderate_amplitude": _optional_band_score(quote.amplitude, 0.0, 1.0, 5.0, 12.0),
            "limit_distance_safety": None if limit_proximity is None else 100.0 * (1.0 - limit_proximity),
            "tail_return_30m": None,
            "tail_volume_ratio": None,
            "close_location": close_location,
            "price_executability": _optional_band_score(quote.price, 1.0, 5.0, 100.0, 300.0),
            "ma20_deviation_inverse": _ma_deviation_inverse(quote.price, ma20),
            "return_20d_not_overheated": _not_overheated(returns[20]),
            "trend_score": trend_score,
            "low_crowding_score": None if limit_proximity is None else 100.0 * (1.0 - limit_proximity),
            "limit_proximity": limit_proximity,
            "price_volume_divergence": None,
            "financial_deterioration": None,
            "reduction_or_unlock": None,
            "pledge_risk": None,
            "negative_announcement_level": None,
            "news_sentiment": None,
            "evidence_freshness": None,
            "value_score": None,
            "growth_score": None,
            "quality_score": None,
            "industry_policy_score": None,
            "risk_protection_score": None,
        }


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
    return clamp(50.0 + math.copysign(volume_signal / 2.0, return_5d))


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


def _not_overheated(return_20d: float | None) -> float | None:
    if not _all_finite(return_20d) or return_20d is None:
        return None
    if return_20d <= 15.0:
        return 100.0
    if return_20d >= 30.0:
        return 0.0
    return 100.0 * (30.0 - return_20d) / 15.0


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


def _market_regime(market_breadth: float | None) -> str:
    if market_breadth is None:
        return "neutral"
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
