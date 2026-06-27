import math
from typing import Any, Iterable, Optional

import pandas as pd

from . import config


COLUMN_ALIASES = {
    "代码": "code",
    "股票代码": "code",
    "ts_code": "code",
    "名称": "name",
    "股票简称": "name",
    "name": "name",
    "行业": "industry",
    "所属行业": "industry",
    "板块": "industry",
    "行业板块": "industry",
    "日期": "trade_date",
    "交易日期": "trade_date",
    "date": "trade_date",
    "最新价": "price",
    "现价": "price",
    "收盘": "price",
    "close": "price",
    "涨跌幅": "pct_chg",
    "涨幅": "pct_chg",
    "pct_chg": "pct_chg",
    "涨跌额": "change",
    "change": "change",
    "成交量": "volume",
    "vol": "volume",
    "成交额": "turnover",
    "amount": "turnover",
    "振幅": "amplitude",
    "最高": "high",
    "high": "high",
    "最低": "low",
    "low": "low",
    "今开": "open",
    "开盘": "open",
    "open": "open",
    "昨收": "prev_close",
    "pre_close": "prev_close",
    "量比": "volume_ratio",
    "换手率": "turnover_rate",
    "市盈率-动态": "pe_dynamic",
    "市盈率": "pe_dynamic",
    "市净率": "pb",
    "总市值": "market_cap",
    "流通市值": "float_market_cap",
    "涨速": "speed",
    "5分钟涨跌": "five_min_pct",
    "60日涨跌幅": "sixty_day_pct",
    "年初至今涨跌幅": "ytd_pct",
}


def rename_known_columns(df: pd.DataFrame) -> pd.DataFrame:
    rename_map = {}
    for column in df.columns:
        if column in COLUMN_ALIASES:
            rename_map[column] = COLUMN_ALIASES[column]
    return df.rename(columns=rename_map)


def coerce_number(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        if math.isnan(value) or math.isinf(value):
            return default
        return float(value)
    text = str(value).strip().replace(",", "").replace("%", "")
    if text in ("", "-", "--", "None", "nan", "NaN"):
        return default
    try:
        number = float(text)
    except ValueError:
        return default
    if math.isnan(number) or math.isinf(number):
        return default
    return number


def normalize_code(code: Any) -> str:
    text = str(code).strip()
    if "." in text:
        text = text.split(".")[0]
    return text.zfill(6)[-6:]


def market_type(code: str) -> str:
    if code.startswith(config.STAR_PREFIXES):
        return "star"
    if code.startswith(config.CHINEXT_PREFIXES):
        return "chinext"
    return "main"


def is_supported_code(code: str) -> bool:
    return code.startswith(config.SUPPORTED_PREFIXES)


def finite_series(df: pd.DataFrame, column: str) -> pd.Series:
    if column not in df.columns:
        return pd.Series([0.0] * len(df), index=df.index)
    return pd.to_numeric(df[column], errors="coerce").replace([math.inf, -math.inf], 0).fillna(0)


def percentile_score(value: float, values: Iterable[float], higher_is_better: bool = True) -> float:
    clean_values = sorted([v for v in values if isinstance(v, (int, float)) and math.isfinite(v)])
    if not clean_values:
        return 50.0
    value = max(min(value, clean_values[-1]), clean_values[0])
    below = sum(1 for item in clean_values if item <= value)
    pct = below / len(clean_values) * 100
    if not higher_is_better:
        pct = 100 - pct
    return round(max(0.0, min(100.0, pct)), 2)


def first_text(row: pd.Series, candidates: Iterable[str], default: str = "") -> str:
    for column in candidates:
        if column in row and pd.notna(row[column]):
            value = str(row[column]).strip()
            if value and value not in ("-", "--", "nan"):
                return value
    return default


def safe_datetime(value: Any) -> Optional[pd.Timestamp]:
    if value is None:
        return None
    try:
        ts = pd.to_datetime(value, errors="coerce")
    except Exception:
        return None
    if pd.isna(ts):
        return None
    return ts
