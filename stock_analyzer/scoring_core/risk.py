from __future__ import annotations

import math
from typing import Dict

import pandas as pd

from .. import config
from ..normalization import coerce_number


__all__ = [
    "_penalty_value",
    "_set_positive_penalty",
    "_smooth_penalty",
    "_sum_penalty",
    "_swing_risk_penalty",
    "_swing_risk_penalty_parts",
    "_tomorrow_risk_penalty",
    "_tomorrow_risk_penalty_parts",
]


def _row_speed(row: pd.Series) -> float:
    speed = coerce_number(row.get("speed"))
    if speed != 0:
        return speed
    return coerce_number(row.get("five_min_pct"))


def _close_location(price: float, high: float, low: float) -> float:
    price = coerce_number(price)
    high = coerce_number(high)
    low = coerce_number(low)
    if price <= 0 or high <= low or low <= 0:
        return 0.5
    return max(0.0, min(1.0, (price - low) / (high - low)))


def _sum_penalty(parts: Dict[str, float]) -> float:
    return round(sum(max(0.0, coerce_number(value)) for value in parts.values()), 2)


def _smooth_penalty(
    value: float,
    threshold: float,
    max_penalty: float,
    steepness: float = 2.0,
    direction: str = "above",
) -> float:
    if direction == "below":
        z = steepness * (threshold - value)
    else:
        z = steepness * (value - threshold)
    z = max(-50.0, min(50.0, z))
    return round(max(0.0, coerce_number(max_penalty)) / (1.0 + math.exp(-z)), 4)


def _penalty_value(
    value: float,
    threshold: float,
    max_penalty: float,
    steepness: float,
    direction: str = "above",
    min_effective: float = 0.25,
) -> float:
    penalty = _smooth_penalty(value, threshold, max_penalty, steepness, direction=direction)
    return round(penalty, 2) if penalty >= min_effective else 0.0


def _set_positive_penalty(parts: Dict[str, float], key: str, value: float) -> None:
    penalty = coerce_number(value)
    if penalty > 0:
        parts[key] = round(max(coerce_number(parts.get(key)), penalty), 2)


def _tomorrow_risk_penalty(row: pd.Series) -> float:
    return _sum_penalty(_tomorrow_risk_penalty_parts(row))


def _tomorrow_risk_penalty_parts(row: pd.Series, provisional: bool = False) -> Dict[str, float]:
    pct = coerce_number(row.get("pct_chg"))
    market = row.get("market")
    upper = config.MAX_BUYABLE_GAIN_GROWTH if market in ("chinext", "star") else config.MAX_BUYABLE_GAIN_MAIN
    amplitude = coerce_number(row.get("amplitude"))
    turnover_rate = coerce_number(row.get("turnover_rate"))
    volume_ratio = coerce_number(row.get("volume_ratio"))
    price = coerce_number(row.get("price"))
    high = coerce_number(row.get("high"))
    low = coerce_number(row.get("low"))
    open_price = coerce_number(row.get("open"))
    speed = _row_speed(row)
    parts = {}
    if bool(getattr(config, "USE_SMOOTH_PENALTY", True)):
        _set_positive_penalty(parts, "intraday_chase", _penalty_value(pct, upper * 0.78, 12, 3.0))
        _set_positive_penalty(parts, "amplitude", _penalty_value(amplitude, 10.0, 10, 1.5))
        _set_positive_penalty(parts, "turnover_rate", _penalty_value(turnover_rate, 16.0, 9, 1.0))
        _set_positive_penalty(parts, "volume_ratio", _penalty_value(volume_ratio, 4.5, 10, 2.0))
    else:
        if pct >= upper * 0.83:
            parts["intraday_chase"] = 12
        elif pct >= upper * 0.72:
            parts["intraday_chase"] = 8
        if amplitude >= 11:
            parts["amplitude"] = 10
        elif amplitude >= 9.0:
            parts["amplitude"] = 4
        if turnover_rate >= 18:
            parts["turnover_rate"] = 9
        elif turnover_rate >= 14:
            parts["turnover_rate"] = 3
        if volume_ratio >= 5:
            parts["volume_ratio"] = 10
        elif volume_ratio >= 4:
            parts["volume_ratio"] = 5

    has_close_range = price > 0 and high > low and low > 0
    close_location = _close_location(price, high, low)
    mid_gain_min = coerce_number(getattr(config, "TOMORROW_MID_GAIN_MIN_PCT", 4.5), 4.5)
    mid_gain_max = coerce_number(getattr(config, "TOMORROW_MID_GAIN_MAX_PCT", 7.0), 7.0)
    weak_close_line = coerce_number(getattr(config, "TOMORROW_MID_GAIN_WEAK_CLOSE_LOCATION", 0.6), 0.6)
    if not provisional and has_close_range and mid_gain_min <= pct < mid_gain_max and close_location < weak_close_line:
        if bool(getattr(config, "USE_SMOOTH_PENALTY", True)):
            _set_positive_penalty(
                parts,
                "mid_gain_weak_close",
                _penalty_value(
                    close_location,
                    weak_close_line,
                    coerce_number(getattr(config, "TOMORROW_MID_GAIN_WEAK_CLOSE_PENALTY", 7.0), 7.0),
                    3.0,
                    direction="below",
                ),
            )
        else:
            parts["mid_gain_weak_close"] = coerce_number(
                getattr(config, "TOMORROW_MID_GAIN_WEAK_CLOSE_PENALTY", 7.0),
                7.0,
            )
    if not provisional and has_close_range and close_location < 0.35:
        if bool(getattr(config, "USE_SMOOTH_PENALTY", True)):
            _set_positive_penalty(
                parts,
                "weak_tail_close",
                _penalty_value(close_location, 0.40, 10, 4.0, direction="below"),
            )
        else:
            parts["weak_tail_close"] = 8
    if open_price > 0 and price > 0:
        gain = (price / open_price - 1.0) * 100.0
        if gain > 6.0:
            parts["intraday_exhaustion"] = 8
        elif gain < -1.0:
            parts["intraday_exhaustion"] = 6

    if volume_ratio < 1.05:
        parts["weak_volume_ratio"] = 4
    if bool(getattr(config, "USE_SMOOTH_PENALTY", True)):
        _set_positive_penalty(parts, "late_chase_speed", _penalty_value(speed, 2.1, 7, 2.0))
        _set_positive_penalty(parts, "late_fade", _penalty_value(speed, -2.1, 7, 2.0, direction="below"))
    else:
        if speed > 3.0:
            parts["late_chase_speed"] = 6
        elif speed < -1.2:
            parts["late_fade"] = 7
    if not provisional and has_close_range and close_location < 0.45:
        if bool(getattr(config, "USE_SMOOTH_PENALTY", True)):
            _set_positive_penalty(
                parts,
                "weak_tail_close",
                _penalty_value(close_location, 0.40, 10, 4.0, direction="below"),
            )
        else:
            parts["weak_tail_close"] = max(parts.get("weak_tail_close", 0), 10)
    if coerce_number(row.get("alphalite_factor_ready")) > 0:
        ret_20d = coerce_number(row.get("ret_20d"))
        ma20_gap = coerce_number(row.get("ma20_gap"))
        volatility_20d = coerce_number(row.get("volatility_20d"))
        if ret_20d < -12:
            parts["history_downtrend"] = 8
        elif ret_20d < -6:
            parts["history_downtrend"] = 4
        if ma20_gap < -6:
            parts["ma20_break"] = 6
        if volatility_20d > 8:
            parts["history_volatility"] = 7
        elif volatility_20d > 6:
            parts["history_volatility"] = 3
    return parts


def _swing_risk_penalty(row: pd.Series) -> float:
    return _sum_penalty(_swing_risk_penalty_parts(row))


def _swing_risk_penalty_parts(row: pd.Series) -> Dict[str, float]:
    pct = coerce_number(row.get("pct_chg"))
    volume_ratio = coerce_number(row.get("volume_ratio"))
    turnover_rate = coerce_number(row.get("turnover_rate"))
    volatility_20d = coerce_number(row.get("volatility_20d"))
    ma5_gap = coerce_number(row.get("ma5_gap"))
    parts = {}
    if bool(getattr(config, "USE_SMOOTH_PENALTY", True)):
        _set_positive_penalty(parts, "intraday_chase", _penalty_value(pct, 7.0, 6, 2.0))
        _set_positive_penalty(parts, "volume_ratio", _penalty_value(volume_ratio, 5.5, 7, 2.0))
        _set_positive_penalty(parts, "turnover_rate", _penalty_value(turnover_rate, 18.0, 6, 1.0))
        _set_positive_penalty(parts, "volatility", _penalty_value(volatility_20d, 7.0, 7, 1.4))
        _set_positive_penalty(parts, "ma5_gap", _penalty_value(ma5_gap, 18.0, 5, 0.45))
    else:
        if pct > 7:
            parts["intraday_chase"] = 6
        if volume_ratio > 5.5:
            parts["volume_ratio"] = 7
        if turnover_rate > 18:
            parts["turnover_rate"] = 6
        if volatility_20d > 7:
            parts["volatility"] = 7
        if ma5_gap > 18:
            parts["ma5_gap"] = 5
    return parts
