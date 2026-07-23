"""Pure recommendation entry-shape and downside protection."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from trader.domain.market.models import (
    FeatureSnapshot,
    MarketQuote,
)
from trader.domain.recommendation.models import Strategy


@dataclass(frozen=True)
class EntrySetup:
    setup_type: Literal["shrink_pullback", "volume_breakout", "trend_unconfirmed", "none"]
    score: float | None


@dataclass(frozen=True)
class DownsideAssessment:
    status: Literal["pass", "observe"]
    reasons: tuple[str, ...]
    atr20_pct: float | None
    intraday_reversal_atr: float | None
    historical_drawdown_pct: float | None
    setup_type: str


_REQUIRED_DOWNSIDE_FIELDS = (
    "atr20_pct",
    "volatility_20d",
    "max_drawdown_20d",
    "low_volatility_score",
    "low_drawdown_score",
)


def derive_entry_setup(snapshot: FeatureSnapshot) -> EntrySetup:
    return derive_entry_setup_values(snapshot.quote, snapshot.values)


def derive_entry_setup_values(quote: MarketQuote, values: Mapping[str, float | None]) -> EntrySetup:
    ma5 = _finite(values.get("ma5"))
    ma10 = _finite(values.get("ma10"))
    ma20 = _finite(values.get("ma20"))
    slope = _finite(values.get("ma20_slope_pct"))
    price = _finite(quote.price)
    volume_ratio = _finite(values.get("volume_to_5d_average"))
    prior_high = _finite(values.get("prior_high_20d"))
    breakout_deviation = _finite(values.get("breakout_deviation_pct"))
    close_location = _finite(values.get("close_location"))
    industry_breadth = _finite(values.get("industry_breadth"))
    required = (
        ma5,
        ma10,
        ma20,
        slope,
        price,
        volume_ratio,
        prior_high,
        breakout_deviation,
        close_location,
    )
    if any(value is None for value in required):
        return EntrySetup("none", None)
    assert ma5 is not None and ma10 is not None and ma20 is not None
    assert slope is not None and price is not None and volume_ratio is not None
    assert prior_high is not None and breakout_deviation is not None
    assert close_location is not None
    trend_ok = ma5 >= ma10 >= ma20 and slope > 0.0 and price >= ma20
    if not trend_ok:
        return EntrySetup("none", 0.0)
    near_support = abs(price / ma5 - 1.0) <= 0.01 or abs(price / ma10 - 1.0) <= 0.02
    if near_support and volume_ratio <= 0.70:
        return EntrySetup("shrink_pullback", 100.0)
    breakout = (
        price >= prior_high
        and volume_ratio >= 2.0
        and close_location >= 70.0
        and 0.0 <= breakout_deviation <= 5.0
        and industry_breadth is not None
        and industry_breadth >= 60.0
    )
    if breakout:
        return EntrySetup("volume_breakout", 100.0)
    return EntrySetup("trend_unconfirmed", 50.0)


def assess_downside(snapshot: FeatureSnapshot, strategy: Strategy) -> DownsideAssessment:
    if strategy is Strategy.LONG:
        return DownsideAssessment("pass", (), None, None, None, "none")
    setup = derive_entry_setup(snapshot)
    missing = any(snapshot.optional_value(field) is None for field in _REQUIRED_DOWNSIDE_FIELDS)
    atr = snapshot.optional_value("atr20_pct")
    historical_drawdown = snapshot.optional_value("max_drawdown_20d")
    intraday_reversal_atr = _intraday_reversal_atr(snapshot, atr)
    if missing:
        return DownsideAssessment(
            "observe",
            ("downside_inputs_missing",),
            atr,
            intraday_reversal_atr,
            historical_drawdown,
            setup.setup_type,
        )

    reasons: list[str] = []
    if intraday_reversal_atr is not None and intraday_reversal_atr >= 1.0 and snapshot.value("close_location") <= 35.0:
        reasons.append("intraday_reversal_atr")
    if snapshot.value("trend_breakdown", 0.0) >= 0.5:
        reasons.append("trend_breakdown")
    if snapshot.value("low_volatility_score") <= 20.0 and snapshot.value("low_drawdown_score") <= 20.0:
        reasons.append("low_stability_tail")
    if _risk_off_weak_close(snapshot, strategy):
        reasons.append("risk_off_weak_close")
    return DownsideAssessment(
        "observe" if reasons else "pass",
        tuple(reasons),
        atr,
        intraday_reversal_atr,
        historical_drawdown,
        setup.setup_type,
    )


def _intraday_reversal_atr(snapshot: FeatureSnapshot, atr: float | None) -> float | None:
    high = _finite(snapshot.quote.high)
    price = _finite(snapshot.quote.price)
    if high is None or price is None or high <= 0.0 or price <= 0.0 or atr is None or atr <= 0.0:
        return None
    return max(0.0, (high - price) / high * 100.0 / atr)


def _risk_off_weak_close(snapshot: FeatureSnapshot, strategy: Strategy) -> bool:
    if snapshot.market_regime != "risk_off" or snapshot.value("market_breadth") > 30.0:
        return False
    close_is_weak = snapshot.value("close_location") <= 50.0
    period_return = (
        snapshot.quote.change_5m if strategy is Strategy.TODAY else snapshot.optional_value("tail_return_30m_pct")
    )
    return close_is_weak or (period_return is not None and math.isfinite(period_return) and period_return < 0.0)


def _finite(value: float | int | str | None) -> float | None:
    try:
        parsed = float(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed is not None and math.isfinite(parsed) else None


__all__ = [
    "DownsideAssessment",
    "EntrySetup",
    "assess_downside",
    "derive_entry_setup",
    "derive_entry_setup_values",
]
