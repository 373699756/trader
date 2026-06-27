from typing import Dict

import pandas as pd

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
)


def build_alphalite_factors(history_by_code: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows = []
    for code, history in history_by_code.items():
        factors = compute_alphalite_for_stock(code, history)
        if factors:
            rows.append(factors)
    if not rows:
        return pd.DataFrame(columns=("code",) + ALPHALITE_COLUMNS)
    return pd.DataFrame(rows)


def compute_alphalite_for_stock(code: str, history: pd.DataFrame) -> Dict[str, float]:
    if history is None or history.empty:
        return {}
    df = rename_known_columns(history.copy())
    if "code" not in df.columns:
        df["code"] = code
    df["code"] = df["code"].map(normalize_code)
    for column in ("price", "close", "high", "low", "turnover", "volume"):
        if column not in df.columns:
            df[column] = 0.0
        df[column] = df[column].map(coerce_number)
    if "price" in df.columns and df["price"].abs().sum() > 0:
        close = df["price"]
    else:
        close = df["close"]
    high = df["high"] if "high" in df.columns else close
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
    }


def merge_alphalite(candidates: pd.DataFrame, factors: pd.DataFrame) -> pd.DataFrame:
    df = candidates.copy()
    if factors is not None and not factors.empty:
        df = df.merge(factors, on="code", how="left")
    for column in ALPHALITE_COLUMNS:
        if column not in df.columns:
            df[column] = 0.0
        df[column] = df[column].map(coerce_number)
    return df


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
