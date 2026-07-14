from __future__ import annotations

from typing import Dict, List

import pandas as pd

from .. import config
from ..event_risk import row_event_risk
from ..normalization import coerce_number, percentile_score
from ..risk_blacklist import row_blacklist_risk
from .weights import THRESHOLDS, WEIGHTS


TRADING_AGENTS_REFERENCE = {
    "repo": "TauricResearch/TradingAgents",
    "url": "https://github.com/TauricResearch/TradingAgents",
    "adopted": "借鉴分析师团队、牛熊研究辩论、交易员、风控和组合经理的分层决策流",
}

PROFILE_COMPONENTS = (
    ("momentum_score", "动量"),
    ("trend_score", "趋势"),
    ("liquidity_score", "流动性"),
    ("execution_score", "买入安全"),
    ("theme_score", "主题"),
    ("sentiment_score", "舆情"),
    ("industry_score", "行业"),
    ("not_overextended_score", "不过热"),
    ("quality_proxy_score", "质量代理"),
    ("fundamental_quality_score", "基本面质量"),
    ("fundamental_value_score", "估值"),
    ("earnings_surprise_score", "业绩超预期"),
    ("early_trend_score", "启动趋势"),
)


__all__ = [
    "mark_backup_watch",
    "mark_tomorrow_backup_watch",
    "_agent_bear_cases",
    "_agent_bull_cases",
    "_append_unique_reason",
    "_attach_signal_explanation",
    "_build_agent_committee",
    "_build_long_term_reasons",
    "_build_position_reasons",
    "_build_reasons",
    "_build_serenity_profile",
    "_build_swing_reasons",
    "_build_tech_potential_reasons",
    "_build_tomorrow_reasons",
    "_chase_risk",
    "_data_coverage",
    "_decision_score",
    "_exit_action",
    "_failure_reasons",
    "_mark_tomorrow_intraday_watch",
    "_overextension_risk",
    "_sell_risk",
    "_trade_action",
    "_unique_strings",
    "_verdict_tier",
    "_weighted_score",
    "_with_regime_reason",
]


def _append_unique_reason(row: Dict[str, object], reason: str) -> None:
    text = str(reason or "").strip()
    if not text:
        return
    reasons = list(row.get("reasons") or [])
    if text not in reasons:
        reasons.append(text)
    row["reasons"] = reasons[:8]


def mark_backup_watch(row: Dict[str, object], label: str = "备选观察", reason: str = "") -> None:
    row["tier"] = "backup_pool"
    row["tier_label"] = label
    row["execution_allowed"] = False
    row["recommendation_class"] = "backup"
    row["recommendation_class_label"] = label
    row["profit_window"] = "不执行"
    row["trade_action"] = {
        "action": "watch_only",
        "label": "只观察",
        "position_size": 0.0,
        "reason": "{}不形成可执行买入指令。".format(label),
    }
    row["exit_action"] = {
        "action": "wait_confirmation",
        "label": "等待确认",
        "reason": "当前仅作观察，等待可执行信号。",
    }
    committee = dict(row.get("agent_committee") or {})
    committee["stance"] = "wait"
    committee["final_action_label"] = label
    row["agent_committee"] = committee
    profile = dict(row.get("serenity_profile") or {})
    profile["level"] = "neutral"
    profile["action_label"] = label
    evidence = []
    for item in profile.get("evidence") or []:
        next_item = dict(item)
        if str(next_item.get("label") or "").startswith("Agent委员会:"):
            next_item["label"] = "Agent委员会:{}".format(label)
            next_item["level"] = "neutral"
        evidence.append(next_item)
    profile["evidence"] = evidence
    row["serenity_profile"] = profile
    verdict = dict(row.get("verdict") or {})
    verdict["tier"] = "watch"
    verdict["label"] = label
    verdict["note"] = "{}不形成可执行推荐".format(label)
    row["verdict"] = verdict
    if reason:
        _append_unique_reason(row, reason)


def mark_tomorrow_backup_watch(
    row: Dict[str, object],
    label: str = "备选观察",
    reason: str = "",
) -> None:
    mark_backup_watch(row, label=label, reason=reason)


def _mark_tomorrow_intraday_watch(row: Dict[str, object]) -> None:
    mark_tomorrow_backup_watch(row, label="盘中观察")
    row["observation_mode"] = "intraday_provisional"
    row["signal_label"] = "盘中观察"
    row["holding_discipline"] = "盘中候选不执行，14:30 后重新确认"
    row["trade_action"]["reason"] = "14:30 前为盘中候选，等待尾盘确认后再决定。"
    row["exit_action"]["reason"] = "盘中候选尚未形成尾盘信号。"
    _append_unique_reason(row, "14:30 前仅盘中观察，不作为重点推荐")
    row["prediction_type"] = "rank_score"
    row["score_note"] = "盘中综合分仅用于候选排序，不是上涨概率或交易指令。"


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
    event_risk = row_event_risk(row)
    if event_risk.get("flags"):
        failure_reasons.extend("事件风险:{}".format(flag.get("label", "")) for flag in event_risk["flags"][:3])
    blacklist_risk = row_blacklist_risk(row)
    if blacklist_risk.get("flags"):
        failure_reasons.extend("黑名单风险:{}".format(flag.get("label", "")) for flag in blacklist_risk["flags"][:3])
    item.update(
        {
            "strategy_name": strategy_name,
            "strategy_label": strategy_label,
            "signal_label": signal_label,
            "chase_risk": chase_risk,
            "overextension": overextension,
            "failure_reasons": failure_reasons,
            "event_risk": event_risk,
            "blacklist_risk": blacklist_risk,
        }
    )
    market_cap = coerce_number(row.get("market_cap"), None)
    if market_cap and market_cap > 0:
        item["market_cap"] = round(market_cap, 2)
    float_market_cap = coerce_number(row.get("float_market_cap"), None)
    if float_market_cap and float_market_cap > 0 and "float_market_cap" not in item:
        item["float_market_cap"] = round(float_market_cap, 2)
    item["agent_committee"] = _build_agent_committee(item, row)
    profile = _build_serenity_profile(item, row)
    item["serenity_profile"] = profile
    item["decision_score"] = _decision_score(item, profile)
    item["sell_risk"] = _sell_risk(item, row, profile)
    item["trade_action"] = _trade_action(item, profile)
    item["exit_action"] = _exit_action(item, profile)

    committee = item["agent_committee"]
    item["bull_score"] = round(coerce_number(committee.get("bull_researcher_score"), 50.0), 2)
    item["bear_score"] = round(coerce_number(committee.get("bear_researcher_score"), 50.0), 2)

    item["verdict"] = _verdict_tier(
        item.get("decision_score", item.get("score")),
        profile.get("risk_score"),
        coerce_number(profile.get("data_coverage"), 0.0),
    )
    return item


def _verdict_tier(score: float, risk_score: float, data_coverage: float) -> Dict[str, object]:
    t = THRESHOLDS["verdict"]
    score = max(0.0, min(100.0, coerce_number(score)))
    risk_score = max(0.0, min(100.0, coerce_number(risk_score)))
    low_coverage = data_coverage < THRESHOLDS["min_data_coverage"]

    if score >= t["strong_buy"] and risk_score < 60:
        tier, label = "strong_buy", "强烈关注"
    elif score >= t["buy"] and risk_score < 68:
        tier, label = "buy", "关注"
    elif score >= t["watch"]:
        if risk_score >= 70:
            tier, label = "reduce", "谨慎"
        elif score >= 60 and risk_score <= 48:
            tier, label = "watch", "观察(偏多)"
        elif score < 56 or risk_score >= 60:
            tier, label = "watch", "观察(偏空)"
        else:
            tier, label = "watch", "观察"
    elif score >= t["reduce"]:
        tier, label = "reduce", "谨慎"
    else:
        tier, label = "avoid", "回避"

    if risk_score >= 80 and tier in ("strong_buy", "buy"):
        tier, label = "reduce", "谨慎"

    note = ""
    if low_coverage and tier in ("strong_buy", "buy"):
        tier, label, note = "watch", "观察(因子不足)", "历史因子覆盖不足，评级降级"
    elif low_coverage:
        note = "历史因子覆盖不足"

    return {
        "tier": tier,
        "label": label,
        "score": round(score, 2),
        "risk_score": round(risk_score, 2),
        "data_coverage": round(data_coverage, 2),
        "note": note,
    }


def _with_regime_reason(
    item: Dict[str, object],
    market_regime: Dict[str, object],
    regime_bonus: float,
) -> Dict[str, object]:
    if not market_regime:
        return item
    reasons = list(item.get("reasons", []))
    if regime_bonus >= 2.5:
        reasons.insert(0, "{}环境顺风".format(market_regime.get("label", "当前")))
    elif regime_bonus <= -2.5:
        reasons.append("{}环境下需谨慎".format(market_regime.get("label", "当前")))
    item["reasons"] = reasons[:6]
    return item


def _build_agent_committee(item: Dict[str, object], row: pd.Series) -> Dict[str, object]:
    chase_risk = item.get("chase_risk") or {}
    overextension = item.get("overextension") or {}
    risk_penalty = max(0.0, coerce_number(item.get("risk_penalty")))
    event_penalty = coerce_number((item.get("event_risk") or {}).get("penalty"))
    blacklist_penalty = coerce_number((item.get("blacklist_risk") or {}).get("penalty"))
    risk_penalty += event_penalty + blacklist_penalty
    regime_bonus = coerce_number(item.get("regime_bonus"))
    risk_words = list(item.get("risk_words") or [])

    technical_score = _weighted_score(
        (
            (item.get("momentum_score"), 0.28),
            (item.get("trend_score"), 0.24),
            (item.get("execution_score"), 0.18),
            (item.get("early_trend_score"), 0.12),
            (item.get("not_overextended_score"), 0.10),
            (item.get("score"), 0.08),
        ),
        fallback=item.get("score"),
    )
    sentiment_score = max(0.0, min(100.0, coerce_number(item.get("sentiment_score"), 50.0) - len(risk_words) * 8.0))
    fundamentals_proxy_score = _weighted_score(
        (
            (item.get("quality_proxy_score"), 0.32),
            (item.get("industry_score"), 0.20),
            (item.get("theme_score"), 0.18),
            (item.get("liquidity_score"), 0.16),
            (item.get("not_overextended_score"), 0.14),
        ),
        fallback=item.get("score"),
    )
    news_environment_score = max(
        0.0,
        min(
            100.0,
            50.0
            + regime_bonus * 6.0
            - risk_penalty * 1.4
            - coerce_number(chase_risk.get("score")) * 4.0
            - coerce_number(overextension.get("score")) * 3.5,
        ),
    )
    liquidity_score = _weighted_score(
        (
            (item.get("liquidity_score"), 0.76),
            (
                percentile_score(coerce_number(row.get("turnover")), [config.MIN_TURNOVER, config.MIN_TURNOVER * 4]),
                0.24,
            ),
        ),
        fallback=50.0,
    )

    bull_score = _weighted_score(
        (
            (technical_score, 0.34),
            (fundamentals_proxy_score, 0.20),
            (sentiment_score, 0.16),
            (liquidity_score, 0.16),
            (news_environment_score, 0.14),
        ),
        fallback=item.get("score"),
    )
    bear_score = max(
        0.0,
        min(
            100.0,
            coerce_number(chase_risk.get("score")) * 11.5
            + coerce_number(overextension.get("score")) * 10.0
            + risk_penalty * 2.0
            + max(0.0, 50.0 - sentiment_score) * 0.55
            + max(0.0, 50.0 - news_environment_score) * 0.45
            + max(0.0, 55.0 - liquidity_score) * 0.25,
        ),
    )
    trader_score = max(0.0, min(100.0, bull_score * 0.62 + (100.0 - bear_score) * 0.28 + news_environment_score * 0.10))
    risk_score = min(100.0, bear_score + max(0.0, risk_penalty - 8.0) * 1.8)
    portfolio_score = max(
        0.0,
        min(
            100.0,
            trader_score * 0.68
            + liquidity_score * 0.14
            + fundamentals_proxy_score * 0.10
            + news_environment_score * 0.08
            - max(0.0, risk_score - 60.0) * 0.45,
        ),
    )

    if risk_score >= 78:
        action_label = "风控否决"
        stance = "reject"
    elif portfolio_score >= 72 and risk_score <= 48:
        action_label = "组合经理批准"
        stance = "approve"
    elif portfolio_score >= 60 and risk_score <= 62:
        action_label = "交易员小仓试单"
        stance = "small_position"
    else:
        action_label = "等待更多确认"
        stance = "wait"

    bull_cases = _agent_bull_cases(item, technical_score, fundamentals_proxy_score, sentiment_score, liquidity_score)
    bear_cases = _agent_bear_cases(item, risk_score, news_environment_score)
    return {
        "version": "trading_agents_committee_v1",
        "reference": TRADING_AGENTS_REFERENCE["repo"],
        "technical_analyst_score": round(technical_score, 2),
        "sentiment_analyst_score": round(sentiment_score, 2),
        "fundamentals_proxy_score": round(fundamentals_proxy_score, 2),
        "news_environment_score": round(news_environment_score, 2),
        "bull_researcher_score": round(bull_score, 2),
        "bear_researcher_score": round(bear_score, 2),
        "trader_score": round(trader_score, 2),
        "risk_manager_score": round(risk_score, 2),
        "portfolio_manager_score": round(portfolio_score, 2),
        "final_score": round(portfolio_score, 2),
        "final_action_label": action_label,
        "stance": stance,
        "bull_cases": bull_cases[:4],
        "bear_cases": bear_cases[:4],
        "source": "参考 TradingAgents 的分析师、研究辩论、交易员、风控和组合经理分层决策流；本项目使用本地量价/舆情/风险字段确定性计算。",
    }


def _build_serenity_profile(item: Dict[str, object], row: pd.Series) -> Dict[str, object]:
    component_values = []
    evidence = []
    for key, label in PROFILE_COMPONENTS:
        if key not in item:
            continue
        value = coerce_number(item.get(key), 0.0)
        component_values.append(value)
        if value >= 72:
            evidence.append({"label": "{}强".format(label), "score": round(value, 2), "level": "positive"})
        elif value <= 38:
            evidence.append({"label": "{}弱".format(label), "score": round(value, 2), "level": "negative"})

    score = coerce_number(item.get("score"), 0.0)
    regime_bonus = coerce_number(item.get("regime_bonus"), 0.0)
    chase_risk = item.get("chase_risk") or {}
    overextension = item.get("overextension") or {}
    committee = item.get("agent_committee") or {}
    agent_score = coerce_number(committee.get("final_score"), 50.0)
    agent_risk_score = coerce_number(committee.get("risk_manager_score"), 0.0)
    risk_score = min(
        100.0,
        coerce_number(chase_risk.get("score")) * 11.0
        + coerce_number(overextension.get("score")) * 10.0
        + max(0.0, coerce_number(item.get("risk_penalty"))) * 2.1
        + coerce_number((item.get("event_risk") or {}).get("penalty")) * 1.5
        + coerce_number((item.get("blacklist_risk") or {}).get("penalty")) * 1.8
        + max(0.0, -regime_bonus) * 4.0
        + max(0.0, agent_risk_score - 62.0) * 0.35,
    )
    data_coverage = _data_coverage(row)
    confidence_score = min(
        100.0,
        max(
            0.0,
            42.0
            + len([value for value in component_values if value >= 60]) * 7.0
            + data_coverage * 18.0
            + max(0.0, regime_bonus) * 1.6
            + max(0.0, agent_score - 55.0) * 0.22
            - risk_score * 0.18,
        ),
    )
    component_average = sum(component_values) / len(component_values) if component_values else score
    quality_score = min(
        100.0,
        max(
            0.0,
            score * 0.36
            + component_average * 0.25
            + confidence_score * 0.16
            + agent_score * 0.15
            - risk_score * 0.20,
        ),
    )
    committee_stance = committee.get("stance")
    if committee_stance == "reject" or risk_score >= 78:
        action_label = "只观察"
        level = "risk"
    elif quality_score >= 72 and risk_score <= 45 and agent_score >= 66:
        action_label = "优先跟踪"
        level = "good"
    elif risk_score >= 72:
        action_label = "只观察"
        level = "risk"
    elif quality_score >= 60 and agent_score >= 54:
        action_label = "小仓观察"
        level = "watch"
    else:
        action_label = "等待确认"
        level = "neutral"

    risk_reasons = list(chase_risk.get("reasons", [])) + list(overextension.get("reasons", []))
    risk_reasons.extend(committee.get("bear_cases", [])[:3])
    if regime_bonus <= -2.5:
        risk_reasons.append("市场状态逆风")
    event_risk = item.get("event_risk") or {}
    for flag in event_risk.get("flags", [])[:3]:
        risk_reasons.append("事件风险:{}".format(flag.get("label", "")))
    blacklist_risk = item.get("blacklist_risk") or {}
    for flag in blacklist_risk.get("flags", [])[:3]:
        risk_reasons.append("黑名单风险:{}".format(flag.get("label", "")))
    if regime_bonus >= 2.5:
        evidence.insert(0, {"label": "市场状态顺风", "score": round(regime_bonus, 2), "level": "positive"})
    if committee.get("final_action_label"):
        evidence.insert(
            0,
            {
                "label": "Agent委员会:{}".format(committee.get("final_action_label")),
                "score": round(agent_score, 2),
                "level": "positive" if committee_stance in ("approve", "small_position") else "negative",
            },
        )
    for case in committee.get("bull_cases", [])[:2]:
        evidence.append({"label": case, "score": round(agent_score, 2), "level": "positive"})

    return {
        "version": "serenity_profile_v1",
        "quality_score": round(quality_score, 2),
        "risk_score": round(risk_score, 2),
        "confidence_score": round(confidence_score, 2),
        "agent_committee_score": round(agent_score, 2),
        "data_coverage": round(data_coverage, 2),
        "level": level,
        "action_label": action_label,
        "evidence": evidence[:5],
        "risk_reasons": _unique_strings(risk_reasons)[:5],
        "source": "借鉴 Serenity 系列库的结构化证据与 TradingAgents 的多角色投研决策流。",
    }


def _weighted_score(pairs, fallback: object = 50.0) -> float:
    total = 0.0
    weight_total = 0.0
    for value, weight in pairs:
        if value is None:
            continue
        num = coerce_number(value)
        if not pd.notna(num):
            continue
        total += max(0.0, min(100.0, num)) * weight
        weight_total += weight
    if weight_total <= 0:
        return max(0.0, min(100.0, coerce_number(fallback, 50.0)))
    return max(0.0, min(100.0, total / weight_total))


def _agent_bull_cases(
    item: Dict[str, object],
    technical_score: float,
    fundamentals_proxy_score: float,
    sentiment_score: float,
    liquidity_score: float,
) -> List[str]:
    cases: List[str] = []
    if technical_score >= 68:
        cases.append("技术分析师支持：趋势/动量组合较强")
    if fundamentals_proxy_score >= 62:
        cases.append("基本面代理支持：主题/行业/稳健代理分较好")
    if sentiment_score >= 60:
        cases.append("情绪分析师支持：舆情或热度偏正面")
    if liquidity_score >= 65:
        cases.append("交易员支持：流动性足，便于执行")
    if coerce_number(item.get("regime_bonus")) >= 2.5:
        cases.append("新闻环境支持：当前市场状态顺风")
    return cases or ["牛方暂无强证据"]


def _agent_bear_cases(item: Dict[str, object], risk_score: float, news_environment_score: float) -> List[str]:
    cases: List[str] = []
    cases.extend(str(reason) for reason in (item.get("failure_reasons") or [])[:3])
    if risk_score >= 65:
        cases.append("风控提示：综合风险分偏高")
    if news_environment_score <= 42:
        cases.append("新闻/市场环境偏逆风")
    if item.get("risk_words"):
        cases.append("情绪分析师提示：存在负面关键词")
    unique: List[str] = []
    for case in cases:
        if case and case not in unique:
            unique.append(case)
    return unique or ["熊方暂无硬性否决项"]


def _decision_score(item: Dict[str, object], profile: Dict[str, object]) -> float:
    committee = item.get("agent_committee") or {}
    base_score = coerce_number(item.get("score"), 0.0)
    execution_score = coerce_number(item.get("execution_score"), 50.0)
    quality_score = coerce_number(profile.get("quality_score"), base_score)
    confidence_score = coerce_number(profile.get("confidence_score"), 50.0)
    committee_score = coerce_number(committee.get("final_score"), 50.0)
    risk_score = coerce_number(profile.get("risk_score"), 50.0)
    weights = WEIGHTS.get("decision_score") or {}
    score = (
        base_score * coerce_number(weights.get("base_score"), 0.32)
        + execution_score * coerce_number(weights.get("execution_score"), 0.20)
        + quality_score * coerce_number(weights.get("quality_score"), 0.18)
        + confidence_score * coerce_number(weights.get("confidence_score"), 0.12)
        + committee_score * coerce_number(weights.get("committee_score"), 0.10)
        + max(0.0, 100.0 - risk_score) * coerce_number(weights.get("risk_guard"), 0.08)
    )
    return round(max(0.0, min(100.0, score)), 2)


def _sell_risk(item: Dict[str, object], row: pd.Series, profile: Dict[str, object]) -> Dict[str, object]:
    reasons: List[str] = []
    score = 8.0
    pct = coerce_number(row.get("pct_chg"))
    speed = coerce_number(row.get("speed"), coerce_number(row.get("five_min_pct")))
    close_location = _close_location(
        coerce_number(row.get("price")),
        coerce_number(row.get("high")),
        coerce_number(row.get("low")),
    )
    risk_score = coerce_number(profile.get("risk_score"), 0.0)
    execution_score = coerce_number(item.get("execution_score"), 50.0)
    volume_ratio = coerce_number(row.get("volume_ratio"))

    if pct >= 7.0:
        score += 26.0
        reasons.append("当日涨幅过高，适合防冲高回落")
    elif pct >= 4.5:
        score += 16.0
        reasons.append("短线已有明显涨幅，注意兑现压力")

    if speed <= -1.2:
        score += 18.0
        reasons.append("盘中转弱，存在回落风险")
    elif speed <= -0.5:
        score += 10.0
        reasons.append("涨速回落，追价性价比下降")

    if close_location < 0.32:
        score += 22.0
        reasons.append("收盘位置偏低，尾盘承接弱")
    elif close_location < 0.45:
        score += 12.0
        reasons.append("尾盘承接一般")

    if risk_score >= 72:
        score += 20.0
        reasons.append("综合风险偏高")
    elif risk_score >= 58:
        score += 10.0
        reasons.append("风险开始抬升")

    if execution_score <= 60:
        score += 10.0
        reasons.append("当前执行性一般")

    if volume_ratio >= 4.5 and pct >= 4.0:
        score += 8.0
        reasons.append("放量冲高，次日分歧概率上升")

    score = max(0.0, min(100.0, score))
    if score >= 65:
        level, label = "high", "高"
    elif score >= 40:
        level, label = "medium", "中"
    else:
        level, label = "low", "低"
    return {
        "score": round(score, 2),
        "level": level,
        "label": label,
        "reasons": reasons[:3],
    }


def _trade_action(item: Dict[str, object], profile: Dict[str, object]) -> Dict[str, object]:
    decision_score = coerce_number(item.get("decision_score"), item.get("score"))
    sell_risk = item.get("sell_risk") or {}
    sell_risk_score = coerce_number(sell_risk.get("score"), 50.0)
    risk_score = coerce_number(profile.get("risk_score"), 50.0)
    confidence = coerce_number(profile.get("confidence_score"), 50.0)
    verdict_tier = str((item.get("verdict") or {}).get("tier") or "")

    action = "watch_only"
    label = "只观察"
    position = 0.0
    reason = "当前信号更适合观察，等待更好的买点。"

    if (
        decision_score >= 78
        and sell_risk_score <= 38
        and risk_score <= 42
        and confidence >= 60
        and verdict_tier in ("strong_buy", "buy", "watch")
    ):
        action = "buy_confirmed"
        label = "确认买入"
        position = 1.0
        reason = "操作分高且风险可控，可按计划仓位执行。"
    elif decision_score >= 68 and sell_risk_score <= 55 and risk_score <= 58:
        action = "buy_small"
        label = "小仓试单"
        position = 0.35
        reason = "信号偏多但仍有波动风险，宜先小仓验证。"
    elif sell_risk_score >= 72 or risk_score >= 72:
        action = "avoid_chase"
        label = "避免追高"
        position = 0.0
        reason = "风险或过热信号偏强，不适合主动追价。"

    return {
        "action": action,
        "label": label,
        "position_size": position,
        "reason": reason,
    }


def _exit_action(item: Dict[str, object], profile: Dict[str, object]) -> Dict[str, object]:
    sell_risk = item.get("sell_risk") or {}
    sell_risk_score = coerce_number(sell_risk.get("score"), 50.0)
    risk_score = coerce_number(profile.get("risk_score"), 50.0)
    decision_score = coerce_number(item.get("decision_score"), item.get("score"))

    action = "hold"
    label = "继续持有"
    reason = "当前未出现明确的减仓或止损信号。"

    if sell_risk_score >= 82 or risk_score >= 80:
        action = "stop_loss"
        label = "止损/退出"
        reason = "风险显著抬升，优先保护本金。"
    elif sell_risk_score >= 68:
        action = "take_profit"
        label = "逢高兑现"
        reason = "短线兑现压力较大，适合主动锁定利润。"
    elif sell_risk_score >= 52 or decision_score < 58:
        action = "trim"
        label = "减仓观察"
        reason = "优势减弱，宜降低仓位继续跟踪。"

    return {
        "action": action,
        "label": label,
        "reason": reason,
    }


def _data_coverage(row: pd.Series) -> float:
    explicit = row.get("alphalite_coverage")
    if explicit is not None:
        return max(0.0, min(1.0, coerce_number(explicit)))
    return 0.0


def _unique_strings(values: List[object]) -> List[str]:
    result: List[str] = []
    for value in values:
        text = str(value)
        if text and text not in result:
            result.append(text)
    return result


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


def _close_location(price: float, high: float, low: float) -> float:
    price = coerce_number(price)
    high = coerce_number(high)
    low = coerce_number(low)
    if price <= 0 or high <= low or low <= 0:
        return 0.5
    return max(0.0, min(1.0, (price - low) / (high - low)))


def _row_speed(row: pd.Series) -> float:
    speed = coerce_number(row.get("speed"))
    if speed != 0:
        return speed
    return coerce_number(row.get("five_min_pct"))


def _build_reasons(row: pd.Series, industry_pct: float, hot_rank, sentiment: Dict[str, object]) -> List[str]:
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
    historical_edge_score: float,
    execution_score: float,
    tail_setup_score: float,
    risk_penalty: float,
) -> List[str]:
    reasons: List[str] = []
    pct = coerce_number(row.get("pct_chg"))
    volume_ratio = coerce_number(row.get("volume_ratio"))
    turnover_rate = coerce_number(row.get("turnover_rate"))
    turnover = coerce_number(row.get("turnover"))
    sixty_day_pct = coerce_number(row.get("sixty_day_pct"))
    amplitude = coerce_number(row.get("amplitude"))
    high = coerce_number(row.get("high"))
    low = coerce_number(row.get("low"))
    price = coerce_number(row.get("price"))
    has_close_range = price > 0 and high > low and low > 0
    close_location = _close_location(price, high, low)
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
    if historical_edge_score >= 68:
        reasons.append("历史量价结构占优")
    elif coerce_number(row.get("alphalite_factor_ready")) > 0 and historical_edge_score < 45:
        reasons.append("历史量价结构偏弱")
    if execution_score >= 75:
        reasons.append("买入安全较好")
    if tail_setup_score >= 72:
        reasons.append("收盘结构适合次日兑现")
    elif has_close_range and close_location < 0.35:
        reasons.append("收盘回落需谨慎")
    if has_close_range and risk_penalty >= 6 and 4.5 <= pct < 7 and close_location < 0.6:
        reasons.append("4-7%涨幅且尾盘承接不足")
    if amplitude >= 9:
        reasons.append("波动偏大")
    if risk_penalty >= 8:
        reasons.append("风险扣分较高")
    if momentum_score >= 70:
        reasons.append("短线动能靠前")
    return reasons[:6] or ["流动性、动量和买入安全综合排名靠前"]


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
        reasons.append("2-5天动量靠前")
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

