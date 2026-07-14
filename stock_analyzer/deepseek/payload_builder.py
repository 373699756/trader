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
        return [
            {
                "role": "system",
                "content": "你是A股五维点时研究结构化器，只能使用输入的财务、行情和evidence，不得补造现金流、主力资金、政策或公司事实。只输出JSON对象results数组，不新增股票，不输出目标价、llm_score、排名或买卖建议；某维数据不足时该维必须unknown，全部证据不足时abstain=true。",
            },
            {
                "role": "user",
                "content": (
                    "策略={label}；周期={horizon}；重点={focus}。对每只股票一次完成价值质量、财务健康、市场资金、行业政策、综合风险五维分析。每项必须含code,event_type,event_direction(-2~2),event_strength,event_reliability,novelty,priced_in,time_horizon,overnight_risk,regulatory_risk,theme_truth,uncertainty,abstain,evidence_ids,risk_flags,reason；并包含value_quality={{assessment:positive|neutral|negative|unknown,confidence,flags}}，financial_health={{profit_trend:improving|stable|deteriorating|unknown,cashflow_trend:improving|stable|deteriorating|unknown,confidence,flags}}，market_flow={{flow_health:healthy|neutral|unhealthy|unknown,price_flow_divergence:true|false,confidence,flags}}，industry_policy={{industry_outlook:growing|stable|contracting|unknown,policy_relevance:direct|indirect|none|unknown,confidence,flags}}，risk_assessment={{risk_level:low|medium|high|unknown,confidence,flags}}，horizon_support={{today,next_day,2_5d}}。horizon_support和confidence为0-100 JSON数字。没有现金流或主力资金数据时对应字段必须unknown。event_type只能是业绩/订单/政策/并购/重组/涨价/监管/减持/解禁/诉讼/传闻/行业/其他/未知；time_horizon只能是today/next_day/2_5d/long_term/unknown；abstain必须是JSON布尔值，evidence_ids必须是输入子集。输入="
                    + json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                ).format(**contract),
            },
        ]
