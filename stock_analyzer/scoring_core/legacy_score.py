from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Tuple

import pandas as pd

from .. import config
from ..normalization import coerce_number, finite_series, percentile_score
from .explanations import _build_reasons
from .scoring_math import _combined_speed, _hot_rank_score, _near_limit_up_risk, _row_speed


__all__ = ["score_candidates"]


def score_candidates(
    df: pd.DataFrame,
    hot_ranks: Dict[str, int],
    industry_strength: Dict[str, float],
    sentiment_lookup: Dict[str, Dict[str, object]],
    top_n: int,
    market_filter: str = "all",
) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    if market_filter in ("main", "chinext", "star"):
        df = df[df["market"] == market_filter].copy()
    if df.empty:
        return [], {"generated_at": datetime.now().isoformat(timespec="seconds")}

    pct_values = finite_series(df, "pct_chg").tolist()
    speed_values = _combined_speed(df).tolist()
    volume_ratio_values = finite_series(df, "volume_ratio").tolist()
    turnover_rate_values = finite_series(df, "turnover_rate").tolist()
    turnover_values = finite_series(df, "turnover").tolist()
    industry_values = list(industry_strength.values())

    rows: List[Dict[str, object]] = []
    for _, row in df.iterrows():
        code = row["code"]
        industry = str(row.get("industry", "") or "")
        pct_chg = coerce_number(row.get("pct_chg"))
        speed = _row_speed(row)
        volume_ratio = coerce_number(row.get("volume_ratio"))
        turnover_rate = coerce_number(row.get("turnover_rate"))
        turnover = coerce_number(row.get("turnover"))
        industry_pct = industry_strength.get(industry, 0.0)
        hot_rank = hot_ranks.get(code)
        sentiment = sentiment_lookup.get(code, {"score": 50.0, "summary": "未拉取到个股舆情"})

        momentum_score = (
            percentile_score(pct_chg, pct_values) * 0.38
            + percentile_score(speed, speed_values) * 0.32
            + percentile_score(volume_ratio, volume_ratio_values) * 0.30
        )
        liquidity_score = (
            percentile_score(turnover_rate, turnover_rate_values) * 0.45
            + percentile_score(turnover, turnover_values) * 0.55
        )
        industry_score = percentile_score(industry_pct, industry_values) if industry_values else 50.0
        hot_score = _hot_rank_score(hot_rank)
        sentiment_score = coerce_number(sentiment.get("score"), 50.0)

        final_score = (
            momentum_score * 0.55
            + liquidity_score * 0.15
            + industry_score * 0.08
            + hot_score * 0.07
            + sentiment_score * 0.15
        )
        if sentiment.get("risk_words"):
            final_score -= 8
        if _near_limit_up_risk(row):
            final_score -= 5

        rows.append(
            {
                "code": code,
                "name": str(row.get("name", "")),
                "market": row.get("market", "main"),
                "market_label": config.MARKET_LABELS.get(row.get("market", "main"), "主板"),
                "industry": industry,
                "price": round(coerce_number(row.get("price")), 3),
                "pct_chg": round(pct_chg, 2),
                "speed": round(coerce_number(row.get("speed")), 2),
                "five_min_pct": round(coerce_number(row.get("five_min_pct")), 2),
                "volume_ratio": round(volume_ratio, 2),
                "turnover_rate": round(turnover_rate, 2),
                "turnover": round(turnover, 2),
                "industry_pct": round(industry_pct, 2),
                "hot_rank": hot_rank,
                "momentum_score": round(momentum_score, 2),
                "liquidity_score": round(liquidity_score, 2),
                "industry_score": round(industry_score, 2),
                "sentiment_score": round(sentiment_score, 2),
                "score": round(max(0.0, min(100.0, final_score)), 2),
                "sentiment_summary": sentiment.get("summary", "暂无明显舆情信号"),
                "risk_words": sentiment.get("risk_words", []),
                "reasons": _build_reasons(row, industry_pct, hot_rank, sentiment),
            }
        )

    rows.sort(key=lambda item: item["score"], reverse=True)
    for rank, row in enumerate(rows[:top_n], start=1):
        row["rank"] = rank

    meta = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "candidate_count": len(df),
        "top_n": top_n,
        "market_filter": market_filter,
    }
    return rows[:top_n], meta
