from __future__ import annotations

import json
from typing import Dict, List

from ..normalization import coerce_number
from .feature_schema import FEATURE_SCHEMA_VERSION, candidate_feature_input, prompt_version, strategy_contract


class PayloadBuilder:
    """Builds compact DeepSeek request payloads and chat messages."""

    def payload_number(self, value, default: float = 0.0, digits: int = 1):
        number = coerce_number(value, default)
        if abs(number) >= 1000000:
            return int(round(number))
        if digits <= 0:
            return int(round(number))
        return round(number, digits)

    def payload_strings(self, values, limit: int = 4) -> List[str]:
        result = []
        for value in values or []:
            text = str(value or "").strip()
            if text and text not in result:
                result.append(text[:40])
            if len(result) >= limit:
                break
        return result

    def payload_news(self, items) -> List[Dict[str, object]]:
        compact = []
        for item in (items or [])[:3]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or item.get("content") or "").strip()[:60]
            if not title:
                continue
            compact.append(
                {
                    "title": title,
                    "source": str(item.get("source") or "")[:20],
                    "time": str(item.get("publish_time") or item.get("time") or "")[:16],
                }
            )
        return compact

    def payload_news_sentiment(self, payload) -> Dict[str, object]:
        if not isinstance(payload, dict):
            return {}
        return {
            "score": self.payload_number(payload.get("score"), 50.0, 0),
            "risk_words": self.payload_strings(payload.get("risk_words"), 4),
            "trigger_words": self.payload_strings(payload.get("trigger_words"), 4),
        }

    def feature_request_payload(self, strategy_name, candidates, market_filter, *, cutoff_at, snapshot_id):
        strategy = str(strategy_name or "")
        return {
            "schema_version": FEATURE_SCHEMA_VERSION,
            "prompt_version": prompt_version(strategy),
            "strategy": strategy,
            "contract": strategy_contract(strategy),
            "market_filter": market_filter,
            "cutoff_at": cutoff_at,
            "snapshot_id": snapshot_id,
            "candidates": [candidate_feature_input(row, cutoff_at) for row in candidates or [] if isinstance(row, dict)],
        }

    def build_feature_messages(self, strategy_name, candidates, market_filter, *, cutoff_at, snapshot_id):
        request = self.feature_request_payload(strategy_name, candidates, market_filter, cutoff_at=cutoff_at, snapshot_id=snapshot_id)
        contract = request["contract"]
        strategy_label = contract["label"]
        strategy_horizon = contract["horizon"]
        strategy_focus = contract["focus"]
        return [
            {
                "role": "system",
                "content": (
                    "你是A股五维点时研究结构化器，只能使用输入字段，不得补造现金流、主力资金、政策或公司事实；不得新增股票，不输出目标价、llm_score、排名或交易动作。"
                    "所有输出字段必须是JSON可解析值，部分缺失时该维必须填unknown/[]/false，不得推断补齐。"
                    "若证据为空或无法判定，必须 abstain=true；否则需给出 evidence_ids 且为输入子集。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"策略={strategy_label}；周期={strategy_horizon}；重点={strategy_focus}。"
                    "请对每只股票在五维框架下给出一次事件判断：价值质量、财务健康、市场资金、行业政策、综合风险。"
                    "输出必须为JSON对象，结果放在results数组，且每只股票只出现一次。"
                    "【必须字段】code,event_type,event_direction,event_strength,event_reliability,novelty,priced_in,time_horizon,overnight_risk,"
                    "regulatory_risk,theme_truth,uncertainty,abstain,evidence_ids,risk_flags,reason。"
                    "【五维结构】value_quality/financial_health/market_flow/industry_policy/risk_assessment/horizon_support。"
                    "value_quality字段：assessment枚举为positive/neutral/negative/unknown，外加confidence和flags。"
                    "financial_health字段：profit_trend枚举为improving/stable/deteriorating/unknown，cashflow_trend枚举为improving/stable/deteriorating/unknown，外加confidence和flags。"
                    "market_flow字段：flow_health枚举为healthy/neutral/unhealthy/unknown，price_flow_divergence为布尔，外加confidence和flags。"
                    "industry_policy字段：industry_outlook枚举为growing/stable/contracting/unknown，policy_relevance枚举为direct/indirect/none/unknown，外加confidence和flags。"
                    "risk_assessment字段：risk_level枚举为low/medium/high/unknown，外加confidence和flags。"
                    "horizon_support包含today/next_day/2_5d，所有confidence和horizon_support为0-100数值。"
                    "event_type可选项：业绩/订单/政策/并购/重组/涨价/监管/减持/解禁/诉讼/传闻/行业/其他/未知；"
                    "time_horizon可选项：today/next_day/2_5d/long_term/unknown。"
                    "abstain必须是JSON布尔值； evidence_ids只能是输入evidence中已有的evidence_id。"
                    "强约束："
                    "1) 未提供现金流数据时financial_health.cashflow_trend=unknown；"
                    "2) 未提供主力资金数据时market_flow.flow_health=unknown且price_flow_divergence=false；"
                    "3) policy evidence为0时industry_policy.policy_relevance=unknown；"
                    "4) 输入中无可判定事实时可设abstain=true并可将reason写明。"
                    "输入载荷="
                    + json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                ),
            },
        ]
