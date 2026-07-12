"""Independent event/catalyst alpha scoring.

This module deliberately stays separate from price/volume scoring. It converts
structured catalyst events into an alpha score that can later be validated and
blended by an ensemble layer.
"""

from typing import Dict, List

from . import config
from .deepseek.event_score import coerce_already_priced_in, event_fit_score
from .normalization import coerce_number


EVENT_TYPE_WEIGHTS = {
    "earnings_preannounce_up": 30.0,
    "业绩预增": 30.0,
    "业绩": 24.0,
    "policy_beneficiary": 20.0,
    "政策受益": 20.0,
    "政策": 18.0,
    "major_order": 18.0,
    "大额订单": 18.0,
    "订单": 16.0,
    "buyback": 12.0,
    "回购": 12.0,
    "restructuring": 15.0,
    "重组": 15.0,
    "institutional_buy": 14.0,
    "机构增持": 14.0,
    "机构调研": 10.0,
    "price_hike": 15.0,
    "涨价": 15.0,
}


def event_alpha_score(events: List[Dict[str, object]], strategy_name: str = "tomorrow_picks") -> Dict[str, object]:
    score = 50.0
    hits = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        event_type = _event_type(event)
        base_weight = EVENT_TYPE_WEIGHTS.get(event_type, EVENT_TYPE_WEIGHTS.get(_normalized_type(event_type), 0.0))
        if base_weight <= 0:
            continue
        confidence = _event_confidence(event)
        horizon_match = event_fit_score(strategy_name, event) / 100.0
        contribution = base_weight * confidence * horizon_match
        if coerce_already_priced_in(event.get("already_priced_in")):
            contribution *= 0.45
        risk_score = coerce_number(event.get("event_risk_score"), 50.0)
        if risk_score > 70:
            contribution -= min(12.0, (risk_score - 70.0) * 0.4)
        if abs(contribution) < 1e-9:
            continue
        score += contribution
        hits.append(
            {
                "type": event_type,
                "contribution": round(contribution, 4),
                "confidence": round(confidence, 4),
                "horizon_match": round(horizon_match, 4),
            }
        )
    score = round(max(0.0, min(100.0, score)), 4)
    min_score = coerce_number(getattr(config, "EVENT_ALPHA_MIN_SCORE", 60.0), 60.0)
    return {
        "event_alpha_score": score,
        "event_alpha_active": score >= min_score and bool(hits),
        "event_count": len([event for event in events or [] if isinstance(event, dict)]),
        "hits": hits,
        "threshold": min_score,
    }


def attach_event_alpha(rows: List[Dict[str, object]], strategy_name: str = "tomorrow_picks") -> None:
    for row in rows or []:
        events = row_event_alpha_events(row)
        result = event_alpha_score(events, strategy_name=strategy_name)
        result["mode"] = "research_only"
        result["trading_enabled"] = False
        row["event_alpha"] = result
        row["event_alpha_score"] = result["event_alpha_score"]


def row_event_alpha_events(row: Dict[str, object]) -> List[Dict[str, object]]:
    if not isinstance(row, dict):
        return []
    explicit = row.get("event_alpha_events") or row.get("events")
    if isinstance(explicit, list):
        return [item for item in explicit if isinstance(item, dict)]
    events = []
    event_type = str(row.get("deepseek_event_type") or row.get("event_type") or "").strip()
    catalyst_score = row.get("deepseek_catalyst_score", row.get("catalyst_score"))
    catalyst_strength = row.get("deepseek_catalyst_strength", row.get("catalyst_strength"))
    if event_type or catalyst_score is not None or catalyst_strength is not None:
        event = {
            "type": event_type or "未知",
            "catalyst_strength": catalyst_strength if catalyst_strength is not None else catalyst_score,
            "catalyst_score": catalyst_score,
            "time_sensitivity": row.get("deepseek_time_sensitivity") or row.get("time_sensitivity"),
            "already_priced_in": row.get("deepseek_already_priced_in", row.get("already_priced_in", False)),
            "event_risk_score": row.get("deepseek_event_risk_score", row.get("event_risk_score")),
        }
        if catalyst_score is not None:
            event["confidence"] = _score_to_confidence(catalyst_score)
        events.append(event)
    for flag in row.get("announcement_flags") or []:
        events.append({"type": str(flag), "confidence": 0.55, "time_sensitivity": "2-5天"})
    return events


def _event_type(event: Dict[str, object]) -> str:
    return str(event.get("type") or event.get("event_type") or "").strip()


def _normalized_type(event_type: str) -> str:
    return str(event_type or "").strip().lower().replace(" ", "_").replace("-", "_")


def _event_confidence(event: Dict[str, object]) -> float:
    if event.get("confidence") is not None:
        return max(0.0, min(1.0, coerce_number(event.get("confidence"), 0.5)))
    for key in ("catalyst_strength", "catalyst_score", "theme_truth_score"):
        if event.get(key) is not None:
            return _score_to_confidence(event.get(key))
    return 0.5


def _score_to_confidence(value) -> float:
    score = coerce_number(value, 50.0)
    if score <= 1.0:
        return max(0.0, min(1.0, score))
    return max(0.0, min(1.0, score / 100.0))
