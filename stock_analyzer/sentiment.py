from datetime import datetime
from typing import Dict, Iterable, List

import pandas as pd

from .normalization import safe_datetime


POSITIVE_WORDS = {
    "中标": 10,
    "签订合同": 9,
    "订单": 7,
    "业绩预增": 10,
    "利润增长": 9,
    "回购": 7,
    "增持": 8,
    "并购": 7,
    "重组": 8,
    "突破": 6,
    "创新高": 7,
    "国产替代": 6,
    "人工智能": 5,
    "算力": 5,
    "芯片": 5,
    "新能源": 4,
    "机器人": 5,
    "低空经济": 5,
    "商业航天": 5,
    "涨停": 4,
}

NEGATIVE_WORDS = {
    "减持": -10,
    "立案": -12,
    "调查": -9,
    "处罚": -10,
    "亏损": -9,
    "业绩预亏": -12,
    "下滑": -7,
    "诉讼": -8,
    "风险": -6,
    "问询函": -7,
    "监管函": -8,
    "退市": -15,
    "债务": -8,
    "解禁": -6,
    "质押": -5,
}

RISK_WORDS = {"立案", "处罚", "退市", "业绩预亏", "减持", "问询函", "监管函"}


def score_news_items(items: Iterable[Dict[str, str]]) -> Dict[str, object]:
    scored_items = []
    total = 0.0
    total_weight = 0.0
    trigger_words: Dict[str, int] = {}
    now = datetime.now()

    for item in items:
        text = "{} {}".format(item.get("title", ""), item.get("content", ""))
        raw_score = 0
        local_triggers = []
        for word, weight in POSITIVE_WORDS.items():
            if word in text:
                raw_score += weight
                local_triggers.append(word)
                trigger_words[word] = trigger_words.get(word, 0) + 1
        for word, weight in NEGATIVE_WORDS.items():
            if word in text:
                raw_score += weight
                local_triggers.append(word)
                trigger_words[word] = trigger_words.get(word, 0) + 1

        age_weight = _age_weight(item.get("publish_time", ""), now)
        weighted = raw_score * age_weight
        total += weighted
        total_weight += age_weight if raw_score != 0 else 0.15
        scored = dict(item)
        scored["raw_score"] = raw_score
        scored["weighted_score"] = round(weighted, 2)
        scored["trigger_words"] = local_triggers
        scored_items.append(scored)

    if not scored_items:
        sentiment_score = 50.0
    else:
        base = 50.0
        normalized = total / max(total_weight, 1.0)
        sentiment_score = max(0.0, min(100.0, base + normalized * 3.0))

    risk_hits = sorted([word for word in trigger_words if word in RISK_WORDS])
    return {
        "score": round(sentiment_score, 2),
        "trigger_words": sorted(trigger_words, key=trigger_words.get, reverse=True)[:12],
        "risk_words": risk_hits,
        "items": scored_items[:20],
        "summary": _summary(sentiment_score, trigger_words, risk_hits),
    }


def score_stock_sentiment(provider, code: str, name: str = "") -> Dict[str, object]:
    items = provider.get_stock_news(code, name=name, limit=20)
    return score_news_items(items)


def build_market_sentiment_index(news_items: List[Dict[str, str]]) -> Dict[str, object]:
    result = score_news_items(news_items)
    return {
        "score": result["score"],
        "trigger_words": result["trigger_words"],
        "risk_words": result["risk_words"],
        "news_count": len(news_items),
    }


def _age_weight(value: str, now: datetime) -> float:
    ts = safe_datetime(value)
    if ts is None:
        return 0.35
    if isinstance(ts, pd.Timestamp):
        ts = ts.to_pydatetime()
    hours = max(0.0, (now - ts).total_seconds() / 3600.0)
    if hours <= 2:
        return 1.0
    if hours <= 8:
        return 0.75
    if hours <= 24:
        return 0.45
    if hours <= 72:
        return 0.2
    return 0.08


def _summary(score: float, trigger_words: Dict[str, int], risk_hits: List[str]) -> str:
    if risk_hits:
        return "命中风险词: {}".format("、".join(risk_hits[:5]))
    top_words = sorted(trigger_words, key=trigger_words.get, reverse=True)[:4]
    if score >= 65 and top_words:
        return "舆情偏正面: {}".format("、".join(top_words))
    if score <= 40 and top_words:
        return "舆情偏负面: {}".format("、".join(top_words))
    if top_words:
        return "舆情中性偏活跃: {}".format("、".join(top_words))
    return "暂无明显舆情信号"
