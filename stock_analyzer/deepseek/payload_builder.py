from __future__ import annotations

import json
from typing import Callable, Dict, List, Sequence

from ..normalization import coerce_number


class PayloadBuilder:
    """Builds compact DeepSeek request payloads and chat messages."""

    def __init__(
        self,
        strategy_context: Callable[[str], Dict[str, str]],
        loss_factors: Sequence[str],
        profit_factors: Sequence[str],
    ) -> None:
        self._strategy_context = strategy_context
        self._loss_factors = tuple(loss_factors or ())
        self._profit_factors = tuple(profit_factors or ())

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

    def request_payload(
        self,
        strategy_name: str,
        candidates: List[Dict[str, object]],
        market_filter: str,
    ) -> List[Dict[str, object]]:
        return [
            {
                "code": row.get("code", ""),
                "name": row.get("name", ""),
                "score": self.payload_number(row.get("score"), 0.0, 0),
                "pct_chg": self.payload_number(row.get("pct_chg"), 0.0, 1),
                "speed": self.payload_number(row.get("speed"), 0.0, 1),
                "volume_ratio": self.payload_number(row.get("volume_ratio"), 0.0, 1),
                "turnover_rate": self.payload_number(row.get("turnover_rate"), 0.0, 1),
                "turnover": self.payload_number(row.get("turnover"), 0.0, 0),
                "amplitude": self.payload_number(row.get("amplitude"), 0.0, 1),
                "sixty_day_pct": self.payload_number(row.get("sixty_day_pct"), 0.0, 1),
                "ret_5d": self.payload_number(row.get("ret_5d"), 0.0, 1),
                "ret_10d": self.payload_number(row.get("ret_10d"), 0.0, 1),
                "ret_20d": self.payload_number(row.get("ret_20d"), 0.0, 1),
                "ma20_gap": self.payload_number(row.get("ma20_gap"), 0.0, 1),
                "vol_amount_5d": self.payload_number(row.get("vol_amount_5d"), 0.0, 1),
                "breakout_20d": bool(row.get("breakout_20d")),
                "volatility_20d": self.payload_number(row.get("volatility_20d"), 0.0, 1),
                "liquidity_score": self.payload_number(row.get("liquidity_score"), 0.0, 0),
                "momentum_score": self.payload_number(row.get("momentum_score"), 0.0, 0),
                "trend_score": self.payload_number(row.get("trend_score"), 0.0, 0),
                "historical_edge_score": self.payload_number(row.get("historical_edge_score"), 0.0, 0),
                "execution_score": self.payload_number(row.get("execution_score"), 0.0, 0),
                "tail_setup_score": self.payload_number(row.get("tail_setup_score"), 0.0, 0),
                "risk_penalty": self.payload_number(row.get("risk_penalty"), 0.0, 0),
                "risk_penalty_parts": row.get("risk_penalty_parts", {}),
                "overheat_damp": self.payload_number(row.get("overheat_damp"), 1.0, 2),
                "failure_reasons": self.payload_strings(row.get("failure_reasons"), 3),
                "market": str(row.get("market", "")),
                "theme": str(row.get("theme", "")),
                "reasons": self.payload_strings(row.get("reasons"), 4),
                "recent_news": self.payload_news(row.get("recent_news")),
                "announcement_flags": self.payload_strings(row.get("announcement_flags"), 5),
                "news_sentiment": self.payload_news_sentiment(row.get("news_sentiment")),
            }
            for row in candidates
            if isinstance(row, dict)
        ]

    def build_messages(
        self,
        strategy_name: str,
        candidates: List[Dict[str, object]],
        market_filter: str,
    ) -> List[Dict[str, object]]:
        context = self._strategy_context(strategy_name)
        return [
            {
                "role": "system",
                "content": "你是A股研究助手。请只输出 JSON，不要解释、不要 Markdown。"
                "输出必须包含 results 数组，每个元素包含 code、llm_score(0-100)、horizon_up_score(0-100)、action、veto、penalty、reason、risk_flags。"
                "可选字段 event_type、sentiment(-2~2)、catalyst_strength(0-100)、time_sensitivity、already_priced_in、catalyst_score、"
                "theme_truth_score、event_risk_score（都是0-100，except sentiment/flag）。"
                "action 只能是 priority、watch、avoid；penalty 是0-30扣分；risk_flags 是字符串数组，最多3项。",
            },
            {
                "role": "user",
                "content": (
                    "策略: {strategy}\n"
                    "策略周期: {horizon}\n"
                    "复核重点: {focus}\n"
                    "市场: {market}，仅聚焦A股（主板/创业板/科创板）\n"
                    "请重点做风险复核和反推荐，不要直接替代本地量化分数。\n"
                    "先输出结构化事件字段，再输出复核判断：\n"
                    "event_type（业绩/订单/政策/并购/涨价/监管/传闻/未知）、sentiment(-2~2)、"
                    "catalyst_strength(0-100)、time_sensitivity(今天/明天/2-5天/长期)、already_priced_in(true/false)。\n"
                    "horizon_up_score 表示策略主周期内上涨/跑赢倾向；如果看起来强但容易回落，请提高 penalty 或 action=avoid。\n"
                    "短周期亏钱因素必须逐项考虑: {loss_factors}\n"
                    "短周期赚钱因素必须逐项考虑: {profit_factors}\n"
                    "主题类策略必须判断 theme_truth_score；如果 recent_news 没有具体标题依据，必须视为题材待证实并降低 theme_truth_score。\n"
                    "announcement_flags/news_sentiment 是真实新闻与事件输入，减持、解禁、质押、问询函、监管函等风险命中时提高 event_risk_score 和 penalty。\n"
                    "如果亏钱因素明显多于赚钱因素，必须 action=avoid 或提高 penalty；如果赚钱因素多但存在追高风险，action=watch。\n"
                    "输出 JSON 示例: {{\"results\":[{{\"code\":\"600519\",\"llm_score\":87.4,\"horizon_up_score\":74,\"action\":\"watch\","
                    "\"veto\":false,\"penalty\":8,\"reason\":\"...\",\"risk_flags\":[\"涨幅透支\"],\"event_type\":\"业绩\","
                    "\"sentiment\":1,\"catalyst_strength\":78,\"time_sensitivity\":\"明天\",\"already_priced_in\":false,"
                    "\"catalyst_score\":55,\"theme_truth_score\":50,\"event_risk_score\":35}}]}}\n"
                    "候选池: {pool}".format(
                        strategy=strategy_name,
                        horizon=context["horizon"],
                        focus=context["focus"],
                        market=market_filter,
                        loss_factors="；".join(self._loss_factors),
                        profit_factors="；".join(self._profit_factors),
                        pool=json.dumps(
                            self.request_payload(strategy_name, candidates, market_filter),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    )
                ),
            },
        ]

    def build_batch_messages(self, request_input: Dict[str, object]) -> List[Dict[str, str]]:
        return [
            {
                "role": "system",
                "content": (
                    "你是A股短线多策略复核器。请只输出 JSON，不要 Markdown。"
                    "一次处理多个策略，必须按策略分别返回 results。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "请对输入中的每个策略候选池做复核。输出格式必须是 "
                    "{\"strategies\":{\"short_term\":{\"results\":[...]},\"tomorrow_picks\":{\"results\":[...]},\"swing_picks\":{\"results\":[...]}}}。"
                    "每个 result 字段同单策略复核: code、llm_score、horizon_up_score、action、veto、penalty、reason、risk_flags、"
                    "event_type、sentiment、catalyst_strength、time_sensitivity、already_priced_in、catalyst_score、theme_truth_score、event_risk_score。"
                    "只评价输入候选，不新增股票；优先识别追高、流动性、事件风险和催化剂真实性。"
                    "输入: "
                    + json.dumps(request_input, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                ),
            },
        ]
