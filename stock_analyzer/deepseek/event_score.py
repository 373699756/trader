from __future__ import annotations

from typing import Dict

from ..normalization import coerce_number


TIME_SENSITIVITY_MAP = {
    "today": "今天",
    "明日": "明天",
    "next day": "明天",
    "tomorrow": "明天",
    "2-5": "2-5天",
    "2_5": "2-5天",
    "long": "长期",
    "longterm": "长期",
    "long-term": "长期",
    "3days": "2-5天",
    "1-3天": "2-5天",
}


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "是", "对"}


def coerce_already_priced_in(value) -> bool:
    return coerce_bool(value)


def coerce_sentiment(value: object) -> float:
    return clamp(coerce_number(value, 0.0), -2.0, 2.0)


def coerce_catalyst_strength(value: object) -> float:
    return clamp(coerce_number(value, 50.0), 0.0, 100.0)


def coerce_time_sensitivity(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "长期"
    normalized = text.lower().replace("_", "-").replace("，", ",")
    return TIME_SENSITIVITY_MAP.get(normalized, text)


def event_fit_score(strategy_name: str, item: Dict[str, object]) -> float:
    time_sensitivity = coerce_time_sensitivity(item.get("time_sensitivity"))
    desired = {
        "today_term": {"今天", "明天"},
        "today_picks": {"今天", "明天"},
        "tomorrow_picks": {"明天"},
        "swing_picks": {"2-5天"},
        "swing_2_5d_picks": {"2-5天"},
    }.get(strategy_name, set())
    if not desired:
        return 50.0
    if time_sensitivity in desired:
        return 100.0
    if strategy_name in {"swing_picks", "swing_2_5d_picks"} and time_sensitivity == "明天":
        return 70.0
    if strategy_name in {"today_term", "today_picks", "tomorrow_picks"} and time_sensitivity == "2-5天":
        return 65.0
    return 35.0


def deepseek_event_score(strategy_name: str, item: Dict[str, object]) -> float:
    sentiment_score = (coerce_sentiment(item.get("sentiment")) + 2.0) * 25.0
    catalyst_strength = coerce_catalyst_strength(item.get("catalyst_strength"))
    catalyst_score = clamp(coerce_number(item.get("catalyst_score"), catalyst_strength), 0.0, 100.0)
    event_risk = clamp(coerce_number(item.get("event_risk_score"), 50.0), 0.0, 100.0)
    fit_score = event_fit_score(strategy_name, item)
    score = (
        catalyst_strength * 0.34
        + catalyst_score * 0.20
        + sentiment_score * 0.18
        + fit_score * 0.18
        + (100.0 - event_risk) * 0.10
    )
    if coerce_already_priced_in(item.get("already_priced_in")):
        score -= 12.0
    return round(clamp(score, 0.0, 100.0), 2)


def deepseek_event_adjustment(strategy_name: str, item: Dict[str, object]) -> Dict[str, float]:
    event_score = deepseek_event_score(strategy_name, item)
    bonus = max(0.0, (event_score - 58.0) * 0.12)
    penalty = max(0.0, (45.0 - event_score) * 0.18)
    if coerce_already_priced_in(item.get("already_priced_in")):
        penalty += 2.0
    sentiment = coerce_sentiment(item.get("sentiment"))
    if sentiment < 0:
        penalty += abs(sentiment) * 1.5
    return {"event_score": event_score, "bonus": round(bonus, 2), "penalty": round(penalty, 2)}

