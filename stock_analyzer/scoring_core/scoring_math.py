from __future__ import annotations

import os
from typing import Dict, List, Tuple

import pandas as pd

from .. import config
from ..factor_ic import load_factor_ic
from ..normalization import (
    SortedNumericValues,
    coerce_number,
    finite_series,
    sorted_numeric_values,
    percentile_score,
)
from . import horizon
from .weights import COMPONENT_FACTOR_KEYS, STRATEGY_COMBINERS, THRESHOLDS, WEIGHTS


_FACTOR_IC_CACHE = {"path": None, "mtime_ns": None, "payload": {}}

# These values are computed from the local historical panel.  When the panel
# is not ready, zero-filled placeholders must not participate in a cross-stock
# percentile distribution.
_HISTORICAL_CONTEXT_COLUMNS = frozenset(
    {
        "sixty_day_pct",
        "ytd_pct",
        "ret_3d",
        "ret_5d",
        "ret_10d",
        "ret_20d",
        "ma5_gap",
        "ma20_gap",
        "ma10_gap",
        "ma60_gap",
        "vol_ma5_ratio",
        "vol_amount_5d",
        "breakout_20d",
        "volatility_20d",
    }
)


__all__ = [
    "_close_location",
    "_combine",
    "_combine_details",
    "_combined_speed",
    "_composite_score",
    "_execution_score",
    "_factor_ic_multiplier",
    "_factor_ic_payload",
    "_has_signal",
    "_historical_factors_ready",
    "_horizon_meta",
    "_horizon_row",
    "_hot_rank_score",
    "_market_regime_adjustment",
    "_not_overextended_score",
    "_optional_factor_score",
    "_overheat_damp_multiplier",
    "_apply_overheat_damp",
    "_regime_component",
    "_regime_component_from_profile",
    "_regime_weight",
    "_regime_weight_profile",
    "_row_speed",
    "_safe_corr",
    "_score_context",
    "_stddev",
    "_tail_close_setup_score",
    "_weighted_score",
]


def _score_context(
    df: pd.DataFrame, industry_strength: Dict[str, float]
) -> Dict[str, object]:
    ready_mask = None
    if "alphalite_factor_ready" in df.columns:
        ready_mask = finite_series(df, "alphalite_factor_ready") > 0

    def context_values(column: str) -> SortedNumericValues:
        source = df
        if ready_mask is not None and column in _HISTORICAL_CONTEXT_COLUMNS:
            # A ranking needs at least two real observations.  With fewer,
            # return an empty distribution so optional factors stay neutral.
            source = df.loc[ready_mask] if int(ready_mask.sum()) >= 2 else df.iloc[0:0]
        return sorted_numeric_values(finite_series(source, column).tolist())

    return {
        "pct_values": context_values("pct_chg"),
        "speed_values": sorted_numeric_values(_combined_speed(df).tolist()),
        "volume_ratio_values": context_values("volume_ratio"),
        "turnover_rate_values": context_values("turnover_rate"),
        "turnover_values": context_values("turnover"),
        "sixty_day_values": context_values("sixty_day_pct"),
        "ytd_values": context_values("ytd_pct"),
        "amplitude_values": context_values("amplitude"),
        "ret_3d_values": context_values("ret_3d"),
        "ret_5d_values": context_values("ret_5d"),
        "ret_10d_values": context_values("ret_10d"),
        "ret_20d_values": context_values("ret_20d"),
        "ma5_gap_values": context_values("ma5_gap"),
        "ma20_gap_values": context_values("ma20_gap"),
        "ma10_gap_values": context_values("ma10_gap"),
        "ma60_gap_values": context_values("ma60_gap"),
        "vol_ma5_ratio_values": context_values("vol_ma5_ratio"),
        "vol_amount_5d_values": context_values("vol_amount_5d"),
        "breakout_20d_values": context_values("breakout_20d"),
        "volatility_20d_values": context_values("volatility_20d"),
        "float_market_cap_values": context_values("float_market_cap"),
        "pe_dynamic_values": context_values("pe_dynamic"),
        "pb_values": context_values("pb"),
        "industry_values": sorted_numeric_values(industry_strength.values()),
        "factor_ic_payload": (
            _factor_ic_payload()
            if getattr(config, "ENABLE_FACTOR_IC_WEIGHTING", False)
            else {}
        ),
    }


def _stddev(values: List[float]) -> float:
    nums = [coerce_number(v) for v in values if pd.notna(coerce_number(v))]
    if len(nums) < 2:
        return 0.0
    mean = sum(nums) / len(nums)
    variance = sum((v - mean) ** 2 for v in nums) / len(nums)
    return variance ** 0.5


def _safe_corr(left: List[float], right: List[float]) -> float:
    size = min(len(left), len(right))
    if size < 2:
        return 0.0
    a = pd.Series(left[:size], dtype="float64")
    b = pd.Series(right[:size], dtype="float64")
    if a.std() <= 1e-12 or b.std() <= 1e-12:
        return 0.0
    value = a.corr(b)
    return round(coerce_number(value), 4)


def _combined_speed(df: pd.DataFrame) -> pd.Series:
    speed = finite_series(df, "speed")
    five_min = finite_series(df, "five_min_pct")
    return speed.where(speed != 0, five_min)


def _row_speed(row: pd.Series) -> float:
    speed = coerce_number(row.get("speed"))
    if speed != 0:
        return speed
    return coerce_number(row.get("five_min_pct"))


def _tail_close_setup_score(row: pd.Series) -> float:
    pct = coerce_number(row.get("pct_chg"))
    price = coerce_number(row.get("price"))
    open_price = coerce_number(row.get("open"))
    high = coerce_number(row.get("high"))
    low = coerce_number(row.get("low"))
    amplitude = coerce_number(row.get("amplitude"))
    volume_ratio = coerce_number(row.get("volume_ratio"))
    turnover_rate = coerce_number(row.get("turnover_rate"))
    speed = _row_speed(row)
    market = row.get("market")
    upper = config.MAX_BUYABLE_GAIN_GROWTH if market in ("chinext", "star") else config.MAX_BUYABLE_GAIN_MAIN

    score = 52.0
    if 1.1 <= pct <= min(upper * 0.78, 5.5):
        score += 20
    elif 0.6 <= pct < 1.1:
        score += 10
    elif 0.4 <= pct < 0.6:
        score += 2
    elif pct > upper * 0.86:
        score -= 22
    elif pct <= 0:
        score -= 20

    if 1.1 <= volume_ratio <= 3.2:
        score += 16
    elif 3.2 < volume_ratio <= 4.5:
        score += 6
    elif 0.8 <= volume_ratio < 1.1:
        score -= 4
    elif volume_ratio > 4.5:
        score -= 14

    if 2.0 <= turnover_rate <= 10.0:
        score += 9
    elif 10.0 < turnover_rate <= 15.0:
        score += 4
    elif turnover_rate > 15.0:
        score -= 10

    close_location = _close_location(price, high, low)
    if close_location >= 0.72:
        score += 16
    elif close_location >= 0.60:
        score += 6
    elif close_location >= 0.52:
        score += 2
    elif close_location < 0.30:
        score -= 28
    elif close_location < 0.45:
        score -= 16

    if open_price > 0 and price > 0:
        intraday_gain = (price / open_price - 1.0) * 100.0
        if 0.3 <= intraday_gain <= 4.8:
            score += 10
        elif intraday_gain < 0:
            score -= 10
        elif intraday_gain > 6.0:
            score -= 14

    if 0 < amplitude <= 6.8:
        score += 10
    elif amplitude <= 9.0:
        score += 4
    elif amplitude >= 11.0:
        score -= 12

    if 0 <= speed <= 1.6:
        score += 8
    elif 1.6 < speed <= 2.4:
        score += 2
    elif -1.2 <= speed < 0:
        score -= 4
    elif speed > 2.4:
        score -= 10
    elif speed < -1.2:
        score -= 7

    return max(0.0, min(100.0, score))


def _close_location(price: float, high: float, low: float) -> float:
    price = coerce_number(price)
    high = coerce_number(high)
    low = coerce_number(low)
    if price <= 0 or high <= low or low <= 0:
        return 0.5
    return max(0.0, min(1.0, (price - low) / (high - low)))


def _hot_rank_score(rank) -> float:
    if not rank:
        return 50.0
    rank = int(rank)
    if rank <= 20:
        return 100.0
    if rank <= 50:
        return 88.0
    if rank <= 100:
        return 76.0
    if rank <= 200:
        return 62.0
    return 52.0


def _optional_factor_score(
    value,
    values: List[float],
    higher_is_better: bool = True,
    fallback=None,
    fallback_values: List[float] = None,
    available: bool = True,
) -> float:
    if not available:
        return 50.0
    if _has_signal(values):
        return percentile_score(value, values, higher_is_better=higher_is_better)
    if fallback is not None and fallback_values is not None:
        return percentile_score(fallback, fallback_values, higher_is_better=higher_is_better)
    return 50.0


def _has_signal(values: List[float]) -> bool:
    return any(abs(coerce_number(value)) > 1e-9 for value in values)


def _historical_factors_ready(row) -> bool:
    if "alphalite_factor_ready" not in row:
        return True
    return coerce_number(row.get("alphalite_factor_ready")) > 0


def _composite_score(parts: List[float]) -> float:
    clean = [max(0.0, min(100.0, coerce_number(value))) for value in parts if pd.notna(coerce_number(value))]
    if not clean:
        return 50.0
    return sum(clean) / len(clean)


def _weighted_score(pairs: Tuple[Tuple[object, float], ...], fallback: object = 50.0) -> float:
    total = 0.0
    weight_total = 0.0
    for value, weight in pairs:
        if value is None:
            continue
        num = coerce_number(value)
        if not pd.notna(num):
            continue
        total += max(0.0, min(100.0, num)) * weight
        weight_total += weight
    if weight_total <= 0:
        return max(0.0, min(100.0, coerce_number(fallback, 50.0)))
    return max(0.0, min(100.0, total / weight_total))


def _market_regime_adjustment(
    row: pd.Series,
    market_regime: Dict[str, object],
    strategy_style: str,
) -> float:
    if not market_regime:
        return 0.0

    level = market_regime.get("level")
    pct = coerce_number(row.get("pct_chg"))
    sixty_day_pct = coerce_number(row.get("sixty_day_pct"))
    amplitude = coerce_number(row.get("amplitude"))
    volume_ratio = coerce_number(row.get("volume_ratio"))
    turnover = coerce_number(row.get("turnover"))
    bonus = 0.0

    if level == "risk_on":
        if strategy_style in ("short", "tomorrow", "swing", "tech"):
            if pct > 0:
                bonus += 1.8
            if 1.1 <= volume_ratio <= 4.5:
                bonus += 1.6
            if turnover >= config.MIN_TURNOVER * 4:
                bonus += 1.2
        if strategy_style in ("long", "position") and sixty_day_pct >= 0:
            bonus += 0.8
        if amplitude > 11:
            bonus -= 1.5
    elif level == "risk_off":
        if strategy_style in ("short", "tomorrow", "tech"):
            if pct > 4:
                bonus -= 4.5
            if volume_ratio > 4.5:
                bonus -= 2.5
            if amplitude > 9:
                bonus -= 2.5
        if strategy_style in ("long", "position"):
            if 0 <= sixty_day_pct <= 40:
                bonus += 2.4
            if amplitude <= 7:
                bonus += 1.6
            if turnover >= config.MIN_TURNOVER * 3:
                bonus += 1.0
        if sixty_day_pct < -12:
            bonus -= 2.5
    else:
        if strategy_style in ("short", "tomorrow", "swing") and 1.0 <= volume_ratio <= 3.5:
            bonus += 0.8
        if amplitude > 12:
            bonus -= 1.2

    return round(bonus, 2)


def _near_limit_up_risk(row: pd.Series) -> bool:
    pct = coerce_number(row.get("pct_chg"))
    market = row.get("market")
    limit = 20 if market in ("chinext", "star") else 10
    turnover = coerce_number(row.get("turnover"))
    return pct >= limit * 0.88 and turnover < config.MIN_TURNOVER * 2


def _regime_weight(key: str, market_regime: Dict[str, object], default: float = 1.0) -> float:
    if not bool(getattr(config, "ENABLE_REGIME_SPECIFIC_WEIGHTS", False)):
        return 1.0
    if not market_regime:
        return default
    level = market_regime.get("level") or "balanced"
    profiles = WEIGHTS.get("regime_profiles") or {}
    profile = profiles.get(level) or profiles.get("balanced") or {}
    value = coerce_number(profile.get(key), default)
    return max(0.5, min(1.5, value))


def _regime_weight_profile(market_regime: Dict[str, object], keys: List[str]) -> Dict[str, float]:
    return {key: round(_regime_weight(key, market_regime), 3) for key in keys}


def _regime_component(score: float, key: str, market_regime: Dict[str, object]) -> float:
    value = max(0.0, min(100.0, coerce_number(score, 50.0)))
    weight = _regime_weight(key, market_regime)
    return max(0.0, min(100.0, 50.0 + (value - 50.0) * weight))


def _regime_component_from_profile(score: float, key: str, profile: Dict[str, object]) -> float:
    value = max(0.0, min(100.0, coerce_number(score, 50.0)))
    weight = coerce_number((profile or {}).get(key), 1.0)
    weight = max(0.5, min(1.5, weight))
    return max(0.0, min(100.0, 50.0 + (value - 50.0) * weight))


def _combine(
    components: Dict[str, object],
    strategy: str,
    weights: Dict[str, object] = None,
    market_regime: Dict[str, object] = None,
    row: pd.Series = None,
    regime_weight_profile: Dict[str, object] = None,
    factor_ic_payload: Dict[str, object] = None,
) -> float:
    return _combine_details(
        components,
        strategy,
        weights=weights,
        market_regime=market_regime,
        row=row,
        regime_weight_profile=regime_weight_profile,
        factor_ic_payload=factor_ic_payload,
    )["score"]


def _combine_details(
    components: Dict[str, object],
    strategy: str,
    weights: Dict[str, object] = None,
    market_regime: Dict[str, object] = None,
    row: pd.Series = None,
    regime_weight_profile: Dict[str, object] = None,
    factor_ic_payload: Dict[str, object] = None,
) -> Dict[str, float]:
    spec = STRATEGY_COMBINERS.get(strategy)
    if not spec:
        raise KeyError("unknown strategy combiner: {}".format(strategy))
    all_weights = weights or WEIGHTS
    strategy_weights = all_weights.get(strategy, {})
    base = 0.0
    term_total = 0.0
    weighted_terms = []
    resolved_factor_ic_payload = factor_ic_payload
    if resolved_factor_ic_payload is None and getattr(
        config,
        "ENABLE_FACTOR_IC_WEIGHTING",
        False,
    ):
        resolved_factor_ic_payload = _factor_ic_payload()
    for term in spec["terms"]:
        key = term["component"]
        weight_key = term["weight_key"]
        weight = coerce_number(strategy_weights.get(weight_key), 0.0)
        if weight <= 0:
            continue
        value = coerce_number(components.get(key), 50.0)
        regime_key = term.get("regime_key")
        if regime_key:
            if regime_weight_profile:
                value = _regime_component_from_profile(value, regime_key, regime_weight_profile)
            else:
                value = _regime_component(value, regime_key, market_regime)
        adjusted_weight = weight * _factor_ic_multiplier(
            key,
            payload=resolved_factor_ic_payload,
        )
        weighted_terms.append((value, weight, adjusted_weight))
        term_total += weight

    adjusted_total = sum(item[2] for item in weighted_terms)
    scale = (term_total / adjusted_total) if adjusted_total > 1e-12 else 1.0
    for value, _, adjusted_weight in weighted_terms:
        base += value * adjusted_weight * scale
    if term_total <= 0:
        base = 0.0

    risk_penalty = coerce_number(components.get("risk_penalty"), 0.0)
    regime_bonus = coerce_number(components.get("regime_bonus"), 0.0)
    raw_score = base - risk_penalty + regime_bonus
    if spec.get("apply_damp"):
        if "overheat_damp" in components:
            damp = coerce_number(components.get("overheat_damp"), 1.0)
        elif row is not None:
            damp = _overheat_damp_multiplier(row)
        else:
            damp = 1.0
        damp = max(0.0, min(1.0, damp))
    else:
        damp = 1.0
    score = max(0.0, min(100.0, raw_score * damp))
    return {
        "score": score,
        "base_score": base,
        "raw_score": raw_score,
        "risk_penalty": risk_penalty,
        "regime_bonus": regime_bonus,
        "overheat_damp": damp,
    }


def _factor_ic_multiplier(
    component: str,
    payload: Dict[str, object] = None,
) -> float:
    if not getattr(config, "ENABLE_FACTOR_IC_WEIGHTING", False):
        return 1.0
    factor_key = COMPONENT_FACTOR_KEYS.get(component)
    if not factor_key:
        return 1.0
    if payload is None:
        payload = _factor_ic_payload()
    info = ((payload or {}).get("ic") or {}).get(factor_key) or {}
    if info.get("status") != "ok":
        return 1.0
    if int(info.get("sample_count") or 0) < int(getattr(config, "FACTOR_IC_MIN_SAMPLES", 30)):
        return 1.0
    band = max(0.0, min(0.8, coerce_number(getattr(config, "FACTOR_IC_WEIGHT_BAND", 0.3), 0.3)))
    ic = max(-1.0, min(1.0, coerce_number(info.get("ic"))))
    return max(0.1, 1.0 + max(-band, min(band, ic * band)))


def _factor_ic_payload() -> Dict[str, object]:
    path = os.path.realpath(str(getattr(config, "FACTOR_IC_PATH", ".runtime/factor_ic.json")))
    try:
        mtime_ns = os.stat(path).st_mtime_ns
    except Exception:
        _FACTOR_IC_CACHE["path"] = path
        _FACTOR_IC_CACHE["mtime_ns"] = None
        _FACTOR_IC_CACHE["payload"] = {}
        return {}
    if _FACTOR_IC_CACHE.get("path") != path or _FACTOR_IC_CACHE.get("mtime_ns") != mtime_ns:
        _FACTOR_IC_CACHE["path"] = path
        _FACTOR_IC_CACHE["mtime_ns"] = mtime_ns
        _FACTOR_IC_CACHE["payload"] = load_factor_ic()
    return _FACTOR_IC_CACHE.get("payload") or {}


def _execution_score(row: pd.Series) -> float:
    pct = coerce_number(row.get("pct_chg"))
    market = row.get("market")
    upper = config.MAX_BUYABLE_GAIN_GROWTH if market in ("chinext", "star") else config.MAX_BUYABLE_GAIN_MAIN
    if pct <= 0:
        return 45.0
    if pct <= upper * 0.55:
        return 88.0
    if pct <= upper * 0.78:
        return 76.0
    return 58.0


def _not_overextended_score(row: pd.Series) -> float:
    sixty_day_pct = coerce_number(row.get("sixty_day_pct"))
    ytd_pct = coerce_number(row.get("ytd_pct"))
    amplitude = coerce_number(row.get("amplitude"))
    score = 86.0
    if sixty_day_pct > 45:
        score -= min(30.0, (sixty_day_pct - 45) * 0.8)
    if ytd_pct > 80:
        score -= min(35.0, (ytd_pct - 80) * 0.6)
    if amplitude > 10:
        score -= 8
    if sixty_day_pct < -20:
        score -= 16
    return max(0.0, min(100.0, score))


def _overheat_damp_multiplier(row: pd.Series) -> float:
    not_overextended = _not_overextended_score(row) / 100.0
    floor = coerce_number(THRESHOLDS.get("overheat_damp_floor"), 0.6)
    return floor + (1.0 - floor) * max(0.0, min(1.0, not_overextended))


def _apply_overheat_damp(final_score: float, row: pd.Series) -> float:
    return final_score * _overheat_damp_multiplier(row)


def _horizon_meta(
    top_n: int,
    market_filter: str,
    candidate_count: int,
    strategy_version: str,
    strategy_label: str,
) -> Dict[str, object]:
    return horizon._horizon_meta(top_n, market_filter, candidate_count, strategy_version, strategy_label)


def _horizon_row(row: pd.Series, scores: Dict[str, object]) -> Dict[str, object]:
    return horizon._horizon_row(row, scores)
