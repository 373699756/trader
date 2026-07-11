from __future__ import annotations

from typing import Dict

import pandas as pd

from .. import config
from ..normalization import coerce_number, finite_series, is_supported_code, market_type, normalize_code


__all__ = [
    "build_market_regime",
    "_history_breadth_metrics",
    "_market_regime_breadth_frame",
    "_market_regime_with_history",
]


def build_market_regime(df: pd.DataFrame, breadth_source: pd.DataFrame = None) -> Dict[str, object]:
    breadth_df = _market_regime_breadth_frame(breadth_source) if breadth_source is not None else df
    if breadth_df.empty:
        breadth_df = df
    if df.empty and breadth_df.empty:
        return {
            "level": "unknown",
            "label": "未知",
            "score": 50.0,
            "breadth_pct": 0.0,
            "history_breadth20_pct": 0.0,
            "history_factor_coverage_pct": 0.0,
            "history_ready_count": 0,
            "strong_pct": 0.0,
            "weak_pct": 0.0,
            "median_pct_chg": 0.0,
            "avg_amplitude": 0.0,
            "avg_turnover": 0.0,
            "leaders": [],
            "advice": "暂无足够样本判断当前盘面环境。",
        }

    pct_values = finite_series(breadth_df, "pct_chg")
    amplitude_values = finite_series(df, "amplitude")
    turnover_values = finite_series(df, "turnover")
    breadth_pct = round(float((pct_values > 0).mean() * 100), 2) if len(pct_values) else 0.0
    strong_pct = round(float((pct_values >= 3).mean() * 100), 2) if len(pct_values) else 0.0
    weak_pct = round(float((pct_values <= -3).mean() * 100), 2) if len(pct_values) else 0.0
    breadth_total = int(len(pct_values))
    up_count = int((pct_values > 0).sum()) if len(pct_values) else 0
    down_count = int((pct_values < 0).sum()) if len(pct_values) else 0
    limit_up_count = int((pct_values >= 9.5).sum()) if len(pct_values) else 0
    limit_down_count = int((pct_values <= -9.5).sum()) if len(pct_values) else 0
    avg_pct_chg = round(coerce_number(pct_values.mean()), 4) if len(pct_values) else 0.0
    median_pct_chg = round(coerce_number(pct_values.median()), 2) if len(pct_values) else 0.0
    avg_amplitude = round(coerce_number(amplitude_values.mean()), 2) if len(amplitude_values) else 0.0
    avg_turnover = round(coerce_number(turnover_values.mean()), 2) if len(turnover_values) else 0.0
    history_breadth = _history_breadth_metrics(df)

    score = 50.0
    score += median_pct_chg * 7.5
    score += (breadth_pct - 50.0) * 0.55
    score += (strong_pct - weak_pct) * 0.35
    score -= max(0.0, avg_amplitude - 7.0) * 2.4
    score = round(max(0.0, min(100.0, score)), 2)

    if score >= 68:
        level = "risk_on"
        label = "偏进攻"
        advice = "盘面承接较强，优先看强势延续与多策略共识标的。"
    elif score <= 42:
        level = "risk_off"
        label = "偏防守"
        advice = "盘面分歧偏大，优先看稳健趋势与低追高风险标的。"
    else:
        level = "balanced"
        label = "均衡震荡"
        advice = "盘面没有明显单边优势，优先看流动性和验证样本更好的策略。"

    leaders: List[Dict[str, object]] = []
    for market in ("main", "chinext", "star"):
        subset = breadth_df[breadth_df["market"] == market]
        if subset.empty:
            continue
        market_pct = finite_series(subset, "pct_chg")
        leaders.append(
            {
                "market": market,
                "market_label": config.MARKET_LABELS.get(market, market),
                "breadth_pct": round(float((market_pct > 0).mean() * 100), 2) if len(market_pct) else 0.0,
                "median_pct_chg": round(coerce_number(market_pct.median()), 2) if len(market_pct) else 0.0,
                "count": int(len(subset)),
            }
        )
    leaders.sort(key=lambda item: (item["median_pct_chg"], item["breadth_pct"]), reverse=True)

    return {
        "level": level,
        "label": label,
        "score": score,
        "breadth_pct": breadth_pct,
        "breadth_sample_count": breadth_total,
        "up_count": up_count,
        "down_count": down_count,
        "limit_up_count": limit_up_count,
        "limit_down_count": limit_down_count,
        "avg_pct_chg": avg_pct_chg,
        **history_breadth,
        "strong_pct": strong_pct,
        "weak_pct": weak_pct,
        "median_pct_chg": median_pct_chg,
        "avg_amplitude": avg_amplitude,
        "avg_turnover": avg_turnover,
        "leaders": leaders[:3],
        "advice": advice,
    }


def _history_breadth_metrics(df: pd.DataFrame) -> Dict[str, object]:
    empty = {
        "history_breadth20_pct": 0.0,
        "history_factor_coverage_pct": 0.0,
        "history_ready_count": 0,
        "history_median_ret5": 0.0,
        "history_median_ret20": 0.0,
    }
    if df is None or df.empty or "ma20_gap" not in df.columns:
        return empty.copy()
    ready = pd.Series([False] * len(df), index=df.index)
    if "alphalite_factor_ready" in df.columns:
        ready = ready | (finite_series(df, "alphalite_factor_ready") > 0)
    ready = ready | (finite_series(df, "ma20_gap").abs() > 1e-12)
    ready_df = df.loc[ready]
    if ready_df.empty:
        return empty.copy()
    ma20_gap = finite_series(ready_df, "ma20_gap")
    ret5 = finite_series(ready_df, "ret_5d")
    ret20 = finite_series(ready_df, "ret_20d")
    return {
        "history_breadth20_pct": round(float((ma20_gap > 0).mean() * 100), 2),
        "history_factor_coverage_pct": round(float(len(ready_df) / max(1, len(df)) * 100), 2),
        "history_ready_count": int(len(ready_df)),
        "history_median_ret5": round(coerce_number(ret5.median()), 2) if len(ret5) else 0.0,
        "history_median_ret20": round(coerce_number(ret20.median()), 2) if len(ret20) else 0.0,
    }


def _market_regime_breadth_frame(quotes: pd.DataFrame) -> pd.DataFrame:
    if quotes is None or quotes.empty:
        return pd.DataFrame()
    df = quotes.copy()
    if "code" not in df.columns:
        return pd.DataFrame()
    if "name" not in df.columns:
        df["name"] = ""
    df["code"] = df["code"].map(normalize_code)
    if "market" not in df.columns:
        df["market"] = df["code"].map(market_type)
    if "price" not in df.columns:
        df["price"] = 0.0
    if "pct_chg" not in df.columns:
        df["pct_chg"] = 0.0
    df["price"] = df["price"].map(coerce_number)
    df["pct_chg"] = df["pct_chg"].map(coerce_number)
    mask = df["code"].map(is_supported_code)
    mask &= ~df["name"].astype(str).str.contains("ST|退", case=False, regex=True, na=False)
    mask &= df["price"] > 0
    return df.loc[mask].reset_index(drop=True)


def _market_regime_with_history(market_regime: Dict[str, object], df: pd.DataFrame) -> Dict[str, object]:
    regime = dict(market_regime or {})
    history_metrics = _history_breadth_metrics(df)
    for key, value in history_metrics.items():
        if key not in regime or coerce_number(regime.get(key)) <= 0:
            regime[key] = value
    return regime
