from __future__ import annotations

from typing import Dict, List

import pandas as pd

from .. import config
from ..normalization import coerce_number, percentile_score
from . import explanations, risk, scoring_math, theme_limits
from .weights import WEIGHTS


__all__ = ["_score_row"]


def _score_row(
    row: pd.Series,
    hot_ranks: Dict[str, int],
    industry_strength: Dict[str, float],
    sentiment_lookup: Dict[str, Dict[str, object]],
    context: Dict[str, List[float]],
    horizon: str,
    market_regime: Dict[str, object] = None,
) -> Dict[str, object]:
    if horizon != "short":
        raise ValueError("{} horizon is retired; current scoring row path supports short only".format(horizon))

    code = row["code"]
    industry = str(row.get("industry", "") or "")
    pct_chg = coerce_number(row.get("pct_chg"))
    speed = scoring_math._row_speed(row)
    volume_ratio = coerce_number(row.get("volume_ratio"))
    turnover_rate = coerce_number(row.get("turnover_rate"))
    turnover = coerce_number(row.get("turnover"))
    sixty_day_pct = coerce_number(row.get("sixty_day_pct"))
    ytd_pct = coerce_number(row.get("ytd_pct"))
    amplitude = coerce_number(row.get("amplitude"))
    ret_3d = coerce_number(row.get("ret_3d"))
    ret_5d = coerce_number(row.get("ret_5d"))
    ret_10d = coerce_number(row.get("ret_10d"))
    ret_20d = coerce_number(row.get("ret_20d"))
    ma5_gap = coerce_number(row.get("ma5_gap"))
    ma20_gap = coerce_number(row.get("ma20_gap"))
    vol_amount_5d = coerce_number(row.get("vol_amount_5d"))
    breakout_20d = coerce_number(row.get("breakout_20d"))
    volatility_20d = coerce_number(row.get("volatility_20d"))
    industry_pct = industry_strength.get(industry, 0.0)
    hot_rank = hot_ranks.get(code)
    sentiment = sentiment_lookup.get(code, {"score": 50.0, "summary": "未拉取到个股舆情"})
    execution_score = scoring_math._execution_score(row)
    history_ready = scoring_math._historical_factors_ready(row)

    def historical_score(value, values, **kwargs):
        return scoring_math._optional_factor_score(
            value,
            values,
            available=history_ready,
            **kwargs,
        )

    momentum_score = (
        percentile_score(pct_chg, context["pct_values"]) * 0.24
        + percentile_score(speed, context["speed_values"]) * 0.24
        + percentile_score(volume_ratio, context["volume_ratio_values"]) * 0.18
        + historical_score(ret_3d, context["ret_3d_values"]) * 0.12
        + historical_score(ret_5d, context["ret_5d_values"]) * 0.10
        + historical_score(vol_amount_5d, context["vol_amount_5d_values"]) * 0.08
        + historical_score(breakout_20d, context["breakout_20d_values"]) * 0.04
    )
    liquidity_score = (
        percentile_score(turnover_rate, context["turnover_rate_values"]) * 0.45
        + percentile_score(turnover, context["turnover_values"]) * 0.55
    )
    trend_score = (
        historical_score(
            ret_20d,
            context["ret_20d_values"],
            fallback=sixty_day_pct,
            fallback_values=context["sixty_day_values"],
        )
        * 0.24
        + historical_score(sixty_day_pct, context["sixty_day_values"]) * 0.20
        + historical_score(ytd_pct, context["ytd_values"]) * 0.14
        + historical_score(ma20_gap, context["ma20_gap_values"]) * 0.14
        + historical_score(ret_10d, context["ret_10d_values"]) * 0.12
        + historical_score(vol_amount_5d, context["vol_amount_5d_values"]) * 0.08
        + historical_score(
            volatility_20d,
            context["volatility_20d_values"],
            higher_is_better=False,
            fallback=amplitude,
            fallback_values=context["amplitude_values"],
        )
        * 0.08
    )
    industry_score = percentile_score(industry_pct, context["industry_values"]) if context["industry_values"] else 50.0
    hot_score = scoring_math._hot_rank_score(hot_rank)
    sentiment_score = coerce_number(sentiment.get("score"), 50.0)
    regime_bonus = scoring_math._market_regime_adjustment(row, market_regime, "short")
    regime_profile = scoring_math._regime_weight_profile(market_regime, ["momentum", "liquidity"])

    risk_penalty_parts = {}
    reversal_tilt = coerce_number(WEIGHTS["short_term"].get("reversal_tilt"), 0.0)
    if reversal_tilt > 0:
        recent_gain = coerce_number(row.get("ret_5d"), pct_chg)
        risk_penalty_parts["reversal_tilt"] = max(0.0, recent_gain) * reversal_tilt
    if sentiment.get("risk_words"):
        risk_penalty_parts["sentiment"] = 8
    if scoring_math._near_limit_up_risk(row):
        risk_penalty_parts["near_limit_up"] = 5
    risk_penalty = risk._sum_penalty(risk_penalty_parts)
    risk_guard_score = max(0.0, min(100.0, 100.0 - risk_penalty * 3.2))
    combined = scoring_math._combine_details(
        {
            "momentum_score": momentum_score,
            "liquidity_score": liquidity_score,
            "trend_score": trend_score,
            "industry_score": industry_score,
            "hot_score": hot_score,
            "sentiment_score": sentiment_score,
            "risk_guard_score": risk_guard_score,
            "risk_penalty": risk_penalty,
            "regime_bonus": regime_bonus,
        },
        "short_term",
        market_regime=market_regime,
        row=row,
        factor_ic_payload=context.get("factor_ic_payload"),
    )
    final_score = combined["score"]
    item = {
        "code": code,
        "name": str(row.get("name", "")),
        "market": row.get("market", "main"),
        "market_label": config.MARKET_LABELS.get(row.get("market", "main"), "主板"),
        "industry": industry,
        "theme": theme_limits._infer_theme_from_row(row) or industry,
        "price": round(coerce_number(row.get("price")), 3),
        "pct_chg": round(pct_chg, 2),
        "speed": round(coerce_number(row.get("speed")), 2),
        "five_min_pct": round(coerce_number(row.get("five_min_pct")), 2),
        "volume_ratio": round(volume_ratio, 2),
        "turnover_rate": round(turnover_rate, 2),
        "turnover": round(turnover, 2),
        "industry_pct": round(industry_pct, 2),
        "sixty_day_pct": round(sixty_day_pct, 2),
        "ytd_pct": round(ytd_pct, 2),
        "ret_3d": round(ret_3d, 2),
        "ret_5d": round(ret_5d, 2),
        "ret_10d": round(ret_10d, 2),
        "ret_20d": round(ret_20d, 2),
        "ma5_gap": round(ma5_gap, 2),
        "ma20_gap": round(ma20_gap, 2),
        "vol_amount_5d": round(vol_amount_5d, 2),
        "breakout_20d": bool(breakout_20d),
        "volatility_20d": round(volatility_20d, 2),
        "hot_rank": hot_rank,
        "hot_score": round(hot_score, 2),
        "momentum_score": round(momentum_score, 2),
        "liquidity_score": round(liquidity_score, 2),
        "trend_score": round(trend_score, 2),
        "execution_score": round(execution_score, 2),
        "industry_score": round(industry_score, 2),
        "sentiment_score": round(sentiment_score, 2),
        "risk_guard_score": round(risk_guard_score, 2),
        "risk_penalty": round(risk_penalty, 2),
        "risk_penalty_parts": risk_penalty_parts,
        "regime_bonus": round(regime_bonus, 2),
        "regime_weight_profile": regime_profile,
        "base_score": round(combined["base_score"], 2),
        "raw_score": round(combined["raw_score"], 2),
        "overheat_damp": round(combined["overheat_damp"], 4),
        "score": round(max(0.0, min(100.0, final_score)), 2),
        "sentiment_summary": sentiment.get("summary", "暂无明显舆情信号"),
        "risk_words": sentiment.get("risk_words", []),
        "reasons": explanations._build_reasons(row, industry_pct, hot_rank, sentiment),
        "horizon": horizon,
    }
    return explanations._with_regime_reason(
        explanations._attach_signal_explanation(item, row, "short_term", "今天策略", "延续至收盘"),
        market_regime,
        regime_bonus,
    )
