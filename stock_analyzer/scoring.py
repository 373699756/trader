from datetime import datetime
from typing import Dict, List, Tuple

import pandas as pd

from . import config
from .normalization import (
    coerce_number,
    finite_series,
    is_supported_code,
    market_type,
    normalize_code,
    percentile_score,
)


TECH_THEMES = {
    "AI/算力": ("人工智能", "AI", "智能", "算力", "数据", "云", "软件", "信息", "数字", "模型"),
    "半导体": ("半导体", "芯片", "集成", "晶", "微", "芯", "硅", "封装", "存储", "光刻"),
    "机器人/智能制造": ("机器人", "自动化", "机床", "装备", "制造", "工业", "控制", "传感"),
    "低空/商业航天": ("航空", "航天", "导航", "无人机", "低空", "雷达", "飞行"),
    "智能汽车/车联网": ("汽车", "车联", "激光", "毫米波", "电驱", "电控", "线控", "座舱"),
    "新材料/高端电子": ("材料", "复材", "光电", "电子", "陶瓷", "碳", "磁", "膜", "玻璃"),
    "脑机/医疗科技": ("脑机", "神经", "医疗", "器械", "生物", "基因", "康复"),
}


def prepare_candidates(quotes: pd.DataFrame) -> pd.DataFrame:
    if quotes.empty:
        return quotes.copy()
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

    mask = df["code"].map(is_supported_code)
    mask &= ~df["name"].str.contains("ST|退", case=False, regex=True, na=False)
    mask &= df["price"] > 0
    mask &= df["turnover"] >= config.MIN_TURNOVER
    mask &= df["pct_chg"] > -8
    mask &= df["pct_chg"] <= config.MAX_RECOMMENDED_GAIN
    mask &= df.apply(_is_buyable_gain, axis=1)
    mask &= ~((df["high"] > 0) & (df["high"] == df["low"]) & (df["pct_chg"] > 8))
    return df.loc[mask].reset_index(drop=True)


def _is_buyable_gain(row: pd.Series) -> bool:
    pct = coerce_number(row.get("pct_chg"))
    market = row.get("market")
    if market in ("chinext", "star"):
        return pct <= config.MAX_BUYABLE_GAIN_GROWTH
    return pct <= config.MAX_BUYABLE_GAIN_MAIN


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


def score_dual_horizon_candidates(
    df: pd.DataFrame,
    hot_ranks: Dict[str, int],
    industry_strength: Dict[str, float],
    sentiment_lookup: Dict[str, Dict[str, object]],
    top_n: int = 10,
    market_filter: str = "all",
) -> Tuple[Dict[str, List[Dict[str, object]]], Dict[str, object]]:
    if market_filter in ("main", "chinext", "star"):
        df = df[df["market"] == market_filter].copy()
    if df.empty:
        return {"short_term": [], "long_term": []}, {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "candidate_count": 0,
            "top_n": top_n,
            "market_filter": market_filter,
        }

    context = _score_context(df, industry_strength)
    short_rows: List[Dict[str, object]] = []
    long_rows: List[Dict[str, object]] = []
    for _, row in df.iterrows():
        short_rows.append(
            _score_row(
                row,
                hot_ranks=hot_ranks,
                industry_strength=industry_strength,
                sentiment_lookup=sentiment_lookup,
                context=context,
                horizon="short",
            )
        )
        long_rows.append(
            _score_row(
                row,
                hot_ranks=hot_ranks,
                industry_strength=industry_strength,
                sentiment_lookup=sentiment_lookup,
                context=context,
                horizon="long",
            )
        )

    short_rows.sort(key=lambda item: item["score"], reverse=True)
    long_rows.sort(key=lambda item: item["score"], reverse=True)
    for rank, row in enumerate(short_rows[:top_n], start=1):
        row["rank"] = rank
    for rank, row in enumerate(long_rows[:top_n], start=1):
        row["rank"] = rank

    meta = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "candidate_count": len(df),
        "top_n": top_n,
        "market_filter": market_filter,
        "strategy": {
            "short_term": "盘中强势：涨跌幅、涨速、量比、换手、热度、舆情",
            "long_term": "趋势稳健：60日/YTD趋势、流动性、板块、舆情、风险惩罚",
        },
    }
    return {"short_term": short_rows[:top_n], "long_term": long_rows[:top_n]}, meta


def score_tomorrow_candidates(
    df: pd.DataFrame,
    top_n: int = 50,
    market_filter: str = "all",
) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    if market_filter in ("main", "chinext", "star"):
        df = df[df["market"] == market_filter].copy()
    if df.empty:
        return [], {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "candidate_count": 0,
            "top_n": top_n,
            "market_filter": market_filter,
            "analysis_window": "14:30",
            "strategy_version": "tomorrow_picks_v2",
            "strategy_label": "明天预测",
            "policy": _tomorrow_policy(),
        }

    context = _score_context(df, {})
    rows: List[Dict[str, object]] = []
    for _, row in df.iterrows():
        pct_chg = coerce_number(row.get("pct_chg"))
        volume_ratio = coerce_number(row.get("volume_ratio"))
        turnover_rate = coerce_number(row.get("turnover_rate"))
        turnover = coerce_number(row.get("turnover"))
        speed = _row_speed(row)
        amplitude = coerce_number(row.get("amplitude"))
        sixty_day_pct = coerce_number(row.get("sixty_day_pct"))
        ytd_pct = coerce_number(row.get("ytd_pct"))

        liquidity_score = (
            percentile_score(turnover, context["turnover_values"]) * 0.58
            + percentile_score(turnover_rate, context["turnover_rate_values"]) * 0.42
        )
        momentum_score = (
            percentile_score(pct_chg, context["pct_values"]) * 0.34
            + percentile_score(speed, context["speed_values"]) * 0.24
            + percentile_score(volume_ratio, context["volume_ratio_values"]) * 0.24
            + _optional_factor_score(sixty_day_pct, context["sixty_day_values"]) * 0.18
        )
        trend_score = (
            percentile_score(sixty_day_pct, context["sixty_day_values"]) * 0.55
            + percentile_score(ytd_pct, context["ytd_values"]) * 0.25
            + _optional_factor_score(
                amplitude,
                context["amplitude_values"],
                higher_is_better=False,
            ) * 0.20
        )
        execution_score = _execution_score(row)
        risk_penalty = _tomorrow_risk_penalty(row)
        final_score = (
            liquidity_score * 0.30
            + momentum_score * 0.28
            + trend_score * 0.20
            + execution_score * 0.22
            - risk_penalty
        )
        item = {
                "code": row["code"],
                "name": str(row.get("name", "")),
                "market": row.get("market", "main"),
                "market_label": config.MARKET_LABELS.get(row.get("market", "main"), "主板"),
                "price": round(coerce_number(row.get("price")), 3),
                "pct_chg": round(pct_chg, 2),
                "speed": round(coerce_number(row.get("speed")), 2),
                "five_min_pct": round(coerce_number(row.get("five_min_pct")), 2),
                "volume_ratio": round(volume_ratio, 2),
                "turnover_rate": round(turnover_rate, 2),
                "turnover": round(turnover, 2),
                "sixty_day_pct": round(sixty_day_pct, 2),
                "ytd_pct": round(ytd_pct, 2),
                "amplitude": round(amplitude, 2),
                "liquidity_score": round(liquidity_score, 2),
                "momentum_score": round(momentum_score, 2),
                "trend_score": round(trend_score, 2),
                "execution_score": round(execution_score, 2),
                "risk_penalty": round(risk_penalty, 2),
                "score": round(max(0.0, min(100.0, final_score)), 2),
                "reasons": _build_tomorrow_reasons(
                    row,
                    liquidity_score,
                    momentum_score,
                    trend_score,
                    execution_score,
                    risk_penalty,
                ),
            }
        rows.append(
            _attach_signal_explanation(
                item,
                row,
                "tomorrow_picks",
                "明天预测",
                "次日冲高",
            )
        )

    rows.sort(key=lambda item: item["score"], reverse=True)
    for rank, row in enumerate(rows[:top_n], start=1):
        row["rank"] = rank
    return rows[:top_n], {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "candidate_count": len(df),
        "top_n": top_n,
        "market_filter": market_filter,
        "analysis_window": "14:30",
        "strategy_version": "tomorrow_picks_v2",
        "strategy_label": "明天预测",
        "strategy": "14:30 明天预测：剔除涨停/近涨停，综合成交额、换手、量比、当日强度、中期趋势和执行性风险",
        "policy": _tomorrow_policy(),
    }


def score_tech_potential_candidates(
    df: pd.DataFrame,
    top_n: int = 50,
    market_filter: str = "all",
) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    if market_filter in ("main", "chinext", "star"):
        df = df[df["market"] == market_filter].copy()
    df = df[
        (finite_series(df, "sixty_day_pct") <= 90)
        & (finite_series(df, "ytd_pct") <= 150)
        & (finite_series(df, "sixty_day_pct") >= -25)
    ].copy()
    if df.empty:
        return [], {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "candidate_count": 0,
            "top_n": top_n,
            "market_filter": market_filter,
        }

    context = _score_context(df, {})
    rows: List[Dict[str, object]] = []
    for _, row in df.iterrows():
        theme, theme_score = _tech_theme_score(row)
        if theme_score <= 0:
            continue
        pct_chg = coerce_number(row.get("pct_chg"))
        turnover = coerce_number(row.get("turnover"))
        turnover_rate = coerce_number(row.get("turnover_rate"))
        volume_ratio = coerce_number(row.get("volume_ratio"))
        sixty_day_pct = coerce_number(row.get("sixty_day_pct"))
        ytd_pct = coerce_number(row.get("ytd_pct"))
        amplitude = coerce_number(row.get("amplitude"))

        liquidity_score = (
            percentile_score(turnover, context["turnover_values"]) * 0.62
            + percentile_score(turnover_rate, context["turnover_rate_values"]) * 0.38
        )
        early_trend_score = _early_trend_score(row)
        volume_score = _balanced_volume_score(volume_ratio)
        valuation_proxy_score = _not_overextended_score(row)
        execution_score = _execution_score(row)
        risk_penalty = _tech_potential_risk_penalty(row)
        final_score = (
            theme_score * 0.28
            + liquidity_score * 0.20
            + early_trend_score * 0.20
            + valuation_proxy_score * 0.16
            + volume_score * 0.09
            + execution_score * 0.07
            - risk_penalty
        )
        item = {
                "code": row["code"],
                "name": str(row.get("name", "")),
                "market": row.get("market", "main"),
                "market_label": config.MARKET_LABELS.get(row.get("market", "main"), "主板"),
                "theme": theme,
                "price": round(coerce_number(row.get("price")), 3),
                "pct_chg": round(pct_chg, 2),
                "volume_ratio": round(volume_ratio, 2),
                "turnover_rate": round(turnover_rate, 2),
                "turnover": round(turnover, 2),
                "sixty_day_pct": round(sixty_day_pct, 2),
                "ytd_pct": round(ytd_pct, 2),
                "amplitude": round(amplitude, 2),
                "theme_score": round(theme_score, 2),
                "liquidity_score": round(liquidity_score, 2),
                "early_trend_score": round(early_trend_score, 2),
                "not_overextended_score": round(valuation_proxy_score, 2),
                "execution_score": round(execution_score, 2),
                "risk_penalty": round(risk_penalty, 2),
                "score": round(max(0.0, min(100.0, final_score)), 2),
                "reasons": _build_tech_potential_reasons(
                    row,
                    theme,
                    early_trend_score,
                    valuation_proxy_score,
                    liquidity_score,
                    risk_penalty,
                ),
            }
        rows.append(
            _attach_signal_explanation(
                item,
                row,
                "tech_potential",
                "科技潜力",
                "科技主题潜力",
            )
        )

    rows.sort(key=lambda item: item["score"], reverse=True)
    for rank, row in enumerate(rows[:top_n], start=1):
        row["rank"] = rank
    return rows[:top_n], {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "candidate_count": len(df),
        "matched_count": len(rows),
        "top_n": top_n,
        "market_filter": market_filter,
        "strategy": "科技潜力：匹配前沿科技方向，排除涨幅透支，偏好刚启动、流动性足、可执行的潜力股",
    }


def score_swing_candidates(
    df: pd.DataFrame,
    top_n: int = 30,
    market_filter: str = "all",
) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    if market_filter in ("main", "chinext", "star"):
        df = df[df["market"] == market_filter].copy()
    df = df[
        (finite_series(df, "pct_chg") <= 8)
        & (finite_series(df, "sixty_day_pct") <= 85)
        & (finite_series(df, "ytd_pct") <= 130)
        & (finite_series(df, "sixty_day_pct") >= -18)
    ].copy()
    if df.empty:
        return [], _horizon_meta(top_n, market_filter, 0, "swing_5_10d_v1", "波段 5-10 日")

    context = _score_context(df, {})
    rows: List[Dict[str, object]] = []
    for _, row in df.iterrows():
        ret_5d = coerce_number(row.get("ret_5d"))
        ret_10d = coerce_number(row.get("ret_10d"))
        ret_20d = coerce_number(row.get("ret_20d"))
        ma5_gap = coerce_number(row.get("ma5_gap"))
        ma20_gap = coerce_number(row.get("ma20_gap"))
        vol_amount_5d = coerce_number(row.get("vol_amount_5d"))
        breakout_20d = coerce_number(row.get("breakout_20d"))
        volatility_20d = coerce_number(row.get("volatility_20d"))
        pct_chg = coerce_number(row.get("pct_chg"))
        turnover = coerce_number(row.get("turnover"))
        turnover_rate = coerce_number(row.get("turnover_rate"))
        volume_ratio = coerce_number(row.get("volume_ratio"))
        sixty_day_pct = coerce_number(row.get("sixty_day_pct"))
        ytd_pct = coerce_number(row.get("ytd_pct"))

        momentum_score = (
            _optional_factor_score(ret_5d, context["ret_5d_values"], fallback=pct_chg, fallback_values=context["pct_values"]) * 0.24
            + _optional_factor_score(ret_10d, context["ret_10d_values"], fallback=sixty_day_pct, fallback_values=context["sixty_day_values"]) * 0.22
            + _optional_factor_score(ma5_gap, context["ma5_gap_values"], fallback=pct_chg, fallback_values=context["pct_values"]) * 0.16
            + _optional_factor_score(vol_amount_5d, context["vol_amount_5d_values"], fallback=volume_ratio, fallback_values=context["volume_ratio_values"]) * 0.18
            + percentile_score(volume_ratio, context["volume_ratio_values"]) * 0.12
            + _optional_factor_score(breakout_20d, context["breakout_20d_values"]) * 0.08
        )
        trend_score = (
            _optional_factor_score(ret_20d, context["ret_20d_values"], fallback=sixty_day_pct, fallback_values=context["sixty_day_values"]) * 0.30
            + percentile_score(sixty_day_pct, context["sixty_day_values"]) * 0.26
            + _optional_factor_score(ma20_gap, context["ma20_gap_values"], fallback=sixty_day_pct, fallback_values=context["sixty_day_values"]) * 0.22
            + percentile_score(ytd_pct, context["ytd_values"]) * 0.10
            + _optional_factor_score(volatility_20d, context["volatility_20d_values"], higher_is_better=False, fallback=coerce_number(row.get("amplitude")), fallback_values=context["amplitude_values"]) * 0.12
        )
        liquidity_score = (
            percentile_score(turnover, context["turnover_values"]) * 0.62
            + percentile_score(turnover_rate, context["turnover_rate_values"]) * 0.38
        )
        execution_score = _execution_score(row)
        risk_penalty = _swing_risk_penalty(row)
        final_score = (
            momentum_score * 0.34
            + trend_score * 0.26
            + liquidity_score * 0.20
            + execution_score * 0.12
            + _not_overextended_score(row) * 0.08
            - risk_penalty
        )
        item = _horizon_row(row, {
            "ret_5d": ret_5d,
            "ret_10d": ret_10d,
            "ret_20d": ret_20d,
            "ma5_gap": ma5_gap,
            "ma20_gap": ma20_gap,
            "vol_amount_5d": vol_amount_5d,
            "breakout_20d": bool(breakout_20d),
            "volatility_20d": volatility_20d,
            "momentum_score": momentum_score,
            "trend_score": trend_score,
            "liquidity_score": liquidity_score,
            "execution_score": execution_score,
            "risk_penalty": risk_penalty,
            "score": final_score,
            "horizon": "swing",
            "reasons": _build_swing_reasons(row, momentum_score, trend_score, liquidity_score, risk_penalty),
        })
        rows.append(_attach_signal_explanation(item, row, "swing_picks", "波段 5-10 日", "波段延续"))

    rows.sort(key=lambda item: item["score"], reverse=True)
    for rank, row in enumerate(rows[:top_n], start=1):
        row["rank"] = rank
    meta = _horizon_meta(len(rows[:top_n]), market_filter, len(df), "swing_5_10d_v1", "波段 5-10 日")
    meta["strategy"] = "波段 5-10 日：偏好5/10/20日趋势延续、温和放量、站上短均线、流动性足且涨幅未透支"
    return rows[:top_n], meta


def score_position_candidates(
    df: pd.DataFrame,
    top_n: int = 30,
    market_filter: str = "all",
) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    if market_filter in ("main", "chinext", "star"):
        df = df[df["market"] == market_filter].copy()
    df = df[
        (finite_series(df, "pct_chg") <= 6)
        & (finite_series(df, "sixty_day_pct") <= 75)
        & (finite_series(df, "ytd_pct") <= 120)
        & (finite_series(df, "sixty_day_pct") >= -25)
    ].copy()
    if df.empty:
        return [], _horizon_meta(top_n, market_filter, 0, "position_1_3m_v1", "中长期 1-3 月")

    context = _score_context(df, {})
    rows: List[Dict[str, object]] = []
    for _, row in df.iterrows():
        ret_20d = coerce_number(row.get("ret_20d"))
        ret_10d = coerce_number(row.get("ret_10d"))
        ma20_gap = coerce_number(row.get("ma20_gap"))
        vol_amount_5d = coerce_number(row.get("vol_amount_5d"))
        volatility_20d = coerce_number(row.get("volatility_20d"))
        turnover = coerce_number(row.get("turnover"))
        turnover_rate = coerce_number(row.get("turnover_rate"))
        sixty_day_pct = coerce_number(row.get("sixty_day_pct"))
        ytd_pct = coerce_number(row.get("ytd_pct"))
        amplitude = coerce_number(row.get("amplitude"))
        theme, theme_score = _tech_theme_score(row)
        if not theme:
            theme = "行业/趋势"
            theme_score = 50.0

        trend_score = (
            percentile_score(sixty_day_pct, context["sixty_day_values"]) * 0.24
            + percentile_score(ytd_pct, context["ytd_values"]) * 0.18
            + _optional_factor_score(ret_20d, context["ret_20d_values"], fallback=sixty_day_pct, fallback_values=context["sixty_day_values"]) * 0.24
            + _optional_factor_score(ret_10d, context["ret_10d_values"], fallback=sixty_day_pct, fallback_values=context["sixty_day_values"]) * 0.12
            + _optional_factor_score(ma20_gap, context["ma20_gap_values"], fallback=sixty_day_pct, fallback_values=context["sixty_day_values"]) * 0.14
            + _optional_factor_score(volatility_20d, context["volatility_20d_values"], higher_is_better=False, fallback=amplitude, fallback_values=context["amplitude_values"]) * 0.08
        )
        quality_proxy_score = (
            _not_overextended_score(row) * 0.50
            + _optional_factor_score(volatility_20d, context["volatility_20d_values"], higher_is_better=False, fallback=amplitude, fallback_values=context["amplitude_values"]) * 0.25
            + _balanced_volume_score(coerce_number(row.get("volume_ratio"))) * 0.15
            + _optional_factor_score(vol_amount_5d, context["vol_amount_5d_values"], fallback=coerce_number(row.get("volume_ratio")), fallback_values=context["volume_ratio_values"]) * 0.10
        )
        liquidity_score = (
            percentile_score(turnover, context["turnover_values"]) * 0.68
            + percentile_score(turnover_rate, context["turnover_rate_values"]) * 0.32
        )
        risk_penalty = _position_risk_penalty(row)
        final_score = (
            trend_score * 0.34
            + quality_proxy_score * 0.26
            + liquidity_score * 0.20
            + theme_score * 0.12
            + _execution_score(row) * 0.08
            - risk_penalty
        )
        item = _horizon_row(row, {
            "theme": theme,
            "theme_score": theme_score,
            "ret_10d": ret_10d,
            "ret_20d": ret_20d,
            "ma20_gap": ma20_gap,
            "vol_amount_5d": vol_amount_5d,
            "volatility_20d": volatility_20d,
            "trend_score": trend_score,
            "quality_proxy_score": quality_proxy_score,
            "liquidity_score": liquidity_score,
            "execution_score": _execution_score(row),
            "risk_penalty": risk_penalty,
            "score": final_score,
            "horizon": "position",
            "reasons": _build_position_reasons(row, theme, trend_score, quality_proxy_score, liquidity_score, risk_penalty),
        })
        rows.append(_attach_signal_explanation(item, row, "position_picks", "中长期 1-3 月", "中期趋势"))

    rows.sort(key=lambda item: item["score"], reverse=True)
    for rank, row in enumerate(rows[:top_n], start=1):
        row["rank"] = rank
    meta = _horizon_meta(len(rows[:top_n]), market_filter, len(df), "position_1_3m_v1", "中长期 1-3 月")
    meta["strategy"] = "中长期 1-3 月：技术趋势版，偏好中期趋势向上、波动可控、涨幅未透支、流动性充足和科技/先进制造方向"
    meta["limitation"] = "当前未接入财务、估值和业绩数据，不能视为基本面价值策略。"
    return rows[:top_n], meta


def _score_context(df: pd.DataFrame, industry_strength: Dict[str, float]) -> Dict[str, List[float]]:
    return {
        "pct_values": finite_series(df, "pct_chg").tolist(),
        "speed_values": _combined_speed(df).tolist(),
        "volume_ratio_values": finite_series(df, "volume_ratio").tolist(),
        "turnover_rate_values": finite_series(df, "turnover_rate").tolist(),
        "turnover_values": finite_series(df, "turnover").tolist(),
        "sixty_day_values": finite_series(df, "sixty_day_pct").tolist(),
        "ytd_values": finite_series(df, "ytd_pct").tolist(),
        "amplitude_values": finite_series(df, "amplitude").tolist(),
        "ret_3d_values": finite_series(df, "ret_3d").tolist(),
        "ret_5d_values": finite_series(df, "ret_5d").tolist(),
        "ret_10d_values": finite_series(df, "ret_10d").tolist(),
        "ret_20d_values": finite_series(df, "ret_20d").tolist(),
        "ma5_gap_values": finite_series(df, "ma5_gap").tolist(),
        "ma20_gap_values": finite_series(df, "ma20_gap").tolist(),
        "vol_amount_5d_values": finite_series(df, "vol_amount_5d").tolist(),
        "breakout_20d_values": finite_series(df, "breakout_20d").tolist(),
        "volatility_20d_values": finite_series(df, "volatility_20d").tolist(),
        "industry_values": list(industry_strength.values()),
    }


def _tomorrow_policy() -> Dict[str, object]:
    return {
        "main_max_gain": config.MAX_BUYABLE_GAIN_MAIN,
        "growth_max_gain": config.MAX_BUYABLE_GAIN_GROWTH,
        "min_turnover": config.MIN_TURNOVER,
        "avoid_limit_up": True,
        "risk_controls": ("高涨幅", "高量比", "高换手", "高振幅", "前期涨幅透支"),
    }


def _horizon_meta(
    top_n: int,
    market_filter: str,
    candidate_count: int,
    strategy_version: str,
    strategy_label: str,
) -> Dict[str, object]:
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "candidate_count": candidate_count,
        "top_n": top_n,
        "market_filter": market_filter,
        "strategy_version": strategy_version,
        "strategy_label": strategy_label,
    }


def _horizon_row(row: pd.Series, scores: Dict[str, object]) -> Dict[str, object]:
    item = {
        "code": row["code"],
        "name": str(row.get("name", "")),
        "market": row.get("market", "main"),
        "market_label": config.MARKET_LABELS.get(row.get("market", "main"), "主板"),
        "industry": str(row.get("industry", "") or ""),
        "price": round(coerce_number(row.get("price")), 3),
        "pct_chg": round(coerce_number(row.get("pct_chg")), 2),
        "volume_ratio": round(coerce_number(row.get("volume_ratio")), 2),
        "turnover_rate": round(coerce_number(row.get("turnover_rate")), 2),
        "turnover": round(coerce_number(row.get("turnover")), 2),
        "sixty_day_pct": round(coerce_number(row.get("sixty_day_pct")), 2),
        "ytd_pct": round(coerce_number(row.get("ytd_pct")), 2),
        "amplitude": round(coerce_number(row.get("amplitude")), 2),
    }
    for key, value in scores.items():
        if key in ("reasons", "horizon", "theme", "breakout_20d"):
            item[key] = value
        elif isinstance(value, (int, float)):
            item[key] = round(max(0.0, min(100.0, value)), 2) if key == "score" else round(value, 2)
        else:
            item[key] = value
    return item


def _score_row(
    row: pd.Series,
    hot_ranks: Dict[str, int],
    industry_strength: Dict[str, float],
    sentiment_lookup: Dict[str, Dict[str, object]],
    context: Dict[str, List[float]],
    horizon: str,
) -> Dict[str, object]:
    code = row["code"]
    industry = str(row.get("industry", "") or "")
    pct_chg = coerce_number(row.get("pct_chg"))
    speed = _row_speed(row)
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

    momentum_score = (
        percentile_score(pct_chg, context["pct_values"]) * 0.24
        + percentile_score(speed, context["speed_values"]) * 0.24
        + percentile_score(volume_ratio, context["volume_ratio_values"]) * 0.18
        + _optional_factor_score(ret_3d, context["ret_3d_values"]) * 0.12
        + _optional_factor_score(ret_5d, context["ret_5d_values"]) * 0.10
        + _optional_factor_score(vol_amount_5d, context["vol_amount_5d_values"]) * 0.08
        + _optional_factor_score(breakout_20d, context["breakout_20d_values"]) * 0.04
    )
    liquidity_score = (
        percentile_score(turnover_rate, context["turnover_rate_values"]) * 0.45
        + percentile_score(turnover, context["turnover_values"]) * 0.55
    )
    trend_score = (
        _optional_factor_score(ret_20d, context["ret_20d_values"], fallback=sixty_day_pct, fallback_values=context["sixty_day_values"]) * 0.24
        + percentile_score(sixty_day_pct, context["sixty_day_values"]) * 0.20
        + percentile_score(ytd_pct, context["ytd_values"]) * 0.14
        + _optional_factor_score(ma20_gap, context["ma20_gap_values"]) * 0.14
        + _optional_factor_score(ret_10d, context["ret_10d_values"]) * 0.12
        + _optional_factor_score(vol_amount_5d, context["vol_amount_5d_values"]) * 0.08
        + _optional_factor_score(
            volatility_20d,
            context["volatility_20d_values"],
            higher_is_better=False,
            fallback=amplitude,
            fallback_values=context["amplitude_values"],
        ) * 0.08
    )
    industry_score = (
        percentile_score(industry_pct, context["industry_values"]) if context["industry_values"] else 50.0
    )
    hot_score = _hot_rank_score(hot_rank)
    sentiment_score = coerce_number(sentiment.get("score"), 50.0)

    if horizon == "long":
        final_score = (
            trend_score * 0.42
            + liquidity_score * 0.20
            + industry_score * 0.13
            + sentiment_score * 0.13
            + momentum_score * 0.07
            + hot_score * 0.05
        )
        final_score -= _long_term_risk_penalty(row, sentiment)
        reasons = _build_long_term_reasons(row, industry_pct, sentiment, trend_score, liquidity_score)
    else:
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
        reasons = _build_reasons(row, industry_pct, hot_rank, sentiment)

    item = {
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
        "momentum_score": round(momentum_score, 2),
        "liquidity_score": round(liquidity_score, 2),
        "trend_score": round(trend_score, 2),
        "industry_score": round(industry_score, 2),
        "sentiment_score": round(sentiment_score, 2),
        "score": round(max(0.0, min(100.0, final_score)), 2),
        "sentiment_summary": sentiment.get("summary", "暂无明显舆情信号"),
        "risk_words": sentiment.get("risk_words", []),
        "reasons": reasons,
        "horizon": horizon,
    }
    if horizon == "long":
        return _attach_signal_explanation(item, row, "long_term", "长期推荐", "趋势稳健")
    return _attach_signal_explanation(item, row, "short_term", "短线推荐", "盘中强势")


def _attach_signal_explanation(
    item: Dict[str, object],
    row: pd.Series,
    strategy_name: str,
    strategy_label: str,
    signal_label: str,
) -> Dict[str, object]:
    chase_risk = _chase_risk(row)
    overextension = _overextension_risk(row)
    failure_reasons = _failure_reasons(row, chase_risk, overextension)
    item.update(
        {
            "strategy_name": strategy_name,
            "strategy_label": strategy_label,
            "signal_label": signal_label,
            "chase_risk": chase_risk,
            "overextension": overextension,
            "failure_reasons": failure_reasons,
        }
    )
    return item


def _chase_risk(row: pd.Series) -> Dict[str, object]:
    pct = coerce_number(row.get("pct_chg"))
    market = row.get("market")
    upper = config.MAX_BUYABLE_GAIN_GROWTH if market in ("chinext", "star") else config.MAX_BUYABLE_GAIN_MAIN
    volume_ratio = coerce_number(row.get("volume_ratio"))
    turnover_rate = coerce_number(row.get("turnover_rate"))
    amplitude = coerce_number(row.get("amplitude"))
    reasons: List[str] = []
    score = 0
    if pct >= upper * 0.85:
        score += 3
        reasons.append("涨幅接近可买上限")
    elif pct >= upper * 0.70:
        score += 2
        reasons.append("当日涨幅偏高")
    if volume_ratio >= 5.5:
        score += 3
        reasons.append("量比过热")
    elif volume_ratio >= 4:
        score += 2
        reasons.append("量比偏高")
    if turnover_rate >= 18:
        score += 3
        reasons.append("换手过热")
    elif turnover_rate >= 12:
        score += 2
        reasons.append("换手偏高")
    if amplitude >= 12:
        score += 2
        reasons.append("振幅偏大")

    if score >= 5:
        level, label = "high", "高"
    elif score >= 2:
        level, label = "medium", "中"
    else:
        level, label = "low", "低"
    return {"level": level, "label": label, "score": score, "reasons": reasons}


def _overextension_risk(row: pd.Series) -> Dict[str, object]:
    sixty_day_pct = coerce_number(row.get("sixty_day_pct"))
    ytd_pct = coerce_number(row.get("ytd_pct"))
    ret_20d = coerce_number(row.get("ret_20d"))
    ma20_gap = coerce_number(row.get("ma20_gap"))
    reasons: List[str] = []
    score = 0
    if sixty_day_pct > 70:
        score += 3
        reasons.append("60日涨幅过大")
    elif sixty_day_pct > 45:
        score += 2
        reasons.append("60日涨幅偏大")
    if ytd_pct > 120:
        score += 3
        reasons.append("年内涨幅过大")
    elif ytd_pct > 80:
        score += 2
        reasons.append("年内涨幅偏大")
    if ret_20d > 45:
        score += 3
        reasons.append("20日涨幅过快")
    elif ret_20d > 25:
        score += 2
        reasons.append("20日涨幅偏快")
    if ma20_gap > 35:
        score += 3
        reasons.append("偏离20日线过远")
    elif ma20_gap > 22:
        score += 2
        reasons.append("偏离20日线偏远")

    if score >= 5:
        level, label = "high", "高"
    elif score >= 2:
        level, label = "medium", "中"
    else:
        level, label = "low", "低"
    return {"level": level, "label": label, "score": score, "reasons": reasons}


def _failure_reasons(
    row: pd.Series,
    chase_risk: Dict[str, object],
    overextension: Dict[str, object],
) -> List[str]:
    reasons: List[str] = []
    reasons.extend(str(reason) for reason in chase_risk.get("reasons", []))
    reasons.extend(str(reason) for reason in overextension.get("reasons", []))

    volume_ratio = coerce_number(row.get("volume_ratio"))
    turnover = coerce_number(row.get("turnover"))
    amplitude = coerce_number(row.get("amplitude"))
    pct = coerce_number(row.get("pct_chg"))
    if volume_ratio < 1:
        reasons.append("量能不足")
    if turnover < config.MIN_TURNOVER * 2:
        reasons.append("成交承接偏弱")
    if amplitude > 10:
        reasons.append("波动大，次日容易分歧")
    if pct < 0:
        reasons.append("当日走势偏弱")

    unique: List[str] = []
    for reason in reasons:
        if reason and reason not in unique:
            unique.append(reason)
    return unique[:6] or ["暂无明显单项风险，仍需次日走势验证"]


def _combined_speed(df: pd.DataFrame) -> pd.Series:
    speed = finite_series(df, "speed")
    five_min = finite_series(df, "five_min_pct")
    return speed.where(speed != 0, five_min)


def _row_speed(row: pd.Series) -> float:
    speed = coerce_number(row.get("speed"))
    if speed != 0:
        return speed
    return coerce_number(row.get("five_min_pct"))


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
    value: float,
    values: List[float],
    higher_is_better: bool = True,
    fallback: float = None,
    fallback_values: List[float] = None,
) -> float:
    if _has_signal(values):
        return percentile_score(value, values, higher_is_better=higher_is_better)
    if fallback is not None and fallback_values is not None:
        return percentile_score(fallback, fallback_values, higher_is_better=higher_is_better)
    return 50.0


def _has_signal(values: List[float]) -> bool:
    return any(abs(coerce_number(value)) > 1e-9 for value in values)


def _near_limit_up_risk(row: pd.Series) -> bool:
    pct = coerce_number(row.get("pct_chg"))
    market = row.get("market")
    limit = 20 if market in ("chinext", "star") else 10
    turnover = coerce_number(row.get("turnover"))
    return pct >= limit * 0.88 and turnover < config.MIN_TURNOVER * 2


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


def _tomorrow_risk_penalty(row: pd.Series) -> float:
    penalty = 0.0
    pct = coerce_number(row.get("pct_chg"))
    market = row.get("market")
    upper = config.MAX_BUYABLE_GAIN_GROWTH if market in ("chinext", "star") else config.MAX_BUYABLE_GAIN_MAIN
    amplitude = coerce_number(row.get("amplitude"))
    turnover_rate = coerce_number(row.get("turnover_rate"))
    volume_ratio = coerce_number(row.get("volume_ratio"))
    ytd_pct = coerce_number(row.get("ytd_pct"))
    if pct >= upper * 0.85:
        penalty += 10
    elif pct >= upper * 0.72:
        penalty += 5
    if amplitude >= 12:
        penalty += 8
    if turnover_rate >= 18:
        penalty += 7
    elif turnover_rate >= 12:
        penalty += 3
    if volume_ratio >= 6:
        penalty += 8
    elif volume_ratio >= 4.5:
        penalty += 4
    if ytd_pct >= 120:
        penalty += 10
    elif ytd_pct >= 80:
        penalty += 5
    return penalty


def _tech_theme_score(row: pd.Series) -> Tuple[str, float]:
    haystack = "{} {}".format(row.get("name", ""), row.get("industry", "")).upper()
    matches: List[str] = []
    for theme, keywords in TECH_THEMES.items():
        if any(keyword.upper() in haystack for keyword in keywords):
            matches.append(theme)
    if not matches:
        broad_keywords = (
            "科技",
            "电子",
            "通信",
            "光电",
            "光",
            "数据",
            "精密",
            "材料",
            "装备",
            "智能",
            "信息",
            "电源",
            "电路",
            "电气",
        )
        if row.get("market") in ("chinext", "star") or any(
            keyword.upper() in haystack for keyword in broad_keywords
        ):
            return "泛科技/先进制造", 48.0
        return "", 0.0
    score = min(100.0, 58.0 + len(matches) * 12.0)
    return " / ".join(matches[:2]), score


def _early_trend_score(row: pd.Series) -> float:
    sixty_day_pct = coerce_number(row.get("sixty_day_pct"))
    ytd_pct = coerce_number(row.get("ytd_pct"))
    pct = coerce_number(row.get("pct_chg"))
    score = 50.0
    if 3 <= sixty_day_pct <= 35:
        score += 24
    elif 0 <= sixty_day_pct < 3:
        score += 10
    elif 35 < sixty_day_pct <= 60:
        score += 8
    else:
        score -= 12
    if 0 <= ytd_pct <= 70:
        score += 16
    elif 70 < ytd_pct <= 100:
        score -= 8
    elif ytd_pct > 100:
        score -= 18
    if 0.5 <= pct <= 6:
        score += 10
    elif pct < -4 or pct > 9:
        score -= 10
    return max(0.0, min(100.0, score))


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


def _balanced_volume_score(volume_ratio: float) -> float:
    if 1.2 <= volume_ratio <= 3.5:
        return 88.0
    if 0.8 <= volume_ratio < 1.2:
        return 68.0
    if 3.5 < volume_ratio <= 5.5:
        return 62.0
    if volume_ratio > 5.5:
        return 45.0
    return 50.0


def _tech_potential_risk_penalty(row: pd.Series) -> float:
    penalty = _tomorrow_risk_penalty(row)
    sixty_day_pct = coerce_number(row.get("sixty_day_pct"))
    ytd_pct = coerce_number(row.get("ytd_pct"))
    pct = coerce_number(row.get("pct_chg"))
    if sixty_day_pct > 60:
        penalty += 12
    if ytd_pct > 100:
        penalty += 14
    if pct > 8:
        penalty += 7
    return penalty


def _swing_risk_penalty(row: pd.Series) -> float:
    penalty = 0.0
    pct = coerce_number(row.get("pct_chg"))
    sixty_day_pct = coerce_number(row.get("sixty_day_pct"))
    ytd_pct = coerce_number(row.get("ytd_pct"))
    volume_ratio = coerce_number(row.get("volume_ratio"))
    turnover_rate = coerce_number(row.get("turnover_rate"))
    volatility_20d = coerce_number(row.get("volatility_20d"))
    ma5_gap = coerce_number(row.get("ma5_gap"))
    if pct > 7:
        penalty += 6
    if sixty_day_pct > 70 or ytd_pct > 110:
        penalty += 9
    if volume_ratio > 5.5:
        penalty += 7
    if turnover_rate > 18:
        penalty += 6
    if volatility_20d > 7:
        penalty += 7
    if ma5_gap > 18:
        penalty += 5
    return penalty


def _position_risk_penalty(row: pd.Series) -> float:
    penalty = 0.0
    pct = coerce_number(row.get("pct_chg"))
    sixty_day_pct = coerce_number(row.get("sixty_day_pct"))
    ytd_pct = coerce_number(row.get("ytd_pct"))
    amplitude = coerce_number(row.get("amplitude"))
    volatility_20d = coerce_number(row.get("volatility_20d"))
    ma20_gap = coerce_number(row.get("ma20_gap"))
    turnover = coerce_number(row.get("turnover"))
    if pct > 5:
        penalty += 5
    if sixty_day_pct > 65 or ytd_pct > 100:
        penalty += 10
    if sixty_day_pct < -20 or ytd_pct < -25:
        penalty += 9
    if amplitude > 10 or volatility_20d > 6:
        penalty += 8
    if ma20_gap > 30:
        penalty += 6
    if turnover < config.MIN_TURNOVER * 2:
        penalty += 5
    return penalty


def _long_term_risk_penalty(row: pd.Series, sentiment: Dict[str, object]) -> float:
    penalty = 0.0
    pct = coerce_number(row.get("pct_chg"))
    sixty_day_pct = coerce_number(row.get("sixty_day_pct"))
    ytd_pct = coerce_number(row.get("ytd_pct"))
    amplitude = coerce_number(row.get("amplitude"))
    ret_20d = coerce_number(row.get("ret_20d"))
    ma20_gap = coerce_number(row.get("ma20_gap"))
    volatility_20d = coerce_number(row.get("volatility_20d"))
    turnover = coerce_number(row.get("turnover"))
    if sentiment.get("risk_words"):
        penalty += 10
    if pct > 9:
        penalty += 6
    if sixty_day_pct > 80 or ytd_pct > 120 or ret_20d > 45:
        penalty += 8
    if sixty_day_pct < -20 or ytd_pct < -25:
        penalty += 10
    if ma20_gap > 35:
        penalty += 5
    if amplitude > 12 or volatility_20d > 6:
        penalty += 5
    if turnover < config.MIN_TURNOVER * 2:
        penalty += 4
    return penalty


def _build_reasons(
    row: pd.Series,
    industry_pct: float,
    hot_rank,
    sentiment: Dict[str, object],
) -> List[str]:
    reasons: List[str] = []
    pct = coerce_number(row.get("pct_chg"))
    speed = _row_speed(row)
    volume_ratio = coerce_number(row.get("volume_ratio"))
    turnover_rate = coerce_number(row.get("turnover_rate"))
    sentiment_score = coerce_number(sentiment.get("score"), 50)

    if pct >= 5:
        reasons.append("涨幅靠前")
    elif pct >= 2:
        reasons.append("涨幅稳步走强")
    if speed >= 1:
        reasons.append("短线涨速转强")
    if volume_ratio >= 2:
        reasons.append("量比明显放大")
    elif volume_ratio >= 1.3:
        reasons.append("量能温和放大")
    if turnover_rate >= 5:
        reasons.append("换手活跃")
    if industry_pct >= 1:
        reasons.append("所属行业偏强")
    if hot_rank and int(hot_rank) <= 100:
        reasons.append("市场人气靠前")
    if sentiment_score >= 65:
        reasons.append(str(sentiment.get("summary", "舆情偏正面")))
    elif sentiment.get("risk_words"):
        reasons.append(str(sentiment.get("summary", "命中风险舆情")))

    return reasons[:6] or ["综合动能和流动性排名靠前"]


def _build_long_term_reasons(
    row: pd.Series,
    industry_pct: float,
    sentiment: Dict[str, object],
    trend_score: float,
    liquidity_score: float,
) -> List[str]:
    reasons: List[str] = []
    sixty_day_pct = coerce_number(row.get("sixty_day_pct"))
    ytd_pct = coerce_number(row.get("ytd_pct"))
    turnover = coerce_number(row.get("turnover"))
    amplitude = coerce_number(row.get("amplitude"))
    ret_20d = coerce_number(row.get("ret_20d"))
    ma20_gap = coerce_number(row.get("ma20_gap"))
    vol_amount_5d = coerce_number(row.get("vol_amount_5d"))
    breakout_20d = coerce_number(row.get("breakout_20d"))
    volatility_20d = coerce_number(row.get("volatility_20d"))
    sentiment_score = coerce_number(sentiment.get("score"), 50)

    if trend_score >= 70:
        reasons.append("中期趋势排名靠前")
    if 5 <= sixty_day_pct <= 60:
        reasons.append("60日趋势稳健")
    if ret_20d >= 5:
        reasons.append("20日动量为正")
    if ma20_gap >= 0:
        reasons.append("站上20日均线")
    if ytd_pct >= 0:
        reasons.append("年内趋势为正")
    if liquidity_score >= 65 or turnover >= config.MIN_TURNOVER * 5:
        reasons.append("成交流动性较好")
    if vol_amount_5d >= 1.2:
        reasons.append("近5日成交额放大")
    if breakout_20d:
        reasons.append("接近20日突破")
    if industry_pct >= 0.8:
        reasons.append("行业趋势偏强")
    if amplitude <= 8 and volatility_20d <= 5:
        reasons.append("波动相对可控")
    if sentiment_score >= 60:
        reasons.append(str(sentiment.get("summary", "舆情偏正面")))
    if sentiment.get("risk_words"):
        reasons.append(str(sentiment.get("summary", "命中风险舆情")))

    return reasons[:6] or ["趋势、流动性和风险综合排名靠前"]


def _build_tomorrow_reasons(
    row: pd.Series,
    liquidity_score: float,
    momentum_score: float,
    trend_score: float,
    execution_score: float,
    risk_penalty: float,
) -> List[str]:
    reasons: List[str] = []
    pct = coerce_number(row.get("pct_chg"))
    volume_ratio = coerce_number(row.get("volume_ratio"))
    turnover_rate = coerce_number(row.get("turnover_rate"))
    turnover = coerce_number(row.get("turnover"))
    sixty_day_pct = coerce_number(row.get("sixty_day_pct"))
    amplitude = coerce_number(row.get("amplitude"))
    if liquidity_score >= 72 or turnover >= 500000000:
        reasons.append("成交额靠前")
    if 1.2 <= volume_ratio <= 4.5:
        reasons.append("量能放大但未过热")
    elif volume_ratio > 4.5:
        reasons.append("量能很强需防分歧")
    if 2 <= pct <= 7:
        reasons.append("涨幅可参与")
    elif pct > 7:
        reasons.append("强势但未触及涨停过滤")
    if turnover_rate >= 3:
        reasons.append("换手活跃")
    if trend_score >= 65 or sixty_day_pct >= 8:
        reasons.append("中期趋势向上")
    if execution_score >= 75:
        reasons.append("执行性较好")
    if amplitude >= 9:
        reasons.append("波动偏大")
    if risk_penalty >= 8:
        reasons.append("风险扣分较高")
    if momentum_score >= 70:
        reasons.append("短线动能靠前")
    return reasons[:6] or ["流动性、动量和执行性综合排名靠前"]


def _build_tech_potential_reasons(
    row: pd.Series,
    theme: str,
    early_trend_score: float,
    not_overextended_score: float,
    liquidity_score: float,
    risk_penalty: float,
) -> List[str]:
    reasons: List[str] = []
    pct = coerce_number(row.get("pct_chg"))
    sixty_day_pct = coerce_number(row.get("sixty_day_pct"))
    ytd_pct = coerce_number(row.get("ytd_pct"))
    volume_ratio = coerce_number(row.get("volume_ratio"))
    if theme:
        reasons.append(theme)
    if early_trend_score >= 70:
        reasons.append("趋势刚启动")
    elif 0 <= sixty_day_pct <= 35:
        reasons.append("60日涨幅未透支")
    if not_overextended_score >= 72:
        reasons.append("前期涨幅可控")
    if liquidity_score >= 65:
        reasons.append("流动性较好")
    if 1.1 <= volume_ratio <= 3.5:
        reasons.append("量能温和放大")
    if 0 <= ytd_pct <= 70:
        reasons.append("年内涨幅未过热")
    if 0.5 <= pct <= 6:
        reasons.append("当日涨幅可参与")
    if risk_penalty >= 10:
        reasons.append("高位风险扣分")
    return reasons[:6] or ["科技方向匹配且涨幅未明显透支"]


def _build_swing_reasons(
    row: pd.Series,
    momentum_score: float,
    trend_score: float,
    liquidity_score: float,
    risk_penalty: float,
) -> List[str]:
    reasons: List[str] = []
    ret_5d = coerce_number(row.get("ret_5d"))
    ret_10d = coerce_number(row.get("ret_10d"))
    ret_20d = coerce_number(row.get("ret_20d"))
    ma5_gap = coerce_number(row.get("ma5_gap"))
    ma20_gap = coerce_number(row.get("ma20_gap"))
    volume_ratio = coerce_number(row.get("volume_ratio"))
    vol_amount_5d = coerce_number(row.get("vol_amount_5d"))
    if momentum_score >= 68:
        reasons.append("5-10日动量靠前")
    if ret_5d > 0 or ret_10d > 0:
        reasons.append("短周期收益转强")
    if ret_20d > 0 or trend_score >= 65:
        reasons.append("20日趋势延续")
    if ma5_gap >= 0 or ma20_gap >= 0:
        reasons.append("站上关键均线")
    if 1.1 <= volume_ratio <= 4.0 or vol_amount_5d >= 1.1:
        reasons.append("量能温和配合")
    if liquidity_score >= 65:
        reasons.append("流动性较好")
    if risk_penalty >= 8:
        reasons.append("波段风险偏高")
    return reasons[:6] or ["波段动量、趋势和流动性综合靠前"]


def _build_position_reasons(
    row: pd.Series,
    theme: str,
    trend_score: float,
    quality_proxy_score: float,
    liquidity_score: float,
    risk_penalty: float,
) -> List[str]:
    reasons: List[str] = []
    sixty_day_pct = coerce_number(row.get("sixty_day_pct"))
    ytd_pct = coerce_number(row.get("ytd_pct"))
    ret_20d = coerce_number(row.get("ret_20d"))
    ma20_gap = coerce_number(row.get("ma20_gap"))
    volatility_20d = coerce_number(row.get("volatility_20d"))
    if theme and theme != "行业/趋势":
        reasons.append(theme)
    if trend_score >= 68:
        reasons.append("中期趋势靠前")
    if 0 <= sixty_day_pct <= 55:
        reasons.append("60日涨幅未过热")
    if 0 <= ytd_pct <= 90:
        reasons.append("年内趋势可控")
    if ret_20d > 0 or ma20_gap >= 0:
        reasons.append("20日趋势向上")
    if quality_proxy_score >= 70:
        reasons.append("涨幅和波动较均衡")
    if liquidity_score >= 65:
        reasons.append("成交承接较好")
    if volatility_20d <= 5:
        reasons.append("波动相对可控")
    if risk_penalty >= 9:
        reasons.append("中长期风险扣分")
    return reasons[:6] or ["中期趋势、流动性和风险控制综合靠前"]
