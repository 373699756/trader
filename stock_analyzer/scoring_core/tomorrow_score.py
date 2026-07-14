from __future__ import annotations

from typing import Dict, List

import pandas as pd

from .. import config
from ..normalization import coerce_number, percentile_score
from . import explanations, risk, scoring_math


__all__ = [
    "_tomorrow_backup_reject",
    "_tomorrow_backup_rows",
    "_tomorrow_historical_edge_score",
]


def _tomorrow_historical_edge_score(row: pd.Series, context: Dict[str, List[float]]) -> float:
    if not scoring_math._historical_factors_ready(row):
        return 50.0
    ret_5d = coerce_number(row.get("ret_5d"))
    ret_10d = coerce_number(row.get("ret_10d"))
    ret_20d = coerce_number(row.get("ret_20d"))
    ma20_gap = coerce_number(row.get("ma20_gap"))
    vol_amount_5d = coerce_number(row.get("vol_amount_5d"))
    volatility_20d = coerce_number(row.get("volatility_20d"))
    breakout_20d = coerce_number(row.get("breakout_20d"))
    ma_bull_aligned = coerce_number(row.get("ma_bull_aligned"))
    score = (
        scoring_math._optional_factor_score(ret_5d, context["ret_5d_values"]) * 0.18
        + scoring_math._optional_factor_score(ret_10d, context["ret_10d_values"]) * 0.18
        + scoring_math._optional_factor_score(ret_20d, context["ret_20d_values"]) * 0.20
        + scoring_math._optional_factor_score(ma20_gap, context["ma20_gap_values"]) * 0.14
        + scoring_math._optional_factor_score(vol_amount_5d, context["vol_amount_5d_values"]) * 0.12
        + scoring_math._optional_factor_score(
            volatility_20d,
            context["volatility_20d_values"],
            higher_is_better=False,
        )
        * 0.12
        + (72.0 if breakout_20d else 50.0) * 0.04
        + (68.0 if ma_bull_aligned else 50.0) * 0.02
    )
    return max(0.0, min(100.0, score))


def _tomorrow_backup_reject(row: pd.Series) -> bool:
    pct = coerce_number(row.get("pct_chg"))
    market = row.get("market")
    upper = config.MAX_BUYABLE_GAIN_GROWTH if market in ("chinext", "star") else config.MAX_BUYABLE_GAIN_MAIN
    volume_ratio = coerce_number(row.get("volume_ratio"))
    amplitude = coerce_number(row.get("amplitude"))
    turnover_rate = coerce_number(row.get("turnover_rate"))
    turnover = coerce_number(row.get("turnover"))
    speed = scoring_math._row_speed(row)
    close_location = scoring_math._close_location(
        coerce_number(row.get("price")),
        coerce_number(row.get("high")),
        coerce_number(row.get("low")),
    )
    if pct <= -1.0 or pct >= upper * 0.88:
        return True
    if volume_ratio < 0.65 or volume_ratio >= 5.0:
        return True
    if turnover_rate > 0 and turnover_rate < 0.8:
        return True
    if turnover_rate >= 20.0:
        return True
    if amplitude >= 12.0:
        return True
    if speed > 4.2 or speed < -2.2:
        return True
    if close_location < 0.15:
        return True
    if scoring_math._near_limit_up_risk(row) and turnover_rate < 8.0:
        return True
    if config.MIN_TURNOVER > 0 and turnover < config.MIN_TURNOVER:
        return True
    if coerce_number(row.get("alphalite_factor_ready")) > 0:
        if (
            coerce_number(row.get("ret_20d")) < -18
            or coerce_number(row.get("ma20_gap")) < -10
            or coerce_number(row.get("volatility_20d")) > 10
        ):
            return True
    return False


def _tomorrow_component_scores(
    row: pd.Series,
    context: Dict[str, List[float]],
    market_regime: Dict[str, object] = None,
    provisional: bool = False,
    momentum_weights: Dict[str, float] = None,
    trend_weights: Dict[str, float] = None,
    risk_penalty_extra: float = 0.0,
) -> Dict[str, object]:
    momentum_weights = momentum_weights or {
        "pct_chg": 0.34,
        "speed": 0.24,
        "volume_ratio": 0.24,
        "sixty_day_pct": 0.18,
    }
    trend_weights = trend_weights or {
        "sixty_day_pct": 0.55,
        "ytd_pct": 0.25,
        "amplitude": 0.20,
    }
    pct_chg = coerce_number(row.get("pct_chg"))
    volume_ratio = coerce_number(row.get("volume_ratio"))
    turnover_rate = coerce_number(row.get("turnover_rate"))
    turnover = coerce_number(row.get("turnover"))
    speed = scoring_math._row_speed(row)
    amplitude = coerce_number(row.get("amplitude"))
    sixty_day_pct = coerce_number(row.get("sixty_day_pct"))
    ytd_pct = coerce_number(row.get("ytd_pct"))
    ret_5d = coerce_number(row.get("ret_5d"))
    ret_10d = coerce_number(row.get("ret_10d"))
    ret_20d = coerce_number(row.get("ret_20d"))
    ma20_gap = coerce_number(row.get("ma20_gap"))
    vol_amount_5d = coerce_number(row.get("vol_amount_5d"))
    volatility_20d = coerce_number(row.get("volatility_20d"))
    breakout_20d = coerce_number(row.get("breakout_20d"))
    history_ready = scoring_math._historical_factors_ready(row)

    def historical_score(value, values):
        return scoring_math._optional_factor_score(
            value,
            values,
            available=history_ready,
        )

    liquidity_score = (
        percentile_score(turnover, context["turnover_values"]) * 0.58
        + percentile_score(turnover_rate, context["turnover_rate_values"]) * 0.42
    )
    momentum_score = (
        percentile_score(pct_chg, context["pct_values"]) * momentum_weights["pct_chg"]
        + percentile_score(speed, context["speed_values"]) * momentum_weights["speed"]
        + percentile_score(volume_ratio, context["volume_ratio_values"]) * momentum_weights["volume_ratio"]
        + historical_score(sixty_day_pct, context["sixty_day_values"])
        * momentum_weights["sixty_day_pct"]
    )
    trend_score = (
        historical_score(sixty_day_pct, context["sixty_day_values"])
        * trend_weights["sixty_day_pct"]
        + historical_score(ytd_pct, context["ytd_values"])
        * trend_weights["ytd_pct"]
        + scoring_math._optional_factor_score(amplitude, context["amplitude_values"], higher_is_better=False)
        * trend_weights["amplitude"]
    )
    execution_score = scoring_math._execution_score(row)
    tail_setup_score = 50.0 if provisional else scoring_math._tail_close_setup_score(row)
    historical_edge_score = _tomorrow_historical_edge_score(row, context)
    risk_penalty_parts = risk._tomorrow_risk_penalty_parts(row, provisional=provisional)
    risk_penalty = risk._sum_penalty(risk_penalty_parts) + risk_penalty_extra
    regime_bonus = scoring_math._market_regime_adjustment(row, market_regime, "tomorrow")
    regime_profile = scoring_math._regime_weight_profile(
        market_regime,
        ["liquidity", "momentum", "trend", "quality"],
    )
    combined = scoring_math._combine_details(
        {
            "liquidity_score": liquidity_score,
            "momentum_score": momentum_score,
            "trend_score": trend_score,
            "historical_edge_score": historical_edge_score,
            "execution_score": execution_score,
            "tail_setup_score": tail_setup_score,
            "risk_penalty": risk_penalty,
            "regime_bonus": regime_bonus,
        },
        "tomorrow_picks",
        market_regime=market_regime,
        row=row,
        factor_ic_payload=context.get("factor_ic_payload"),
    )
    return {
        "pct_chg": pct_chg,
        "volume_ratio": volume_ratio,
        "turnover_rate": turnover_rate,
        "turnover": turnover,
        "amplitude": amplitude,
        "sixty_day_pct": sixty_day_pct,
        "ytd_pct": ytd_pct,
        "ret_5d": ret_5d,
        "ret_10d": ret_10d,
        "ret_20d": ret_20d,
        "ma20_gap": ma20_gap,
        "vol_amount_5d": vol_amount_5d,
        "breakout_20d": breakout_20d,
        "volatility_20d": volatility_20d,
        "liquidity_score": liquidity_score,
        "momentum_score": momentum_score,
        "trend_score": trend_score,
        "historical_edge_score": historical_edge_score,
        "execution_score": execution_score,
        "tail_setup_score": tail_setup_score,
        "risk_penalty": risk_penalty,
        "risk_penalty_parts": risk_penalty_parts,
        "regime_bonus": regime_bonus,
        "regime_weight_profile": regime_profile,
        "combined": combined,
    }


def _tomorrow_backup_components(
    row: pd.Series,
    context: Dict[str, List[float]],
    market_regime: Dict[str, object] = None,
    provisional: bool = False,
) -> Dict[str, object]:
    return _tomorrow_component_scores(
        row,
        context,
        market_regime=market_regime,
        provisional=provisional,
        momentum_weights={
            "pct_chg": 0.30,
            "speed": 0.20,
            "volume_ratio": 0.22,
            "sixty_day_pct": 0.28,
        },
        trend_weights={
            "sixty_day_pct": 0.60,
            "ytd_pct": 0.25,
            "amplitude": 0.15,
        },
        risk_penalty_extra=6.0,
    )


def _tomorrow_candidate_components(
    row: pd.Series,
    context: Dict[str, List[float]],
    market_regime: Dict[str, object] = None,
    intraday_relaxed: bool = False,
) -> Dict[str, object]:
    return _tomorrow_component_scores(
        row,
        context,
        market_regime=market_regime,
        provisional=intraday_relaxed,
    )


def _tomorrow_row_payload(row: pd.Series, scores: Dict[str, object], final_score: float) -> Dict[str, object]:
    combined = scores["combined"]
    return {
        "code": row["code"],
        "name": str(row.get("name", "")),
        "market": row.get("market", "main"),
        "market_label": config.MARKET_LABELS.get(row.get("market", "main"), "主板"),
        "industry": str(row.get("industry", "") or ""),
        "price": round(coerce_number(row.get("price")), 3),
        "pct_chg": round(scores["pct_chg"], 2),
        "speed": round(coerce_number(row.get("speed")), 2),
        "five_min_pct": round(coerce_number(row.get("five_min_pct")), 2),
        "volume_ratio": round(scores["volume_ratio"], 2),
        "turnover_rate": round(scores["turnover_rate"], 2),
        "turnover": round(scores["turnover"], 2),
        "sixty_day_pct": round(scores["sixty_day_pct"], 2),
        "ytd_pct": round(scores["ytd_pct"], 2),
        "amplitude": round(scores["amplitude"], 2),
        "ret_5d": round(scores["ret_5d"], 2),
        "ret_10d": round(scores["ret_10d"], 2),
        "ret_20d": round(scores["ret_20d"], 2),
        "ma20_gap": round(scores["ma20_gap"], 2),
        "vol_amount_5d": round(scores["vol_amount_5d"], 2),
        "breakout_20d": bool(scores["breakout_20d"]),
        "volatility_20d": round(scores["volatility_20d"], 2),
        "alphalite_factor_ready": round(coerce_number(row.get("alphalite_factor_ready")), 2),
        "alphalite_coverage": round(coerce_number(row.get("alphalite_coverage")), 2),
        "liquidity_score": round(scores["liquidity_score"], 2),
        "momentum_score": round(scores["momentum_score"], 2),
        "trend_score": round(scores["trend_score"], 2),
        "historical_edge_score": round(scores["historical_edge_score"], 2),
        "execution_score": round(scores["execution_score"], 2),
        "tail_setup_score": round(scores["tail_setup_score"], 2),
        "risk_penalty": round(scores["risk_penalty"], 2),
        "risk_penalty_parts": scores["risk_penalty_parts"],
        "regime_bonus": round(scores["regime_bonus"], 2),
        "regime_weight_profile": scores["regime_weight_profile"],
        "base_score": round(combined["base_score"], 2),
        "raw_score": round(combined["raw_score"], 2),
        "overheat_damp": round(combined["overheat_damp"], 4),
        "score": round(final_score, 2),
    }


def _tomorrow_reasons(row: pd.Series, scores: Dict[str, object]) -> List[str]:
    return explanations._build_tomorrow_reasons(
        row,
        scores["liquidity_score"],
        scores["momentum_score"],
        scores["trend_score"],
        scores["historical_edge_score"],
        scores["execution_score"],
        scores["tail_setup_score"],
        scores["risk_penalty"],
    )


def _tomorrow_candidate_row(
    row: pd.Series,
    context: Dict[str, List[float]],
    market_regime: Dict[str, object] = None,
    intraday_relaxed: bool = False,
) -> Dict[str, object]:
    scores = _tomorrow_candidate_components(
        row,
        context,
        market_regime=market_regime,
        intraday_relaxed=intraday_relaxed,
    )
    final_score = max(0.0, min(100.0, scores["combined"]["score"]))
    item = _tomorrow_row_payload(row, scores, final_score)
    item.update(
        {
            "mid_gain_weak_close_flag": bool(scores["risk_penalty_parts"].get("mid_gain_weak_close")),
            "holding_discipline": "T日14:30形成初版、14:50前冻结最终版；T+1按固定规则退出",
            "profit_window": "T日14:30后至T+1规则退出",
            "recommendation_class": "post_1430_next_day",
            "recommendation_class_label": "明日收益",
            "reasons": _tomorrow_reasons(row, scores),
        }
    )
    return explanations._with_regime_reason(
        explanations._attach_signal_explanation(item, row, "tomorrow_picks", "明日优先", "次日冲高"),
        market_regime,
        scores["regime_bonus"],
    )


def _build_tomorrow_backup_row(
    row: pd.Series,
    scores: Dict[str, object],
    market_regime: Dict[str, object] = None,
) -> Dict[str, object]:
    combined = scores["combined"]
    final_score = max(0.0, min(100.0, combined["score"] - 4.0))
    item = _tomorrow_row_payload(row, scores, final_score)
    item.update(
        {
            "holding_discipline": "T日14:30形成初版、14:50前冻结最终版；T+1按固定规则退出",
            "profit_window": "T日14:30后至T+1规则退出",
            "recommendation_class": "post_1430_next_day",
            "recommendation_class_label": "明日收益",
        }
    )
    item["reasons"] = ["备选观察：严格明日优先池为空"] + _tomorrow_reasons(row, scores)
    return explanations._with_regime_reason(
        explanations._attach_signal_explanation(item, row, "tomorrow_picks", "明日优先", "备选观察"),
        market_regime,
        scores["regime_bonus"],
    )


def _tomorrow_backup_rows(
    df: pd.DataFrame,
    context: Dict[str, List[float]],
    market_regime: Dict[str, object] = None,
    provisional: bool = False,
) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for _, row in df.iterrows():
        if _tomorrow_backup_reject(row):
            continue
        scores = _tomorrow_backup_components(
            row,
            context,
            market_regime=market_regime,
            provisional=provisional,
        )
        rows.append(_build_tomorrow_backup_row(row, scores, market_regime=market_regime))
    rows.sort(key=lambda item: item["score"], reverse=True)
    return rows
