from typing import Dict, List

import pandas as pd

from . import config
from .normalization import coerce_number, is_supported_code, market_type, normalize_code, rename_known_columns
from .risk_blacklist import blacklist_risk_for_code
from .scoring import STRATEGY_LABELS, build_strategy_consensus


HORIZON_STRATEGIES = {
    "short": ("short_term", "tomorrow_picks", "swing_picks"),
    "long": ("long_term", "position_picks", "tech_potential", "chokepoint_picks"),
}

HORIZON_LABELS = {
    "short": "短期",
    "long": "长期",
}


def build_stock_prediction(
    code: str,
    candidates: pd.DataFrame,
    strategy_rows: Dict[str, List[Dict[str, object]]],
    strategy_metas: Dict[str, Dict[str, object]] = None,
    market_regime: Dict[str, object] = None,
    raw_quotes: pd.DataFrame = None,
    fallback_history: pd.DataFrame = None,
    fallback_error: str = "",
) -> Dict[str, object]:
    code = normalize_code(code)
    row = _candidate_row(code, candidates)
    if row is None:
        raw_row = _raw_quote_row(code, raw_quotes)
        if raw_row is None:
            history_result = _history_stock_prediction(code, fallback_history, market_regime or {}, fallback_error)
            if history_result is not None:
                return history_result
            return _missing_quote_prediction(code, market_regime or {}, fallback_error)
        return _filtered_stock_prediction(code, raw_row, market_regime or {}, data_source="实时行情")

    strategy_metas = strategy_metas or {}
    market_regime = market_regime or {}
    horizons = {
        name: _build_horizon_prediction(
            code,
            name,
            strategy_rows,
            strategy_metas,
            market_regime,
        )
        for name in ("short", "long")
    }
    hits = horizons["short"]["strategy_hits"] + horizons["long"]["strategy_hits"]
    missed = horizons["short"]["missed_strategies"] + horizons["long"]["missed_strategies"]
    consensus_rows = build_strategy_consensus(_ordered_strategy_rows(strategy_rows), minimum_appearances=1, top_n=50)
    consensus = _find_row(code, consensus_rows) or {}
    verdict = _prediction_verdict(row, hits, consensus, market_regime)
    return {
        "ok": True,
        "code": code,
        "name": str(row.get("name", "")),
        "market": row.get("market", ""),
        "market_label": str(row.get("market_label", "")),
        "price": coerce_number(row.get("price")),
        "pct_chg": coerce_number(row.get("pct_chg")),
        "turnover": coerce_number(row.get("turnover")),
        "volume_ratio": coerce_number(row.get("volume_ratio")),
        "sixty_day_pct": coerce_number(row.get("sixty_day_pct")),
        "ytd_pct": coerce_number(row.get("ytd_pct")),
        "data_source": "实时行情",
        "market_regime": {
            "label": market_regime.get("label", "未知"),
            "score": market_regime.get("score", 50.0),
            "advice": market_regime.get("advice", ""),
        },
        "horizons": horizons,
        "prediction": verdict,
        "strategy_hits": hits,
        "missed_strategies": missed,
        "consensus": consensus,
        "disclaimer": "仅按当前策略打分给出涨跌倾向，不构成投资建议，也不是确定性价格预测。",
    }


def _build_horizon_prediction(
    code: str,
    horizon: str,
    strategy_rows: Dict[str, List[Dict[str, object]]],
    strategy_metas: Dict[str, Dict[str, object]],
    market_regime: Dict[str, object],
) -> Dict[str, object]:
    names = HORIZON_STRATEGIES[horizon]
    rows_for_horizon = {name: strategy_rows.get(name, []) for name in names}
    hits = []
    missed = []
    for strategy_name in names:
        matched = _find_row(code, strategy_rows.get(strategy_name, []))
        if matched:
            hit = _strategy_hit(strategy_name, matched, strategy_metas.get(strategy_name) or {})
            hit["horizon"] = horizon
            hit["horizon_label"] = HORIZON_LABELS[horizon]
            hits.append(hit)
        else:
            missed.append(
                {
                    "horizon": horizon,
                    "horizon_label": HORIZON_LABELS[horizon],
                    "strategy_name": strategy_name,
                    "strategy_label": STRATEGY_LABELS.get(strategy_name, strategy_name),
                    "reason": "未进入该策略候选榜或被策略条件过滤",
                }
            )
    consensus_rows = build_strategy_consensus(rows_for_horizon, minimum_appearances=1, top_n=50)
    consensus = _find_row(code, consensus_rows) or {}
    return {
        "horizon": horizon,
        "label": HORIZON_LABELS[horizon],
        "prediction": _prediction_verdict({}, hits, consensus, market_regime, horizon=horizon),
        "strategy_hits": hits,
        "missed_strategies": missed,
        "consensus": consensus,
    }


def _ordered_strategy_rows(strategy_rows: Dict[str, List[Dict[str, object]]]) -> Dict[str, List[Dict[str, object]]]:
    ordered = {}
    for name in HORIZON_STRATEGIES["short"] + HORIZON_STRATEGIES["long"]:
        if name in strategy_rows:
            ordered[name] = strategy_rows[name]
    for name, rows in strategy_rows.items():
        if name not in ordered:
            ordered[name] = rows
    return ordered


def _candidate_row(code: str, candidates: pd.DataFrame):
    if candidates is None or candidates.empty or "code" not in candidates.columns:
        return None
    matched = candidates[candidates["code"].astype(str) == code]
    if matched.empty:
        return None
    item = matched.iloc[0].to_dict()
    item["market_label"] = item.get("market_label") or _market_label(item.get("market"))
    return item


def _raw_quote_row(code: str, quotes: pd.DataFrame):
    if quotes is None or quotes.empty or "code" not in rename_known_columns(quotes.copy()).columns:
        return None
    df = rename_known_columns(quotes.copy())
    df["code"] = df["code"].map(normalize_code)
    matched = df[df["code"] == code]
    if matched.empty:
        return None
    item = matched.iloc[0].to_dict()
    item["market"] = item.get("market") or market_type(code)
    item["market_label"] = item.get("market_label") or _market_label(item.get("market"))
    return item


def _history_stock_prediction(
    code: str,
    history: pd.DataFrame,
    market_regime: Dict[str, object],
    fallback_error: str = "",
) -> Dict[str, object]:
    row = _history_quote_row(code, history)
    if row is None:
        return None
    risks = [
        "实时行情源未返回该股票，无法确认当前是否停牌、退市或可交易",
        "使用历史行情兜底，缺少盘口、涨停板和最新成交状态",
    ]
    if fallback_error:
        risks.append("实时行情兜底异常：{}".format(fallback_error))
    return _filtered_stock_prediction(
        code,
        row,
        market_regime,
        extra_risks=risks,
        data_source="历史行情兜底",
        disclaimer="该结果基于历史行情兜底生成，实时交易状态不可确认；仅作风险诊断，不构成投资建议。",
    )


def _history_quote_row(code: str, history: pd.DataFrame):
    if history is None or history.empty:
        return None
    try:
        df = rename_known_columns(history.copy())
    except Exception:
        df = history.copy()
    if "price" not in df.columns:
        return None
    df = df.reset_index(drop=True)
    latest = df.iloc[-1].to_dict()
    price = coerce_number(latest.get("price"))
    if price <= 0:
        return None
    prev_price = coerce_number(df.iloc[-2].get("price")) if len(df) >= 2 else 0.0
    pct = coerce_number(latest.get("pct_chg"))
    if pct == 0.0 and prev_price > 0:
        pct = (price / prev_price - 1.0) * 100.0
    sixty_base = coerce_number(df.iloc[max(0, len(df) - 61)].get("price")) if len(df) >= 2 else 0.0
    ytd_base = _history_ytd_base(df)
    volume = coerce_number(latest.get("volume"))
    volume_series = pd.to_numeric(df["volume"], errors="coerce").tail(6).fillna(0) if "volume" in df.columns else pd.Series(dtype=float)
    volume_mean = float(volume_series.iloc[:-1].mean()) if len(volume_series) >= 2 else 0.0
    return {
        "code": code,
        "name": str(latest.get("name", "")),
        "market": market_type(code),
        "market_label": _market_label(market_type(code)),
        "price": price,
        "pct_chg": pct,
        "turnover": coerce_number(latest.get("turnover")),
        "volume": volume,
        "volume_ratio": volume / volume_mean if volume_mean > 0 else 0.0,
        "sixty_day_pct": (price / sixty_base - 1.0) * 100.0 if sixty_base > 0 else 0.0,
        "ytd_pct": (price / ytd_base - 1.0) * 100.0 if ytd_base > 0 else 0.0,
        "high": coerce_number(latest.get("high")),
        "low": coerce_number(latest.get("low")),
        "open": coerce_number(latest.get("open")),
    }


def _history_ytd_base(df: pd.DataFrame) -> float:
    if "trade_date" in df.columns:
        dates = pd.to_datetime(df["trade_date"], errors="coerce")
        if dates.notna().any():
            latest_year = int(dates.dropna().iloc[-1].year)
            same_year = df.loc[dates.dt.year == latest_year]
            if not same_year.empty:
                return coerce_number(same_year.iloc[0].get("price"))
    return coerce_number(df.iloc[0].get("price"))


def _missing_quote_prediction(code: str, market_regime: Dict[str, object], fallback_error: str = "") -> Dict[str, object]:
    risks = [
        "实时行情源没有返回该股票",
        "历史行情也不可用，无法确认是否代码不存在、停牌、退市或免费源缺失",
        "数据不足，不能给出正向预测，默认按高风险处理",
    ]
    if fallback_error:
        risks.append("历史行情兜底失败：{}".format(fallback_error))
    return _filtered_stock_prediction(
        code,
        {
            "code": code,
            "name": "",
            "market": market_type(code),
            "market_label": _market_label(market_type(code)),
            "price": 0.0,
            "pct_chg": 0.0,
            "turnover": 0.0,
            "volume_ratio": 0.0,
            "sixty_day_pct": 0.0,
            "ytd_pct": 0.0,
        },
        market_regime,
        extra_risks=risks,
        data_source="无可用行情",
        disclaimer="实时行情和历史行情均不可用，系统只能给出高风险诊断；不构成投资建议。",
    )


def _filtered_stock_prediction(
    code: str,
    row: Dict[str, object],
    market_regime: Dict[str, object],
    extra_risks: List[str] = None,
    data_source: str = "实时行情",
    disclaimer: str = "",
) -> Dict[str, object]:
    blacklist_risk = blacklist_risk_for_code(code)
    blacklist_risks = ["黑名单风险:{}".format(flag.get("label", "历史重大负面风险")) for flag in blacklist_risk.get("flags", [])[:4]]
    risks = blacklist_risks + list(extra_risks or []) + _filter_risks(row)
    risk_score = _filter_risk_score(risks)
    if blacklist_risk.get("hard_exclude"):
        risk_score = max(95.0, risk_score)
    label = "高风险/不建议参与" if risk_score >= 80 else "偏弱/风险较高" if risk_score >= 55 else "未入选推荐池"
    score = round(max(5.0, 48.0 - risk_score * 0.38), 2)
    confidence = round(min(95.0, 45.0 + risk_score * 0.45), 2)
    common = {
        "direction": "down" if risk_score >= 55 else "neutral",
        "label": label,
        "confidence": confidence,
        "score": score,
        "risk_level": "high" if risk_score >= 70 else "medium" if risk_score >= 40 else "unknown",
        "avg_risk": risk_score,
        "appearances": 0,
    }
    short = {
        **common,
        "advice": "短期未通过推荐池风控，优先回避追涨；若必须关注，先等流动性、涨跌幅和可交易状态恢复。",
    }
    long = {
        **common,
        "label": "长期不适合持有" if risk_score >= 70 else "长期缺少正向证据",
        "advice": "长期策略没有足够质量/趋势证据支撑，且存在过滤风险；不建议作为中长期仓位核心。",
    }
    missed = [
        {
            "horizon": horizon,
            "horizon_label": HORIZON_LABELS[horizon],
            "strategy_name": strategy_name,
            "strategy_label": STRATEGY_LABELS.get(strategy_name, strategy_name),
            "reason": "未通过基础股票池风控：{}".format("；".join(risks[:3]) if risks else "数据不足"),
        }
        for horizon in ("short", "long")
        for strategy_name in HORIZON_STRATEGIES[horizon]
    ]
    return {
        "ok": True,
        "filtered": True,
        "code": code,
        "name": str(row.get("name", "")),
        "market": row.get("market", ""),
        "market_label": str(row.get("market_label", "")),
        "price": coerce_number(row.get("price")),
        "pct_chg": coerce_number(row.get("pct_chg")),
        "turnover": coerce_number(row.get("turnover")),
        "volume_ratio": coerce_number(row.get("volume_ratio")),
        "sixty_day_pct": coerce_number(row.get("sixty_day_pct")),
        "ytd_pct": coerce_number(row.get("ytd_pct")),
        "data_source": data_source,
        "market_regime": {
            "label": market_regime.get("label", "未知"),
            "score": market_regime.get("score", 50.0),
            "advice": market_regime.get("advice", ""),
        },
        "horizons": {
            "short": {
                "horizon": "short",
                "label": "短期",
                "prediction": short,
                "strategy_hits": [],
                "missed_strategies": [item for item in missed if item["horizon"] == "short"],
                "consensus": {},
            },
            "long": {
                "horizon": "long",
                "label": "长期",
                "prediction": long,
                "strategy_hits": [],
                "missed_strategies": [item for item in missed if item["horizon"] == "long"],
                "consensus": {},
            },
        },
        "prediction": {
            **common,
            "advice": "该股票没有进入可推荐股票池，直接判定为风险票；主要风险：{}".format("；".join(risks[:5]) if risks else "数据不足"),
        },
        "strategy_hits": [],
        "missed_strategies": missed,
        "consensus": {},
        "risk_flags": risks,
        "blacklist_risk": blacklist_risk,
        "disclaimer": disclaimer or "该结果是风控诊断，不构成投资建议；被过滤股票默认不按推荐策略给正向评分。",
    }


def _find_row(code: str, rows: List[Dict[str, object]]) -> Dict[str, object]:
    for row in rows or []:
        if normalize_code(row.get("code")) == code:
            return row
    return {}


def _strategy_hit(
    strategy_name: str,
    row: Dict[str, object],
    meta: Dict[str, object],
) -> Dict[str, object]:
    profile = row.get("serenity_profile") or {}
    committee = row.get("agent_committee") or {}
    return {
        "strategy_name": strategy_name,
        "strategy_label": STRATEGY_LABELS.get(strategy_name, strategy_name),
        "rank": row.get("rank"),
        "score": coerce_number(row.get("score")),
        "direction_score": _direction_score(row),
        "action": committee.get("final_action_label")
        or profile.get("action_label")
        or row.get("signal_label")
        or "观察",
        "risk_score": coerce_number(profile.get("risk_score"), 50.0),
        "quality_score": coerce_number(profile.get("quality_score"), row.get("score")),
        "confidence_score": coerce_number(profile.get("confidence_score"), 50.0),
        "verdict": row.get("verdict") or {},
        "reasons": list(row.get("reasons") or [])[:4],
        "failure_reasons": list(row.get("failure_reasons") or [])[:4],
        "strategy_version": meta.get("strategy_version", ""),
    }


def _direction_score(row: Dict[str, object]) -> float:
    profile = row.get("serenity_profile") or {}
    committee = row.get("agent_committee") or {}
    base = coerce_number(row.get("score"), 50.0)
    quality = coerce_number(profile.get("quality_score"), base)
    confidence = coerce_number(profile.get("confidence_score"), 50.0)
    risk = coerce_number(profile.get("risk_score"), 50.0)
    agent = coerce_number(committee.get("final_score"), quality)
    score = base * 0.30 + quality * 0.24 + confidence * 0.18 + agent * 0.18 + (100.0 - risk) * 0.10
    return round(max(0.0, min(100.0, score)), 2)


def _prediction_verdict(
    row: Dict[str, object],
    hits: List[Dict[str, object]],
    consensus: Dict[str, object],
    market_regime: Dict[str, object],
    horizon: str = "",
) -> Dict[str, object]:
    if not hits:
        return {
            "direction": "neutral",
            "label": "震荡/不确定",
            "confidence": 22.0,
            "score": 45.0,
            "advice": _horizon_no_hit_advice(horizon),
            "risk_level": "unknown",
        }

    avg_direction = sum(item["direction_score"] for item in hits) / len(hits)
    avg_risk = sum(item["risk_score"] for item in hits) / len(hits)
    appearances = int(consensus.get("appearances") or len(hits))
    consensus_bonus = min(10.0, max(0, appearances - 1) * 4.0)
    regime_score = coerce_number(market_regime.get("score"), 50.0)
    regime_adjust = (regime_score - 50.0) * 0.08
    raw_score = avg_direction + consensus_bonus + regime_adjust - max(0.0, avg_risk - 65.0) * 0.22
    score = round(max(0.0, min(100.0, raw_score)), 2)
    confidence = round(max(0.0, min(100.0, 35.0 + len(hits) * 12.0 + consensus_bonus + (100.0 - avg_risk) * 0.12)), 2)
    risk_level = "high" if avg_risk >= 70 else "medium" if avg_risk >= 50 else "low"
    if score >= 68 and avg_risk < 70:
        direction = "up"
        label = "偏涨" if horizon != "long" else "长期偏强"
        advice = _horizon_up_advice(horizon)
    elif score <= 45 or avg_risk >= 78:
        direction = "down"
        label = "偏弱/回落风险" if horizon != "long" else "长期偏弱"
        advice = _horizon_down_advice(horizon)
    else:
        direction = "neutral"
        label = "震荡/待确认" if horizon != "long" else "长期待确认"
        advice = _horizon_neutral_advice(horizon)
    return {
        "direction": direction,
        "label": label,
        "confidence": confidence,
        "score": score,
        "risk_level": risk_level,
        "avg_risk": round(avg_risk, 2),
        "appearances": appearances,
        "advice": advice,
    }


def _horizon_no_hit_advice(horizon: str) -> str:
    if horizon == "long":
        return "长期策略未给出有效正向信号，暂不作为中长期持仓候选。"
    if horizon == "short":
        return "短期策略未给出有效正向信号，不建议仅凭代码追短线。"
    return "未被当前策略选中，不建议仅凭代码操作。"


def _horizon_up_advice(horizon: str) -> str:
    if horizon == "long":
        return "长期策略给出正向信号，可进入中长期观察池；仍需结合基本面、仓位和回撤纪律。"
    if horizon == "short":
        return "短期策略给出正向信号，可加入短线观察；若已大涨或风险偏高，只适合小仓/等待回踩。"
    return "多策略给出正向信号，可加入观察；若已大涨或风险偏高，只适合小仓/等待回踩。"


def _horizon_down_advice(horizon: str) -> str:
    if horizon == "long":
        return "长期趋势、质量或风险收益比不足，不适合作为中长期持仓核心。"
    if horizon == "short":
        return "短期信号不足或回落风险较高，优先回避追高。"
    return "当前策略胜率信号不足或风险过高，优先回避追高。"


def _horizon_neutral_advice(horizon: str) -> str:
    if horizon == "long":
        return "长期存在部分正向因素，但趋势质量或策略共识不够强，等待更明确的中期确认。"
    if horizon == "short":
        return "短期存在部分正向因素，但共识或风险收益比不够强，等待确认。"
    return "存在部分正向因素，但策略共识或风险收益比不够强，等待确认。"


def _filter_risks(row: Dict[str, object]) -> List[str]:
    code = normalize_code(row.get("code"))
    name = str(row.get("name", "") or "")
    price = coerce_number(row.get("price"))
    pct = coerce_number(row.get("pct_chg"))
    turnover = coerce_number(row.get("turnover"))
    high = coerce_number(row.get("high"))
    low = coerce_number(row.get("low"))
    sixty = coerce_number(row.get("sixty_day_pct"))
    ytd = coerce_number(row.get("ytd_pct"))
    market = row.get("market") or market_type(code)
    risks = []
    if not is_supported_code(code):
        risks.append("非当前支持市场代码")
    if "ST" in name.upper() or "退" in name:
        risks.append("ST/退市风险标记")
    if price <= 0:
        risks.append("无有效现价，可能停牌或行情异常")
    if turnover < config.MIN_TURNOVER:
        risks.append("成交额不足，流动性弱")
    if pct <= -8:
        risks.append("当日跌幅过大，短期抛压强")
    if pct > config.MAX_RECOMMENDED_GAIN:
        risks.append("当日涨幅过高，追高风险")
    if market in ("chinext", "star") and pct > config.MAX_BUYABLE_GAIN_GROWTH:
        risks.append("创业/科创涨幅接近风控上限，买入性价比差")
    if market not in ("chinext", "star") and pct > config.MAX_BUYABLE_GAIN_MAIN:
        risks.append("主板涨幅接近风控上限，买入性价比差")
    if high > 0 and high == low and pct > 8:
        risks.append("疑似一字板或流动性不可交易")
    if sixty > 90:
        risks.append("60日涨幅过高，阶段涨幅透支")
    if ytd > 150:
        risks.append("年内涨幅过高，长期回撤风险大")
    if not risks:
        risks.append("未进入推荐池，策略正向证据不足")
    return risks


def _filter_risk_score(risks: List[str]) -> float:
    score = 35.0 + len(risks) * 10.0
    high_risk_keywords = (
        "ST", "退市", "停牌", "无有效现价", "一字板", "流动性", "跌幅过大",
        "实时行情源", "历史行情", "数据不足",
    )
    if any(any(keyword in risk for keyword in high_risk_keywords) for risk in risks):
        score += 20.0
    if any("涨幅过高" in risk or "透支" in risk for risk in risks):
        score += 12.0
    return round(max(0.0, min(100.0, score)), 2)


def _market_label(market: str) -> str:
    if market == "chinext":
        return "创业板"
    if market == "star":
        return "科创板"
    return "主板"
