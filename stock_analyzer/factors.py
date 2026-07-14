from typing import Dict, Iterable, Optional

import pandas as pd

from . import config
from .normalization import coerce_number, normalize_code, rename_known_columns


ALPHALITE_COLUMNS = (
    "ret_3d",
    "ret_5d",
    "ret_10d",
    "ret_20d",
    "ma5_gap",
    "ma10_gap",
    "ma20_gap",
    "ma60_gap",
    "ma_bull_aligned",
    "vol_amount_5d",
    "vol_ma5_ratio",
    "turnover_20d",
    "breakout_20d",
    "volatility_20d",
    "close_vs_vwap",
    "upper_wick_ratio",
    "lower_wick_ratio",
    "price_position_20d",
    "consecutive_up_days",
    "consecutive_down_days",
    "amplitude_5d_mean",
)

ALPHALITE_META_COLUMNS = (
    "alphalite_factor_ready",
    "alphalite_coverage",
)


def build_alphalite_factors(history_by_code: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows = []
    for code, history in history_by_code.items():
        factors = compute_alphalite_for_stock(code, history)
        if factors:
            rows.append(factors)
    if not rows:
        return pd.DataFrame(columns=("code",) + ALPHALITE_COLUMNS + ALPHALITE_META_COLUMNS)
    return pd.DataFrame(rows)


def compute_alphalite_for_stock(code: str, history: pd.DataFrame) -> Dict[str, float]:
    if history is None or history.empty:
        return {}
    df = _prepare_alphalite_frame(code, history)
    if df is None:
        return {}
    if "price" in df.columns and df["price"].abs().sum() > 0:
        close = df["price"]
    else:
        close = df["close"]
    open_price = df["open"] if "open" in df.columns and df["open"].abs().sum() > 0 else close
    high = df["high"] if "high" in df.columns else close
    low = df["low"] if "low" in df.columns and df["low"].abs().sum() > 0 else close
    turnover = df["turnover"] if "turnover" in df.columns else pd.Series([0.0] * len(df))
    volume = df["volume"] if "volume" in df.columns else turnover
    if len(close) < 6 or close.iloc[-1] <= 0:
        return {}

    latest = close.iloc[-1]
    avg_amount_5 = turnover.tail(5).mean()
    prev_avg_amount_5 = turnover.tail(10).head(5).mean() if len(turnover) >= 10 else avg_amount_5
    high_20 = high.tail(20).max() if len(high) >= 20 else high.max()
    returns = close.pct_change().dropna()
    volatility_20 = returns.tail(20).std() * 100 if len(returns) else 0.0

    # 均线多头排列：MA5 > MA10 > MA20 > MA60（需 >=60 根）。
    ma5 = close.tail(5).mean() if len(close) >= 5 else latest
    ma10 = close.tail(10).mean() if len(close) >= 10 else ma5
    ma20 = close.tail(20).mean() if len(close) >= 20 else ma10
    ma60 = close.tail(60).mean() if len(close) >= 60 else ma20
    ma_bull = 1.0 if (len(close) >= 60 and ma5 > ma10 > ma20 > ma60) else 0.0
    # 量能突破：最新成交量 / 5 日均量。
    avg_vol_5 = volume.tail(5).mean()
    latest_vol = volume.iloc[-1]
    vol_ma5_ratio = round(latest_vol / avg_vol_5, 4) if avg_vol_5 > 0 else 0.0
    availability = {
        "ret_3d": _period_available(close, 3),
        "ret_5d": _period_available(close, 5),
        "ret_10d": _period_available(close, 10),
        "ret_20d": _period_available(close, 20),
        "ma5_gap": _ma_available(close, 5),
        "ma20_gap": _ma_available(close, 20),
        "vol_amount_5d": len(turnover) >= 10 and prev_avg_amount_5 > 0,
        "breakout_20d": len(high) >= 20 and high_20 > 0,
        "volatility_20d": len(returns) >= 20,
    }
    coverage = sum(1 for value in availability.values() if value) / len(availability)
    enhanced = _enhanced_factors(open_price, high, low, close) if getattr(config, "ENABLE_ENHANCED_FACTORS", False) else _empty_enhanced_factors()

    return {
        "code": normalize_code(code),
        "ret_3d": _period_return(close, 3),
        "ret_5d": _period_return(close, 5),
        "ret_10d": _period_return(close, 10),
        "ret_20d": _period_return(close, 20),
        "ma5_gap": _ma_gap(close, 5),
        "ma10_gap": _ma_gap(close, 10),
        "ma20_gap": _ma_gap(close, 20),
        "ma60_gap": _ma_gap(close, 60),
        "ma_bull_aligned": ma_bull,
        "vol_amount_5d": _ratio(avg_amount_5, prev_avg_amount_5),
        "vol_ma5_ratio": vol_ma5_ratio,
        "turnover_20d": round(coerce_number(turnover.tail(20).mean()), 4),
        "breakout_20d": 1.0 if high_20 > 0 and latest >= high_20 * 0.995 else 0.0,
        "volatility_20d": round(coerce_number(volatility_20), 4),
        **enhanced,
        "alphalite_factor_ready": 1.0,
        "alphalite_coverage": round(coverage, 4),
    }


def compute_alphalite_panel(
    code: str,
    history: pd.DataFrame,
    indices: Iterable[int],
    lookback_window: int = 25,
) -> Dict[int, Dict[str, float]]:
    """Compute AlphaLite factors for many signal dates from one normalized frame.

    Rolling Series are built once per stock.  Windows that are too short or use a
    different source-column fallback use the reference single-window calculator,
    preserving its missing-data semantics.
    """
    df = _prepare_alphalite_frame(code, history)
    if df is None or df.empty:
        return {}
    target_indices = sorted(
        {
            int(index)
            for index in (indices or [])
            if isinstance(index, (int, float)) and 0 <= int(index) < len(df)
        }
    )
    if not target_indices:
        return {}
    window = max(25, int(lookback_window or 25))
    price = df["price"]
    close_column = df["close"]
    price_mode = bool(price.abs().sum() > 0)
    close = price if price_mode else close_column
    open_column = df["open"]
    low_column = df["low"]
    open_mode = bool(open_column.abs().sum() > 0)
    low_mode = bool(low_column.abs().sum() > 0)
    turnover = df["turnover"]
    volume = df["volume"]

    rolling_5 = close.rolling(5, min_periods=1).mean()
    rolling_10 = close.rolling(10, min_periods=1).mean()
    rolling_20 = close.rolling(20, min_periods=1).mean()
    rolling_60 = close.rolling(60, min_periods=1).mean()
    turnover_5 = turnover.rolling(5, min_periods=1).mean()
    previous_turnover_5 = turnover.shift(5).rolling(5, min_periods=5).mean()
    high_20 = df["high"].rolling(20, min_periods=1).max()
    volume_5 = volume.rolling(5, min_periods=1).mean()
    turnover_20 = turnover.rolling(20, min_periods=1).mean()
    returns = close.pct_change()
    volatility_20 = returns.rolling(20, min_periods=20).std() * 100
    enhanced_enabled = bool(getattr(config, "ENABLE_ENHANCED_FACTORS", False))
    result: Dict[int, Dict[str, float]] = {}

    for index in target_indices:
        end = index + 1
        start = max(0, end - window)
        window_length = end - start
        if window_length < window:
            reference = compute_alphalite_for_stock(
                code,
                df.iloc[start:end],
            )
            if reference:
                result[index] = reference
            continue
        window_price_mode = bool(price.iloc[start:end].abs().sum() > 0)
        window_open_mode = bool(open_column.iloc[start:end].abs().sum() > 0)
        window_low_mode = bool(low_column.iloc[start:end].abs().sum() > 0)
        if (
            window_price_mode != price_mode
            or window_open_mode != open_mode
            or window_low_mode != low_mode
        ):
            reference = compute_alphalite_for_stock(code, df.iloc[start:end])
            if reference:
                result[index] = reference
            continue
        latest = coerce_number(close.iloc[index])
        if latest <= 0:
            continue

        def period_return(days: int) -> float:
            base_index = index - days
            if base_index < start:
                return 0.0
            base = coerce_number(close.iloc[base_index])
            return round((latest / base - 1) * 100, 4) if base > 0 else 0.0

        def moving_average_gap(days: int) -> float:
            if window_length < days:
                return 0.0
            average = coerce_number(
                (rolling_5 if days == 5 else rolling_10 if days == 10 else rolling_20 if days == 20 else rolling_60).iloc[index]
            )
            return round((latest / average - 1) * 100, 4) if average > 0 else 0.0

        current_turnover_5 = coerce_number(turnover_5.iloc[index])
        previous = coerce_number(previous_turnover_5.iloc[index])
        if window_length < 10:
            previous = current_turnover_5
        high_value = coerce_number(high_20.iloc[index])
        volatility = coerce_number(volatility_20.iloc[index])
        average_volume = coerce_number(volume_5.iloc[index])
        volume_ratio = round(coerce_number(volume.iloc[index]) / average_volume, 4) if average_volume > 0 else 0.0
        availability = {
            "ret_3d": index - 3 >= start and coerce_number(close.iloc[index - 3]) > 0,
            "ret_5d": index - 5 >= start and coerce_number(close.iloc[index - 5]) > 0,
            "ret_10d": index - 10 >= start and coerce_number(close.iloc[index - 10]) > 0,
            "ret_20d": index - 20 >= start and coerce_number(close.iloc[index - 20]) > 0,
            "ma5_gap": window_length >= 5 and coerce_number(rolling_5.iloc[index]) > 0,
            "ma20_gap": window_length >= 20 and coerce_number(rolling_20.iloc[index]) > 0,
            "vol_amount_5d": window_length >= 10 and previous > 0,
            "breakout_20d": window_length >= 20 and high_value > 0,
            # ``pct_change().dropna()`` counts infinite returns in the
            # reference implementation, even when the resulting std is NaN.
            "volatility_20d": window_length >= 21,
        }
        coverage = round(
            sum(1 for value in availability.values() if value) / len(availability),
            4,
        )
        if enhanced_enabled:
            open_values = open_column.iloc[start:end] if open_mode else close.iloc[start:end]
            low_values = low_column.iloc[start:end] if low_mode else close.iloc[start:end]
            enhanced = _enhanced_factors(
                open_values,
                df["high"].iloc[start:end],
                low_values,
                close.iloc[start:end],
            )
        else:
            enhanced = _empty_enhanced_factors()
        result[index] = {
            "code": normalize_code(code),
            "ret_3d": period_return(3),
            "ret_5d": period_return(5),
            "ret_10d": period_return(10),
            "ret_20d": period_return(20),
            "ma5_gap": moving_average_gap(5),
            "ma10_gap": moving_average_gap(10),
            "ma20_gap": moving_average_gap(20),
            "ma60_gap": moving_average_gap(60),
            "ma_bull_aligned": 1.0
            if window_length >= 60
            and rolling_5.iloc[index] > rolling_10.iloc[index] > rolling_20.iloc[index] > rolling_60.iloc[index]
            else 0.0,
            "vol_amount_5d": _ratio(current_turnover_5, previous),
            "vol_ma5_ratio": volume_ratio,
            "turnover_20d": round(coerce_number(turnover_20.iloc[index]), 4),
            "breakout_20d": 1.0 if high_value > 0 and latest >= high_value * 0.995 else 0.0,
            "volatility_20d": round(volatility, 4),
            **enhanced,
            "alphalite_factor_ready": 1.0,
            "alphalite_coverage": coverage,
        }
    return result


def _prepare_alphalite_frame(code: str, history: pd.DataFrame) -> Optional[pd.DataFrame]:
    if history is None or history.empty:
        return None
    df = rename_known_columns(history.copy())
    if df.columns.duplicated().any():
        # Prefer the first canonical column when a source supplies both an
        # alias (for example ``close``) and its canonical counterpart.
        df = df.loc[:, ~df.columns.duplicated(keep="first")]
    if "code" not in df.columns:
        df["code"] = code
    df["code"] = df["code"].map(normalize_code)
    for column in ("price", "close", "open", "high", "low", "turnover", "volume"):
        if column not in df.columns:
            df[column] = 0.0
        df[column] = df[column].map(coerce_number)
    return df


def merge_alphalite(candidates: pd.DataFrame, factors: pd.DataFrame) -> pd.DataFrame:
    df = candidates.copy()
    if factors is not None and not factors.empty:
        df = df.merge(factors, on="code", how="left")
    for column in ALPHALITE_COLUMNS:
        if column not in df.columns:
            df[column] = 0.0
        df[column] = df[column].map(coerce_number)
    for column in ALPHALITE_META_COLUMNS:
        if column not in df.columns:
            df[column] = 0.0
        df[column] = df[column].map(coerce_number)
    return df


def _period_available(close: pd.Series, days: int) -> bool:
    return len(close) > days and close.iloc[-days - 1] > 0


def _ma_available(close: pd.Series, window: int) -> bool:
    return len(close) >= window and close.tail(window).mean() > 0


def _period_return(close: pd.Series, days: int) -> float:
    if len(close) <= days:
        return 0.0
    base = close.iloc[-days - 1]
    latest = close.iloc[-1]
    if base <= 0:
        return 0.0
    return round((latest / base - 1) * 100, 4)


def _ma_gap(close: pd.Series, window: int) -> float:
    if len(close) < window:
        return 0.0
    ma = close.tail(window).mean()
    if ma <= 0:
        return 0.0
    return round((close.iloc[-1] / ma - 1) * 100, 4)


def _ratio(value: float, base: float) -> float:
    if base <= 0:
        return 0.0
    return round(value / base, 4)


def _enhanced_factors(
    open_price: pd.Series,
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
) -> Dict[str, float]:
    latest_close = coerce_number(close.iloc[-1]) if len(close) else 0.0
    latest_high = coerce_number(high.iloc[-1]) if len(high) else latest_close
    latest_low = coerce_number(low.iloc[-1]) if len(low) else latest_close
    latest_open = coerce_number(open_price.iloc[-1]) if len(open_price) else latest_close
    typical = (latest_high + latest_low + latest_close) / 3.0
    close_vs_vwap = (latest_close / typical - 1.0) * 100 if typical > 0 else 0.0
    daily_range = latest_high - latest_low
    if daily_range > 0:
        upper_wick_ratio = (latest_high - max(latest_open, latest_close)) / daily_range
        lower_wick_ratio = (min(latest_open, latest_close) - latest_low) / daily_range
    else:
        upper_wick_ratio = 0.0
        lower_wick_ratio = 0.0
    high_20 = high.tail(20).max() if len(high) >= 20 else high.max()
    low_20 = low.tail(20).min() if len(low) >= 20 else low.min()
    price_position_20d = (latest_close - low_20) / (high_20 - low_20) * 100 if high_20 > low_20 else 50.0
    streak = _consecutive_direction(close)
    return {
        "close_vs_vwap": round(coerce_number(close_vs_vwap), 4),
        "upper_wick_ratio": round(max(0.0, min(1.0, coerce_number(upper_wick_ratio))), 4),
        "lower_wick_ratio": round(max(0.0, min(1.0, coerce_number(lower_wick_ratio))), 4),
        "price_position_20d": round(max(0.0, min(100.0, coerce_number(price_position_20d))), 4),
        "consecutive_up_days": float(streak.get("up", 0)),
        "consecutive_down_days": float(streak.get("down", 0)),
        "amplitude_5d_mean": round(_amplitude_mean(high, low, close, 5), 4),
    }


def _empty_enhanced_factors() -> Dict[str, float]:
    return {
        "close_vs_vwap": 0.0,
        "upper_wick_ratio": 0.0,
        "lower_wick_ratio": 0.0,
        "price_position_20d": 0.0,
        "consecutive_up_days": 0.0,
        "consecutive_down_days": 0.0,
        "amplitude_5d_mean": 0.0,
    }


def _consecutive_direction(close: pd.Series) -> Dict[str, int]:
    values = [coerce_number(value) for value in close.tolist()]
    up = 0
    down = 0
    for idx in range(len(values) - 1, 0, -1):
        if values[idx] > values[idx - 1]:
            if down:
                break
            up += 1
            continue
        if values[idx] < values[idx - 1]:
            if up:
                break
            down += 1
            continue
        break
    return {"up": up, "down": down}


def _amplitude_mean(high: pd.Series, low: pd.Series, close: pd.Series, window: int) -> float:
    if len(high) <= 1 or len(low) <= 1 or len(close) <= 1:
        return 0.0
    values = []
    start = max(1, len(close) - max(1, int(window)))
    for idx in range(start, len(close)):
        prev_close = coerce_number(close.iloc[idx - 1])
        if prev_close <= 0:
            continue
        values.append((coerce_number(high.iloc[idx]) - coerce_number(low.iloc[idx])) / prev_close * 100.0)
    return sum(values) / len(values) if values else 0.0
