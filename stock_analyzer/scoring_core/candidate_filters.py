from __future__ import annotations

from typing import Dict

import pandas as pd

from .. import config
from ..normalization import coerce_number, is_supported_code, market_type, normalize_code


__all__ = [
    "HARD_FILTER_LABELS",
    "candidate_filter_report",
    "prepare_candidates",
    "_candidate_base_frame",
    "_candidate_filter_masks",
    "_combine_candidate_masks",
    "_is_buyable_gain",
]

HARD_FILTER_LABELS = {
    "unsupported_code": "非主流A股代码",
    "special_treatment": "ST/退市风险名称",
    "positive_price": "无有效价格",
    "min_turnover": "成交额不足",
    "deep_drop": "跌幅过深",
    "max_gain": "涨幅过高",
    "buyable_gain": "接近涨停不可买",
    "one_word_limit": "一字板/极端封板",
}


def prepare_candidates(quotes: pd.DataFrame) -> pd.DataFrame:
    if quotes.empty:
        return quotes.copy()
    df = _candidate_base_frame(quotes)
    mask = _combine_candidate_masks(_candidate_filter_masks(df))
    result = df.loc[mask].reset_index(drop=True)
    result.attrs.update(quotes.attrs)
    return result


def candidate_filter_report(quotes: pd.DataFrame) -> Dict[str, object]:
    if quotes is None or quotes.empty:
        return {"raw_count": 0, "passed_count": 0, "rejected_count": 0, "reasons": []}
    df = _candidate_base_frame(quotes)
    masks = _candidate_filter_masks(df)
    remaining = pd.Series(True, index=df.index)
    reasons = []
    for key in HARD_FILTER_LABELS:
        failed = remaining & ~masks[key]
        count = int(failed.sum())
        if count:
            reasons.append({"key": key, "label": HARD_FILTER_LABELS[key], "count": count})
        remaining &= masks[key]
    passed_count = int(remaining.sum())
    return {
        "raw_count": int(len(df)),
        "passed_count": passed_count,
        "rejected_count": int(len(df) - passed_count),
        "reasons": reasons,
    }


def _candidate_base_frame(quotes: pd.DataFrame) -> pd.DataFrame:
    df = quotes.copy()
    if "code" not in df.columns:
        raise ValueError("行情数据缺少代码字段")
    if "name" not in df.columns:
        df["name"] = ""

    df["code"] = df["code"].map(normalize_code)
    df["name"] = df["name"].astype(str)
    df["market"] = df["code"].map(market_type)
    for column in (
        "price",
        "pct_chg",
        "change",
        "volume",
        "turnover",
        "amplitude",
        "high",
        "low",
        "open",
        "prev_close",
        "volume_ratio",
        "turnover_rate",
        "speed",
        "five_min_pct",
        "sixty_day_pct",
        "ytd_pct",
        "float_market_cap",
        "market_cap",
        "pe_dynamic",
        "pb",
    ):
        if column not in df.columns:
            df[column] = 0.0
        df[column] = df[column].map(coerce_number)

    if "industry" not in df.columns:
        for candidate in ("所属行业", "行业", "板块"):
            if candidate in df.columns:
                df["industry"] = df[candidate].astype(str)
                break
        else:
            df["industry"] = ""
    return df


def _candidate_filter_masks(df: pd.DataFrame) -> Dict[str, pd.Series]:
    return {
        "unsupported_code": df["code"].map(is_supported_code),
        "special_treatment": ~df["name"].str.contains("ST|退", case=False, regex=True, na=False),
        "positive_price": df["price"] > 0,
        "min_turnover": df["turnover"] >= config.MIN_TURNOVER,
        "deep_drop": df["pct_chg"] > -8,
        "max_gain": df["pct_chg"] <= config.MAX_RECOMMENDED_GAIN,
        "buyable_gain": df.apply(_is_buyable_gain, axis=1),
        "one_word_limit": ~((df["high"] > 0) & (df["high"] == df["low"]) & (df["pct_chg"] > 8)),
    }


def _combine_candidate_masks(masks: Dict[str, pd.Series]) -> pd.Series:
    combined = None
    for mask in masks.values():
        combined = mask if combined is None else combined & mask
    return combined


def _is_buyable_gain(row: pd.Series) -> bool:
    pct = coerce_number(row.get("pct_chg"))
    market = row.get("market")
    if market in ("chinext", "star"):
        return pct <= config.MAX_BUYABLE_GAIN_GROWTH
    return pct <= config.MAX_BUYABLE_GAIN_MAIN
